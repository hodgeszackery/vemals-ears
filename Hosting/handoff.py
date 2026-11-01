"""Authenticated, bounded worker handoffs. No relay database or session export.

Version 1 is a fixed binary header, AES-256-CBC/PKCS7 ciphertext, and a
32-byte HMAC-SHA256 over header+ciphertext. PBKDF2-HMAC-SHA256 derives separate
encryption and authentication keys. Authenticate before invoking AES decryption.
The only plaintext files this module creates are the protected imported state.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import struct
import subprocess
import tempfile
from urllib.parse import urlsplit

from GuildHub import relay_env
from Hosting import setup


KIND = "vemals-ears-worker-handoff"
MAGIC = b"VE-HANDOFF\x00\x01"
ITERATIONS = 600_000
HEADER = struct.Struct(">12sI32s16sI")
MAX_LEDGER_BYTES = 1_048_576
MAX_ENTRIES = 10_000
MAX_PAYLOAD_BYTES = 2 * MAX_LEDGER_BYTES
MAX_BUNDLE_BYTES = HEADER.size + MAX_PAYLOAD_BYTES + 16 + 32
PAYLOAD_KEYS = frozenset({
    "schema", "kind", "guild_id", "guild_name", "application_id",
    "public_server_url", "public_http_url", "client_download_url",
    "bot_token", "publisher_token", "consent", "mapping",
})
CLASSES = frozenset({"death-knight", "demon-hunter", "druid", "evoker", "hunter",
                     "mage", "monk", "paladin", "priest", "rogue", "shaman",
                     "warlock", "warrior"})
ROLES = frozenset({"damage", "healer", "tank"})
OPEN_ERROR = "The handoff could not be opened. Check its password and file."
DATA_ERROR = "The worker handoff data is invalid or unsupported."
HELPER = Path(__file__).with_name("Handoff-Crypto.ps1")


class HandoffError(ValueError):
    """A safe-to-display error which never includes credentials or input data."""


def _require(condition: bool) -> None:
    if not condition:
        raise HandoffError(DATA_ERROR)


def _json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=True, allow_nan=False,
                      sort_keys=True, separators=(",", ":")).encode("ascii")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise HandoffError(DATA_ERROR)
        result[key] = value
    return result


def _timestamp(value) -> None:
    _require(isinstance(value, str) and len(value) <= 40)
    _require(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})", value) is not None)
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HandoffError(DATA_ERROR) from None


def _public_url(value, scheme: str, *, origin: bool):
    _require(isinstance(value, str) and 1 <= len(value) <= 2048)
    _require(all(33 <= ord(char) <= 126 for char in value) and "\\" not in value and "#" not in value)
    try:
        parsed = urlsplit(value)
        _require(parsed.scheme == scheme and parsed.hostname is not None
                 and parsed.username is None and parsed.password is None and not parsed.fragment)
        setup.hostname(parsed.hostname)
        port = parsed.port or 443
        _require(parsed.port != 0)
        _require(parsed.netloc.lower() == parsed.hostname + (":" + str(parsed.port) if parsed.port is not None else ""))
        if origin:
            _require(parsed.path == "" and "?" not in value)
        return parsed.hostname, port
    except (ValueError, setup.SetupError):
        raise HandoffError(DATA_ERROR) from None


def _ledger(value, guild_id: str, *, mapping: bool) -> None:
    _require(type(value) is dict and set(value) == {"version", "updatedAt", "entries"})
    _require(type(value["version"]) is int and value["version"] == 1)
    _timestamp(value["updatedAt"])
    entries = value["entries"]
    _require(type(entries) is dict and len(entries) <= MAX_ENTRIES)
    for key, entry in entries.items():
        _require(isinstance(key, str) and re.fullmatch(re.escape(guild_id) + r":[0-9]{1,20}", key) is not None)
        if not mapping:
            _require(type(entry) is str and entry in {"granted", "declined"})
            continue
        _require(type(entry) is dict and set(entry) == {"characterName", "realm", "wowClass", "role", "mappedAt"})
        name = entry["characterName"]
        _require(isinstance(name, str) and 2 <= len(name) <= 24 and name[0].isalpha()
                 and all(char.isalpha() or char in "'-" for char in name))
        realm = entry["realm"]
        _require(realm is None or (isinstance(realm, str) and 1 <= len(realm) <= 32
                                  and realm[0].isalnum()
                                  and all(char.isalnum() or char in " '-" for char in realm)))
        _require(type(entry["wowClass"]) is str and entry["wowClass"] in CLASSES)
        _require(type(entry["role"]) is str and entry["role"] in ROLES)
        if entry["mappedAt"] is not None:
            _timestamp(entry["mappedAt"])
    _require(len(_json_bytes(value)) <= MAX_LEDGER_BYTES)


def validate_payload(payload: dict) -> dict:
    """Validate every field and return a detached copy; no unknown keys survive."""
    _require(type(payload) is dict and set(payload) == PAYLOAD_KEYS)
    _require(type(payload["schema"]) is int and payload["schema"] == 1 and payload["kind"] == KIND)
    for field in ("guild_id", "application_id"):
        _require(isinstance(payload[field], str) and re.fullmatch(r"[0-9]{17,20}", payload[field]) is not None)
    name = payload["guild_name"]
    _require(isinstance(name, str) and 1 <= len(name) <= 80 and bool(name.strip())
             and all(32 <= ord(char) <= 126 for char in name))
    origin = _public_url(payload["public_server_url"], "wss", origin=True)
    _require(origin == _public_url(payload["public_http_url"], "https", origin=True))
    _public_url(payload["client_download_url"], "https", origin=False)
    for field in ("bot_token", "publisher_token"):
        value = payload[field]
        _require(isinstance(value, str) and 1 <= len(value) <= 512
                 and all(33 <= ord(char) <= 126 for char in value))
    # Match GuildHub.host's stable viewer-identity key input exactly. Import
    # must not succeed with a publisher key the worker will later refuse.
    _require(re.fullmatch(r"[A-Za-z0-9_-]{32,192}", payload["publisher_token"]) is not None)
    _ledger(payload["consent"], payload["guild_id"], mapping=False)
    _ledger(payload["mapping"], payload["guild_id"], mapping=True)
    serialized = _json_bytes(payload)
    _require(len(serialized) <= MAX_PAYLOAD_BYTES)
    return json.loads(serialized)


def _password(value: str) -> bytes:
    if (not isinstance(value, str) or not 12 <= len(value) <= 256
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise HandoffError("Use a handoff password containing 12 to 256 characters.")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        raise HandoffError("Use a handoff password containing 12 to 256 characters.") from None
    if len(encoded) > 1024:
        raise HandoffError("Use a handoff password containing 12 to 256 characters.")
    return encoded


def validate_password(password: str) -> None:
    """Validate before a source worker is stopped; never expose derived bytes."""
    _password(password)


def _aes(payload: bytes, key: bytes, iv: bytes, *, decrypt: bool) -> bytes:
    """Call only Windows' AES implementation; all sensitive bytes use pipes."""
    if os.name != "nt":
        raise HandoffError("Worker handoff encryption requires Windows.")
    if len(key) != 32 or len(iv) != 16 or len(payload) > MAX_PAYLOAD_BYTES + 16:
        raise HandoffError(DATA_ERROR)
    powershell = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    request = bytes((1, 2 if decrypt else 1)) + key + iv + struct.pack("<I", len(payload)) + payload
    try:
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(HELPER)],
            input=request, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        raise HandoffError("Windows could not complete handoff encryption.") from None
    expected = (len(payload) // 16 + 1) * 16
    if result.returncode or (not decrypt and len(result.stdout) != expected) or (decrypt and not 0 <= len(result.stdout) < len(payload)):
        raise HandoffError("Windows could not complete handoff encryption.")
    return result.stdout


def encrypt_bundle(payload: dict, password: str) -> bytes:
    plaintext = _json_bytes(validate_payload(payload))
    password_bytes = _password(password)
    salt, iv = secrets.token_bytes(32), secrets.token_bytes(16)
    keys = hashlib.pbkdf2_hmac("sha256", password_bytes, salt, ITERATIONS, dklen=64)
    ciphertext = _aes(plaintext, keys[:32], iv, decrypt=False)
    authenticated = HEADER.pack(MAGIC, ITERATIONS, salt, iv, len(ciphertext)) + ciphertext
    return authenticated + hmac.digest(keys[32:], authenticated, "sha256")


def decrypt_bundle(data: bytes, password: str) -> dict:
    """Reject malformed/wrong-password/tampered input before decrypting anything."""
    try:
        _require(type(data) is bytes and HEADER.size + 48 <= len(data) <= MAX_BUNDLE_BYTES)
        magic, iterations, salt, iv, length = HEADER.unpack_from(data)
        _require(magic == MAGIC and iterations == ITERATIONS and length >= 16
                 and length % 16 == 0 and length <= MAX_PAYLOAD_BYTES + 16
                 and len(data) == HEADER.size + length + 32)
        keys = hashlib.pbkdf2_hmac("sha256", _password(password), salt, ITERATIONS, dklen=64)
        _require(hmac.compare_digest(hmac.digest(keys[32:], data[:-32], "sha256"), data[-32:]))
        plaintext = _aes(data[HEADER.size:-32], keys[:32], iv, decrypt=True)
        _require(len(plaintext) <= MAX_PAYLOAD_BYTES)
        payload = json.loads(plaintext.decode("ascii"), object_pairs_hook=_pairs)
        return validate_payload(payload)
    except (HandoffError, ValueError, TypeError, UnicodeError, RecursionError, struct.error):
        raise HandoffError(OPEN_ERROR) from None


def _safe_path(value: Path) -> Path:
    path = Path(value)
    if (not path.is_absolute() or path == Path(path.anchor)
            or any(ord(char) < 32 or ord(char) > 126 for char in str(path))):
        raise HandoffError("Choose an absolute, dedicated handoff path using printable ASCII characters.")
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise HandoffError("Handoff paths cannot traverse a link or reparse point.")
    return path.resolve()


def _rename_new(source: Path, target: Path) -> None:
    # Windows rename fails atomically if the destination exists, including an
    # empty directory. Both paths are siblings on the same volume.
    if os.name != "nt":
        raise HandoffError("Worker handoff file operations require Windows.")
    source.rename(target)


def _write_private(path: Path, content: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def write_bundle(path: Path, payload: dict, password: str) -> Path:
    """Publish one encrypted file, without replacing it or changing its parent ACL."""
    try:
        target = _safe_path(path)
        if target.exists() or not target.parent.is_dir():
            raise HandoffError("Choose a new handoff filename in an existing directory.")
        encrypted = encrypt_bundle(payload, password)
        temporary_directory = tempfile.TemporaryDirectory(prefix=".VemalsEarsHandoff-", dir=target.parent)
        published = False
        try:
            staging = Path(temporary_directory.name).resolve()
            setup.protect_directory(staging)
            temporary = staging / "handoff.vehost"
            _write_private(temporary, encrypted)
            # Copy only inherited private ACEs to explicit ACEs before moving
            # the encrypted file out of its protected temporary directory.
            result = subprocess.run(["icacls.exe", str(temporary), "/inheritance:d"],
                                    capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:
                raise HandoffError("Could not protect the handoff file.")
            _rename_new(temporary, target)
            published = True
        finally:
            try:
                temporary_directory.cleanup()
            except OSError:
                # Once the bundle is published, failure to remove its empty
                # private staging directory must never signal export failure:
                # the source controller must keep the original worker stopped.
                if not published:
                    raise
        return target
    except (OSError, subprocess.SubprocessError, setup.SetupError):
        raise HandoffError("Could not save the protected handoff file. Existing files were not replaced.") from None


def read_bundle(path: Path, password: str) -> dict:
    try:
        target = _safe_path(path)
        info = target.lstat()
        _require(stat.S_ISREG(info.st_mode) and info.st_size <= MAX_BUNDLE_BYTES)
        with target.open("rb") as stream:
            _require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode))
            data = stream.read(MAX_BUNDLE_BYTES + 1)
        return decrypt_bundle(data, password)
    except (HandoffError, OSError):
        raise HandoffError(OPEN_ERROR) from None


def import_worker(payload: dict, state: Path) -> dict[str, str]:
    """Create one fresh protected worker state; never start or modify a service."""
    values_in = validate_payload(payload)
    try:
        if os.name != "nt":
            raise HandoffError("Worker handoff import requires Windows.")
        state = setup.state_path(_safe_path(state))
        if state.exists():
            raise HandoffError("Choose a new worker state directory. Existing host state is never overwritten.")
        values = {
            "VEMALS_EARS_HOST_MODE": "remote-worker",
            "VEMALS_EARS_GUILD_ID": values_in["guild_id"],
            "VEMALS_EARS_GUILD_NAME": values_in["guild_name"],
            "VEMALS_EARS_PUBLIC_SERVER_URL": values_in["public_server_url"],
            "VEMALS_EARS_PUBLIC_HTTP_URL": values_in["public_http_url"],
            "VEMALS_EARS_CLIENT_DOWNLOAD_URL": values_in["client_download_url"],
            "VEMALS_EARS_PUBLISHER_TOKEN_FILE": (state / "secrets/publisher-token").as_posix(),
            "VEMALS_EARS_DISCORD_APPLICATION_ID_FILE": (state / "secrets/discord-application-id").as_posix(),
            "VEMALS_EARS_DISCORD_BOT_TOKEN_FILE": (state / "secrets/discord-bot-token").as_posix(),
        }
        state.parent.mkdir(parents=True, exist_ok=True)
        temporary_directory = tempfile.TemporaryDirectory(prefix=".VemalsEarsWorker-", dir=state.parent)
        published = False
        try:
            staging = Path(temporary_directory.name).resolve()
            setup.protect_directory(staging)
            for relative in ("secrets", "consent", "logs", "profile/WoWVoiceCaptions"):
                (staging / relative).mkdir(parents=True, exist_ok=True)
            for name, field in (("publisher-token", "publisher_token"), ("discord-application-id", "application_id"),
                                ("discord-bot-token", "bot_token")):
                _write_private(staging / "secrets" / name, (values_in[field] + "\n").encode("ascii"))
            for name in ("consent", "mapping"):
                _write_private(staging / "consent" / (name + ".json"), _json_bytes(values_in[name]) + b"\n")
            _write_private(staging / "host-settings.json", _json_bytes({
                "schema": 1, "mode": "remote-worker", "takeover_confirmed": False,
            }) + b"\n")
            relay_env.save(values, staging / "relay.env")
            _rename_new(staging, state)
            published = True
        finally:
            try:
                temporary_directory.cleanup()
            except OSError:
                if not published:
                    raise
        return values
    except (OSError, subprocess.SubprocessError, setup.SetupError):
        raise HandoffError("Could not import the protected worker state. Existing host state was not replaced.") from None
