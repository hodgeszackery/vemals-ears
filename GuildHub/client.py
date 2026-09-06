"""Thin Vemal's Ears pairing and live-caption client.

Only validated caption text reaches the existing overlay. Enrollment codes and
device tokens remain in memory or Windows Credential Manager and are never
included in status output or exception text.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit

try:
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed, WebSocketException
except ImportError:  # pragma: no cover - exercised by the packaging tests.
    # Bound to placeholders rather than left undefined, so every `except`
    # clause below still parses and simply never matches. The real report
    # happens once, at the point of use, where it can name the package.
    connect = None

    class ConnectionClosed(Exception):
        """Placeholder: websockets is not installed."""

    class WebSocketException(Exception):
        """Placeholder: websockets is not installed."""

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from GuildHub.credentials import (
    DeviceCredentialStore,
    DeviceCredentialStoreError,
    DeviceCredentialValidationError,
    DeviceCredentials,
    WindowsDeviceCredentialStore,
    normalize_server_url,
    validate_device_credentials,
)
from GuildHub.pairing import (
    DEFAULT_PAIR_TIMEOUT_SECONDS,
    DEEP_LINK_SCHEME,
    DEEP_LINK_SCHEMES,
    DOWNTIME_DEEP_LINK_SCHEME,
    GuildHubClientError,
    GuildHubPairingCancelledError,
    GuildHubPairingConfirmationError,
    PairLink,
    _fetch_pairing_config,
    _is_server_error,
    _pair_response,
    _require_subprotocol,
    _safe_code,
    _socket_url,
    pair_device,
    parse_join_link,
    parse_pair_link,
    resolve_pair_link,
)
from GuildHub.protocol import (
    CAPTION_SUBPROTOCOLS,
    CAPTION_PATH,
    MAX_CURSOR,
    MAX_STREAM_ID,
    MAX_TOKEN,
    MAX_WIRE_BYTES,
    PAIR_PATH,
    QUICK_CHAT_PATH,
    QUICK_CHAT_PROFILE_PATH,
    SUBPROTOCOL,
    VIEWER_LINK_PATH,
    GuildHubProtocolError,
    QuickChatAcknowledgement,
    QuickChatProfile,
    decode_message,
    encode_message,
    parse_caption_envelope,
    parse_cursor_advance,
    parse_pair_request,
    parse_quick_chat,
    parse_quick_chat_acknowledgement,
    parse_quick_chat_profile,
    parse_quick_chat_profile_request,
    parse_viewer_link_request,
    parse_viewer_link_response,
)
from VoiceCaptions.events import (
    CaptionProtocolError,
    ClearEvent,
    ProtocolEvent,
    SessionEvent,
    parse_event,
)
from VoiceCaptions.member_overlay import OverlaySink

# Re-exported so every existing GuildHub.client.CLIENT_VERSION reader
# keeps working. Absolute import, not relative: this module is run
# directly as a script by the member install
# (pythonw.exe app/GuildHub/client.py --watch), and a relative import
# raises ImportError under __main__.
from GuildHub.version import CLIENT_VERSION
DEFAULT_AUTH_TIMEOUT_SECONDS = 15.0
DEFAULT_MAX_RETRIES = 6
DEFAULT_RETRY_INITIAL_SECONDS = 0.5
DEFAULT_RETRY_MAX_SECONDS = 8.0
DEFAULT_WATCH_POLL_SECONDS = 2.0

_SAFE_CODE = re.compile(r"^[A-Za-z0-9_-]+$")


class GuildHubAuthenticationError(GuildHubClientError):
    """The saved device is no longer authorized."""


class GuildHubMaintenanceError(GuildHubClientError):
    """The relay is restarting on purpose. Not a fault, and not the
    viewer's problem to solve - it resolves itself."""


class GuildHubVoiceChannelRequiredError(GuildHubClientError):
    """The linked Discord user is not in the active voice channel."""


class CaptionSink(Protocol):
    def send(self, event: ProtocolEvent) -> bool: ...


class ClosableCaptionSink(CaptionSink, Protocol):
    def close(self) -> None: ...


def _require_websockets() -> None:
    """Fail with something a member can act on, not a traceback.

    The bundle ships no third-party packages, so this is the first thing
    a member hits if the installer did not vendor websockets. Raised as a
    GuildHubClientError because every caller already reports that one.
    """
    if connect is None:
        raise GuildHubClientError(
            "Vemal's Ears needs the websockets package, which is not "
            "installed. Reinstall with the Vemal's Ears installer, or "
            "from a source checkout run: python -m pip install -r "
            "GuildHub/requirements-client.txt"
        )


def _require_caption_subprotocol(socket: Any) -> str:
    subprotocol = getattr(socket, "subprotocol", None)
    if subprotocol not in CAPTION_SUBPROTOCOLS:
        raise GuildHubClientError(
            "The guild server did not negotiate a caption protocol."
        )
    return subprotocol

# This is the live signal. A notice frame cannot be used for a shutdown:
# a message sent before close() is discarded by the client once the close
# handshake completes, drain or no drain, so the relay closes and says it
# in the close frame instead.
#
# 1001 is "going away" - the code a server sends when it is shutting down
# on purpose. 1012 and 1013 are the service-restart and try-again codes a
# proxy in front of the relay may substitute; all three mean "come back",
# none of them mean "something is wrong with you".
PLANNED_CLOSE_CODES = frozenset({1001, 1012, 1013})


def _is_planned_close(closed: BaseException) -> bool:
    for attribute in ("rcvd", "sent"):
        frame = getattr(closed, attribute, None)
        if frame is not None and getattr(frame, "code", None) in PLANNED_CLOSE_CODES:
            return True
    if hasattr(closed, "rcvd") or hasattr(closed, "sent"):
        # A real websockets exception carries the code on its frames, and
        # its own .code attribute is deprecated. Having looked there and
        # found nothing, this close was not a planned one.
        return False
    return getattr(closed, "code", None) in PLANNED_CLOSE_CODES


