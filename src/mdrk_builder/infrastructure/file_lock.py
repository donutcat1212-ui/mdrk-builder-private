"""OS file lock shared by portable files in the program directory."""
import os
from pathlib import Path
import time
from typing import BinaryIO


class InterprocessFileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._file: BinaryIO | None = None
        self._locked = False

    def acquire(self, *, timeout: float) -> bool:
        self._file = self.path.open("a+b")
        try:
            self._ensure_lock_byte()
        except OSError:
            self._close()
            raise
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                self._try_lock()
                self._locked = True
                return True
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    self._close()
                    return False
                time.sleep(0.05)

    def release(self) -> None:
        if self._file is None:
            return
        try:
            if self._locked:
                self._unlock()
        finally:
            self._locked = False
            self._close()

    def _ensure_lock_byte(self) -> None:
        assert self._file is not None
        self._file.seek(0, os.SEEK_END)
        if self._file.tell() == 0:
            self._file.write(b"\0")
            self._file.flush()
        self._file.seek(0)

    def _try_lock(self) -> None:
        assert self._file is not None
        if os.name == "nt":
            import msvcrt

            self._file.seek(0)
            msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            return

        import fcntl

        fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(self) -> None:
        assert self._file is not None
        if os.name == "nt":
            import msvcrt

            self._file.seek(0)
            msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            return

        import fcntl

        fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)

    def _close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
