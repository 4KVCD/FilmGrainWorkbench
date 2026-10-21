"""VVC grain analyzer: a source video, after VVenC removed its grain, and with vvdec's grain synthesis.

Browsing shows the source alone, decoded by ffmpeg. "Encode this scene" encodes
the frames around the one shown with VVenC in all three film grain analysis
modes: the selected one first, shown as soon as it is ready, the other two in
the background, so switching modes is instant. Inside that scene every view is
available, and a changed VVenC setting encodes the scene again.

Temporary files: a window keeps one scene (its source frames and the three
encodes, each decoded twice) in its own folder in %TEMP%, deletes whatever is
replaced as soon as nothing reads it, and the folder when it closes. Folders
left by a crash are deleted when the next window opens.
"""

from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
import os
import shutil

import numpy as np
from PySide6.QtCore import QSettings, QStandardPaths, Qt, QTimer
from PySide6.QtGui import QFont, QImage, QKeySequence, QShortcut
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
                               QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSlider, QSpinBox,
                               QSplitter, QVBoxLayout, QWidget)

from .. import display
from ..frames import Crop, Frame, Geometry, read_frames
from ..probe import VideoInfo, probe
from ..tools import Tools, ToolError, detect_tools
from ..vvcgrain import (DECODED, FGA_MODES, GRAIN as GRAIN_FILE, PRESETS, SOURCE as SOURCE_FILE, Clip,
                        SceneCut, VvcSettings, cut_scene, detect_black_bars, encode_scene, find_vvc_tools,
                        frame_stats, open_work_dir)
from .jobs import Job
from .viewer import ENCODED, GRAIN, GRAIN_ONLY, SOURCE, FrameView, PreviewState, ViewerWindow

VIEW_NAMES = {SOURCE: "Source", ENCODED: "VVC, grain removed", GRAIN: "VVC + grain synthesis",
              GRAIN_ONLY: "Grain only (×4)"}
VIEW_TIPS = {SOURCE: "The source frame (cropped like the encode).",
             ENCODED: "What VVenC coded: its MCTF pre-filter and the quantisation removed the grain. "
                      "Decoded by vvdec with film grain synthesis off.",
             GRAIN: "The same picture decoded by vvdec with film grain synthesis on, from the FGC SEI "
                    "message VVenC's film grain analysis wrote.",
             GRAIN_ONLY: "The grain vvdec adds, amplified 4×, on mid-grey."}
VIDEO_FILTER = "Video files (*.mkv *.mp4 *.mov *.webm *.ts *.m2ts *.y4m);;All files (*)"
PREFETCH = 4                    # source frames decoded per seek while browsing
SOURCE_CACHE = 16               # source frames kept for stepping back and forth (~18 MB each at 4K)


@dataclass
class Scene:
    """The frames "Encode this scene" was pressed for: one cut of the source, an encode per mode."""
    ident: int
    start: int
    frames: int
    crop: Crop | None
    base: tuple                                    # VvcSettings.without_mode() the encodes use
    folder: Path
    cut: SceneCut | None = None
    clips: dict[int, Clip] = field(default_factory=dict)      # by --fga, encoded with ``base``
    older: dict[int, Clip] = field(default_factory=dict)      # earlier settings, shown until replaced

    def covers(self, index: int) -> bool:
        start, count = (self.cut.start, self.cut.count) if self.cut else (self.start, self.frames)
        return start <= index < start + count

    def settings(self, fga: int) -> VvcSettings:
        return VvcSettings(fga, *self.base)


def _qimage(rgb: np.ndarray) -> QImage:
    rgb = np.ascontiguousarray(rgb)
    h, w, _ = rgb.shape
    return QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()


def _timecode(seconds: float) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:06.3f}" if h else f"{int(m)}:{s:06.3f}"