def _session_snapshot(payload: Any) -> SessionEvent | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise GuildHubClientError(
            "The guild server returned an invalid authentication response."
        )
    required = {"type", "state"}
    if set(payload) not in (required, required | {"message"}):
        raise GuildHubClientError(
            "The guild server returned an invalid authentication response."
        )
    if "message" in payload and not isinstance(payload["message"], str):
        raise GuildHubClientError(
            "The guild server returned an invalid authentication response."
        )
    try:
        event = parse_event(payload)
    except CaptionProtocolError:
        raise GuildHubClientError(
            "The guild server returned an invalid authentication response."
        ) from None
    if not isinstance(event, SessionEvent):
        raise GuildHubClientError(
            "The guild server returned an invalid authentication response."
        )
    return event


def _authenticated_response(payload: Any) -> tuple[str, int]:
    # "update" is optional: the relay includes it only when the operator
    # has published a build and advertised its version. This exact-shape
    # check did NOT allow that, so the first time a version was ever
    # advertised, the offer broke authentication for every member - the
    # feature that exists to ship fixes stopped anyone connecting. Same
    # failure as the relay's caption check rejecting wow_class, from the
    # other direction, in the same evening: two halves of a wire contract
    # edited on one side only.
    required = {"type", "stream_id", "cursor"}
    optional = {"update", "session"}
    if (
        not isinstance(payload, dict)
        or not required.issubset(payload)
        or not set(payload).issubset(required | optional)
    ):
        raise GuildHubClientError("The guild server returned an invalid authentication response.")
    stream_id = payload.get("stream_id")
    cursor = payload.get("cursor")
    if (
        payload.get("type") != "authenticated"
        or not isinstance(stream_id, str)
        or not stream_id
        or len(stream_id) > MAX_STREAM_ID
        or not _SAFE_CODE.fullmatch(stream_id)
        or isinstance(cursor, bool)
        or not isinstance(cursor, int)
    ):
        raise GuildHubClientError("The guild server returned an invalid authentication response.")
    if cursor < 0 or cursor > MAX_CURSOR:
        raise GuildHubClientError("The guild server returned an invalid authentication response.")
    # Validate the optional snapshot here even though the caller reads it
    # separately. Authentication succeeds only when the whole frame is safe.
    if "session" in payload and _session_snapshot(payload["session"]) is None:
        raise GuildHubClientError(
            "The guild server returned an invalid authentication response."
        )
    return stream_id, cursor


MAX_UPDATE_BYTES = 200 * 1024 * 1024
_SHA256_TEXT = re.compile(r"^[0-9a-f]{64}$")
_VERSION_PART = re.compile(r"^[0-9]{1,6}$")


def _version_order(value: str):
    """Dotted integers as a comparable tuple, or None if it is not one.

    ASCII digits only, on purpose: str.isdigit() is true for characters
    like the Arabic-Indic numerals, and a version that sorts by
    surprising rules is worse than one that simply refuses to sort.

    None means "these two cannot be ordered", and the caller then
    declines to guess rather than inventing a ranking.
    """
    parts = value.strip().split(".")
    if not 1 <= len(parts) <= 4:
        return None
    if not all(_VERSION_PART.fullmatch(part) for part in parts):
        return None
    return tuple(int(part) for part in parts)


def parse_update_offer(payload: Any, current_version: str = CLIENT_VERSION):
    """Read an update advertisement, or None if there is nothing usable.

    Deliberately silent about malformed offers. This arrives on the
    authenticated channel, so a bad one means the relay is confused or
    someone is playing, and neither is worth interrupting a member mid
    raid to report. The check that matters happens later, against the
    bytes actually downloaded.
    """
    offer = payload.get("update") if isinstance(payload, dict) else None
    if not isinstance(offer, dict):
        return None
    version = offer.get("version")
    digest = offer.get("sha256")
    url = offer.get("url")
    size = offer.get("bytes")
    if (
        not isinstance(version, str)
        or not version.strip()
        or len(version) > 32
        or not isinstance(digest, str)
        or not _SHA256_TEXT.fullmatch(digest)
        or not isinstance(url, str)
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size <= 0
        or size > MAX_UPDATE_BYTES
    ):
        return None
    # https only. An update is the one thing a member is invited to run,
    # so it does not travel in clear text however the relay asks.
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return None
    offered = version.strip()
    running = (current_version or "").strip()
    if offered == running:
        return None
    # Updates only ever go forward.
    #
    # This used to be the equality check alone, which meant "different"
    # counted as "newer". Roll the relay back after a bad release and
    # every member on the newer build is offered the older one. The
    # installer does refuse it - "a newer version of Vemal's Ears is
    # already installed" - so nothing is destroyed, but the banner
    # cannot clear: the versions still differ, so it re-offers the same
    # dead end every session, forever, after a 21 MB download each time.
    #
    # Xenon's agent updater carries the same guard for the same reason.
    #
    # Unorderable numbers fall through to the old behaviour deliberately:
    # if we cannot rank them we defer to the relay rather than silently
    # refusing every update a non-numeric scheme might offer.
    offered_order = _version_order(offered)
    running_order = _version_order(running)
    if (
        offered_order is not None
        and running_order is not None
        and offered_order <= running_order
    ):
        return None
    return {
        "version": version.strip(),
        "sha256": digest,
        "bytes": size,
        "url": url,
    }


UPDATE_NOTICE_NAME = "update-available.json"


