"""The parts both tabs share: the frame pane (view buttons, frame, navigation), its keys and the status bar."""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage, QKeySequence, QShortcut
from PySide6.QtWidgets import (QButtonGroup, QHBoxLayout, QLabel, QMainWindow, QProgressBar, QPushButton,
                               QSizePolicy, QSlider, QSpinBox, QVBoxLayout, QWidget)

from .viewer import ENCODED, GRAIN, GRAIN_ONLY, SOURCE, FrameView, PreviewState

MODE_KEYS = ((SOURCE, "1"), (ENCODED, "2"), (GRAIN, "3"), (GRAIN_ONLY, "4"))
SHORTCUT_HELP = ("← → {left} / {right} · 1–4 choose view · , . previous / next frame · PgUp PgDn ±1 s · "
                 "wheel or + − zoom · 0 = 100 % · F fit · drag to pan")


def to_qimage(rgb: np.ndarray) -> QImage:
    """An (H, W, 3) uint8 array as a QImage that owns its pixels."""
    rgb = np.ascontiguousarray(rgb)
    h, w, _ = rgb.shape
    return QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()


def timecode(seconds: float) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:06.3f}" if h else f"{int(m)}:{s:06.3f}"


def install_viewer_shortcuts(widget: QWidget, view: FrameView, set_mode: Callable[[int], None],
                             step: Callable[[int], None], second: Callable[[], int]) -> list[QShortcut]:
    """The viewer's keys on ``widget`` (a tab or a stand-alone frame window).

    ← / → flip between the picture without and with grain, 1-4 choose a view,
    , and . step one frame, PgUp / PgDn one second (``second()`` frames).
    """
    shortcuts = []

    def add(key, fn) -> None:
        shortcuts.append(QShortcut(QKeySequence(key), widget, fn))
    add(Qt.Key.Key_Left, lambda: set_mode(ENCODED))
    add(Qt.Key.Key_Right, lambda: set_mode(GRAIN))
    for mode, key in MODE_KEYS:
        add(key, lambda m=mode: set_mode(m))
    add(",", lambda: step(-1))
    add(".", lambda: step(1))
    add(Qt.Key.Key_PageUp, lambda: step(-second()))
    add(Qt.Key.Key_PageDown, lambda: step(second()))
    add("+", lambda: view.step_zoom(1))
    add("=", lambda: view.step_zoom(1))
    add("-", lambda: view.step_zoom(-1))
    add("0", lambda: view.set_zoom(1.0))
    add("F", view.fit)
    return shortcuts


class FramePane(QWidget):
    """View buttons and zoom, the frame, an optional strip under it, and frame navigation.

    The pane only reports what was asked for (``frame_requested``,
    ``step_requested``, ``mode_requested``, ``popout_requested``); the tab
    decides what happens and calls :meth:`show_frame` / :meth:`sync_modes`.
    """
    frame_requested = Signal(int)
    step_requested = Signal(int)
    mode_requested = Signal(int)
    popout_requested = Signal()

    def __init__(self, state: PreviewState, tips: dict[int, str] | None = None,
                 below_view: QWidget | None = None, hint: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 8, 8)

        bar = QHBoxLayout()
        self.mode_group = QButtonGroup(self)
        self.mode_buttons: dict[int, QPushButton] = {}
        for mode, key in MODE_KEYS:
            button = QPushButton(f"{state.names[mode]}  [{key}]", checkable=True)
            if tips:
                button.setToolTip(tips[mode])
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(lambda _=False, m=mode: self.mode_requested.emit(m))
            self.mode_group.addButton(button)
            self.mode_buttons[mode] = button
            bar.addWidget(button)
        bar.addStretch(1)
        self.view = FrameView(state)
        for text, action in (("Fit", self.view.fit), ("100%", lambda: self.view.set_zoom(1)),
                             ("200%", lambda: self.view.set_zoom(2)), ("400%", lambda: self.view.set_zoom(4))):
            bar.addWidget(self._flat_button(text, action))
        popout = self._flat_button("Open in window", self.popout_requested.emit)
        popout.setToolTip("Show the frame on its own in a separate, resizable window.")
        bar.addWidget(popout)
        layout.addLayout(bar)
        self.view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self.view, 1)
        if below_view is not None:
            layout.addWidget(below_view)

        nav = QHBoxLayout()
        previous, following = QPushButton("◀"), QPushButton("▶")
        for button, delta in ((previous, -1), (following, 1)):
            button.setFixedWidth(34)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(lambda _=False, d=delta: self.step_requested.emit(d))
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.slider.valueChanged.connect(self.frame_requested.emit)
        self.frame_spin = QSpinBox(minimum=0, maximum=0)
        self.frame_spin.setKeyboardTracking(False)
        self.frame_spin.valueChanged.connect(self.frame_requested.emit)
        self.time_label = QLabel("0:00.000")
        self.time_label.setMinimumWidth(90)
        nav.addWidget(previous)
        nav.addWidget(self.slider, 1)
        nav.addWidget(following)
        nav.addWidget(QLabel("Frame"))
        nav.addWidget(self.frame_spin)
        nav.addWidget(self.time_label)
        layout.addLayout(nav)
        if hint:
            label = QLabel(hint)
            label.setObjectName("info")
            layout.addWidget(label)

    @staticmethod
    def _flat_button(text: str, action) -> QPushButton:
        button = QPushButton(text)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.clicked.connect(action)
        return button

    def set_frame_count(self, frames: int) -> None:
        for widget in (self.slider, self.frame_spin):
            widget.blockSignals(True)
            widget.setMaximum(max(0, frames - 1))
            widget.blockSignals(False)

    def show_frame(self, index: int, seconds: float) -> None:
        """Move the slider and the frame box to ``index`` without asking for it again."""
        for widget in (self.slider, self.frame_spin):
            widget.blockSignals(True)
            widget.setValue(index)
            widget.blockSignals(False)
        self.time_label.setText(timecode(seconds))

    def sync_modes(self, available: set[int]) -> None:
        """Enable the view buttons that can be shown; check the one that is."""
        for mode, button in self.mode_buttons.items():
            button.setEnabled(mode in available)
            button.setChecked(mode == self.state.mode and bool(self.state.images))


class StatusBar:
    """A tab's status line with a progress bar and a Cancel button that show while a job runs."""

    def __init__(self, window: QMainWindow, on_cancel: Callable[[], None], progress_width: int = 320) -> None:
        self.label = QLabel("Ready.")
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(progress_width)
        self.progress.setTextVisible(True)
        self.progress.hide()
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.hide()
        self.cancel_button.clicked.connect(on_cancel)
        bar = window.statusBar()
        bar.addWidget(self.label, 1)
        bar.addPermanentWidget(self.progress)
        bar.addPermanentWidget(self.cancel_button)

    def busy(self, fraction: float | None = None) -> None:
        """Show the progress (None or a negative fraction: busy without a percentage) and Cancel."""
        if fraction is None or fraction < 0:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 1000)
            self.progress.setValue(int(fraction * 1000))
        self.progress.show()
        self.cancel_button.show()
        self.cancel_button.setEnabled(True)

    def idle(self) -> None:
        self.progress.hide()
        self.cancel_button.hide()
