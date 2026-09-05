"""Memory-only caption publisher for a Vemal's Ears host.

The Discord/STT worker calls ``send`` synchronously. A private writer thread
authenticates to the guild server and waits for an acknowledgement before it
releases each caption from memory. Caption bodies and credentials are never
logged or written to disk.
"""

from __future__ import annotations

import ipaddress
import json
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit, urlunsplit

from VoiceCaptions.events import ProtocolEvent, event_to_json

from .protocol import (
    MAX_CURSOR,
    MAX_WIRE_BYTES,
    PUBLISH_PATH,
    SUBPROTOCOL,
    GuildHubProtocolError,
    decode_message,
    encode_message,
    parse_audience_update,
    parse_publish_request,
    parse_viewer_enrollment,
    parse_viewer_link,
)

try:
    from websockets.sync.client import connect as websocket_connect
except ImportError:  # pragma: no cover - exercised by deployment readiness.
    websocket_connect = None


PUBLISH_QUEUE_SIZE = 64
AUTH_TIMEOUT_SECONDS = 8.0
ACK_TIMEOUT_SECONDS = 8.0
_STOP = object()


@dataclass(frozen=True)
class _PendingFrame:
    payload: str
    kind: str
    generation: int = 0
    revision: int = 0
    code: str = ""


@dataclass(frozen=True)
class _FlushBarrier:
    reached: threading.Event


class PublisherConnection(Protocol):
    def send(self, message: str) -> None: ...

    def recv(self, timeout: float | None = None) -> str | bytes: ...

    def close(self, code: int = 1000, reason: str = "") -> None: ...


class PublisherError(RuntimeError):
    """The remote caption channel could not be used safely."""


