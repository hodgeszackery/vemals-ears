"""Windows-only, app-isolated device credential storage.

The active guild server and its device token live in one Windows Generic
Credential. There is deliberately no file-backed fallback: if Credential
Manager is unavailable, enrollment fails closed instead of writing a token to
disk.
"""

from __future__ import annotations

import ctypes
import ipaddress
import json
import os
import re
import unicodedata
from ctypes import wintypes
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from GuildHub.protocol import MAX_DEVICE_ID, MAX_TOKEN

CREDENTIAL_TARGET = "Vemal's Ears.GuildHub.Device"
CREDENTIAL_COMMENT = "Vemal's Ears guild device"
DOWNTIME_CREDENTIAL_TARGET = "Downtime.GuildHub.Device"
DOWNTIME_CREDENTIAL_COMMENT = "Downtime guild device"

# A non-secret marker in Downtime's own slot prevents a deliberate unlink from
# silently importing the VE credential again on the next launch.
_UNPAIRED_DEVICE = ("unpaired", b'{"version":1,"state":"unpaired"}')

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168
MAX_CREDENTIAL_BLOB_BYTES = 1_024
MAX_SERVER_URL = 320
MAX_GUILD_ID = 64
MAX_GUILD_NAME = 96

_DNS_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


class DeviceCredentialValidationError(ValueError):
    """A device credential or server URL is malformed."""


class DeviceCredentialStoreError(RuntimeError):
    """Credential Manager could not complete the requested operation."""


@dataclass(frozen=True, repr=False)
class DeviceCredentials:
    server_url: str
    device_id: str
    device_token: str
    guild_id: str
    guild_name: str

    def __repr__(self) -> str:
        return "DeviceCredentials(<redacted>)"

    __str__ = __repr__


class DeviceCredentialStore(Protocol):
    def save(self, credentials: DeviceCredentials) -> None: ...

    def load(self) -> DeviceCredentials | None: ...

    def delete(self) -> None: ...


class _CredentialApi(Protocol):
    def write(self, target: str, username: str, secret: bytes) -> None: ...

    def read(self, target: str) -> tuple[str, bytes] | None: ...

    def delete(self, target: str) -> None: ...