def update_notice_path() -> Path:
    """Beside the overlay's own state, for the same reason it lives there.

    The caption client runs as a child of the companion window, so a small
    file is how the two agree on something. Nothing secret is in it - a
    version, a size, a public URL and a checksum - and it is rewritten or
    removed on every connection, so it cannot go stale while the client
    is running.
    """
    from VoiceCaptions.settings import default_settings_path

    return default_settings_path().with_name(UPDATE_NOTICE_NAME)


def write_update_notice(offer, path: Path | None = None) -> None:
    target = path or update_notice_path()
    try:
        if offer is None:
            target.unlink(missing_ok=True)
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(
            json.dumps(offer, ensure_ascii=True, sort_keys=True) + "\n",
            encoding="ascii",
        )
        os.replace(temporary, target)
    except OSError:
        # Telling somebody about an update is never worth losing their
        # captions over.
        pass


def read_update_notice(path: Path | None = None):
    """The offer the running client last saw, or None."""
    target = path or update_notice_path()
    try:
        payload = json.loads(target.read_text(encoding="ascii"))
    except (OSError, ValueError):
        return None
    # Re-validated on the way in: this file is written by a sibling
    # process, and the companion is about to download whatever it names.
    return parse_update_offer({"update": payload}, CLIENT_VERSION)


def download_verified_update(offer, destination: Path, *, fetch=None,
                             timeout: float = 300.0) -> Path:
    """Fetch the offered build and refuse anything that is not exactly it.

    The download route is public, so these bytes are not trusted because
    of where they came from - they are trusted because they hash to the
    value the relay named on the authenticated connection. An attacker
    who can rewrite the download cannot also rewrite that number without
    the device credential.

    Nothing is executed here, and the file only takes its real name after
    it has been checked, so a partial or wrong download can never be the
    thing a member is invited to run.
    """
    import urllib.request

    expected = offer["bytes"]
    opener = fetch or urllib.request.urlopen
    request = urllib.request.Request(
        offer["url"],
        headers={"User-Agent": "VemalsEars-Client/" + CLIENT_VERSION},
    )
    digest = hashlib.sha256()
    received = 0
    temporary = destination.with_name(destination.name + ".part")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with opener(request, timeout=timeout) as response, \
                open(temporary, "wb") as handle:
            while True:
                # Bounded by the advertised size, so a hostile or broken
                # response cannot fill a member's disk.
                block = response.read(min(1024 * 1024, expected - received + 1))
                if not block:
                    break
                received += len(block)
                if received > expected:
                    raise GuildHubClientError(
                        "The update download was larger than the guild "
                        "server said it would be, so it was discarded."
                    )
                digest.update(block)
                handle.write(block)
        if received != expected or digest.hexdigest() != offer["sha256"]:
            raise GuildHubClientError(
                "The update download did not match the guild server's "
                "checksum, so it was discarded."
            )
        os.replace(temporary, destination)
    except GuildHubClientError:
        temporary.unlink(missing_ok=True)
        raise
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise GuildHubClientError(
            "The update could not be downloaded: " + str(error)
        ) from None
    return destination


async def send_quick_chat(
    credential_store: DeviceCredentialStore,
    text: str,
    *,
    request_id: str,
    connector: Callable[..., Any] = connect,
    timeout: float = DEFAULT_AUTH_TIMEOUT_SECONDS,
) -> QuickChatAcknowledgement:
    """Post one typed line to Discord and the guild caption stream.

    The one-shot connection keeps the long-running caption receiver
    receive-only and avoids replacing its authenticated session. The request
    ID makes a retry safe when delivery succeeded but its acknowledgement was
    lost. Success is returned only after the Discord worker confirms its post
    and the relay publishes the matching caption.
    """
    _require_websockets()
    if timeout <= 0 or timeout > 60:
        raise ValueError("timeout must be between 0 and 60 seconds")
    try:
        credentials = credential_store.load()
    except DeviceCredentialStoreError:
        raise
    if credentials is None:
        raise GuildHubClientError(
            "Pair this computer with a Vemal's Ears guild server first."
        )

    request = {
        "type": "quick_chat",
        "device_id": credentials.device_id,
        "device_token": credentials.device_token,
        "request_id": request_id,
        "text": text,
    }
    try:
        parse_quick_chat(request)
    except GuildHubProtocolError as error:
        raise GuildHubClientError(str(error)) from None

    try:
        async with connector(
            _socket_url(credentials.server_url, QUICK_CHAT_PATH),
            subprotocols=[SUBPROTOCOL],
            max_size=MAX_WIRE_BYTES,
            open_timeout=timeout,
            close_timeout=3,
        ) as socket:
            _require_subprotocol(socket)
            await socket.send(encode_message(request))
            raw = await asyncio.wait_for(socket.recv(), timeout=timeout)
    except (GuildHubAuthenticationError, GuildHubClientError):
        raise
    except (OSError, TimeoutError, ConnectionClosed, WebSocketException):
        raise GuildHubClientError(
            "Quick chat could not reach the guild server. Try again."
        ) from None

    try:
        response = decode_message(raw)
        if _is_server_error(response):
            if response.get("code") == "authentication_failed":
                raise GuildHubAuthenticationError(
                    "This Vemal's Ears device is no longer authorized. Pair it again."
                )
            raise GuildHubClientError(str(response["message"]))
        acknowledgement = parse_quick_chat_acknowledgement(response)
        if acknowledgement.request_id != request_id:
            raise GuildHubClientError(
                "The guild server acknowledged a different quick-chat request."
            )
        return acknowledgement
    except (GuildHubAuthenticationError, GuildHubClientError):
        raise
    except GuildHubProtocolError:
        raise GuildHubClientError(
            "The guild server returned an invalid quick-chat response."
        ) from None


