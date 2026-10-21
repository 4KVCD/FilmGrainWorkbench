"""Background work with progress and cancellation."""

from __future__ import annotations

import traceback

import shiboken6
from PySide6.QtCore import QThread, Signal

from ..tools import CancelToken, Cancelled, ToolError


def running(job: "Job | None") -> bool:
    """Whether ``job`` is still running. A finished Job deletes itself, so an old
    reference to one reads as not running instead of raising."""
    return job is not None and shiboken6.isValid(job) and job.isRunning()


class Job(QThread):
    """Runs ``fn(token, progress)`` off the GUI thread.

    ``progress(fraction, message)`` may be called from any thread; fraction
    is 0..1, or a negative number for "busy, no percentage". A finished job
    deletes itself (it is parented to a window, which would otherwise keep
    every job of the session); check it with :func:`running`.
    """
    progress = Signal(float, str)
    succeeded = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, name: str, fn, parent=None) -> None:
        super().__init__(parent)
        self.name = name
        self.fn = fn
        self.token = CancelToken()
        self.finished.connect(self.deleteLater)

    def cancel(self) -> None:
        self.token.cancel()

    def run(self) -> None:
        try:
            result = self.fn(self.token, self.progress.emit)
        except Cancelled:
            self.cancelled.emit()
        except (ToolError, RuntimeError, ValueError, OSError) as exc:
            if self.token.cancelled:
                self.cancelled.emit()
            else:
                self.failed.emit(str(exc))
        except Exception:                       # unexpected: keep the traceback
            self.failed.emit(traceback.format_exc(limit=6))
        else:
            if self.token.cancelled:
                self.cancelled.emit()
            else:
                self.succeeded.emit(result)
