"""Draw the app icon: afgs1wb/gui/resources/icon.ico (16-256 px) and icon.png (256 px).

    python packaging/make_icon.py

A rounded square split on the diagonal: a smooth picture on one side, the
same picture with film grain on the other, which is what the app compares.
"""

from __future__ import annotations

from pathlib import Path
import random
import struct
import sys

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath, QPen

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "afgs1wb" / "gui" / "resources"
SIZES = (16, 24, 32, 48, 64, 128, 256)
GRAIN_SEED = 7391


def draw(size: int) -> QImage:
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    margin = size * 0.06
    box = QRectF(margin, margin, size - 2 * margin, size - 2 * margin)
    shape = QPainterPath()
    shape.addRoundedRect(box, size * 0.18, size * 0.18)
    p.setClipPath(shape)

    sky = QLinearGradient(box.topLeft(), box.bottomLeft())
    sky.setColorAt(0.0, QColor("#3d6ea8"))
    sky.setColorAt(0.55, QColor("#2f8f6f"))
    sky.setColorAt(1.0, QColor("#1b3a33"))
    p.fillRect(box, sky)

    # The grainy half: below the diagonal from bottom-left to top-right.
    grainy = QPainterPath()
    grainy.moveTo(box.bottomLeft())
    grainy.lineTo(box.topRight())
    grainy.lineTo(box.bottomRight())
    grainy.closeSubpath()
    p.save()
    p.setClipPath(shape.intersected(grainy))
    rng = random.Random(GRAIN_SEED)
    dot = max(1.0, size / 96)
    for _ in range(int(size * size / (dot * dot) * 0.7)):
        x, y = rng.uniform(box.left(), box.right()), rng.uniform(box.top(), box.bottom())
        light = rng.random() < 0.5
        alpha = rng.randint(35, 120)
        p.fillRect(QRectF(x, y, dot, dot), QColor(255, 255, 255, alpha) if light else QColor(0, 0, 0, alpha))
    p.restore()

    p.setClipPath(shape)
    p.setPen(QPen(QColor(255, 255, 255, 230), max(1.0, size / 32)))
    p.drawLine(QPointF(box.left(), box.bottom()), QPointF(box.right(), box.top()))
    p.setClipping(False)
    p.setPen(QPen(QColor(255, 255, 255, 60), max(1.0, size / 64)))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPath(shape)
    p.end()
    return image


def png_bytes(image: QImage) -> bytes:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(data)


def write_ico(path: Path, images: dict[int, bytes]) -> None:
    """An ICO file with PNG-compressed entries (Windows Vista and later read them)."""
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries, blobs = b"", b""
    for size, data in images.items():
        side = 0 if size >= 256 else size                  # 0 means 256 in an ICO directory
        entries += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(data), offset + len(blobs))
        blobs += data
    path.write_bytes(header + entries + blobs)


def main() -> int:
    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1] + ["-platform", "offscreen"])
    OUT.mkdir(parents=True, exist_ok=True)
    images = {size: png_bytes(draw(size)) for size in SIZES}
    write_ico(OUT / "icon.ico", images)
    (OUT / "icon.png").write_bytes(images[256])
    print(f"wrote {OUT / 'icon.ico'} ({len(SIZES)} sizes) and icon.png")
    del app
    return 0


if __name__ == "__main__":
    sys.exit(main())