async def _quick_chat_profile_request(
    credential_store: DeviceCredentialStore,
    name: str | None,
    *,
    update: bool,
    connector: Callable[..., Any],
    timeout: float,
) -> QuickChatProfile:
    """Read or update the paired device's own typed-chat display name."""
    _require_websockets()
    if timeout <= 0 or timeout > 60:
        raise ValueError("timeout must be between 0 and 60 seconds")
    try:
        credentials = credential_store.load()
    except DeviceCredentialStoreError:
        raise
    if credentials is None:
        raise GuildHubClientError(
            "Pair this computer with a Vemal's Ears guild server first."
        )

    request = {
        "type": "quick_chat_profile",
        "device_id": credentials.device_id,
        "device_token": credentials.device_token,
    }
    if update:
        request["name"] = name
    try:
        parse_quick_chat_profile_request(request)
    except GuildHubProtocolError as error:
        raise GuildHubClientError(str(error)) from None

    try:
        async with connector(
            _socket_url(credentials.server_url, QUICK_CHAT_PROFILE_PATH),
            subprotocols=[SUBPROTOCOL],
            max_size=MAX_WIRE_BYTES,
            open_timeout=timeout,
            close_timeout=3,
        ) as socket:
            _require_subprotocol(socket)
            await socket.send(encode_message(request))
            raw = await asyncio.wait_for(socket.recv(), timeout=timeout)
    except (GuildHubAuthenticationError, GuildHubClientError):
        raise
    except (OSError, TimeoutError, ConnectionClosed, WebSocketException):
        raise GuildHubClientError(
            "The typed-chat profile could not reach the guild server. Try again."
        ) from None

    try:
        response = decode_message(raw)
        if _is_server_error(response):
            if response.get("code") == "authentication_failed":
                raise GuildHubAuthenticationError(
                    "This Vemal's Ears device is no longer authorized. Pair it again."
                )
            raise GuildHubClientError(str(response["message"]))
        profile = parse_quick_chat_profile(response)
        if update and profile.quick_chat_name is None:
            raise GuildHubProtocolError(
                "updated quick chat profile is missing its name"
            )
        return profile
    except (GuildHubAuthenticationError, GuildHubClientError):
        raise
    except GuildHubProtocolError:
        raise GuildHubClientError(
            "The guild server returned an invalid typed-chat profile response."
        ) from None


async def get_quick_chat_profile(
    credential_store: DeviceCredentialStore,
    *,
    connector: Callable[..., Any] = connect,
    timeout: float = DEFAULT_AUTH_TIMEOUT_SECONDS,
) -> QuickChatProfile:
    """Return the paired device's current server-owned display name."""
    return await _quick_chat_profile_request(
        credential_store,
        None,
        update=False,
        connector=connector,
        timeout=timeout,
    )


async def set_quick_chat_profile(
    credential_store: DeviceCredentialStore,
    name: str,
    *,
    connector: Callable[..., Any] = connect,
    timeout: float = DEFAULT_AUTH_TIMEOUT_SECONDS,
) -> QuickChatProfile:
    """Set only the paired device's typed-chat display name."""
    return await _quick_chat_profile_request(
        credential_store,
        name,
        update=True,
        connector=connector,
        timeout=timeout,
    )


async def create_viewer_link(
    credential_store: DeviceCredentialStore,
    *,
    connector: Callable[..., Any] = connect,
    timeout: float = DEFAULT_AUTH_TIMEOUT_SECONDS,
):
    """Create one short-lived code that links this device to a Discord user."""
    _require_websockets()
    if timeout <= 0 or timeout > 60:
        raise ValueError("timeout must be between 0 and 60 seconds")
    try:
        credentials = credential_store.load()
    except DeviceCredentialStoreError:
        raise
    if credentials is None:
        raise GuildHubClientError(
            "Pair this computer with a Vemal's Ears guild server first."
        )

    request = {
        "type": "viewer_link_request",
        "device_id": credentials.device_id,
        "device_token": credentials.device_token,
    }
    try:
        parse_viewer_link_request(request)
    except GuildHubProtocolError as error:
        raise GuildHubClientError(str(error)) from None

    try:
        async with connector(
            _socket_url(credentials.server_url, VIEWER_LINK_PATH),
            subprotocols=[SUBPROTOCOL],
            max_size=MAX_WIRE_BYTES,
            open_timeout=timeout,
            close_timeout=3,
        ) as socket:
            _require_subprotocol(socket)
            await socket.send(encode_message(request))
            raw = await asyncio.wait_for(socket.recv(), timeout=timeout)
    except (GuildHubAuthenticationError, GuildHubClientError):
        raise
    except (OSError, TimeoutError, ConnectionClosed, WebSocketException):
        raise GuildHubClientError(
            "The Discord link request could not reach the guild server. Try again."
        ) from None

    try:
        response = decode_message(raw)
        if _is_server_error(response):
            if response.get("code") == "authentication_failed":
                raise GuildHubAuthenticationError(
                    "This Vemal's Ears device is no longer authorized. Pair it again."
                )
            raise GuildHubClientError(str(response["message"]))
        return parse_viewer_link_response(response)
    except (GuildHubAuthenticationError, GuildHubClientError):
        raise
    except GuildHubProtocolError:
        raise GuildHubClientError(
            "The guild server returned an invalid Discord link response."
        ) from None


