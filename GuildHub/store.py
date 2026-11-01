"""SQLite-backed enrollment and device credential storage.

Only keyed hashes of enrollment codes and device tokens are persisted. The
caller is responsible for showing a newly-created secret exactly once.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
import unicodedata
import re
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from VoiceCaptions.events import CLASS_COLORS


MIN_PEPPER_LENGTH = 32
MAX_ENROLLMENT_TTL_SECONDS = 7 * 24 * 60 * 60
HOSTED_JOBS_CAPABILITY = "hosted_jobs"
DEVICE_CAPABILITIES = frozenset({HOSTED_JOBS_CAPABILITY})
MAX_QUICK_CHAT_NAME = 32
_VIEWER_TOKEN = re.compile(r"^[0-9a-f]{24}$")


class PairingError(ValueError):
    """An enrollment code couldn't be claimed."""


@dataclass(frozen=True)
class EnrollmentCode:
    code: str = field(repr=False)
    created_at: float
    expires_at: float


@dataclass(frozen=True)
class DeviceCredentials:
    device_id: str
    device_token: str = field(repr=False)
    created_at: float


@dataclass(frozen=True)
class DeviceRecord:
    device_id: str
    device_name: str
    client_version: str
    created_at: float
    last_seen_at: float | None
    revoked_at: float | None
    hosted_jobs: bool = False
    quick_chat_name: str | None = None
    quick_chat_wow_class: str = ""
    viewer_token: str | None = None


def validate_viewer_token(value: str) -> str:
    if not isinstance(value, str) or not _VIEWER_TOKEN.fullmatch(value):
        raise ValueError("viewer token is invalid")
    return value


def validate_quick_chat_profile(name: str, wow_class: str = "") -> tuple[str, str]:
    """Return one canonical typed-chat identity."""
    if not isinstance(name, str):
        raise ValueError("quick-chat display name must be text")
    normalized_name = unicodedata.normalize("NFKC", name)
    if any(
        unicodedata.category(character).startswith("C")
        for character in normalized_name
    ):
        raise ValueError("quick-chat display name contains control characters")
    clean_name = " ".join(normalized_name.split())
    if not clean_name or len(clean_name) > MAX_QUICK_CHAT_NAME:
        raise ValueError("quick-chat display name must be 1 to 32 characters")
    if not isinstance(wow_class, str):
        raise ValueError("WoW class must be text")
    clean_class = unicodedata.normalize("NFKC", wow_class).strip().lower()
    if clean_class and clean_class not in CLASS_COLORS:
        raise ValueError("WoW class is unsupported")
    return clean_name, clean_class


