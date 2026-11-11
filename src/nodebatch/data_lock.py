"""Lifetime OS lock for a canonical local data directory.

The file is deliberately never unlinked: replacing its inode would let another
process acquire a different lock. The OS releases ownership on process death;
there are no PID-based stale-lock decisions.
"""
from .i18n import tr
from pathlib import Path
import errno
import os


class DataDirectoryBusy(RuntimeError):
    pass


class DataDirectoryLock:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._file = (self.root / ".nodebatch.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                # Windows byte-range locking needs an existing byte.
                self._file.seek(0, os.SEEK_END)
                if not self._file.tell():
                    self._file.write(b"\0")
                    self._file.flush()
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._file.close()
            self._file = None
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise DataDirectoryBusy(
                    tr('The data directory is already used by another NodeBatch instance: {p0}. Close it or choose another --data-dir.', p0=self.root)) from None
            raise

    def close(self):
        if self._file is not None:
            # Closing the descriptor releases the OS lock on both platforms.
            self._file.close()
            self._file = None
