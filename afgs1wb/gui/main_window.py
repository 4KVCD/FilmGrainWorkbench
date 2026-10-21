"""The AV1 tab: videos -> tune -> write, with a large frame viewer that estimates the grain live.

The frames are loaded by :class:`FrameLoader` (av1_frames.py), Write and
Verify are :class:`Writer` (av1_write.py); this module holds the controls, the
settings and the jobs.
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
import hashlib
import os
import time

import numpy as np
from PySide6.QtCore import QSettings, QStandardPaths, Qt, QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QFileDialog, QFormLayout, QFrame, QGridLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
                               QPushButton, QScrollArea, QSlider, QSpinBox, QSplitter, QToolButton,
                               QVBoxLayout, QWidget)

from .. import display
from ..alignment import detect_offset
from ..fullscan import TimelineModel, analyzer_path, default_threads
from ..frames import Crop, Frame, source_crop_for
from ..grain_params import GrainParams, TableEntry, format_grain_table
from ..probe import VideoInfo, probe
from ..tools import Tools, ToolError, detect_tools
from ..tuning import FramePlan, GrainPlan, TimelinePlan, Tuning, entry_sigmas, sigma_in_8bit_units
from .av1_frames import FrameLoader
from .av1_write import Writer, Written
from .curves import CurvePlot
from .frame_pane import SHORTCUT_HELP, FramePane, StatusBar, install_viewer_shortcuts
from .jobs import Job, running
from .timeline import GrainTimeline
from .tools_dialog import ToolsDialog
from .viewer import GRAIN, GRAIN_ONLY, SOURCE, PreviewState, ViewerWindow

VIDEO_FILTER = "Video files (*.mkv *.mp4 *.mov *.webm *.ts *.m2ts);;All files (*)"
FRAME_DELAY_MS = 120              # a frame loads once stepping pauses this long
TUNE_DELAY_MS = 150               # the grain is synthesised again once a slider pauses
OFFSET_DELAY_MS = 400             # "19" typed into the offset passes through 1 on the way
MAX_STRENGTH_SLIDER = 400         # %, the slider's end; the box beside it goes further

# Before the tabs, the AV1 window's settings were top-level keys; they live under av1/ now.
# tools/* and files/last_dir stay shared by both tabs.
_OLD_KEYS = {"window/splitter": "av1/splitter", "files/source": "av1/source", "files/encoded": "av1/encoded",
             "viewer/geometry": "av1/viewer_geometry", "window/geometry": None}
_OLD_GROUPS = ("tune", "analysis", "offsets")


def migrate_settings(settings: QSettings) -> None:
    """Move settings saved before the tabs to their av1/ keys (once; later calls find nothing)."""
    for old in settings.allKeys():
        if old in _OLD_KEYS:
            new = _OLD_KEYS[old]
        elif old.split("/", 1)[0] in _OLD_GROUPS:
            new = "av1/" + old
        else:
            continue
        if new and not settings.contains(new):
            settings.setValue(new, settings.value(old))
        settings.remove(old)


class MainWindow(FrameLoader, Writer, QMainWindow):
    tools_changed = Signal()                     # the external programs were set up anew

    def __init__(self, settings: QSettings | None = None, data_dir: Path | None = None) -> None:
        super().__init__()
        self.setWindowTitle("AFGS1 Grain Workbench")
        self.settings = settings or QSettings("AFGS1Workbench", "AFGS1Workbench")
        migrate_settings(self.settings)
        self.data_dir = data_dir or Path(QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppDataLocation))
        self.tools = self._load_tools()
        self.source: VideoInfo | None = None
        self.encoded: VideoInfo | None = None
        self.crop: Crop | None = None
        self.model: TimelineModel | None = None          # the every-frame analysis, once there is one
        self.estimates: dict[int, GrainParams] = {}      # live single-frame estimates otherwise
        self.plan: GrainPlan | None = None
        self.written: Written | None = None
        self.job: Job | None = None
        self.frame_job: Job | None = None
        self.model_job: Job | None = None                # loads a saved analysis in the background
        self.pending_index: int | None = None
        self.grain_dirty = False
        self.frame_cache: OrderedDict[tuple, Frame] = OrderedDict()
        self.converter: display.Converter | None = None
        self.current_index = 0
        self.state = PreviewState()
        self.viewer_window: ViewerWindow | None = None
        self._job_started = 0.0
        self._last_requested = 0
        self._detected = ""                  # Detect's result for the current pair and offset
        self._applied_offset = 0             # the offset the frames shown and estimates use
        self._held: tuple[int, dict] | None = None    # a frame waiting for its grain (see _frame_ready)
        self._output_is_auto = True          # the output name follows the encode until one is chosen

        self._build()
        self._install_shortcuts(self)
        self._restore()
        self.frame_timer = QTimer(self, singleShot=True, interval=FRAME_DELAY_MS)
        self.frame_timer.timeout.connect(self._load_current_frame)
        self.tune_timer = QTimer(self, singleShot=True, interval=TUNE_DELAY_MS)
        self.tune_timer.timeout.connect(self._retune)
        self.offset_timer = QTimer(self, singleShot=True, interval=OFFSET_DELAY_MS)
        self.offset_timer.timeout.connect(self._offset_changed)
        QTimer.singleShot(0, self._startup)

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        sidebar = QWidget()
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(12, 12, 12, 12)
        side.setSpacing(10)

        self.tools_banner = QLabel()
        self.tools_banner.setWordWrap(True)
        self.tools_banner.setObjectName("warning")
        self.tools_banner.linkActivated.connect(lambda _link: self.edit_tools())
        side.addWidget(self.tools_banner)
        side.addWidget(self._videos_group())
        side.addWidget(self._tune_group())
        side.addWidget(self._write_group())
        self._add_log(side)
        side.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(sidebar)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setMinimumWidth(430)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.grain_timeline = GrainTimeline()
        self.grain_timeline.frame_clicked.connect(self._go_to)
        self.grain_timeline.hide()
        self.pane = FramePane(self.state, below_view=self.grain_timeline,
                              hint=SHORTCUT_HELP.format(left="encoded", right="encoded + grain"))
        self.pane.frame_requested.connect(self._go_to)
        self.pane.step_requested.connect(self._step)
        self.pane.mode_requested.connect(self._set_mode)
        self.pane.popout_requested.connect(self._open_viewer_window)
        self.view = self.pane.view

        self.splitter = QSplitter()
        self.splitter.addWidget(scroll)
        self.splitter.addWidget(self.pane)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([460, 1100])
        self.setCentralWidget(self.splitter)
        self.status = StatusBar(self, self._cancel_job)

        menu = self.menuBar().addMenu("&Settings")
        menu.addAction("External programs…", self.edit_tools)

        self.state.changed.connect(self._sync_mode_buttons)
        self._update_estimate()
        self._update_enabled()

    def _videos_group(self) -> QGroupBox:
        files = QGroupBox("1 · Videos")
        grid = QGridLayout(files)
        self.source_edit, self.encoded_edit = QLineEdit(), QLineEdit()
        self.source_edit.setPlaceholderText("Original, grainy source (e.g. the Blu-ray remux)")
        self.encoded_edit.setPlaceholderText("VVC encode made from it")
        self.source_info_label, self.encoded_info_label = QLabel(), QLabel()
        for row, (title, edit, info, kind) in enumerate((
                ("Source", self.source_edit, self.source_info_label, "source"),
                ("Encoded VVC", self.encoded_edit, self.encoded_info_label, "encoded"))):
            browse = QPushButton("Browse…")
            browse.clicked.connect(lambda _=False, k=kind: self._browse_video(k))
            edit.editingFinished.connect(lambda k=kind: self._load_video(k))
            info.setObjectName("info")
            info.setWordWrap(True)
            grid.addWidget(QLabel(title), row * 2, 0)
            grid.addWidget(edit, row * 2, 1)
            grid.addWidget(browse, row * 2, 2)
            grid.addWidget(info, row * 2 + 1, 1, 1, 2)
        self.pair_note = QLabel()
        self.pair_note.setWordWrap(True)
        self.pair_note.setObjectName("info")
        offset_row = QHBoxLayout()
        self.offset_spin = QSpinBox(minimum=-100000, maximum=100000, value=0)
        self.offset_spin.setToolTip("Encoded frame N shows source frame N + offset. Leave at 0 unless "
                                    "the encode was trimmed or has extra frames.")
        self.offset_spin.valueChanged.connect(lambda _: self.offset_timer.start())
        self.detect_button = QPushButton("Detect")
        self.detect_button.setToolTip("Find the offset by matching frames at three places.")
        self.detect_button.clicked.connect(self._detect_offset)
        offset_row.addWidget(self.offset_spin, 1)
        offset_row.addWidget(self.detect_button)
        grid.addWidget(QLabel("Frame offset"), 4, 0)
        grid.addLayout(offset_row, 4, 1, 1, 2)
        self.offset_note = QLabel()
        self.offset_note.setWordWrap(True)
        self.offset_note.setObjectName("info")
        self.offset_note.setToolTip("How closely the source frame paired with the encoded frame shown "
                                    "matches it (PSNR of the luma). The same frame matches far better "
                                    "than its neighbours; a low value usually means a wrong offset.")
        grid.addWidget(self.offset_note, 5, 1, 1, 2)
        grid.addWidget(self.pair_note, 6, 0, 1, 3)
        return files

    def _tune_group(self) -> QGroupBox:
        tune = QGroupBox("2 · Tune grain")
        # Rows with a label sit in a form; everything else in a plain column,
        # where word-wrapped text gets the height it needs (a form row clips it).
        tcol = QVBoxLayout(tune)
        self.plot = CurvePlot()
        tcol.addWidget(self.plot)
        tcol.addSpacing(8)
        tform = QFormLayout()
        tcol.addLayout(tform)
        self.luma_slider, self.luma_spin = self._strength_row(tform, "Luma strength",
                                                              "Scales the luma grain the encode lost. "
                                                              "100 % = as measured.")
        self.chroma_slider, self.chroma_spin = self._strength_row(tform, "Chroma strength",
                                                                  "Scales the colour grain. 0 % = none.")
        self.seed_spin = QSpinBox(minimum=0, maximum=65535, value=7391)
        self.seed_spin.setToolTip("Base random seed. Every frame derives its own seed from it, so the "
                                  "grain moves; change it only to get a different pattern.")
        self.seed_spin.valueChanged.connect(self._tuning_changed)
        tform.addRow("Seed", self.seed_spin)
        self.overlap_check = QCheckBox("Blend grain block edges (on in libaom)")
        self.overlap_check.setChecked(True)
        self.overlap_check.setToolTip("overlap_flag: blend the 32×32 grain blocks where they meet. libaom's "
                                      "noise model always turns it on.")
        self.overlap_check.toggled.connect(self._tuning_changed)
        self.clip_check = QCheckBox("Clip grained picture to limited (TV) range (off in libaom)")
        self.clip_check.setChecked(False)
        self.clip_check.setToolTip("clip_to_restricted_range: clip grained samples to 16–235 (chroma "
                                   "16–240), scaled to the bit depth. libaom's noise model never sets it, "
                                   "so grain in the darkest and brightest areas is not cut off.")
        self.clip_check.toggled.connect(self._tuning_changed)
        tcol.addWidget(self.overlap_check)
        tcol.addWidget(self.clip_check)
        self.tune_label = QLabel()
        self.tune_label.setWordWrap(True)
        self.tune_label.setObjectName("info")
        tcol.addWidget(self.tune_label)
        model_row = QHBoxLayout()
        reset = QPushButton("Reset")
        reset.setToolTip("Back to 100 % luma and chroma strength.")
        reset.clicked.connect(self._reset_tuning)
        self.export_table_button = QPushButton("Export grain table…")
        self.export_table_button.setToolTip("Save the tuned parameters as an AOM film grain table "
                                            "(usable with aomenc / SVT-AV1 --fgs-table).")
        self.export_table_button.clicked.connect(self._export_table)
        for w in (reset, self.export_table_button):
            model_row.addWidget(w)
        tcol.addLayout(model_row)
        return tune

    def _write_group(self) -> QGroupBox:
        write = QGroupBox("3 · Write")
        wgrid = QGridLayout(write)
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("Output .mkv")
        out_browse = QPushButton("Browse…")
        out_browse.clicked.connect(self._browse_output)
        wgrid.addWidget(QLabel("Output"), 0, 0)
        wgrid.addWidget(self.output_edit, 0, 1)
        wgrid.addWidget(out_browse, 0, 2)
        run_row = QHBoxLayout()
        run_row.setContentsMargins(0, 0, 0, 0)
        self.workers_spin = QSpinBox(minimum=1, maximum=128, value=default_threads())
        self.workers_spin.setToolTip("Threads for the every-frame grain analysis. All CPU threads is "
                                     "fastest; use fewer to keep the PC responsive while it runs.")
        self.gpu_check = QCheckBox("Decode source on GPU")
        self.gpu_check.setChecked(True)
        self.gpu_check.setToolTip("Let ffmpeg decode the source with hardware acceleration (bit-exact) "
                                  "to leave the CPU to the grain analysis.")
        run_row.addWidget(self.workers_spin)
        run_row.addWidget(QLabel("threads"))
        run_row.addWidget(self.gpu_check)
        run_row.addStretch(1)
        wgrid.addWidget(QLabel("Analysis"), 1, 0)
        wgrid.addLayout(run_row, 1, 1, 1, 2)
        self.workers_spin.valueChanged.connect(self._update_estimate)
        self.write_estimate_label = QLabel()
        self.write_estimate_label.setWordWrap(True)
        self.write_estimate_label.setObjectName("info")
        wgrid.addWidget(self.write_estimate_label, 2, 0, 1, 3)
        self.analysis_label = QLabel()
        self.analysis_label.setWordWrap(True)
        self.analysis_label.setObjectName("info")
        self.analysis_label.hide()
        wgrid.addWidget(self.analysis_label, 6, 0, 1, 3)
        self.write_button = QPushButton("Write MKV with grain metadata")
        self.write_button.setProperty("primary", True)
        self.write_button.clicked.connect(self._write)
        wgrid.addWidget(self.write_button, 3, 0, 1, 3)
        self.verify_button = QPushButton("Verify with ffmpeg")
        self.verify_button.setToolTip("Decode the current frame of the written file with ffmpeg and "
                                      "compare it to the preview.")
        self.verify_button.clicked.connect(self._verify)
        self.open_folder_button = QPushButton("Open folder")
        self.open_folder_button.clicked.connect(self._open_folder)
        wgrid.addWidget(self.verify_button, 4, 0, 1, 2)
        wgrid.addWidget(self.open_folder_button, 4, 2)
        self.write_label = QLabel()
        self.write_label.setWordWrap(True)
        self.write_label.setObjectName("info")
        wgrid.addWidget(self.write_label, 5, 0, 1, 3)
        return write

    def _add_log(self, column: QVBoxLayout) -> None:
        """The activity log, folded away under its button."""
        self.log_toggle = QToolButton(text="Activity log", checkable=True)
        self.log_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.log_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.log_toggle.toggled.connect(self._show_log)
        self.log_view = QPlainTextEdit(readOnly=True)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setMinimumHeight(140)
        self.log_view.hide()
        column.addWidget(self.log_toggle)
        column.addWidget(self.log_view)

    def _show_log(self, shown: bool) -> None:
        self.log_view.setVisible(shown)
        self.log_toggle.setArrowType(Qt.ArrowType.DownArrow if shown else Qt.ArrowType.RightArrow)

    def _strength_row(self, form: QFormLayout, title: str, tip: str) -> tuple[QSlider, QSpinBox]:
        row = QHBoxLayout()
        slider = QSlider(Qt.Orientation.Horizontal, minimum=0, maximum=MAX_STRENGTH_SLIDER, value=100)
        slider.setPageStep(10)
        spin = QSpinBox(minimum=0, maximum=800, value=100, suffix=" %")
        for w in (slider, spin):
            w.setToolTip(tip)
        slider.valueChanged.connect(spin.setValue)
        spin.valueChanged.connect(lambda value: slider.setValue(min(value, MAX_STRENGTH_SLIDER)))
        spin.valueChanged.connect(self._tuning_changed)
        row.addWidget(slider, 1)
        row.addWidget(spin)
        form.addRow(title, row)
        return slider, spin

    def _install_shortcuts(self, widget: QWidget) -> None:
        """The viewer's keys on the tab or on the frame window."""
        view = self.viewer_window.view if widget is self.viewer_window else self.view
        install_viewer_shortcuts(widget, view, self._set_mode, self._step, self._frames_per_second)

    # ------------------------------------------------------------------ settings
    def _load_tools(self) -> Tools:
        found = detect_tools()
        tools = Tools()
        for name in Tools.LABELS:
            saved = self.settings.value(f"tools/{name}", "")
            setattr(tools, name, saved if saved and Path(saved).is_file() else getattr(found, name))
        return tools

    def _restore(self) -> None:
        s = self.settings
        if s.value("av1/splitter"):
            self.splitter.restoreState(s.value("av1/splitter"))
        self.luma_spin.setValue(int(s.value("av1/tune/luma", 100)))
        self.chroma_spin.setValue(int(s.value("av1/tune/chroma", 100)))
        self.seed_spin.setValue(int(s.value("av1/tune/seed", 7391)))
        self.overlap_check.setChecked(s.value("av1/tune/overlap", "true") in (True, "true"))
        self.clip_check.setChecked(s.value("av1/tune/clip", "false") in (True, "true"))
        self.workers_spin.setValue(int(s.value("av1/analysis/threads", default_threads())))
        self.gpu_check.setChecked(s.value("av1/analysis/gpu", "true") in (True, "true"))
        self._update_estimate()

    def _save(self) -> None:
        s = self.settings
        s.setValue("av1/splitter", self.splitter.saveState())
        for name in Tools.LABELS:
            s.setValue(f"tools/{name}", getattr(self.tools, name))
        s.setValue("av1/source", self.source_edit.text())
        s.setValue("av1/encoded", self.encoded_edit.text())
        s.setValue("av1/analysis/threads", self.workers_spin.value())
        s.setValue("av1/analysis/gpu", self.gpu_check.isChecked())
        s.setValue("av1/tune/luma", self.luma_spin.value())
        s.setValue("av1/tune/chroma", self.chroma_spin.value())
        s.setValue("av1/tune/seed", self.seed_spin.value())
        s.setValue("av1/tune/overlap", self.overlap_check.isChecked())
        s.setValue("av1/tune/clip", self.clip_check.isChecked())

    def _startup(self) -> None:
        self._update_tools_banner()
        self.source_edit.setText(self.settings.value("av1/source", ""))
        self.encoded_edit.setText(self.settings.value("av1/encoded", ""))
        if self.source_edit.text():
            self._load_video("source", quiet=True)
        if self.encoded_edit.text():
            self._load_video("encoded", quiet=True)

    def edit_tools(self) -> None:
        dialog = ToolsDialog(self.tools, self)
        if dialog.exec():
            self.tools = dialog.tools()
            self._save()
            self._update_tools_banner()
            self._update_enabled()
            self.tools_changed.emit()

    def _update_tools_banner(self) -> None:
        missing = self.tools.missing()
        self.tools_banner.setVisible(bool(missing))
        if missing:
            self.tools_banner.setText(f"Missing: {', '.join(missing)}. "
                                      "<a href='#'>Set up external programs…</a>")

    # ------------------------------------------------------------------ videos
    def _browse_video(self, kind: str) -> None:
        edit = self.source_edit if kind == "source" else self.encoded_edit
        start = edit.text() or self.settings.value("files/last_dir", "")
        path, _ = QFileDialog.getOpenFileName(self, f"Choose the {'source' if kind == 'source' else 'encoded VVC'} video",
                                              start, VIDEO_FILTER)
        if path:
            edit.setText(os.path.normpath(path))
            self.settings.setValue("files/last_dir", str(Path(path).parent))
            self._load_video(kind)

    def _load_video(self, kind: str, quiet: bool = False) -> None:
        edit = self.source_edit if kind == "source" else self.encoded_edit
        label = self.source_info_label if kind == "source" else self.encoded_info_label
        path = edit.text().strip().strip('"')
        current = self.source if kind == "source" else self.encoded
        if current and str(current.path) == path:
            return
        info = None
        if path:
            if self.tools.missing("ffprobe"):
                label.setText("Set up ffprobe first (Settings → External programs).")
            else:
                self.setCursor(Qt.CursorShape.WaitCursor)
                try:
                    info = probe(path, self.tools.ffprobe)
                    label.setText(info.describe())
                    label.setStyleSheet("")
                except (ToolError, OSError, ValueError) as exc:
                    label.setText(str(exc).splitlines()[0])
                    label.setStyleSheet("color: #ff7b72;")
                finally:
                    self.unsetCursor()
        else:
            label.setText("")
        if kind == "source":
            self.source = info
        else:
            self.encoded = info
            if info and info.codec != "vvc":
                label.setText(label.text() + "  —  not VVC: grain metadata can only be written into VVC.")
                label.setStyleSheet("color: #e3b341;")
        self._pair_changed()
        self._save()

    def _pair_changed(self) -> None:
        self.model = self.plan = self.written = None
        self.estimates = {}
        self.frame_cache.clear()
        self.crop = None
        notes = []
        src, enc = self.source, self.encoded
        if enc:
            self.converter = display.Converter(display.DisplayFormat(
                enc.bit_depth, enc.limited_range, enc.matrix, enc.transfer, enc.primaries,
                enc.width, enc.height))
            self.pane.set_frame_count(enc.frame_count)
            if not self.output_edit.text() or self._output_is_auto:
                self.output_edit.setText(str(enc.path.with_name(enc.path.stem + " AFGS1.mkv")))
                self._output_is_auto = True
        if src and enc:
            self.crop = source_crop_for(src, enc)
            if self.crop:
                notes.append(f"Source is cropped to {self.crop.width}×{self.crop.height} "
                             f"(offset {self.crop.x},{self.crop.y}) to match the encode's picture.")
            cw, ch = (self.crop.width, self.crop.height) if self.crop else (src.width, src.height)
            if (cw, ch) != (enc.width, enc.height):
                notes.append(f"Source is scaled from {cw}×{ch} to {enc.width}×{enc.height} for comparison; "
                             "grain measured on a scaled source is only approximate.")
            if abs(float(src.fps) - float(enc.fps)) > 0.01:
                notes.append(f"⚠ Frame rates differ ({float(src.fps):.3f} vs {float(enc.fps):.3f} fps): "
                             "frames cannot be matched one-to-one.")
            if abs(src.duration - enc.duration) > 2:
                notes.append(f"Durations differ by {abs(src.duration - enc.duration):.1f} s — "
                             "check the frame offset (Detect).")
        self.pair_note.setText("\n".join(notes))
        self.pair_note.setVisible(bool(notes))
        self._detected = ""
        self.offset_note.setText("")
        if src and enc:
            # Each pair keeps its own offset; a new pair starts at 0, not at the last pair's.
            self.offset_timer.stop()
            self.offset_spin.blockSignals(True)
            self.offset_spin.setValue(int(self.settings.value(f"av1/offsets/{self._pair_key()}", 0)))
            self.offset_spin.blockSignals(False)
        self._applied_offset = self.offset_spin.value()
        self._clear_model_ui()
        self._rebuild_plan()
        if src and enc:
            self._autoload_model()
        if enc:
            self._go_to(min(self.current_index, enc.frame_count - 1) if self.current_index else enc.frame_count // 3,
                        force=True)
        else:
            self.state.clear("Choose a source and an encoded video to see frames.")
        self._update_estimate()
        self._update_enabled()

    def _pair_key(self) -> str:
        return hashlib.sha1(f"{self.source.path}|{self.encoded.path}".encode()).hexdigest()[:16]

    # ------------------------------------------------------------------ enabling
    def _busy(self) -> bool:
        return running(self.job)

    def _update_enabled(self) -> None:
        busy = self._busy()
        pair = bool(self.source and self.encoded)
        for w in (self.source_edit, self.encoded_edit, self.offset_spin, self.workers_spin, self.gpu_check):
            w.setEnabled(not busy)
        self.detect_button.setEnabled(pair and not busy)
        blocker = self._write_blocker()
        self.write_button.setEnabled(not blocker and not busy)
        self.write_button.setToolTip(blocker or "Analyses every frame (or reuses a finished analysis), then "
                                                "writes the MKV with the strength set above.")
        self.verify_button.setEnabled(bool(self.written) and not busy)
        self.open_folder_button.setEnabled(bool(self.written))
        self.export_table_button.setEnabled(self.model is not None)
        self._sync_mode_buttons()

    def _write_blocker(self) -> str:
        """Why Write cannot run now, or "" when it can."""
        if not (self.source and self.encoded):
            return "Choose the source and the encoded video first."
        if self.encoded.codec != "vvc":
            return "The encode is not VVC."
        if not analyzer_path().is_file():
            return "The grain analyzer is not built: run analyzer\\build.py."
        if self.tools.missing("ffmpeg", "mkvmerge"):
            return "Needs ffmpeg and mkvmerge from MKVToolNix (Settings → External programs)."
        return ""

    # ------------------------------------------------------------------ jobs
    def _start_job(self, name: str, fn, on_success, busy_text: str) -> None:
        if self._busy():
            return
        self.job = Job(name, fn, self)
        self.job.progress.connect(self._job_progress)
        self.job.succeeded.connect(on_success)
        self.job.failed.connect(lambda msg, n=name: self._job_failed(n, msg))
        self.job.cancelled.connect(lambda n=name: self._job_cancelled(n))
        self.job.finished.connect(self._job_finished)
        self._job_started = time.perf_counter()
        self.status.label.setText(busy_text)
        self.status.busy(0.0)
        self.log(busy_text)
        self.job.start()
        self._update_enabled()

    def _job_progress(self, fraction: float, message: str) -> None:
        self.status.busy(fraction)
        elapsed = time.perf_counter() - self._job_started
        self.status.label.setText(f"{message}  ({elapsed:.0f} s)")

    def _job_finished(self) -> None:
        self.status.idle()
        self._update_enabled()

    def _job_failed(self, name: str, message: str) -> None:
        self.status.label.setText(f"{name} failed.")
        self.log(f"{name} failed: {message}")
        QMessageBox.warning(self, f"{name} failed", message)

    def _job_cancelled(self, name: str) -> None:
        self.status.label.setText(f"{name} cancelled.")
        self.log(f"{name} cancelled.")

    def _cancel_job(self) -> None:
        if self._busy():
            self.status.cancel_button.setEnabled(False)
            self.status.label.setText("Cancelling…")
            self.job.cancel()

    def log(self, text: str) -> None:
        self.log_view.appendPlainText(time.strftime("%H:%M:%S  ") + text)

    # ------------------------------------------------------------------ analysis
    def _detect_offset(self) -> None:
        src, enc, crop, tools = self.source, self.encoded, self.crop, self.tools

        def work(token, progress):
            progress(-1, "Matching frames at three places to find the offset…")
            return detect_offset(tools, src, enc, crop, token)

        def done(result):
            offset, confidence = result
            self.offset_spin.blockSignals(True)
            self.offset_spin.setValue(offset)
            self.offset_spin.blockSignals(False)
            quality = "confident" if confidence > 0.5 else "not certain — check a few frames"
            text = f"Detected offset {offset:+d} frames ({quality})."
            self.status.label.setText(text)
            self.log(text)
            self.offset_timer.stop()
            self._offset_changed()
            self._detected = text           # kept on screen: the frame loads that follow update the status bar
            self.offset_note.setText(text)

        self._start_job("Offset detection", work, done, "Detecting the frame offset…")

    def _offset_changed(self) -> None:
        """Other source frames are paired with the encode now: estimate and show them again."""
        if self.offset_spin.value() == self._applied_offset:
            return                       # e.g. "19" retyped over 19 passes through 1 on the way
        self._applied_offset = self.offset_spin.value()
        if self.source and self.encoded:
            self.settings.setValue(f"av1/offsets/{self._pair_key()}", self.offset_spin.value())
        self._detected = ""
        self.estimates.clear()
        self._rebuild_plan()
        for key in [k for k in self.frame_cache if k[0] == "src"]:
            del self.frame_cache[key]
        self._update_estimate()
        self._go_to(self.current_index, force=True)

    def _set_model(self, model: TimelineModel, loaded_from: str | None = None) -> None:
        """Use an every-frame analysis for the preview (it is what Write puts in the file)."""
        self.model = model
        self.written = None
        scale = 1 << (model.bit_depth - 8)
        h, rem = divmod(int(model.seconds), 3600)
        took = (f" {h} h {rem // 60:02d} min" if model.seconds >= 3600 else
                f" {model.seconds / 60:.0f} min" if model.seconds >= 60 else
                f" {model.seconds:.0f} s" if model.seconds else "")
        if took:
            took = (" The analysis took" + took
                    + (" after resuming (not counting the part before)." if model.notes.get("resumed") else "."))
        values = np.array(model.sigma_values or [0.0]) / scale
        text = (f"Every frame analysed: {model.frames:,} frames, {len(model.segments):,} parameter "
                f"changes. Grain σ at mid-grey across the film: median {np.median(values):.2f}, "
                f"5–95 % {np.percentile(values, 5):.2f}–{np.percentile(values, 95):.2f} (8-bit).{took}")
        if model.alignment.count("shifted"):
            text += (f" ⚠ {model.alignment.count('shifted')} of {len(model.alignment)} alignment "
                     "checks matched a neighbouring frame better (check the offset).")
        for warning in model.notes.get("warnings", []):
            text += f" ⚠ {warning}"
        if loaded_from:
            text = f"Loaded from {loaded_from}. " + text
        self.analysis_label.setText(text)
        self.analysis_label.show()
        self.grain_timeline.show()
        self._retune()
        self._update_enabled()

    def _clear_model_ui(self) -> None:
        self.grain_timeline.hide()
        self.analysis_label.hide()
        self.plot.clear("The grain of the frame shown is estimated when it loads.")
        self.tune_label.setText("")
        self.write_label.setText("")
        self.state.update_images({GRAIN: None, GRAIN_ONLY: None})

    # model cache: reopening the same pair restores its analysis
    def _cache_path(self) -> Path | None:
        if not (self.source and self.encoded):
            return None
        stat = self.encoded.path.stat()
        key = f"{self.source.path}|{self.encoded.path}|{stat.st_size}|{int(stat.st_mtime)}"
        folder = self.data_dir / "models"
        return folder / (hashlib.sha1(key.encode()).hexdigest()[:16] + ".json")

    def _model_cache_file(self) -> Path | None:
        path = self._cache_path()
        return path.with_suffix(".every-frame.json") if path else None

    def _autoload_model(self) -> None:
        """Load this pair's saved every-frame analysis in the background.

        A whole-film analysis is ~150,000 parameter sets and takes a few
        seconds to read; the window stays usable meanwhile, showing the live
        per-frame estimates until it arrives.
        """
        candidate = self._model_cache_file()
        if not candidate or not candidate.is_file():
            return
        pair = (str(self.source.path), str(self.encoded.path))
        self.analysis_label.setText("Loading the saved every-frame analysis of these files…")
        self.analysis_label.show()

        def work(token, progress):
            return TimelineModel.from_json(candidate.read_text(encoding="utf-8"))

        job = Job("Loading the saved analysis", work, self)
        job.succeeded.connect(lambda model: self._model_loaded(model, pair))
        job.failed.connect(lambda msg: self._model_loaded(None, pair, msg))
        self.model_job = job
        job.start()

    def _model_loaded(self, model: TimelineModel | None, pair: tuple[str, str], error: str = "") -> None:
        still_here = bool(self.source and self.encoded) and \
            (str(self.source.path), str(self.encoded.path)) == pair
        if not still_here or self.model is not None:     # another pair now, or a Write finished first
            return
        if model is None or model.kind != "every-frame":
            self.log(f"The saved analysis could not be read: {error or 'not an every-frame analysis'}")
            self.analysis_label.hide()
            return
        self._set_model(model, loaded_from="the previous every-frame analysis")

    def _export_table(self) -> None:
        if not self.plan:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export AOM film grain table", "grain.tbl",
                                              "Film grain table (*.tbl *.txt)")
        if not path:
            return
        entries = [TableEntry(e.start, e.end, self.plan.params_at(seg.start))
                   for e, seg in zip(self.model.grain_table(), self.model.segments)]
        Path(path).write_text(format_grain_table(entries), encoding="ascii")
        self.log(f"Exported grain table ({len(entries):,} entries) to {path}")

    # ------------------------------------------------------------------ tuning
    def _tuning(self) -> Tuning:
        return Tuning(self.luma_spin.value() / 100, self.chroma_spin.value() / 100,
                      self.overlap_check.isChecked(), self.clip_check.isChecked(), self.seed_spin.value())

    def _tuning_changed(self, *_) -> None:
        if self.encoded:
            self.tune_timer.start()

    def _reset_tuning(self) -> None:
        self.luma_spin.setValue(100)
        self.chroma_spin.setValue(100)

    def _rebuild_plan(self) -> None:
        """Every-frame parameters when analysed, else the live per-frame estimates, at the set strength."""
        enc, tuning = self.encoded, self._tuning()
        if self.model:
            self.plan = TimelinePlan(self.model, tuning)
        elif enc:
            self.plan = FramePlan(self.estimates, tuning, (enc.subsampling_x, enc.subsampling_y) == (1, 1))
        else:
            self.plan = None

    def _retune(self) -> None:
        if not self.encoded:
            return
        self._rebuild_plan()
        tuning, m = self._tuning(), self.model
        if m:
            scale = 1 << (m.bit_depth - 8)
            self.grain_timeline.set_data(m.frames, np.array(m.sigma_frames),
                                         np.array(m.sigma_values) / scale * tuning.luma_strength,
                                         float(m.fps))
            self.grain_timeline.set_current(self.current_index)
        self._update_segment_plot()
        self._save()
        self._render_grain()

    def _update_segment_plot(self) -> None:
        """Brightness curves of the parameters in use at the current frame."""
        if not (self.plan and self.encoded):
            return
        enc, tuning, index = self.encoded, self._tuning(), self.current_index
        params = self.plan.params_at(index)
        if params is None:
            self.plot.clear("Estimating the grain of this frame…" if self.source
                            else "Choose the source video to estimate the grain.")
            self.tune_label.setText("")
            return
        y, cb, cr = entry_sigmas(params, enc.bit_depth, enc.subsampling_x, enc.subsampling_y)
        to8 = lambda a: sigma_in_8bit_units(a, enc.bit_depth)
        mid = float(to8(y)[128]) if params.apply_grain and params.y_points else 0.0
        self.plot.set_data(to8(y), to8(cb), to8(cr))
        strength = f"{tuning.luma_strength * 100:.0f} %"
        if self.model:
            segment = self.model.segment_at(index)
            fps = float(self.model.fps)
            span = segment.end - segment.start
            # libaom's model moves on almost every frame, so most frames have their own parameters.
            whose = ("the every-frame parameters measured for this frame" if span == 1 else
                     f"every-frame parameters shared by frames {segment.start:,}–{segment.end - 1:,}, "
                     f"{span / fps:.1f} s")
            text = (f"At this frame ({whose}): luma grain σ {mid:.2f} at mid-grey (8-bit) with "
                    f"{strength} strength.")
            clipped = self.plan.clipped
        elif not params.apply_grain:
            text = "No grain estimate for this frame: it has too few flat areas to measure."
            clipped = False
        else:
            text = (f"Estimated from this frame: luma grain σ {mid:.2f} at mid-grey (8-bit) with {strength} "
                    "strength. Writing analyses every frame the same way, carrying the estimate "
                    "through each shot like libaom's encoder.")
            clipped = self.plan.clipped_at(index)
        if clipped:
            text += " ⚠ strength capped by the format's maximum."
        self.tune_label.setText(text)

    # ------------------------------------------------------------------ frames
    # ------------------------------------------------------------------ view modes
    def _set_mode(self, mode: int) -> None:
        if not self.state.set_mode(mode):
            reason = {SOURCE: "Choose the source video to see it here.",
                      GRAIN: "The grain appears once this frame's estimate is ready.",
                      GRAIN_ONLY: "The grain appears once this frame's estimate is ready."}.get(mode, "")
            if reason:
                self.status.label.setText(reason)
        self._sync_mode_buttons()

    def _sync_mode_buttons(self) -> None:
        """Views the frame has, and the grain views whenever there is a pair: they follow the frame."""
        pair = bool(self.source and self.encoded)
        self.pane.sync_modes(set(self.state.images) | ({GRAIN, GRAIN_ONLY} if pair else set()))

    def _open_viewer_window(self) -> None:
        if self.viewer_window is None:
            self.viewer_window = ViewerWindow(self.state)
            self._install_shortcuts(self.viewer_window)
            geometry = self.settings.value("av1/viewer_geometry")
            if geometry:
                self.viewer_window.restoreGeometry(geometry)
        self.viewer_window.show_sized()

    # ------------------------------------------------------------------ writing
    # ------------------------------------------------------------------ close
    def can_close(self) -> bool:
        """Ask before closing would cancel a running job (a Write can take hours)."""
        if not self._busy():
            return True
        extra = (" The grain analysis so far is kept; the next Write continues from there."
                 if self.job.name == "Writing" else "")
        answer = QMessageBox.question(self, "Work in progress",
                                      f"{self.job.name} is still running. Cancel it and quit?{extra}")
        return answer == QMessageBox.StandardButton.Yes

    def shutdown(self) -> None:
        """Stop every job and save the settings; nothing is asked any more."""
        for job, seconds in ((self.job, 10), (self.frame_job, 5)):
            if running(job):
                job.cancel()
                job.wait(seconds * 1000)
        if running(self.model_job):
            self.model_job.wait(15000)           # reading a saved analysis cannot be interrupted
        if self.viewer_window:
            self.settings.setValue("av1/viewer_geometry", self.viewer_window.saveGeometry())
            self.viewer_window.close()
        self._save()

    def closeEvent(self, event) -> None:
        if not self.can_close():
            event.ignore()
            return
        self.shutdown()
        event.accept()