def _clean_text(value: object, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise DeviceCredentialValidationError(field + " must be text")
    clean = unicodedata.normalize("NFKC", value).strip()
    if not clean or len(clean) > limit:
        raise DeviceCredentialValidationError(field + " is missing or too long")
    if any(unicodedata.category(character).startswith("C") for character in clean):
        raise DeviceCredentialValidationError(field + " contains control characters")
    return clean


def _safe_id(value: object, field: str, limit: int) -> str:
    clean = _clean_text(value, field, limit)
    if not all(
        character.isascii() and (character.isalnum() or character in "_-")
        for character in clean
    ):
        raise DeviceCredentialValidationError(field + " contains unsupported characters")
    return clean


def normalize_server_url(value: object) -> str:
    """Validate a websocket origin and return its canonical form.

    Remote servers must use TLS. Plain websocket origins are accepted only for
    a loopback development server.
    """

    raw = _clean_text(value, "server URL", MAX_SERVER_URL)
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        raise DeviceCredentialValidationError("server URL is invalid") from None
    scheme = parsed.scheme.lower()
    if scheme not in {"ws", "wss"}:
        raise DeviceCredentialValidationError("server URL must use wss")
    if not parsed.netloc or parsed.hostname is None:
        raise DeviceCredentialValidationError("server URL must include a host")
    if parsed.username is not None or parsed.password is not None:
        raise DeviceCredentialValidationError("server URL cannot include user information")
    if parsed.query or parsed.fragment:
        raise DeviceCredentialValidationError("server URL cannot include a query or fragment")
    if parsed.path not in {"", "/"}:
        raise DeviceCredentialValidationError("server URL must not include a path")
    if port is not None and not (1 <= port <= 65535):
        raise DeviceCredentialValidationError("server URL port is invalid")

    hostname = parsed.hostname
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        is_loopback = hostname.lower() == "localhost"
        try:
            canonical_host = hostname.encode("idna").decode("ascii").lower()
        except UnicodeError:
            raise DeviceCredentialValidationError("server URL host is invalid") from None
        labels = canonical_host.split(".")
        if (
            len(canonical_host) > 253
            or any(not _DNS_LABEL.fullmatch(label) for label in labels)
        ):
            raise DeviceCredentialValidationError("server URL host is invalid")
    else:
        is_loopback = address.is_loopback
        canonical_host = address.compressed

    if scheme == "ws" and not is_loopback:
        raise DeviceCredentialValidationError(
            "remote Vemal's Ears servers must use encrypted wss connections"
        )

    rendered_host = (
        "[" + canonical_host + "]" if ":" in canonical_host else canonical_host
    )
    rendered_port = "" if port is None else ":" + str(port)
    return scheme + "://" + rendered_host + rendered_port


def validate_device_credentials(
    server_url: object,
    device_id: object,
    device_token: object,
    guild_id: object,
    guild_name: object,
) -> DeviceCredentials:
    return DeviceCredentials(
        server_url=normalize_server_url(server_url),
        device_id=_safe_id(device_id, "device ID", MAX_DEVICE_ID),
        device_token=_safe_id(device_token, "device token", MAX_TOKEN),
        guild_id=_safe_id(guild_id, "guild ID", MAX_GUILD_ID),
        guild_name=_clean_text(guild_name, "guild name", MAX_GUILD_NAME),
    )


_PBYTE = ctypes.POINTER(ctypes.c_ubyte)


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", _PBYTE),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class _CtypesCredentialApi:
    """Small ctypes wrapper around Windows Credential Manager."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise DeviceCredentialStoreError(
                "Windows Credential Manager is unavailable on this computer."
            )
        library = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self._cred_write = library.CredWriteW
        self._cred_write.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
        self._cred_write.restype = wintypes.BOOL

        self._cred_read = library.CredReadW
        self._cred_read.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(_CREDENTIALW)),
        ]
        self._cred_read.restype = wintypes.BOOL

        self._cred_delete = library.CredDeleteW
        self._cred_delete.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self._cred_delete.restype = wintypes.BOOL

        self._cred_free = library.CredFree
        self._cred_free.argtypes = [ctypes.c_void_p]
        self._cred_free.restype = None

    def write(self, target: str, username: str, secret: bytes) -> None:
        blob = (ctypes.c_ubyte * len(secret)).from_buffer_copy(secret)
        credential = _CREDENTIALW()
        credential.Type = CRED_TYPE_GENERIC
        credential.TargetName = target
        credential.Comment = (
            DOWNTIME_CREDENTIAL_COMMENT
            if target == DOWNTIME_CREDENTIAL_TARGET else CREDENTIAL_COMMENT
        )
        credential.CredentialBlobSize = len(secret)
        credential.CredentialBlob = ctypes.cast(blob, _PBYTE)
        credential.Persist = CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = username
        if not self._cred_write(ctypes.byref(credential), 0):
            raise ctypes.WinError(ctypes.get_last_error())

    def read(self, target: str) -> tuple[str, bytes] | None:
        pointer = ctypes.POINTER(_CREDENTIALW)()
        if not self._cred_read(target, CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
            error = ctypes.get_last_error()
            if error == ERROR_NOT_FOUND:
                return None
            raise ctypes.WinError(error)
        try:
            credential = pointer.contents
            return (
                credential.UserName or "",
                ctypes.string_at(
                    credential.CredentialBlob, credential.CredentialBlobSize
                ),
            )
        finally:
            self._cred_free(pointer)

    def delete(self, target: str) -> None:
        if self._cred_delete(target, CRED_TYPE_GENERIC, 0):
            return
        error = ctypes.get_last_error()
        if error != ERROR_NOT_FOUND:
            raise ctypes.WinError(error)


class WindowsDeviceCredentialStore:
    """Store exactly one active Vemal's Ears device credential."""

    target = CREDENTIAL_TARGET
    product_name = "Vemal's Ears"

    def __init__(self, api: _CredentialApi | None = None) -> None:
        self._api = api if api is not None else _CtypesCredentialApi()

    def save(self, credentials: DeviceCredentials) -> None:
        checked = validate_device_credentials(
            credentials.server_url,
            credentials.device_id,
            credentials.device_token,
            credentials.guild_id,
            credentials.guild_name,
        )
        payload = json.dumps(
            {
                "version": 1,
                "server_url": checked.server_url,
                "device_token": checked.device_token,
                "guild_id": checked.guild_id,
                "guild_name": checked.guild_name,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload) > MAX_CREDENTIAL_BLOB_BYTES:
            raise DeviceCredentialValidationError("device credential is too large")
        try:
            self._api.write(self.target, checked.device_id, payload)
        except OSError:
            raise DeviceCredentialStoreError(
                "Windows could not save the " + self.product_name
                + " device in Credential Manager."
            ) from None

    def _read(self) -> tuple[str, bytes] | None:
        try:
            return self._api.read(self.target)
        except OSError:
            raise DeviceCredentialStoreError(
                "Windows could not read the saved " + self.product_name + " device."
            ) from None

    def load(self) -> DeviceCredentials | None:
        stored = self._read()
        if stored is None:
            return None
        return self._decode(stored)

    def _decode(self, stored: tuple[str, bytes]) -> DeviceCredentials:
        device_id, raw_payload = stored
        try:
            if len(raw_payload) > MAX_CREDENTIAL_BLOB_BYTES:
                raise DeviceCredentialValidationError("credential payload is too large")
            payload = json.loads(raw_payload.decode("utf-8"))
            if not isinstance(payload, dict) or set(payload) != {
                "version",
                "server_url",
                "device_token",
                "guild_id",
                "guild_name",
            }:
                raise DeviceCredentialValidationError("unexpected credential fields")
            if payload.get("version") != 1:
                raise DeviceCredentialValidationError("unsupported credential version")
            return validate_device_credentials(
                payload.get("server_url"),
                device_id,
                payload.get("device_token"),
                payload.get("guild_id"),
                payload.get("guild_name"),
            )
        except (
            UnicodeError,
            json.JSONDecodeError,
            DeviceCredentialValidationError,
        ):
            raise DeviceCredentialStoreError(
                "The saved " + self.product_name
                + " device is invalid. Pair this computer again."
            ) from None

    def delete(self) -> None:
        try:
            self._api.delete(self.target)
        except OSError:
            raise DeviceCredentialStoreError(
                "Windows could not remove the saved " + self.product_name + " device."
            ) from None


class WindowsDowntimeCredentialStore(WindowsDeviceCredentialStore):
    """Keep Downtime pairing independent from the caption application's slot.

    A first launch copies an existing combined-app pairing once. The server
    supports the same device token for HTTP jobs and websocket captions, so
    this does not consume another invitation or interrupt VE. Subsequent
    pairing and unlinking affect only Downtime. An unlink keeps a non-secret
    marker so legacy credentials cannot silently restore the connection.
    """

    target = DOWNTIME_CREDENTIAL_TARGET
    product_name = "Downtime"

    def load(self) -> DeviceCredentials | None:
        stored = self._read()
        if stored == _UNPAIRED_DEVICE:
            return None
        if stored is not None:
            return self._decode(stored)
        legacy_store = WindowsDeviceCredentialStore(self._api)
        legacy_payload = legacy_store._read()
        legacy = None
        if legacy_payload is not None:
            try:
                legacy = legacy_store._decode(legacy_payload)
            except DeviceCredentialStoreError:
                # Migration is optional. A malformed legacy token cannot be
                # imported, but it must not prevent a fresh Downtime pairing.
                # Preserve VE's slot so that app can handle its own recovery.
                pass
        if legacy is not None:
            self.save(legacy)
        else:
            self.delete()
        return legacy

    def delete(self) -> None:
        try:
            self._api.write(self.target, *_UNPAIRED_DEVICE)
        except OSError:
            raise DeviceCredentialStoreError(
                "Windows could not remove the saved Downtime device."
            ) from None
