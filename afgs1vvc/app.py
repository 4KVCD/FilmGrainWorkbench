"""PySide6 desktop interface for estimating and writing AFGS1 metadata."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import shutil
import tempfile

from PySide6.QtCore import QSettings, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGraphicsScene, QGraphicsView, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QProgressBar, QPushButton, QSlider,
    QSpinBox, QSplitter, QTabWidget, QTableWidget, QTableWidgetItem, QTextEdit,
    QVBoxLayout, QWidget,
)

from .grain import GrainEntry, parameter_at, parse_aom_table, preview_summary
from .inject import inject_afgs1
from .media import (VideoInfo, estimate_offset_frames, extract_preview_png,
                    probe_video, render_vfgs_preview, run_aom_analysis)


WORK_DIR = Path(__file__).resolve().parents[3] / "work"
WORK_DIR.mkdir(parents=True, exist_ok=True)


class JobThread(QThread):
    succeeded = Signal(object)
    failed = Signal(str)
    progress = Signal(int, str)

    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def run(self):
        try:
            result = self.callback(self.progress.emit)
            self.succeeded.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


def _browse_file(parent, title: str, current: str, file_filter: str) -> str:
    path, _ = QFileDialog.getOpenFileName(parent, title, current, file_filter)
    return path


class FrameViewerDialog(QDialog):
    """Large, zoomable A/B viewer for the encoded and synthesized frames."""

    def __init__(self, encoded_png: bytes, grain_png: bytes,
                 timestamp: float, strength: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Frame viewer · Encoded video")
        self.resize(1400, 900)
        self.setMinimumSize(720, 480)
        self._fit_mode = True
        self._mode = 0

        self.scene = QGraphicsScene(self)
        self.image_item = self.scene.addPixmap(QPixmap())
        self.view = QGraphicsView(self.scene)
        self.view.setObjectName("largeFrameView")
        self.view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.view.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.view.setBackgroundBrush(Qt.GlobalColor.black)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 8)
        layout.setSpacing(6)
        header = QHBoxLayout()
        self.caption = QLabel()
        self.caption.setObjectName("viewerCaption")
        header.addWidget(self.caption)
        header.addStretch(1)
        header.addWidget(QLabel("← Encoded, no grain    → Synthesized grain"))
        layout.addLayout(header)
        layout.addWidget(self.view, 1)

        controls = QHBoxLayout()
        self.fit_button = QPushButton("Fit to window")
        self.fit_button.clicked.connect(self._fit_image)
        controls.addWidget(self.fit_button)
        self.actual_size_button = QPushButton("100%")
        self.actual_size_button.clicked.connect(lambda: self._set_zoom(100))
        controls.addWidget(self.actual_size_button)
        controls.addWidget(QLabel("Zoom"))
        self.zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self.zoom_slider.setRange(25, 300)
        self.zoom_slider.setSingleStep(5)
        self.zoom_slider.setPageStep(25)
        self.zoom_slider.setValue(100)
        self.zoom_slider.valueChanged.connect(self._set_zoom)
        controls.addWidget(self.zoom_slider, 1)
        self.zoom_label = QLabel("Fit")
        self.zoom_label.setMinimumWidth(48)
        controls.addWidget(self.zoom_label)
        layout.addLayout(controls)

        self._left_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Left), self)
        self._left_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self._left_shortcut.activated.connect(lambda: self._show_mode(0))
        self._right_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Right), self)
        self._right_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self._right_shortcut.activated.connect(lambda: self._show_mode(1))

        self.setStyleSheet("""
            QDialog { background: #101820; color: #e6edf3; }
            QLabel#viewerCaption { color: #a9d8cb; font-size: 11pt; font-weight: 700; }
            QGraphicsView { background: #05090c; border: 1px solid #354653; }
            QPushButton, QSlider { color: #e6edf3; }
        """)
        self.set_frames(encoded_png, grain_png, timestamp, strength)

    def set_frames(self, encoded_png: bytes, grain_png: bytes,
                   timestamp: float, strength: int):
        encoded = QPixmap()
        grain = QPixmap()
        if not encoded.loadFromData(encoded_png, "PNG") or not grain.loadFromData(grain_png, "PNG"):
            raise RuntimeError("Could not open the full-resolution preview frames")
        self._images = (encoded, grain)
        self._timestamp = timestamp
        self._strength = strength
        self._show_mode(self._mode)

    def _show_mode(self, mode: int):
        self._mode = mode
        pixmap = self._images[mode]
        self.image_item.setPixmap(pixmap)
        self.scene.setSceneRect(self.image_item.boundingRect())
        if mode == 0:
            label = f"Encoded VVC · no grain · {self._timestamp:.2f} s"
            self.setWindowTitle("Frame viewer · Encoded VVC")
        else:
            label = f"Encoded VVC + VFGS grain · {self._timestamp:.2f} s · {self._strength}%"
            self.setWindowTitle("Frame viewer · Synthesized grain")
        self.caption.setText(label)
        self._update_render_quality()
        if self._fit_mode:
            self._fit_image()

    def _update_render_quality(self):
        self.view.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, self._fit_mode)

    def _fit_image(self):
        self._fit_mode = True
        self.zoom_label.setText("Fit")
        self._update_render_quality()
        if not self.image_item.pixmap().isNull():
            self.view.resetTransform()
            self.view.fitInView(self.image_item, Qt.AspectRatioMode.KeepAspectRatio)

    def _set_zoom(self, percent: int):
        self._fit_mode = False
        self.zoom_label.setText(f"{percent}%")
        self._update_render_quality()
        self.view.resetTransform()
        self.view.scale(percent / 100.0, percent / 100.0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._fit_mode:
            self._fit_image()

    def showEvent(self, event):
        super().showEvent(event)
        if self._fit_mode:
            QTimer.singleShot(0, self._fit_image)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("Codex", "AFGS1VVCWorkbench")
        self.source_info: VideoInfo | None = None
        self.encoded_info: VideoInfo | None = None
        self.entries: list[GrainEntry] = []
        self.table_text = ""
        self.job: JobThread | None = None
        self.preview_pngs: tuple[bytes, bytes] | None = None
        self.preview_mode: str | None = None
        self.preview_update_timer = QTimer(self)
        self.preview_update_timer.setSingleShot(True)
        self.preview_update_timer.setInterval(250)
        self.preview_update_timer.timeout.connect(self._refresh_preview_at_selected_time)
        self.setWindowTitle("AFGS1 VVC Workbench")
        self.resize(1480, 980)
        self._build_ui()
        self._load_settings()

    def _build_ui(self):
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(8)

        hero = QHBoxLayout()
        title_stack = QVBoxLayout()
        title = QLabel("AFGS1 VVC Workbench")
        title.setObjectName("title")
        subtitle = QLabel("Estimate source grain, tune the model, and write T.35 metadata into a VVC MKV.")
        subtitle.setObjectName("subtitle")
        subtitle.setWordWrap(True)
        workflow = QLabel("Choose videos → Align and analyze → Tune grain → Write AFGS1 MKV.")
        workflow.setObjectName("workflow")
        workflow.setWordWrap(True)
        links = QLabel('<a href="https://aomediacodec.github.io/afgs1-spec/">AFGS1 spec</a>'
                       ' &nbsp;·&nbsp; <a href="https://aomedia.googlesource.com/aom/+/refs/heads/main/examples/noise_model.c">AOM noise_model</a>'
                       ' &nbsp;·&nbsp; <a href="https://github.com/InterDigitalInc/VersatileFilmGrain">VFGS synthesizer</a>')
        links.setOpenExternalLinks(True)
        links.setStyleSheet("color: #83cdb2; font-size: 9pt;")
        title_stack.addWidget(title)
        title_stack.addWidget(subtitle)
        title_stack.addWidget(workflow)
        title_stack.addWidget(links)
        hero.addLayout(title_stack)
        hero.addStretch(1)
        layout.addLayout(hero)

        self.source_edit = QLineEdit()
        self.encoded_edit = QLineEdit()
        self.output_edit = QLineEdit()
        self.ffmpeg_edit = QLineEdit()
        self.ffprobe_edit = QLineEdit()
        self.noise_edit = QLineEdit()
        self.vfgs_edit = QLineEdit()
        self.source_edit.setPlaceholderText("Choose the original grainy source video")
        self.encoded_edit.setPlaceholderText("Choose the encoded VVC video")
        self.output_edit.setPlaceholderText("Choose where to write the AFGS1 MKV")

        files = QGroupBox("Step 1 · Videos and output")
        file_grid = QGridLayout(files)
        file_grid.addWidget(QLabel("Source"), 0, 0)
        file_grid.addWidget(self.source_edit, 0, 1)
        file_grid.addWidget(self._browse_button(lambda: self._choose_video(self.source_edit, "source")), 0, 2)
        file_grid.addWidget(QLabel("Encoded VVC"), 1, 0)
        file_grid.addWidget(self.encoded_edit, 1, 1)
        file_grid.addWidget(self._browse_button(lambda: self._choose_video(self.encoded_edit, "encoded")), 1, 2)
        file_grid.addWidget(QLabel("Output MKV"), 2, 0)
        file_grid.addWidget(self.output_edit, 2, 1)
        file_grid.addWidget(self._browse_button(self._choose_output), 2, 2)
        layout.addWidget(files)

        self.source_info_label = QLabel("Source properties will appear here")
        self.encoded_info_label = QLabel("Encoded properties will appear here")
        self.source_info_label.setObjectName("info")
        self.encoded_info_label.setObjectName("info")
        self.source_info_label.setWordWrap(True)
        self.encoded_info_label.setWordWrap(True)
        info_row = QHBoxLayout()
        info_row.addWidget(self.source_info_label, 1)
        info_row.addWidget(self.encoded_info_label, 1)
        layout.addLayout(info_row)
        self.source_edit.textChanged.connect(self._on_video_path_changed)
        self.encoded_edit.textChanged.connect(self._on_video_path_changed)
        self.source_edit.editingFinished.connect(self._refresh_video_info)
        self.encoded_edit.editingFinished.connect(self._refresh_video_info)

        controls_row = QHBoxLayout()
        controls_row.setSpacing(12)
        alignment = QGroupBox("Step 2 · Align and analyze")
        alignment.setObjectName("alignmentControls")
        align_form = QFormLayout(alignment)
        align_form.setVerticalSpacing(5)
        self.offset_spin = QSpinBox()
        self.offset_spin.setRange(-100000, 100000)
        self.offset_spin.setValue(0)
        self.offset_spin.setToolTip("Positive means the encoded video starts later than the source.")
        offset_row = QHBoxLayout()
        offset_row.addWidget(self.offset_spin, 1)
        self.auto_align_button = QPushButton("Auto align")
        self.auto_align_button.clicked.connect(self._auto_align)
        offset_row.addWidget(self.auto_align_button)
        align_form.addRow("Encoded frame offset", offset_row)
        self.frame_limit_spin = QSpinBox()
        self.frame_limit_spin.setRange(2, 2000)
        self.frame_limit_spin.setValue(120)
        self.frame_limit_spin.setSuffix(" sampled frames")
        align_form.addRow("Analysis limit", self.frame_limit_spin)
        self.analyze_button = QPushButton("Analyze grain")
        self.analyze_button.setObjectName("primary")
        self.analyze_button.clicked.connect(self._analyze)
        align_form.addRow("", self.analyze_button)
        controls_row.addWidget(alignment, 1)

        grain_box = QGroupBox("Step 3 · Tune grain")
        grain_box.setObjectName("grainControls")
        grain_form = QFormLayout(grain_box)
        grain_form.setVerticalSpacing(5)
        self.apply_check = QCheckBox("Apply grain")
        self.apply_check.setChecked(True)
        grain_form.addRow("Model", self.apply_check)
        strength_row = QHBoxLayout()
        self.strength_slider = QSlider(Qt.Orientation.Horizontal)
        self.strength_slider.setRange(0, 400)
        self.strength_slider.setValue(150)
        self.strength_value = QLabel("150%")
        self.strength_slider.valueChanged.connect(lambda v: self.strength_value.setText(f"{v}%"))
        strength_row.addWidget(self.strength_slider, 1)
        strength_row.addWidget(self.strength_value)
        grain_form.addRow("Overall strength", strength_row)
        self.seed_spin = QSpinBox()
        self.seed_spin.setRange(-65535, 65535)
        self.seed_spin.setValue(0)
        grain_form.addRow("Seed offset", self.seed_spin)
        self.scale_delta_spin = QSpinBox()
        self.scale_delta_spin.setRange(-3, 3)
        self.scale_delta_spin.setValue(0)
        grain_form.addRow("Grain scale shift", self.scale_delta_spin)
        self.overlap_check = QCheckBox("Blend grain block edges")
        self.overlap_check.setChecked(True)
        grain_form.addRow("Overlap", self.overlap_check)
        self.restricted_check = QCheckBox("Clip to limited video range")
        self.restricted_check.setChecked(True)
        grain_form.addRow("Range", self.restricted_check)
        controls_row.addWidget(grain_box, 1)
        layout.addLayout(controls_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        model_widget = QWidget()
        model_layout = QVBoxLayout(model_widget)
        model_layout.setContentsMargins(0, 0, 8, 0)
        model_layout.setSpacing(8)
        self.model_summary = QLabel("No grain model analyzed yet")
        self.model_summary.setObjectName("summary")
        model_layout.addWidget(self.model_summary)
        self.model_table = QTableWidget(0, 4)
        self.model_table.setHorizontalHeaderLabels(["Start (s)", "Y points", "Cb points", "Cr points"])
        self.model_table.horizontalHeader().setStretchLastSection(True)
        self.model_table.setMaximumHeight(150)
        model_layout.addWidget(self.model_table)

        self.save_table_button = QPushButton("Save AOM grain table…")
        self.save_table_button.setEnabled(False)
        self.save_table_button.clicked.connect(self._save_table)
        model_layout.addWidget(self.save_table_button)
        model_layout.addStretch(1)
        splitter.addWidget(model_widget)

        preview_widget = QWidget()
        preview_layout = QVBoxLayout(preview_widget)
        preview_layout.setContentsMargins(8, 0, 0, 0)
        preview_head = QHBoxLayout()
        preview_head.addWidget(QLabel("Compare frames"))
        preview_head.addStretch(1)
        self.preview_time = QDoubleSpinBox()
        self.preview_time.setRange(0, 999999)
        self.preview_time.setDecimals(4)
        self.preview_time.setValue(0)
        self.preview_time.setSingleStep(1 / 24)
        self.preview_time.setSuffix(" s")
        self.preview_time.setToolTip(
            "Preview timestamp in seconds. The arrows move by one video frame and changing "
            "the time renders that frame automatically."
        )
        self.preview_time.valueChanged.connect(self._on_preview_time_changed)
        preview_head.addWidget(QLabel("Time"))
        preview_head.addWidget(self.preview_time)
        self.preview_button = QPushButton("Compare")
        self.preview_button.clicked.connect(self._load_preview)
        self.preview_button.setToolTip("Load the source and encoded frames at this time.")
        preview_head.addWidget(self.preview_button)
        self.synth_preview_button = QPushButton("Render VFGS preview")
        self.synth_preview_button.clicked.connect(self._load_synth_preview)
        self.synth_preview_button.setToolTip("Render one frame with InterDigital's VFGS tool. The synthesized frame appears in the right preview pane.")
        preview_head.addWidget(self.synth_preview_button)
        preview_layout.addLayout(preview_head)

        images = QHBoxLayout()
        source_column = QVBoxLayout()
        encoded_column = QVBoxLayout()
        self.source_preview_caption = QLabel("Original source")
        self.encoded_preview_caption = QLabel("Encoded VVC")
        self.source_preview_caption.setObjectName("previewCaption")
        self.encoded_preview_caption.setObjectName("previewCaption")
        self.source_preview = QLabel("Source frame")
        self.encoded_preview = QLabel("Encoded frame")
        for label in (self.source_preview, self.encoded_preview):
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setMinimumSize(280, 230)
            label.setObjectName("preview")
            label.setScaledContents(False)
        source_column.addWidget(self.source_preview_caption)
        source_column.addWidget(self.source_preview, 1)
        encoded_column.addWidget(self.encoded_preview_caption)
        encoded_column.addWidget(self.encoded_preview, 1)
        images.addLayout(source_column, 1)
        images.addLayout(encoded_column, 1)
        preview_layout.addLayout(images, 1)

        self.preview_note = QLabel("This comparison shows the pre-synthesis source and encode. Grain appears after an AFGS1-capable decoder applies the SEI metadata.")
        self.preview_note.setWordWrap(True)
        self.preview_note.setObjectName("note")
        preview_layout.addWidget(self.preview_note)
        splitter.addWidget(preview_widget)
        splitter.setSizes([420, 900])
        layout.addWidget(splitter, 1)

        tools_page = QWidget()
        tools_layout = QVBoxLayout(tools_page)
        tools_layout.setContentsMargins(24, 24, 24, 24)
        tools_layout.setSpacing(14)
        tools_intro = QLabel(
            "Configure the command-line tools used by the workbench. Required tools are "
            "FFmpeg, FFprobe, and AOM noise_model; VFGS is optional and enables the "
            "synthesized-grain frame preview. Paths are remembered between launches."
        )
        tools_intro.setWordWrap(True)
        tools_intro.setObjectName("note")
        tools_layout.addWidget(tools_intro)

        tools = QGroupBox("External tools")
        tool_grid = QGridLayout(tools)
        for row, (label, field) in enumerate((
            ("FFmpeg", self.ffmpeg_edit),
            ("FFprobe", self.ffprobe_edit),
            ("AOM noise_model", self.noise_edit),
            ("VFGS (optional preview)", self.vfgs_edit),
        )):
            tool_grid.addWidget(QLabel(label), row, 0)
            tool_grid.addWidget(field, row, 1)
            button = self._browse_button(lambda f=field: self._choose_executable(f))
            tool_grid.addWidget(button, row, 2)
        tools_layout.addWidget(tools)
        tools_layout.addStretch(1)

        actions = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.status_label = QLabel("Choose both videos, then configure the External tools tab to begin.")
        actions.addWidget(self.status_label, 1)
        actions.addWidget(self.progress, 1)
        self.write_button = QPushButton("Step 4 · Write AFGS1 MKV")
        self.write_button.setObjectName("primary")
        self.write_button.setEnabled(False)
        self.write_button.clicked.connect(self._write_mkv)
        actions.addWidget(self.write_button)
        layout.addLayout(actions)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(64)
        self.log.setPlaceholderText("Analysis and muxing details will appear here.")
        layout.addWidget(self.log)
        tabs = QTabWidget()
        tabs.addTab(root, "Workbench")
        tabs.addTab(tools_page, "External tools")
        self.setCentralWidget(tabs)
        self.setStyleSheet("""
            QWidget { background: #121a22; color: #e6edf3; font-size: 10pt; }
            QGroupBox { border: 1px solid #354453; border-radius: 9px; margin-top: 8px; padding: 7px; font-weight: 600; }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; color: #a9d8cb; }
            QLineEdit, QSpinBox, QDoubleSpinBox, QTextEdit, QTableWidget { background: #0d1319; border: 1px solid #334451; border-radius: 5px; padding: 6px; }
            QPushButton { background: #243440; border: 1px solid #3b5362; border-radius: 6px; padding: 5px 10px; }
            QPushButton:hover { background: #304955; }
            QPushButton:disabled { color: #71808a; background: #1a242c; }
            QPushButton#primary { color: #071611; background: #75d1b0; border: 0; font-weight: 700; }
            QPushButton#primary:hover { background: #90e3c4; }
            QLabel#title { font-size: 22pt; font-weight: 700; color: #f2fbf7; }
            QLabel#subtitle { color: #a9bac5; }
            QLabel#workflow { color: #d0e6de; font-size: 9pt; padding: 2px 0; }
            QLabel#info { color: #a8c0cb; padding: 3px; }
            QLabel#summary { color: #80d6b5; padding: 5px 0; }
            QLabel#preview { background: #090e12; border: 1px solid #354653; border-radius: 8px; color: #91a3ad; }
            QLabel#previewCaption { color: #a9d8cb; font-weight: 700; padding: 2px 4px; }
            QLabel#note { color: #9aabb5; font-size: 9pt; }
            QProgressBar { border: 1px solid #354653; border-radius: 5px; text-align: center; height: 18px; }
            QProgressBar::chunk { background: #57bb97; border-radius: 4px; }
            QHeaderView::section { background: #1d2a33; padding: 5px; border: 0; }
        """)

    def _browse_button(self, callback):
        button = QPushButton("Browse…")
        button.clicked.connect(callback)
        return button

    def _load_settings(self):
        ffmpeg = shutil.which("ffmpeg") or ""
        ffprobe = shutil.which("ffprobe") or ""
        vfgs = shutil.which("vfgs") or shutil.which("vfgs.exe") or ""
        for key, field, default in (("ffmpeg", self.ffmpeg_edit, ffmpeg),
                                    ("ffprobe", self.ffprobe_edit, ffprobe),
                                    ("noise_model", self.noise_edit, ""),
                                    ("vfgs", self.vfgs_edit, vfgs)):
            field.setText(self.settings.value(key, default))
        self.source_edit.setText(self.settings.value("source", ""))
        self.encoded_edit.setText(self.settings.value("encoded", ""))
        self.output_edit.setText(self.settings.value("output", ""))
        if self.source_edit.text() and self.encoded_edit.text():
            self._refresh_video_info()

    def _save_settings(self):
        for key, field in (("ffmpeg", self.ffmpeg_edit), ("ffprobe", self.ffprobe_edit),
                           ("noise_model", self.noise_edit), ("vfgs", self.vfgs_edit),
                           ("source", self.source_edit),
                           ("encoded", self.encoded_edit), ("output", self.output_edit)):
            self.settings.setValue(key, field.text())

    def _choose_video(self, field: QLineEdit, kind: str):
        path = _browse_file(self, "Choose video", field.text(),
                            "Video files (*.mkv *.mp4 *.mov *.avi *.ts *.m2ts *.265 *.266 *.vvc);;All files (*.*)")
        if not path:
            return
        field.setText(path)
        if kind == "encoded" and not self.output_edit.text():
            target = Path(path)
            self.output_edit.setText(str(target.with_name(target.stem + "_AFGS1.mkv")))
        self._refresh_video_info()
        self._save_settings()

    def _choose_output(self):
        start = self.output_edit.text() or self.encoded_edit.text()
        path, _ = QFileDialog.getSaveFileName(self, "Write AFGS1 MKV", start, "Matroska video (*.mkv)")
        if path:
            if not path.lower().endswith(".mkv"):
                path += ".mkv"
            self.output_edit.setText(path)
            self._save_settings()

    def _choose_executable(self, field: QLineEdit):
        path = _browse_file(self, "Choose executable", field.text(), "Programs (*.exe);;All files (*.*)")
        if path:
            field.setText(path)
            self._save_settings()

    def _refresh_video_info(self):
        ffprobe = self.ffprobe_edit.text().strip()
        source_path = self.source_edit.text().strip()
        encoded_path = self.encoded_edit.text().strip()
        if not source_path or not encoded_path:
            self.source_info = self.encoded_info = None
            self.source_info_label.setText("Choose the original source video")
            self.encoded_info_label.setText("Choose the encoded VVC video")
            self.status_label.setText("Choose both video files to load their properties.")
            return False
        if not ffprobe or not Path(ffprobe).is_file():
            self.source_info = self.encoded_info = None
            self.source_info_label.setText("Source properties unavailable")
            self.encoded_info_label.setText("Encoded properties unavailable")
            self.status_label.setText("Choose a valid FFprobe executable to inspect the videos.")
            return False
        try:
            self.source_info = probe_video(source_path, ffprobe)
            self.encoded_info = probe_video(encoded_path, ffprobe)
            self.preview_time.setSingleStep(1 / float(self.encoded_info.fps))
            self.source_info_label.setText(self._describe(self.source_info, "SOURCE"))
            self.encoded_info_label.setText(self._describe(self.encoded_info, "ENCODED"))
            self.status_label.setText("Videos loaded. Set alignment, then analyze the grain.")
            return True
        except Exception as exc:
            self.source_info = self.encoded_info = None
            self.source_info_label.setText("Source properties unavailable")
            self.encoded_info_label.setText("Encoded properties unavailable")
            self.status_label.setText("Video probe failed")
            self._log(str(exc))
            return False

    def _on_video_path_changed(self, _text: str = ""):
        self.source_info = self.encoded_info = None
        self.entries.clear()
        self.table_text = ""
        self.model_summary.setText("Video paths changed. Analyze grain again.")
        self.model_table.setRowCount(0)
        self.save_table_button.setEnabled(False)
        self.write_button.setEnabled(False)
        self.preview_pngs = None
        self.preview_mode = None
        self.preview_update_timer.stop()
        self.status_label.setText("Video paths changed. Load the videos and analyze grain again.")
        for label, caption in ((self.source_preview, "Source frame"),
                               (self.encoded_preview, "Encoded frame")):
            label.clear()
            label.setText(caption)
        self.source_preview_caption.setText("Original source")
        self.encoded_preview_caption.setText("Encoded VVC")

    @staticmethod
    def _describe(info: VideoInfo, label: str) -> str:
        duration = f"{info.duration:.1f}s" if info.duration > 0 else "duration unknown"
        return (f"{label}: {info.width}×{info.height} · {info.fps_text} fps · "
                f"{info.pixel_format} · {info.codec} · {duration}")

    def _tools(self):
        ffmpeg = self.ffmpeg_edit.text().strip()
        ffprobe = self.ffprobe_edit.text().strip()
        noise = self.noise_edit.text().strip()
        missing = [name for name, path in (("FFmpeg", ffmpeg), ("FFprobe", ffprobe),
                                            ("AOM noise_model", noise)) if not Path(path).is_file()]
        if missing:
            raise RuntimeError("Missing tool path(s): " + ", ".join(missing))
        return ffmpeg, ffprobe, noise

    def _auto_align(self):
        self._refresh_video_info()
        if not self.source_info or not self.encoded_info:
            return
        ffmpeg = self.ffmpeg_edit.text().strip()
        if not Path(ffmpeg).is_file():
            self._error("Choose a valid ffmpeg executable first.")
            return
        source, encoded = self.source_info, self.encoded_info
        self._start_job("Estimating frame alignment…", lambda progress:
                        estimate_offset_frames(ffmpeg, source, encoded, scratch_dir=WORK_DIR),
                        self._on_alignment)

    def _on_alignment(self, offset: int):
        self.offset_spin.setValue(offset)
        fps = float(self.encoded_info.fps) if self.encoded_info else 0
        seconds = offset / fps if fps else 0
        self._log(f"Automatic alignment estimate: {offset:+d} encoded frame(s) ({seconds:+.3f} s). Refine manually if needed.")
        self.status_label.setText("Alignment estimate ready. Review the comparison before analysis.")

    def _analyze(self):
        self._refresh_video_info()
        if not self.source_info or not self.encoded_info:
            return
        try:
            ffmpeg, ffprobe, noise = self._tools()
        except Exception as exc:
            self._error(str(exc))
            return
        source, encoded = self.source_info, self.encoded_info
        offset = self.offset_spin.value()
        source_offset = round(max(0, offset) * float(source.fps / encoded.fps))
        encoded_offset = max(0, -offset)
        max_frames = self.frame_limit_spin.value()

        def task(progress):
            with tempfile.TemporaryDirectory(prefix="afgs1-analysis-", dir=WORK_DIR) as folder:
                folder = Path(folder)
                table = folder / "film_grain.tbl"
                n, rate = run_aom_analysis(
                    ffmpeg, noise, source, encoded, source_offset, encoded_offset,
                    max_frames, folder / "source.yuv", folder / "encoded.yuv", table,
                    progress=progress)
                entries = parse_aom_table(table)
                return {"entries": entries, "table": table.read_text(encoding="ascii"),
                        "frames": n, "rate": rate}

        self._start_job("Starting grain analysis…", task, self._on_analysis,
                        indeterminate=True)

    def _on_analysis(self, result):
        self.entries = result["entries"]
        self.table_text = result["table"]
        # Analysis creates the model needed for a grain preview. Reset a prior
        # comparison mode so the next time change prefers VFGS when available.
        self.preview_mode = None
        if not self.output_edit.text().strip() and self.encoded_edit.text().strip():
            encoded_path = Path(self.encoded_edit.text().strip())
            self.output_edit.setText(str(encoded_path.with_name(encoded_path.stem + "_AFGS1.mkv")))
        self.model_summary.setText(preview_summary(self.entries))
        self.model_table.setRowCount(0)
        for entry in self.entries[:100]:
            row = self.model_table.rowCount()
            self.model_table.insertRow(row)
            self.model_table.setItem(row, 0, QTableWidgetItem(f"{entry.start_time / 10_000_000:.3f}"))
            self.model_table.setItem(row, 1, QTableWidgetItem(self._fmt_points(entry.params.y_points)))
            self.model_table.setItem(row, 2, QTableWidgetItem(self._fmt_points(entry.params.cb_points)))
            self.model_table.setItem(row, 3, QTableWidgetItem(self._fmt_points(entry.params.cr_points)))
        if len(self.entries) > 100:
            self._log(f"Showing first 100 of {len(self.entries)} grain-model time segments.")
        self.save_table_button.setEnabled(True)
        self.write_button.setEnabled(True)
        self._log(f"Analyzed {result['frames']} matched frames at {result['rate']} fps; {preview_summary(self.entries)}.")
        self.status_label.setText(
            "Analysis complete. Adjust strength, optionally preview, then choose/check the output MKV and write it.")

    @staticmethod
    def _fmt_points(points):
        return "  ".join(f"{x}:{y}" for x, y in points) or "—"

    def _save_table(self):
        if not self.table_text:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save AOM film-grain table", "film_grain.tbl",
                                               "AOM grain table (*.tbl);;All files (*.*)")
        if path:
            Path(path).write_text(self.table_text, encoding="ascii")
            self._log(f"Saved analyzer table to {path}")

    def _load_preview(self):
        if not self.source_edit.text() or not self.encoded_edit.text():
            self._error("Choose both videos before loading a comparison.")
            return
        if not self.source_info or not self.encoded_info:
            self._refresh_video_info()
        if not self.source_info or not self.encoded_info:
            return
        ffmpeg = self.ffmpeg_edit.text().strip()
        if not Path(ffmpeg).is_file():
            self._error("Choose a valid ffmpeg executable first.")
            return
        timestamp = self.preview_time.value()
        fps = float(self.encoded_info.fps) if self.encoded_info else 0
        offset = self.offset_spin.value()
        source_time = timestamp + (max(0, offset) / fps if fps else 0)
        encoded_time = timestamp + (max(0, -offset) / fps if fps else 0)
        source_path, encoded_path = self.source_edit.text(), self.encoded_edit.text()
        self.preview_mode = "comparison"

        def task(progress):
            return (extract_preview_png(ffmpeg, source_path, source_time),
                    extract_preview_png(ffmpeg, encoded_path, encoded_time))

        self._start_job("Loading comparison frames…", task, self._on_preview)

    def _load_synth_preview(self):
        if not self.entries or not self.encoded_info:
            self._error("Analyze the grain model before rendering a synthesis preview.")
            return
        ffmpeg = self.ffmpeg_edit.text().strip()
        vfgs = self.vfgs_edit.text().strip()
        if not Path(ffmpeg).is_file() or not Path(vfgs).is_file():
            self._error("Choose valid FFmpeg and VFGS executables. VFGS is optional and is used only for this preview.")
            return
        timestamp = self.preview_time.value()
        info = self.encoded_info
        model_time = max(0, timestamp - max(0, -self.offset_spin.value()) / float(self.encoded_info.fps))
        params = deepcopy(parameter_at(self.entries, round(model_time * 10_000_000)))
        if not self.apply_check.isChecked():
            params.apply_grain = 0
        strength = self.strength_slider.value()
        frame_seed = params.random_seed + self.seed_spin.value() + round(timestamp * float(info.fps))
        scale_delta = self.scale_delta_spin.value()
        overlap = self.overlap_check.isChecked()
        source_path = self.source_edit.text()
        offset = self.offset_spin.value()
        source_time = timestamp + (max(0, offset) / float(info.fps))
        self.preview_mode = "synth"

        def task(progress):
            progress(0, f"Extracting the original frame at {source_time:.2f} s…")
            source_png = extract_preview_png(ffmpeg, source_path, source_time)
            progress(0, f"Preparing VFGS preview at {timestamp:.2f} s…")
            grain_png = render_vfgs_preview(ffmpeg, vfgs, info, timestamp, params,
                                            strength, frame_seed, scale_delta, overlap, WORK_DIR,
                                            progress=progress)
            return source_png, grain_png

        self._start_job("Rendering the selected frame with VFGS…", task,
                        lambda images: self._on_synth_preview(images, timestamp, strength),
                        indeterminate=True)

    def _on_preview_time_changed(self, value: float):
        if self.preview_mode is None:
            self.preview_mode = self._default_preview_mode()
        if self.preview_mode in ("comparison", "synth"):
            self.status_label.setText(f"Updating preview at {value:.4f} s…")
            self.preview_update_timer.start()
        else:
            self.status_label.setText(
                "Choose both videos and configure FFmpeg to load a frame preview."
            )

    def _default_preview_mode(self) -> str | None:
        ffmpeg = self.ffmpeg_edit.text().strip()
        vfgs = self.vfgs_edit.text().strip()
        if self.entries and self.encoded_info and Path(ffmpeg).is_file() and Path(vfgs).is_file():
            return "synth"
        if self.source_info and self.encoded_info and Path(ffmpeg).is_file():
            return "comparison"
        return None

    def _refresh_preview_at_selected_time(self):
        if self.job and self.job.isRunning():
            return
        if self.preview_mode == "synth":
            self._load_synth_preview()
        elif self.preview_mode == "comparison":
            self._load_preview()

    def _on_preview(self, images: tuple[bytes, bytes]):
        self.preview_pngs = images
        self._show_image(self.source_preview, images[0])
        self._show_image(self.encoded_preview, images[1])
        self.source_preview.setToolTip("Original grainy source")
        self.encoded_preview.setToolTip("Encoded VVC reconstruction")
        self.source_preview_caption.setText("Original source")
        self.encoded_preview_caption.setText("Encoded VVC")
        self.preview_note.setText("This comparison shows the pre-synthesis source and encode. Grain appears after an AFGS1-capable decoder applies the SEI metadata.")
        self.status_label.setText("Comparison loaded.")

    def _on_synth_preview(self, images: tuple[bytes, bytes], timestamp: float, strength: int):
        self.preview_pngs = images
        self._show_image(self.source_preview, images[0])
        self._show_image(self.encoded_preview, images[1])
        self.source_preview.setToolTip("Original grainy source")
        self.encoded_preview.setToolTip("VFGS synthesized-grain preview (software model)")
        self.source_preview_caption.setText("Original source")
        self.encoded_preview_caption.setText(f"VFGS synthesized grain · {timestamp:.2f} s · {strength}%")
        self.preview_note.setText("The right frame shows VFGS synthesized grain at the selected timestamp and strength. It is a visual guide; decoder implementations can differ slightly.")
        self.status_label.setText("VFGS preview is displayed in the right-hand frame.")
        self._log(f"Rendered VFGS grain preview at {timestamp:.2f} s with {strength}% strength.")

    @staticmethod
    def _show_image(label: QLabel, data: bytes):
        pix = QPixmap()
        if pix.loadFromData(data, "PNG"):
            label.setPixmap(pix.scaled(label.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                       Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.preview_pngs:
            self._show_image(self.source_preview, self.preview_pngs[0])
            self._show_image(self.encoded_preview, self.preview_pngs[1])

    def _write_mkv(self):
        if not self.entries or not self.encoded_info:
            self._error("Analyze the grain model before writing the MKV.")
            return
        output = self.output_edit.text().strip()
        if not output:
            self._error("Choose an output MKV path.")
            return
        if Path(output).resolve() == Path(self.encoded_edit.text()).resolve():
            self._error("The output path must differ from the encoded input.")
            return
        params = deepcopy(self.entries)
        if not self.apply_check.isChecked():
            for entry in params:
                entry.params.apply_grain = 0
        info = self.encoded_info
        settings = (self.strength_slider.value() / 100.0, self.seed_spin.value(),
                    self.scale_delta_spin.value(), self.overlap_check.isChecked(),
                    self.restricted_check.isChecked())
        analysis_prefix_seconds = max(0, -self.offset_spin.value()) / float(info.fps)
        input_path = self.encoded_edit.text()

        def task(progress):
            return inject_afgs1(input_path, output, params, info.width, info.height,
                                info.bit_depth, info.subsampling_x, info.subsampling_y,
                                *settings, analysis_prefix_seconds=analysis_prefix_seconds,
                                progress=progress)

        self._start_job("Writing AFGS1 metadata into the VVC stream…", task,
                        lambda count: self._on_written(count, output))

    def _on_written(self, count: int, output: str):
        self._log(f"Wrote AFGS1 registered T.35 metadata on {count} VVC picture packet(s): {output}")
        self.status_label.setText(
            "AFGS1 metadata written. Playback grain appears in players/decoders that support AFGS1.")
        self.progress.setValue(100)

    def _start_job(self, status: str, callback, on_success, *, indeterminate: bool = False):
        if self.job and self.job.isRunning():
            return
        self._set_busy(True)
        self.progress.setRange(0, 0) if indeterminate else self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.status_label.setText(status)
        self.job = JobThread(callback)
        self.job.progress.connect(self._on_progress)
        self.job.succeeded.connect(lambda result: self._finish_job(on_success, result))
        self.job.failed.connect(self._on_job_error)
        self.job.start()

    def _finish_job(self, callback, result):
        self._set_busy(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        callback(result)

    def _on_progress(self, percent: int, message: str):
        if self.progress.maximum() > self.progress.minimum():
            self.progress.setValue(percent)
        self.status_label.setText(message)

    def _on_job_error(self, message: str):
        self._set_busy(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.status_label.setText("Operation failed")
        self._error(message)

    def _set_busy(self, busy: bool):
        self.preview_time.setEnabled(not busy)
        for widget in (self.analyze_button, self.auto_align_button, self.write_button,
                       self.preview_button, self.synth_preview_button, self.save_table_button):
            if busy and widget.isEnabled():
                widget.setEnabled(False)
            elif not busy:
                if widget is self.write_button:
                    widget.setEnabled(bool(self.entries))
                elif widget is self.save_table_button:
                    widget.setEnabled(bool(self.table_text))
                else:
                    widget.setEnabled(True)

    def _log(self, message: str):
        self.log.append(message)

    def _error(self, message: str):
        self._log(message)
        QMessageBox.critical(self, "AFGS1 VVC Workbench", message)

    def closeEvent(self, event):
        self._save_settings()
        if self.job and self.job.isRunning():
            QMessageBox.information(self, "Work is still running",
                                    "Wait for the current operation to finish before closing the workbench.")
            event.ignore()
            return
        event.accept()


def main():
    app = QApplication([])
    app.setApplicationName("AFGS1 VVC Workbench")
    window = MainWindow()
    window.show()
    app.exec()


if __name__ == "__main__":
    main()