class GuildCaptionClient:
    """Reconnect one authenticated device to the memory-only caption stream."""

    def __init__(
        self,
        credential_store: DeviceCredentialStore,
        sink: CaptionSink,
        *,
        connector: Callable[..., Any] = connect,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_initial: float = DEFAULT_RETRY_INITIAL_SECONDS,
        retry_max: float = DEFAULT_RETRY_MAX_SECONDS,
        auth_timeout: float = DEFAULT_AUTH_TIMEOUT_SECONDS,
        connection_status: Callable[[bool], None] | None = None,
    ) -> None:
        if max_retries < 0 or max_retries > 20:
            raise ValueError("max_retries must be between 0 and 20")
        if retry_initial < 0 or retry_max < retry_initial or retry_max > 60:
            raise ValueError("retry delays are invalid")
        if auth_timeout <= 0 or auth_timeout > 60:
            raise ValueError("auth_timeout must be between 0 and 60 seconds")
        if connection_status is not None and not callable(connection_status):
            raise ValueError("connection_status must be callable")
        self._credential_store = credential_store
        self._sink = sink
        self._connector = connector
        self._max_retries = max_retries
        self._retry_initial = retry_initial
        self._retry_max = retry_max
        self._auth_timeout = auth_timeout
        self._connection_status = connection_status
        self._stop_event = asyncio.Event()
        self._active_socket: Any | None = None
        self._running = False
        self._stream_id = ""
        self._cursor = 0
        self._last_session_event: SessionEvent | None = None
        # Set when the relay announces a planned restart, cleared when a
        # connection authenticates again. While set, failures are expected
        # and must not spend the retry budget.
        self._in_maintenance = False
        # Flat, not exponential: a service restart finishes in seconds, so
        # the useful behaviour is to keep knocking politely at one pace.
        self._maintenance_retry = 5.0
        # Whether the relay ACCEPTED the most recent connection. The retry
        # budget counts consecutive connections the relay would not take -
        # not connections that took and then dropped. A quiet channel
        # delivers nothing for minutes at a time, and an accepted-then-
        # dropped socket is a fresh event, not evidence of a dead server.
        self._relay_accepted_last_connection = False

    @property
    def cursor(self) -> int:
        return self._cursor

    @property
    def stream_id(self) -> str:
        return self._stream_id

    def _report_connection_status(self, authenticated: bool) -> None:
        if self._connection_status is None:
            return
        try:
            self._connection_status(authenticated)
        except Exception:
            # UI status is best effort and must never stop caption delivery.
            pass

    def _send_session_state(self, event: SessionEvent) -> bool:
        if event == self._last_session_event:
            return True
        if not self._sink.send(event):
            return False
        self._last_session_event = event
        return True

    def _announce_maintenance(self) -> None:
        """Say why the captions stopped, in the words Shino asked for."""
        if not self._sink.send(ClearEvent()):
            raise GuildHubClientError(
                "The Vemal's Ears caption window is unavailable."
            )
        if not self._send_session_state(
            SessionEvent(
                state="paused",
                # Someone who cannot hear the room has no other way to
                # tell a deploy from a crash, and the difference decides
                # whether they wait or go looking for a fix.
                message="Paused for updates - captions resume by themselves.",
            )
        ):
            raise GuildHubClientError(
                "The Vemal's Ears caption window is unavailable."
            )

    def _enter_maintenance(self) -> None:
        """Show the pause once, then hold. Repeating it would blink."""
        if self._in_maintenance:
            return
        self._in_maintenance = True
        self._report_connection_status(False)
        self._announce_maintenance()

    def _deny_voice_channel_access(self) -> None:
        """Remove prior text before reporting the local access boundary."""
        if not self._sink.send(ClearEvent()):
            raise GuildHubClientError(
                "The Vemal's Ears caption window is unavailable."
            )
        if not self._send_session_state(
            SessionEvent(
                state="paused",
                # Name the cause and the cure. Silence here reads as a
                # crash to someone who cannot hear the room, and the
                # window stays up precisely so it can say this.
                message="Captions paused - you left the voice channel or "
                "have not linked your Vemal's Ears application.",
            )
        ):
            raise GuildHubClientError(
                "The Vemal's Ears caption window is unavailable."
            )

    async def stop(self) -> None:
        self._stop_event.set()
        socket = self._active_socket
        if socket is not None:
            try:
                await socket.close(code=1000, reason="Vemal's Ears stopped")
            except (OSError, ConnectionClosed, WebSocketException):
                pass

    async def _wait_for_retry(self, delay: float) -> bool:
        if self._stop_event.is_set():
            return False
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
        except TimeoutError:
            return True
        return False

    async def _consume_once(self, credentials: DeviceCredentials) -> int:
        delivered = 0
        authenticated = False
        voice_access_denied = False
        self._relay_accepted_last_connection = False
        requested_stream_id = self._stream_id
        requested_cursor = self._cursor
        async with self._connector(
            _socket_url(credentials.server_url, CAPTION_PATH),
            subprotocols=list(CAPTION_SUBPROTOCOLS),
            max_size=MAX_WIRE_BYTES,
            open_timeout=self._auth_timeout,
            close_timeout=3,
        ) as socket:
            self._active_socket = socket
            try:
                subprotocol = _require_caption_subprotocol(socket)
                await socket.send(
                    encode_message(
                        {
                            "type": "authenticate",
                            "device_id": credentials.device_id,
                            "device_token": credentials.device_token,
                            "last_stream_id": requested_stream_id,
                            "last_cursor": requested_cursor,
                            "client_version": CLIENT_VERSION,
                        }
                    )
                )
                raw = await asyncio.wait_for(
                    socket.recv(), timeout=self._auth_timeout
                )
                try:
                    response = decode_message(raw)
                except GuildHubProtocolError:
                    raise GuildHubClientError(
                        "The guild server returned an invalid authentication response."
                    ) from None
                if _is_server_error(response):
                    # Kept for a notice sent on a connection that stays
                    # open, which does deliver. Without it such a frame
                    # would fall through to "pair this device again" -
                    # the worst possible advice during a deploy.
                    if response.get("code") == "server_maintenance":
                        raise GuildHubMaintenanceError(
                            "The guild server is restarting."
                        )
                    if response.get("code") == "voice_channel_required":
                        voice_access_denied = True
                        self._deny_voice_channel_access()
                        raise GuildHubVoiceChannelRequiredError(
                            "Join the active Discord voice channel to view captions."
                        )
                    raise GuildHubAuthenticationError(
                        "This Vemal's Ears device is no longer authorized. Pair it again."
                    )
                server_stream_id, baseline_cursor = _authenticated_response(response)
                session_snapshot = _session_snapshot(response.get("session"))
                # Written on every connection, so the notice cannot
                # outlive the version it describes.
                write_update_notice(parse_update_offer(response))
                discontinuity = (
                    server_stream_id != requested_stream_id
                    or baseline_cursor != requested_cursor
                )
                if discontinuity:
                    if not self._sink.send(ClearEvent()):
                        raise GuildHubClientError(
                            "The Vemal's Ears caption window is unavailable."
                        )
                # A new relay reports the exact current session. An older
                # relay omits it, which is uncertainty - never proof that
                # nobody may be listening.
                if not self._send_session_state(
                    session_snapshot
                    or SessionEvent(
                        state="unknown",
                        message="Listening status unavailable",
                    )
                ):
                    raise GuildHubClientError(
                        "The Vemal's Ears caption window is unavailable."
                    )
                self._stream_id = server_stream_id
                self._cursor = baseline_cursor
                authenticated = True
                # Reaching this point IS the all-clear: the relay is back
                # and talking. Clearing here rather than on the next
                # caption means a quiet channel still ends the pause.
                self._in_maintenance = False
                # And the same all-clear for the retry budget. The pause
                # got this treatment when the deploy complaint was fixed;
                # the budget did not, so a quiet channel that dropped
                # seven times was reported as a server that could not be
                # reached, and each reconnect before that waited longer
                # than the last for a relay that was fine.
                self._relay_accepted_last_connection = True
                self._report_connection_status(True)

                async for raw_event in socket:
                    if self._stop_event.is_set():
                        break
                    try:
                        payload = decode_message(raw_event)
                        if _is_server_error(payload):
                            if payload.get("code") == "server_maintenance":
                                raise GuildHubMaintenanceError(
                                    "The guild server is restarting."
                                )
                            if payload.get("code") == "voice_channel_required":
                                voice_access_denied = True
                                self._deny_voice_channel_access()
                                raise GuildHubVoiceChannelRequiredError(
                                    "Join the active Discord voice channel to view captions."
                                )
                            raise GuildHubAuthenticationError(
                                "This Vemal's Ears device is no longer authorized. Pair it again."
                            )
                        if payload.get("type") == "cursor_advance":
                            advance = parse_cursor_advance(payload)
                            if advance.stream_id != self._stream_id:
                                raise GuildHubClientError(
                                    "The guild server changed caption streams unexpectedly."
                                )
                            if advance.cursor != self._cursor + 1:
                                raise GuildHubClientError(
                                    "The guild server sent a discontinuous caption stream."
                                )
                            self._cursor = advance.cursor
                            continue
                        envelope = parse_caption_envelope(
                            payload,
                            subprotocol=subprotocol,
                        )
                    except (
                        GuildHubAuthenticationError,
                        GuildHubVoiceChannelRequiredError,
                        GuildHubMaintenanceError,
                    ):
                        raise
                    except (GuildHubProtocolError, GuildHubClientError):
                        raise GuildHubClientError(
                            "The guild server sent an invalid caption message."
                        ) from None
                    if envelope.stream_id != self._stream_id:
                        raise GuildHubClientError(
                            "The guild server changed caption streams unexpectedly."
                        )
                    if envelope.cursor != self._cursor + 1:
                        raise GuildHubClientError(
                            "The guild server sent a discontinuous caption stream."
                        )
                    delivered_to_overlay = (
                        self._send_session_state(envelope.event)
                        if isinstance(envelope.event, SessionEvent)
                        else self._sink.send(envelope.event)
                    )
                    if not delivered_to_overlay:
                        raise GuildHubClientError(
                            "The Vemal's Ears caption window is unavailable."
                        )
                    self._cursor = envelope.cursor
                    delivered += 1
            finally:
                if authenticated:
                    if not self._stop_event.is_set() and not voice_access_denied:
                        self._send_session_state(
                            SessionEvent(
                                state="unknown",
                                message="Listening status unavailable",
                            )
                        )
                    self._report_connection_status(False)
                if self._active_socket is socket:
                    self._active_socket = None
        return delivered

    async def run(self) -> None:
        if self._running:
            raise GuildHubClientError("Vemal's Ears captions are already running.")
        if self._stop_event.is_set():
            raise GuildHubClientError("This Vemal's Ears caption client has stopped.")
        try:
            credentials = self._credential_store.load()
        except DeviceCredentialStoreError:
            raise
        if credentials is None:
            raise GuildHubClientError(
                "Pair this computer with a Vemal's Ears guild server first."
            )

        self._running = True
        failed_attempts = 0
        try:
            while not self._stop_event.is_set():
                delivered = 0
                try:
                    delivered = await self._consume_once(credentials)
                except GuildHubMaintenanceError:
                    self._enter_maintenance()
                except (GuildHubAuthenticationError, GuildHubClientError):
                    raise
                except asyncio.CancelledError:
                    raise
                except ConnectionClosed as closed:
                    # A relay that dies mid-frame has no chance to send an
                    # error first; the close code is the whole message.
                    if _is_planned_close(closed):
                        self._enter_maintenance()
                except (OSError, TimeoutError, WebSocketException):
                    pass
                if self._stop_event.is_set():
                    return
                if self._in_maintenance:
                    # A restart takes as long as it takes. Counting these
                    # attempts is what turned a deploy into "could not
                    # reconnect"; backing off exponentially is what left
                    # captions dark long after the relay came back.
                    if not await self._wait_for_retry(self._maintenance_retry):
                        return
                    continue
                # An accepted connection resets the budget whether or not a
                # caption ever arrived on it. Counting only delivered
                # captions made a quiet raid look like an outage: seven
                # unplanned drops with nobody speaking in between raised
                # "could not reconnect" against a relay that accepted every
                # one of them, tore the client down, and wiped the overlay.
                succeeded = bool(delivered) or self._relay_accepted_last_connection
                failed_attempts = 0 if succeeded else failed_attempts + 1
                if failed_attempts > self._max_retries:
                    raise GuildHubClientError(
                        "Vemal's Ears could not reconnect to the guild server."
                    )
                delay = min(
                    self._retry_initial * (2 ** max(0, failed_attempts - 1)),
                    self._retry_max,
                )
                if not await self._wait_for_retry(delay):
                    return
        finally:
            self._running = False
            socket = self._active_socket
            self._active_socket = None
            if socket is not None:
                try:
                    await socket.close(code=1000, reason="Vemal's Ears stopped")
                except (OSError, ConnectionClosed, WebSocketException):
                    pass


