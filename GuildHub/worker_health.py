"""Dependency-light worker health marker shared by host and console."""

from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path


MAX_HEALTH_FILE_BYTES = 32
MAX_HEALTH_PATH_CHARS = 4096
DEFAULT_HEALTH_FILE = str(Path(tempfile.gettempdir()) / "vemals-ears-worker.state")
HEALTH_FILE_SETTING = "VEMALS_EARS_WORKER_HEALTH_FILE"
HEALTHY_STATES = frozenset({"ready", "paused", "active"})


class WorkerHealthError(RuntimeError):
    """The non-secret worker health marker could not be updated safely."""


def resolve_worker_health_file(environment: Mapping[str, str]) -> Path:
    """Resolve the shared marker path without importing the worker runtime."""
    path_text = environment.get(HEALTH_FILE_SETTING)
    if path_text is None:
        path_text = DEFAULT_HEALTH_FILE
    if not isinstance(path_text, str):
        raise WorkerHealthError("Worker health path is invalid.")
    path_text = path_text.strip() or DEFAULT_HEALTH_FILE
    if len(path_text) > MAX_HEALTH_PATH_CHARS or "\x00" in path_text:
        raise WorkerHealthError("Worker health path is invalid.")
    try:
        path = Path(path_text)
    except (TypeError, ValueError):
        raise WorkerHealthError("Worker health path is invalid.") from None
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise WorkerHealthError("Worker health path is invalid.")
    return path


class WorkerHealthMarker:
    """Atomic, non-secret state used only by the worker healthcheck."""

    def __init__(self, path: Path) -> None:
        if not isinstance(path, Path) or not path.is_absolute():
            raise WorkerHealthError("Worker health path is invalid.")
        self.path = path

    def set_state(self, state: str) -> None:
        if state not in HEALTHY_STATES:
            raise WorkerHealthError("Worker health state is invalid.")
        parent = self.path.parent
        temporary_path: Path | None = None
        try:
            metadata = parent.lstat()
            if parent.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
                raise OSError("unsafe health directory")
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=parent,
                prefix="." + self.path.name + ".",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                handle.write((state + "\n").encode("ascii"))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, self.path)
            temporary_path = None
        except (OSError, ValueError):
            raise WorkerHealthError("Worker health could not be updated.") from None
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except OSError:
                    pass

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            return
        except OSError:
            raise WorkerHealthError("Worker health could not be cleared.") from None

    def read_state(self) -> str | None:
        """Return the exact accepted session state, or None when unavailable."""
        try:
            metadata = self.path.lstat()
            if self.path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
                return None
            if metadata.st_size < 1 or metadata.st_size > MAX_HEALTH_FILE_BYTES:
                return None
            with self.path.open("rb") as handle:
                payload = handle.read(MAX_HEALTH_FILE_BYTES + 1)
            if len(payload) > MAX_HEALTH_FILE_BYTES:
                return None
            text = payload.decode("ascii")
            if not text.endswith("\n"):
                return None
            state = text[:-1]
            return state if state in HEALTHY_STATES else None
        except (OSError, UnicodeError, ValueError):
            return None

    def is_healthy(self) -> bool:
        return self.read_state() is not None
