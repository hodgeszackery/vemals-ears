"""Host-to-host transfer core: bundle, database snapshot, staged import, records.

A transfer moves a whole standalone host (bot identity, publisher token, relay
database with its pepper, pairings, consent, mappings, tunnel token) so the old
PC can be switched off. Session resume, logs, the speech runtime, audio and
transcripts are never part of it: the last two are not stored anywhere.

The bundle is a binary length-prefixed container (JSON manifest + raw blobs),
AES-256-CBC/PKCS7 encrypted with a 32-byte HMAC-SHA256 over header+ciphertext,
both keys taken from the PAKE session (Hosting.transfer_pake). Authenticate
before invoking AES decryption. The only plaintext files this module creates
are the protected staging state and a database snapshot that is erased again.

Nothing here starts, stops or installs a service, and nothing talks to the
network; the sender and receiver state machines drive these pieces.
"""

from __future__ import annotations

import base64
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import functools
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import stat
import struct
import subprocess
import tempfile
import threading
import time

from GuildHub import relay_env
from GuildHub.release_pointer import MAX_POINTER_BYTES, POINTER_FILENAME
from Hosting import handoff, service, setup, transfer_pake
from Hosting.version import HOST_VERSION


KIND = "vemals-ears-host-transfer"
MAGIC = b"VE-TRANSFER\x01"
HEADER = struct.Struct(">12s16s16sQ")   # magic, transfer id, IV, ciphertext length
CONTAINER_MAGIC = b"VE-BUNDLE\x00\x00\x01"
MAX_DATABASE_BYTES = 16 * 1_048_576
MAX_MANIFEST_BYTES = 65_536
MAX_PLAIN_BYTES = handoff.MAX_TRANSFER_PAYLOAD_BYTES
MAX_BUNDLE_BYTES = HEADER.size + MAX_PLAIN_BYTES + 16 + 32
MAX_ARTIFACTS = 8
MAX_ARTIFACT_BYTES = 512 * 1_048_576
# The relay refuses a larger WebSocket frame (GuildHub/protocol.py).
FRAME_LIMIT = 16_384
FRAME_MAGIC = b"VETF"
FRAME_HEADER = struct.Struct(">4sB16sI")  # magic, type, transfer id, sequence
FRAME_DATA, FRAME_END = 1, 2
FRAME_END_BODY = struct.Struct(">Q32s")  # total length, SHA-256 of the bundle
ACTIVATION_PENDING = "pending"
ACTIVATION_ACTIVE = "active"

MANIFEST_KEYS = frozenset({
    "schema", "kind", "transfer_id", "release_commitment", "host_version",
    "settings", "tunnel_enabled", "blobs", "artifacts",
})
DATABASE = "relay/guild-hub.sqlite3"
POINTER = "downloads/" + POINTER_FILENAME
TUNNEL_TOKEN = "secrets/cloudflare-tunnel-token"
# name: (required, largest accepted size). The order is the container order,
# and on import the write order: the release pointer is always written last.
BLOBS = {
    "secrets/discord-bot-token": (True, 512),
    "secrets/discord-application-id": (True, 20),
    "secrets/publisher-token": (True, 192),
    "secrets/pepper": (True, 512),
    TUNNEL_TOKEN: (False, 16_384),
    DATABASE: (True, MAX_DATABASE_BYTES),
    "consent/consent.json": (True, handoff.MAX_LEDGER_BYTES),
    "consent/mapping.json": (True, handoff.MAX_LEDGER_BYTES),
    POINTER: (False, MAX_POINTER_BYTES),
}
# Values copied into the regenerated relay.env. Every path in that file is
# rebuilt for the destination and BIND is always 127.0.0.1.
REQUIRED_SETTINGS = (
    "VEMALS_EARS_GUILD_ID", "VEMALS_EARS_GUILD_NAME", "VEMALS_EARS_PORT",
    "VEMALS_EARS_PUBLIC_SERVER_URL", "VEMALS_EARS_PUBLIC_HTTP_URL",
    "VEMALS_EARS_CLIENT_DOWNLOAD_URL",
)
NUMERIC_SETTINGS = (
    "VEMALS_EARS_CONNECTION_LIMIT", "VEMALS_EARS_REVALIDATION_SECONDS",
    "VEMALS_EARS_HOUSEKEEPING_SECONDS", "VEMALS_EARS_UNUSED_DEVICE_SECONDS",
    "VEMALS_EARS_IDLE_DEVICE_SECONDS", "VEMALS_EARS_REVOKED_RETENTION_SECONDS",
)
PORTABLE_SETTINGS = frozenset(REQUIRED_SETTINGS + NUMERIC_SETTINGS + ("VEMALS_EARS_CLIENT_VERSION",))

SENDER, RECEIVER = "sender", "receiver"
SENDER_STATES = ("offered", "sending", "released", "moved", "cancelled")
RECEIVER_STATES = ("pending", "active", "discarded")
# A released host never goes back: the only way on from "released" is "moved".
_TRANSITIONS = {
    SENDER: {"offered": {"sending", "cancelled"}, "sending": {"released", "cancelled"},
             "released": {"moved"}},
    RECEIVER: {"pending": {"active", "discarded"}},
}
_TERMINAL = frozenset({"moved", "cancelled", "active", "discarded"})

DATA_ERROR = "The host transfer data is invalid or unsupported."
ARCHIVE_ERROR = "This PC holds the locked archive of a host that moved. Erase the archive first."
SNAPSHOT_PREFIX = ".VemalsEarsSnapshot-"
SNAPSHOT_LEFTOVER_ERROR = ("A temporary copy of the relay database could not be erased. Nothing was moved. "
                           "Erase it before trying again.")
OPEN_ERROR = "The host transfer could not be opened. Start the transfer again."
FRAME_ERROR = "The other PC sent an invalid transfer message. Start the transfer again."
RECORD_ERROR = "The transfer record cannot be read. Nothing was changed."
# Never tells the operator to discard: when a release code is being typed,
# the copy on this PC may be the only one left.
WRONG_RELEASE_CODE = ("That release code does not match this transfer. Read it again on the old PC, on "
                      "This host has moved, and type it exactly. Do not discard the copy on this PC.")
_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_ARTIFACT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class TransferError(ValueError):
    """A safe-to-display error which never includes credentials or input data."""


@dataclass(frozen=True, repr=False)
class Bundle:
    """A validated manifest and its blobs; the repr never shows the blobs."""

    manifest: dict
    blobs: dict


def _require(condition: bool) -> None:
    if not condition:
        raise TransferError(DATA_ERROR)


def new_transfer_id() -> str:
    return secrets.token_hex(16)


def _ascii(data: bytes, pattern: str) -> str:
    try:
        text = data.decode("ascii")
    except UnicodeError:
        raise TransferError(DATA_ERROR) from None
    _require(re.fullmatch(pattern, text) is not None)
    return text


def _json(data: bytes):
    try:
        return json.loads(data.decode("ascii"), object_pairs_hook=handoff._pairs)
    except (ValueError, UnicodeError, RecursionError):
        raise TransferError(DATA_ERROR) from None


def _settings(value) -> dict:
    _require(type(value) is dict and set(value) <= PORTABLE_SETTINGS
             and all(key in value for key in REQUIRED_SETTINGS)
             and all(type(item) is str for item in value.values()))
    _require(re.fullmatch(r"[0-9]{17,20}", value["VEMALS_EARS_GUILD_ID"]) is not None)
    name = value["VEMALS_EARS_GUILD_NAME"]
    _require(1 <= len(name) <= 80 and bool(name.strip()) and all(32 <= ord(char) <= 126 for char in name))
    _require(re.fullmatch(r"[1-9][0-9]{0,4}", value["VEMALS_EARS_PORT"]) is not None
             and int(value["VEMALS_EARS_PORT"]) <= 65535)
    try:
        origin = handoff._public_url(value["VEMALS_EARS_PUBLIC_SERVER_URL"], "wss", origin=True)
        _require(origin == handoff._public_url(value["VEMALS_EARS_PUBLIC_HTTP_URL"], "https", origin=True))
        handoff._public_url(value["VEMALS_EARS_CLIENT_DOWNLOAD_URL"], "https", origin=False)
    except handoff.HandoffError:
        raise TransferError(DATA_ERROR) from None
    for key in NUMERIC_SETTINGS:
        _require(key not in value or re.fullmatch(r"[0-9]{1,10}", value[key]) is not None)
    _require(re.fullmatch(r"[0-9A-Za-z.+-]{0,40}", value.get("VEMALS_EARS_CLIENT_VERSION", "")) is not None)
    return dict(value)


