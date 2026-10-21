"""Frame viewer: the embedded view and the stand-alone window share one state."""

from __future__ import annotations

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (QColor, QGuiApplication, QImage, QKeySequence, QPainter, QPixmap,
                           QShortcut, QTransform)
from PySide6.QtWidgets import QGraphicsPixmapItem, QGraphicsScene, QGraphicsView, QLabel, QVBoxLayout, QWidget

SOURCE, ENCODED, GRAIN, GRAIN_ONLY = range(4)
VIEW_NAMES = {SOURCE: "Source", ENCODED: "Encoded", GRAIN: "Encoded + grain",
              GRAIN_ONLY: "Grain only (×4)"}
ZOOM_STEPS = [0.125, 0.25, 1 / 3, 0.5, 2 / 3, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0, 16.0]


class PreviewState(QObject):
    """Images for the current frame and which one is shown."""
    changed = Signal()

    def __init__(self, names: dict[int, str] | None = None) -> None:
        super().__init__()
        self.names = names or VIEW_NAMES       # what each view is called in captions
        self.images: dict[int, QPixmap] = {}
        self.mode = ENCODED
        self.caption = ""
        self.placeholder = "Choose a source and an encoded video to see frames."

    def set_images(self, images: dict[int, QImage], caption: str) -> None:
        self.images = {k: QPixmap.fromImage(v) for k, v in images.items()}
        self.caption = caption
        if self.mode not in self.images:
            self.mode = ENCODED if ENCODED in self.images else next(iter(self.images), ENCODED)
        self.changed.emit()

    def update_images(self, images: dict[int, QImage]) -> None:
        for key, image in images.items():
            if image is None:
                self.images.pop(key, None)
            else:
                self.images[key] = QPixmap.fromImage(image)
        self.changed.emit()

    def clear(self, placeholder: str) -> None:
        self.images = {}
        self.placeholder = placeholder
        self.changed.emit()

    def set_mode(self, mode: int) -> bool:
        if mode not in self.images:
            return False
        self.mode = mode
        self.changed.emit()
        return True

    @property
    def pixmap(self) -> QPixmap | None:
        return self.images.get(self.mode)


class FrameView(QGraphicsView):
    """Zoomable, pannable view; nearest-neighbour from 100 % up so grain stays crisp."""
    zoom_changed = Signal(float)

    def __init__(self, state: PreviewState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self.setScene(QGraphicsScene(self))
        self.item = QGraphicsPixmapItem()
        self.item.setTransformationMode(Qt.TransformationMode.FastTransformation)
        self.scene().addItem(self.item)
        self.setBackgroundBrush(QColor(12, 14, 16))
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._zoom = 1.0
        self._fit = True
        self._size = None
        self.overlay = QLabel(self)
        self.overlay.setStyleSheet("background: rgba(0,0,0,150); color: #f2f2f2; padding: 4px 8px;"
                                   "border-radius: 4px; font-size: 10pt;")
        self.overlay.move(10, 10)
        self.message = QLabel(self)
        self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message.setWordWrap(True)
        self.message.setStyleSheet("color: #9aa4ad; font-size: 11pt;")
        state.changed.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        pixmap = self.state.pixmap
        if pixmap is None:
            self.item.setPixmap(QPixmap())
            self.overlay.hide()
            self.message.setText(self.state.placeholder)
            self.message.show()
            self._layout_message()
            return
        self.message.hide()
        self.item.setPixmap(pixmap)
        size = (pixmap.width(), pixmap.height())
        self.scene().setSceneRect(QRectF(0, 0, *size))
        if size != self._size:
            self._size = size
            if self._fit:
                self.fit()
        self._update_overlay()

    def _update_overlay(self) -> None:
        zoom = "fit" if self._fit else f"{self._zoom * 100:.0f}%"
        self.overlay.setText(f"<b>{self.state.names[self.state.mode]}</b> &nbsp; {self.state.caption}"
                             f" &nbsp; {zoom}")
        self.overlay.adjustSize()
        self.overlay.show()

    def _apply(self, zoom: float, anchor: QPointF | None = None) -> None:
        self._zoom = zoom
        self.item.setTransformationMode(Qt.TransformationMode.SmoothTransformation if zoom < 1
                                        else Qt.TransformationMode.FastTransformation)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, zoom < 1)
        self.setTransform(QTransform.fromScale(zoom, zoom))
        self._update_overlay()
        self.zoom_changed.emit(zoom)

    def fit(self) -> None:
        self._fit = True
        if self.item.pixmap().isNull():
            return
        rect = self.viewport().rect()
        w, h = self.item.pixmap().width(), self.item.pixmap().height()
        self._apply(min(rect.width() / w, rect.height() / h))
        self._fit = True
        self.centerOn(self.item)
        self._update_overlay()

    def set_zoom(self, zoom: float) -> None:
        self._fit = False
        self._apply(max(ZOOM_STEPS[0], min(ZOOM_STEPS[-1], zoom)))

    def step_zoom(self, direction: int) -> None:
        current = self._zoom
        if direction > 0:
            target = next((z for z in ZOOM_STEPS if z > current * 1.001), ZOOM_STEPS[-1])
        else:
            target = next((z for z in reversed(ZOOM_STEPS) if z < current / 1.001), ZOOM_STEPS[0])
        self.set_zoom(target)

    def wheelEvent(self, event) -> None:
        self.step_zoom(1 if event.angleDelta().y() > 0 else -1)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._fit:
            self.fit()
        self._layout_message()

    def _layout_message(self) -> None:
        self.message.setGeometry(20, 0, max(10, self.width() - 40), self.height())

    def mouseDoubleClickEvent(self, event) -> None:
        if self._fit:
            self.set_zoom(1.0)
        else:
            self.fit()


class ViewerWindow(QWidget):
    """A plain window holding only the frame."""

    def __init__(self, state: PreviewState, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("Frame viewer")
        self.view = FrameView(state, self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)
        state.changed.connect(self._title)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, self.close)

    def _title(self) -> None:
        state = self.view.state
        self.setWindowTitle(f"{state.names[state.mode]} — {state.caption}" if state.pixmap else "Frame viewer")

    def show_sized(self) -> None:
        """Open at 100 % if the frame fits on screen, otherwise as large as sensible."""
        screen = QGuiApplication.primaryScreen().availableGeometry()
        pixmap = self.view.state.pixmap
        if not self.isVisible():
            w, h = (pixmap.width(), pixmap.height()) if pixmap else (1280, 720)
            self.resize(min(w, int(screen.width() * 0.9)), min(h, int(screen.height() * 0.9)))
        self.show()
        self.raise_()
        self.activateWindow()
        if pixmap and pixmap.width() <= self.view.viewport().width() + 2:
            self.view.set_zoom(1.0)
        else:
            self.view.fit()
