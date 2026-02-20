import os
import atexit
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None


class SingleInstanceError(RuntimeError):
    pass


class SingleInstanceLock:
    def __init__(self, name: str, lock_dir: str = "/tmp/tradebot-locks"):
        self.name = str(name).strip() or "tradebot"
        self.lock_dir = Path(lock_dir)
        self.lock_file = self.lock_dir / f"{self.name}.lock"
        self._handle = None
        self._locked = False

    def acquire(self) -> None:
        if fcntl is None:
            return

        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.lock_file, "a+", encoding="utf-8")

        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._handle.seek(0)
            existing = self._handle.read().strip()
            raise SingleInstanceError(
                f"Another instance is already running for '{self.name}'"
                + (f" ({existing})" if existing else "")
            )

        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(f"pid={os.getpid()} cwd={os.getcwd()}\n")
        self._handle.flush()
        self._locked = True
        atexit.register(self.release)

    def release(self) -> None:
        if not self._locked:
            return
        try:
            if self._handle is not None and fcntl is not None:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            if self._handle is not None:
                self._handle.close()
        except OSError:
            pass
        self._handle = None
        self._locked = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False
