"""One run at a time per database.

A scheduled run and a manual run against the same SQLite file would interleave their
writes and race on the push watermark. The lock is a file created with O_EXCL next to
the database, so taking it is atomic on every platform.

A lock is taken over when its holder is gone: the recorded PID is no longer running,
or the lock is older than `stale_after` (longer than any real run, which also covers
a PID that was reused by an unrelated process). A run killed mid-way -- closed
console, power loss, OOM -- never gets to delete its lock, so without the liveness
check it would block every run for hours.

Liveness is checked per platform on purpose: on Windows `os.kill(pid, 0)` does not
probe a process, it TERMINATES it, so there it goes through OpenProcess instead.
"""
from __future__ import annotations

import ctypes
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

#: Longer than any real run: a full multi-repo backfill is under two hours.
DEFAULT_STALE_AFTER = 6 * 3600


class RunLocked(RuntimeError):
    """Another run holds the lock."""


def pid_alive(pid: int) -> bool:
    """Is a process with this PID running? Never signals or harms it."""
    if pid <= 0:
        return False
    if os.name == "nt":
        from ctypes import wintypes

        # use_last_error so GetLastError survives ctypes' own calls; explicit types
        # because HANDLE is pointer-sized and the default c_int truncates it on x64.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        process_query_limited_information = 0x1000
        still_active = 259
        error_access_denied = 5

        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            # It exists but belongs to someone we may not query: treat it as alive.
            return ctypes.get_last_error() == error_access_denied
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)  # POSIX only: signal 0 is a pure existence check
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def lock_path(db_path: str | Path) -> Path:
    return Path(f"{db_path}.lock")


@contextmanager
def run_lock(db_path: str | Path, *, stale_after: float = DEFAULT_STALE_AFTER) -> Iterator[Path]:
    """Hold the database's run lock for the duration of the block."""
    path = lock_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    for _attempt in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            age = time.time() - path.stat().st_mtime
            try:
                holder = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                holder = {}
            alive = pid_alive(int(holder.get("pid") or 0))
            if alive and age < stale_after:
                raise RunLocked(
                    f"another gitscout run holds {path} (pid {holder.get('pid', '?')}, "
                    f"started {int(age // 60)} min ago). If no run is active, delete the file."
                ) from None
            # Abandoned: its process is gone, or it outlived any real run.
            path.unlink(missing_ok=True)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"pid": os.getpid(), "started": time.time()}, fh)
        try:
            yield path
        finally:
            path.unlink(missing_ok=True)
        return
    raise RunLocked(f"could not take {path}")