def _artifact(value) -> dict:
    _require(type(value) is dict and set(value) == {"name", "size", "sha256"})
    _require(type(value["name"]) is str and _ARTIFACT_NAME.fullmatch(value["name"]) is not None
             and value["name"].lower() != POINTER_FILENAME and not value["name"].endswith("."))
    _require(type(value["size"]) is int and 0 < value["size"] <= MAX_ARTIFACT_BYTES)
    _require(type(value["sha256"]) is str and _HEX64.fullmatch(value["sha256"]) is not None)
    return dict(value)


def _ledger(data: bytes, guild_id: str, *, mapping: bool) -> None:
    value = _json(data)
    try:
        handoff._ledger(value, guild_id, mapping=mapping)
    except handoff.HandoffError:
        raise TransferError(DATA_ERROR) from None
    # Only the canonical form travels, so the import writes exactly these bytes.
    _require(handoff._json_bytes(value) == data)


def _blob(name: str, data: bytes, guild_id: str) -> None:
    if name == "secrets/discord-bot-token":
        _ascii(data, r"[\x21-\x7e]{1,512}")
    elif name == "secrets/discord-application-id":
        _ascii(data, r"[0-9]{17,20}")
    elif name == "secrets/publisher-token":
        # The same stable viewer-identity key input GuildHub.host accepts.
        _ascii(data, r"[A-Za-z0-9_-]{32,192}")
    elif name == "secrets/pepper":
        _ascii(data, r"[\x21-\x7e]{32,512}")
    elif name == TUNNEL_TOKEN:
        _ascii(data, r"[A-Za-z0-9_+=/.-]{32,16384}")
    elif name == DATABASE:
        _require(data[:16] == b"SQLite format 3\x00")
    elif name == POINTER:
        _require(type(_json(data)) is dict)
    else:
        _ledger(data, guild_id, mapping=name == "consent/mapping.json")


def validate_bundle(bundle: Bundle) -> Bundle:
    """Validate every field and blob and return a detached copy; no unknown key or name survives."""
    _require(isinstance(bundle, Bundle))
    manifest, blobs = bundle.manifest, bundle.blobs
    _require(type(manifest) is dict and set(manifest) == MANIFEST_KEYS and type(blobs) is dict)
    _require(type(manifest["schema"]) is int and manifest["schema"] == 1 and manifest["kind"] == KIND)
    _require(type(manifest["transfer_id"]) is str and _HEX32.fullmatch(manifest["transfer_id"]) is not None)
    _require(type(manifest["release_commitment"]) is str
             and _HEX64.fullmatch(manifest["release_commitment"]) is not None)
    _require(type(manifest["host_version"]) is str
             and re.fullmatch(r"[0-9A-Za-z.+-]{1,40}", manifest["host_version"]) is not None)
    settings = _settings(manifest["settings"])
    _require(type(manifest["tunnel_enabled"]) is bool)
    listed = manifest["blobs"]
    _require(type(listed) is list and len(listed) <= len(BLOBS))
    names = []
    for entry in listed:
        _require(type(entry) is dict and set(entry) == {"name", "size", "sha256"}
                 and type(entry["name"]) is str and entry["name"] in BLOBS)
        data = blobs.get(entry["name"])
        _require(type(data) is bytes and type(entry["size"]) is int
                 and entry["size"] == len(data) and 0 < len(data) <= BLOBS[entry["name"]][1])
        _require(type(entry["sha256"]) is str and _HEX64.fullmatch(entry["sha256"]) is not None
                 and hmac.compare_digest(entry["sha256"], hashlib.sha256(data).hexdigest()))
        _blob(entry["name"], data, settings["VEMALS_EARS_GUILD_ID"])
        names.append(entry["name"])
    # Fixed order, no repeats, nothing missing and no blob the manifest omits.
    _require(names == [name for name in BLOBS if name in names] and set(names) == set(blobs)
             and all(name in names for name, (required, _) in BLOBS.items() if required))
    _require(not manifest["tunnel_enabled"] or TUNNEL_TOKEN in names)
    artifacts = manifest["artifacts"]
    _require(type(artifacts) is list and len(artifacts) <= MAX_ARTIFACTS)
    artifacts = [_artifact(entry) for entry in artifacts]
    _require(len({entry["name"].lower() for entry in artifacts}) == len(artifacts))
    detached = json.loads(handoff._json_bytes({**manifest, "settings": settings, "artifacts": artifacts}))
    _require(len(handoff._json_bytes(detached)) <= MAX_MANIFEST_BYTES)
    return Bundle(detached, {name: blobs[name] for name in names})


def build_bundle(*, transfer_id: str, release_commitment: bytes, settings: dict,
                 tunnel_enabled: bool, blobs: dict, artifacts=()) -> Bundle:
    """Describe the blobs in their fixed order; the result is always validated."""
    _require(type(blobs) is dict and type(release_commitment) is bytes and len(release_commitment) == 32
             and all(type(data) is bytes for data in blobs.values()))
    return validate_bundle(Bundle({
        "schema": 1, "kind": KIND, "transfer_id": transfer_id,
        "release_commitment": release_commitment.hex(), "host_version": HOST_VERSION,
        "settings": settings, "tunnel_enabled": tunnel_enabled,
        "blobs": [{"name": name, "size": len(blobs[name]), "sha256": hashlib.sha256(blobs[name]).hexdigest()}
                  for name in BLOBS if name in blobs],
        "artifacts": list(artifacts),
    }, blobs))


def pack_bundle(bundle: Bundle) -> bytes:
    """magic, uint32 manifest length, manifest, then uint64 length + bytes per blob."""
    bundle = validate_bundle(bundle)
    manifest = handoff._json_bytes(bundle.manifest)
    parts = [CONTAINER_MAGIC, struct.pack(">I", len(manifest)), manifest]
    for entry in bundle.manifest["blobs"]:
        parts += [struct.pack(">Q", entry["size"]), bundle.blobs[entry["name"]]]
    packed = b"".join(parts)
    _require(len(packed) <= MAX_PLAIN_BYTES)
    return packed


def unpack_bundle(data: bytes) -> Bundle:
    try:
        _require(type(data) is bytes and len(CONTAINER_MAGIC) + 4 <= len(data) <= MAX_PLAIN_BYTES
                 and data.startswith(CONTAINER_MAGIC))
        offset = len(CONTAINER_MAGIC)
        (length,) = struct.unpack_from(">I", data, offset)
        offset += 4
        _require(length <= MAX_MANIFEST_BYTES and offset + length <= len(data))
        manifest = _json(data[offset:offset + length])
        offset += length
        _require(type(manifest) is dict and type(manifest.get("blobs")) is list
                 and len(manifest["blobs"]) <= len(BLOBS))
        blobs = {}
        for entry in manifest["blobs"]:
            _require(type(entry) is dict and type(entry.get("name")) is str and entry["name"] not in blobs)
            (length,) = struct.unpack_from(">Q", data, offset)
            offset += 8
            _require(length <= len(data) - offset)
            blobs[entry["name"]] = data[offset:offset + length]
            offset += length
        _require(offset == len(data))
        return validate_bundle(Bundle(manifest, blobs))
    except (TransferError, struct.error, TypeError):
        raise TransferError(DATA_ERROR) from None


def _raw_id(transfer_id: str) -> bytes:
    _require(type(transfer_id) is str and _HEX32.fullmatch(transfer_id) is not None)
    return bytes.fromhex(transfer_id)


