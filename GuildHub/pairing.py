"""Shared guild enrollment for Downtime and Vemal's Ears.

Only one-use enrollment links and validated device credentials pass this
boundary. It has no caption receiver, overlay, or voice runtime dependency.
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import parse_qsl, urlsplit

try:
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed, WebSocketException
except ImportError:  # pragma: no cover - validated by clean-bundle tests.
    connect = None

    class ConnectionClosed(Exception):
        """Placeholder when websockets is unavailable."""

    class WebSocketException(Exception):
        """Placeholder when websockets is unavailable."""

from GuildHub.credentials import (
    DeviceCredentialStore,
    DeviceCredentialStoreError,
    DeviceCredentialValidationError,
    DeviceCredentials,
    normalize_server_url,
    validate_device_credentials,
)
from GuildHub.protocol import (
    MAX_TOKEN,
    MAX_WIRE_BYTES,
    PAIR_PATH,
    SUBPROTOCOL,
    GuildHubProtocolError,
    decode_message,
    encode_message,
    parse_pair_request,
)
from GuildHub.version import CLIENT_VERSION

DEEP_LINK_SCHEME = "vemalsears"
DOWNTIME_DEEP_LINK_SCHEME = "downtime"
DEEP_LINK_SCHEMES = frozenset({DEEP_LINK_SCHEME, DOWNTIME_DEEP_LINK_SCHEME})
DEFAULT_PAIR_TIMEOUT_SECONDS = 15.0
_SAFE_CODE = re.compile(r"^[A-Za-z0-9_-]+$")


class GuildHubClientError(RuntimeError):
    """A guild client could not safely complete an operation."""


class GuildHubPairingCancelledError(GuildHubClientError):
    """The user declined to replace the active guild device."""


class GuildHubPairingConfirmationError(GuildHubClientError):
    """The local confirmation boundary could not safely make a decision."""


@dataclass(frozen=True, repr=False)
class PairLink:
    server_url: str
    enrollment_code: str

    def __repr__(self) -> str:
        return "PairLink(<redacted>)"

    __str__ = __repr__



def _require_websockets() -> None:
    """Fail with something a member can act on, not a traceback.

    The bundle ships no third-party packages, so this is the first thing
    a member hits if the installer did not vendor websockets. Raised as a
    GuildHubClientError because every caller already reports that one.
    """
    if connect is None:
        raise GuildHubClientError(
            "Guild pairing needs the websockets package, which is not "
            "installed. Reinstall the companion application, or "
            "from a source checkout run: python -m pip install -r "
            "GuildHub/requirements-client.txt"
        )

def _safe_code(value: object) -> str:
    if not isinstance(value, str):
        raise GuildHubClientError("The pairing link is missing its one-use code.")
    code = value.strip()
    if not code or len(code) > MAX_TOKEN or not _SAFE_CODE.fullmatch(code):
        raise GuildHubClientError("The pairing link has an invalid one-use code.")
    return code


def parse_pair_link(value: object) -> PairLink:
    """Parse a strict app pairing link, including legacy VE invitations."""

    if not isinstance(value, str) or not value.strip():
        raise GuildHubClientError("A guild pairing link is required.")
    raw = value.strip()
    try:
        parsed = urlsplit(raw)
    except ValueError:
        raise GuildHubClientError("The guild pairing link is invalid.") from None
    if parsed.scheme.lower() not in DEEP_LINK_SCHEMES:
        raise GuildHubClientError(
            "Paste either the invitation link from your guild administrator "
            "(https://.../join#...), downtime://, or vemalsears:// pairing link."
        )
    if parsed.username is not None or parsed.password is not None:
        raise GuildHubClientError("The pairing link cannot include user information.")
    if parsed.netloc.lower() != "pair" or parsed.path not in {"", "/"}:
        raise GuildHubClientError("The pairing link has an invalid action.")
    if parsed.fragment:
        raise GuildHubClientError("The pairing link cannot include a fragment.")
    try:
        fields = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=3,
        )
    except ValueError:
        raise GuildHubClientError("The pairing link query is invalid.") from None
    if len(fields) != 2 or {name for name, _value in fields} != {"server", "code"}:
        raise GuildHubClientError(
            "The pairing link must contain one server and one one-use code."
        )
    query = dict(fields)
    try:
        server_url = normalize_server_url(query["server"])
    except DeviceCredentialValidationError as error:
        raise GuildHubClientError(str(error)) from None
    return PairLink(server_url=server_url, enrollment_code=_safe_code(query["code"]))


def parse_join_link(value: object) -> tuple[str, str]:
    """Split an https join link into (origin, code), strictly.

    This is the link the operator console mints and the one a member
    is most likely to paste. http is accepted for loopback origins
    only, which is what the test harness uses; everything real is
    https, and the code must live in the fragment, where it never
    crosses the network.
    """
    if not isinstance(value, str) or not value.strip():
        raise GuildHubClientError("A guild invitation link is required.")
    raw = value.strip()
    try:
        parsed = urlsplit(raw)
    except ValueError:
        raise GuildHubClientError("The invitation link is invalid.") from None
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    loopback = host in {"127.0.0.1", "localhost", "::1"}
    if scheme != "https" and not (scheme == "http" and loopback):
        raise GuildHubClientError(
            "Paste either the invitation link from your guild administrator "
            "(https://.../join#...), downtime://, or vemalsears:// pairing link."
        )
    if parsed.username is not None or parsed.password is not None:
        raise GuildHubClientError("The invitation link cannot include user information.")
    if parsed.path.rstrip("/") != "/join" or parsed.query:
        raise GuildHubClientError("The invitation link has an invalid action.")
    code = _safe_code(parsed.fragment)
    origin = scheme + "://" + parsed.netloc
    return origin, code


def _fetch_pairing_config(origin: str, timeout: float) -> dict:
    """Ask the relay for its websocket URL - the join page publishes the
    same value in its data-server attribute, so nothing new is exposed.
    """
    import json as _json
    import urllib.request

    # A descriptive User-Agent, because edge providers block the default
    # Python-urllib agent outright - curl sails through while every real
    # member's pairing dialog would bounce off a 403.
    request = urllib.request.Request(
        origin + "/pairing-config",
        headers={"User-Agent": "VemalsEars-Client/" + CLIENT_VERSION},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(4096)
    return _json.loads(raw.decode("utf-8"))


async def resolve_pair_link(
    value: object,
    *,
    timeout: float = 10.0,
    config_fetch: Callable[[str, float], dict] | None = None,
) -> PairLink:
    """Accept either pairing form and return a strict PairLink.

    App links resolve locally. https join links cost one
    request to the SAME origin the member was handed - no third party
    enters the picture, and the enrollment code is not sent: only the
    public pairing config is fetched.
    """
    raw = value.strip() if isinstance(value, str) else ""
    if any(raw.lower().startswith(scheme + ":") for scheme in DEEP_LINK_SCHEMES):
        return parse_pair_link(raw)
    origin, code = parse_join_link(raw)
    fetch = config_fetch or _fetch_pairing_config
    try:
        payload = await asyncio.to_thread(fetch, origin, timeout)
    except GuildHubClientError:
        raise
    except OSError as error:
        detail = type(error).__name__
        code = getattr(error, "code", None)
        if code:
            detail = "HTTP " + str(code)
        raise GuildHubClientError(
            "Could not reach the guild server behind that invitation: " + detail
        ) from None
    except ValueError:
        raise GuildHubClientError("That link does not point at a Vemal's Ears server.") from None
    # Validated HERE, not in the fetcher, so an injected transport cannot
    # accidentally skip the one check that names the failure honestly.
    if not isinstance(payload, dict) or payload.get("service") != "vemals-ears":
        raise GuildHubClientError("That link does not point at a Vemal's Ears server.")
    try:
        server_url = normalize_server_url(str(payload.get("server_url", "")))
    except DeviceCredentialValidationError as error:
        raise GuildHubClientError(str(error)) from None
    return PairLink(server_url=server_url, enrollment_code=code)


def _pair_response(payload: Any, server_url: str) -> DeviceCredentials:
    if not isinstance(payload, dict) or set(payload) != {
        "type",
        "device_id",
        "device_token",
        "guild_id",
        "guild_name",
    }:
        raise GuildHubClientError("The guild server returned an invalid pairing response.")
    if payload.get("type") != "paired":
        raise GuildHubClientError("The guild server returned an invalid pairing response.")
    try:
        return validate_device_credentials(
            server_url,
            payload.get("device_id"),
            payload.get("device_token"),
            payload.get("guild_id"),
            payload.get("guild_name"),
        )
    except DeviceCredentialValidationError:
        raise GuildHubClientError("The guild server returned an invalid pairing response.") from None


def _is_server_error(payload: Any) -> bool:
    if not isinstance(payload, dict) or payload.get("type") != "error":
        return False
    if set(payload) != {"type", "code", "message"}:
        raise GuildHubClientError("The guild server returned an invalid error response.")
    code = payload.get("code")
    message = payload.get("message")
    if (
        not isinstance(code, str)
        or not code
        or len(code) > 48
        or not _SAFE_CODE.fullmatch(code)
        or not isinstance(message, str)
        or not message.strip()
        or len(message) > 160
        or any(
            unicodedata.category(character).startswith("C")
            for character in message
        )
    ):
        raise GuildHubClientError("The guild server returned an invalid error response.")
    return True


def _socket_url(server_url: str, path: str) -> str:
    return server_url + path


def _require_subprotocol(socket: Any) -> None:
    if getattr(socket, "subprotocol", None) != SUBPROTOCOL:
        raise GuildHubClientError(
            "The guild server did not select the Vemal's Ears protocol."
        )


async def pair_device(
    deep_link: str,
    credential_store: DeviceCredentialStore,
    *,
    device_name: str | None = None,
    client_version: str = CLIENT_VERSION,
    connector: Callable[..., Any] = connect,
    timeout: float = DEFAULT_PAIR_TIMEOUT_SECONDS,
    confirm: Callable[
        [DeviceCredentials, DeviceCredentials | None], bool
    ] | None = None,
) -> DeviceCredentials:
    """Exchange one enrollment code and replace the active device credential."""
    _require_websockets()

    # Either form: the protocol handler's vemalsears:// link, or the
    # https join link the operator console mints - the one a member is
    # most likely to paste into the dialog.
    link = await resolve_pair_link(deep_link, timeout=timeout)
    if confirm is not None and not callable(confirm):
        raise GuildHubPairingConfirmationError(
            "Could not safely confirm this guild pairing."
        )
    existing_credentials = None
    if confirm is not None:
        existing_credentials = credential_store.load()
    chosen_name = (
        "Windows PC" if device_name is None else device_name
    ).strip()
    request = {
        "type": "pair",
        "enrollment_code": link.enrollment_code,
        "device_name": chosen_name,
        "client_version": client_version,
    }
    try:
        parse_pair_request(request)
    except GuildHubProtocolError as error:
        raise GuildHubClientError(str(error)) from None

    try:
        async with connector(
            _socket_url(link.server_url, PAIR_PATH),
            subprotocols=[SUBPROTOCOL],
            max_size=MAX_WIRE_BYTES,
            open_timeout=timeout,
            close_timeout=3,
        ) as socket:
            _require_subprotocol(socket)
            await socket.send(encode_message(request))
            raw = await asyncio.wait_for(socket.recv(), timeout=timeout)
    except GuildHubClientError:
        raise
    except (OSError, TimeoutError, ConnectionClosed, WebSocketException):
        raise GuildHubClientError(
            "Could not securely reach the guild server."
        ) from None

    try:
        payload = decode_message(raw)
    except GuildHubProtocolError:
        raise GuildHubClientError("The guild server returned an invalid pairing response.") from None
    if _is_server_error(payload):
        raise GuildHubClientError(
            "The one-use pairing link was rejected or has already been used."
        )
    credentials = _pair_response(payload, link.server_url)
    if confirm is not None:
        try:
            confirmed = bool(confirm(credentials, existing_credentials))
        except Exception:
            raise GuildHubPairingConfirmationError(
                "Could not safely confirm this guild pairing."
            ) from None
        if not confirmed:
            raise GuildHubPairingCancelledError(
                "Guild pairing was cancelled. Ask for a new one-use link to try again."
            )
    try:
        credential_store.save(credentials)
    except (DeviceCredentialValidationError, DeviceCredentialStoreError):
        raise
    return credentials
