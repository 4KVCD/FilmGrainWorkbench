"""Plot of grain strength (noise sigma) against brightness."""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

LUMA = QColor(235, 235, 235)
CB = QColor(96, 160, 255)
CR = QColor(255, 110, 96)


class CurvePlot(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(170)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.curves: list[tuple[np.ndarray, QColor, float]] = []
        self.empty_text = "The grain of the frame shown is estimated when it loads."
        self.setToolTip("Grain standard deviation the decoder will add at each brightness for the "
                        "frame shown, in 8-bit code values, at the strength set below.\n"
                        "White: luma. Blue/red: Cb/Cr.")

    def sizeHint(self) -> QSize:
        # A fixed-height widget is laid out at its size hint; without one a
        # plain column squeezes the plot under the rows below it.
        return QSize(360, 170)

    def set_data(self, luma, cb, cr) -> None:
        self.curves = [(np.asarray(luma), LUMA, 2.0), (np.asarray(cb), CB, 1.5),
                       (np.asarray(cr), CR, 1.5)]
        self.update()

    def clear(self, text: str | None = None) -> None:
        self.curves = []
        if text:
            self.empty_text = text
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(24, 27, 31))
        small = QFont(self.font())
        small.setPointSizeF(max(7.0, small.pointSizeF() - 1.5))
        p.setFont(small)
        if not self.curves:
            p.setPen(QColor(140, 148, 156))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                       self.empty_text)
            return
        area = QRectF(34, 8, self.width() - 44, self.height() - 34)
        top = max([float(c.max()) for c, _, _ in self.curves] + [0.5])
        top = self._nice(top * 1.1)
        p.setPen(QPen(QColor(60, 66, 72), 1))
        for i in range(5):
            y = area.bottom() - area.height() * i / 4
            p.drawLine(QPointF(area.left(), y), QPointF(area.right(), y))
            p.setPen(QColor(140, 148, 156))
            p.drawText(QRectF(0, y - 8, 30, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       f"{top * i / 4:.1f}")
            p.setPen(QPen(QColor(60, 66, 72), 1))
        p.setPen(QColor(140, 148, 156))
        for x in (0, 64, 128, 192, 255):
            px = area.left() + area.width() * x / 255
            p.drawText(QRectF(px - 20, area.bottom() + 2, 40, 14), Qt.AlignmentFlag.AlignCenter, str(x))
        p.drawText(QRectF(area.left(), area.bottom() + 13, area.width(), 14), Qt.AlignmentFlag.AlignCenter,
                   "brightness (8-bit)   ·   σ in 8-bit code values")

        def path(values: np.ndarray) -> QPainterPath:
            path = QPainterPath()
            for i, v in enumerate(values):
                point = QPointF(area.left() + area.width() * i / 255,
                                area.bottom() - area.height() * min(float(v), top) / top)
                path.moveTo(point) if i == 0 else path.lineTo(point)
            return path

        for values, color, width in reversed(self.curves):
            p.setPen(QPen(color, width))
            p.drawPath(path(values))

        legend = [("Luma", LUMA), ("Cb", CB), ("Cr", CR)]
        x = area.right() - 6
        for label, color in reversed(legend):
            width = p.fontMetrics().horizontalAdvance(label)
            x -= width
            p.setPen(color)
            p.drawText(QPointF(x, area.top() + 12), label)
            x -= 16
            p.setPen(QPen(color, 2))
            p.drawLine(QPointF(x, area.top() + 8), QPointF(x + 12, area.top() + 8))
            x -= 10

    @staticmethod
    def _nice(value: float) -> float:
        for step in (0.5, 1, 2, 2.5, 4, 5, 8, 10, 16, 20, 32, 40, 64):
            if value <= step:
                return step
        return float(np.ceil(value / 16) * 16)