class CredentialStore:
    """Persist one-use enrollments and revocable device credentials."""

    def __init__(
        self,
        database: str | Path,
        pepper: str,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(pepper, str) or len(pepper) < MIN_PEPPER_LENGTH:
            raise ValueError("pepper must contain at least 32 characters")
        self.database = str(Path(database))
        self._pepper = pepper.encode("utf-8")
        self._clock = clock

    def initialize(self) -> None:
        parent = Path(self.database).resolve().parent
        parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS enrollments (
                    code_hash BLOB PRIMARY KEY,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    claimed_at REAL,
                    claimed_by TEXT,
                    viewer_token TEXT
                );

                CREATE TABLE IF NOT EXISTS devices (
                    device_id TEXT PRIMARY KEY,
                    token_hash BLOB NOT NULL UNIQUE,
                    device_name TEXT NOT NULL,
                    client_version TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_seen_at REAL,
                    revoked_at REAL,
                    quick_chat_name TEXT,
                    quick_chat_wow_class TEXT NOT NULL DEFAULT '',
                    viewer_token TEXT
                );

                CREATE TABLE IF NOT EXISTS device_capabilities (
                    device_id TEXT NOT NULL,
                    capability TEXT NOT NULL,
                    granted_at REAL NOT NULL,
                    PRIMARY KEY (device_id, capability),
                    FOREIGN KEY (device_id) REFERENCES devices(device_id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS enrollments_expiry
                    ON enrollments(expires_at);
                CREATE INDEX IF NOT EXISTS devices_revoked
                    ON devices(revoked_at);
                CREATE INDEX IF NOT EXISTS device_capabilities_name
                    ON device_capabilities(capability);
                """
            )
            # SQLite has no portable `ADD COLUMN IF NOT EXISTS`. Serialize
            # this tiny migration so the relay and loopback console may both
            # initialize an older database without racing each other.
            connection.execute("BEGIN IMMEDIATE")
            try:
                columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(devices)")
                }
                if "quick_chat_name" not in columns:
                    connection.execute(
                        "ALTER TABLE devices ADD COLUMN quick_chat_name TEXT"
                    )
                if "quick_chat_wow_class" not in columns:
                    connection.execute(
                        "ALTER TABLE devices ADD COLUMN "
                        "quick_chat_wow_class TEXT NOT NULL DEFAULT ''"
                    )
                if "viewer_token" not in columns:
                    connection.execute(
                        "ALTER TABLE devices ADD COLUMN viewer_token TEXT"
                    )
                enrollment_columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(enrollments)")
                }
                if "viewer_token" not in enrollment_columns:
                    connection.execute(
                        "ALTER TABLE enrollments ADD COLUMN viewer_token TEXT"
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def create_enrollment(
        self,
        ttl_seconds: float = 30 * 60,
        *,
        code: str | None = None,
        viewer_token: str | None = None,
    ) -> EnrollmentCode:
        """Mint an enrollment, optionally with a caller-chosen code and identity.

        Shino, 2026-09-01: captions should follow voice presence, with no
        separate "link Discord" step. Presence gating needs to know which
        device belongs to which Discord person, and the honest place to
        learn that is the one step they already take: pairing. A pairing
        invited from Discord arrives carrying the requester's viewer token,
        so the device inherits it at claim time and never asks again.

        The caller may supply the code because the invitation is written in
        Discord before this relay hears about it - the adapter mints it
        with its own CSPRNG and shows it once, privately, to the person who
        asked. A supplied code must still look like one of ours.
        """
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, (int, float))
            or ttl_seconds <= 0
            or ttl_seconds > MAX_ENROLLMENT_TTL_SECONDS
        ):
            raise ValueError("enrollment lifetime must be between 0 and 7 days")
        if viewer_token is not None and (
            not isinstance(viewer_token, str)
            or not re.fullmatch(r"[0-9a-f]{24}", viewer_token)
        ):
            raise ValueError("viewer token is invalid")
        if code is not None and (
            not isinstance(code, str)
            or not re.fullmatch(r"ghe_[A-Za-z0-9_-]{32,64}", code)
        ):
            raise ValueError("enrollment code is invalid")
        created_at = float(self._clock())
        expires_at = created_at + float(ttl_seconds)
        attempts = 1 if code is not None else 4
        for _attempt in range(attempts):
            chosen = code if code is not None else "ghe_" + secrets.token_urlsafe(32)
            code_hash = self._digest("enrollment", chosen)
            try:
                with closing(self._connect()) as connection:
                    connection.execute(
                        """
                        INSERT INTO enrollments(
                            code_hash, created_at, expires_at, claimed_at,
                            claimed_by, viewer_token
                        ) VALUES (?, ?, ?, NULL, NULL, ?)
                        """,
                        (
                            sqlite3.Binary(code_hash),
                            created_at,
                            expires_at,
                            viewer_token,
                        ),
                    )
                return EnrollmentCode(chosen, created_at, expires_at)
            except sqlite3.IntegrityError:
                if code is not None:
                    # A repeated invitation is not a new one; the person
                    # can ask again and get a fresh code.
                    raise PairingError("enrollment code is already in use")
                continue
        raise RuntimeError("unable to allocate a unique enrollment code")

    def claim_enrollment(
        self,
        code: str,
        device_name: str,
        client_version: str,
    ) -> DeviceCredentials:
        """Atomically consume an enrollment code and create one device."""
        now = float(self._clock())
        code_hash = self._digest("enrollment", code)
        for _attempt in range(4):
            device_id = "ghdev_" + secrets.token_urlsafe(18)
            device_token = "ghd_" + secrets.token_urlsafe(32)
            token_hash = self._digest("device", device_token)
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                invited = connection.execute(
                    "SELECT viewer_token FROM enrollments WHERE code_hash = ?",
                    (sqlite3.Binary(code_hash),),
                ).fetchone()
                inherited = invited[0] if invited is not None else None
                claimed = connection.execute(
                    """
                    UPDATE enrollments
                       SET claimed_at = ?, claimed_by = ?
                     WHERE code_hash = ?
                       AND claimed_at IS NULL
                       AND expires_at > ?
                    """,
                    (
                        now,
                        device_id,
                        sqlite3.Binary(code_hash),
                        now,
                    ),
                )
                if claimed.rowcount != 1:
                    connection.rollback()
                    raise PairingError("enrollment could not be claimed")
                connection.execute(
                    """
                    INSERT INTO devices(
                        device_id, token_hash, device_name, client_version,
                        created_at, last_seen_at, revoked_at, viewer_token
                    ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)
                    """,
                    (
                        device_id,
                        sqlite3.Binary(token_hash),
                        device_name,
                        client_version,
                        now,
                        inherited,
                    ),
                )
                connection.commit()
                return DeviceCredentials(device_id, device_token, now)
            except PairingError:
                raise
            except sqlite3.IntegrityError:
                connection.rollback()
                continue
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()
        raise RuntimeError("unable to allocate a unique device credential")

    def authenticate_device(
        self,
        device_id: str,
        device_token: str,
        client_version: str | None = None,
    ) -> bool:
        """Validate a device token without exposing device existence."""
        return (
            self.authenticate_device_record(
                device_id, device_token, client_version=client_version
            )
            is not None
        )

    def authenticate_device_record(
        self,
        device_id: str,
        device_token: str,
        client_version: str | None = None,
    ) -> DeviceRecord | None:
        """Authenticate and return server-managed metadata for one device."""
        candidate_hash = self._digest("device", device_token)
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT token_hash, device_name, client_version, created_at,
                       last_seen_at, revoked_at, quick_chat_name,
                       quick_chat_wow_class, viewer_token,
                       EXISTS(
                           SELECT 1
                             FROM device_capabilities
                            WHERE device_id = devices.device_id
                              AND capability = ?
                       )
                  FROM devices
                 WHERE device_id = ?
                """,
                (HOSTED_JOBS_CAPABILITY, device_id),
            ).fetchone()
            stored_hash = bytes(row[0]) if row is not None else bytes(32)
            secret_matches = hmac.compare_digest(candidate_hash, stored_hash)
            active = row is not None and row[5] is None
            authenticated = bool(secret_matches and active)
            if not authenticated or row is None:
                return None
            seen_at = float(self._clock())
            # A silent client must not erase the last version it reported.
            if client_version:
                touched = connection.execute(
                    """
                    UPDATE devices
                       SET last_seen_at = ?, client_version = ?
                     WHERE device_id = ? AND revoked_at IS NULL
                    """,
                    (seen_at, client_version, device_id),
                )
            else:
                touched = connection.execute(
                    """
                    UPDATE devices
                       SET last_seen_at = ?
                     WHERE device_id = ? AND revoked_at IS NULL
                    """,
                    (seen_at, device_id),
                )
            if touched.rowcount != 1:
                return None
            return DeviceRecord(
                device_id=device_id,
                device_name=str(row[1]),
                client_version=str(client_version or row[2]),
                created_at=float(row[3]),
                last_seen_at=seen_at,
                revoked_at=None,
                hosted_jobs=bool(row[9]),
                quick_chat_name=(
                    str(row[6]) if row[6] is not None else None
                ),
                quick_chat_wow_class=str(row[7] or ""),
                viewer_token=str(row[8]) if row[8] is not None else None,
            )

    def bind_viewer_token(self, device_id: str, token: str) -> bool:
        """Bind an opaque identity after the relay authenticated a link code."""
        checked = validate_viewer_token(token)
        with closing(self._connect()) as connection:
            changed = connection.execute(
                """
                UPDATE devices
                   SET viewer_token = ?
                 WHERE device_id = ? AND revoked_at IS NULL
                """,
                (checked, device_id),
            )
            linked = changed.rowcount == 1
            connection.commit()
            return linked

    def authenticate_device_viewer_token(
        self, device_id: str, device_token: str
    ) -> str | None:
        """Read a viewer binding only after authenticating the device secret."""
        record = self.authenticate_device_record(device_id, device_token)
        return record.viewer_token if record is not None else None

    def set_quick_chat_profile(
        self, device_id: str, name: str, wow_class: str = ""
    ) -> bool:
        """Let the operator assign both typed-chat fields for a device."""
        clean_name, clean_class = validate_quick_chat_profile(name, wow_class)
        with closing(self._connect()) as connection:
            changed = connection.execute(
                """
                UPDATE devices
                   SET quick_chat_name = ?, quick_chat_wow_class = ?
                 WHERE device_id = ? AND revoked_at IS NULL
                """,
                (clean_name, clean_class, device_id),
            )
            return changed.rowcount == 1

    def set_quick_chat_name(self, device_id: str, name: str) -> str | None:
        """Update only one active device's self-service typed-chat name.

        The class remains operator-managed because a member's Discord class
        mapping is a separate authenticated identity. Returning the canonical
        name lets the relay report exactly what reached durable storage.
        """
        clean_name, _unused_class = validate_quick_chat_profile(name)
        with closing(self._connect()) as connection:
            changed = connection.execute(
                """
                UPDATE devices
                   SET quick_chat_name = ?
                 WHERE device_id = ? AND revoked_at IS NULL
                """,
                (clean_name, device_id),
            )
            return clean_name if changed.rowcount == 1 else None

    def revoke_device(self, device_id: str) -> bool:
        with closing(self._connect()) as connection:
            changed = connection.execute(
                """
                UPDATE devices
                   SET revoked_at = ?
                 WHERE device_id = ? AND revoked_at IS NULL
                """,
                (float(self._clock()), device_id),
            )
            return changed.rowcount == 1

    def set_device_capability(
        self,
        device_id: str,
        capability: str,
        enabled: bool,
    ) -> bool:
        """Grant or remove one known capability on an active device.

        Pairing remains caption-only by default. Hosted compute is an
        operator decision because it spends shared server capacity.
        """
        if capability not in DEVICE_CAPABILITIES:
            raise ValueError("unknown device capability")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be true or false")
        with closing(self._connect()) as connection:
            active = connection.execute(
                """
                SELECT 1 FROM devices
                 WHERE device_id = ? AND revoked_at IS NULL
                """,
                (device_id,),
            ).fetchone()
            if active is None:
                return False
            if enabled:
                connection.execute(
                    """
                    INSERT INTO device_capabilities(device_id, capability, granted_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(device_id, capability) DO NOTHING
                    """,
                    (device_id, capability, float(self._clock())),
                )
            else:
                connection.execute(
                    """
                    DELETE FROM device_capabilities
                     WHERE device_id = ? AND capability = ?
                    """,
                    (device_id, capability),
                )
            return True

    def device_has_capability(self, device_id: str, capability: str) -> bool:
        if capability not in DEVICE_CAPABILITIES:
            return False
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT 1
                  FROM device_capabilities AS capability
                  JOIN devices AS device ON device.device_id = capability.device_id
                 WHERE capability.device_id = ?
                   AND capability.capability = ?
                   AND device.revoked_at IS NULL
                """,
                (device_id, capability),
            ).fetchone()
        return row is not None

    def list_devices(self) -> list[DeviceRecord]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT device.device_id, device.device_name,
                       device.client_version, device.created_at,
                       device.last_seen_at, device.revoked_at,
                       CASE WHEN capability.device_id IS NULL THEN 0 ELSE 1 END,
                       device.quick_chat_name, device.quick_chat_wow_class,
                       device.viewer_token
                  FROM devices AS device
             LEFT JOIN device_capabilities AS capability
                    ON capability.device_id = device.device_id
                   AND capability.capability = ?
                 ORDER BY device.created_at, device.device_id
                """,
                (HOSTED_JOBS_CAPABILITY,),
            ).fetchall()
        return [
            DeviceRecord(
                device_id=str(row[0]),
                device_name=str(row[1]),
                client_version=str(row[2]),
                created_at=float(row[3]),
                last_seen_at=float(row[4]) if row[4] is not None else None,
                revoked_at=float(row[5]) if row[5] is not None else None,
                hosted_jobs=bool(row[6]),
                quick_chat_name=str(row[7]) if row[7] is not None else None,
                quick_chat_wow_class=str(row[8] or ""),
                viewer_token=str(row[9]) if row[9] is not None else None,
            )
            for row in rows
        ]

    def prune_enrollments(self) -> int:
        """Remove expired and already-claimed enrollment records."""
        with closing(self._connect()) as connection:
            changed = connection.execute(
                """
                DELETE FROM enrollments
                 WHERE claimed_at IS NOT NULL OR expires_at <= ?
                """,
                (float(self._clock()),),
            )
            return changed.rowcount

    def revoke_unused_devices(
        self,
        *,
        unused_after: float,
        idle_after: float,
    ) -> tuple[int, int]:
        """Revoke credentials nobody is using. Returns (never_used, idle).

        Two different kinds of clutter, deliberately separated so the log
        can say which happened:

        - NEVER USED: paired but never once connected. Most of these are
          orphans - the relay claimed the invitation and created the
          device, then the pairing prompt was never answered, so the app
          never saved the credential. Nobody holds these; they are dead
          the moment they are made.
        - IDLE: connected once and then not for a long time. A real member
          who stopped playing, or a machine that was replaced.

        Revoking is reversible: the person pairs again and gets a new
        credential. Nothing here deletes a device that is in use, and a
        zero or negative window disables that half entirely.
        """
        now = float(self._clock())
        never_used = idle = 0
        with closing(self._connect()) as connection:
            if unused_after > 0:
                never_used = connection.execute(
                    """
                    UPDATE devices
                       SET revoked_at = ?
                     WHERE revoked_at IS NULL
                       AND last_seen_at IS NULL
                       AND created_at <= ?
                    """,
                    (now, now - unused_after),
                ).rowcount
            if idle_after > 0:
                idle = connection.execute(
                    """
                    UPDATE devices
                       SET revoked_at = ?
                     WHERE revoked_at IS NULL
                       AND last_seen_at IS NOT NULL
                       AND last_seen_at <= ?
                    """,
                    (now, now - idle_after),
                ).rowcount
        return never_used, idle

    def forget_revoked_devices(self, *, older_than: float) -> int:
        """Delete long-revoked devices so the console list stays readable.

        Recent revocations stay visible, because "why did this stop
        working" is a question worth being able to answer. Capabilities
        go with the device through the existing ON DELETE CASCADE.
        """
        if older_than <= 0:
            return 0
        now = float(self._clock())
        with closing(self._connect()) as connection:
            return connection.execute(
                "DELETE FROM devices WHERE revoked_at IS NOT NULL AND revoked_at <= ?",
                (now - older_than,),
            ).rowcount

    def _digest(self, purpose: str, secret: str) -> bytes:
        if not isinstance(secret, str):
            secret = ""
        payload = purpose.encode("ascii") + b"\0" + secret.encode(
            "utf-8", errors="replace"
        )
        return hmac.new(self._pepper, payload, hashlib.sha256).digest()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database,
            timeout=5.0,
            isolation_level=None,
        )
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection
