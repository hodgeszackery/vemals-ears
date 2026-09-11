"""Background discovery and safe staging for Vemal's Ears updates.

The relay may tell a paired device where an installer is, but it does not get
to decide which publisher the desktop app trusts. Automatic execution is
available only to installer-managed copies whose installed payload manifest
names an Authenticode publisher. Unsigned builds retain the explicit,
interactive installer path.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from GuildHub.credentials import DeviceCredentialStore
from GuildHub.version import CLIENT_VERSION


INITIAL_CHECK_SECONDS = 15 * 60
HEALTHY_CHECK_SECONDS = 6 * 60 * 60
FAILED_CHECK_SECONDS = 60 * 60
BUSY_RETRY_SECONDS = 5 * 60
INSTALLER_TIMEOUT_SECONDS = 5 * 60
MAX_MANIFEST_BYTES = 16 * 1024
SESSION_STATE_NAME = "update-session.json"
SESSION_STATE_SCHEMA = 1
RELAUNCH_STATE_NAME = "update-relaunch.json"
RELAUNCH_STATE_SCHEMA = 1
RELAUNCH_STATE_MAX_AGE_SECONDS = 10 * 60
TRUST_SCHEMA = 1
KNOWN_SESSION_STATES = frozenset(
    ("active", "error", "paused", "ready", "stopped", "unknown")
)
_PUBLISHER_LIMIT = 4
_PUBLISHER_LENGTH = 320
_VERSION_PART = re.compile(r"^[0-9]{1,6}$")
_STAGED_FILE = re.compile(
    r"^[0-9]+(?:\.[0-9]+){2,3}-[0-9a-f]{16}\.exe(?:\.part)?$"
)


class AutomaticUpdateError(RuntimeError):
    """An update could not be safely discovered, staged, or launched."""


class _InstallerVerificationError(AutomaticUpdateError):
    """The staged installer changed or failed its local trust policy."""


@dataclass(frozen=True)
class SessionState:
    state: str
    observed_at: float


@dataclass(frozen=True)
class LaunchRequest:
    path: Path
    version: str
    automatic: bool


@dataclass(frozen=True)
class _WorkerResult:
    operation: str
    identity: tuple[str, str, int, str] | None
    value: Any = None
    error: str = ""
    invalidates_stage: bool = False


@dataclass(frozen=True)
class _PreflightResult:
    latest: dict[str, Any] | None
    launch: LaunchRequest | None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url,
            code,
            "Update discovery does not follow redirects.",
            headers,
            fp,
        )


def _offer_identity(offer: dict[str, Any]) -> tuple[str, str, int, str]:
    return (
        offer["version"],
        offer["sha256"],
        offer["bytes"],
        offer["url"],
    )


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n",
        encoding="ascii",
    )
    os.replace(temporary, path)


def session_state_path() -> Path:
    from VoiceCaptions.settings import default_settings_path

    return default_settings_path().with_name(SESSION_STATE_NAME)


def write_session_state(state: str, path: Path | None = None, *, now=None) -> None:
    """Publish only the local session enum and its transition time.

    No guild, channel, member, message, or caption data belongs in this file.
    Failure is deliberately best effort because status bookkeeping must never
    interrupt captions.
    """
    if state not in KNOWN_SESSION_STATES:
        state = "unknown"
    target = path or session_state_path()
    observed_at = float((now or time.time)())
    try:
        _atomic_json(
            target,
            {
                "observed_at": observed_at,
                "schema": SESSION_STATE_SCHEMA,
                "state": state,
            },
        )
    except OSError:
        pass


def read_session_state(path: Path | None = None) -> SessionState | None:
    target = path or session_state_path()
    try:
        payload = json.loads(target.read_text(encoding="ascii"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or set(payload) != {
        "observed_at", "schema", "state"
    }:
        return None
    observed_at = payload.get("observed_at")
    state = payload.get("state")
    if (
        payload.get("schema") != SESSION_STATE_SCHEMA
        or state not in KNOWN_SESSION_STATES
        or isinstance(observed_at, bool)
        or not isinstance(observed_at, (int, float))
        or observed_at < 0
    ):
        return None
    return SessionState(state=state, observed_at=float(observed_at))


def update_endpoint(server_url: str) -> str:
    parts = urlsplit(server_url)
    if parts.scheme == "wss":
        scheme = "https"
    elif parts.scheme == "ws" and parts.hostname in {"127.0.0.1", "::1", "localhost"}:
        scheme = "http"
    else:
        raise AutomaticUpdateError("The saved guild server cannot check for updates.")
    return urlunsplit((scheme, parts.netloc, "/v1/update", "", ""))


def fetch_update_offer(
    credential_store: DeviceCredentialStore,
    *,
    current_version: str = CLIENT_VERSION,
    opener=None,
    timeout: float = 30.0,
) -> dict[str, Any] | None:
    """Fetch release metadata without crossing the caption privacy gate."""
    try:
        credentials = credential_store.load()
    except Exception as error:
        raise AutomaticUpdateError("Vemal's Ears could not read its saved pairing.") from error
    if credentials is None:
        return None
    request = urllib.request.Request(
        update_endpoint(credentials.server_url),
        headers={
            "Authorization": "Bearer " + credentials.device_token,
            "X-Vemals-Ears-Device-ID": credentials.device_id,
            "X-Vemals-Ears-Client-Version": current_version,
            "User-Agent": "VemalsEars-Updater/" + current_version,
        },
    )
    fetch = opener or urllib.request.build_opener(_NoRedirect()).open
    try:
        with fetch(request, timeout=timeout) as response:
            body = response.read(MAX_MANIFEST_BYTES + 1)
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as error:
        raise AutomaticUpdateError("Vemal's Ears could not check for updates.") from error
    if len(body) > MAX_MANIFEST_BYTES:
        raise AutomaticUpdateError("The guild server returned an invalid update response.")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise AutomaticUpdateError("The guild server returned an invalid update response.") from error
    if (
        not isinstance(payload, dict)
        or set(payload) != {"type", "update"}
        or payload.get("type") != "update_manifest"
    ):
        raise AutomaticUpdateError("The guild server returned an invalid update response.")
    if payload.get("update") is None:
        return None
    from GuildHub.client import is_newer_update_version, validate_update_offer

    offer = validate_update_offer({"update": payload["update"]})
    if offer is None:
        raise AutomaticUpdateError("The guild server returned an invalid update response.")
    if not is_newer_update_version(offer["version"], current_version):
        return None
    return offer


def _publisher_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    if not value or len(value) > _PUBLISHER_LENGTH:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return None
    return value


def load_update_publishers(
    app_root: Path,
    *,
    current_version: str = CLIENT_VERSION,
) -> tuple[str, ...]:
    """Read the trust identity installed by a signed VE installer."""
    if app_root.name.casefold() != "app":
        return ()
    try:
        payload = json.loads(
            (app_root.parent / "payload-manifest.json").read_text(encoding="ascii")
        )
    except (OSError, ValueError):
        return ()
    trust = payload.get("update_trust") if isinstance(payload, dict) else None
    publishers = trust.get("authenticode_publishers") if isinstance(trust, dict) else None
    if (
        payload.get("schema") != 1
        or payload.get("product") != "Vemal's Ears"
        or payload.get("version") != current_version
        or not isinstance(trust, dict)
        or set(trust) != {"authenticode_publishers", "schema"}
        or trust.get("schema") != TRUST_SCHEMA
        or not isinstance(publishers, list)
        or not 1 <= len(publishers) <= _PUBLISHER_LIMIT
    ):
        return ()
    clean = tuple(_publisher_text(value) for value in publishers)
    if any(value is None for value in clean) or len(set(clean)) != len(clean):
        return ()
    return tuple(value for value in clean if value is not None)


def _file_digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(block)
            digest.update(block)
    return size, digest.hexdigest()


def verify_staged_bytes(path: Path, offer: dict[str, Any]) -> None:
    try:
        with path.open("rb") as stream:
            magic = stream.read(2)
        size, digest = _file_digest(path)
    except OSError as error:
        raise AutomaticUpdateError("The staged update is unavailable.") from error
    if magic != b"MZ" or size != offer["bytes"] or digest != offer["sha256"]:
        raise AutomaticUpdateError("The staged update did not pass verification.")


def _numeric_version(value: str) -> tuple[int, int, int, int] | None:
    parts = value.strip().split(".")
    if not 3 <= len(parts) <= 4 or not all(_VERSION_PART.fullmatch(part) for part in parts):
        return None
    numbers = [int(part) for part in parts]
    if any(number > 65535 for number in numbers):
        return None
    numbers.extend([0] * (4 - len(numbers)))
    return (numbers[0], numbers[1], numbers[2], numbers[3])


def windows_file_version(path: Path) -> tuple[int, int, int, int]:
    try:
        import win32api

        info = win32api.GetFileVersionInfo(str(path), "\\")
        ms = int(info["FileVersionMS"])
        ls = int(info["FileVersionLS"])
    except (ImportError, OSError, KeyError, TypeError, ValueError) as error:
        raise AutomaticUpdateError("The update installer has no valid embedded version.") from error
    return (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)


def verify_authenticode_publisher(
    path: Path,
    publishers: tuple[str, ...],
    *,
    script: Path,
    runner=subprocess.run,
) -> None:
    if not publishers or not script.is_file():
        raise AutomaticUpdateError("Automatic installation is unavailable for this build.")
    for publisher in publishers:
        powershell = (
            Path(os.environ.get("SystemRoot", "C:/Windows"))
            / "System32/WindowsPowerShell/v1.0/powershell.exe"
        )
        try:
            result = runner(
                [
                    str(powershell),
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script),
                    "-FilePath",
                    str(path),
                    "-ExpectedPublisher",
                    publisher,
                ],
                capture_output=True,
                text=True,
                timeout=60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return
    raise AutomaticUpdateError(
        "The update is not signed by the trusted Vemal's Ears publisher."
    )


def verify_automatic_installer(
    path: Path,
    offer: dict[str, Any],
    publishers: tuple[str, ...],
    *,
    script: Path,
    signature_verifier=verify_authenticode_publisher,
    version_reader=windows_file_version,
) -> None:
    verify_staged_bytes(path, offer)
    expected = _numeric_version(offer["version"])
    if expected is None or version_reader(path) != expected:
        raise AutomaticUpdateError(
            "The update installer's embedded version does not match its offer."
        )
    signature_verifier(path, publishers, script=script)


def verify_manual_installer(
    path: Path,
    offer: dict[str, Any],
    *,
    version_reader=windows_file_version,
) -> None:
    """Bind an interactive installer to the advertised release version."""
    verify_staged_bytes(path, offer)
    expected = _numeric_version(offer["version"])
    if expected is None or version_reader(path) != expected:
        raise AutomaticUpdateError(
            "The update installer's embedded version does not match its offer."
        )


def staged_update_path(offer: dict[str, Any], state_root: Path) -> Path:
    version = offer["version"]
    safe_version = "".join(character if character.isalnum() or character in ".-" else "_"
                           for character in version)
    return state_root / "updates" / (
        safe_version + "-" + offer["sha256"][:16] + ".exe"
    )


def prune_staged_updates(state_root: Path, keep: Path | None = None) -> None:
    """Keep only the verified installer for the current advertised release."""
    update_root = state_root / "updates"
    try:
        entries = tuple(update_root.iterdir())
    except OSError:
        return
    for entry in entries:
        if keep is not None and (
            entry == keep or entry == keep.with_name(keep.name + ".part")
        ):
            continue
        if not _STAGED_FILE.fullmatch(entry.name):
            continue
        try:
            entry.unlink(missing_ok=True)
        except OSError:
            pass


def relaunch_state_path(state_root: Path) -> Path:
    return state_root / RELAUNCH_STATE_NAME


def write_relaunch_state(
    state_root: Path,
    version: str,
    window_state: str,
    *,
    now=None,
) -> Path:
    if window_state not in {"hidden", "minimized", "visible"}:
        window_state = "visible"
    target = relaunch_state_path(state_root)
    _atomic_json(
        target,
        {
            "created_at": float((now or time.time)()),
            "schema": RELAUNCH_STATE_SCHEMA,
            "version": version,
            "window_state": window_state,
        },
    )
    return target


def consume_relaunch_state(
    state_root: Path,
    *,
    current_version: str = CLIENT_VERSION,
    now=None,
) -> str | None:
    target = relaunch_state_path(state_root)
    try:
        payload = json.loads(target.read_text(encoding="ascii"))
    except (OSError, ValueError):
        payload = None
    finally:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
    current_time = float((now or time.time)())
    if (
        not isinstance(payload, dict)
        or set(payload) != {"created_at", "schema", "version", "window_state"}
        or payload.get("schema") != RELAUNCH_STATE_SCHEMA
        or payload.get("version") != current_version
        or payload.get("window_state") not in {"hidden", "minimized", "visible"}
        or isinstance(payload.get("created_at"), bool)
        or not isinstance(payload.get("created_at"), (int, float))
        or payload["created_at"] < 0
        or current_time < payload["created_at"]
        or current_time - payload["created_at"] > RELAUNCH_STATE_MAX_AGE_SECONDS
    ):
        return None
    return payload["window_state"]


class UpdateCoordinator:
    """Single-flight updater state machine, driven by the Tk event loop."""

    def __init__(
        self,
        credential_store: DeviceCredentialStore,
        *,
        app_root: Path,
        state_root: Path,
        installed: bool,
        current_version: str = CLIENT_VERSION,
        fetch_offer=fetch_update_offer,
        download=None,
        automatic_verifier=verify_automatic_installer,
        manual_verifier=verify_manual_installer,
        clock=time.time,
        jitter: Callable[[], int] | None = None,
    ) -> None:
        self.credential_store = credential_store
        self.app_root = app_root
        self.state_root = state_root
        self.installed = installed
        self.current_version = current_version
        self.fetch_offer = fetch_offer
        self.download = download
        self.automatic_verifier = automatic_verifier
        self.manual_verifier = manual_verifier
        self.clock = clock
        self.publishers = (
            load_update_publishers(app_root, current_version=current_version)
            if installed else ()
        )
        self.signature_script = app_root / "GuildHub" / "verify_update.ps1"
        self.offer: dict[str, Any] | None = None
        self.staged: Path | None = None
        self.auto_trusted = False
        self.busy = False
        self.error = ""
        self.launched_at = 0.0
        self.stage_retry_at = 0.0
        self.apply_retry_at = 0.0
        self.manual_requested = False
        self.auto_suppressed = False
        self._launched_automatic: bool | None = None
        self.remote_authoritative = False
        self._thread: threading.Thread | None = None
        self._results: queue.Queue[_WorkerResult] = queue.Queue()
        spread = (jitter or (lambda: secrets.randbelow(91)))()
        self.next_check = self.clock() + INITIAL_CHECK_SECONDS + max(0, min(90, spread))

    @property
    def automatic_available(self) -> bool:
        return self.installed and bool(self.publishers)

    def offer_from_caption_client(self, offer: dict[str, Any] | None) -> None:
        if self.remote_authoritative or offer is None:
            return
        self._accept_offer(offer)

    def request_check_now(self) -> None:
        self.next_check = min(self.next_check, self.clock())

    def request_manual_install(self) -> None:
        if self.offer is None or self.launched_at:
            return
        self.manual_requested = True
        self.error = ""
        if self.staged is not None and not self.busy:
            self._start_preflight(automatic=False)
        elif not self.busy:
            self.stage_retry_at = 0.0
            self._start_stage(self.offer)

    def _start(self, operation: str, identity, function: Callable[[], Any]) -> None:
        if self.busy:
            return
        self.busy = True
        self.error = ""

        def run() -> None:
            try:
                value = function()
                result = _WorkerResult(operation, identity, value=value)
            except Exception as error:
                result = _WorkerResult(
                    operation,
                    identity,
                    error=str(error),
                    invalidates_stage=isinstance(error, _InstallerVerificationError),
                )
            self._results.put(result)

        self._thread = threading.Thread(
            target=run,
            name="vemals-ears-updater-" + operation,
            daemon=True,
        )
        self._thread.start()

    def _start_check(self) -> None:
        self._start(
            "check",
            None,
            lambda: self.fetch_offer(
                self.credential_store,
                current_version=self.current_version,
            ),
        )

    def _start_stage(self, offer: dict[str, Any]) -> None:
        identity = _offer_identity(offer)
        target = staged_update_path(offer, self.state_root)

        def stage():
            try:
                verify_staged_bytes(target, offer)
            except AutomaticUpdateError:
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass
                downloader = self.download
                if downloader is None:
                    from GuildHub.client import download_verified_update

                    downloader = download_verified_update
                downloader(offer, target)
                verify_staged_bytes(target, offer)
            try:
                if self.automatic_available:
                    self.automatic_verifier(
                        target,
                        offer,
                        self.publishers,
                        script=self.signature_script,
                    )
                    trusted = True
                else:
                    self.manual_verifier(target, offer)
                    trusted = False
            except Exception:
                # A build that carries publisher trust never falls back to a
                # clickable unsigned installer. A failed embedded-version check
                # is equally final for an unsigned/manual build.
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            prune_staged_updates(self.state_root, target)
            return target, trusted

        self._start("stage", identity, stage)

    def _start_preflight(self, *, automatic: bool) -> None:
        if self.offer is None or self.staged is None:
            return
        offer = dict(self.offer)
        target = self.staged
        identity = _offer_identity(offer)

        def preflight():
            latest = self.fetch_offer(
                self.credential_store,
                current_version=self.current_version,
            )
            if latest is None or _offer_identity(latest) != identity:
                return _PreflightResult(latest=latest, launch=None)
            try:
                verify_staged_bytes(target, offer)
                # The installed manifest decides the trust policy. A member click
                # cannot downgrade an install that pins an Authenticode publisher.
                if self.automatic_available:
                    self.automatic_verifier(
                        target,
                        offer,
                        self.publishers,
                        script=self.signature_script,
                    )
                else:
                    self.manual_verifier(target, offer)
            except Exception as error:
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass
                message = str(error) or "The staged update did not pass verification."
                raise _InstallerVerificationError(message) from error
            return _PreflightResult(
                latest=latest,
                launch=LaunchRequest(target, offer["version"], automatic),
            )

        self._start("auto_preflight" if automatic else "manual_preflight", identity, preflight)

    def _accept_offer(self, offer: dict[str, Any] | None) -> None:
        if offer is None:
            self.offer = None
            self.staged = None
            self.auto_trusted = False
            self.manual_requested = False
            self.stage_retry_at = 0.0
            self.apply_retry_at = 0.0
            self.auto_suppressed = False
            if not self.busy:
                prune_staged_updates(self.state_root)
            return
        identity = _offer_identity(offer)
        if self.offer is not None and _offer_identity(self.offer) == identity:
            return
        self.offer = dict(offer)
        self.staged = None
        self.auto_trusted = False
        self.manual_requested = False
        self.stage_retry_at = 0.0
        self.apply_retry_at = 0.0
        self.auto_suppressed = False
        if not self.busy:
            prune_staged_updates(
                self.state_root,
                staged_update_path(self.offer, self.state_root),
            )
            self._start_stage(self.offer)

    def _drain(self) -> LaunchRequest | None:
        launch = None
        while True:
            try:
                result = self._results.get_nowait()
            except queue.Empty:
                break
            self.busy = False
            self._thread = None
            now = self.clock()
            if result.operation == "check":
                if result.error:
                    self.error = result.error
                    self.next_check = now + FAILED_CHECK_SECONDS
                else:
                    self.remote_authoritative = True
                    self.next_check = now + HEALTHY_CHECK_SECONDS
                    self._accept_offer(result.value)
                continue
            if result.identity is None or self.offer is None:
                continue
            if result.identity != _offer_identity(self.offer):
                continue
            if result.error:
                self.error = result.error
                self.manual_requested = False
                if result.operation == "stage":
                    self.stage_retry_at = now + FAILED_CHECK_SECONDS
                elif result.operation in {"auto_preflight", "manual_preflight"}:
                    if result.invalidates_stage:
                        self.staged = None
                        self.auto_trusted = False
                        self.stage_retry_at = now + FAILED_CHECK_SECONDS
                    else:
                        self.apply_retry_at = now + FAILED_CHECK_SECONDS
                continue
            if result.operation == "stage":
                self.staged, self.auto_trusted = result.value
                self.stage_retry_at = 0.0
                self.apply_retry_at = 0.0
                if self.manual_requested:
                    self._start_preflight(automatic=False)
                continue
            if result.operation in {"auto_preflight", "manual_preflight"}:
                self.manual_requested = False
                outcome = result.value
                self.remote_authoritative = True
                self.next_check = now + HEALTHY_CHECK_SECONDS
                if outcome.launch is None:
                    self._accept_offer(outcome.latest)
                    continue
                launch = outcome.launch
        return launch

    def tick(self, *, safe_to_apply: bool) -> LaunchRequest | None:
        launch = self._drain()
        now = self.clock()
        if self.launched_at:
            if now - self.launched_at >= INSTALLER_TIMEOUT_SECONDS:
                self.launched_at = 0.0
                self.busy = False
                self.error = "The installer did not finish; VE kept running."
                if self._launched_automatic:
                    self.apply_retry_at = now + FAILED_CHECK_SECONDS
                else:
                    # An interactive installer may still be open or may have
                    # been cancelled. Never turn that click into a silent retry.
                    self.auto_suppressed = True
                self._launched_automatic = None
            return launch
        if launch is not None:
            if launch.automatic and not safe_to_apply:
                self.apply_retry_at = now + BUSY_RETRY_SECONDS
                return None
            return launch
        if not self.busy and now >= self.next_check:
            self._start_check()
        elif (
            not self.busy
            and self.offer is not None
            and self.staged is None
            and now >= self.stage_retry_at
        ):
            self._start_stage(self.offer)
        elif (
            not self.busy
            and safe_to_apply
            and now >= self.apply_retry_at
            and not self.auto_suppressed
            and self.auto_trusted
            and self.offer is not None
            and self.staged is not None
        ):
            self._start_preflight(automatic=True)
        return None

    def mark_launched(self, *, automatic: bool) -> None:
        self.launched_at = self.clock()
        self.busy = True
        self._launched_automatic = bool(automatic)

    def mark_launch_failed(self, message: str, *, automatic: bool) -> None:
        self.launched_at = 0.0
        self.busy = False
        self.error = message
        self._launched_automatic = None
        if automatic:
            self.apply_retry_at = self.clock() + FAILED_CHECK_SECONDS
        else:
            self.auto_suppressed = True
