"""Private Discord credential storage for Voice Captions.

The bot token and application ID are kept in one Windows Generic Credential.
This module deliberately has no file-backed fallback: if Credential Manager is
unavailable, setup fails closed instead of writing a plaintext secret.
"""

from __future__ import annotations

import ctypes
import os
import re
from ctypes import wintypes
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlencode

CREDENTIAL_TARGET = "WoWVoiceCaptions.Discord"
CREDENTIAL_COMMENT = "Vemal's Ears Discord bot"

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168
MAX_CREDENTIAL_BLOB_BYTES = 512

DISCORD_AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
PERMISSION_VIEW_CHANNEL = 1 << 10
PERMISSION_SEND_MESSAGES = 1 << 11
PERMISSION_CONNECT = 1 << 20
BOT_INSTALL_PERMISSIONS = (
    PERMISSION_VIEW_CHANNEL | PERMISSION_SEND_MESSAGES | PERMISSION_CONNECT
)
BOT_INSTALL_PERMISSION_NAMES = ("View Channel", "Send Messages", "Connect")

_APPLICATION_ID = re.compile(r"^[0-9]{17,20}$")


class CredentialValidationError(ValueError):
    """A value is not safe to store or use in an install link."""


class CredentialStoreError(RuntimeError):
    """Credential Manager could not complete the requested operation."""


@dataclass(frozen=True, repr=False)
class DiscordCredentials:
    application_id: str
    bot_token: str

    def __repr__(self) -> str:
        return "DiscordCredentials(application_id=<redacted>, bot_token=<redacted>)"

    __str__ = __repr__


class CredentialStore(Protocol):
    def save(self, credentials: DiscordCredentials) -> None: ...

    def load(self) -> DiscordCredentials | None: ...

    def delete(self) -> None: ...


class _CredentialApi(Protocol):
    def write(self, target: str, username: str, secret: bytes) -> None: ...

    def read(self, target: str) -> tuple[str, bytes] | None: ...

    def delete(self, target: str) -> None: ...


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
    """Small ctypes wrapper around the Windows Credential Manager API."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise CredentialStoreError(
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
        credential.Comment = CREDENTIAL_COMMENT
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
            username = credential.UserName or ""
            secret = ctypes.string_at(
                credential.CredentialBlob, credential.CredentialBlobSize
            )
            return username, secret
        finally:
            self._cred_free(pointer)

    def delete(self, target: str) -> None:
        if self._cred_delete(target, CRED_TYPE_GENERIC, 0):
            return
        error = ctypes.get_last_error()
        if error != ERROR_NOT_FOUND:
            raise ctypes.WinError(error)


def validate_application_id(application_id: str) -> str:
    value = application_id.strip() if isinstance(application_id, str) else ""
    if not _APPLICATION_ID.fullmatch(value):
        raise CredentialValidationError(
            "Application ID must be the 17 to 20 digit number from Discord."
        )
    return value


def validate_credentials(application_id: str, bot_token: str) -> DiscordCredentials:
    application_id = validate_application_id(application_id)
    token = bot_token.strip() if isinstance(bot_token, str) else ""
    if len(token) < 20:
        raise CredentialValidationError("Bot Token is missing or incomplete.")
    if any(character.isspace() for character in token):
        raise CredentialValidationError("Bot Token cannot contain spaces or line breaks.")
    if len(token.encode("utf-8")) > MAX_CREDENTIAL_BLOB_BYTES:
        raise CredentialValidationError("Bot Token is longer than Discord allows.")
    return DiscordCredentials(application_id=application_id, bot_token=token)


def build_bot_install_url(application_id: str) -> str:
    """Build an install URL with the smallest permissions this service uses."""
    application_id = validate_application_id(application_id)
    query = urlencode(
        (
            ("client_id", application_id),
            ("permissions", str(BOT_INSTALL_PERMISSIONS)),
            ("scope", "bot applications.commands"),
        )
    )
    return f"{DISCORD_AUTHORIZE_URL}?{query}"


class WindowsCredentialStore:
    """Store one Discord application ID/token pair in Credential Manager."""

    def __init__(self, api: _CredentialApi | None = None) -> None:
        self._api = api if api is not None else _CtypesCredentialApi()

    def save(self, credentials: DiscordCredentials) -> None:
        checked = validate_credentials(
            credentials.application_id, credentials.bot_token
        )
        try:
            self._api.write(
                CREDENTIAL_TARGET,
                checked.application_id,
                checked.bot_token.encode("utf-8"),
            )
        except OSError:
            raise CredentialStoreError(
                "Windows could not save the Discord details in Credential Manager."
            ) from None

    def load(self) -> DiscordCredentials | None:
        try:
            stored = self._api.read(CREDENTIAL_TARGET)
        except OSError:
            raise CredentialStoreError(
                "Windows could not read the saved Discord details."
            ) from None
        if stored is None:
            return None
        application_id, raw_token = stored
        try:
            return validate_credentials(application_id, raw_token.decode("utf-8"))
        except (UnicodeError, CredentialValidationError):
            raise CredentialStoreError(
                "The saved Discord details are invalid. Replace them in setup."
            ) from None

    def delete(self) -> None:
        try:
            self._api.delete(CREDENTIAL_TARGET)
        except OSError:
            raise CredentialStoreError(
                "Windows could not remove the saved Discord details."
            ) from None
