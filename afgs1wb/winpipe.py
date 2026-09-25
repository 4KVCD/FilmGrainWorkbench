"""Windows named pipes, so ffmpeg output can stream into programs that only open files.

AOM's ``noise_model`` reads its two inputs with ``fopen``. A whole 4K film as
raw YUV would be several terabytes, so instead each input is a named pipe
(``\\\\.\\pipe\\...``) that this process serves, relaying ffmpeg's stdout.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import uuid

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateNamedPipeW.restype = wt.HANDLE
_k32.CreateNamedPipeW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD,
                                  wt.DWORD, ctypes.c_void_p]
_k32.ConnectNamedPipe.argtypes = [wt.HANDLE, ctypes.c_void_p]
_k32.ConnectNamedPipe.restype = wt.BOOL
_k32.WriteFile.argtypes = [wt.HANDLE, ctypes.c_char_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
_k32.WriteFile.restype = wt.BOOL
_k32.CloseHandle.argtypes = [wt.HANDLE]
_k32.DisconnectNamedPipe.argtypes = [wt.HANDLE]
_k32.CreateFileW.restype = wt.HANDLE
_k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE]
_k32.SetThreadExecutionState.argtypes = [wt.DWORD]
_k32.SetThreadExecutionState.restype = wt.DWORD

_INVALID = wt.HANDLE(-1).value
_PIPE_ACCESS_OUTBOUND = 0x2
_PIPE_TYPE_BYTE = 0x0
_PIPE_WAIT = 0x0
_ERROR_PIPE_CONNECTED = 535
_GENERIC_READ = 0x80000000
_OPEN_EXISTING = 3
_BUFFER = 1 << 24


class PipeClosed(Exception):
    """The reading program closed its end (it exited or failed)."""


class NamedPipeWriter:
    """Server end of an outbound named pipe; the other program opens ``path``."""

    def __init__(self, tag: str) -> None:
        self.path = "\\\\.\\pipe\\afgs1wb-" + tag + "-" + uuid.uuid4().hex[:8]
        self._handle = _k32.CreateNamedPipeW(self.path, _PIPE_ACCESS_OUTBOUND,
                                             _PIPE_TYPE_BYTE | _PIPE_WAIT, 1, _BUFFER, _BUFFER, 0, None)
        if self._handle == _INVALID or self._handle is None:
            raise OSError(ctypes.get_last_error(), f"could not create named pipe {self.path}")
        self._closed = False

    def wait_for_reader(self) -> None:
        """Block until the other program opens the pipe."""
        if not _k32.ConnectNamedPipe(self._handle, None):
            error = ctypes.get_last_error()
            if error != _ERROR_PIPE_CONNECTED:
                raise PipeClosed(f"pipe connection failed (Windows error {error})")
        if self._closed:
            raise PipeClosed("pipe closed before a reader connected")

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        offset = 0
        while offset < len(data):
            written = wt.DWORD()
            chunk = bytes(view[offset:]) if offset else data
            if not _k32.WriteFile(self._handle, chunk, len(chunk), ctypes.byref(written), None):
                raise PipeClosed(f"reader closed the pipe (Windows error {ctypes.get_last_error()})")
            offset += written.value

    def unblock(self) -> None:
        """Wake a thread stuck in :meth:`wait_for_reader` (used when cancelling)."""
        self._closed = True
        handle = _k32.CreateFileW(self.path, _GENERIC_READ, 0, None, _OPEN_EXISTING, 0, None)
        if handle not in (_INVALID, None):
            _k32.CloseHandle(handle)

    def close(self) -> None:
        if self._handle:
            _k32.CloseHandle(self._handle)
            self._handle = None


def keep_awake(on: bool) -> None:
    """Stop Windows sleeping (the display may still turn off) during long work."""
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    _k32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
