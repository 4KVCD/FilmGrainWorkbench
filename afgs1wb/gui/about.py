"""Help menu: About, and the installation check (which programs were found, which versions)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QFont, QPixmap
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPlainTextEdit,
                               QVBoxLayout, QWidget)

from .. import __version__
from ..fullscan import analyzer_path
from ..paths import app_dir, resource
from ..tools import Tools
from ..vvcgrain import VvcTools

APP_NAME = "Film Grain Workbench"
CHECK_TIMEOUT_S = 10              # a program that does not answer --version by then is reported as hung
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class ProgramCheck:
    name: str
    purpose: str
    path: str                     # "" when not found
    detail: str                   # its version line, or what is wrong
    ok: bool


def _first_line(command: list[str]) -> tuple[bool, str]:
    """(answered, the first line it printed) for a program asked for its version."""
    try:
        result = subprocess.run(command, capture_output=True, stdin=subprocess.DEVNULL,
                                timeout=CHECK_TIMEOUT_S, creationflags=_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"does not run: {exc}"
    text = (result.stdout or result.stderr).decode("utf-8", "replace").strip()
    return (True, text.splitlines()[0]) if text else (False, f"no output (exit code {result.returncode})")


def _ffmpeg_detail(ffmpeg: str) -> tuple[bool, str]:
    ok, version = _first_line([ffmpeg, "-hide_banner", "-version"])
    if not ok:
        return ok, version
    try:
        decoders = subprocess.run([ffmpeg, "-hide_banner", "-decoders"], capture_output=True,
                                  stdin=subprocess.DEVNULL, timeout=CHECK_TIMEOUT_S,
                                  creationflags=_NO_WINDOW).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired):
        decoders = ""
    if not any(line.split()[1:2] == ["vvc"] for line in decoders.splitlines()):
        return False, f"{version}: no VVC decoder (FFmpeg 7.1 or newer has one)"
    return True, version


def _analyzer_detail(analyzer: str) -> tuple[bool, str]:
    ok, text = _first_line([analyzer, "--help"])
    return (True, "runs") if ok and text.startswith("usage") else (False, text)


def check_installation(tools: Tools, vvc_tools: VvcTools) -> list[ProgramCheck]:
    """Every program the app runs: where it was found and whether it answers."""
    def program(name: str, purpose: str, path: str, detail_of) -> ProgramCheck:
        if not path or not Path(path).is_file():
            return ProgramCheck(name, purpose, "", "not found", False)
        ok, detail = detail_of(path)
        return ProgramCheck(name, purpose, path, detail, ok)

    def version(path: str) -> tuple[bool, str]:
        return _first_line([path, "--version"])

    checks = [
        program("ffmpeg", "decodes video (both tabs)", tools.ffmpeg, _ffmpeg_detail),
        program("ffprobe", "reads video properties (both tabs)", tools.ffprobe,
                lambda path: _first_line([path, "-hide_banner", "-version"])),
        program("mkvmerge", "writes the MKV (AV1 tab)", tools.mkvmerge, version),
        program("vvencapp", "encodes scenes (VVC tab)", vvc_tools.vvencapp, version),
        program("vvdecapp", "decodes scenes (VVC tab)", vvc_tools.vvdecapp, version),
        program("grain analyzer", "every-frame grain analysis (AV1 tab)", str(analyzer_path()), _analyzer_detail),
    ]
    try:
        import av
        checks.append(ProgramCheck("PyAV", "reads the encode when writing (AV1 tab)", av.__file__,
                                   f"version {av.__version__}", True))
    except ImportError as exc:
        checks.append(ProgramCheck("PyAV", "reads the encode when writing (AV1 tab)", "", str(exc), False))
    return checks


def format_report(checks: list[ProgramCheck]) -> str:
    lines = [f"{APP_NAME} {__version__}", f"Folder: {app_dir()}", ""]
    for c in checks:
        status = "OK" if c.ok else ("MISSING" if not c.path else "FAIL")
        lines += [f"{status:8s}{c.name}: {c.purpose}", f"        {c.path or '(not found)'}", f"        {c.detail}"]
    if not all(c.ok for c in checks):
        lines += ["", "ffmpeg, ffprobe and mkvmerge are set under Settings > External programs.",
                  "ffmpeg: ffmpeg.org/download.html (a build with VVC decoding, FFmpeg 7.1 or newer).",
                  "mkvmerge: mkvtoolnix.download (needed only to write MKVs in the AV1 tab)."]
    return "\n".join(lines)


class InstallationDialog(QDialog):
    def __init__(self, tools: Tools, vvc_tools: VvcTools, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Check installation")
        self.resize(820, 460)
        layout = QVBoxLayout(self)
        text = QPlainTextEdit(readOnly=True)
        text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        text.setFont(mono)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            text.setPlainText(format_report(check_installation(tools, vvc_tools)))
        finally:
            QApplication.restoreOverrideCursor()
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        copy = buttons.addButton("Copy", QDialogButtonBox.ButtonRole.ActionRole)
        copy.clicked.connect(lambda: QApplication.clipboard().setText(text.toPlainText()))
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class AboutDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"About {APP_NAME}")
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        icon = QLabel()
        icon.setPixmap(QPixmap(str(resource("icon.png"))).scaled(
            96, 96, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        top.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
        text = QLabel(
            f"<h2>{APP_NAME} {__version__}</h2>"
            "<p>See what VVC's own film grain analysis (VVenC) and synthesis (vvdec) do to a film, "
            "scene by scene; and measure the grain a VVC encode lost, then write it back as AV1 "
            "film grain (AFGS1) metadata.</p>"
            "<p>Bundled: VVenC and vvdec (Fraunhofer HHI, BSD 3-Clause Clear), a grain analyzer "
            "built on libaom's noise model (AOMedia, BSD 2-Clause), Qt for Python (LGPL v3), "
            "NumPy, PyAV and the FFmpeg libraries it ships with. Their licenses are in the "
            "<i>licenses</i> folder.</p>")
        text.setWordWrap(True)
        text.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        text.setMinimumWidth(460)
        top.addWidget(text, 1)
        layout.addLayout(top)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        licenses = app_dir() / "licenses"
        if licenses.is_dir():
            show = buttons.addButton("Open licenses folder", QDialogButtonBox.ButtonRole.ActionRole)
            show.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(licenses))))
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
