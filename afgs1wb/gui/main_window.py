"""Main window: videos -> analyse -> tune -> write, with a large frame viewer."""

from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
import hashlib
import os
import time

import numpy as np
from PySide6.QtCore import QSettings, QStandardPaths, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QImage, QKeySequence, QShortcut
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFormLayout,
                               QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar,
                               QPushButton, QRadioButton, QScrollArea, QSizePolicy, QSlider, QSpinBox,
                               QSplitter, QToolButton, QVBoxLayout, QWidget)

from .. import display, synth
from ..afgs1 import picture_seed
from ..analysis import AnalysisSettings, GrainModel, analyze, detect_offset, entry_sigmas
from ..fullscan import (TimelineModel, analyze_every_frame, analyzer_path, cache_key,
                        checkpoint_frame, default_threads, estimate_seconds, quick_timeline)
from ..frames import Crop, Frame, Geometry, read_frame, read_frames, source_crop_for
from ..grain_params import TableEntry, format_grain_table
from ..mux import WriteReport, write_afgs1_mkv
from ..probe import VideoInfo, probe
from ..tools import Tools, ToolError, detect_tools, keep_awake
from ..tuning import GrainPlan, TimelinePlan, Tuning, build_params, sigma_in_8bit_units
from .curves import CurvePlot
from .jobs import Job
from .timeline import GrainTimeline
from .tools_dialog import ToolsDialog
from .viewer import ENCODED, GRAIN, GRAIN_ONLY, SOURCE, VIEW_NAMES, FrameView, PreviewState, ViewerWindow

PREFETCH = 4                      # frames decoded per seek while browsing
CACHE_BYTES = 800 * 1024 * 1024   # decoded frames kept for instant back/forth stepping
VIDEO_FILTER = "Video files (*.mkv *.mp4 *.mov *.webm *.ts *.m2ts);;All files (*)"
SHORTCUT_HELP = ("← → encoded / encoded + grain · 1–4 choose view · , . previous / next frame · "
                 "PgUp PgDn ±1 s · wheel or + − zoom · 0 = 100 % · F fit · drag to pan")


def _qimage(rgb: np.ndarray) -> QImage:
    rgb = np.ascontiguousarray(rgb)
    h, w, _ = rgb.shape
    return QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()


def _timecode(seconds: float) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:06.3f}" if h else f"{int(m)}:{s:06.3f}"


@dataclass
class Written:
    report: WriteReport
    params: object
    seed: int


