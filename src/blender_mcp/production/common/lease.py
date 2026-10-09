"""OS-held exclusive production controller lease (released even after process death)."""
import os
from pathlib import Path
import tempfile


class ControllerLease:
    def __init__(self, port: int):
        self.path = Path(tempfile.gettempdir()) / f"blender-production-{port}.lock"
        self.file = None

    def __enter__(self):
        self.file = self.path.open("a+b")
        self.file.seek(0, 2)
        if not self.file.tell():
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            self.file = None
            raise RuntimeError("Another production controller already holds this Blender port") from exc
        return self

    def __exit__(self, *exc):
        if self.file:
            self.file.close()
            self.file = None
