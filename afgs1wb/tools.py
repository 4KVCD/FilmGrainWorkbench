"""External tools (ffmpeg, ffprobe, mkvmerge) and cancellable process runs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import shutil
import subprocess
import threading

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class Cancelled(Exception):
    """Raised when the user cancels a running job."""


class ToolError(RuntimeError):
    pass


class CancelToken:
    """Shared between a job and the GUI; cancelling kills running child processes."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._processes: set[subprocess.Popen] = set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()
        with self._lock:
            for process in list(self._processes):
                try:
                    process.kill()
                except OSError:
                    pass

    def check(self) -> None:
        if self._event.is_set():
            raise Cancelled()

    def _track(self, process: subprocess.Popen) -> None:
        with self._lock:
            self._processes.add(process)
        if self._event.is_set():
            process.kill()

    def _untrack(self, process: subprocess.Popen) -> None:
        with self._lock:
            self._processes.discard(process)


def run_with_log(command: list[str], cancel: CancelToken | None = None, *,
                 stdin: bytes | None = None) -> tuple[bytes, str]:
    """Run a command; return (stdout, stderr text). Raise ToolError on failure."""
    if cancel:
        cancel.check()
    try:
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=_NO_WINDOW)
    except FileNotFoundError as exc:
        raise ToolError(f"Program not found: {command[0]}") from exc
    if cancel:
        cancel._track(process)
    try:
        out, err = process.communicate(stdin)
    finally:
        if cancel:
            cancel._untrack(process)
    if cancel and cancel.cancelled:
        raise Cancelled()
    message = err.decode("utf-8", "replace")
    if process.returncode != 0:
        lines = [l for l in message.strip().splitlines() if "Parsed_showinfo" not in l]
        tail = "\n".join(lines[-12:])
        raise ToolError(f"{Path(command[0]).name} failed (exit code {process.returncode}):\n{tail}")
    return out, message


def run(command: list[str], cancel: CancelToken | None = None, *,
        stdin: bytes | None = None) -> bytes:
    """Run a command and return its stdout; raise ToolError with stderr on failure."""
    return run_with_log(command, cancel, stdin=stdin)[0]


def run_text(command: list[str], cancel: CancelToken | None = None) -> str:
    return run(command, cancel).decode("utf-8", "replace")


@dataclass
class Tools:
    ffmpeg: str = ""
    ffprobe: str = ""
    mkvmerge: str = ""

    LABELS = {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe", "mkvmerge": "mkvmerge (MKVToolNix)"}

    def missing(self, *names: str) -> list[str]:
        names = names or tuple(self.LABELS)
        return [self.LABELS[n] for n in names
                if not getattr(self, n) or not Path(getattr(self, n)).is_file()]


def detect_tools() -> Tools:
    """Best-effort discovery from PATH and a few conventional locations."""
    tools = Tools(shutil.which("ffmpeg") or "", shutil.which("ffprobe") or "",
                  shutil.which("mkvmerge") or "")
    if not tools.mkvmerge:
        for root in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
            candidate = Path(root) / "MKVToolNix" / "mkvmerge.exe"
            if candidate.is_file():
                tools.mkvmerge = str(candidate)
                break
    return tools


def cpu_count() -> int:
    return os.cpu_count() or 4


def keep_awake(on: bool) -> None:
    """Stop Windows sleeping during long work (the display may still turn off)."""
    if os.name != "nt":
        return
    import ctypes
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
