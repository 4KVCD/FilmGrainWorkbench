"""VVC grain analyzer: a source video, after VVenC removed its grain, and with vvdec's grain synthesis.

Browsing shows the source alone, decoded by ffmpeg. "Encode this scene" encodes
the frames around the one shown with VVenC in all three film grain analysis
modes: the selected one first, shown as soon as it is ready, the other two in
the background, so switching modes is instant. Inside that scene every view is
available, and a changed VVenC setting encodes the scene again.

Temporary files: a tab keeps one scene (its source frames and the three
encodes, each decoded twice) in its own folder in %TEMP%, deletes whatever is
replaced as soon as nothing reads it, and the folder when it closes. Folders
left by a crash are deleted when the next one opens.
"""

from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
import os
import shutil

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QFont, QImage, QKeySequence, QShortcut
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout,
                               QGroupBox, QLabel, QLineEdit, QMainWindow, QPushButton, QScrollArea, QSpinBox,
                               QSplitter, QVBoxLayout, QWidget)

from .. import display
from ..frames import Crop, Frame, Geometry, read_frames
from ..probe import VideoInfo, probe
from ..tools import Tools, ToolError, load_tools
from ..vvcgrain import (DECODED, FGA_MODES, GRAIN as GRAIN_FILE, PRESETS, SOURCE as SOURCE_FILE, Clip,
                        FrameStats, SceneCut, VvcSettings, cut_scene, detect_black_bars, encode_scene,
                        find_vvc_tools, frame_stats, open_work_dir)
from ..paths import missing_program_hint
from .frame_pane import SHORTCUT_HELP, FramePane, StatusBar, install_viewer_shortcuts, timecode, to_qimage
from .jobs import Job, running
from .viewer import ENCODED, GRAIN, GRAIN_ONLY, SOURCE, PreviewState, ViewerWindow

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
FRAME_DELAY_MS = 120            # a frame is shown once stepping pauses this long
SETTINGS_DELAY_MS = 600         # spin boxes change step by step: encode after the last step


@dataclass(frozen=True)
class JobKey:
    """What the tab's one background job does: find the black bars, cut a scene, or encode a mode."""
    kind: str                                      # "crop", "cut" or "encode"
    scene: int = 0                                 # Scene.ident
    coding: tuple = ()                             # VvcSettings.coding() of an encode
    fga: int = 0                                   # the mode an encode is for


@dataclass
class Scene:
    """The frames "Encode this scene" was pressed for: one cut of the source, an encode per mode."""
    ident: int
    start: int
    frames: int
    crop: Crop | None
    base: tuple[int, float, str]                   # VvcSettings.coding() the encodes use
    folder: Path
    cut: SceneCut | None = None
    clips: dict[int, Clip] = field(default_factory=dict)      # by --fga, encoded with ``base``
    older: dict[int, Clip] = field(default_factory=dict)      # earlier settings, shown until replaced

    def covers(self, index: int) -> bool:
        start, count = (self.cut.start, self.cut.count) if self.cut else (self.start, self.frames)
        return start <= index < start + count

    def holds(self, index: int, frames: int, crop: Crop | None) -> bool:
        """Whether Encode this scene at ``index`` would cut these same frames again."""
        return self.covers(index) and (self.frames, self.crop) == (frames, crop)

    def settings(self, fga: int) -> VvcSettings:
        return VvcSettings(fga, *self.base, self.frames)

    def next_work(self, selected: int) -> JobKey | None:
        """The cut first, then the selected mode, then the others in order; None when all are done."""
        if self.cut is None:
            return JobKey("cut", self.ident)
        missing = [m for m in (selected, *sorted(FGA_MODES)) if m not in self.clips]
        return JobKey("encode", self.ident, self.base, missing[0]) if missing else None

    def wants(self, key: JobKey) -> bool:
        """Whether the result of the job ``key`` still belongs in this scene."""
        if key.scene != self.ident:
            return False
        if key.kind == "cut":
            return self.cut is None
        return key.coding == self.base and key.fga not in self.clips


