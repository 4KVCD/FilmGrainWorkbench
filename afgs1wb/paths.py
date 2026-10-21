"""Where the app's own files are: next to the executable when packaged, in the repository otherwise."""

from __future__ import annotations

from pathlib import Path
import sys


def packaged() -> bool:
    """Running as the packaged app (PyInstaller), not from the source."""
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """The folder holding bin/ and licenses/: the executable's folder, or the repository."""
    if packaged():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def bundled_program(name: str) -> Path:
    """A program the app ships in its bin folder: the grain analyzer, vvencapp, vvdecapp."""
    return app_dir() / "bin" / f"{name}.exe"


def missing_program_hint() -> str:
    """What to do when a bundled program is not there."""
    if packaged():
        return "The app's bin folder is incomplete: download and unzip the app again."
    return "Build it with: python build.py"


def resource(name: str) -> Path:
    """A file in afgs1wb/gui/resources (the icon), packaged or not."""
    return Path(__file__).resolve().parent / "gui" / "resources" / name
