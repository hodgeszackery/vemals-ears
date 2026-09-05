"""Small, strict wire contract shared by the Guild Hub server and client.

Caption and typed quick-chat text are allowed on the wire. The relay keeps
them in bounded memory and never writes them to its database or logs; a
confirmed quick-chat destination is Discord, where the guild's normal text
retention applies. Tokens and raw audio are never valid caption events.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from VoiceCaptions.events import (
    CLASS_COLORS,
    CaptionEvent,
    CaptionProtocolError,
    ClearEvent,
    ProtocolEvent,
    SessionEvent,
    event_to_json,
    parse_event,
)

SUBPROTOCOL = "guild-hub.v1"
PAIR_PATH = "/v1/pair"
CAPTION_PATH = "/v1/captions"
PUBLISH_PATH = "/v1/publish"
VIEWER_LINK_PATH = "/v1/viewer-link"
QUICK_CHAT_PATH = "/v1/quick-chat"
QUICK_CHAT_PROFILE_PATH = "/v1/quick-chat-profile"
QUICK_CHAT_WORKER_PATH = "/v1/quick-chat-worker"
ADMIN_PATH = "/v1/admin"

MAX_WIRE_BYTES = 16_384
MAX_DEVICE_NAME = 64
MAX_CLIENT_VERSION = 32
MAX_TOKEN = 192
MAX_DEVICE_ID = 64
MAX_STREAM_ID = 64
MAX_ERROR_MESSAGE = 160
MAX_CURSOR = 9_007_199_254_740_991
MAX_QUICK_CHAT_NAME = 32
MAX_QUICK_CHAT_TEXT = 320
MAX_REQUEST_ID = 64
VIEWER_TOKEN_LENGTH = 24
VIEWER_LINK_CODE_LENGTH = 8

QUICK_CHAT_DELIVERY_STATUSES = frozenset(
    {"delivered", "no_active_session", "delivery_failed"}
)

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_VIEWER_TOKEN = re.compile(r"^[0-9a-f]{24}$")
_VIEWER_LINK_CODE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{8}$")


class GuildHubProtocolError(ValueError):
    """A Guild Hub message is malformed or outside the protocol limits."""


def _object(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise GuildHubProtocolError("message must be an object")
    return payload


def _exact_keys(payload: dict[str, Any], allowed: set[str]) -> None:
    if set(payload) != allowed:
        raise GuildHubProtocolError("message fields do not match the protocol")


def _text(
    value: Any,
    field: str,
    limit: int,
    *,
    safe_id: bool = False,
) -> str:
    if not isinstance(value, str):
        raise GuildHubProtocolError(field + " must be text")
    clean = unicodedata.normalize("NFKC", value).strip()
    if not clean or len(clean) > limit:
        raise GuildHubProtocolError(field + " is missing or too long")
    if any(unicodedata.category(char).startswith("C") for char in clean):
        raise GuildHubProtocolError(field + " contains control characters")
    if safe_id and not _SAFE_ID.fullmatch(clean):
        raise GuildHubProtocolError(field + " contains unsupported characters")
    return clean


def _cursor(value: Any, field: str = "cursor") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GuildHubProtocolError(field + " must be an integer")
    if value < 0 or value > MAX_CURSOR:
        raise GuildHubProtocolError(field + " is out of range")
    return value


def viewer_token(value: Any) -> str:
    """Return one opaque Discord-viewer token without accepting user IDs."""
    if not isinstance(value, str) or not _VIEWER_TOKEN.fullmatch(value):
        raise GuildHubProtocolError("viewer_token is invalid")
    return value


def viewer_link_code(value: Any) -> str:
    if not isinstance(value, str) or not _VIEWER_LINK_CODE.fullmatch(value):
        raise GuildHubProtocolError("viewer link code is invalid")
    return value


def _optional_safe_id(value: Any, field: str, limit: int) -> str:
    if value == "":
        return ""
    return _text(value, field, limit, safe_id=True)


def decode_message(raw: str | bytes) -> dict[str, Any]:
    if isinstance(raw, bytes):
        if len(raw) > MAX_WIRE_BYTES:
            raise GuildHubProtocolError("message is too large")
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise GuildHubProtocolError("message is not valid UTF-8") from error
    elif not isinstance(raw, str):
        raise GuildHubProtocolError("message must be text")
    if len(raw.encode("utf-8", errors="replace")) > MAX_WIRE_BYTES:
        raise GuildHubProtocolError("message is too large")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise GuildHubProtocolError("message contains a duplicate field")
            result[key] = value
        return result

    try:
        return _object(json.loads(raw, object_pairs_hook=unique_object))
    except GuildHubProtocolError:
        raise
    except (json.JSONDecodeError, RecursionError) as error:
        raise GuildHubProtocolError("message is not valid JSON") from error


def encode_message(payload: dict[str, Any]) -> str:
    _object(payload)
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_WIRE_BYTES:
        raise GuildHubProtocolError("message is too large")
    return encoded


@dataclass(frozen=True)
class PairRequest:
    enrollment_code: str
    device_name: str
    client_version: str


def parse_pair_request(payload: Any) -> PairRequest:
    message = _object(payload)
    _exact_keys(
        message, {"type", "enrollment_code", "device_name", "client_version"}
    )
    if message.get("type") != "pair":
        raise GuildHubProtocolError("expected pair message")
    return PairRequest(
        enrollment_code=_text(
            message.get("enrollment_code"), "enrollment_code", MAX_TOKEN, safe_id=True
        ),
        device_name=_text(message.get("device_name"), "device_name", MAX_DEVICE_NAME),
        client_version=_text(
            message.get("client_version"), "client_version", MAX_CLIENT_VERSION
        ),
    )


@dataclass(frozen=True)
class DeviceAuthentication:
    device_id: str
    device_token: str
    last_stream_id: str
    last_cursor: int
    # Empty when the client did not send one. Clients before this existed
    # do not, and must keep authenticating exactly as they always have.
    client_version: str = ""


def parse_device_authentication(payload: Any) -> DeviceAuthentication:
    message = _object(payload)
    # client_version is the ONE optional key. Anything else extra is still
    # rejected. Accepting it here ships BEFORE any client sends it: the
    # relay has twice been broken by adding a field to one side of a wire
    # contract first, so the tolerant side always goes out in front.
    required = {"type", "device_id", "device_token", "last_stream_id", "last_cursor"}
    if set(message) != required and set(message) != required | {"client_version"}:
        raise GuildHubProtocolError("message fields do not match the protocol")
    if message.get("type") != "authenticate":
        raise GuildHubProtocolError("expected authenticate message")
    reported = ""
    if "client_version" in message:
        reported = _text(
            message.get("client_version"), "client_version", MAX_CLIENT_VERSION
        )
    return DeviceAuthentication(
        client_version=reported,
        device_id=_text(message.get("device_id"), "device_id", MAX_DEVICE_ID, safe_id=True),
        device_token=_text(
            message.get("device_token"), "device_token", MAX_TOKEN, safe_id=True
        ),
        last_stream_id=_optional_safe_id(
            message.get("last_stream_id"), "last_stream_id", MAX_STREAM_ID
        ),
        last_cursor=_cursor(message.get("last_cursor", 0), "last_cursor"),
    )


@dataclass(frozen=True, repr=False)
class ViewerLinkRequest:
    device_id: str
    device_token: str

    def __repr__(self) -> str:
        return "ViewerLinkRequest(device_id=<redacted>, device_token=<redacted>)"


def parse_viewer_link_request(payload: Any) -> ViewerLinkRequest:
    message = _object(payload)
    _exact_keys(message, {"type", "device_id", "device_token"})
    if message.get("type") != "viewer_link_request":
        raise GuildHubProtocolError("expected viewer link request")
    return ViewerLinkRequest(
        device_id=_text(
            message.get("device_id"), "device_id", MAX_DEVICE_ID, safe_id=True
        ),
        device_token=_text(
            message.get("device_token"), "device_token", MAX_TOKEN, safe_id=True
        ),
    )


def viewer_link_response(code: str, expires_at: int) -> dict[str, Any]:
    checked_code = viewer_link_code(code)
    if (
        isinstance(expires_at, bool)
        or not isinstance(expires_at, int)
        or expires_at < 0
        or expires_at > MAX_CURSOR
    ):
        raise GuildHubProtocolError("viewer link expiry is invalid")
    return {
        "type": "viewer_link",
        "code": checked_code,
        "expires_at": expires_at,
    }


@dataclass(frozen=True)
class ViewerLinkResponse:
    code: str
    expires_at: int


def parse_viewer_link_response(payload: Any) -> ViewerLinkResponse:
    """Parse the relay response consumed by :mod:`GuildHub.client`."""
    message = _object(payload)
    _exact_keys(message, {"type", "code", "expires_at"})
    if message.get("type") != "viewer_link":
        raise GuildHubProtocolError("expected viewer link response")
    expires_at = message.get("expires_at")
    if (
        isinstance(expires_at, bool)
        or not isinstance(expires_at, int)
        or expires_at < 0
        or expires_at > MAX_CURSOR
    ):
        raise GuildHubProtocolError("viewer link expiry is invalid")
    return ViewerLinkResponse(
        code=viewer_link_code(message.get("code")),
        expires_at=expires_at,
    )


@dataclass(frozen=True, repr=False)
class QuickChatRequest:
    """One authenticated line a member deliberately typed.

    The content fields stay out of repr so an unexpected diagnostic cannot
    turn the typed line into a log entry.
    """

    device_id: str
    device_token: str
    request_id: str
    text: str

    def __repr__(self) -> str:
        return "QuickChatRequest(<redacted>)"


def parse_quick_chat(payload: Any) -> QuickChatRequest:
    message = _object(payload)
    _exact_keys(
        message,
        {"type", "request_id", "device_id", "device_token", "text"},
    )
    if message.get("type") != "quick_chat":
        raise GuildHubProtocolError("expected quick chat message")
    return QuickChatRequest(
        device_id=_text(
            message.get("device_id"), "device_id", MAX_DEVICE_ID, safe_id=True
        ),
        device_token=_text(
            message.get("device_token"), "device_token", MAX_TOKEN, safe_id=True
        ),
        request_id=_text(
            message.get("request_id"), "request_id", MAX_REQUEST_ID, safe_id=True
        ),
        text=_text(message.get("text"), "text", MAX_QUICK_CHAT_TEXT),
    )


@dataclass(frozen=True)
class QuickChatAcknowledgement:
    request_id: str
    cursor: int


def quick_chat_acknowledgement(request_id: str, cursor: int) -> dict[str, Any]:
    return {
        "type": "quick_chat_sent",
        "request_id": _text(
            request_id, "request_id", MAX_REQUEST_ID, safe_id=True
        ),
        "cursor": _cursor(cursor),
    }


def parse_quick_chat_acknowledgement(payload: Any) -> QuickChatAcknowledgement:
    message = _object(payload)
    _exact_keys(message, {"type", "request_id", "cursor"})
    if message.get("type") != "quick_chat_sent":
        raise GuildHubProtocolError("expected quick chat acknowledgement")
    return QuickChatAcknowledgement(
        request_id=_text(
            message.get("request_id"), "request_id", MAX_REQUEST_ID, safe_id=True
        ),
        cursor=_cursor(message.get("cursor")),
    )


@dataclass(frozen=True, repr=False)
class QuickChatProfileRequest:
    """One device-authenticated read or update of its own typed-chat name.

    The optional name distinguishes an update from a read. Credentials stay
    out of repr so protocol diagnostics can never disclose the device token.
    """

    device_id: str
    device_token: str
    name: str | None

    def __repr__(self) -> str:
        return "QuickChatProfileRequest(<redacted>)"


def parse_quick_chat_profile_request(payload: Any) -> QuickChatProfileRequest:
    message = _object(payload)
    required = {"type", "device_id", "device_token"}
    fields = frozenset(message)
    if fields not in {frozenset(required), frozenset(required | {"name"})}:
        raise GuildHubProtocolError("message fields do not match the protocol")
    if message.get("type") != "quick_chat_profile":
        raise GuildHubProtocolError("expected quick chat profile message")
    name = (
        _text(message.get("name"), "name", MAX_QUICK_CHAT_NAME)
        if "name" in message
        else None
    )
    return QuickChatProfileRequest(
        device_id=_text(
            message.get("device_id"), "device_id", MAX_DEVICE_ID, safe_id=True
        ),
        device_token=_text(
            message.get("device_token"), "device_token", MAX_TOKEN, safe_id=True
        ),
        name=name,
    )


@dataclass(frozen=True, repr=False)
class QuickChatProfile:
    quick_chat_name: str | None

    def __repr__(self) -> str:
        return "QuickChatProfile(<redacted>)"


def quick_chat_profile(name: str | None) -> dict[str, Any]:
    return {
        "type": "quick_chat_profile",
        "name": (
            None
            if name is None
            else _text(name, "name", MAX_QUICK_CHAT_NAME)
        ),
    }


def parse_quick_chat_profile(payload: Any) -> QuickChatProfile:
    message = _object(payload)
    _exact_keys(message, {"type", "name"})
    if message.get("type") != "quick_chat_profile":
        raise GuildHubProtocolError("expected quick chat profile response")
    rendered = quick_chat_profile(message.get("name"))
    return QuickChatProfile(quick_chat_name=rendered["name"])


@dataclass(frozen=True, repr=False)
class QuickChatCommand:
    """A relay-authorized Discord delivery command.

    Text is deliberately excluded from repr because commands remain
    memory-only and must never become diagnostic output.
    """

    request_id: str
    speaker: str
    wow_class: str
    text: str

    def __repr__(self) -> str:
        return "QuickChatCommand(<redacted>)"


def _quick_chat_class(value: Any) -> str:
    if value == "":
        return ""
    wow_class = _text(value, "wow_class", 32, safe_id=True).lower()
    if wow_class not in CLASS_COLORS:
        raise GuildHubProtocolError("wow_class is unsupported")
    return wow_class


def quick_chat_command(
    request_id: str,
    speaker: str,
    wow_class: str,
    text: str,
) -> dict[str, Any]:
    return {
        "type": "quick_chat_command",
        "request_id": _text(
            request_id, "request_id", MAX_REQUEST_ID, safe_id=True
        ),
        "speaker": _text(speaker, "speaker", MAX_QUICK_CHAT_NAME),
        "wow_class": _quick_chat_class(wow_class),
        "text": _text(text, "text", MAX_QUICK_CHAT_TEXT),
    }


def parse_quick_chat_command(payload: Any) -> QuickChatCommand:
    message = _object(payload)
    _exact_keys(
        message, {"type", "request_id", "speaker", "wow_class", "text"}
    )
    if message.get("type") != "quick_chat_command":
        raise GuildHubProtocolError("expected quick chat command")
    command = quick_chat_command(
        message.get("request_id"),
        message.get("speaker"),
        message.get("wow_class"),
        message.get("text"),
    )
    return QuickChatCommand(
        request_id=command["request_id"],
        speaker=command["speaker"],
        wow_class=command["wow_class"],
        text=command["text"],
    )


@dataclass(frozen=True)
class QuickChatDelivery:
    request_id: str
    status: str


def quick_chat_delivery(request_id: str, status: str) -> dict[str, Any]:
    checked_status = _text(status, "status", 32, safe_id=True)
    if checked_status not in QUICK_CHAT_DELIVERY_STATUSES:
        raise GuildHubProtocolError("quick chat delivery status is unsupported")
    return {
        "type": "quick_chat_delivery",
        "request_id": _text(
            request_id, "request_id", MAX_REQUEST_ID, safe_id=True
        ),
        "status": checked_status,
    }


def parse_quick_chat_delivery(payload: Any) -> QuickChatDelivery:
    message = _object(payload)
    _exact_keys(message, {"type", "request_id", "status"})
    if message.get("type") != "quick_chat_delivery":
        raise GuildHubProtocolError("expected quick chat delivery")
    delivery = quick_chat_delivery(
        message.get("request_id"), message.get("status")
    )
    return QuickChatDelivery(
        request_id=delivery["request_id"],
        status=delivery["status"],
    )


def parse_service_authentication(payload: Any, field: str) -> str:
    message = _object(payload)
    _exact_keys(message, {"type", field})
    if message.get("type") != "authenticate":
        raise GuildHubProtocolError("expected authenticate message")
    return _text(message.get(field), field, MAX_TOKEN, safe_id=True)


@dataclass(frozen=True)
class PublishRequest:
    event: ProtocolEvent
    audience_generation: int | None = None
    audience_revision: int | None = None


@dataclass(frozen=True)
class AudienceUpdate:
    generation: int
    revision: int
    viewer_tokens: tuple[str, ...]


@dataclass(frozen=True)
class ViewerLink:
    code: str
    viewer_token: str


@dataclass(frozen=True)
class ViewerEnrollment:
    """A pairing invitation written in Discord, carrying who asked for it.

    The adapter mints the code because it has to show it to the person in
    the same breath they ask; the relay creates the enrollment behind it
    and remembers the viewer token, so the device that claims the code
    inherits its owner's identity without a separate linking step.
    """

    code: str
    viewer_token: str
    ttl_seconds: int


PublisherCommand = PublishRequest | AudienceUpdate | ViewerLink | ViewerEnrollment


def _audience_generation(value: Any) -> int:
    generation = _cursor(value, "audience_generation")
    if generation == 0:
        raise GuildHubProtocolError("audience_generation is out of range")
    return generation


def parse_publish_request(payload: Any) -> PublishRequest:
    message = _object(payload)
    keys = set(message)
    base = {"type", "event"}
    routed = base | {"audience_generation", "audience_revision"}
    if keys not in (base, routed):
        raise GuildHubProtocolError("message fields do not match the protocol")
    if message.get("type") != "publish":
        raise GuildHubProtocolError("expected publish message")
    try:
        event = _parse_exact_event(message.get("event"))
    except CaptionProtocolError as error:
        raise GuildHubProtocolError("invalid caption event: " + str(error)) from error
    if keys == routed:
        if not isinstance(event, CaptionEvent):
            raise GuildHubProtocolError(
                "audience routing is supported only for caption events"
            )
        return PublishRequest(
            event=event,
            audience_generation=_audience_generation(
                message.get("audience_generation")
            ),
            audience_revision=_cursor(
                message.get("audience_revision"), "audience_revision"
            ),
        )
    return PublishRequest(event=event)


def parse_publish(payload: Any) -> ProtocolEvent:
    """Compatibility parser for callers that only need the public event."""
    return parse_publish_request(payload).event


def parse_audience_update(payload: Any) -> AudienceUpdate:
    message = _object(payload)
    _exact_keys(
        message,
        {"type", "generation", "revision", "viewer_tokens"},
    )
    if message.get("type") != "audience_update":
        raise GuildHubProtocolError("expected audience update")
    raw_tokens = message.get("viewer_tokens")
    if not isinstance(raw_tokens, list):
        raise GuildHubProtocolError("viewer_tokens must be a list")
    tokens = tuple(viewer_token(value) for value in raw_tokens)
    if len(tokens) != len(set(tokens)):
        raise GuildHubProtocolError("viewer_tokens contains duplicates")
    return AudienceUpdate(
        generation=_audience_generation(message.get("generation")),
        revision=_cursor(message.get("revision"), "revision"),
        viewer_tokens=tokens,
    )


def parse_viewer_link(payload: Any) -> ViewerLink:
    message = _object(payload)
    _exact_keys(message, {"type", "code", "viewer_token"})
    if message.get("type") != "viewer_link":
        raise GuildHubProtocolError("expected viewer link")
    return ViewerLink(
        code=viewer_link_code(message.get("code")),
        viewer_token=viewer_token(message.get("viewer_token")),
    )


def parse_viewer_enrollment(payload: Any) -> ViewerEnrollment:
    message = _object(payload)
    _exact_keys(message, {"type", "code", "viewer_token", "ttl_seconds"})
    if message.get("type") != "viewer_enrollment":
        raise GuildHubProtocolError("expected viewer enrollment")
    code = _text(message.get("code"), "code", MAX_TOKEN, safe_id=True)
    if not re.fullmatch(r"ghe_[A-Za-z0-9_-]{32,64}", code):
        raise GuildHubProtocolError("enrollment code is invalid")
    ttl = message.get("ttl_seconds")
    if (
        isinstance(ttl, bool)
        or not isinstance(ttl, int)
        or ttl <= 0
        or ttl > 7 * 24 * 60 * 60
    ):
        raise GuildHubProtocolError("enrollment lifetime is invalid")
    return ViewerEnrollment(
        code=code,
        viewer_token=viewer_token(message.get("viewer_token")),
        ttl_seconds=ttl,
    )


def parse_publisher_command(payload: Any) -> PublisherCommand:
    message = _object(payload)
    command_type = message.get("type")
    if command_type == "publish":
        return parse_publish_request(message)
    if command_type == "audience_update":
        return parse_audience_update(message)
    if command_type == "viewer_link":
        return parse_viewer_link(message)
    if command_type == "viewer_enrollment":
        return parse_viewer_enrollment(message)
    raise GuildHubProtocolError("unknown publisher command")


def caption_envelope(
    stream_id: str,
    cursor: int,
    event: ProtocolEvent,
) -> dict[str, Any]:
    if not isinstance(event, (CaptionEvent, SessionEvent, ClearEvent)):
        raise GuildHubProtocolError("unsupported caption event")
    return {
        "type": "caption_event",
        "stream_id": _text(
            stream_id, "stream_id", MAX_STREAM_ID, safe_id=True
        ),
        "cursor": _cursor(cursor),
        "event": json.loads(event_to_json(event)),
    }


def cursor_advance(stream_id: str, cursor: int) -> dict[str, Any]:
    """Advance one private stream cursor without carrying an event body."""
    return {
        "type": "cursor_advance",
        "stream_id": _text(
            stream_id, "stream_id", MAX_STREAM_ID, safe_id=True
        ),
        "cursor": _cursor(cursor),
    }


@dataclass(frozen=True)
class CursorAdvance:
    stream_id: str
    cursor: int


def parse_cursor_advance(payload: Any) -> CursorAdvance:
    message = _object(payload)
    _exact_keys(message, {"type", "stream_id", "cursor"})
    if message.get("type") != "cursor_advance":
        raise GuildHubProtocolError("expected cursor advance")
    return CursorAdvance(
        stream_id=_text(
            message.get("stream_id"), "stream_id", MAX_STREAM_ID, safe_id=True
        ),
        cursor=_cursor(message.get("cursor")),
    )


@dataclass(frozen=True)
class CaptionEnvelope:
    stream_id: str
    cursor: int
    event: ProtocolEvent


def parse_caption_envelope(payload: Any) -> CaptionEnvelope:
    message = _object(payload)
    _exact_keys(message, {"type", "stream_id", "cursor", "event"})
    if message.get("type") != "caption_event":
        raise GuildHubProtocolError("expected caption event")
    try:
        event = _parse_exact_event(message.get("event"))
    except CaptionProtocolError as error:
        raise GuildHubProtocolError("invalid caption event: " + str(error)) from error
    return CaptionEnvelope(
        stream_id=_text(
            message.get("stream_id"), "stream_id", MAX_STREAM_ID, safe_id=True
        ),
        cursor=_cursor(message.get("cursor")),
        event=event,
    )


def _parse_exact_event(payload: Any) -> ProtocolEvent:
    event = _object(payload)
    event_type = event.get("type")
    if event_type == "caption":
        required = {
            "type",
            "speaker_id",
            "speaker",
            "character",
            "role",
            "text",
            "final",
            "sequence",
            "timestamp",
        }
        # Optional fields are omitted when they have no visible effect:
        # wow_class for an unmapped character and speech_style for normal
        # speech. Every combination is legitimate, but unknown keys remain
        # a protocol error.
        #
        # This exact list is one half of a contract whose other half lives
        # in VoiceCaptions.events.event_to_json. Adding a field there and
        # not here rejected every caption from a MAPPED speaker while
        # unmapped ones sailed through - so the bot stayed up until the
        # one person with a character mapped opened their mouth, then the
        # publisher failed and the whole pipeline came down. There is a
        # test below that pins the two halves together; keep it passing
        # rather than editing this set by hand.
        optional = {"wow_class", "speech_style"}
        fields = set(event)
        if not required.issubset(fields) or not fields.issubset(required | optional):
            raise GuildHubProtocolError(
                "caption event fields do not match the protocol"
            )
    elif event_type == "session":
        if set(event) not in ({"type", "state"}, {"type", "state", "message"}):
            raise GuildHubProtocolError("caption event fields do not match the protocol")
    elif event_type == "clear":
        _exact_keys(event, {"type"})
    else:
        raise GuildHubProtocolError("unknown caption event type")
    return parse_event(event)


def error_message(code: str, message: str) -> dict[str, str]:
    return {
        "type": "error",
        "code": _text(code, "code", 48, safe_id=True),
        "message": _text(message, "message", MAX_ERROR_MESSAGE),
    }