class MainWindow(QMainWindow):
    def __init__(self, settings: QSettings | None = None, data_dir: Path | None = None) -> None:
        super().__init__()
        self.setWindowTitle("AFGS1 Grain Workbench")
        self.settings = settings or QSettings("AFGS1Workbench", "AFGS1Workbench")
        self.data_dir = data_dir or Path(QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppDataLocation))
        self.tools = self._load_tools()
        self.source: VideoInfo | None = None
        self.encoded: VideoInfo | None = None
        self.crop: Crop | None = None
        self.model: TimelineModel | None = None
        self.plan: GrainPlan | None = None
        self.written: Written | None = None
        self.job: Job | None = None
        self.frame_job: Job | None = None
        self.pending_index: int | None = None
        self.grain_dirty = False
        self.frame_cache: OrderedDict[tuple, Frame] = OrderedDict()
        self.converter: display.Converter | None = None
        self.current_index = 0
        self.state = PreviewState()
        self.viewer_window: ViewerWindow | None = None
        self._job_started = 0.0
        self._last_requested = 0

        self._build()
        self._install_shortcuts(self, self.view)
        self._restore()
        self.frame_timer = QTimer(self, singleShot=True, interval=120)
        self.frame_timer.timeout.connect(lambda: self._request_frame(self.current_index))
        self.tune_timer = QTimer(self, singleShot=True, interval=150)
        self.tune_timer.timeout.connect(self._retune)
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
        self.tools_banner.linkActivated.connect(lambda _: self._edit_tools())
        side.addWidget(self.tools_banner)

        # 1 videos
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
        grid.addWidget(self.pair_note, 4, 0, 1, 3)
        side.addWidget(files)

        # 2 analyse
        analysis = QGroupBox("2 · Analyse grain")
        form = QFormLayout(analysis)
        mode_row = QHBoxLayout()
        self.quick_radio = QRadioButton("Quick sample")
        self.full_radio = QRadioButton("Every frame")
        self.quick_radio.setToolTip("Sample a few places and apply one grain setting to the whole video. "
                                    "About a minute; for previewing, not for the final file.")
        self.full_radio.setToolTip("Analyse every frame with AOM noise_model, like libaom's encoder: the "
                                   "grain parameters follow the film shot by shot. Takes hours for a 4K "
                                   "film; can be stopped and resumed.")
        self.full_radio.setChecked(True)
        for radio in (self.quick_radio, self.full_radio):
            radio.toggled.connect(self._mode_changed)
            mode_row.addWidget(radio)
        mode_row.addStretch(1)
        form.addRow("Mode", mode_row)
        runs_row = QHBoxLayout()
        self.runs_spin = QSpinBox(minimum=2, maximum=64, value=16)
        self.frames_spin = QSpinBox(minimum=3, maximum=24, value=8)
        self.runs_spin.setToolTip("How many places in the video to sample. More places = a more "
                                  "representative model, longer analysis.")
        self.frames_spin.setToolTip("Consecutive frames decoded at each place. Decoding a run costs "
                                    "about the same as decoding one frame, so 8 is cheap.")
        runs_row.addWidget(self.runs_spin)
        runs_row.addWidget(QLabel("places ×"))
        runs_row.addWidget(self.frames_spin)
        runs_row.addWidget(QLabel("frames"))
        runs_row.addStretch(1)
        self.quick_options = QWidget()
        self.quick_options.setLayout(runs_row)
        runs_row.setContentsMargins(0, 0, 0, 0)
        form.addRow("Sample", self.quick_options)
        self.sample_label = form.labelForField(self.quick_options)
        full_row = QHBoxLayout()
        full_row.setContentsMargins(0, 0, 0, 0)
        self.workers_spin = QSpinBox(minimum=1, maximum=128, value=default_threads())
        self.workers_spin.setToolTip("Threads for the grain analysis. All CPU threads is fastest; use "
                                     "fewer to keep the PC responsive while it runs.")
        self.gpu_check = QCheckBox("Decode source on GPU")
        self.gpu_check.setChecked(True)
        self.gpu_check.setToolTip("Let ffmpeg decode the source with hardware acceleration (bit-exact) "
                                  "to leave the CPU to noise_model.")
        full_row.addWidget(self.workers_spin)
        full_row.addWidget(QLabel("threads"))
        full_row.addWidget(self.gpu_check)
        full_row.addStretch(1)
        self.full_options = QWidget()
        self.full_options.setLayout(full_row)
        form.addRow("Run", self.full_options)
        self.run_label = form.labelForField(self.full_options)
        self.workers_spin.valueChanged.connect(self._update_estimate)
        self.estimate_label = QLabel()
        self.estimate_label.setWordWrap(True)
        self.estimate_label.setObjectName("info")
        form.addRow("", self.estimate_label)
        offset_row = QHBoxLayout()
        self.offset_spin = QSpinBox(minimum=-100000, maximum=100000, value=0)
        self.offset_spin.setToolTip("Encoded frame N shows source frame N + offset. Leave at 0 unless "
                                    "the encode was trimmed or has extra frames.")
        self.detect_button = QPushButton("Detect")
        self.detect_button.setToolTip("Find the offset by matching frames at three places.")
        self.detect_button.clicked.connect(self._detect_offset)
        offset_row.addWidget(self.offset_spin, 1)
        offset_row.addWidget(self.detect_button)
        form.addRow("Frame offset", offset_row)
        self.analyse_button = QPushButton("Analyse grain")
        self.analyse_button.setProperty("primary", True)
        self.analyse_button.clicked.connect(self._analyse)
        form.addRow(self.analyse_button)
        self.analysis_label = QLabel("Not analysed yet.")
        self.analysis_label.setWordWrap(True)
        self.analysis_label.setObjectName("info")
        form.addRow(self.analysis_label)
        self.runs_combo = QComboBox()
        self.runs_combo.setToolTip("Jump the viewer to one of the sampled places.")
        self.runs_combo.activated.connect(self._jump_to_run)
        form.addRow("Go to", self.runs_combo)
        for spin in (self.runs_spin, self.frames_spin):
            spin.valueChanged.connect(self._update_estimate)
        side.addWidget(analysis)

        # 3 tune
        tune = QGroupBox("3 · Tune grain")
        tform = QFormLayout(tune)
        self.plot = CurvePlot()
        tform.addRow(self.plot)
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
        self.overlap_check = QCheckBox("Blend grain block edges (recommended)")
        self.overlap_check.setChecked(True)
        self.overlap_check.toggled.connect(self._tuning_changed)
        self.clip_check = QCheckBox("Keep within limited (TV) range")
        self.clip_check.setChecked(True)
        self.clip_check.setToolTip("Clip grained samples to 16–235/240 (scaled to the bit depth). "
                                   "Set automatically from the encode's range.")
        self.clip_check.toggled.connect(self._tuning_changed)
        tform.addRow(self.overlap_check)
        tform.addRow(self.clip_check)
        self.tune_label = QLabel()
        self.tune_label.setWordWrap(True)
        self.tune_label.setObjectName("info")
        tform.addRow(self.tune_label)
        model_row = QHBoxLayout()
        reset = QPushButton("Reset")
        reset.setToolTip("Back to 100 % luma and chroma strength.")
        reset.clicked.connect(self._reset_tuning)
        self.save_model_button = QPushButton("Save model…")
        self.save_model_button.clicked.connect(self._save_model)
        load_model = QPushButton("Load model…")
        load_model.clicked.connect(self._load_model_file)
        self.export_table_button = QPushButton("Export grain table…")
        self.export_table_button.setToolTip("Save the tuned parameters as an AOM film grain table "
                                            "(usable with aomenc / SVT-AV1 --fgs-table).")
        self.export_table_button.clicked.connect(self._export_table)
        for w in (reset, self.save_model_button, load_model, self.export_table_button):
            model_row.addWidget(w)
        tform.addRow(model_row)
        side.addWidget(tune)

        # 4 write
        write = QGroupBox("4 · Write")
        wgrid = QGridLayout(write)
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("Output .mkv")
        out_browse = QPushButton("Browse…")
        out_browse.clicked.connect(self._browse_output)
        wgrid.addWidget(QLabel("Output"), 0, 0)
        wgrid.addWidget(self.output_edit, 0, 1)
        wgrid.addWidget(out_browse, 0, 2)
        self.write_button = QPushButton("Write MKV with grain metadata")
        self.write_button.setProperty("primary", True)
        self.write_button.clicked.connect(self._write)
        wgrid.addWidget(self.write_button, 1, 0, 1, 3)
        self.verify_button = QPushButton("Verify with ffmpeg")
        self.verify_button.setToolTip("Decode the current frame of the written file with ffmpeg and "
                                      "compare it to the preview.")
        self.verify_button.clicked.connect(self._verify)
        self.open_folder_button = QPushButton("Open folder")
        self.open_folder_button.clicked.connect(self._open_folder)
        wgrid.addWidget(self.verify_button, 2, 0, 1, 2)
        wgrid.addWidget(self.open_folder_button, 2, 2)
        self.write_label = QLabel()
        self.write_label.setWordWrap(True)
        self.write_label.setObjectName("info")
        wgrid.addWidget(self.write_label, 3, 0, 1, 3)
        side.addWidget(write)

        self.log_toggle = QToolButton(text="Activity log", checkable=True)
        self.log_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.log_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.log = QPlainTextEdit(readOnly=True)
        self.log.setMaximumBlockCount(2000)
        self.log.setMinimumHeight(140)
        self.log.hide()
        self.log_toggle.toggled.connect(lambda on: (self.log.setVisible(on), self.log_toggle.setArrowType(
            Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)))
        side.addWidget(self.log_toggle)
        side.addWidget(self.log)
        side.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(sidebar)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setMinimumWidth(430)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        # viewer column
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 8, 8, 8)
        bar = QHBoxLayout()
        self.mode_group = QButtonGroup(self)
        self.mode_buttons = {}
        for mode, key in ((SOURCE, "1"), (ENCODED, "2"), (GRAIN, "3"), (GRAIN_ONLY, "4")):
            button = QPushButton(f"{VIEW_NAMES[mode]}  [{key}]", checkable=True)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(lambda _=False, m=mode: self._set_mode(m))
            self.mode_group.addButton(button)
            self.mode_buttons[mode] = button
            bar.addWidget(button)
        bar.addStretch(1)
        for text, action in (("Fit", lambda: self.view.fit()), ("100%", lambda: self.view.set_zoom(1)),
                             ("200%", lambda: self.view.set_zoom(2)), ("400%", lambda: self.view.set_zoom(4))):
            b = QPushButton(text)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.clicked.connect(action)
            bar.addWidget(b)
        self.popout_button = QPushButton("Open in window")
        self.popout_button.setToolTip("Show the frame on its own in a separate, resizable window.")
        self.popout_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.popout_button.clicked.connect(self._open_viewer_window)
        bar.addWidget(self.popout_button)
        rl.addLayout(bar)
        self.view = FrameView(self.state)
        self.view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        rl.addWidget(self.view, 1)
        self.grain_timeline = GrainTimeline()
        self.grain_timeline.frame_clicked.connect(self._go_to)
        self.grain_timeline.hide()
        rl.addWidget(self.grain_timeline)
        nav = QHBoxLayout()
        prev_b, next_b = QPushButton("◀"), QPushButton("▶")
        for b, d in ((prev_b, -1), (next_b, 1)):
            b.setFixedWidth(34)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.clicked.connect(lambda _=False, d=d: self._step(d))
        self.timeline = QSlider(Qt.Orientation.Horizontal)
        self.timeline.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.timeline.valueChanged.connect(self._timeline_moved)
        self.frame_spin = QSpinBox(minimum=0, maximum=0)
        self.frame_spin.setKeyboardTracking(False)
        self.frame_spin.valueChanged.connect(self._go_to)
        self.time_label = QLabel("0:00.000")
        self.time_label.setMinimumWidth(90)
        nav.addWidget(prev_b)
        nav.addWidget(self.timeline, 1)
        nav.addWidget(next_b)
        nav.addWidget(QLabel("Frame"))
        nav.addWidget(self.frame_spin)
        nav.addWidget(self.time_label)
        rl.addLayout(nav)
        hint = QLabel(SHORTCUT_HELP)
        hint.setObjectName("info")
        rl.addWidget(hint)

        self.splitter = QSplitter()
        self.splitter.addWidget(scroll)
        self.splitter.addWidget(right)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([460, 1100])
        self.setCentralWidget(self.splitter)

        status = self.statusBar()
        self.status_label = QLabel("Ready.")
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(320)
        self.progress.setTextVisible(True)
        self.progress.hide()
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.hide()
        self.cancel_button.clicked.connect(self._cancel_job)
        status.addWidget(self.status_label, 1)
        status.addPermanentWidget(self.progress)
        status.addPermanentWidget(self.cancel_button)

        menu = self.menuBar().addMenu("&Settings")
        menu.addAction("External programs…", self._edit_tools)

        self.state.changed.connect(self._sync_mode_buttons)
        self._update_estimate()
        self._update_enabled()

    def _strength_row(self, form: QFormLayout, title: str, tip: str):
        row = QHBoxLayout()
        slider = QSlider(Qt.Orientation.Horizontal, minimum=0, maximum=400, value=100)
        slider.setPageStep(10)
        spin = QSpinBox(minimum=0, maximum=800, value=100, suffix=" %")
        for w in (slider, spin):
            w.setToolTip(tip)
        slider.valueChanged.connect(spin.setValue)
        spin.valueChanged.connect(lambda v: slider.setValue(min(v, 400)))
        spin.valueChanged.connect(self._tuning_changed)
        row.addWidget(slider, 1)
        row.addWidget(spin)
        form.addRow(title, row)
        return slider, spin

    def _install_shortcuts(self, window: QWidget, view: FrameView) -> None:
        def add(key, fn):
            QShortcut(QKeySequence(key), window, fn)
        add(Qt.Key.Key_Left, lambda: self._toggle_grain(False))
        add(Qt.Key.Key_Right, lambda: self._toggle_grain(True))
        for key, mode in (("1", SOURCE), ("2", ENCODED), ("3", GRAIN), ("4", GRAIN_ONLY)):
            add(key, lambda m=mode: self._set_mode(m))
        add(",", lambda: self._step(-1))
        add(".", lambda: self._step(1))
        add(Qt.Key.Key_PageUp, lambda: self._step(-self._fps_frames()))
        add(Qt.Key.Key_PageDown, lambda: self._step(self._fps_frames()))
        add("+", lambda: view.step_zoom(1))
        add("=", lambda: view.step_zoom(1))
        add("-", lambda: view.step_zoom(-1))
        add("0", lambda: view.set_zoom(1.0))
        add("F", view.fit)

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
        if s.value("window/geometry"):
            self.restoreGeometry(s.value("window/geometry"))
        else:
            self.resize(1600, 980)
        if s.value("window/splitter"):
            self.splitter.restoreState(s.value("window/splitter"))
        self.runs_spin.setValue(int(s.value("analysis/runs", 16)))
        self.frames_spin.setValue(int(s.value("analysis/frames", 8)))
        self.luma_spin.setValue(int(s.value("tune/luma", 100)))
        self.chroma_spin.setValue(int(s.value("tune/chroma", 100)))
        self.seed_spin.setValue(int(s.value("tune/seed", 7391)))
        self.overlap_check.setChecked(s.value("tune/overlap", "true") in (True, "true"))
        # "analysis/mode" held Quick sample as the old default, not as a choice.
        if s.value("analysis/grain_mode", "every") == "quick":
            self.quick_radio.setChecked(True)
        self.workers_spin.setValue(int(s.value("analysis/threads", default_threads())))
        self.gpu_check.setChecked(s.value("analysis/gpu", "true") in (True, "true"))
        self._mode_changed()

    def _save(self) -> None:
        s = self.settings
        s.setValue("window/geometry", self.saveGeometry())
        s.setValue("window/splitter", self.splitter.saveState())
        for name in Tools.LABELS:
            s.setValue(f"tools/{name}", getattr(self.tools, name))
        s.setValue("files/source", self.source_edit.text())
        s.setValue("files/encoded", self.encoded_edit.text())
        s.setValue("analysis/runs", self.runs_spin.value())
        s.setValue("analysis/frames", self.frames_spin.value())
        s.setValue("analysis/grain_mode", "every" if self.full_radio.isChecked() else "quick")
        s.setValue("analysis/threads", self.workers_spin.value())
        s.setValue("analysis/gpu", self.gpu_check.isChecked())
        s.setValue("tune/luma", self.luma_spin.value())
        s.setValue("tune/chroma", self.chroma_spin.value())
        s.setValue("tune/seed", self.seed_spin.value())
        s.setValue("tune/overlap", self.overlap_check.isChecked())

    def _startup(self) -> None:
        self._update_tools_banner()
        self.source_edit.setText(self.settings.value("files/source", ""))
        self.encoded_edit.setText(self.settings.value("files/encoded", ""))
        if self.source_edit.text():
            self._load_video("source", quiet=True)
        if self.encoded_edit.text():
            self._load_video("encoded", quiet=True)

    def _edit_tools(self) -> None:
        dialog = ToolsDialog(self.tools, self)
        if dialog.exec():
            self.tools = dialog.tools()
            self._save()
            self._update_tools_banner()
            self._update_enabled()

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
        self.frame_cache.clear()
        self.crop = None
        notes = []
        src, enc = self.source, self.encoded
        if enc:
            self.converter = display.Converter(display.DisplayFormat(
                enc.bit_depth, enc.limited_range, enc.matrix, enc.transfer, enc.primaries,
                enc.width, enc.height))
            self.clip_check.setChecked(enc.limited_range)
            self.frame_spin.setMaximum(enc.frame_count - 1)
            self.timeline.setMaximum(enc.frame_count - 1)
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
        self._clear_model_ui()
        if src and enc:
            self._autoload_model()
        if enc:
            self._go_to(min(self.current_index, enc.frame_count - 1) if self.current_index else enc.frame_count // 3,
                        force=True)
        else:
            self.state.clear("Choose a source and an encoded video to see frames.")
        self._update_estimate()
        self._update_enabled()

    _output_is_auto = True

    # ------------------------------------------------------------------ enabling
    def _busy(self) -> bool:
        return bool(self.job and self.job.isRunning())

    def _update_enabled(self) -> None:
        busy = self._busy()
        pair = bool(self.source and self.encoded)
        for w in (self.source_edit, self.encoded_edit, self.runs_spin, self.frames_spin, self.offset_spin,
                  self.quick_radio, self.full_radio, self.workers_spin, self.gpu_check):
            w.setEnabled(not busy)
        if self.full_radio.isChecked():
            ready = analyzer_path().is_file()
            why = "" if ready else "The grain analyzer is not built: run analyzer\\build.py."
        else:
            ready = not self.tools.missing("noise_model")
            why = "" if ready else "Needs AOM noise_model (Settings → External programs)."
        self.analyse_button.setEnabled(pair and not busy and not self.tools.missing("ffmpeg") and ready)
        self.analyse_button.setToolTip(why)
        self.detect_button.setEnabled(pair and not busy)
        has_model = self.model is not None
        can_write = has_model and bool(self.encoded) and self.encoded.codec == "vvc"
        self.write_button.setEnabled(can_write and not busy and not self.tools.missing("mkvmerge"))
        self.write_button.setToolTip("" if not self.tools.missing("mkvmerge") else
                                     "Needs mkvmerge from MKVToolNix (Settings → External programs).")
        self.verify_button.setEnabled(bool(self.written) and not busy)
        self.open_folder_button.setEnabled(bool(self.written))
        self.save_model_button.setEnabled(has_model)
        self.export_table_button.setEnabled(has_model)
        self.runs_combo.setEnabled(has_model and self.runs_combo.count() > 0)
        self.analyse_button.setText("Analyse every frame" if self.full_radio.isChecked() else "Analyse grain")
        for mode, button in self.mode_buttons.items():
            button.setEnabled(mode in self.state.images or (mode in (GRAIN, GRAIN_ONLY) and has_model))

    def _mode_changed(self, *_) -> None:
        full = self.full_radio.isChecked()
        for w in (self.quick_options, self.sample_label):
            w.setVisible(not full)
        for w in (self.full_options, self.run_label):
            w.setVisible(full)
        self._update_estimate()
        self._update_enabled()

    def _fullscan_dir(self) -> Path | None:
        if not (self.source and self.encoded):
            return None
        key = cache_key(self.source, self.encoded, self.offset_spin.value(), self.crop)
        return self.data_dir / "every-frame" / key

    def _update_estimate(self) -> None:
        if self.full_radio.isChecked():
            if not analyzer_path().is_file():
                self.estimate_label.setText("The grain analyzer is not built yet: run analyzer\\build.py.")
                return
            if not (self.encoded and self.source):
                self.estimate_label.setText("Every frame of the encode is compared with the source.")
                return
            seconds = estimate_seconds(self.encoded, self.workers_spin.value())
            h, m = divmod(int(seconds // 60), 60)
            text = (f"{self.encoded.frame_count:,} frames · about {h} h {m:02d} min on this PC" if h
                    else f"{self.encoded.frame_count:,} frames · about {max(1, m)} min on this PC")
            folder = self._fullscan_dir()
            if folder and (folder / "done.json").exists():
                text += " · already analysed, will be reused"
            elif folder:
                at = checkpoint_frame(folder / "checkpoint.bin")
                if at:
                    text += f" · stopped at frame {at:,}, will resume there"
            self.estimate_label.setText(text + ". Progress is saved about once a minute.")
            return
        frames = self.runs_spin.value() * self.frames_spin.value()
        text = f"{frames} frames"
        if self.encoded:
            scale = self.encoded.width * self.encoded.height / (3840 * 2160)
            seconds = 6 + 0.38 * frames * scale
            text += f" · about {seconds:.0f} s" if seconds < 90 else f" · about {seconds / 60:.1f} min"
        self.estimate_label.setText(text)

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
        self.status_label.setText(busy_text)
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.show()
        self.cancel_button.show()
        self.cancel_button.setEnabled(True)
        self._log(busy_text)
        self.job.start()
        self._update_enabled()

    def _job_progress(self, fraction: float, message: str) -> None:
        if fraction < 0:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 1000)
            self.progress.setValue(int(fraction * 1000))
        elapsed = time.perf_counter() - self._job_started
        self.status_label.setText(f"{message}  ({elapsed:.0f} s)")

    def _job_finished(self) -> None:
        self.progress.hide()
        self.cancel_button.hide()
        self._update_enabled()

    def _job_failed(self, name: str, message: str) -> None:
        self.status_label.setText(f"{name} failed.")
        self._log(f"{name} failed: {message}")
        QMessageBox.warning(self, f"{name} failed", message)

    def _job_cancelled(self, name: str) -> None:
        self.status_label.setText(f"{name} cancelled.")
        self._log(f"{name} cancelled.")

    def _cancel_job(self) -> None:
        if self._busy():
            self.cancel_button.setEnabled(False)
            self.status_label.setText("Cancelling…")
            self.job.cancel()

    def _log(self, text: str) -> None:
        self.log.appendPlainText(time.strftime("%H:%M:%S  ") + text)

    # ------------------------------------------------------------------ analysis
    def _detect_offset(self) -> None:
        src, enc, crop, tools = self.source, self.encoded, self.crop, self.tools

        def work(token, progress):
            progress(-1, "Matching frames at three places to find the offset…")
            return detect_offset(tools, src, enc, crop, token)

        def done(result):
            offset, confidence = result
            self.offset_spin.setValue(offset)
            quality = "confident" if confidence > 0.5 else "not certain — check a few frames"
            text = f"Detected offset {offset:+d} frames ({quality})."
            self.status_label.setText(text)
            self._log(text)
            self.frame_cache.clear()
            self._go_to(self.current_index, force=True)

        self._start_job("Offset detection", work, done, "Detecting the frame offset…")

    def _analyse(self) -> None:
        if self.full_radio.isChecked():
            self._analyse_every_frame()
            return
        settings = AnalysisSettings(self.runs_spin.value(), self.frames_spin.value(),
                                    self.offset_spin.value(), self.crop)
        src, enc, tools = self.source, self.encoded, self.tools
        work_dir = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.TempLocation))

        def work(token, progress):
            return analyze(tools, src, enc, settings, progress, token, work_dir)

        self._start_job("Analysis", work, self._analysis_done,
                        f"Analysing {settings.runs} places × {settings.frames_per_run} frames…")

    def _analyse_every_frame(self) -> None:
        folder = self._fullscan_dir()
        src, enc, tools, crop = self.source, self.encoded, self.tools, self.crop
        offset, threads, gpu = self.offset_spin.value(), self.workers_spin.value(), self.gpu_check.isChecked()
        seconds = estimate_seconds(enc, threads)
        if seconds > 600 and self.confirm_long_runs:
            h, m = divmod(int(seconds // 60), 60)
            answer = QMessageBox.question(
                self, "Analyse every frame?",
                f"This analyses all {enc.frame_count:,} frames and will take about {h} h {m:02d} min, "
                "using most of the CPU. The PC is kept awake while it runs.\n\n"
                "You can keep browsing frames meanwhile. Progress is saved about once a minute; if "
                "you cancel or close the app, the next run continues from there.")
            if answer != QMessageBox.StandardButton.Yes:
                return

        def work(token, progress):
            return analyze_every_frame(tools, src, enc, offset, crop, folder, progress, token,
                                       threads=threads, source_hwaccel=gpu)

        keep_awake(True)
        self._start_job("Every-frame analysis", work, self._analysis_done,
                        f"Analysing all {enc.frame_count:,} frames…")
        self.job.finished.connect(lambda: (keep_awake(False), self._update_estimate()))

    confirm_long_runs = True

    def _analysis_done(self, model) -> None:
        if isinstance(model, GrainModel):
            # Quick sample: one parameter set for the whole video, as a one-segment timeline.
            params = build_params(model, Tuning()).params
            scale = 1 << (model.bit_depth - 8)
            runs = [{"start": r.start, "frames": r.frames, "psnr": round(r.psnr, 2), "alignment": r.alignment,
                     "luma8": None if r.luma_sigma is None else [round(float(v) / scale, 3) for v in r.luma_sigma]}
                    for r in model.runs]
            model = quick_timeline(params, self.encoded, model.seconds, {"runs": runs})
        self._set_model(model)
        self._store_model_cache(model)
        h, rem = divmod(int(model.seconds), 3600)
        took = f"{h} h {rem // 60:02d} min" if h else f"{model.seconds:.0f} s"
        self.status_label.setText(f"Analysis finished in {took}.")
        self._log(self.analysis_label.text())

    def _set_model(self, model: TimelineModel, loaded_from: str | None = None) -> None:
        self.model = model
        self.written = None
        self.runs_combo.clear()
        scale = 1 << (model.bit_depth - 8)
        h, rem = divmod(int(model.seconds), 3600)
        took = (f" Took {h} h {rem // 60:02d} min." if model.seconds >= 3600 else
                f" Took {model.seconds / 60:.0f} min." if model.seconds >= 60 else
                f" Took {model.seconds:.0f} s." if model.seconds else "")
        if model.kind == "quick":
            runs = model.notes.get("runs", [])
            ok = [r for r in runs if r["alignment"] in ("ok", "unclear") and r["psnr"] > 0]
            excluded = len(runs) - len(ok)
            mid = (model.sigma_values[0] / scale) if model.sigma_values else 0.0
            sampled = sum(r.get("frames", 0) for r in runs)
            text = (f"Quick sample, not the whole video: {len(runs)} places"
                    + (f" ({sampled:,} frames)" if sampled else "")
                    + f", one grain setting for all {model.frames:,} frames. "
                    f"{len(ok)} of {len(runs)} places used"
                    + (f" ({excluded} excluded: frames did not line up)" if excluded else "")
                    + (f", median match {np.median([r['psnr'] for r in ok]):.1f} dB" if ok else "")
                    + f". The encode lost grain of σ ≈ {mid:.2f} (8-bit) at mid-grey.{took}")
            fps = float(model.fps)
            for r in runs:
                mark = "" if r["alignment"] in ("ok", "unclear") else "  (excluded)"
                self.runs_combo.addItem(f"{_timecode(r['start'] / fps)} · frame {r['start']:,} · "
                                        f"{r['psnr']:.1f} dB{mark}", r["start"])
            self.grain_timeline.hide()
        else:
            values = np.array(model.sigma_values or [0.0]) / scale
            text = (f"Every frame analysed: {model.frames:,} frames, {len(model.segments):,} parameter "
                    f"changes. Grain σ at mid-grey across the film: median {np.median(values):.2f}, "
                    f"5–95 % {np.percentile(values, 5):.2f}–{np.percentile(values, 95):.2f} (8-bit).{took}")
            if model.alignment.count("shifted"):
                text += (f" ⚠ {model.alignment.count('shifted')} of {len(model.alignment)} alignment "
                         "checks matched a neighbouring frame better (check the offset).")
            for warning in model.notes.get("warnings", []):
                text += f" ⚠ {warning}"
            self.grain_timeline.show()
        if loaded_from:
            text = f"Loaded from {loaded_from}. " + text
        self.analysis_label.setText(text)
        self._retune()
        self._update_enabled()

    def _clear_model_ui(self) -> None:
        self.grain_timeline.hide()
        self.analysis_label.setText("Not analysed yet.")
        self.runs_combo.clear()
        self.plot.clear("Analyse the grain to see its strength curve.")
        self.tune_label.setText("")
        self.write_label.setText("")
        self.state.update_images({GRAIN: None, GRAIN_ONLY: None})

    def _jump_to_run(self, index: int) -> None:
        start = self.runs_combo.itemData(index)
        if start is not None:
            self._go_to(int(start) + self.frames_spin.value() // 2)

    # model cache: reopening the same pair restores its analysis
    def _cache_path(self) -> Path | None:
        if not (self.source and self.encoded):
            return None
        stat = self.encoded.path.stat()
        key = f"{self.source.path}|{self.encoded.path}|{stat.st_size}|{int(stat.st_mtime)}"
        folder = self.data_dir / "models"
        return folder / (hashlib.sha1(key.encode()).hexdigest()[:16] + ".json")

    def _store_model_cache(self, model: TimelineModel) -> None:
        path = self._cache_path()
        if path:
            path = path.with_suffix(f".{model.kind}.json")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(model.to_json(), encoding="utf-8")

    @staticmethod
    def _model_from_json(text: str) -> TimelineModel:
        return TimelineModel.from_json(text)

    def _autoload_model(self) -> None:
        path = self._cache_path()
        if not path:
            return
        for suffix, label in ((".every-frame.json", "the previous every-frame analysis"),
                              (".quick.json", "the previous analysis of these files")):
            candidate = path.with_suffix(suffix)
            if candidate.is_file():
                try:
                    self._set_model(self._model_from_json(candidate.read_text(encoding="utf-8")),
                                    loaded_from=label)
                    return
                except (ValueError, KeyError):
                    continue

    def _save_model(self) -> None:
        if not self.model:
            return
        start = str(self.encoded.path.with_suffix(".grainmodel.json")) if self.encoded else ""
        path, _ = QFileDialog.getSaveFileName(self, "Save grain model", start, "Grain model (*.json)")
        if path:
            Path(path).write_text(self.model.to_json(), encoding="utf-8")
            self._log(f"Saved model to {path}")

    def _load_model_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Load grain model", "", "Grain model (*.json)")
        if not path:
            return
        try:
            model = self._model_from_json(Path(path).read_text(encoding="utf-8"))
        except (ValueError, KeyError, OSError) as exc:
            QMessageBox.warning(self, "Could not load model", str(exc))
            return
        if self.encoded and (model.bit_depth, model.subsampling_x, model.subsampling_y) != (
                self.encoded.bit_depth, self.encoded.subsampling_x, self.encoded.subsampling_y):
            QMessageBox.warning(self, "Model does not fit", "The model was made for a different bit depth "
                                "or chroma format than the loaded encode.")
            return
        self._set_model(model, loaded_from=Path(path).name)

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
        self._log(f"Exported grain table ({len(entries):,} entries) to {path}")

    # ------------------------------------------------------------------ tuning
    def _tuning(self) -> Tuning:
        return Tuning(self.luma_spin.value() / 100, self.chroma_spin.value() / 100,
                      self.overlap_check.isChecked(), self.clip_check.isChecked(), self.seed_spin.value())

    def _tuning_changed(self, *_) -> None:
        if self.model:
            self.tune_timer.start()

    def _reset_tuning(self) -> None:
        self.luma_spin.setValue(100)
        self.chroma_spin.setValue(100)

    def _retune(self) -> None:
        if not self.model:
            return
        tuning = self._tuning()
        m = self.model
        self.plan = TimelinePlan(m, tuning)
        if m.kind != "quick":
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
        if not (self.model and self.plan):
            return
        m, tuning = self.model, self._tuning()
        params = self.plan.params_at(self.current_index)
        y, cb, cr = entry_sigmas(params, m.bit_depth, m.subsampling_x, m.subsampling_y)
        to8 = lambda a: sigma_in_8bit_units(a, m.bit_depth)
        mid = float(to8(y)[128])
        if m.kind == "quick":
            runs = [np.array(r["luma8"]) for r in m.notes.get("runs", []) if r.get("luma8")]
            self.plot.set_data(to8(y), to8(cb), to8(cr), runs)
            text = (f"Mid-grey luma grain σ {mid:.2f} (8-bit) at {tuning.luma_strength * 100:.0f} % strength · "
                    f"{len(params.y_points)} luma / {len(params.cb_points)} chroma points · "
                    f"scaling shift {params.scaling_shift}")
        else:
            segment = m.segment_at(self.current_index)
            self.plot.set_data(to8(y), to8(cb), to8(cr), [])
            fps = float(m.fps)
            text = (f"At this frame (parameters of frames {segment.start:,}–{segment.end - 1:,}, "
                    f"{(segment.end - segment.start) / fps:.1f} s): luma grain σ {mid:.2f} at mid-grey "
                    f"(8-bit) with {tuning.luma_strength * 100:.0f} % strength.")
        if self.plan.clipped:
            text += " ⚠ strength capped by the format's maximum somewhere."
        self.tune_label.setText(text)

    # ------------------------------------------------------------------ frames
    def _fps_frames(self) -> int:
        return int(round(float(self.encoded.fps))) if self.encoded else 24

    def _step(self, delta: int) -> None:
        if self.encoded:
            self._go_to(max(0, min(self.encoded.frame_count - 1, self.current_index + delta)))

    def _timeline_moved(self, value: int) -> None:
        if value != self.current_index:
            self._go_to(value)

    def _go_to(self, index: int, force: bool = False) -> None:
        if not self.encoded:
            return
        index = max(0, min(self.encoded.frame_count - 1, int(index)))
        if index == self.current_index and not force and self.state.images:
            return
        self.current_index = index
        for w, v in ((self.timeline, index), (self.frame_spin, index)):
            w.blockSignals(True)
            w.setValue(v)
            w.blockSignals(False)
        self.time_label.setText(_timecode(self.encoded.time_of(index)))
        self.grain_timeline.set_current(index)
        self.frame_timer.start()

    def _caption(self, index: int) -> str:
        return f"frame {index:,} · {_timecode(self.encoded.time_of(index))}"

    def _cached(self, key) -> Frame | None:
        frame = self.frame_cache.get(key)
        if frame is not None:
            self.frame_cache.move_to_end(key)
        return frame

    def _request_frame(self, index: int) -> None:
        if not self.encoded or self.tools.missing("ffmpeg"):
            return
        if self.frame_job and self.frame_job.isRunning():
            self.pending_index = index
            return
        self.pending_index = None
        src_info, enc_info, crop, tools = self.source, self.encoded, self.crop, self.tools
        offset = self.offset_spin.value()
        geometry = Geometry.of(enc_info)
        src_key = ("src", str(src_info.path) if src_info else "", index + offset, crop)
        enc_key = ("enc", str(enc_info.path), index)
        src_frame = self._cached(src_key) if src_info else None
        enc_frame = self._cached(enc_key)
        params = self.plan.params_at(index) if self.plan else None
        seed = picture_seed(self.seed_spin.value(), index)
        converter = self.converter
        # A seek into a long-GOP stream costs ~1-2 s at 4K, the next few frames
        # almost nothing: decode a small window in the direction of travel.
        backwards = index < self._last_requested
        self._last_requested = index
        start = max(0, index - PREFETCH + 1) if backwards else index
        count = min(PREFETCH, enc_info.frame_count - start)

        def work(token, progress):
            with ThreadPoolExecutor(2) as pool:
                need_src = src_info is not None and src_frame is None \
                    and 0 <= index + offset < src_info.frame_count
                s_start = max(0, start + offset)
                fs = pool.submit(read_frames, tools.ffmpeg, src_info, s_start,
                                 min(count, src_info.frame_count - s_start), token, geometry, crop) \
                    if need_src else None
                fe = pool.submit(read_frames, tools.ffmpeg, enc_info, start, count, token) \
                    if enc_frame is None else None
                s_run = fs.result() if fs else []
                e_run = fe.result() if fe else []
            s = src_frame or next((f for f in s_run if f.index == index + offset), None)
            e = enc_frame or next((f for f in e_run if f.index == index), None)
            if e is None:
                raise RuntimeError(f"frame {index} is past the end of the encode")
            with ThreadPoolExecutor(3) as pool:
                jobs = {ENCODED: pool.submit(lambda: _qimage(converter.to_rgb(e.y, e.u, e.v)))}
                if s is not None:
                    jobs[SOURCE] = pool.submit(lambda: _qimage(converter.to_rgb(s.y, s.u, s.v)))
                grain = pool.submit(self._grain_images, e, params, seed) if params is not None else None
                images = {k: j.result() for k, j in jobs.items()}
                if grain:
                    images.update(grain.result())
            extra = [(("src", str(src_info.path), f.index, crop), f) for f in s_run] + \
                    [(("enc", str(enc_info.path), f.index), f) for f in e_run]
            return index, extra, images

        job = Job("Frame", work, self)
        job.succeeded.connect(self._frame_ready)
        job.failed.connect(lambda msg: self._frame_failed(index, msg))
        job.finished.connect(self._frame_job_finished)
        self.frame_job = job
        if not self.state.images:
            self.state.clear(f"Loading frame {index:,}…")
        if not self._busy():
            self.status_label.setText(f"Loading frame {index:,}…")
        job.start()

    def _grain_images(self, e: Frame, params, seed: int) -> dict:
        g = e.geometry
        y, u, v = synth.apply_grain(e.y, e.u, e.v, params, seed, g.bit_depth,
                                    g.subsampling_x, g.subsampling_y)
        return {GRAIN: _qimage(self.converter.to_rgb(y, u, v)),
                GRAIN_ONLY: _qimage(display.grain_only(e.y, y, g.bit_depth))}

    def _frame_ready(self, result) -> None:
        index, extra, images = result
        for key, frame in extra:
            self.frame_cache[key] = frame
            self.frame_cache.move_to_end(key)
        budget = CACHE_BYTES
        for key in reversed(list(self.frame_cache)):
            f = self.frame_cache[key]
            budget -= f.y.nbytes + f.u.nbytes + f.v.nbytes
            if budget < 0:
                del self.frame_cache[key]
        if index != self.current_index:
            return
        self.state.set_images(images, self._caption(index))
        if not self._busy():
            self.status_label.setText(f"Frame {index:,} loaded.")
        self._update_segment_plot()
        self._update_enabled()

    def _frame_failed(self, index: int, message: str) -> None:
        self._log(f"Could not load frame {index}: {message}")
        if index == self.current_index:
            self.state.clear(f"Could not decode frame {index:,}.\n\n{message.splitlines()[0]}")

    def _frame_job_finished(self) -> None:
        if self.pending_index is not None:
            self._request_frame(self.pending_index)
        elif self.grain_dirty:
            self._render_grain()

    def _render_grain(self) -> None:
        """Re-synthesise the grain views for the current frame after a tuning change."""
        if not (self.plan and self.encoded):
            return
        if self.frame_job and self.frame_job.isRunning():
            self.grain_dirty = True
            return
        self.grain_dirty = False
        enc = self._cached(("enc", str(self.encoded.path), self.current_index))
        if enc is None:
            self._request_frame(self.current_index)
            return
        params, index = self.plan.params_at(self.current_index), self.current_index
        seed = picture_seed(self.seed_spin.value(), index)

        def work(token, progress):
            return index, self._grain_images(enc, params, seed)

        job = Job("Grain preview", work, self)
        job.succeeded.connect(self._grain_ready)
        job.failed.connect(lambda msg: self._log(f"Grain preview failed: {msg}"))
        job.finished.connect(self._frame_job_finished)
        self.frame_job = job
        job.start()

    def _grain_ready(self, result) -> None:
        index, images = result
        if index == self.current_index:
            self.state.update_images(images)
            self._update_enabled()

    # ------------------------------------------------------------------ view modes
    def _set_mode(self, mode: int) -> None:
        if not self.state.set_mode(mode):
            reason = {SOURCE: "Choose the source video to see it here.",
                      GRAIN: "Analyse the grain (step 2) to preview it.",
                      GRAIN_ONLY: "Analyse the grain (step 2) to preview it."}.get(mode, "")
            if reason:
                self.status_label.setText(reason)
        self._sync_mode_buttons()

    def _toggle_grain(self, towards_grain: bool) -> None:
        self._set_mode(GRAIN if towards_grain else ENCODED)

    def _sync_mode_buttons(self) -> None:
        for mode, button in self.mode_buttons.items():
            button.setChecked(mode == self.state.mode)

    def _open_viewer_window(self) -> None:
        if self.viewer_window is None:
            self.viewer_window = ViewerWindow(self.state)
            self._install_shortcuts(self.viewer_window, self.viewer_window.view)
            geometry = self.settings.value("viewer/geometry")
            if geometry:
                self.viewer_window.restoreGeometry(geometry)
        self.viewer_window.show_sized()

    # ------------------------------------------------------------------ writing
    def _browse_output(self) -> None:
        start = self.output_edit.text() or (str(self.encoded.path.parent) if self.encoded else "")
        path, _ = QFileDialog.getSaveFileName(self, "Write MKV", start, "Matroska video (*.mkv)")
        if path:
            if not path.lower().endswith(".mkv"):
                path += ".mkv"
            self.output_edit.setText(os.path.normpath(path))
            self._output_is_auto = False

    def _write(self) -> None:
        if not (self.plan and self.encoded):
            return
        output = Path(self.output_edit.text().strip().strip('"'))
        if output.suffix.lower() != ".mkv":
            QMessageBox.warning(self, "Output", "The output must be an .mkv file.")
            return
        if output.resolve() == self.encoded.path.resolve():
            QMessageBox.warning(self, "Output", "The output would overwrite the encode. Choose another name.")
            return
        if output.exists() and QMessageBox.question(
                self, "Replace file?", f"{output.name} already exists. Replace it?") != QMessageBox.StandardButton.Yes:
            return
        params, seed, enc, tools = self.plan, self.seed_spin.value(), self.encoded, self.tools

        def work(token, progress):
            return write_afgs1_mkv(tools, enc, output, params, seed, progress, token)

        def done(report: WriteReport):
            self.written = Written(report, params, seed)
            size = report.output.stat().st_size / 1e9
            text = (f"Wrote {report.output.name} ({size:.2f} GB): grain metadata on all "
                    f"{report.pictures:,} pictures; audio, subtitles and chapters copied.")
            if report.notes:
                text += " " + " ".join(report.notes)
            self.write_label.setText(text + " Use Verify to check it with ffmpeg.")
            self.status_label.setText("Written.")
            self._log(text)
            self._update_enabled()

        self._start_job("Writing", work, done, f"Writing {output.name}…")

    def _verify(self) -> None:
        if not (self.written and self.encoded):
            return
        written, enc, tools, index = self.written, self.encoded, self.tools, self.current_index

        def work(token, progress):
            progress(-1, f"Decoding frame {index:,} of the written file with ffmpeg…")
            out_info = probe(written.report.output, tools.ffprobe, token)
            theirs = read_frame(tools.ffmpeg, out_info, index, token)
            clean = read_frame(tools.ffmpeg, enc, index, token)
            g = clean.geometry
            mine = synth.apply_grain(clean.y, clean.u, clean.v, written.params.params_at(index),
                                     picture_seed(written.seed, index), g.bit_depth,
                                     g.subsampling_x, g.subsampling_y)
            diffs = [int(np.abs(a.astype(np.int32) - b.astype(np.int32)).max())
                     for a, b in zip(mine, (theirs.y, theirs.u, theirs.v))]
            added = float(np.abs(theirs.y.astype(np.int32) - clean.y.astype(np.int32)).mean())
            return diffs, added

        def done(result):
            diffs, added = result
            if added == 0:
                text = ("ffmpeg decoded the frame without grain. Your ffmpeg build may be too old to apply "
                        "AFGS1 (FFmpeg 7.1+ applies it for VVC).")
            elif max(diffs) == 0:
                text = (f"✔ Frame {index:,}: ffmpeg's decode of the written file matches the preview exactly "
                        f"(grain changes luma by {added:.2f} code values on average).")
            else:
                text = f"✖ Frame {index:,}: ffmpeg differs from the preview by up to {diffs} (Y, Cb, Cr)."
            self.write_label.setText(text)
            self._log(text)

        self._start_job("Verification", work, done, "Verifying with ffmpeg…")

    def _open_folder(self) -> None:
        if self.written:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.written.report.output.parent)))

    # ------------------------------------------------------------------ close
    def closeEvent(self, event) -> None:
        if self._busy():
            extra = (" Finished parts are kept; the next run continues from there."
                     if self.job.name == "Every-frame analysis" else "")
            answer = QMessageBox.question(self, "Work in progress",
                                          f"{self.job.name} is still running. Cancel it and quit?{extra}")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.job.cancel()
            self.job.wait(10000)
        if self.frame_job and self.frame_job.isRunning():
            self.frame_job.cancel()
            self.frame_job.wait(5000)
        if self.viewer_window:
            self.settings.setValue("viewer/geometry", self.viewer_window.saveGeometry())
            self.viewer_window.close()
        self._save()
        event.accept()
