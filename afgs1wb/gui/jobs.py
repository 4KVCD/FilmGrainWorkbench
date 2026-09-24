"""Background work with progress and cancellation."""

from __future__ import annotations

import traceback

from PySide6.QtCore import QThread, Signal

from ..tools import CancelToken, Cancelled, ToolError


class Job(QThread):
    """Runs ``fn(token, progress)`` off the GUI thread.

    ``progress(fraction, message)`` may be called from any thread; fraction
    is 0..1, or a negative number for "busy, no percentage".
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
