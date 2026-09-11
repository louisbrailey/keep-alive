"""flock-based single-instance lock for KeepAlive itself.

Non-blocking: if another instance already holds the lock, ``acquired`` is
``False`` and the caller (the engine) treats that as an ``overlap_skipped``
cycle rather than raising.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from types import TracebackType
from typing import IO


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.acquired = False
        self._handle: IO[str] | None = None

    def __enter__(self) -> InstanceLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            self.acquired = False
            self._handle = None
            return self
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._handle = handle
        self.acquired = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._handle is not None:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            finally:
                self._handle.close()
            self._handle = None
        self.acquired = False


__all__ = ["InstanceLock"]