class VvcGrainWindow(QMainWindow):
    def __init__(self, settings: QSettings | None = None, work_dir: Path | None = None) -> None:
        super().__init__()
        self.setWindowTitle("VVC Grain Analyzer — VVenC film grain analysis + vvdec synthesis")
        self.settings = settings or QSettings("AFGS1Workbench", "AFGS1Workbench")
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
        self.job: Job | None = None                 # black-bar detection, a cut or an encode
        self.job_key: JobKey | None = None
        self.job_folder: Path | None = None         # where the running job writes
        self.converter: display.Converter | None = None
        self.current_index = 0
        self.frame_job: Job | None = None
        self.frame_pending = False
        self.source_frames: OrderedDict[tuple[int, Crop | None], Frame] = OrderedDict()
        self.last_read = 0
        self.preferred_mode = GRAIN                 # the view chosen last, restored inside a scene
        self.state = PreviewState(VIEW_NAMES)
        self.viewer_window: ViewerWindow | None = None
        self.started = False                        # the controls are restored and may be saved
        self._build()
        self._restore()
        self.frame_timer = QTimer(self, singleShot=True, interval=FRAME_DELAY_MS)
        self.frame_timer.timeout.connect(self._show)
        self.settings_timer = QTimer(self, singleShot=True, interval=SETTINGS_DELAY_MS)
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
        side.addWidget(self._source_group())
        side.addWidget(self._vvenc_group())
        side.addWidget(self._frame_group())
        side.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(sidebar)
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(440)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.pane = FramePane(self.state, VIEW_TIPS, hint=SHORTCUT_HELP.format(left="VVC", right="VVC + grain"))
        self.pane.frame_requested.connect(self._go_to)
        self.pane.step_requested.connect(self._step)
        self.pane.mode_requested.connect(self._set_mode)
        self.pane.popout_requested.connect(self._open_viewer_window)
        self.view = self.pane.view

        self.splitter = QSplitter()
        self.splitter.addWidget(scroll)
        self.splitter.addWidget(self.pane)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([470, 1100])
        self.setCentralWidget(self.splitter)
        self.status = StatusBar(self, self._stop, progress_width=240)

        self.state.changed.connect(self._sync_mode_buttons)
        self._install_shortcuts(self)
        self._update_tools()

    def _source_group(self) -> QGroupBox:
        box = QGroupBox("1 · Source")
        grid = QGridLayout(box)
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
        return box

    def _vvenc_group(self) -> QGroupBox:
        box = QGroupBox("2 · VVenC")
        form = QFormLayout(box)
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
        self.frames_spin.setToolTip("Frames Encode this scene encodes, centred on the frame shown. VVenC "
                                    "analyses the grain once per GOP (32 frames), so 33 covers one GOP and "
                                    "the next.")
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
        return box

    def _frame_group(self) -> QGroupBox:
        box = QGroupBox("3 · This frame")
        column = QVBoxLayout(box)
        self.info_label = QLabel("Choose a source video.")
        self.info_label.setWordWrap(True)
        self.info_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.info_label.setFont(mono)
        column.addWidget(self.info_label)
        return box

    def _install_shortcuts(self, widget: QWidget) -> None:
        """The viewer's keys, and E for Encode this scene, on the tab or the frame window."""
        view = self.viewer_window.view if widget is self.viewer_window else self.view
        install_viewer_shortcuts(widget, view, self._set_mode, self._step, self._frames_per_second)
        QShortcut(QKeySequence("E"), widget, self._encode_scene)

    # ------------------------------------------------------------------ settings
    def _load_tools(self) -> Tools:
        return load_tools(self.settings)

    def reload_tools(self) -> None:
        """ffmpeg and ffprobe as set up in Settings → External programs (shared with the AV1 tab)."""
        self.tools = self._load_tools()
        self._update_tools()

    def _update_tools(self) -> None:
        problems = []
        external = self.tools.missing("ffmpeg", "ffprobe")
        if external:
            problems.append(f"Missing: {', '.join(external)} (Settings → External programs).")
        bundled = self.vvc_tools.missing()
        if bundled:
            problems.append(f"Missing: {', '.join(bundled)}. {missing_program_hint()}")
        self.tools_banner.setVisible(bool(problems))
        self.tools_banner.setText(" ".join(problems))
        self.tools_label.setText(f"vvencapp: {self.vvc_tools.vvencapp or '—'}\n"
                                 f"vvdecapp: {self.vvc_tools.vvdecapp or '—'}")

    def _restore(self) -> None:
        s = self.settings
        index = self.fga_combo.findData(int(s.value("vvc/fga", 2)))
        self.fga_combo.setCurrentIndex(max(0, index))
        self.strength_spin.setValue(int(s.value("vvc/strength", 100)))
        self.qp_spin.setValue(float(s.value("vvc/qp", 28)))
        self.preset_combo.setCurrentText(str(s.value("vvc/preset", "faster")))
        self.frames_spin.setValue(int(s.value("vvc/frames", 33)))
        self.crop_check.setChecked(s.value("vvc/crop", "true") in (True, "true"))

    def _save(self) -> None:
        if not self.started:
            return      # restoring the controls fires their change signals: they would save an empty source
        s = self.settings
        s.setValue("vvc/source", self.source_edit.text())
        s.setValue("vvc/fga", self.fga_combo.currentData())
        s.setValue("vvc/strength", self.strength_spin.value())
        s.setValue("vvc/qp", self.qp_spin.value())
        s.setValue("vvc/preset", self.preset_combo.currentText())
        s.setValue("vvc/frames", self.frames_spin.value())
        s.setValue("vvc/crop", self.crop_check.isChecked())

    def _vvc_settings(self) -> VvcSettings:
        return VvcSettings(self._selected_mode(), self.strength_spin.value(), float(self.qp_spin.value()),
                           self.preset_combo.currentText(), self.frames_spin.value())

    def _selected_mode(self) -> int:
        return int(self.fga_combo.currentData())

    def _startup(self) -> None:
        self.started = True
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
        self.pane.set_frame_count(self.source.frame_count)
        self._save()
        self._go_to(self.source.frame_count // 3, force=True)

    def _crop_toggled(self) -> None:
        self._save()
        self.source_frames.clear()
        self._retire_scene()
        if self.source:
            self.converter = None
            self._show()

    # ------------------------------------------------------------------ navigation
    def _frames_per_second(self) -> int:
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
        self.pane.show_frame(index, index / float(self.source.fps))
        self.frame_timer.start()

    def _settings_changed(self, *_) -> None:
        self._save()
        if self.source:
            self._update_scene_label()
            self.settings_timer.start()

    def _mode_changed(self, *_) -> None:
        """Another analysis mode: shown at once when it is encoded, encoded next otherwise."""
        self._save()
        if self.source:
            self._update_scene_label()
            self._show()

    def _apply_settings(self) -> None:
        """Inside the encoded scene, new strength, QP or preset encode its three modes again.

        Outside it nothing is encoded, and a new scene length waits for Encode
        this scene: it decides which frames the next scene holds.
        """
        scene = self.scene
        if scene and scene.covers(self.current_index):
            self._recode(scene, self._vvc_settings().coding())
        self._show()

    # ------------------------------------------------------------------ scenes
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
        _, length = self._scene_range()
        scene = self.scene
        if scene and scene.holds(self.current_index, length, self._wanted_crop()):
            # The same frames: continue a stopped scene, or encode it with the current settings,
            # keeping its cut and every mode already encoded with them.
            self.stopped = False
            self._recode(scene, self._vvc_settings().coding())
            self.preferred_mode = GRAIN
            self._show()
            return
        self._new_scene()

    def _new_scene(self) -> None:
        start, length = self._scene_range()
        self._retire_scene()
        self.scene_count += 1
        self.scene = Scene(self.scene_count, start, length, self._wanted_crop(),
                           self._vvc_settings().coding(), self.work_root / f"scene-{self.scene_count}")
        self.stopped = False
        self.preferred_mode = GRAIN
        self._show()

    def _recode(self, scene: Scene, coding: tuple[int, float, str]) -> None:
        """Encode the scene's modes again with other coding settings; the current ones stay
        on screen (as ``older``) until each replacement is ready."""
        if coding == scene.base:
            return
        self.stopped = False
        for fga, clip in scene.clips.items():
            old = scene.older.pop(fga, None)
            if old:
                self.retired.append(old.folder)
            scene.older[fga] = clip
        scene.clips, scene.base = {}, coding
        self._remove_retired()

    def _retire_scene(self) -> None:
        if self.scene:
            self.retired.append(self.scene.folder)
            self.scene = None
            self._remove_retired()

    def _remove_retired(self) -> None:
        """Delete replaced files, unless a frame is being read from them or a job still writes there."""
        if running(self.frame_job):
            return                                  # again when that frame job finishes
        busy = self.job_folder if running(self.job) else None
        keep = []
        for folder in self.retired:
            if busy and (busy == folder or folder in busy.parents):
                keep.append(folder)                 # again when that job finishes
            else:
                shutil.rmtree(folder, ignore_errors=True)
        self.retired = keep

    # ------------------------------------------------------------------ what is shown, what runs
    def _show(self) -> None:
        """Show the current frame: the selected mode's encode inside the scene, the source otherwise."""
        if not self.source:
            return
        if self.crop_check.isChecked() and not self.crop_known:
            # The whole picture meanwhile; the cropped one follows when the bars are known.
            self._start_job(JobKey("crop"), self._detect_bars())
            self._render_source(self.current_index)
            return
        self._schedule()
        index, scene = self.current_index, self.scene
        if scene and scene.covers(index):
            fga = self._selected_mode()
            clip = scene.clips.get(fga) or scene.older.get(fga)
            if clip and clip.covers(index):
                self._render(clip, index)
                return
        self._render_source(index)

    def _schedule(self) -> None:
        """Start the scene's next piece of work, or cancel a job whose result is no longer wanted."""
        if running(self.job):
            if self._job_unwanted(self.job_key):
                self.job.cancel()                   # its finished signal schedules again
            return
        scene = self.scene
        if not scene or self.stopped:
            return
        key = scene.next_work(self._selected_mode())
        if key is None:
            return
        self._start_job(key, self._cut(scene) if key.kind == "cut" else self._encode(scene, key.fga))

    def _job_unwanted(self, key: JobKey | None) -> bool:
        """The running job's result is not needed, or it holds up the mode chosen in the list.

        A background encode of another mode is cancelled while the chosen mode
        is missing: the chosen one shows a few seconds sooner, and the cancelled
        one is encoded again afterwards.
        """
        if key is None or key.kind == "crop":
            return False
        scene = self.scene
        if scene is None or not scene.wants(key):
            return True
        selected = self._selected_mode()
        return key.kind == "encode" and key.fga != selected and selected not in scene.clips \
            and not self.stopped

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
            if self.scene is not scene or scene.base != settings.coding():
                self.retired.append(clip.folder)    # settings or scene changed meanwhile
                return
            old = scene.older.pop(fga, None)
            if old:
                self.retired.append(old.folder)
            scene.clips[fga] = clip
            left = [m for m in FGA_MODES if m not in scene.clips]
            self.status.label.setText(
                f"--fga {fga} encoded in {clip.encode_seconds:.1f} s ({clip.kbps:,.0f} kb/s)"
                + (f"; --fga {', '.join(map(str, left))} next, in the background." if left else
                   "; all three modes are ready."))
        return f"Encoding --fga {fga}", work, done, scene.folder

    def _start_job(self, key: JobKey, spec) -> None:
        name, work, done, folder = spec
        if running(self.job):
            return
        self.job_key, self.job_folder = key, folder
        self.job = Job(name, work, self)
        self.job.progress.connect(lambda fraction, message: self.status.label.setText(message))
        self.job.succeeded.connect(done)
        self.job.failed.connect(lambda message, n=name, k=key: self._failed(n, k, message))
        self.job.cancelled.connect(self._job_cancelled)
        self.job.finished.connect(self._job_finished)
        self.status.busy()
        self._update_scene_label()
        self.job.start()

    def _job_finished(self) -> None:
        self.status.idle()
        self.job_key = self.job_folder = None
        self._remove_retired()
        self._update_scene_label()
        QTimer.singleShot(0, self._show)            # the next piece of work, or the frame asked for meanwhile

    def _job_cancelled(self) -> None:
        if self.stopped:                            # Cancel pressed; other cancels are rescheduling
            self.status.label.setText("Cancelled.")

    def _failed(self, name: str, key: JobKey, message: str) -> None:
        self.status.label.setText(f"{name} failed.")
        if key.kind == "crop":
            self.crop_known = True                  # show the whole picture rather than try again
            self.crop_label.setText("Black bar detection failed: " + message.splitlines()[0])
            return
        self.stopped = True                         # not again until asked
        self.state.clear(f"{name} failed:\n\n{message}")

    def _stop(self) -> None:
        """The Cancel button: stop the scene's work until Encode this scene or a setting asks again."""
        if running(self.job):
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

    def _caption(self, index: int) -> str:
        return f"frame {index:,} · {timecode(index / float(self.source.fps))}"

    def _start_frame_job(self, index: int, work, on_ready) -> None:
        job = Job("Frame", work, self)
        job.succeeded.connect(on_ready)
        job.failed.connect(lambda message: self.state.clear(f"Could not show frame {index:,}:\n\n{message}"))
        job.finished.connect(self._frame_job_finished)
        self.frame_job = job
        job.start()

    def _render_source(self, index: int) -> None:
        """Show the source frame alone (no encode): browsing for a scene."""
        if running(self.frame_job):
            self.frame_pending = True
            return
        self.frame_pending = False
        src, crop, g, ffmpeg = self.source, self._wanted_crop(), self._geometry(), self.tools.ffmpeg
        converter = self._converter(g)
        cached = self.source_frames.get((index, crop))
        backwards = index < self.last_read
        self.last_read = index
        first = max(0, index - PREFETCH + 1) if backwards else index

        def work(token, progress):
            frames = [cached] if cached else read_frames(ffmpeg, src, first, PREFETCH, token, g, crop)
            frame = next((f for f in frames if f.index == index), None)
            if frame is None:
                raise RuntimeError(f"frame {index:,} could not be decoded (a cut file starts with pictures "
                                   "that need earlier frames)")
            return index, frames, to_qimage(converter.to_rgb(frame.y, frame.u, frame.v))

        self._start_frame_job(index, work, lambda result, c=crop: self._source_ready(*result, c))

    def _source_ready(self, index: int, frames: list[Frame], image: QImage, crop: Crop | None) -> None:
        for f in frames:
            self.source_frames[(f.index, crop)] = f
            self.source_frames.move_to_end((f.index, crop))
        while len(self.source_frames) > SOURCE_CACHE:
            self.source_frames.popitem(last=False)
        if index != self.current_index:
            self.frame_pending = True
            return
        self.state.set_images({SOURCE: image}, self._caption(index))
        scene = self.scene
        if scene and scene.covers(index) and not self.stopped:
            text = f"Encoding --fga {self._selected_mode()} for this scene…"
        else:
            text = ("Source frame. Press Encode this scene (E) to encode the frames around it with VVenC "
                    "and see them without and with the grain synthesis.")
        self.info_label.setText(text)
        if not running(self.job):
            self.status.label.setText(f"Frame {index:,}.")
        self._update_scene_label()

    def _render(self, clip: Clip, index: int) -> None:
        """Show the frame from an encoded scene: source, decoded, decoded + grain, grain only."""
        if running(self.frame_job):
            self.frame_pending = True
            return
        self.frame_pending = False
        g = clip.geometry
        converter = self._converter(g)

        def work(token, progress):
            s, d, gr = (clip.frame(which, index) for which in (SOURCE_FILE, DECODED, GRAIN_FILE))
            with ThreadPoolExecutor(3) as pool:
                jobs = {view: pool.submit(lambda f=f: to_qimage(converter.to_rgb(f.y, f.u, f.v)))
                        for view, f in ((SOURCE, s), (ENCODED, d), (GRAIN, gr))}
                only = to_qimage(display.grain_only(d.y, gr.y, g.bit_depth))
                stats = frame_stats(s, d, gr)
                images = {view: job.result() for view, job in jobs.items()}
            images[GRAIN_ONLY] = only
            return index, images, stats

        self._start_frame_job(index, work, lambda result, c=clip: self._frame_ready(c, *result))

    def _frame_job_finished(self) -> None:
        self._remove_retired()
        if self.frame_pending:
            self._show()

    def _frame_ready(self, clip: Clip, index: int, images: dict, stats: FrameStats) -> None:
        if index != self.current_index:
            self.frame_pending = True
            return
        self.state.set_images(images, self._caption(index))
        self.state.set_mode(self.preferred_mode)
        self._update_scene_label()
        self.info_label.setText(self._frame_text(clip, index, stats))
        if not running(self.job):
            self.status.label.setText(f"Frame {index:,}.")

    @staticmethod
    def _frame_text(clip: Clip, index: int, stats: FrameStats) -> str:
        grain = clip.grain_at(index)
        signalled = grain.describe() if grain else "no film grain parameters"
        n_sets = len({g.describe() for g in clip.grain if g})
        y, cb, cr = stats.removed
        ay, acb, acr = stats.added
        sy, scb, scr = stats.shift
        s = clip.settings
        return (f"Signalled (FGC SEI) for this frame:\n{signalled}\n\n"
                f"Measured on this frame, 8-bit units:\n"
                f"  removed by VVenC  σ  Y {y:5.2f}  Cb {cb:5.2f}  Cr {cr:5.2f}\n"
                f"  added by vvdec    σ  Y {ay:5.2f}  Cb {acb:5.2f}  Cr {acr:5.2f}\n"
                f"  colour shift      mean Y {sy:+.3f}  Cb {scb:+.3f}  Cr {scr:+.3f}\n\n"
                f"Scene: frames {clip.start:,}–{clip.start + clip.count - 1:,}, {n_sets} parameter "
                f"set{'s' if n_sets != 1 else ''}; --fga {s.fga}, {s.strength} %, QP {s.qp:g}, {s.preset}; "
                f"{clip.kbps:,.0f} kb/s, encoded in {clip.encode_seconds:.1f} s.")

    def _update_scene_label(self) -> None:
        if not self.source:
            self.scene_label.setText("")
            return
        scene, key = self.scene, self.job_key if running(self.job) else None
        start, length = self._scene_range()
        text = f"Encodes frames {start:,}–{start + length - 1:,} ({length / float(self.source.fps):.1f} s)."
        if scene:
            first, count = (scene.cut.start, scene.cut.count) if scene.cut else (scene.start, scene.frames)

            def status(m: int) -> str:
                if m in scene.clips:
                    return "ready"
                if key and key.kind == "encode" and key.scene == scene.ident and key.fga == m:
                    return "encoding"
                return "stopped" if self.stopped else "waiting"
            modes = " · ".join(f"--fga {m} {status(m)}" for m in sorted(FGA_MODES))
            here = scene.covers(self.current_index)
            text = f"{'This scene' if here else 'Encoded scene'}: frames {first:,}–{first + count - 1:,}. {modes}."
            if here:
                if scene.base != self._vvc_settings().coding():
                    text += " Encoded with other settings than these: press Encode this scene to use them."
                elif scene.older and self._selected_mode() not in scene.clips:
                    text += " Shown with the earlier settings until the new encode is ready."
                if self.frames_spin.value() != scene.frames:
                    text += (f" The new scene length ({self.frames_spin.value()}) applies when Encode this "
                             "scene is pressed again.")
            else:
                text += f" Encode this scene would encode frames {start:,}–{start + length - 1:,}."
        self.scene_label.setText(text)

    # ------------------------------------------------------------------ view modes
    def _set_mode(self, mode: int) -> None:
        if self.state.set_mode(mode):
            self.preferred_mode = mode
        elif mode != SOURCE and self.source:
            self.status.label.setText("Press Encode this scene (E) to see the VVC views of this frame.")
        self._sync_mode_buttons()

    def _sync_mode_buttons(self) -> None:
        self.pane.sync_modes(set(self.state.images))

    def _open_viewer_window(self) -> None:
        if self.viewer_window is None:
            self.viewer_window = ViewerWindow(self.state)
            self._install_shortcuts(self.viewer_window)
        self.viewer_window.show_sized()

    # ------------------------------------------------------------------ closing
    def can_close(self) -> bool:
        """Nothing here needs asking: a scene is quick to encode again."""
        return True

    def shutdown(self) -> None:
        """Stop the work and delete the temporary files (the scene, the work folder)."""
        self._save()
        for job in (self.job, self.frame_job):
            if running(job):
                job.cancel()
                job.wait(10000)
        if self.viewer_window:
            self.viewer_window.close()
        self.frame_job = self.job = None
        self._retire_scene()
        self.work_lock.close()
        shutil.rmtree(self.work_root, ignore_errors=True)

    def closeEvent(self, event) -> None:
        if not self.can_close():
            event.ignore()
            return
        self.shutdown()
        event.accept()