# What credential_store.load() can ACTUALLY raise. The Windows store
# converts OSError, json errors, unicode errors and its own validation
# error into DeviceCredentialStoreError - a RuntimeError subclass - so
# catching (OSError, ValueError) alone never fired, and the respawn
# loop it was written to stop ran exactly as before. The other two stay
# for stores that do not wrap: a test double, or a plain-file store.
CREDENTIAL_LOAD_ERRORS = (
    DeviceCredentialStoreError,
    DeviceCredentialValidationError,
    OSError,
    ValueError,
)


async def _watch_wait(stop_event: asyncio.Event, seconds: float) -> bool:
    if stop_event.is_set():
        return False
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except TimeoutError:
        return True
    return False


async def _wait_for_credential_change(
    credential_store: DeviceCredentialStore,
    current: DeviceCredentials,
    stop_event: asyncio.Event,
    poll_interval: float,
) -> bool:
    """Notice a new pairing without restarting the companion process."""
    while not stop_event.is_set():
        if not await _watch_wait(stop_event, poll_interval):
            return False
        try:
            latest = credential_store.load()
        except DeviceCredentialStoreError:
            continue
        if latest != current:
            return True
    return False


async def _wait_for_sink_death(
    sink: "ClosableCaptionSink",
    stopped: asyncio.Event,
    check_seconds: float = 2.0,
) -> bool:
    """True the moment a sink that can report liveness stops being alive.

    Sinks without an alive() simply never trigger this - the fakes in
    tests, and any future sink that has no child process to lose.
    """
    alive = getattr(sink, "alive", None)
    if not callable(alive):
        await stopped.wait()
        return False
    while not stopped.is_set():
        try:
            if alive() is not True:
                return True
        except Exception:  # noqa: BLE001
            return True
        try:
            await asyncio.wait_for(stopped.wait(), timeout=check_seconds)
        except (asyncio.TimeoutError, TimeoutError):
            continue
    return False