def encrypt_bundle(bundle: Bundle, keys) -> bytes:
    """Seal for one transfer; keys is a transfer_pake.SessionKeys (bundle_enc, bundle_mac)."""
    plaintext = pack_bundle(bundle)
    iv = secrets.token_bytes(16)
    try:
        ciphertext = handoff.transfer_aes(plaintext, keys.bundle_enc, iv, decrypt=False)
    except handoff.HandoffError as error:
        raise TransferError(str(error)) from None
    authenticated = HEADER.pack(MAGIC, _raw_id(bundle.manifest["transfer_id"]), iv, len(ciphertext)) + ciphertext
    return authenticated + hmac.digest(keys.bundle_mac, authenticated, "sha256")


def decrypt_bundle(data: bytes, keys, transfer_id: str) -> Bundle:
    """Reject malformed/wrong-session/tampered input before decrypting anything."""
    try:
        _require(type(data) is bytes and HEADER.size + 48 <= len(data) <= MAX_BUNDLE_BYTES)
        magic, identifier, iv, length = HEADER.unpack_from(data)
        _require(magic == MAGIC and identifier == _raw_id(transfer_id) and length >= 16
                 and length % 16 == 0 and length <= MAX_PLAIN_BYTES + 16
                 and len(data) == HEADER.size + length + 32)
        _require(hmac.compare_digest(hmac.digest(keys.bundle_mac, data[:-32], "sha256"), data[-32:]))
        bundle = unpack_bundle(handoff.transfer_aes(data[HEADER.size:-32], keys.bundle_enc, iv, decrypt=True))
        _require(bundle.manifest["transfer_id"] == transfer_id)
        return bundle
    except (TransferError, handoff.HandoffError, ValueError, TypeError, struct.error):
        raise TransferError(OPEN_ERROR) from None


def _frame_mac(mac_key: bytes, body: bytes) -> bytes:
    # Bundle frames flow one way only (old host to new host), so this prefix
    # is all the separation they need. Control frames, when they are built on
    # the same key, must not reuse it: give them their own prefix plus a
    # direction and a sequence in the MACed body, or a key per direction, so
    # no frame can be reflected or replayed across the two streams.
    return hmac.digest(mac_key, b"VemalsEars host transfer v1 bundle frame\x00" + body, "sha256")


def split_frames(data: bytes, transfer_id: str, mac_key: bytes) -> list:
    """Cut an encrypted bundle into authenticated frames the relay will carry.

    Each frame is header + chunk + HMAC and at most FRAME_LIMIT bytes. Frames
    are binary only: on the transfer sockets a text frame is relay control,
    never peer data. The last frame carries no chunk: it is the authenticated
    "sending complete" with the total length and SHA-256.
    """
    _require(type(data) is bytes and 0 < len(data) <= MAX_BUNDLE_BYTES
             and type(mac_key) is bytes and len(mac_key) == 32)
    identifier = _raw_id(transfer_id)
    step = FRAME_LIMIT - FRAME_HEADER.size - 32
    bodies = [FRAME_HEADER.pack(FRAME_MAGIC, FRAME_DATA, identifier, sequence) + data[offset:offset + step]
              for sequence, offset in enumerate(range(0, len(data), step))]
    bodies.append(FRAME_HEADER.pack(FRAME_MAGIC, FRAME_END, identifier, len(bodies))
                  + FRAME_END_BODY.pack(len(data), hashlib.sha256(data).digest()))
    return [body + _frame_mac(mac_key, body) for body in bodies]


class FrameAssembler:
    """Receiver side of split_frames: strict order, every frame authenticated."""

    def __init__(self, transfer_id: str, mac_key: bytes) -> None:
        _require(type(mac_key) is bytes and len(mac_key) == 32)
        self._identifier = _raw_id(transfer_id)
        self._mac_key = mac_key
        self._chunks = []
        self._digest = hashlib.sha256()
        self._length = 0
        self._failed = False
        self.complete = False    # an authenticated "sending complete" was seen

    def feed(self, frame) -> bytes | None:
        """Return the whole encrypted bundle on the final frame, None before it."""
        try:
            if self._failed or self.complete:
                raise TransferError(FRAME_ERROR)
            if type(frame) is not bytes or not FRAME_HEADER.size + 32 <= len(frame) <= FRAME_LIMIT:
                raise TransferError(FRAME_ERROR)
            body, mac = frame[:-32], frame[-32:]
            if not hmac.compare_digest(_frame_mac(self._mac_key, body), mac):
                raise TransferError(FRAME_ERROR)
            magic, kind, identifier, sequence = FRAME_HEADER.unpack_from(body)
            chunk = body[FRAME_HEADER.size:]
            # A replayed, reordered or foreign frame ends the transfer.
            if magic != FRAME_MAGIC or identifier != self._identifier or sequence != len(self._chunks):
                raise TransferError(FRAME_ERROR)
            if kind == FRAME_DATA and chunk and self._length + len(chunk) <= MAX_BUNDLE_BYTES:
                self._chunks.append(chunk)
                self._digest.update(chunk)
                self._length += len(chunk)
                return None
            if kind != FRAME_END or len(chunk) != FRAME_END_BODY.size or not self._chunks:
                raise TransferError(FRAME_ERROR)
            length, digest = FRAME_END_BODY.unpack(chunk)
            if length != self._length or not hmac.compare_digest(digest, self._digest.digest()):
                raise TransferError(FRAME_ERROR)
            self.complete = True
            data, self._chunks = b"".join(self._chunks), []
            return data
        except (TransferError, ValueError, struct.error):
            self._failed = True
            self._chunks = []
            raise TransferError(FRAME_ERROR) from None


