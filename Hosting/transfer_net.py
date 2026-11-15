"""Network seams of a host-to-host transfer: the two relay sockets and two HTTP checks.

Everything here is a thin, replaceable edge. The state machines in
Hosting.transfer_flow receive these as injected callables, so tests swap in
fakes or point them at an in-process relay. Nothing in this module reads or
writes host state, and the one-time code is never passed to it: the claim
socket carries PAKE messages only, never the code.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import socket
import ssl
import time
import urllib.error
import urllib.request

from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI
from websockets.sync.client import connect

from GuildHub.protocol import SUBPROTOCOL, TRANSFER_CLAIM_PATH, TRANSFER_OFFER_PATH
from Hosting import handoff, setup


FRAME_LIMIT = 16_384
HEALTH = {"status": "ok", "service": "vemals-ears"}
OPEN_TIMEOUT = 15
ANSWERS, QUIET, UNKNOWN = "answers", "quiet", "unknown"
# Proof that this PC is online when the public address gives no HTTP answer:
# the Discord API host every caption worker needs anyway. Nothing about this
# host or its operator is sent, and only "some HTTP reply came back" is used.
CONTROL_URL = "https://discord.com/api/v10/gateway"
STATE_PROBES = 3
STATE_PAUSE = 2.0


class Closed(Exception):
    """The socket is gone. `reason` is the close reason the relay gave, if any."""

    def __init__(self, reason: str = "") -> None:
        super().__init__(reason)
        self.reason = reason


class Unavailable(Exception):
    """The socket could not be opened at all (no offer, wrong key, no route)."""


def _reason(error: ConnectionClosed) -> str:
    frame = error.rcvd
    return frame.reason if frame is not None and isinstance(frame.reason, str) else ""


def _open(url: str):
    try:
        return connect(url, subprotocols=[SUBPROTOCOL], compression=None, max_size=FRAME_LIMIT,
                       open_timeout=OPEN_TIMEOUT, close_timeout=3)
    except (OSError, InvalidHandshake, InvalidURI, TimeoutError, ValueError):
        raise Unavailable() from None


class _Socket:
    def __init__(self, socket) -> None:
        self._socket = socket

    def _receive(self, timeout: float):
        try:
            return self._socket.recv(timeout=max(0.0, timeout))
        except TimeoutError:
            return None
        except ConnectionClosed as error:
            raise Closed(_reason(error)) from None
        except OSError:
            raise Closed() from None

    def _send(self, data) -> None:
        try:
            self._socket.send(data)
        except ConnectionClosed as error:
            raise Closed(_reason(error)) from None
        except OSError:
            raise Closed() from None

    def close(self) -> None:
        try:
            self._socket.close()
        except Exception:  # noqa: BLE001 - closing must never raise
            pass


class OfferSocket(_Socket):
    """The old host's socket on its own relay.

    Text frames are relay control in both directions; binary frames belong to
    the other host and are piped unread. The relay is reached on loopback and
    proves nothing by that: the socket is admitted by the transfer key the
    elevated Host app wrote for this one offer.
    """

    def __init__(self, port: int, transfer_key: str, *, url: str | None = None) -> None:
        super().__init__(_open(url or "ws://127.0.0.1:" + str(int(port)) + TRANSFER_OFFER_PATH))
        try:
            self._send(json.dumps({"type": "authenticate", "transfer_key": transfer_key}))
            reply = self._receive(10)
            notice = json.loads(reply) if isinstance(reply, str) else None
        except (Closed, ValueError):
            notice = None
        if not isinstance(notice, dict) or notice.get("type") != "transfer_offered":
            self.close()
            raise Unavailable()
        self.ttl_seconds = int(notice.get("ttl_seconds", 600))

    def control(self, kind: str, **fields) -> None:
        self._send(json.dumps({"type": kind, **fields}))

    def send(self, frame: bytes) -> None:
        self._send(frame)

    def receive(self, timeout: float):
        """("notice", dict) from the relay, ("peer", bytes) from the other host, or None on timeout."""
        raw = self._receive(timeout)
        if raw is None:
            return None
        if isinstance(raw, bytes):
            return "peer", raw
        try:
            notice = json.loads(raw)
        except ValueError:
            notice = None
        return "notice", notice if isinstance(notice, dict) else {}


class ClaimSocket(_Socket):
    """The new host's socket on the old host's public relay: binary frames only."""

    def __init__(self, url: str) -> None:
        super().__init__(_open(url))

    def send(self, frame: bytes) -> None:
        self._send(frame)

    def receive(self, timeout: float):
        """The next frame from the old host, or None on timeout."""
        raw = self._receive(timeout)
        if isinstance(raw, str):
            raise Closed()      # the relay never sends a claim text; treat it as a broken pipe
        return raw


def public_origin(address: str) -> str:
    """The one spelling of a host's public HTTPS origin, from what a person types.

    Both PCs must derive byte-identical text or the code exchange fails like a
    wrong code, so there is exactly one form: https://hostname, plus :port
    only when it is not 443. The transfer code is never part of an address.
    """
    text = address.strip() if isinstance(address, str) else ""
    if "://" not in text:
        text = "https://" + text
    try:
        hostname, port = handoff._public_url(text.rstrip("/"), "https", origin=True)
    except handoff.HandoffError:
        raise setup.SetupError("Enter the public address shown on the old PC, such as ears.example.org.") from None
    return "https://" + hostname + ("" if port == 443 else ":" + str(port))


def claim_url(origin: str) -> str:
    return "wss://" + origin[len("https://"):] + TRANSFER_CLAIM_PATH


def open_offer(port: int, transfer_key: str) -> OfferSocket:
    return OfferSocket(port, transfer_key)


def open_claim(origin: str) -> ClaimSocket:
    return ClaimSocket(claim_url(origin))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def public_answers(origin: str, *, timeout: float = 4.0) -> bool:
    """Whether a Vemal's Ears relay answers at this public address right now.

    For display only (the repoint page). False also means "could not check",
    so nothing may be started or erased on it: decisions use public_state().
    """
    try:
        with _opener().open(origin.rstrip("/") + "/healthz", timeout=timeout) as response:
            return json.loads(response.read(4096)) == HEALTH
    except urllib.error.HTTPError as error:
        error.close()
        return False
    except (OSError, ValueError):
        return False


def _online(timeout: float) -> bool:
    """Whether this PC reaches the internet right now: any HTTP reply from the control URL."""
    try:
        with _opener().open(CONTROL_URL, timeout=timeout) as response:
            response.read(1)
        return True
    except urllib.error.HTTPError as error:
        error.close()
        return True
    except (OSError, ValueError):
        return False


# HTTP statuses that positively mean nothing is connected behind the
# address: Cloudflare 530 (error 1033 and friends: the tunnel has no
# connector at all) and 521/522/523 (a proxied origin refused, timed out or
# is unreachable). Every other error status is not evidence that the old
# host is gone. 502 from a tunnel means a connector is still live and only
# its relay is down, so a second connector would split members; 401, 403,
# 407, 429, 451 and 503 are a proxy, a WAF or a rate limit that may sit in
# front of a relay that is still running; a 404 or a redirect is something
# else answering, which can be the old connector's catch-all rule.
_NO_ORIGIN_STATUSES = frozenset({521, 522, 523, 530})


def _probe(origin: str, timeout: float, online) -> str:
    try:
        with _opener().open(origin.rstrip("/") + "/healthz", timeout=timeout) as response:
            body = response.read(4096)
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        return QUIET if status in _NO_ORIGIN_STATUSES else UNKNOWN
    except (OSError, ValueError) as error:
        reason = getattr(error, "reason", error)
        if isinstance(reason, (socket.gaierror, ssl.SSLError)) or not isinstance(reason, OSError):
            # A name that does not resolve or a broken TLS session says
            # nothing about the host behind the address.
            return UNKNOWN
        # Refused, reset or timed out. That is what a dead PC behind your own
        # forwarding looks like, and also what an offline PC sees for every
        # address, so it only counts with proof that this PC is online.
        return QUIET if online(timeout) else UNKNOWN
    try:
        return ANSWERS if json.loads(body) == HEALTH else QUIET
    except (ValueError, RecursionError):
        return QUIET


def public_state(origin: str, *, timeout: float = 4.0, probes: int = STATE_PROBES, pause: float = STATE_PAUSE,
                 sleep=time.sleep, online=_online) -> str:
    """"answers", "quiet" or "unknown" for a host's public address, from this PC.

    "answers": a Vemal's Ears relay replied. "quiet": the address was
    reached and no relay is behind it, on every probe. "unknown": this PC
    could not tell (no internet, no DNS, broken TLS). Not being able to
    reach an address is never taken as proof that the host there is gone:
    the callers that would start something on "quiet" refuse on "unknown".
    """
    seen = set()
    for attempt in range(max(1, probes)):
        if attempt:
            sleep(pause)
        state = _probe(origin, timeout, online)
        if state == ANSWERS:
            return ANSWERS
        seen.add(state)
    return QUIET if seen == {QUIET} else UNKNOWN


def fetch(url: str, destination: Path, size: int, *, attempts: int = 6, pause: float = 5.0,
          sleep=time.sleep) -> str:
    """Download one served file to `destination` and return its SHA-256.

    The relay has one download slot and answers 503 while it is busy, so that
    is retried. More bytes than the authenticated size are never written.
    """
    for attempt in range(attempts):
        digest, written = hashlib.sha256(), 0
        try:
            with _opener().open(url, timeout=30) as response, Path(destination).open("wb") as stream:
                while chunk := response.read(1_048_576):
                    written += len(chunk)
                    if written > size:
                        raise ValueError("oversized")
                    digest.update(chunk)
                    stream.write(chunk)
            if written != size:
                raise ValueError("short")
            return digest.hexdigest()
        except urllib.error.HTTPError as error:
            busy = error.code == 503
            error.close()
        except (OSError, ValueError):
            busy = False
        Path(destination).unlink(missing_ok=True)
        if not busy or attempt == attempts - 1:
            break
        sleep(pause)
    raise OSError("download failed")