async def watch_captions(
    credential_store: DeviceCredentialStore,
    sink_factory: Callable[[], ClosableCaptionSink],
    *,
    client_factory: Callable[..., GuildCaptionClient] = GuildCaptionClient,
    stop_event: asyncio.Event | None = None,
    poll_interval: float = DEFAULT_WATCH_POLL_SECONDS,
    report: Callable[[str], None] = print,
) -> None:
    """Wait for pairing and keep the caption receiver available.

    This is the compatibility mode used by the unified companion. It polls only
    Credential Manager, never a plaintext marker file, and emits status text
    only when the state changes.
    """
    _require_websockets()

    if poll_interval < 0 or poll_interval > 60:
        raise ValueError("poll_interval must be between 0 and 60 seconds")
    stopped = stop_event if stop_event is not None else asyncio.Event()
    last_status = ""
    rejected_credentials: DeviceCredentials | None = None
    sink: ClosableCaptionSink | None = None
    sink_credentials: DeviceCredentials | None = None
    voice_channel_waiting = False

    def status_once(message: str) -> None:
        nonlocal last_status
        if message != last_status:
            report(message)
            last_status = message

    def close_sink() -> None:
        nonlocal sink, sink_credentials, voice_channel_waiting
        previous, sink = sink, None
        sink_credentials = None
        voice_channel_waiting = False
        if previous is not None:
            previous.close()

    try:
        while not stopped.is_set():
            try:
                credentials = credential_store.load()
            except CREDENTIAL_LOAD_ERRORS as error:
                # A broken saved pairing does not repair itself on respawn.
                status_once(
                    "Vemal's Ears could not read its saved pairing, so it "
                    "cannot start. Pair this computer again. "
                    f"({error})"
                )
                if not await _watch_wait(stopped, poll_interval):
                    return
                continue
            if credentials is None:
                close_sink()
                rejected_credentials = None
                status_once("Vemal's Ears is waiting for a pairing link.")
                if not await _watch_wait(stopped, poll_interval):
                    return
                continue
            if sink_credentials is not None and credentials != sink_credentials:
                # Never carry the previous guild's window into a new pairing.
                close_sink()
            if rejected_credentials is not None and credentials == rejected_credentials:
                status_once("Vemal's Ears needs a new pairing link.")
                if not await _watch_wait(stopped, poll_interval):
                    return
                continue

            if sink is None:
                status_once("Vemal's Ears is opening the caption window.")
                try:
                    sink = sink_factory()
                except OSError as error:
                    status_once(
                        "Vemal's Ears could not open the caption window; "
                        f"retrying shortly. ({error})"
                    )
                    if not await _watch_wait(stopped, poll_interval):
                        return
                    continue
                sink_credentials = credentials
                # Only a new window needs this initial uncertainty. Reusing
                # a denied window must keep its paused message unchanged.
                sink.send(
                    SessionEvent(
                        state="unknown",
                        message="Listening status unavailable",
                    )
                )
            keep_warning_during_retry = False

            def connection_status(connected: bool) -> None:
                nonlocal voice_channel_waiting
                if connected:
                    # The client has delivered the authoritative snapshot.
                    voice_channel_waiting = False
                elif voice_channel_waiting:
                    return
                status_once(
                    "Vemal's Ears is connected to guild captions."
                    if connected
                    else "Vemal's Ears is opening the caption window."
                )

            client = client_factory(
                credential_store,
                sink,
                connection_status=connection_status,
            )
            run_task = asyncio.create_task(client.run())
            stop_task = asyncio.create_task(stopped.wait())
            credential_task = asyncio.create_task(
                _wait_for_credential_change(
                    credential_store,
                    credentials,
                    stopped,
                    poll_interval,
                )
            )
            # Detect a dead overlay even when no new caption is arriving.
            sink_task = asyncio.create_task(_wait_for_sink_death(sink, stopped))
            try:
                done, _pending = await asyncio.wait(
                    {run_task, stop_task, credential_task, sink_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stop_task in done and stopped.is_set():
                    await client.stop()
                    await asyncio.gather(run_task, return_exceptions=True)
                    return
                if sink_task in done and sink_task.result():
                    status_once(
                        "The Vemal's Ears caption window closed; reopening it."
                    )
                    await client.stop()
                    await asyncio.gather(run_task, return_exceptions=True)
                    continue
                if credential_task in done:
                    if not credential_task.result():
                        await client.stop()
                        await asyncio.gather(run_task, return_exceptions=True)
                        return
                    rejected_credentials = None
                    await client.stop()
                    await asyncio.gather(run_task, return_exceptions=True)
                    status_once("Vemal's Ears is switching to the newly paired guild.")
                    continue
                stop_task.cancel()
                await asyncio.gather(stop_task, return_exceptions=True)
                try:
                    await run_task
                except GuildHubVoiceChannelRequiredError:
                    voice_channel_waiting = True
                    status_once(
                        "Vemal's Ears is paused until you rejoin the Discord voice channel."
                    )
                except GuildHubAuthenticationError:
                    voice_channel_waiting = False
                    rejected_credentials = credentials
                    status_once("Vemal's Ears needs a new pairing link.")
                except GuildHubClientError as error:
                    # Keep the known local pause during a failed rejoin
                    # attempt. A network error is not renewed permission.
                    if not voice_channel_waiting:
                        detail = str(error).strip()
                        status_once(
                            "Vemal's Ears could not use the guild connection: "
                            + (detail or type(error).__name__)
                            + " Retrying."
                        )
                keep_warning_during_retry = True
            finally:
                stop_task.cancel()
                credential_task.cancel()
                sink_task.cancel()
                if not run_task.done():
                    await client.stop()
                await asyncio.gather(
                    run_task, stop_task, credential_task, sink_task,
                    return_exceptions=True,
                )
                if keep_warning_during_retry:
                    if not voice_channel_waiting:
                        sink.send(
                            SessionEvent(
                                state="unknown",
                                message="Listening status unavailable",
                            )
                        )
                else:
                    close_sink()

            if not await _watch_wait(stopped, poll_interval):
                return
            if not voice_channel_waiting and credentials != rejected_credentials:
                close_sink()
            # Keep a paused window across rejoin attempts, or REC? while a
            # rejected pairing awaits replacement. Neither bypasses auth.
    finally:
        # Includes cancellation while waiting between access retries.
        close_sink()


async def _pair_cli(args: argparse.Namespace) -> int:
    store = WindowsDeviceCredentialStore()
    credentials = await pair_device(
        args.link,
        store,
        device_name=args.device_name,
        client_version=CLIENT_VERSION,
    )
    print("Vemal's Ears paired with " + credentials.guild_name + ".", flush=True)
    return 0


async def _captions_cli(_args: argparse.Namespace) -> int:
    store = WindowsDeviceCredentialStore()
    sink = OverlaySink.start()
    client = GuildCaptionClient(store, sink)
    print("Vemal's Ears caption window is ready.", flush=True)
    try:
        await client.run()
    finally:
        await client.stop()
        sink.close()
    return 0


async def _watch_cli(_args: argparse.Namespace) -> int:
    await watch_captions(WindowsDeviceCredentialStore(), OverlaySink.start)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pair and run the Vemal's Ears guild caption client."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    pair_parser = commands.add_parser("pair", help="Use a one-time pairing link.")
    pair_parser.add_argument("link", help="The vemalsears://pair link.")
    pair_parser.add_argument("--device-name", help="Name shown to the guild admin.")
    pair_parser.set_defaults(handler=_pair_cli)

    captions_parser = commands.add_parser(
        "vears", help="Open the live caption window."
    )
    captions_parser.set_defaults(handler=_captions_cli)

    watch_parser = commands.add_parser(
        "watch", help="Wait for pairing and keep captions available."
    )
    watch_parser.set_defaults(handler=_watch_cli)
    return parser


def main(argv: list[str] | None = None) -> int:
    command_argv = list(sys.argv[1:] if argv is None else argv)
    if command_argv == ["--watch"]:
        # Compatibility with the unified WoW companion service launcher.
        command_argv = ["watch"]
    args = build_parser().parse_args(command_argv)
    try:
        return asyncio.run(args.handler(args))
    except KeyboardInterrupt:
        return 0
    except (GuildHubClientError, DeviceCredentialStoreError, DeviceCredentialValidationError) as error:
        print("Vemal's Ears: " + str(error), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