def _erase(path: Path) -> None:
    """Overwrite then remove one plaintext file; a link is removed, not followed."""
    info = path.lstat()
    if stat.S_ISREG(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400:
        with path.open("r+b") as stream:
            remaining = info.st_size
            while remaining > 0:
                step = min(remaining, 1_048_576)
                stream.write(bytes(step))
                remaining -= step
            stream.flush()
            os.fsync(stream.fileno())
    path.unlink()


def _erase_tree(folder: Path) -> None:
    for directory, _, files in os.walk(folder):
        for name in files:
            _erase(Path(directory) / name)
    shutil.rmtree(folder)


def _erase_snapshot(folder: Path) -> None:
    """Erase one plaintext snapshot folder; a failure names it, never hides it."""
    for attempt in (0, 1):
        try:
            if folder.is_symlink() or getattr(folder, "is_junction", lambda: False)():
                folder.unlink()
            elif folder.exists():
                _erase_tree(folder)
            return
        except OSError:
            if not attempt:
                time.sleep(0.25)    # a handle that was just closed may still be letting go
    raise TransferError(f"{SNAPSHOT_LEFTOVER_ERROR} Folder: {folder}")


def erase_leftover_snapshots(work_directory: Path) -> int:
    """Erase snapshot folders an earlier run could not remove; returns how many.

    The sender calls this before it takes a snapshot and when it cancels, so
    a plaintext database copy never outlives the transfer that made it.
    """
    work_directory = Path(work_directory)
    try:
        leftovers = sorted(work_directory.glob(SNAPSHOT_PREFIX + "*")) if work_directory.is_dir() else []
    except OSError:
        raise TransferError(f"{SNAPSHOT_LEFTOVER_ERROR} Folder: {work_directory}") from None
    for folder in leftovers:
        _erase_snapshot(folder)
    return len(leftovers)


def _check_database(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise TransferError("The relay database did not pass its integrity check. Nothing was moved.")
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if not {"devices", "enrollments"} <= tables:
        raise TransferError("The relay database did not pass its integrity check. Nothing was moved.")


def _read_only(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0)
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def snapshot_database(database: Path, work_directory: Path) -> bytes:
    """One consistent copy of the relay database, taken without stopping writers.

    SQLite's backup API copies a single committed state even while another
    connection holds a write transaction; a raw file copy would not. The copy
    lives in a protected temporary folder only long enough to be checked and
    read, then it is overwritten and removed. The source is opened read-only.
    If the copy cannot be erased the error names its folder and nothing is
    returned; erase_leftover_snapshots(work_directory) removes it later.
    """
    database, work_directory = Path(database), Path(work_directory)
    failure = "The relay database could not be copied. Nothing was moved."
    try:
        if not database.is_file():
            raise TransferError(failure)
        if database.stat().st_size > MAX_DATABASE_BYTES:
            raise TransferError("The relay database is too large to move. Nothing was moved.")
        work_directory.mkdir(parents=True, exist_ok=True)
        folder = Path(tempfile.mkdtemp(prefix=SNAPSHOT_PREFIX, dir=work_directory))
        try:
            setup.protect_directory(folder)
            target = folder / "relay.sqlite3"
            with closing(_read_only(database)) as source, closing(sqlite3.connect(target)) as copy:
                source.backup(copy)
                _check_database(copy)
            if target.stat().st_size > MAX_DATABASE_BYTES:
                raise TransferError("The relay database is too large to move. Nothing was moved.")
            return target.read_bytes()
        finally:
            _erase_snapshot(folder)
    except (OSError, sqlite3.Error, subprocess.SubprocessError, setup.SetupError):
        raise TransferError(failure) from None


def _regular(path: Path, limit: int) -> bytes:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
            or info.st_size > limit):
        raise TransferError("A host state file is not a regular file of the expected size. Nothing was moved.")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise TransferError("A host state file is not a regular file of the expected size. Nothing was moved.")
    return data


def describe_artifact(path: Path) -> dict:
    """Name, size and SHA-256 of one served download, for the identity card and manifest."""
    path = Path(path)
    digest, size = hashlib.sha256(), 0
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise TransferError(DATA_ERROR)
        with path.open("rb") as stream:
            while chunk := stream.read(1_048_576):
                digest.update(chunk)
                size += len(chunk)
    except OSError:
        raise TransferError("A member download could not be read.") from None
    return _artifact({"name": path.name, "size": size, "sha256": digest.hexdigest()})


def collect_state(state: Path, *, transfer_id: str, release_commitment: bytes,
                  work_directory: Path, artifacts=()) -> Bundle:
    """Read everything a transfer moves from a standalone host whose worker is stopped.

    The caller stops and disables the worker first (the bot token is read here)
    and takes this as late as possible. Nothing in the state is modified.
    """
    try:
        state = setup.state_path(handoff._safe_path(Path(state)))
        values = service.load_configuration(state)
    except (service.ConfigurationError, setup.SetupError, handoff.HandoffError, OSError):
        raise TransferError("This host's configuration could not be read. Nothing was moved.") from None
    if values["VEMALS_EARS_HOST_MODE"] != service.STANDALONE:
        raise TransferError("Only a standalone host can be moved. Nothing was moved.")
    try:
        def private(key: str, name: str) -> Path:
            path = Path(values[key])
            if path.resolve() != (state / name).resolve():
                raise TransferError("A host state file is outside its expected private location. Nothing was moved.")
            return path

        blobs = {}
        for key, name in (("VEMALS_EARS_DISCORD_BOT_TOKEN_FILE", "secrets/discord-bot-token"),
                          ("VEMALS_EARS_DISCORD_APPLICATION_ID_FILE", "secrets/discord-application-id"),
                          ("VEMALS_EARS_PUBLISHER_TOKEN_FILE", "secrets/publisher-token"),
                          ("VEMALS_EARS_PEPPER_FILE", "secrets/pepper")):
            blobs[name] = _regular(private(key, name), 16_384).strip()
        tunnel = state / TUNNEL_TOKEN
        if tunnel.exists():
            blobs[TUNNEL_TOKEN] = _regular(tunnel, BLOBS[TUNNEL_TOKEN][1] + 2).strip()
        metadata = _json(_regular(state / "host-settings.json", 16_384)) if (state / "host-settings.json").exists() else {}
        tunnel_enabled = type(metadata) is dict and metadata.get("tunnel_enabled") is True

        consent = state / "consent/consent.json"
        marker = Path(str(consent) + ".dirty")
        if marker.exists():
            # The same rule as the worker handoff: never move a ledger whose
            # last write may not have reached the disk.
            value = _json(_regular(marker, 4096))
            if type(value) is not dict or value.get("version") != 1 or value.get("synced") is not True:
                raise TransferError("The consent ledger has an unfinished write. Start this host again, "
                                    "let it settle, then move it.")
        empty = {"version": 1, "updatedAt": datetime.now(timezone.utc).isoformat(), "entries": {}}
        for name in ("consent/consent.json", "consent/mapping.json"):
            path = state / name
            ledger = _json(_regular(path, 4 * handoff.MAX_LEDGER_BYTES)) if path.exists() else empty
            try:
                handoff._ledger(ledger, values["VEMALS_EARS_GUILD_ID"], mapping=name.endswith("mapping.json"))
            except handoff.HandoffError:
                raise TransferError("A consent or mapping ledger is damaged. Restore it before moving this host.") from None
            blobs[name] = handoff._json_bytes(ledger)

        if "VEMALS_EARS_DOWNLOAD_FILE" in values:
            pointer = Path(values["VEMALS_EARS_DOWNLOAD_FILE"]).parent / POINTER_FILENAME
            if pointer.exists():
                blobs[POINTER] = _regular(pointer, MAX_POINTER_BYTES)
        # Last, so the copy is as fresh as it can be.
        blobs[DATABASE] = snapshot_database(private("VEMALS_EARS_DATABASE", DATABASE), work_directory)
        return build_bundle(
            transfer_id=transfer_id, release_commitment=release_commitment,
            settings={key: values[key] for key in sorted(PORTABLE_SETTINGS) if key in values},
            tunnel_enabled=tunnel_enabled, blobs=blobs, artifacts=artifacts)
    except OSError:
        raise TransferError("This host's state could not be read. Nothing was moved.") from None


def staging_path(state: Path) -> Path:
    """The protected sibling a receiver prepares; published by one rename."""
    state = Path(state)
    return state.with_name(state.name + ".transfer-staging")


def _destination(state: Path) -> Path:
    if os.name != "nt":
        raise TransferError("Host transfer import requires Windows.")
    try:
        return setup.state_path(handoff._safe_path(Path(state)))
    except (setup.SetupError, handoff.HandoffError):
        raise TransferError("Choose an absolute, dedicated state directory using printable ASCII characters.") from None


def _staging(state: Path) -> Path:
    staging = staging_path(state)
    try:
        handoff._safe_path(staging)
    except handoff.HandoffError:
        raise TransferError("The transfer staging directory must not be linked.") from None
    return staging


def create_staging(state: Path) -> Path:
    """Create the empty, protected staging directory. The receiving host must be empty."""
    state = _destination(state)
    staging = _staging(state)
    try:
        if state.exists():
            raise TransferError("This PC already has a host. A transfer needs an empty Host install.")
        if staging.exists():
            raise TransferError("An unfinished transfer is staged on this PC. Discard it before starting again.")
        state.parent.mkdir(parents=True, exist_ok=True)
        staging.mkdir()
        # The ACL is in place before the runtime or any secret is written.
        setup.protect_directory(staging)
        return staging
    except (OSError, subprocess.SubprocessError, setup.SetupError):
        raise TransferError("Could not create the protected staging directory. Nothing was imported.") from None



def protect_staging(state: Path) -> Path:
    """Restrict an existing staging directory, descendants included, before it holds any secret.

    A directory left by an attempt whose ACL step failed, or one created by
    something else, is never trusted to already be private: its DACL is
    rebuilt and verified, and a link, reparse point or foreign owner anywhere
    in it is refused (setup.protect_tree).
    """
    state = _destination(state)
    staging = _staging(state)
    try:
        if state.exists():
            raise TransferError("This PC already has a host. A transfer needs an empty Host install.")
        if not staging.is_dir():
            raise TransferError("The transfer staging directory is missing. Start the transfer again.")
        setup.protect_tree(staging)
        return staging
    except (OSError, subprocess.SubprocessError, setup.SetupError):
        raise TransferError("Could not protect the staging directory. Discard the unfinished transfer "
                            "and start again. Nothing was imported.") from None


def _state_files() -> list:
    return [*BLOBS, "relay.env", "relay.tmp", "host-settings.json"]


def discard_staging(state: Path) -> None:
    """Erase a staged import (secrets overwritten first). Never touches a published state."""
    staging = _staging(_destination(state))
    try:
        if staging.exists():
            _erase_tree(staging)
    except OSError:
        raise TransferError("Could not erase the staged transfer. Close other programs and retry.") from None


def import_state(state: Path, bundle: Bundle) -> dict:
    """Write a received bundle into the staging directory; never start or modify a service.

    relay.env is regenerated with the destination's own paths and the host is
    marked activation-pending. Every artifact the manifest names must already
    be mirrored into staging/downloads and match by size and SHA-256; only then
    is the release pointer written, last. On failure the files written here
    are erased and the prepared runtime is left alone.
    """
    bundle = validate_bundle(bundle)
    state = _destination(state)
    staging = _staging(state)
    failure = "Could not import the transferred host state. Existing host state was not replaced."
    try:
        if state.exists():
            raise TransferError("This PC already has a host. Existing host state is never overwritten.")
        if not staging.is_dir() or any((staging / name).exists() for name in _state_files()):
            raise TransferError("The transfer staging directory is missing or already holds an import. "
                                "Discard it and start again.")
        for artifact in bundle.manifest["artifacts"]:
            if describe_artifact(staging / "downloads" / artifact["name"]) != artifact:
                raise TransferError("A member download does not match the one the old host serves. Nothing was imported.")
    except OSError:
        raise TransferError(failure) from None
    settings = bundle.manifest["settings"]
    values = {
        "VEMALS_EARS_BIND": "127.0.0.1",
        "VEMALS_EARS_DOWNLOAD_FILE": (state / "downloads/Vemals-Ears-Setup.exe").as_posix(),
        "VEMALS_EARS_DATABASE": (state / DATABASE).as_posix(),
        "VEMALS_EARS_PEPPER_FILE": (state / "secrets/pepper").as_posix(),
        "VEMALS_EARS_PUBLISHER_TOKEN_FILE": (state / "secrets/publisher-token").as_posix(),
        "VEMALS_EARS_DISCORD_APPLICATION_ID_FILE": (state / "secrets/discord-application-id").as_posix(),
        "VEMALS_EARS_DISCORD_BOT_TOKEN_FILE": (state / "secrets/discord-bot-token").as_posix(),
        "VEMALS_EARS_CLIENT_VERSION": "",
        **settings,
    }
    try:
        try:
            for relative in ("secrets", "relay", "downloads", "logs", "consent", "profile/WoWVoiceCaptions"):
                (staging / relative).mkdir(parents=True, exist_ok=True)
            for name, data in bundle.blobs.items():
                if name == POINTER:
                    continue
                text = name.startswith(("secrets/", "consent/"))
                handoff._write_private(staging / name, data + (b"\n" if text else b""))
            with closing(_read_only(staging / DATABASE)) as connection:
                _check_database(connection)
            relay_env.save(values, staging / "relay.env")
            service.load_configuration(staging)
            handoff._write_private(staging / "host-settings.json", handoff._json_bytes({
                "schema": 1, "tunnel_enabled": bundle.manifest["tunnel_enabled"], "client_installer": "",
                "activation": ACTIVATION_PENDING, "transfer_id": bundle.manifest["transfer_id"],
                # One tunnel token on two PCs splits members between them. A
                # received host's tunnel is withheld from the start; only a
                # check that the old public address is quiet clears this.
                "tunnel_withheld": bundle.manifest["tunnel_enabled"],
            }) + b"\n")
            if POINTER in bundle.blobs:
                handoff._write_private(staging / POINTER, bundle.blobs[POINTER])
        except BaseException:
            for name in _state_files():
                if (staging / name).exists():
                    _erase(staging / name)
            raise
    except (OSError, sqlite3.Error, ValueError) as error:
        if isinstance(error, TransferError):
            raise
        raise TransferError(failure) from None
    return values


def activation(state: Path) -> str:
    """"pending" for an imported host that may not start yet, "active" once released, else ""."""
    try:
        metadata = json.loads((Path(state) / "host-settings.json").read_text(encoding="ascii"))
    except (OSError, ValueError, UnicodeError):
        return ""
    value = metadata.get("activation") if isinstance(metadata, dict) else None
    return value if value in (ACTIVATION_PENDING, ACTIVATION_ACTIVE) else ""


def mark_active(state: Path) -> None:
    """Clear the pending mark. Only call with a verified release (TransferRecords.activate)."""
    path = Path(state) / "host-settings.json"
    try:
        metadata = json.loads(path.read_text(encoding="ascii"))
        if not isinstance(metadata, dict) or metadata.get("activation") != ACTIVATION_PENDING:
            raise TransferError("This host is not waiting for a transfer to finish.")
        metadata["activation"] = ACTIVATION_ACTIVE
        temporary = path.with_suffix(".tmp")
        with temporary.open("wb") as stream:
            stream.write(handoff._json_bytes(metadata) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except (OSError, ValueError, UnicodeError) as error:
        if isinstance(error, TransferError):
            raise
        raise TransferError("Could not mark the transferred host as active.") from None


def publish_staging(state: Path) -> Path:
    """Publish a completed, still-pending import with one rename; refuse any existing state."""
    state = _destination(state)
    staging = _staging(state)
    try:
        if state.exists():
            raise TransferError("This PC already has a host. Existing host state is never overwritten.")
        if not (staging / "relay.env").is_file() or activation(staging) != ACTIVATION_PENDING:
            raise TransferError("The staged transfer is incomplete. Discard it and start again.")
        handoff._rename_new(staging, state)
        return state
    except (OSError, handoff.HandoffError):
        raise TransferError("Could not publish the transferred host state. Existing host state was not replaced.") from None


def _replace(source: Path, target: Path) -> None:
    """Rename over the target; on Windows the rename itself is written through."""
    if os.name == "nt":
        import ctypes
        move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        move.argtypes, move.restype = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32), ctypes.c_int
        # MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH: os.replace omits the second.
        if not move(str(source), str(target), 0x1 | 0x8):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        os.replace(source, target)


def _serial(method):
    """Run one records step at a time within this process."""
    @functools.wraps(method)
    def step(self, *arguments, **keywords):
        with self._mutex:
            return method(self, *arguments, **keywords)
    return step


class TransferRecords:
    """The persisted receipt and release secret under a protected transfer directory.

    Old host: offered -> sending -> released -> moved, or cancelled before
    "released". New host: pending -> active, or discarded. The receipt, not
    relay memory, says what a reboot found. The directory lives outside the
    state directory so it survives archiving; pass it in (tests use a temp dir).
    Hold locked() around a whole step so two operations never interleave.

    A "moved" receipt is what keeps the locked archive from starting, so it is
    never replaced by a new offer or receive, and erase() removes it only when
    the caller says the archive itself is already erased ("Erase archive").
    """

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.receipt = self.directory / "transfer.json"
        self._secret = self.directory / "release-secret"
        # locked() keeps other processes out. This keeps two threads of this
        # process (a move and a cancel) from interleaving a read-then-write.
        self._mutex = threading.RLock()

    def _prepare(self) -> None:
        if self.directory.is_symlink() or getattr(self.directory, "is_junction", lambda: False)():
            raise TransferError("The transfer record directory must not be linked.")
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            setup.protect_directory(self.directory)
        except (OSError, subprocess.SubprocessError, setup.SetupError):
            raise TransferError("Could not protect the transfer record directory.") from None

    @contextmanager
    def locked(self):
        self._prepare()
        with (self.directory / "operation.lock").open("a+b") as stream:
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise TransferError("Another transfer operation is running. Wait for it to finish.") from None
            try:
                yield self
            finally:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)

    def read(self) -> dict:
        """The current receipt, or {} when this PC has none. A damaged one is an error, never 'none'."""
        if not self.receipt.exists():
            return {}
        try:
            value = json.loads(_regular(self.receipt, 65_536).decode("ascii"), object_pairs_hook=handoff._pairs)
        except (OSError, ValueError, UnicodeError, RecursionError):
            raise TransferError(RECORD_ERROR) from None
        if (type(value) is not dict
                or set(value) != {"schema", "role", "state", "transfer_id", "commitment", "code_failures",
                                  "updated_at", "details"}
                or value["schema"] != 1 or value["role"] not in (SENDER, RECEIVER)
                or value["state"] not in (SENDER_STATES if value["role"] == SENDER else RECEIVER_STATES)
                or type(value["transfer_id"]) is not str or not _HEX32.fullmatch(value["transfer_id"])
                or type(value["commitment"]) is not str or not _HEX64.fullmatch(value["commitment"])
                or type(value["code_failures"]) is not int or type(value["details"]) is not dict):
            raise TransferError(RECORD_ERROR)
        return value

    def _write(self, value: dict) -> dict:
        value = {**value, "updated_at": datetime.now(timezone.utc).isoformat()}
        data = handoff._json_bytes(value) + b"\n"
        if len(data) > 65_536:
            raise TransferError(DATA_ERROR)
        self._prepare()
        temporary = self.receipt.with_suffix(".tmp")
        try:
            # Content is flushed before the rename and the rename after it, so
            # a state that was returned to the caller is the state a reboot
            # will find. That matters most for "released": R leaves this PC
            # only after this returns.
            with temporary.open("wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            _replace(temporary, self.receipt)
        except OSError:
            raise TransferError("Could not save the transfer record. Nothing was changed.") from None
        self._flush()
        return value

    def _flush(self) -> None:
        """Force the published receipt, and the rename that published it, to disk."""
        try:
            # Windows flushes only through a writable handle; flushing the
            # file also forces the NTFS log that holds its rename.
            with self.receipt.open("r+b") as stream:
                os.fsync(stream.fileno())
            if os.name != "nt":
                descriptor = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except OSError:
            raise TransferError("Could not confirm the transfer record is on disk. Try this step again.") from None

    def _begin(self, role: str, state: str, transfer_id: str, commitment: bytes, details: dict) -> dict:
        current = self.read()
        if current and current["state"] not in _TERMINAL:
            raise TransferError("A transfer is already in progress on this PC.")
        if current and current["state"] == "moved":
            raise TransferError(ARCHIVE_ERROR)
        self._forget_secret()
        return {"schema": 1, "role": role, "state": state, "transfer_id": transfer_id,
                "commitment": commitment.hex(), "code_failures": 0, "details": dict(details)}

    def _forget_secret(self) -> None:
        try:
            if self._secret.exists():
                _erase(self._secret)
        except OSError:
            raise TransferError("Could not erase the old release proof. Nothing was changed.") from None

    def _current(self, role: str) -> dict:
        current = self.read()
        if not current or current["role"] != role:
            raise TransferError("This PC has no such transfer in progress.")
        return current

    # Old host.

    @_serial
    def offer(self, transfer_id: str | None = None, **details) -> tuple:
        """Start an offer: make R, keep it private here, return (transfer_id, commitment)."""
        transfer_id = transfer_id or new_transfer_id()
        release_secret = transfer_pake.new_release_secret()
        try:
            commitment = transfer_pake.release_commitment(transfer_id, release_secret)
        except transfer_pake.PakeError:
            raise TransferError(DATA_ERROR) from None
        value = self._begin(SENDER, "offered", transfer_id, commitment, details)
        self._prepare()
        try:
            handoff._write_private(self._secret, release_secret)
        except OSError:
            raise TransferError("Could not save the transfer record. Nothing was changed.") from None
        self._write(value)
        return transfer_id, commitment

    @_serial
    def advance(self, state: str, **details) -> dict:
        """Move the receipt to sending, moved, cancelled or discarded.

        "released" is only reached through release() and "active" only through
        a verified activation. Cancelling erases the release secret, so a
        receiver left with a pending copy can never be handed a valid proof.
        """
        current = self.read()
        if (not current or state in ("released", "active")
                or state not in _TRANSITIONS[current["role"]].get(current["state"], ())):
            raise TransferError("That step is not allowed for this transfer.")
        value = self._write({**current, "state": state, "details": {**current["details"], **details}})
        if state == "cancelled":
            self._forget_secret()
        return value

    @_serial
    def note(self, **details) -> dict:
        """Record facts about the current transfer (for example sending_complete=True)."""
        current = self.read()
        if not current or current["state"] in _TERMINAL:
            raise TransferError("This PC has no such transfer in progress.")
        return self._write({**current, "details": {**current["details"], **details}})

    @_serial
    def release(self, **details) -> bytes:
        """Write "released" durably, THEN hand out R for the RELEASE frame or the release code."""
        current = self._current(SENDER)
        if current["state"] != "released":
            if "released" not in _TRANSITIONS[SENDER].get(current["state"], ()):
                raise TransferError("That step is not allowed for this transfer.")
            self._write({**current, "state": "released", "details": {**current["details"], **details}})
        else:
            # A retry after a flush that failed: confirm it now, before R.
            self._flush()
        return self.release_secret()

    @_serial
    def release_secret(self) -> bytes:
        """R, and only once this host is durably released (or already moved)."""
        current = self._current(SENDER)
        if current["state"] not in ("released", "moved"):
            raise TransferError("The release proof is not available before this host is released.")
        try:
            release_secret = _regular(self._secret, transfer_pake.RELEASE_SECRET_BYTES)
        except OSError:
            raise TransferError(RECORD_ERROR) from None
        if not transfer_pake.verify_release(current["transfer_id"], bytes.fromhex(current["commitment"]), release_secret):
            raise TransferError(RECORD_ERROR)
        return release_secret

    # New host.

    @_serial
    def receive(self, transfer_id: str, commitment: bytes, **details) -> dict:
        """Record a staged import as pending. Only the commitment is kept, never R."""
        if (type(transfer_id) is not str or not _HEX32.fullmatch(transfer_id)
                or type(commitment) is not bytes or len(commitment) != 32):
            raise TransferError(DATA_ERROR)
        return self._write(self._begin(RECEIVER, "pending", transfer_id, commitment, details))

    def _pending(self) -> dict:
        current = self._current(RECEIVER)
        if current["state"] != "pending":
            raise TransferError("This PC has no such transfer in progress.")
        return current

    @_serial
    def activate(self, release_secret: bytes) -> dict:
        """pending -> active with R from the authenticated RELEASE frame."""
        current = self._pending()
        if not transfer_pake.verify_release(current["transfer_id"], bytes.fromhex(current["commitment"]), release_secret):
            raise TransferError("The old host has not released this transfer.")
        return self._write({**current, "state": "active"})

    @_serial
    def activate_with_code(self, text: str) -> dict:
        """pending -> active with the release code typed from the old PC.

        The genuine code is never refused, however many wrong ones came first:
        R is 256 random bits and cannot be guessed, and by the time a code is
        typed this copy may be the only one left. Wrong codes are counted for
        display only, and a typing mistake (caught by the code's check group)
        is not even counted.
        """
        current = self._pending()
        try:
            release_secret = transfer_pake.parse_release_code(text)
        except transfer_pake.PakeError as error:
            raise TransferError(str(error)) from None
        if not transfer_pake.verify_release(current["transfer_id"], bytes.fromhex(current["commitment"]), release_secret):
            self._write({**current, "code_failures": min(current["code_failures"] + 1, 1_000_000)})
            raise TransferError(WRONG_RELEASE_CODE)
        return self._write({**current, "state": "active"})

    @_serial
    def activate_by_override(self) -> dict:
        """Emergency only: the old PC is gone for good and can never release.

        Refused unless an authenticated "sending complete" was recorded with
        note(sending_complete=True). The caller owns the other conditions: the
        wait, the typed attestation and a public address that no longer answers.
        """
        current = self._pending()
        if current["details"].get("sending_complete") is not True:
            raise TransferError("The old host never finished sending. Discard this transfer and start again.")
        return self._write({**current, "state": "active", "details": {**current["details"], "override": True}})

    @_serial
    def erase(self, *, archive_erased: bool = False) -> None:
        """Remove a finished transfer's receipt and release secret (terminal states only).

        A "moved" receipt locks the archive: pass archive_erased=True only
        after "Erase archive" has removed the archived host state.
        """
        current = self.read()
        if current and current["state"] not in _TERMINAL:
            raise TransferError("A transfer is still in progress on this PC.")
        if current and current["state"] == "moved" and archive_erased is not True:
            raise TransferError(ARCHIVE_ERROR)
        self._forget_secret()
        try:
            if self.receipt.exists():
                self.receipt.unlink()
        except OSError:
            raise TransferError("Could not erase the transfer record.") from None


# Control frames, the profile delta, the locked archive and the member
# downloads a host serves. The sender and receiver sequences in
# Hosting.transfer_flow are built from these.

CONTROL_MAGIC = b"VETC"
CONTROL_HEADER = struct.Struct(">4s16sBI")  # magic, transfer id, direction, sequence
TO_CLAIM, TO_OFFER = 1, 2
MAX_CONTROL_BODY = FRAME_LIMIT - CONTROL_HEADER.size - 32
MAX_DELTA_ROWS = 5_000
MAX_DELTA_BYTES = 2 * 1_048_576
DELTA_PART_BYTES = 8_000
ARCHIVE_SUFFIX = ".moved-archive"
INSTALLER_NAME = "Vemals-Ears-Setup.exe"
SOURCE_NAME = "vemals-ears-member.zip"
_DEVICE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_ARTIFACT_PATH = re.compile(r"/download|/source(/[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6})?")
_SENSITIVE = ("secrets", "relay", "consent")


def records_directory(state: Path) -> Path:
    """The protected receipt directory of the host whose state is here."""
    return service.transfer_directory(state)


def is_control_frame(frame) -> bool:
    return type(frame) is bytes and frame[:4] == CONTROL_MAGIC


class ControlChannel:
    """Authenticated control messages between the two hosts of one transfer.

    Control frames have their own MAC domain, separate from bundle frames
    that share the key. The direction and a strict per-direction sequence are
    inside the authenticated body, so a frame can be neither reflected back
    to its sender nor replayed, reordered or moved to another transfer.
    The frames themselves are integrity-protected only. Two things in them
    are private, and each is handled on its own: the DELTA rows (device IDs,
    typed-chat names, classes, revocations) are AES-encrypted under the
    session's bundle key before they are put in a frame (seal_delta), and R
    in the RELEASE frame is sent in clear, only after "released" is durable;
    it is useless without local access to the new PC.

    seal() may be called from two threads (a move and its cancel), so the
    sequence number is taken under a lock.
    """

    def __init__(self, transfer_id: str, mac_key: bytes, role: str) -> None:
        _require(type(mac_key) is bytes and len(mac_key) == 32
                 and role in (transfer_pake.ROLE_OFFER, transfer_pake.ROLE_CLAIM))
        self._identifier = _raw_id(transfer_id)
        self._mac_key = mac_key
        offer = role == transfer_pake.ROLE_OFFER
        self._outgoing, self._incoming = (TO_CLAIM, TO_OFFER) if offer else (TO_OFFER, TO_CLAIM)
        self._sent = self._received = 0
        self._failed = False
        self._lock = threading.Lock()

    def _mac(self, body: bytes) -> bytes:
        return hmac.digest(self._mac_key, b"VemalsEars host transfer v1 control frame\x00" + body, "sha256")

    def seal(self, kind: str, **fields) -> bytes:
        _require(type(kind) is str and re.fullmatch(r"[a-z_]{1,32}", kind) is not None and "type" not in fields)
        message = handoff._json_bytes({"type": kind, **fields})
        _require(len(message) <= MAX_CONTROL_BODY)
        with self._lock:
            body = CONTROL_HEADER.pack(CONTROL_MAGIC, self._identifier, self._outgoing, self._sent) + message
            self._sent += 1
        return body + self._mac(body)

    def open(self, frame) -> dict:
        """Verify one frame and return its message; any failure ends the channel."""
        try:
            if self._failed or type(frame) is not bytes or not CONTROL_HEADER.size + 32 <= len(frame) <= FRAME_LIMIT:
                raise TransferError(FRAME_ERROR)
            body, mac = frame[:-32], frame[-32:]
            if not hmac.compare_digest(self._mac(body), mac):
                raise TransferError(FRAME_ERROR)
            magic, identifier, direction, sequence = CONTROL_HEADER.unpack_from(body)
            if (magic != CONTROL_MAGIC or identifier != self._identifier
                    or direction != self._incoming or sequence != self._received):
                raise TransferError(FRAME_ERROR)
            message = _json(body[CONTROL_HEADER.size:])
            if type(message) is not dict or type(message.get("type")) is not str:
                raise TransferError(FRAME_ERROR)
            self._received += 1
            return message
        except (TransferError, ValueError, struct.error):
            self._failed = True
            raise TransferError(FRAME_ERROR) from None


def _writable(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(Path(database).resolve().as_uri() + "?mode=rw", uri=True,
                                 timeout=5.0, isolation_level=None)
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def profile_rows(database: Path) -> dict:
    """{device_id: (typed-chat name, class, revoked_at)} as one committed state.

    Read inside BEGIN IMMEDIATE so no writer is half way through: the lock is
    held for this one query only, never across a network step, because every
    typed line authenticates with a small write of its own.
    """
    try:
        with closing(_writable(database)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = connection.execute(
                    "SELECT device_id, quick_chat_name, quick_chat_wow_class, revoked_at FROM devices").fetchall()
            finally:
                connection.execute("ROLLBACK")
    except (sqlite3.Error, OSError):
        raise TransferError("The relay database could not be read. Nothing was moved.") from None
    return {str(row[0]): (row[1], str(row[2] or ""), row[3]) for row in rows}


def profile_delta(before: dict, database: Path) -> list:
    """Rows whose typed-chat profile or revocation changed since `before`.

    Profile writes are never refused during a move, so the ones that landed
    after the snapshot follow it in authenticated control frames.
    """
    rows = [[device, name, wow_class, revoked]
            for device, (name, wow_class, revoked) in sorted(profile_rows(database).items())
            if before.get(device) != (name, wow_class, revoked)]
    if len(rows) > MAX_DELTA_ROWS:
        raise TransferError("Too many typed-chat profiles changed during the move. Start the move again.")
    return rows


def seal_delta(rows: list, keys) -> list:
    """The delta as the fields of one or more DELTA control frames, encrypted.

    The rows name members' devices and typed-chat names, so they are sealed
    with the session's bundle key (AES-256-CBC, fresh IV) exactly like the
    bundle; the control frames that carry the parts authenticate them. Always
    at least one part, the last one marked final.
    """
    _require(type(rows) is list and len(rows) <= MAX_DELTA_ROWS)
    plaintext = handoff._json_bytes(rows)
    if len(plaintext) > MAX_DELTA_BYTES:
        raise TransferError("Too many typed-chat profiles changed during the move. Start the move again.")
    iv = secrets.token_bytes(16)
    try:
        data = iv + handoff.transfer_aes(plaintext, keys.bundle_enc, iv, decrypt=False)
    except handoff.HandoffError as error:
        raise TransferError(str(error)) from None
    parts = [data[index:index + DELTA_PART_BYTES] for index in range(0, len(data), DELTA_PART_BYTES)]
    return [{"data": base64.b64encode(part).decode("ascii"), "final": index == len(parts) - 1}
            for index, part in enumerate(parts)]


def delta_part(message: dict) -> tuple:
    """(bytes, final) from one authenticated DELTA control message."""
    data, final = message.get("data"), message.get("final")
    _require(type(data) is str and 0 < len(data) <= 2 * DELTA_PART_BYTES and type(final) is bool)
    try:
        return base64.b64decode(data.encode("ascii"), validate=True), final
    except (ValueError, UnicodeError):
        raise TransferError(FRAME_ERROR) from None


def open_delta(data: bytes, keys) -> list:
    """Decrypt the joined DELTA parts. The control channel authenticated every part first."""
    _require(type(data) is bytes and 32 <= len(data) <= MAX_DELTA_BYTES + 48 and len(data) % 16 == 0)
    try:
        rows = _json(handoff.transfer_aes(data[16:], keys.bundle_enc, data[:16], decrypt=True))
    except handoff.HandoffError:
        raise TransferError(FRAME_ERROR) from None
    _require(type(rows) is list and len(rows) <= MAX_DELTA_ROWS)
    return rows


def _delta_row(row) -> tuple:
    _require(type(row) is list and len(row) == 4)
    device, name, wow_class, revoked = row
    _require(type(device) is str and _DEVICE_ID.fullmatch(device) is not None)
    _require(name is None or (type(name) is str and 1 <= len(name) <= 64
                              and all(ord(char) >= 32 for char in name)))
    _require(type(wow_class) is str and re.fullmatch(r"[a-z-]{0,32}", wow_class) is not None)
    _require(revoked is None or (type(revoked) in (int, float) and 0 <= revoked < 1e11))
    return device, name, wow_class, revoked


def apply_profile_delta(database: Path, rows: list) -> int:
    """Apply a received delta to the staged database in one transaction.

    Only the typed-chat profile and a revocation can change, only on devices
    the snapshot already holds, and a revoked device is never un-revoked.
    """
    _require(type(rows) is list and len(rows) <= MAX_DELTA_ROWS)
    checked = [_delta_row(row) for row in rows]
    try:
        with closing(_writable(database)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                changed = 0
                for device, name, wow_class, revoked in checked:
                    changed += connection.execute(
                        "UPDATE devices SET quick_chat_name = ?, quick_chat_wow_class = ?, "
                        "revoked_at = COALESCE(revoked_at, ?) WHERE device_id = ?",
                        (name, wow_class, revoked, device)).rowcount
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
    except (sqlite3.Error, OSError):
        raise TransferError("The latest typed-chat profiles could not be saved. Nothing was moved.") from None
    return changed


def served_artifacts(state: Path) -> list:
    """Every member download this host serves, as {name, size, sha256, path}.

    With a release pointer: the installer and source bundle it names. Without
    one: the rolling installer, the rolling source bundle and the source
    bundle for the configured client version. `path` is where the old host
    serves the file; the new PC mirrors each one and checks size and SHA-256.
    Nothing is listed for a host that only links to an external download.
    """
    from GuildHub.release_pointer import ReleasePointerError, canonical_version, load_release_pointer

    try:
        values = service.load_configuration(Path(state))
    except service.ConfigurationError:
        raise TransferError("This host's configuration could not be read. Nothing was moved.") from None
    download = values.get("VEMALS_EARS_DOWNLOAD_FILE", "")
    if not download:
        return []
    folder = Path(download).parent
    try:
        snapshot = load_release_pointer(folder)
    except ReleasePointerError:
        raise TransferError("The published member release on this host is damaged. Publish it again, "
                            "then move the host.") from None
    files = []
    if snapshot is not None:
        files = [(snapshot.installer.path, "/download"), (snapshot.source.path, "/source")]
    else:
        if Path(download).is_file():
            if Path(download).name != INSTALLER_NAME:
                raise TransferError("The member installer on this host has an unexpected file name. "
                                    "Choose it again in Edit settings, then move the host.")
            files.append((Path(download), "/download"))
        if (folder / SOURCE_NAME).is_file():
            files.append((folder / SOURCE_NAME, "/source"))
        version = canonical_version(values.get("VEMALS_EARS_CLIENT_VERSION", ""))
        if version is not None and (folder / ("vemals-ears-member-" + version + ".zip")).is_file():
            files.append((folder / ("vemals-ears-member-" + version + ".zip"), "/source/" + version))
    artifacts = [{**describe_artifact(path), "path": served} for path, served in files]
    if len({entry["name"].lower() for entry in artifacts}) != len(artifacts) or len(artifacts) > MAX_ARTIFACTS:
        raise TransferError("The member downloads on this host cannot be listed. Publish them again, "
                            "then move the host.")
    return artifacts


def card_artifacts(value) -> list:
    """Validate the artifact list of an identity card; returns detached entries."""
    _require(type(value) is list and len(value) <= MAX_ARTIFACTS)
    result = []
    for entry in value:
        _require(type(entry) is dict and set(entry) == {"name", "size", "sha256", "path"}
                 and type(entry["path"]) is str and _ARTIFACT_PATH.fullmatch(entry["path"]) is not None)
        result.append({**_artifact({key: entry[key] for key in ("name", "size", "sha256")}),
                       "path": entry["path"]})
    _require(len({entry["name"].lower() for entry in result}) == len(result)
             and len({entry["path"] for entry in result}) == len(result))
    return result


def _erase_host_tree(folder: Path) -> None:
    """Remove a host state tree: credentials and the database are overwritten first.

    The speech runtime, logs and downloads hold nothing private and are large,
    so they are only deleted.
    """
    for name in ("relay.env", "relay.tmp", "host-settings.json", "host-settings.tmp"):
        if (folder / name).is_file() or (folder / name).is_symlink():
            _erase(folder / name)
    for name in _SENSITIVE:
        if (folder / name).is_symlink() or getattr(folder / name, "is_junction", lambda: False)():
            (folder / name).unlink()
        elif (folder / name).is_dir():
            _erase_tree(folder / name)
    shutil.rmtree(folder)


def archive_path(state: Path) -> Path:
    """Where the locked archive of a moved host is kept: a sibling of its state."""
    state = Path(state)
    return state.with_name(state.name + ARCHIVE_SUFFIX)


def archive_state(state: Path) -> Path:
    """Rename a stopped host's state into its locked archive.

    The archive keeps the state directory's own protection. It cannot start:
    its services are removed, nothing is configured at the state path any
    more, and the "moved" receipt refuses every start route until the archive
    is erased.
    """
    state = Path(state)
    archive = archive_path(state)
    try:
        handoff._safe_path(archive)
        if archive.exists():
            raise TransferError("An older archive is still on this PC. Erase it, then finish the move.")
        for attempt in range(20):
            try:
                state.rename(archive)
                break
            except PermissionError:
                # A service that has only just stopped may still be letting go.
                if attempt == 19:
                    raise
                time.sleep(0.5)
        setup.protect_directory(archive)
        return archive
    except (OSError, subprocess.SubprocessError, setup.SetupError, handoff.HandoffError):
        raise TransferError("The stopped host could not be archived. Close other programs using its "
                            "folder, then finish the move.") from None


def erase_archive(state: Path) -> bool:
    """"Erase archive": overwrite and remove the archived host. True when one was there."""
    archive = archive_path(Path(state))
    try:
        handoff._safe_path(archive)
        if archive.is_symlink() or getattr(archive, "is_junction", lambda: False)():
            archive.unlink()
            return True
        if not archive.exists():
            return False
        _erase_host_tree(archive)
        return True
    except (OSError, handoff.HandoffError):
        raise TransferError("The archive could not be erased. Close other programs using its folder and retry.") from None


def discard_published(state: Path) -> bool:
    """Erase a received host that was published but never released. True when one was there.

    Refuses anything that is not marked activation-pending: an active host is
    never removed by a discard.
    """
    state = _destination(state)
    try:
        if not state.exists():
            return False
        if activation(state) != ACTIVATION_PENDING:
            raise TransferError("This PC's host is not a pending transfer. Nothing was erased.")
        _erase_host_tree(state)
        return True
    except OSError:
        raise TransferError("Could not erase the pending transfer. Close other programs and retry.") from None
