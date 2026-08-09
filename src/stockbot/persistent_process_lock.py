from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Callable


PERSISTENT_LIVE_MUTEX_NAME = "Global\\StockBotLiveScheduler"
ERROR_ACCESS_DENIED = 5
ERROR_ALREADY_EXISTS = 183


class PersistentLiveProcessLock:
    def __init__(
        self,
        handle: int | None = None,
        *,
        close_handle: Callable[[int], object] | None = None,
    ) -> None:
        self._handle = handle
        self._close_handle = close_handle

    def close(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is not None and self._close_handle is not None:
            self._close_handle(handle)

    def __enter__(self) -> PersistentLiveProcessLock:
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


def acquire_persistent_live_process_lock(
    *,
    name: str = PERSISTENT_LIVE_MUTEX_NAME,
) -> PersistentLiveProcessLock:
    if os.name != "nt":
        return PersistentLiveProcessLock()

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    create_mutex.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    ctypes.set_last_error(0)
    handle = create_mutex(None, False, name)
    error_code = ctypes.get_last_error()
    if not handle:
        if error_code == ERROR_ACCESS_DENIED:
            raise RuntimeError("another StockBot persistent live scheduler is already active")
        raise OSError(error_code, "unable to acquire the StockBot persistent live process lock")
    if error_code == ERROR_ALREADY_EXISTS:
        close_handle(handle)
        raise RuntimeError("another StockBot persistent live scheduler is already active")
    return PersistentLiveProcessLock(int(handle), close_handle=close_handle)
