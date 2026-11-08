import errno
import os
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def instance_lock(root: Path):
    with (root / "tracker.lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt

            if not lock.tell():
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            acquire = lambda: msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            acquire = lambda: fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            acquire()
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise
            raise SystemExit("Трекер уже запущен в этой папке. Остановите предыдущий процесс.") from None
        yield
