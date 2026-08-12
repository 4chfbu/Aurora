from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import tempfile
from typing import Iterator, TextIO


def _database_identity(database_url: str) -> str:
    prefix = "sqlite:///"
    if database_url.startswith(prefix):
        database_path = database_url[len(prefix) :]
        if database_path != ":memory:":
            return f"sqlite:///{Path(database_path).resolve()}"
    return database_url


def api_instance_lock_path(database_url: str, *, lock_dir: Path | None = None) -> Path:
    digest = hashlib.sha256(_database_identity(database_url).encode("utf-8")).hexdigest()[:16]
    return (lock_dir or Path(tempfile.gettempdir())) / f"aurora-api-{digest}.lock"


@contextmanager
def acquire_api_instance_lock(database_url: str, *, lock_dir: Path | None = None) -> Iterator[Path]:
    """Allow only one API process to mutate startup recovery state per database."""
    path = api_instance_lock_path(database_url, lock_dir=lock_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle: TextIO = path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                f"another Aurora API instance is already active for this database (lock: {path})"
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        yield path
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
