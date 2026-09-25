"""Strip chart of grain strength over the whole video; click to jump there."""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget


class GrainTimeline(QWidget):
    frame_clicked = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(64)
        self.setMaximumHeight(90)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.frames = 1
        self.starts = np.zeros(0)
        self.sigmas = np.zeros(0)
        self.current = 0
        self.fps = 24.0
        self.setToolTip("Grain strength (σ at mid-grey, 8-bit code values) over the whole video, "
                        "after the strength setting. Click or drag to jump to that frame.")

    def set_data(self, frames: int, starts: np.ndarray, sigmas: np.ndarray, fps: float) -> None:
        self.frames, self.starts, self.sigmas, self.fps = max(1, frames), starts, sigmas, fps
        self.update()

    def set_current(self, index: int) -> None:
        self.current = index
        self.update()

    def _column_values(self, columns: int) -> np.ndarray:
        """Mean sigma of the frames each pixel column covers (segment-weighted)."""
        if not len(self.starts):
            return np.zeros(columns)
        edges = np.linspace(0, self.frames, columns + 1)
        idx = np.clip(np.searchsorted(self.starts, edges[:-1], side="right") - 1, 0, len(self.sigmas) - 1)
        return self.sigmas[idx]

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(24, 27, 31))
        area = QRectF(34, 6, self.width() - 40, self.height() - 12)
        values = self._column_values(max(2, int(area.width())))
        top = max(0.5, float(np.percentile(values, 99.5)) * 1.15) if len(values) else 1.0
        p.setPen(QColor(140, 148, 156))
        small = p.font()
        small.setPointSizeF(max(7.0, small.pointSizeF() - 1.5))
        p.setFont(small)
        p.drawText(QRectF(0, area.top() - 4, 30, 14), Qt.AlignmentFlag.AlignRight, f"{top:.1f}")
        p.drawText(QRectF(0, area.bottom() - 10, 30, 14), Qt.AlignmentFlag.AlignRight, "0")
        path = QPainterPath(QPointF(area.left(), area.bottom()))
        for i, v in enumerate(values):
            x = area.left() + area.width() * i / (len(values) - 1)
            path.lineTo(QPointF(x, area.bottom() - area.height() * min(float(v), top) / top))
        path.lineTo(QPointF(area.right(), area.bottom()))
        p.fillPath(path, QColor(159, 211, 199, 90))
        p.setPen(QPen(QColor(159, 211, 199), 1))
        p.drawPath(path)
        x = area.left() + area.width() * self.current / max(1, self.frames - 1)
        p.setPen(QPen(QColor(255, 200, 90), 2))
        p.drawLine(QPointF(x, area.top()), QPointF(x, area.bottom()))

    def _emit_at(self, x: float) -> None:
        area_left, area_width = 34, max(1, self.width() - 40)
        fraction = min(1.0, max(0.0, (x - area_left) / area_width))
        self.frame_clicked.emit(int(round(fraction * (self.frames - 1))))

    def mousePressEvent(self, event) -> None:
        self._emit_at(event.position().x())

    def mouseMoveEvent(self, event) -> None:
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._emit_at(event.position().x())
