"""Validated pointer to the currently published Vemal's Ears release."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any


POINTER_FILENAME = "vemals-ears-release.json"
MAX_POINTER_BYTES = 16 * 1024
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
_VERSION_PART = re.compile(r"^[0-9]{1,6}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FINGERPRINTS: dict[tuple[str, int, int, int, int, int, int], str] = {}
_FINGERPRINT_LOCK = threading.Lock()


class ReleasePointerError(ValueError):
    """A present pointer cannot safely identify one complete release."""


@dataclass(frozen=True)
class ReleaseArtifact:
    filename: str
    path: Path
    bytes: int
    sha256: str


@dataclass(frozen=True)
class ReleaseSnapshot:
    version: str
    installer: ReleaseArtifact
    source: ReleaseArtifact


def canonical_version(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parts = value.split(".")
    if (
        len(parts) != 3
        or not all(_VERSION_PART.fullmatch(part) for part in parts)
        or any(int(part) > 65535 for part in parts)
    ):
        return None
    canonical = ".".join(str(int(part)) for part in parts)
    return canonical if canonical == value else None


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReleasePointerError("release pointer contains a duplicate key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ReleasePointerError("release pointer contains a non-finite number")


def _bounded_int(value: str) -> int:
    if len(value) > 10:
        raise ReleasePointerError("release pointer integer is too large")
    return int(value)


def _windows_change_time_ns(path: Path) -> int:
    """Return NTFS ChangeTime, which Python's Windows stat omits.

    On Windows, st_ctime is still the creation time. ChangeTime is the field
    that detects a same-size rewrite even when copy2 restores the old mtime.
    """
    import ctypes
    from ctypes import wintypes

    class FileBasicInfo(ctypes.Structure):
        _fields_ = (
            ("CreationTime", ctypes.c_longlong),
            ("LastAccessTime", ctypes.c_longlong),
            ("LastWriteTime", ctypes.c_longlong),
            ("ChangeTime", ctypes.c_longlong),
            ("FileAttributes", wintypes.DWORD),
        )

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    get_information = kernel32.GetFileInformationByHandleEx
    get_information.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    get_information.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    handle = create_file(
        str(path),
        0x0080,  # FILE_READ_ATTRIBUTES
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,  # OPEN_EXISTING
        0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise OSError(ctypes.get_last_error(), "cannot inspect file ChangeTime")
    try:
        basic = FileBasicInfo()
        if not get_information(handle, 0, ctypes.byref(basic), ctypes.sizeof(basic)):
            raise OSError(
                ctypes.get_last_error(), "cannot inspect file ChangeTime"
            )
        return int(basic.ChangeTime) * 100
    finally:
        close_handle(handle)


def _file_identity(
    path: Path,
) -> tuple[tuple[str, int, int, int, int, int, int], int]:
    try:
        details = path.lstat()
    except OSError as error:
        raise ReleasePointerError(
            "release artifact is missing: " + path.name
        ) from error
    if not stat.S_ISREG(details.st_mode):
        raise ReleasePointerError(
            "release artifact is not a regular sibling file: " + path.name
        )
    try:
        change_time_ns = (
            _windows_change_time_ns(path)
            if os.name == "nt"
            else details.st_ctime_ns
        )
    except OSError as error:
        raise ReleasePointerError(
            "release artifact metadata cannot be read: " + path.name
        ) from error
    return (
        (
            str(path),
            details.st_dev,
            details.st_ino,
            details.st_mtime_ns,
            details.st_ctime_ns,
            change_time_ns,
            details.st_size,
        ),
        details.st_size,
    )


def _regular_file(path: Path, expected_bytes: int, expected_sha256: str) -> None:
    identity, actual_bytes = _file_identity(path)
    if actual_bytes != expected_bytes:
        raise ReleasePointerError(
            "release artifact size does not match the pointer: " + path.name
        )
    with _FINGERPRINT_LOCK:
        digest = _FINGERPRINTS.get(identity)
        if digest is None:
            # Keep cold validation serialized too. A pointer lookup can hash
            # two 512 MiB artifacts, and concurrent first requests must not
            # multiply that disk and CPU load on the caption relay.
            fingerprint = hashlib.sha256()
            try:
                with path.open("rb") as handle:
                    for block in iter(lambda: handle.read(1024 * 1024), b""):
                        fingerprint.update(block)
            except OSError as error:
                raise ReleasePointerError(
                    "release artifact cannot be read: " + path.name
                ) from error
            after, _size = _file_identity(path)
            if after != identity:
                raise ReleasePointerError(
                    "release artifact changed while it was validated: " + path.name
                )
            digest = fingerprint.hexdigest()
            if len(_FINGERPRINTS) >= 16:
                _FINGERPRINTS.clear()
            _FINGERPRINTS[identity] = digest
    if digest != expected_sha256:
        raise ReleasePointerError(
            "release artifact SHA-256 does not match the pointer: " + path.name
        )


def _artifact(
    document: Any,
    directory: Path,
    expected_filename: str,
) -> ReleaseArtifact:
    if not isinstance(document, dict) or set(document) != {
        "filename",
        "bytes",
        "sha256",
    }:
        raise ReleasePointerError("release artifact has an invalid shape")
    filename = document["filename"]
    size = document["bytes"]
    digest = document["sha256"]
    if filename != expected_filename:
        raise ReleasePointerError("release artifact filename is inconsistent")
    if type(size) is not int or size <= 0 or size > MAX_ARTIFACT_BYTES:
        raise ReleasePointerError("release artifact byte count is invalid")
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise ReleasePointerError("release artifact SHA-256 is invalid")
    path = directory / filename
    if path.parent != directory:
        raise ReleasePointerError("release artifact is not a local sibling")
    _regular_file(path, size, digest)
    return ReleaseArtifact(filename, path, size, digest)


def load_release_pointer(directory: Path) -> ReleaseSnapshot | None:
    """Read one atomic pointer snapshot; return None only when it is absent."""
    pointer = directory / POINTER_FILENAME
    try:
        details = pointer.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ReleasePointerError("release pointer cannot be inspected") from error
    if not stat.S_ISREG(details.st_mode):
        raise ReleasePointerError("release pointer is not a regular file")
    if details.st_size <= 0 or details.st_size > MAX_POINTER_BYTES:
        raise ReleasePointerError("release pointer size is invalid")
    try:
        raw = pointer.read_bytes()
        if len(raw) > MAX_POINTER_BYTES:
            raise ReleasePointerError("release pointer is too large")
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
            parse_int=_bounded_int,
        )
    except ReleasePointerError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as error:
        raise ReleasePointerError("release pointer is not valid UTF-8 JSON") from error
    if not isinstance(document, dict) or set(document) != {
        "schema",
        "version",
        "installer",
        "source",
    }:
        raise ReleasePointerError("release pointer has an invalid shape")
    if type(document["schema"]) is not int or document["schema"] != 1:
        raise ReleasePointerError("release pointer schema is unsupported")
    version = canonical_version(document["version"])
    if version is None:
        raise ReleasePointerError("release pointer version is not canonical")
    installer = _artifact(
        document["installer"],
        directory,
        "Vemals-Ears-Setup-" + version + ".exe",
    )
    source = _artifact(
        document["source"],
        directory,
        "vemals-ears-member-" + version + ".zip",
    )
    return ReleaseSnapshot(version, installer, source)


def immutable_artifact(
    directory: Path,
    version: str,
    *,
    installer: bool,
) -> Path | None:
    """Resolve an older immutable artifact by its canonical safe filename."""
    normalized = canonical_version(version)
    if normalized is None:
        return None
    name = (
        "Vemals-Ears-Setup-" + normalized + ".exe"
        if installer
        else "vemals-ears-member-" + normalized + ".zip"
    )
    candidate = directory / name
    try:
        details = candidate.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(details.st_mode) or details.st_size <= 0:
        return None
    return candidate