def _is_loopback(hostname: str | None) -> bool:
    if not hostname:
        return False
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def publisher_url(server_url: str) -> str:
    """Return the fixed publisher endpoint, rejecting unsafe remote WS."""
    try:
        parsed = urlsplit(server_url.strip())
        port = parsed.port
    except (AttributeError, TypeError, ValueError) as error:
        raise PublisherError("The Vemal's Ears server address is invalid.") from error
    if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
        raise PublisherError("The Vemal's Ears server address is invalid.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise PublisherError("The Vemal's Ears server address is invalid.")
    if parsed.scheme == "ws" and not _is_loopback(parsed.hostname):
        raise PublisherError("A remote Vemal's Ears server must use encrypted WSS.")
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = "[" + host + "]"
    netloc = host + ((":" + str(port)) if port is not None else "")
    return urlunsplit((parsed.scheme, netloc, PUBLISH_PATH, "", ""))


def _cursor_message(raw: str | bytes, expected_type: str) -> int:
    message = decode_message(raw)
    if set(message) != {"type", "cursor"} or message.get("type") != expected_type:
        raise PublisherError("The Vemal's Ears server returned an unexpected response.")
    cursor = message.get("cursor")
    if isinstance(cursor, bool) or not isinstance(cursor, int):
        raise PublisherError("The Vemal's Ears server returned an invalid cursor.")
    if cursor < 0 or cursor > MAX_CURSOR:
        raise PublisherError("The Vemal's Ears server returned an invalid cursor.")
    return cursor


def _audience_ack(raw: str | bytes, generation: int, revision: int) -> None:
    message = decode_message(raw)
    if message != {
        "type": "audience_updated",
        "generation": generation,
        "revision": revision,
    }:
        raise PublisherError("The Vemal's Ears server returned an unexpected response.")


def _viewer_enrollment_ack(raw: str | bytes, code: str) -> bool:
    """The relay's answer to a Discord-issued pairing invitation.

    0.4.5 shipped the invitation frame with no branch here, so the very
    first /vem setup fell through to "an invalid caption entered the
    publish queue", marked the publisher failed, and took every caption
    with it. Every frame kind the publisher can send needs its ack read
    here; the send happens before the dispatch, so the relay had already
    acted by the time this raised.
    """
    message = decode_message(raw)
    if set(message) != {"type", "code", "created"}:
        raise PublisherError("The Vemal's Ears server returned an unexpected response.")
    if (
        message.get("type") != "viewer_enrolled"
        or message.get("code") != code
        or not isinstance(message.get("created"), bool)
    ):
        raise PublisherError("The Vemal's Ears server returned an unexpected response.")
    return bool(message["created"])


def _viewer_link_ack(raw: str | bytes, code: str) -> bool:
    message = decode_message(raw)
    if set(message) != {"type", "code", "linked"}:
        raise PublisherError("The Vemal's Ears server returned an unexpected response.")
    if (
        message.get("type") != "viewer_linked"
        or message.get("code") != code
        or not isinstance(message.get("linked"), bool)
    ):
        raise PublisherError("The Vemal's Ears server returned an unexpected response.")
    return bool(message["linked"])


class RemoteCaptionSink:
    """Synchronous event sink backed by one authenticated WebSocket."""

    def __init__(
        self,
        server_url: str,
        publisher_token: str,
        *,
        connector: Callable[..., PublisherConnection] | None = None,
        max_pending: int = PUBLISH_QUEUE_SIZE,
    ) -> None:
        if max_pending < 1 or max_pending > 256:
            raise ValueError("max_pending must be between 1 and 256")
        if not isinstance(publisher_token, str) or len(publisher_token.strip()) < 32:
            raise PublisherError("The Vemal's Ears publisher credential is missing.")
        if any(character.isspace() for character in publisher_token):
            raise PublisherError("The Vemal's Ears publisher credential is invalid.")
        if connector is None:
            if websocket_connect is None:
                raise PublisherError("The Vemal's Ears network runtime is not installed.")
            connector = websocket_connect
        self.url = publisher_url(server_url)
        self._token = publisher_token
        self._connector = connector
        self._pending: queue.Queue[object] = queue.Queue(maxsize=max_pending)
        self._ready = threading.Event()
        self._failed = threading.Event()
        self._closing = threading.Event()
        self._connection: PublisherConnection | None = None
        self._last_cursor = 0
        self.refused_invitations = 0
        self._writer = threading.Thread(
            target=self._run,
            name="VemalsEarsPublisher",
            daemon=True,
        )
        self._writer.start()

    @classmethod
    def start(
        cls,
        server_url: str,
        publisher_token: str,
        *,
        connector: Callable[..., PublisherConnection] | None = None,
        timeout: float = AUTH_TIMEOUT_SECONDS,
    ) -> "RemoteCaptionSink":
        sink = cls(server_url, publisher_token, connector=connector)
        if sink._ready.wait(timeout) and sink.alive():
            return sink
        sink.close()
        raise PublisherError("The Vemal's Ears caption server is unavailable.")

    def alive(self) -> bool:
        return (
            self._ready.is_set()
            and not self._failed.is_set()
            and not self._closing.is_set()
            and self._writer.is_alive()
        )

    def send(
        self,
        event: ProtocolEvent,
        *,
        audience_generation: int | None = None,
        audience_revision: int | None = None,
    ) -> bool:
        if not self.alive():
            return False
        try:
            # Round-trip through the trusted local event encoder now, before
            # the writer thread owns the only remaining reference.
            message: dict[str, Any] = {
                "type": "publish",
                "event": json.loads(event_to_json(event)),
            }
            if audience_generation is not None or audience_revision is not None:
                message["audience_generation"] = audience_generation
                message["audience_revision"] = audience_revision
            parse_publish_request(message)
            self._pending.put_nowait(
                _PendingFrame(encode_message(message), "publish")
            )
            return True
        except (GuildHubProtocolError, TypeError, ValueError, queue.Full):
            self._failed.set()
            return False

    def flush(self, timeout: float = ACK_TIMEOUT_SECONDS) -> bool:
        """Wait until every frame accepted before this call has its relay ACK."""
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or timeout <= 0
            or timeout > 60
        ):
            raise ValueError("flush timeout must be between 0 and 60 seconds")
        if not self.alive():
            return False
        deadline = time.monotonic() + float(timeout)
        barrier = _FlushBarrier(threading.Event())
        try:
            self._pending.put(barrier, timeout=float(timeout))
        except queue.Full:
            return False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if barrier.reached.wait(min(0.05, remaining)):
                return self.alive()
            if self._failed.is_set() or not self._writer.is_alive():
                return False

    def update_audience(
        self, generation: int, revision: int, viewer_tokens: list[str] | tuple[str, ...]
    ) -> bool:
        if not self.alive():
            return False
        try:
            message = {
                "type": "audience_update",
                "generation": generation,
                "revision": revision,
                "viewer_tokens": list(viewer_tokens),
            }
            parsed = parse_audience_update(message)
            self._pending.put_nowait(
                _PendingFrame(
                    encode_message(message),
                    "audience_update",
                    generation=parsed.generation,
                    revision=parsed.revision,
                )
            )
            return True
        except (GuildHubProtocolError, TypeError, ValueError, queue.Full):
            self._failed.set()
            return False

    def enroll_viewer(self, code: str, viewer_token: str, ttl_seconds: int = 1800) -> bool:
        """Create a pairing invitation bound to one Discord person."""
        if not self.alive():
            return False
        try:
            message = {
                "type": "viewer_enrollment",
                "code": code,
                "viewer_token": viewer_token,
                "ttl_seconds": int(ttl_seconds),
            }
            parse_viewer_enrollment(message)
            self._pending.put_nowait(
                _PendingFrame(
                    encode_message(message),
                    "viewer_enrollment",
                    code=code,
                )
            )
            return True
        except (GuildHubProtocolError, TypeError, ValueError, queue.Full):
            self._failed.set()
            return False

    def link_viewer(self, code: str, viewer_token: str) -> bool:
        if not self.alive():
            return False
        try:
            message = {
                "type": "viewer_link",
                "code": code,
                "viewer_token": viewer_token,
            }
            parsed = parse_viewer_link(message)
            self._pending.put_nowait(
                _PendingFrame(
                    encode_message(message),
                    "viewer_link",
                    code=parsed.code,
                )
            )
            return True
        except (GuildHubProtocolError, TypeError, ValueError, queue.Full):
            self._failed.set()
            return False

    def _run(self) -> None:
        connection: PublisherConnection | None = None
        try:
            connection = self._connector(
                self.url,
                subprotocols=[SUBPROTOCOL],
                compression=None,
                max_size=MAX_WIRE_BYTES,
                open_timeout=AUTH_TIMEOUT_SECONDS,
                close_timeout=2,
            )
            self._connection = connection
            connection.send(
                encode_message(
                    {"type": "authenticate", "publisher_token": self._token}
                )
            )
            self._last_cursor = _cursor_message(
                connection.recv(timeout=AUTH_TIMEOUT_SECONDS), "authenticated"
            )
            # Authentication no longer needs a second in-memory copy.
            self._token = ""
            self._ready.set()
            while not self._closing.is_set():
                payload = self._pending.get()
                try:
                    if payload is _STOP:
                        return
                    if isinstance(payload, _FlushBarrier):
                        # FIFO is the contract: reaching this memory-only
                        # marker means every earlier frame received its ACK.
                        payload.reached.set()
                        continue
                    if not isinstance(payload, _PendingFrame):
                        raise PublisherError("An invalid caption entered the publish queue.")
                    connection.send(payload.payload)
                    response = connection.recv(timeout=ACK_TIMEOUT_SECONDS)
                    if payload.kind == "publish":
                        cursor = _cursor_message(response, "published")
                        # This is the relay's global stream cursor. Typed
                        # chat publishes directly to that stream between
                        # worker frames, so acknowledgements can skip ahead.
                        # Only a repeated or backward cursor is invalid here;
                        # subscribers still require contiguous replay.
                        if cursor <= self._last_cursor:
                            raise PublisherError(
                                "The Vemal's Ears server returned a non-advancing cursor."
                            )
                        self._last_cursor = cursor
                    elif payload.kind == "audience_update":
                        _audience_ack(
                            response, payload.generation, payload.revision
                        )
                    elif payload.kind == "viewer_link":
                        _viewer_link_ack(response, payload.code)
                    elif payload.kind == "viewer_enrollment":
                        if not _viewer_enrollment_ack(response, payload.code):
                            # One refused invitation is one person's problem,
                            # not a reason to drop every caption in the raid;
                            # count it so a status line can say it happened.
                            self.refused_invitations += 1
                    else:
                        raise PublisherError("An invalid caption entered the publish queue.")
                finally:
                    payload = None
                    self._pending.task_done()
        except Exception:
            # Do not render the exception: network libraries can include a
            # request or credential in their diagnostic text.
            self._failed.set()
        finally:
            self._ready.set()
            self._token = ""
            self._connection = None
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    def close(self) -> None:
        first_close = not self._closing.is_set()
        self._closing.set()
        self._token = ""
        if first_close:
            try:
                self._pending.put_nowait(_STOP)
            except queue.Full:
                pass
        connection = self._connection
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        self._writer.join(timeout=3)
        while True:
            try:
                self._pending.get_nowait()
            except queue.Empty:
                break
            else:
                self._pending.task_done()