class VvcGrainWindow(QMainWindow):
    def __init__(self, settings: QSettings | None = None, work_dir: Path | None = None) -> None:
        super().__init__()
        self.setWindowTitle("VVC Grain Analyzer — VVenC film grain analysis + vvdec synthesis")
        self.settings = settings or QSettings("AFGS1Workbench", "AFGS1Workbench")
        self.data_dir = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation))
        self.tools = self._load_tools()
        self.vvc_tools = find_vvc_tools()
        self.work_root, self.work_lock = open_work_dir(work_dir)
        self.source: VideoInfo | None = None
        self.crop: Crop | None = None
        self.crop_known = False                     # black bars detected for this source
        self.scene: Scene | None = None
        self.scene_count = 0
        self.stopped = False                        # Cancel pressed or an encode failed: no more work
        self.retired: list[Path] = []               # folders to delete once nothing reads them
        self.job_folder: Path | None = None         # where the running job writes
        self.converter: display.Converter | None = None
        self.current_index = 0
        self.job: Job | None = None                 # black-bar detection or an encode
        self.job_key: tuple | None = None
        self.frame_job: Job | None = None
        self.frame_pending = False
        self.source_frames: OrderedDict[tuple, Frame] = OrderedDict()
        self.last_read = 0
        self.preferred_mode = GRAIN                 # the view chosen last, restored inside a scene
        self.state = PreviewState(VIEW_NAMES)
        self.viewer_window: ViewerWindow | None = None
        self._build()
        self._restore()
        self.frame_timer = QTimer(self, singleShot=True, interval=120)
        self.frame_timer.timeout.connect(self._show)
        self.settings_timer = QTimer(self, singleShot=True, interval=600)
        self.settings_timer.timeout.connect(self._apply_settings)
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
        side.addWidget(self.tools_banner)

        files = QGroupBox("1 · Source")
        grid = QGridLayout(files)
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText("Original, grainy video (e.g. the Blu-ray remux)")
        self.source_edit.editingFinished.connect(self._load_source)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        self.source_info = QLabel()
        self.source_info.setObjectName("info")
        self.source_info.setWordWrap(True)
        self.crop_check = QCheckBox("Remove black bars, as an encode would")
        self.crop_check.setChecked(True)
        self.crop_check.setToolTip("Encode only the picture area. Black bars are flat and would take part "
                                   "in VVenC's grain analysis otherwise.")
        self.crop_check.toggled.connect(self._crop_toggled)
        self.crop_label = QLabel()
        self.crop_label.setObjectName("info")
        grid.addWidget(self.source_edit, 0, 0)
        grid.addWidget(browse, 0, 1)
        grid.addWidget(self.source_info, 1, 0, 1, 2)
        grid.addWidget(self.crop_check, 2, 0, 1, 2)
        grid.addWidget(self.crop_label, 3, 0, 1, 2)
        side.addWidget(files)

        enc = QGroupBox("2 · VVenC")
        form = QFormLayout(enc)
        self.fga_combo = QComboBox()
        for mode, text in FGA_MODES.items():
            self.fga_combo.addItem(text, mode)
        self.fga_combo.setToolTip("--fga: which film grain analysis writes the FGC SEI message. "
                                  "3 is shown only by decoders with the auto-regressive model (vvdec).")
        self.strength_spin = QSpinBox(minimum=0, maximum=400, value=100, singleStep=10, suffix=" %")
        self.strength_spin.setToolTip("--fgastrength: grain strength in percent of what the analysis measured.")
        self.qp_spin = QDoubleSpinBox(minimum=0, maximum=63, value=28, decimals=1, singleStep=1)
        self.qp_spin.setToolTip("--qp: the higher, the more grain the coding removes.")
        self.preset_combo = QComboBox()
        self.preset_combo.addItems(PRESETS)
        self.frames_spin = QSpinBox(minimum=9, maximum=129, value=33)
        self.frames_spin.setToolTip("Frames Encode this scene encodes, centred on the frame shown. VVenC analyses the grain once per "
                                    "GOP (32 frames), so 33 covers one GOP and the next.")
        for label, widget in (("Film grain analysis", self.fga_combo), ("Strength", self.strength_spin),
                              ("QP", self.qp_spin), ("Preset", self.preset_combo),
                              ("Scene length", self.frames_spin)):
            form.addRow(label, widget)
        self.fga_combo.currentIndexChanged.connect(self._mode_changed)
        self.preset_combo.currentIndexChanged.connect(self._settings_changed)
        for spin in (self.strength_spin, self.qp_spin, self.frames_spin):
            spin.valueChanged.connect(self._settings_changed)
        self.encode_button = QPushButton("Encode this scene  [E]")
        self.encode_button.setProperty("primary", True)
        self.encode_button.setToolTip("Encode the frames around the one shown with VVenC, then show them "
                                      "without and with vvdec's grain synthesis.")
        self.encode_button.clicked.connect(self._encode_scene)
        form.addRow(self.encode_button)
        self.scene_label = QLabel()
        self.scene_label.setObjectName("info")
        self.scene_label.setWordWrap(True)
        form.addRow(self.scene_label)
        self.tools_label = QLabel()
        self.tools_label.setObjectName("info")
        self.tools_label.setWordWrap(True)
        form.addRow(self.tools_label)
        side.addWidget(enc)

        info = QGroupBox("3 · This frame")
        il = QVBoxLayout(info)
        self.info_label = QLabel("Choose a source video.")
        self.info_label.setWordWrap(True)
        self.info_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.info_label.setFont(mono)
        il.addWidget(self.info_label)
        side.addWidget(info)
        side.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(sidebar)
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(440)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 8, 8, 8)
        bar = QHBoxLayout()
        self.mode_group = QButtonGroup(self)
        self.mode_buttons = {}
        for mode, key in ((SOURCE, "1"), (ENCODED, "2"), (GRAIN, "3"), (GRAIN_ONLY, "4")):
            button = QPushButton(f"{VIEW_NAMES[mode]}  [{key}]", checkable=True)
            button.setToolTip(VIEW_TIPS[mode])
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(lambda _=False, m=mode: self._set_mode(m))
            self.mode_group.addButton(button)
            self.mode_buttons[mode] = button
            bar.addWidget(button)
        bar.addStretch(1)
        self.view = FrameView(self.state)
        for text, action in (("Fit", self.view.fit), ("100%", lambda: self.view.set_zoom(1)),
                             ("200%", lambda: self.view.set_zoom(2)), ("400%", lambda: self.view.set_zoom(4))):
            b = QPushButton(text)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.clicked.connect(action)
            bar.addWidget(b)
        popout = QPushButton("Open in window")
        popout.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        popout.clicked.connect(self._open_viewer_window)
        bar.addWidget(popout)
        rl.addLayout(bar)
        self.view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        rl.addWidget(self.view, 1)
        nav = QHBoxLayout()
        prev_b, next_b = QPushButton("◀"), QPushButton("▶")
        for b, d in ((prev_b, -1), (next_b, 1)):
            b.setFixedWidth(34)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.clicked.connect(lambda _=False, d=d: self._step(d))
        self.timeline = QSlider(Qt.Orientation.Horizontal)
        self.timeline.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.timeline.valueChanged.connect(lambda v: v != self.current_index and self._go_to(v))
        self.frame_spin = QSpinBox(minimum=0, maximum=0)
        self.frame_spin.setKeyboardTracking(False)
        self.frame_spin.valueChanged.connect(self._go_to)
        self.time_label = QLabel("0:00.000")
        self.time_label.setMinimumWidth(90)
        for w in (prev_b,):
            nav.addWidget(w)
        nav.addWidget(self.timeline, 1)
        nav.addWidget(next_b)
        nav.addWidget(QLabel("Frame"))
        nav.addWidget(self.frame_spin)
        nav.addWidget(self.time_label)
        rl.addLayout(nav)
        hint = QLabel("← → VVC / VVC + grain · 1–4 choose view · , . previous / next frame · PgUp PgDn ±1 s · "
                      "wheel or + − zoom · 0 = 100 % · F fit · drag to pan")
        hint.setObjectName("info")
        rl.addWidget(hint)

        self.splitter = QSplitter()
        self.splitter.addWidget(scroll)
        self.splitter.addWidget(right)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([470, 1100])
        self.setCentralWidget(self.splitter)

        status = self.statusBar()
        self.status_label = QLabel("Ready.")
        self.progress = QProgressBar(maximumWidth=240)
        self.progress.setRange(0, 0)
        self.progress.hide()
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.hide()
        self.cancel_button.clicked.connect(self._stop)
        status.addWidget(self.status_label, 1)
        status.addPermanentWidget(self.progress)
        status.addPermanentWidget(self.cancel_button)

        self.state.changed.connect(self._sync_mode_buttons)
        self._install_shortcuts(self, self.view)
        self._update_tools()

    def _install_shortcuts(self, window: QWidget, view: FrameView) -> None:
        def add(key, fn):
            QShortcut(QKeySequence(key), window, fn)
        add(Qt.Key.Key_Left, lambda: self._set_mode(ENCODED))
        add(Qt.Key.Key_Right, lambda: self._set_mode(GRAIN))
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
        add("E", self._encode_scene)

    # ------------------------------------------------------------------ settings
    def _load_tools(self) -> Tools:
        found = detect_tools()
        for name in ("ffmpeg", "ffprobe"):
            saved = self.settings.value(f"tools/{name}", "")
            if saved and Path(saved).is_file():
                setattr(found, name, saved)
        return found

    def _update_tools(self) -> None:
        missing = self.tools.missing("ffmpeg", "ffprobe") + self.vvc_tools.missing()
        self.tools_banner.setVisible(bool(missing))
        self.tools_banner.setText("Missing: " + ", ".join(missing) + ". vvencapp and vvdecapp are built by "
                                  "vvc\\build_vvc_tools.ps1.")
        self.tools_label.setText(f"vvencapp: {self.vvc_tools.vvencapp or '—'}\nvvdecapp: {self.vvc_tools.vvdecapp or '—'}")

    def _restore(self) -> None:
        s = self.settings
        if s.value("vvc/geometry"):
            self.restoreGeometry(s.value("vvc/geometry"))
        else:
            self.resize(1600, 980)
        index = self.fga_combo.findData(int(s.value("vvc/fga", 2)))
        self.fga_combo.setCurrentIndex(max(0, index))
        self.strength_spin.setValue(int(s.value("vvc/strength", 100)))
        self.qp_spin.setValue(float(s.value("vvc/qp", 28)))
        self.preset_combo.setCurrentText(str(s.value("vvc/preset", "faster")))
        self.frames_spin.setValue(int(s.value("vvc/frames", 33)))
        self.crop_check.setChecked(s.value("vvc/crop", "true") in (True, "true"))

    def _save(self) -> None:
        s = self.settings
        s.setValue("vvc/geometry", self.saveGeometry())
        s.setValue("vvc/source", self.source_edit.text())
        s.setValue("vvc/fga", self.fga_combo.currentData())
        s.setValue("vvc/strength", self.strength_spin.value())
        s.setValue("vvc/qp", self.qp_spin.value())
        s.setValue("vvc/preset", self.preset_combo.currentText())
        s.setValue("vvc/frames", self.frames_spin.value())
        s.setValue("vvc/crop", self.crop_check.isChecked())

    def _vvc_settings(self) -> VvcSettings:
        return VvcSettings(int(self.fga_combo.currentData()), self.strength_spin.value(),
                           float(self.qp_spin.value()), self.preset_combo.currentText(), self.frames_spin.value())

    def _startup(self) -> None:
        saved = self.settings.value("vvc/source", "")
        if saved:
            self.source_edit.setText(saved)
            self._load_source()

    # ------------------------------------------------------------------ source
    def _browse(self) -> None:
        start = self.source_edit.text() or self.settings.value("files/last_dir", "")
        path, _ = QFileDialog.getOpenFileName(self, "Choose the source video", start, VIDEO_FILTER)
        if path:
            self.source_edit.setText(os.path.normpath(path))
            self.settings.setValue("files/last_dir", str(Path(path).parent))
            self._load_source()

    def _load_source(self) -> None:
        path = self.source_edit.text().strip().strip('"')
        if self.source and str(self.source.path) == path:
            return
        self._retire_scene()                        # a running job is cancelled when its finish shows
        self.source_frames.clear()
        self.converter = None
        self.source, self.crop, self.crop_known = None, None, False
        self.crop_label.setText("")
        if not path:
            self.source_info.setText("")
            self.state.clear("Choose a source video.")
            return
        try:
            self.source = probe(path, self.tools.ffprobe)
        except (ToolError, OSError, ValueError) as exc:
            self.source_info.setText(str(exc).splitlines()[0])
            self.source_info.setStyleSheet("color: #ff7b72;")
            self.state.clear("The source could not be read.")
            return
        self.source_info.setStyleSheet("")
        self.source_info.setText(self.source.describe())
        n = self.source.frame_count
        for w in (self.timeline, self.frame_spin):
            w.blockSignals(True)
            w.setMaximum(n - 1)
            w.blockSignals(False)
        self._save()
        self._go_to(n // 3, force=True)

    def _crop_toggled(self) -> None:
        self._save()
        self.source_frames.clear()
        self._retire_scene()
        if self.source:
            self.converter = None
            self._show()

    # ------------------------------------------------------------------ navigation
    def _fps_frames(self) -> int:
        return int(round(float(self.source.fps))) if self.source else 24

    def _step(self, delta: int) -> None:
        if self.source:
            self._go_to(self.current_index + delta)

    def _go_to(self, index: int, force: bool = False) -> None:
        if not self.source:
            return
        index = max(0, min(self.source.frame_count - 1, int(index)))
        if index == self.current_index and not force:
            return
        self.current_index = index
        for w in (self.timeline, self.frame_spin):
            w.blockSignals(True)
            w.setValue(index)
            w.blockSignals(False)
        self.time_label.setText(_timecode(index / float(self.source.fps)))
        self.frame_timer.start()

    def _settings_changed(self, *_) -> None:
        self._save()
        if self.source:
            self._update_scene_label()
            self.settings_timer.start()             # spin boxes change step by step: wait for the last

    def _mode_changed(self, *_) -> None:
        """Another analysis mode: shown at once when it is encoded, encoded next otherwise."""
        self._save()
        if self.source:
            self._update_scene_label()
            self._show()

    def _apply_settings(self) -> None:
        """Inside the encoded scene: a new mode is shown (encoded already or next), other changed
        settings encode the scene's three modes again; a new length makes a new scene. Outside it,
        nothing is encoded."""
        scene, settings = self.scene, self._vvc_settings()
        if scene and scene.covers(self.current_index):
            if settings.frames != scene.frames:
                self._new_scene()
                return
            if settings.without_mode() != scene.base:
                for fga, clip in scene.clips.items():
                    old = scene.older.pop(fga, None)
                    if old:
                        self.retired.append(old.folder)
                    scene.older[fga] = clip
                scene.clips, scene.base, self.stopped = {}, settings.without_mode(), False
                self._remove_retired()
        self._show()

    # ------------------------------------------------------------------ encoding
    def _wanted_crop(self) -> Crop | None:
        return self.crop if self.crop_check.isChecked() else None

    def _scene_range(self) -> tuple[int, int]:
        """First frame and length of the scene "Encode this scene" would encode: centred on the frame shown."""
        length = min(self.frames_spin.value(), self.source.frame_count)
        start = max(0, min(self.current_index - length // 2, self.source.frame_count - length))
        return start, length

    def _encode_scene(self) -> None:
        if not self.source or self.vvc_tools.missing():
            return
        self._new_scene()

    def _new_scene(self) -> None:
        start, length = self._scene_range()
        self._retire_scene()
        self.scene_count += 1
        self.scene = Scene(self.scene_count, start, length, self._wanted_crop(),
                           self._vvc_settings().without_mode(), self.work_root / f"scene-{self.scene_count}")
        self.stopped = False
        self.preferred_mode = GRAIN
        self._show()

    def _retire_scene(self) -> None:
        if self.scene:
            self.retired.append(self.scene.folder)
            self.scene = None
            self._remove_retired()

    def _remove_retired(self) -> None:
        """Delete replaced files, unless a frame is being read from them or a job still writes there."""
        if self.frame_job and self.frame_job.isRunning():
            return                                  # again when that frame job finishes
        busy = self.job_folder if self.job and self.job.isRunning() else None
        keep = []
        for folder in self.retired:
            if busy and (busy == folder or folder in busy.parents):
                keep.append(folder)                 # again when that job finishes
            else:
                shutil.rmtree(folder, ignore_errors=True)
        self.retired = keep

    def _show(self) -> None:
        """Show the current frame: the selected mode's encode inside the scene, the source otherwise."""
        if not self.source:
            return
        if self.crop_check.isChecked() and not self.crop_known:
            self._start_job(("crop", str(self.source.path)), self._detect_bars())
            return
        self._schedule()
        index, scene = self.current_index, self.scene
        if scene and scene.covers(index):
            fga = int(self.fga_combo.currentData())
            clip = scene.clips.get(fga) or scene.older.get(fga)
            if clip and clip.covers(index):
                self._render(clip, index)
                return
        self._render_source(index)

    def _schedule(self) -> None:
        """Start the scene's next piece of work: the cut, then the selected mode, then the other two."""
        if self.job and self.job.isRunning():
            if not self._job_wanted(self.job_key):
                self.job.cancel()                   # its finished signal schedules again
            return
        scene = self.scene
        if not scene or self.stopped:
            return
        if scene.cut is None:
            self._start_job(("cut", scene.ident), self._cut(scene))
            return
        selected = int(self.fga_combo.currentData())
        missing = [m for m in [selected, *sorted(FGA_MODES)] if m not in scene.clips]
        if missing:
            self._start_job(("encode", scene.ident, scene.base, missing[0]), self._encode(scene, missing[0]))

    def _job_wanted(self, key: tuple | None) -> bool:
        scene = self.scene
        if not key or key[0] == "crop":
            return True
        if not scene or key[1] != scene.ident:
            return False
        if key[0] == "cut":
            return scene.cut is None
        return key[2] == scene.base and key[3] not in scene.clips

    def _detect_bars(self):
        source, ffmpeg = self.source, self.tools.ffmpeg

        def work(token, progress):
            progress(-1, "Looking for black bars…")
            return detect_black_bars(ffmpeg, source, token)

        def done(crop):
            if self.source is None or self.source.path != source.path:
                return                              # found for a video no longer loaded
            self.crop, self.crop_known = crop, True
            self.crop_label.setText(f"Picture area {crop.width}×{crop.height} at ({crop.x}, {crop.y})" if crop
                                    else "No black bars found.")
        return "Black bar detection", work, done, None

    def _cut(self, scene: Scene):
        source, ffmpeg = self.source, self.tools.ffmpeg

        def work(token, progress):
            return cut_scene(ffmpeg, source, scene.start, scene.frames, scene.crop, scene.folder, token, progress)

        def done(cut: SceneCut):
            if self.scene is scene:
                scene.cut = cut                     # otherwise the scene's folder is retired already
        return "Reading the scene", work, done, scene.folder

    def _encode(self, scene: Scene, fga: int):
        settings, vvc = scene.settings(fga), self.vvc_tools

        def work(token, progress):
            return encode_scene(vvc, scene.cut, settings, token, progress)

        def done(clip: Clip):
            if self.scene is not scene or scene.base != settings.without_mode():
                self.retired.append(clip.folder)    # settings or scene changed meanwhile
                return
            old = scene.older.pop(fga, None)
            if old:
                self.retired.append(old.folder)
            scene.clips[fga] = clip
            left = [m for m in FGA_MODES if m not in scene.clips]
            self.status_label.setText(
                f"--fga {fga} encoded in {clip.encode_seconds:.1f} s ({clip.kbps:,.0f} kb/s)"
                + (f"; --fga {', '.join(map(str, left))} next, in the background." if left else
                   "; all three modes are ready."))
        return f"Encoding --fga {fga}", work, done, scene.folder

    def _start_job(self, key: tuple, spec) -> None:
        name, work, done, folder = spec
        if self.job and self.job.isRunning():
            return
        self.job_key, self.job_folder = key, folder
        self.job = Job(name, work, self)
        self.job.progress.connect(lambda f, m: self.status_label.setText(m))
        self.job.succeeded.connect(done)
        self.job.failed.connect(lambda msg, n=name, k=key: self._failed(n, k, msg))
        self.job.cancelled.connect(lambda: self.status_label.setText("Cancelled."))
        self.job.finished.connect(self._job_finished)
        self.progress.show()
        self.cancel_button.show()
        self._update_scene_label()
        self.job.start()

    def _job_finished(self) -> None:
        self.progress.hide()
        self.cancel_button.hide()
        self.job_key = self.job_folder = None
        self._remove_retired()
        self._update_scene_label()
        QTimer.singleShot(0, self._show)            # the next piece of work, or the frame asked for meanwhile

    def _failed(self, name: str, key: tuple, message: str) -> None:
        self.status_label.setText(f"{name} failed.")
        if key[0] == "crop":
            self.crop_known = True                  # show the whole picture rather than try again
            self.crop_label.setText("Black bar detection failed: " + message.splitlines()[0])
            return
        self.stopped = True                         # not again until asked
        self.state.clear(f"{name} failed:\n\n{message}")

    def _stop(self) -> None:
        """The Cancel button: stop the scene's work until Encode this scene or a setting asks again."""
        if self.job and self.job.isRunning():
            self.stopped = True
            self.job.cancel()

    # ------------------------------------------------------------------ frames
    def _geometry(self) -> Geometry:
        """10-bit 4:2:0 at the picture size encoded: what VVenC codes and what is shown."""
        crop = self._wanted_crop()
        w, h = (crop.width, crop.height) if crop else (self.source.width, self.source.height)
        return Geometry(w, h, 10, 1, 1)

    def _converter(self, g: Geometry) -> display.Converter:
        src = self.source
        if self.converter is None or (self.converter.fmt.width, self.converter.fmt.height) != (g.width, g.height):
            self.converter = display.Converter(display.DisplayFormat(
                g.bit_depth, src.limited_range, src.matrix, src.transfer, src.primaries, g.width, g.height))
        return self.converter

    def _render_source(self, index: int) -> None:
        """Show the source frame alone (no encode): browsing for a scene."""
        if self.frame_job and self.frame_job.isRunning():
            self.frame_pending = True
            return
        self.frame_pending = False
        src, crop, g, ffmpeg = self.source, self._wanted_crop(), self._geometry(), self.tools.ffmpeg
        converter = self._converter(g)
        cached = self.source_frames.get(index)
        backwards = index < self.last_read
        self.last_read = index
        first = max(0, index - PREFETCH + 1) if backwards else index

        def work(token, progress):
            frames = [cached] if cached else read_frames(ffmpeg, src, first, PREFETCH, token, g, crop)
            frame = next((f for f in frames if f.index == index), None)
            if frame is None:
                raise RuntimeError(f"frame {index:,} could not be decoded (a cut file starts with pictures "
                                   "that need earlier frames)")
            return index, frames, _qimage(converter.to_rgb(frame.y, frame.u, frame.v))

        job = Job("Frame", work, self)
        job.succeeded.connect(lambda result: self._source_ready(*result))
        job.failed.connect(lambda msg: self.state.clear(f"Could not show frame {index:,}:\n\n{msg}"))
        job.finished.connect(self._frame_job_finished)
        self.frame_job = job
        job.start()

    def _source_ready(self, index: int, frames: list[Frame], image: QImage) -> None:
        for f in frames:
            self.source_frames[f.index] = f
            self.source_frames.move_to_end(f.index)
        while len(self.source_frames) > SOURCE_CACHE:
            self.source_frames.popitem(last=False)
        if index != self.current_index:
            self.frame_pending = True
            return
        self.state.set_images({SOURCE: image}, f"frame {index:,} · {_timecode(index / float(self.source.fps))}")
        scene = self.scene
        if scene and scene.covers(index) and not self.stopped:
            text = f"Encoding --fga {int(self.fga_combo.currentData())} for this scene…"
        else:
            text = ("Source frame. Press Encode this scene (E) to encode the frames around it with VVenC "
                    "and see them without and with the grain synthesis.")
        self.info_label.setText(text)
        if not (self.job and self.job.isRunning()):
            self.status_label.setText(f"Frame {index:,}.")
        self._update_scene_label()

    def _update_scene_label(self) -> None:
        if not self.source:
            self.scene_label.setText("")
            return
        scene, key = self.scene, self.job_key if self.job and self.job.isRunning() else None
        start, length = self._scene_range()
        text = f"Encodes frames {start:,}–{start + length - 1:,} ({length / float(self.source.fps):.1f} s)."
        if scene:
            first, count = (scene.cut.start, scene.cut.count) if scene.cut else (scene.start, scene.frames)

            def status(m: int) -> str:
                if m in scene.clips:
                    return "ready"
                if key and key[0] == "encode" and key[1] == scene.ident and key[3] == m:
                    return "encoding"
                return "stopped" if self.stopped else "waiting"
            modes = " · ".join(f"--fga {m} {status(m)}" for m in sorted(FGA_MODES))
            where = "This scene" if scene.covers(self.current_index) else "Encoded scene"
            text = f"{where}: frames {first:,}–{first + count - 1:,}. {modes}."
            if scene.covers(self.current_index):
                if scene.base != self._vvc_settings().without_mode():
                    text += " Encoded with other settings than these: press Encode this scene to use them."
                elif scene.older and int(self.fga_combo.currentData()) not in scene.clips:
                    text += " Shown with the earlier settings until the new encode is ready."
            else:
                text += f" Encode this scene would encode frames {start:,}–{start + length - 1:,}."
        self.scene_label.setText(text)

    def _render(self, clip: Clip, index: int) -> None:
        if self.frame_job and self.frame_job.isRunning():
            self.frame_pending = True
            return
        self.frame_pending = False
        if index < clip.start:
            self.state.clear(f"Frame {index:,} cannot be decoded from the source (a cut file starts with "
                             "pictures that need earlier frames).")
            return
        g = clip.geometry
        converter = self._converter(g)

        def work(token, progress):
            frames = {which: clip.frame(which, index) for which in (SOURCE_FILE, DECODED, GRAIN_FILE)}
            s, d, gr = frames[SOURCE_FILE], frames[DECODED], frames[GRAIN_FILE]
            with ThreadPoolExecutor(3) as pool:
                jobs = {SOURCE: pool.submit(lambda: _qimage(converter.to_rgb(s.y, s.u, s.v))),
                        ENCODED: pool.submit(lambda: _qimage(converter.to_rgb(d.y, d.u, d.v))),
                        GRAIN: pool.submit(lambda: _qimage(converter.to_rgb(gr.y, gr.u, gr.v)))}
                only = _qimage(display.grain_only(d.y, gr.y, g.bit_depth))
                stats = frame_stats(s, d, gr)
                images = {k: j.result() for k, j in jobs.items()}
            images[GRAIN_ONLY] = only
            return index, images, stats

        job = Job("Frame", work, self)
        job.succeeded.connect(lambda result, c=clip: self._frame_ready(c, *result))
        job.failed.connect(lambda msg: self.state.clear(f"Could not show frame {index:,}:\n\n{msg}"))
        job.finished.connect(self._frame_job_finished)
        self.frame_job = job
        job.start()

    def _frame_job_finished(self) -> None:
        self._remove_retired()
        if self.frame_pending:
            self._show()

    def _frame_ready(self, clip: Clip, index: int, images: dict, stats) -> None:
        if index != self.current_index:
            self.frame_pending = True
            return
        caption = f"frame {index:,} · {_timecode(index / float(self.source.fps))}"
        self.state.set_images(images, caption)
        self.state.set_mode(self.preferred_mode)
        self._update_scene_label()
        grain = clip.grain_at(index)
        signalled = grain.describe() if grain else "no film grain parameters"
        n_sets = len({g.describe() for g in clip.grain if g})
        y, cb, cr = stats.removed
        ay, acb, acr = stats.added
        sy, scb, scr = stats.shift
        s = clip.settings
        self.info_label.setText(
            f"Signalled (FGC SEI) for this frame:\n{signalled}\n\n"
            f"Measured on this frame, 8-bit units:\n"
            f"  removed by VVenC  σ  Y {y:5.2f}  Cb {cb:5.2f}  Cr {cr:5.2f}\n"
            f"  added by vvdec    σ  Y {ay:5.2f}  Cb {acb:5.2f}  Cr {acr:5.2f}\n"
            f"  colour shift      mean Y {sy:+.3f}  Cb {scb:+.3f}  Cr {scr:+.3f}\n\n"
            f"Scene: frames {clip.start:,}–{clip.start + clip.count - 1:,}, {n_sets} parameter "
            f"set{'s' if n_sets != 1 else ''}; --fga {s.fga}, {s.strength} %, QP {s.qp:g}, {s.preset}; "
            f"{clip.kbps:,.0f} kb/s, encoded in {clip.encode_seconds:.1f} s.")
        if not (self.job and self.job.isRunning()):
            self.status_label.setText(f"Frame {index:,}.")

    # ------------------------------------------------------------------ view modes
    def _set_mode(self, mode: int) -> None:
        if self.state.set_mode(mode):
            self.preferred_mode = mode
        elif mode != SOURCE and self.source:
            self.status_label.setText("Press Encode this scene (E) to see the VVC views of this frame.")
        self._sync_mode_buttons()

    def _sync_mode_buttons(self) -> None:
        for mode, button in self.mode_buttons.items():
            button.setEnabled(mode in self.state.images)
            button.setChecked(mode == self.state.mode and bool(self.state.images))

    def _open_viewer_window(self) -> None:
        if self.viewer_window is None:
            self.viewer_window = ViewerWindow(self.state)
            self._install_shortcuts(self.viewer_window, self.viewer_window.view)
        self.viewer_window.show_sized()

    def closeEvent(self, event) -> None:
        self._save()
        for job in (self.job, self.frame_job):
            if job and job.isRunning():
                job.cancel()
                job.wait(10000)
        if self.viewer_window:
            self.viewer_window.close()
        self.frame_job = None
        self.job = None
        self._retire_scene()
        self.work_lock.close()
        shutil.rmtree(self.work_root, ignore_errors=True)
        event.accept()
