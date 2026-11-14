"""Sender and receiver sequences of a host-to-host transfer, and their recovery.

The old PC (Sender) only ever goes down; the new PC (Receiver) starts only on
proof that the old one was released. The proof is R, a random secret the old
host made at offer time: it leaves the old PC (RELEASE frame, or the release
code shown to the operator) only after "released" is durably recorded there,
and "released" is recorded only after the old caption worker was verified
stopped and disabled. So there are never two workers on one bot identity.

Order of one move, over the old host's own relay:

    new -> old   hello                                    (within 5 s)
    old -> new   hello, share, offer id
    new -> old   share, confirmation      the claim confirms FIRST: the old
    old -> new   confirmation             host sends nothing derived from the
                                          session until it has verified it
    old -> new   CARD        non-secret identity card; nothing else is sent
                             before both operators confirmed guild and server ID
    new -> old   READY       after the downloads were mirrored and verified
    old          receipt "sending", freeze (flag file + relay control), worker
                 disabled, stopped and verified, late snapshot; the relay and
                 tunnel keep running (and come back frozen after a reboot)
    old -> new   bundle frames, the last one an authenticated "sending complete"
    new          receipt "pending", staged import, one rename, services
                 registered DISABLED
    new -> old   STAGED
    old -> new   DELTA       typed-chat profile writes that landed after the
                             snapshot, encrypted like the bundle
    new -> old   APPLIED
    old          receipt "released" written through to disk
    old -> new   RELEASE(R)
    old          maintenance announce, tunnel then relay stopped and disabled,
                 services removed, state archived, receipt "moved"
    new          receipt "active", relay, worker; the tunnel last and only once
                 the old public address is verified quiet

Service control, the relay sockets, downloads and the public health check are
injected, so every sequence runs in tests against fakes. Captions and typed
chat are untouched by all of this: no audio or transcript is stored or moved,
and typed chat keeps depending on the relay alone until that relay stops.
"""

from __future__ import annotations

from collections import deque
from contextlib import ExitStack
from pathlib import Path
import re
import secrets
import socket
import threading
import time

from Hosting import control, handoff, service, setup, transfer, transfer_net, transfer_pake
from Hosting.transfer import TransferError
from Hosting.version import HOST_VERSION


RELAY, WORKER, TUNNEL = control.RELAY, control.WORKER, control.TUNNEL
READY_WAIT_SECONDS = 30 * 60        # the new operator reads the card, downloads are mirrored
SENDER_RESTORE_SECONDS = 120        # silence after which the old host restores itself
RELEASE_ACK_SECONDS = 10
BUNDLE_WAIT_SECONDS = 240           # the old host stops its worker and snapshots first
DELTA_WAIT_SECONDS = 60
RELEASE_WAIT_SECONDS = 90
PUBLIC_QUIET_SECONDS = 60
# The emergency override is only offered after a wait longer than the old
# host's own restore timeout, so a host that was merely slow has restored
# itself (and answers again at its public address) before anyone can use it.
OVERRIDE_WAIT_SECONDS = 10 * 60
OVERRIDE_PHRASE = "THE OLD PC IS GONE FOR GOOD"
PROGRESS_STAGES = frozenset({"downloads", "importing", "services"})
CARD_KEYS = frozenset({"guild_name", "guild_id", "application_id", "public_origin", "port",
                       "tunnel_enabled", "host_version", "artifacts"})

NOTHING_MOVED = "Nothing was moved. This host keeps running here."
RESTORE_FAILED = ("The move was cancelled, but this host could not be put back automatically. "
                  "Reopen the Host app and choose Keep hosting here.")
RELEASED_UNFINISHED = ("This host was released to the other PC but could not finish stopping here. "
                       "Reopen the Host app and choose Finish the move.")
NOT_EMPTY = ("This PC already has a host. A transfer needs an empty Host install. Uninstall Vemal's Ears "
             "Host and remove its saved state, or use a different PC, then choose Check again.")
WRONG_CODE = "That code did not match. Check the code shown on the old PC and try again."
NO_OFFER = ("No transfer is waiting at that address. Check the address, and that the old PC "
            "is showing a transfer code.")
VERSION_ERROR = "Both PCs need the same Vemal's Ears Host version. Update the older one, then try again."
DOWNLOAD_HELP = " Check the internet connection on both PCs, then start again with a new code."
ANSWERS, QUIET, UNKNOWN = transfer_net.ANSWERS, transfer_net.QUIET, transfer_net.UNKNOWN
# After this PC's own relay and tunnel were stopped for the check below, the
# edge needs a moment to notice before the public address is asked.
KEEP_SETTLE_SECONDS = 3
TAKEN_OVER = ("Another PC is already hosting at this host's public address, so nothing was started here: "
              "two PCs must never run the same bot. Choose Finish the move to lock this PC's copy as an archive.")
TAKEN_OVER_CANCELLED = ("Another PC is already hosting at this host's public address, so nothing was started "
                        "here: two PCs must never run the same bot. This PC's copy is out of date; uninstall "
                        "Vemal's Ears Host on this PC.")
CANNOT_CHECK = ("This PC cannot reach the internet address to check that no other PC took over this host. "
                "Nothing was started. Fix the connection, then choose Keep hosting here again.")
OVERRIDE_CANNOT_CHECK = ("This PC cannot reach the internet address to check that the old host is gone. "
                         "Fix the connection and try again.")
ERASE_UNCONFIRMED = ("The other PC never confirmed that it took over. Check on that PC that it shows its "
                     "dashboard and that captions work, then confirm that here before erasing.")


class _Cancelled(Exception):
    pass


class _Stop(Exception):
    """The move cannot continue; the text is safe to show."""


class _Abort(_Stop):
    """The other host said, in an authenticated frame, that it stopped the move."""


def _records(host, records):
    return records if records is not None else transfer.TransferRecords(transfer.records_directory(host.state))


def _version(text: str) -> tuple:
    return tuple(int(part) for part in re.findall(r"[0-9]+", text)[:4])


def _message(frame) -> dict:
    """A pre-session peer message: a small JSON object in a binary frame."""
    if type(frame) is not bytes or len(frame) > transfer.FRAME_LIMIT:
        return {}
    try:
        value = transfer._json(frame)
    except TransferError:
        return {}
    return value if type(value) is dict else {}


def _hex(value, length: int) -> bytes | None:
    if type(value) is not str or len(value) != 2 * length or re.fullmatch(r"[0-9a-f]*", value) is None:
        return None
    return bytes.fromhex(value)


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            listener.bind(("127.0.0.1", int(port)))
        except OSError:
            return False
    return True


def _remove(path: Path) -> None:
    """Erase one small private file if it is there."""
    try:
        if path.is_symlink() or path.exists():
            transfer._erase(path)
    except OSError:
        raise TransferError("A temporary transfer file could not be erased. Close other programs and retry.") from None


def _key_file(host) -> Path:
    return host.state / "secrets" / "transfer-key"


def _freeze_flag(host) -> Path:
    return host.state / "transfer-frozen"


def _write_new(path: Path, data: bytes) -> None:
    _remove(path)
    try:
        handoff._write_private(path, data)
    except OSError:
        raise TransferError("A transfer file could not be written. Nothing was moved.") from None


def identity(host) -> dict:
    """The non-secret identity card of this standalone host, for the operator to confirm."""
    try:
        settings = host.read_settings()
    except (control.HostError, service.ConfigurationError) as error:
        raise TransferError(str(error)) from None
    if not settings.get("configured") or settings.get("mode") != service.STANDALONE:
        raise TransferError("Only a configured standalone host can be moved to another PC.")
    try:
        origin = transfer_net.public_origin(settings["public_http_url"])
    except setup.SetupError:
        raise TransferError("This host's public address cannot be used for a move. Check Edit settings.") from None
    return {"guild_name": settings["guild_name"], "guild_id": settings["guild_id"],
            "application_id": settings["application_id"], "public_origin": origin,
            "port": settings["port"], "tunnel_enabled": settings["tunnel_enabled"],
            "host_version": HOST_VERSION}


def _card(value) -> dict:
    """Validate a received identity card; nothing in it is secret."""
    try:
        if type(value) is not dict or set(value) != CARD_KEYS:
            raise TransferError(transfer.FRAME_ERROR)
        name = value["guild_name"]
        if (type(name) is not str or not 1 <= len(name) <= 80 or any(not 32 <= ord(char) <= 126 for char in name)
                or type(value["guild_id"]) is not str or not re.fullmatch(r"[0-9]{17,20}", value["guild_id"])
                or type(value["application_id"]) is not str
                or not re.fullmatch(r"[0-9]{17,20}", value["application_id"])
                or type(value["public_origin"]) is not str
                or transfer_net.public_origin(value["public_origin"]) != value["public_origin"]
                or type(value["port"]) is not int or not 1 <= value["port"] <= 65535
                or type(value["tunnel_enabled"]) is not bool
                or type(value["host_version"]) is not str
                or not re.fullmatch(r"[0-9A-Za-z.+-]{1,40}", value["host_version"])):
            raise TransferError(transfer.FRAME_ERROR)
        return {**value, "artifacts": transfer.card_artifacts(value["artifacts"])}
    except setup.SetupError:
        raise TransferError(transfer.FRAME_ERROR) from None


def _triple(artifact: dict) -> dict:
    return {key: artifact[key] for key in ("name", "size", "sha256")}


# ---------------------------------------------------------------------------
# Old PC
# ---------------------------------------------------------------------------

def _restore_sender(host, records, *, reason: str = "", offer_socket=None, notify=None) -> None:
    """Cancel an unreleased move and put this host back exactly as it was.

    Safe to repeat: the receipt keeps the original start types, so a restore
    that failed half way is simply run again ("Keep hosting here"). The
    receipt is cancelled first, which erases R: a receiver left with a pending
    copy can then never be handed a valid release proof. Only after that is
    the other PC told (notify), so its copy may be discarded safely.
    """
    current = records.read()
    if not current or current["role"] != transfer.SENDER:
        raise TransferError("This PC has no move to cancel.")
    if current["state"] in ("offered", "sending"):
        records.advance("cancelled", reason=reason[:200])
    elif current["state"] != "cancelled":
        raise TransferError("This host was already released to the other PC. It can no longer be kept here.")
    if notify is not None:
        notify()
    services = current["details"].get("services")
    try:
        if host.state.exists():
            _remove(_freeze_flag(host))
            _remove(_key_file(host))
        if offer_socket is not None:
            try:
                offer_socket.control("transfer_unfreeze")
            except transfer_net.Closed:
                pass
        if type(services) is dict:
            for name in (RELAY, TUNNEL, WORKER):
                saved = services.get(name)
                if type(saved) is dict and saved.get("start"):
                    host.restore_service(name, saved["start"], running=saved.get("status") == "Running")
        transfer.erase_leftover_snapshots(records.directory)
    except control.HostError as error:
        raise TransferError(str(error)) from None


def _complete_move(host, records, *, wall=time.time) -> dict:
    """After "released": stop for good, remove the services, archive the state. Safe to repeat."""
    current = records.read()
    if not current or current["role"] != transfer.SENDER or current["state"] not in ("released", "moved"):
        raise TransferError("This host has not been released.")
    archive = transfer.archive_path(host.state)
    if current["state"] == "released":
        try:
            if host.state.exists():
                states = host.service_states()
                # The tunnel goes first so no public traffic reaches a relay
                # that is about to stop; each must be STOPPED and disabled.
                for name in (WORKER, TUNNEL, RELAY):
                    if states.get(name, {}).get("status", "Not installed") != "Not installed":
                        host.disable_service(name, stop=True)
                host.remove_services()
                _remove(_key_file(host))
                _remove(_freeze_flag(host))
                transfer.erase_leftover_snapshots(records.directory)
                transfer.archive_state(host.state)
            elif not archive.exists():
                raise TransferError("The released host's state is missing. Check this PC before continuing.")
        except control.HostError as error:
            raise TransferError(str(error)) from None
        records.advance("moved", archive=str(archive), moved_at=wall())
    return {"state": "moved", "archive": str(archive),
            "release_code": transfer_pake.release_code(records.release_secret())}


class Sender:
    """The old PC's side of one move. open_offer(), then run() on a worker thread."""

    def __init__(self, host, *, records=None, open_offer=transfer_net.open_offer,
                 clock=time.monotonic, wall=time.time) -> None:
        self.host = host
        self.records = _records(host, records)
        self._open_offer = open_offer
        self._clock, self._wall = clock, wall
        self.ready_wait = READY_WAIT_SECONDS
        self.silence = SENDER_RESTORE_SECONDS
        self.ack_wait = RELEASE_ACK_SECONDS
        self._cancel = threading.Event()
        self._stack = ExitStack()
        self._socket = None
        self._queue = deque()
        self._claim = None
        self._confirmed = False
        self._code = self._transfer_id = self._commitment = self._card = self._control = None
        self._released = False
        self._running = False
        self._state_lock = threading.Lock()     # close() against the start of run()
        self._send_lock = threading.Lock()      # one control frame on the wire at a time

    # -- offer ---------------------------------------------------------------

    def open_offer(self) -> dict:
        """Make the one-time code and open the offer on this host's own relay.

        Returns the two things to show, as separate fields: the public
        hostname and the code. The code is never part of an address.
        """
        card = identity(self.host)
        try:
            if self.host.transfer_hold():
                raise TransferError("This host is already part of a move. Finish or cancel that first.")
            states = self.host.service_states()
        except control.HostError as error:
            raise TransferError(str(error)) from None
        if states[RELAY]["status"] != "Running":
            raise TransferError("Start this host first. The move travels through its own relay.")
        if transfer.archive_path(self.host.state).exists():
            raise TransferError(transfer.ARCHIVE_ERROR)
        self._stack.enter_context(self.records.locked())
        try:
            # A plaintext database copy never outlives the move that made it.
            transfer.erase_leftover_snapshots(self.records.directory)
            stale = self.records.read()
            if stale and stale["role"] == transfer.SENDER and stale["state"] == "offered":
                # A code that was shown and never used (the app was closed).
                self.records.advance("cancelled", reason="replaced by a new offer")
            artifacts = transfer.served_artifacts(self.host.state)
            self._transfer_id, self._commitment = self.records.offer(
                public_origin=card["public_origin"], offered_at=self._wall())
            key = secrets.token_urlsafe(32)
            _write_new(_key_file(self.host), key.encode("ascii") + b"\n")
            try:
                self._socket = self._open_offer(card["port"], key)
            except transfer_net.Unavailable:
                raise TransferError("This host's relay did not accept the transfer. Restart the host "
                                    "once, then try again.") from None
            self._code = transfer_pake.new_code()
        except BaseException:
            self._abandon("offer failed")
            raise
        self._card = {**card, "artifacts": artifacts}
        return {"hostname": card["public_origin"][len("https://"):], "code": transfer_pake.format_code(self._code),
                "ttl_seconds": getattr(self._socket, "ttl_seconds", 600), "transfer_id": self._transfer_id,
                "forwarding": not card["tunnel_enabled"]}

    def cancel(self) -> None:
        """Ask a running move to stop and keep hosting here. Honoured until the release point."""
        self._cancel.set()

    def close(self) -> None:
        """Give up an offer that run() was never called for.

        Once run() has started this only asks it to cancel: run() alone
        changes the receipt from then on, so a close from another thread can
        never cancel and restore around the release point.
        """
        with self._state_lock:
            if self._running:
                self._cancel.set()
                return
            if self._transfer_id is not None and not self._released:
                self._abandon("closed")

    def _abandon(self, reason: str) -> None:
        try:
            current = self.records.read()
            if (current and current["role"] == transfer.SENDER and current["state"] in ("offered", "sending")
                    and current["transfer_id"] == self._transfer_id):
                _restore_sender(self.host, self.records, reason=reason, offer_socket=self._socket,
                                notify=self._tell_abort)
            elif self.host.state.exists():
                _remove(_key_file(self.host))
        finally:
            if self._socket is not None:
                self._socket.close()
                self._socket = None
            self._code = None
            self._stack.close()

    def _tell_abort(self) -> None:
        """Tell the new PC, authenticated, that this move is cancelled for good."""
        if self._socket is not None and self._control is not None:
            try:
                self._send("abort")
            except (transfer_net.Closed, TransferError, OSError):
                pass

    # -- socket --------------------------------------------------------------

    def _event(self, timeout: float):
        """The next relay notice or peer frame; relay notices that end the move raise."""
        deadline = self._clock() + timeout
        while True:
            if self._cancel.is_set() and not self._released:
                raise _Cancelled()
            remaining = deadline - self._clock()
            if remaining <= 0:
                return None
            try:
                event = self._socket.receive(min(remaining, 0.5))
            except transfer_net.Closed as closed:
                if "failures" in closed.reason:
                    raise _Stop("The code was entered wrongly too many times, so it was cancelled. "
                                + NOTHING_MOVED) from None
                if "expired" in closed.reason or "claims" in closed.reason:
                    raise _Stop("The transfer code expired before it was used. " + NOTHING_MOVED) from None
                raise _Stop("The connection to this host's relay was lost.") from None
            if event is None:
                continue
            kind, value = event
            if kind == "notice":
                if value.get("type") == "transfer_claim_closed" and value.get("claim") == self._claim:
                    if self._confirmed:
                        raise _Stop("The other PC disconnected.")
                    self._claim = None
                    return "closed", value
                if value.get("type") == "transfer_ended":
                    continue    # the close frame that follows carries the reason
                return "notice", value
            return "peer", value

    def _peer(self, timeout: float):
        if self._queue:
            return self._queue.popleft()
        deadline = self._clock() + timeout
        while True:
            event = self._event(deadline - self._clock())
            if event is None:
                return None
            if event[0] == "peer":
                return event[1]

    def _notice(self, kind: str, timeout: float = 10.0) -> dict:
        deadline = self._clock() + timeout
        while True:
            event = self._event(deadline - self._clock())
            if event is None:
                raise _Stop("This host's relay did not answer.")
            if event[0] == "peer":
                self._queue.append(event[1])
            elif event[0] == "notice" and event[1].get("type") == kind:
                return event[1]

    def _expect(self, kind: str, timeout: float, progress) -> dict:
        """The next control message of this kind; any authenticated frame resets the silence clock."""
        deadline = self._clock() + timeout
        while True:
            frame = self._peer(deadline - self._clock())
            if frame is None:
                raise _Stop("The other PC went silent.")
            message = self._control.open(frame)
            if message["type"] == "progress" and message.get("stage") in PROGRESS_STAGES:
                progress("peer_" + message["stage"])
                deadline = self._clock() + timeout
            elif message["type"] == "abort":
                raise _Abort("The other PC stopped the move.")
            elif message["type"] == kind:
                return message
            else:
                raise TransferError(transfer.FRAME_ERROR)

    def _send(self, kind: str, **fields) -> None:
        with self._send_lock:
            self._socket.send(self._control.seal(kind, **fields))

    # -- key agreement -------------------------------------------------------

    def _await_claim(self, progress):
        """Wait for the new PC and agree a session. The claim side confirms first.

        Nothing derived from the session leaves this host before the claim's
        confirmation has been verified, and a wrong one is reported to the
        relay as transfer_failed, so every guess at the code costs the
        guesser one of three attempts.
        """
        origin = self._card["public_origin"]
        session, phase = None, "dead"
        while True:
            event = self._event(3600)
            if event is None:
                continue
            kind, value = event
            if kind == "notice" and value.get("type") == "transfer_claim_opened":
                self._claim, session, phase = value.get("claim"), None, "hello"
                continue
            if kind != "peer" or self._claim is None:
                continue
            message = _message(value)
            if phase == "hello" and message.get("type") == "hello":
                if (message.get("protocol") != transfer_pake.PROTOCOL_VERSION
                        or type(message.get("host_version")) is not str
                        or _version(message["host_version"]) < _version(HOST_VERSION)):
                    self._socket.send(handoff._json_bytes({"type": "error", "code": "version"}))
                    phase = "dead"
                    continue
                session = transfer_pake.Session(transfer_pake.ROLE_OFFER, self._code, self._transfer_id, origin)
                self._socket.send(handoff._json_bytes({
                    "type": "hello", "protocol": transfer_pake.PROTOCOL_VERSION, "host_version": HOST_VERSION,
                    "offer_id": self._transfer_id, "share": session.share.hex()}))
                phase = "confirm"
                progress("connected")
            elif phase == "confirm" and message.get("type") == "confirm":
                share, tag = _hex(message.get("share"), transfer_pake.SHARE_BYTES), _hex(message.get("tag"), 32)
                keys = None
                try:
                    if share is not None and tag is not None:
                        candidate = session.finish(share)
                        keys = candidate if candidate.verify_confirmation(tag) else None
                except transfer_pake.PakeError:
                    keys = None
                phase = "dead"
                if keys is None:
                    self._socket.control("transfer_failed", claim=self._claim)
                    progress("wrong_code")
                    continue
                self._socket.control("transfer_confirmed", claim=self._claim)
                self._confirmed = True
                self._socket.send(handoff._json_bytes({"type": "confirm", "tag": keys.confirmation().hex()}))
                return keys
            else:
                phase = "dead"      # not a guess: the relay drops a stalled claim uncounted

    # -- the move ------------------------------------------------------------

    def run(self, progress=lambda stage: None) -> dict:
        """Carry the move through. Returns the "moved" summary with the release code.

        Until the release point any failure, silence or cancel restores this
        host and raises TransferError("... Nothing was moved ..."). After it
        this host only goes down; a failure then leaves "released" for
        finish_move() to complete.
        """
        with self._state_lock:
            if self._socket is None or self._code is None or self._running:
                raise TransferError("Show a transfer code first.")
            self._running = True
        try:
            try:
                progress("waiting")
                keys = self._await_claim(progress)
                self._code = None
                self._control = transfer.ControlChannel(self._transfer_id, keys.control_mac, transfer_pake.ROLE_OFFER)
                # The card is the only thing sent before both operators confirm.
                self._send("card", card=self._card)
                progress("confirmed")
                self._expect("ready", self.ready_wait, progress)
                release_secret = self._send_state(keys, progress)
            except _Cancelled:
                self._abandon("cancelled")
                raise TransferError("The move was cancelled. " + NOTHING_MOVED) from None
            except (_Stop, TransferError, control.HostError, transfer_net.Closed, OSError) as error:
                detail = str(error) if isinstance(error, (_Stop, TransferError, control.HostError)) else ""
                try:
                    reached = self.records.read().get("state")
                except TransferError:
                    reached = ""
                if reached == "released":
                    # "released" reached the disk but could not be confirmed:
                    # R was never handed out, and there is no way back.
                    raise TransferError(RELEASED_UNFINISHED) from None
                try:
                    self._abandon(detail or "failed")
                except (TransferError, control.HostError, OSError):
                    raise TransferError(RESTORE_FAILED) from None
                if NOTHING_MOVED in detail:
                    raise TransferError(detail) from None
                raise TransferError((detail + " " if detail else "") + NOTHING_MOVED) from None
            # Released: R may leave this PC now, and this host only goes down.
            self._released = True
            progress("released")
            acknowledged = False
            try:
                self._send("release", secret=release_secret.hex())
                self._socket.control("transfer_maintenance")
                deadline = self._clock() + self.ack_wait
                while not acknowledged and self._clock() < deadline:
                    frame = self._peer(deadline - self._clock())
                    if frame is None:
                        break
                    acknowledged = self._control.open(frame).get("type") == "active"
            except (_Stop, TransferError, transfer_net.Closed, OSError):
                pass    # a lost RELEASE is what the release code is for; the stop goes on
            if acknowledged:
                # Kept in the receipt: Erase archive asks for more when the
                # new PC never said that it took over.
                try:
                    self.records.note(acknowledged=True)
                except TransferError:
                    acknowledged = False
            self._socket.close()
            self._socket = None
            progress("stopping")
            try:
                result = _complete_move(self.host, self.records, wall=self._wall)
            except (TransferError, control.HostError, OSError):
                raise TransferError(RELEASED_UNFINISHED) from None
            progress("moved")
            return {**result, "acknowledged": acknowledged}
        finally:
            if self._socket is not None:
                self._socket.close()
                self._socket = None
            self._stack.close()

    def _send_state(self, keys, progress) -> bytes:
        """READY arrived: freeze, stop the worker, snapshot late, send, and release. Returns R."""
        host, state = self.host, self.host.state
        states = host.service_states()
        self.records.advance("sending", services=states, sending_at=self._wall())
        progress("sending")
        # The flag file is what keeps a restarted relay frozen; the control
        # frame freezes the running one. Pairing and viewer linking wait,
        # typed chat and profile writes do not.
        _write_new(_freeze_flag(host), b"1\n")
        self._socket.control("transfer_freeze")
        if self._notice("transfer_frozen").get("frozen") is not True:
            raise _Stop("This host's relay could not be frozen.")
        # Only the caption worker, which holds the bot identity, is taken
        # down: disabled first so it cannot come back with Windows or after a
        # crash, then stopped and verified. The relay and tunnel keep their
        # start types. They carry the move, and after a reboot the relay
        # comes back frozen from the flag file, so typed chat never waits for
        # somebody to open this app.
        if states[WORKER]["status"] != "Not installed":
            host.disable_service(WORKER, stop=True)
        progress("worker_stopped")
        # The card was made when the code was shown, up to forty minutes ago.
        # The new PC mirrored exactly those files, so they must still be the
        # ones this host serves and its release pointer names.
        if ([_triple(entry) for entry in transfer.served_artifacts(state)]
                != [_triple(entry) for entry in self._card["artifacts"]]):
            raise _Stop("The member downloads on this host changed during the move.")
        database = state / transfer.DATABASE
        before = transfer.profile_rows(database)
        bundle = transfer.collect_state(
            state, transfer_id=self._transfer_id, release_commitment=self._commitment,
            work_directory=self.records.directory, artifacts=[_triple(entry) for entry in self._card["artifacts"]])
        sealed = transfer.encrypt_bundle(bundle, keys)
        del bundle
        progress("bundle")
        for frame in transfer.split_frames(sealed, self._transfer_id, keys.control_mac):
            if self._cancel.is_set():
                raise _Cancelled()
            self._socket.send(frame)
        del sealed
        self.records.note(bundle_sent=True)
        self._expect("staged", self.silence, progress)
        # The new PC now holds a complete, staged copy; only from here may an
        # interrupted move be finished by hand with the release code.
        self.records.note(staged=True)
        progress("staged")
        # Member rows: sealed with the session's bundle key, never in clear.
        for part in transfer.seal_delta(transfer.profile_delta(before, database), keys):
            self._send("delta", **part)
        self._expect("applied", self.silence, progress)
        if self._cancel.is_set():
            raise _Cancelled()
        worker = host.service_states()[WORKER]
        if worker["status"] != "Stopped" or worker["start"] != control.DISABLED:
            raise _Stop("The caption worker on this PC did not stay stopped and disabled.")
        # Written through to disk before R is handed out; from here no path
        # leads back to hosting on this PC.
        return self.records.release(released_at=self._wall())


def _require_not_taken_over(host, records, current, public_state, sleep) -> None:
    """Refuse to restore a host that another PC has taken over in the meantime.

    "Keep hosting here" is chosen when the Host app is reopened on an
    interrupted move, possibly days later, and the new PC may have been
    started with its emergency step since. This PC's own relay and tunnel are
    taken off the public address first (disabled, then stopped), so an answer
    there can only come from another PC. Unless the address is then verified
    quiet, everything on this PC stays disabled and nothing is restored.

    Limit: a host behind the operator's own forwarding that still points at
    this PC hides the other PC; only the warning on the page covers that.
    """
    origin = current["details"].get("public_origin")
    if type(origin) is not str or not origin:
        raise TransferError(transfer.RECORD_ERROR)
    stopped = False
    try:
        states = host.service_states() if host.state.exists() else {}
        for name in (TUNNEL, RELAY):
            entry = states.get(name, {"status": "Not installed"})
            if entry["status"] != "Not installed" and (entry["status"] != "Stopped"
                                                       or entry["start"] != control.DISABLED):
                host.disable_service(name, stop=True)
                stopped = stopped or entry["status"] != "Stopped"
    except control.HostError as error:
        raise TransferError(str(error)) from None
    if stopped:
        sleep(KEEP_SETTLE_SECONDS)
    state = public_state(origin)
    if state == ANSWERS:
        if current["state"] != "sending":
            raise TransferError(TAKEN_OVER_CANCELLED)
        # The other PC is live, so this copy may be archived even if that PC
        # was never seen to confirm it (finish_move).
        records.note(taken_over=True)
        raise TransferError(TAKEN_OVER)
    if state != QUIET:
        raise TransferError(CANNOT_CHECK)


def keep_hosting(host, records=None, *, public_state=transfer_net.public_state, sleep=time.sleep) -> dict:
    """"Keep hosting here": cancel an interrupted, unreleased move and restore this host.

    Once the caption worker was stopped for a move, this first makes sure no
    other PC is hosting (see _require_not_taken_over). An unused offer, where
    nothing was stopped or sent, is simply withdrawn.
    """
    records = _records(host, records)
    with records.locked():
        current = records.read()
        if (current and current["role"] == transfer.SENDER and current["state"] in ("sending", "cancelled")
                and type(current["details"].get("services")) is dict):
            _require_not_taken_over(host, records, current, public_state, sleep)
        _restore_sender(host, records, reason="kept by the operator")
    return status(host, records)


def finish_move(host, records=None, *, wall=time.time) -> dict:
    """"Finish the move": release (if the whole bundle was sent) and stop for good.

    Returns the release code to type on the new PC. For a move interrupted
    after the new PC confirmed its staged copy, this is the operator's
    decision to let that copy take over. Before that confirmation it is
    refused: the new PC erases an incomplete copy by itself, and releasing
    would leave no host anywhere. The one exception is a copy that was seen
    hosting at the public address (Keep hosting here found it there).
    """
    records = _records(host, records)
    with records.locked():
        current = records.read()
        if not current or current["role"] != transfer.SENDER:
            raise TransferError("This PC has no move to finish.")
        if current["state"] == "sending":
            if current["details"].get("staged") is not True and current["details"].get("taken_over") is not True:
                raise TransferError("The other PC never confirmed a complete copy of this host. "
                                    "Choose Keep hosting here.")
            try:
                states = host.service_states()
                if states[WORKER]["status"] != "Not installed":
                    host.disable_service(WORKER, stop=True)
            except control.HostError as error:
                raise TransferError(str(error)) from None
            records.release(released_at=wall(), finished_by_operator=True)
        elif current["state"] not in ("released", "moved"):
            raise TransferError("This PC has no move to finish.")
        try:
            return _complete_move(host, records, wall=wall)
        except control.HostError as error:
            raise TransferError(str(error)) from None


def erase_archive(host, records=None, *, checked_other_pc: bool = False) -> None:
    """"Erase archive": overwrite and remove the moved host's archive, then its receipt.

    The archive holds the only copy of the release code. When the new PC
    never acknowledged the release, erasing needs the operator's extra
    confirmation that the other PC was checked and is hosting.
    """
    records = _records(host, records)
    with records.locked():
        current = records.read()
        if not current or current["role"] != transfer.SENDER or current["state"] != "moved":
            raise TransferError("This PC holds no archive of a moved host.")
        if current["details"].get("acknowledged") is not True and checked_other_pc is not True:
            raise TransferError(ERASE_UNCONFIRMED)
        transfer.erase_archive(host.state)
        records.erase(archive_erased=True)


# ---------------------------------------------------------------------------
# New PC
# ---------------------------------------------------------------------------

def _checked(state) -> str:
    """Anything but a clear "answers" or "quiet" is "unknown"."""
    return state if state in (ANSWERS, QUIET) else UNKNOWN


def _settle(origin: str, public_state, *, wait: float, clock, sleep) -> str:
    """The old public address, asked until it is quiet or the wait is over."""
    deadline = clock() + wait
    while True:
        state = _checked(public_state(origin))
        if state == QUIET or clock() >= deadline:
            return state
        sleep(min(2.0, max(0.0, deadline - clock())))


def _finish_activation(host, records, *, public_state, clock, sleep, quiet_wait: float, known: str = "") -> dict:
    """The receipt says "active": mark the state, then relay, worker and the tunnel last.

    One tunnel token on two PCs would split members between them, so the
    tunnel stays withheld (control.release_tunnel is not called) unless the
    old public address is verified quiet. Not being able to check is not
    quiet. `known` is a state the caller has just verified itself.
    """
    current = records.read()
    if not current or current["role"] != transfer.RECEIVER or current["state"] != "active":
        raise TransferError("This host has not been released by the old PC.")
    origin = current["details"].get("public_origin", "")
    tunnel = current["details"].get("tunnel_enabled") is True
    try:
        withheld = tunnel and host.read_settings()["tunnel_withheld"]
        state = QUIET
        if withheld:
            state = known or (_settle(origin, public_state, wait=quiet_wait, clock=clock, sleep=sleep)
                              if origin else UNKNOWN)
        if transfer.activation(host.state) == transfer.ACTIVATION_PENDING:
            transfer.mark_active(host.state)
        host.enable_services(tunnel=False)
        host.start_services(tunnel=False)
        if tunnel and state == QUIET:
            host.release_tunnel()
    except (control.HostError, service.ConfigurationError) as error:
        raise TransferError(str(error)) from None
    return {"state": "active", "tunnel_enabled": tunnel, "tunnel_started": tunnel and state == QUIET,
            "public_still_answering": state == ANSWERS, "public_unchecked": state == UNKNOWN,
            "forwarding": not tunnel, "override": current["details"].get("override") is True}


def _require_published(host) -> None:
    if transfer.activation(host.state) != transfer.ACTIVATION_PENDING:
        raise TransferError("The received host was not completely staged on this PC. Discard it and start again.")


def activate(host, records=None, *, release_secret: bytes | None = None, release_code: str | None = None,
             public_state=transfer_net.public_state, clock=time.monotonic, sleep=time.sleep,
             quiet_wait: float = PUBLIC_QUIET_SECONDS) -> dict:
    """Start a received host on proof of release: R from RELEASE, or the typed release code."""
    records = _records(host, records)
    with records.locked():
        current = records.read()
        if current and current["role"] == transfer.RECEIVER and current["state"] == "pending":
            _require_published(host)
            if release_secret is not None:
                records.activate(release_secret)
            else:
                records.activate_with_code(release_code if isinstance(release_code, str) else "")
        return _finish_activation(host, records, public_state=public_state, clock=clock, sleep=sleep,
                                  quiet_wait=quiet_wait)


def override_state(host, records=None, *, wall=time.time) -> dict:
    """Whether the emergency override may be offered yet, and how long is left to wait."""
    current = _records(host, records).read()
    if (not current or current["role"] != transfer.RECEIVER or current["state"] != "pending"
            or current["details"].get("sending_complete") is not True
            or transfer.activation(host.state) != transfer.ACTIVATION_PENDING):
        return {"possible": False, "wait_remaining": 0}
    staged = current["details"].get("staged_at")
    waited = wall() - staged if type(staged) in (int, float) else 0
    return {"possible": True, "wait_remaining": max(0, int(OVERRIDE_WAIT_SECONDS - waited + 0.999)),
            "phrase": OVERRIDE_PHRASE}


def activate_by_override(host, records=None, *, attestation: str, public_state=transfer_net.public_state,
                         wall=time.time, clock=time.monotonic, sleep=time.sleep) -> dict:
    """Emergency only: the old PC is dead for good and can never release.

    Every condition must hold: the whole bundle arrived authenticated, the
    wait is longer than the old host's own restore timeout, the operator
    typed the attestation, and the public address is verified quiet from
    this PC. "Verified" needs this PC to be online: an address that could
    not be reached at all proves nothing about the old host, and a dropped
    connection on this PC is exactly what interrupts a move while the old
    host restores itself and keeps hosting. This is the one human-trust path
    around the release proof.
    """
    records = _records(host, records)
    with records.locked():
        state = override_state(host, records, wall=wall)
        if not state["possible"]:
            raise TransferError("The old host never finished sending. Discard this transfer and start again.")
        if state["wait_remaining"] > 0:
            raise TransferError("Wait for the old PC a little longer. This emergency step unlocks in "
                                + str((state["wait_remaining"] + 59) // 60) + " minute(s).")
        if not isinstance(attestation, str) or attestation.strip() != OVERRIDE_PHRASE:
            raise TransferError("Type the sentence exactly as shown to confirm the old PC is gone for good.")
        origin = records.read()["details"].get("public_origin", "")
        public = _checked(public_state(origin)) if origin else UNKNOWN
        if public == ANSWERS:
            raise TransferError("The old host is still answering at its public address, so it is not gone. "
                                "Finish or cancel the move on the old PC.")
        if public != QUIET:
            raise TransferError(OVERRIDE_CANNOT_CHECK)
        records.activate_by_override()
        return _finish_activation(host, records, public_state=public_state, clock=clock, sleep=sleep,
                                  quiet_wait=0, known=QUIET)


def start_tunnel(host, records=None, *, public_state=transfer_net.public_state) -> dict:
    """"Connect members": start the withheld tunnel once the old public address is verified quiet.

    Works from the durable mark in the host settings, whatever the transfer
    receipt says by now. The mark is cleared nowhere else.
    """
    records = _records(host, records)
    with records.locked():
        try:
            if host.transfer_hold():
                raise TransferError("This host is part of a move. Finish that first.")
            settings = host.read_settings()
            if not settings.get("configured") or settings.get("mode") != service.STANDALONE:
                raise TransferError("This PC has no host to connect.")
            tunnel = settings["tunnel_enabled"] is True
            state = QUIET
            if tunnel and settings["tunnel_withheld"]:
                state = _checked(public_state(transfer_net.public_origin(settings["public_http_url"])))
                if state == QUIET:
                    host.release_tunnel()
        except (control.HostError, service.ConfigurationError, setup.SetupError) as error:
            raise TransferError(str(error)) from None
        return {"state": "active", "tunnel_enabled": tunnel, "tunnel_started": tunnel and state == QUIET,
                "public_still_answering": state == ANSWERS, "public_unchecked": state == UNKNOWN,
                "forwarding": not tunnel, "override": False}


def discard_unused_staging(host, records=None) -> bool:
    """Remove a staging directory that holds a prepared speech runtime and no host state.

    For a PC where Receive was opened and a new host is set up instead: the
    runtime prepared for the transfer (about 1.2 GB) would otherwise stay
    beside the state directory for good. Never touches a transfer in
    progress or a staging directory that holds host state.
    """
    staging = transfer.staging_path(host.state)
    if not staging.is_dir():
        return False
    records = _records(host, records)
    with records.locked():
        current = records.read()
        if current and current["state"] not in transfer._TERMINAL:
            return False
        if any((staging / name).exists() for name in transfer._state_files()):
            return False
        transfer.discard_staging(host.state)
        return True


def discard_pending(host, records=None) -> None:
    """"Discard and start again": erase a received copy that was never released."""
    records = _records(host, records)
    with records.locked():
        current = records.read()
        if current and current["role"] == transfer.SENDER and current["state"] not in transfer._TERMINAL:
            raise TransferError("This PC is sending a host, not receiving one.")
        if current and current["role"] == transfer.RECEIVER and current["state"] == "active":
            raise TransferError("This host is active. Nothing was discarded.")
        _discard(host, records, everything=True)


def _discard(host, records, *, everything: bool, reason: str = "") -> None:
    """Remove a pending copy: its disabled services, its state, then the receipt."""
    state = host.state
    try:
        if state.exists():
            if transfer.activation(state) != transfer.ACTIVATION_PENDING:
                raise TransferError("This PC's host is not a pending transfer. Nothing was erased.")
            if any(entry["status"] != "Not installed" for entry in host.service_states().values()):
                host.remove_services()
            transfer.discard_published(state)
        staging = transfer.staging_path(state)
        if everything:
            transfer.discard_staging(state)
        elif staging.is_dir():
            # Keep the prepared runtime and verified downloads for the retry.
            for name in transfer._state_files():
                if (staging / name).exists():
                    transfer._erase(staging / name)
    except control.HostError as error:
        raise TransferError(str(error)) from None
    except OSError:
        raise TransferError("Could not erase the pending transfer. Close other programs and retry.") from None
    current = records.read()
    if current and current["role"] == transfer.RECEIVER and current["state"] == "pending":
        records.advance("discarded", reason=reason[:200])


class Receiver:
    """The new, empty PC's side of one move: preflight(), prepare(), connect(), accept()."""

    def __init__(self, host, *, records=None, open_claim=transfer_net.open_claim, fetch=transfer_net.fetch,
                 public_state=transfer_net.public_state, port_free=_port_free,
                 clock=time.monotonic, wall=time.time, sleep=time.sleep) -> None:
        self.host = host
        self.records = _records(host, records)
        self._open_claim, self._fetch = open_claim, fetch
        self._public_state, self._port_free = public_state, port_free
        self._clock, self._wall, self._sleep = clock, wall, sleep
        self.bundle_wait = BUNDLE_WAIT_SECONDS
        self.delta_wait = DELTA_WAIT_SECONDS
        self.release_wait = RELEASE_WAIT_SECONDS
        self.quiet_wait = PUBLIC_QUIET_SECONDS
        self._cancel = threading.Event()
        self._stack = ExitStack()
        self._send_lock = threading.Lock()      # cancel() seals its abort on another thread
        self._socket = self._control = self._keys = self._card = self._origin = self._transfer_id = None

    @property
    def staging(self) -> Path:
        return transfer.staging_path(self.host.state)

    def preflight(self) -> Path:
        """Refuse anything but an empty Host install, then make the protected staging directory."""
        current = self.records.read()
        if current and current["state"] == "moved":
            # A reverse move onto the PC the host once left: its stale archive goes first.
            raise TransferError(transfer.ARCHIVE_ERROR)
        if current and current["state"] not in transfer._TERMINAL:
            raise TransferError("A transfer is already in progress on this PC.")
        try:
            configured = self.host.read_settings().get("configured")
            installed = any(entry["status"] != "Not installed" for entry in self.host.service_states().values())
        except (control.HostError, service.ConfigurationError):
            configured = installed = True
        if configured or installed or self.host.state.exists():
            raise TransferError(NOT_EMPTY)
        if transfer.archive_path(self.host.state).exists():
            raise TransferError(transfer.ARCHIVE_ERROR)
        staging = self.staging
        if staging.is_dir() and not any((staging / name).exists() for name in transfer._state_files()):
            return staging      # an earlier attempt's prepared runtime, holding no host state
        return transfer.create_staging(self.host.state)

    def prepare(self, progress=lambda message: None) -> None:
        """Prepare the GPU speech runtime inside staging, before any cutover."""
        try:
            self.host.prepare_staged_runtime(self.preflight(), progress)
        except control.HostError as error:
            raise TransferError(str(error)) from None

    def cancel(self) -> None:
        self._cancel.set()
        self._close(abort=True)

    def _close(self, *, abort: bool = False) -> None:
        peer, self._socket = self._socket, None
        if peer is not None:
            if abort and self._control is not None:
                try:
                    with self._send_lock:
                        peer.send(self._control.seal("abort"))
                except (transfer_net.Closed, TransferError):
                    pass
            peer.close()
        self._stack.close()

    def _receive(self, timeout: float) -> bytes:
        deadline = self._clock() + timeout
        while True:
            if self._cancel.is_set() or self._socket is None:
                raise _Cancelled()
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise _Stop("The old PC went silent.")
            frame = self._socket.receive(min(remaining, 0.5))
            if frame is not None:
                return frame

    def _expect(self, kind: str, timeout: float) -> dict:
        message = self._control.open(self._receive(timeout))
        if message["type"] == "abort":
            raise _Abort("The old PC stopped the move.")
        if message["type"] != kind:
            raise TransferError(transfer.FRAME_ERROR)
        return message

    def _send(self, kind: str, **fields) -> None:
        peer = self._socket
        if peer is None:
            raise _Cancelled()
        with self._send_lock:
            peer.send(self._control.seal(kind, **fields))

    # -- key agreement -------------------------------------------------------

    def connect(self, address: str, code: str) -> dict:
        """Type the address and the code as two fields; returns the authenticated identity card.

        The code is used only inside the key agreement. It is never sent,
        never part of a URL, and a wrong one fails here without a second try
        on the same connection.
        """
        try:
            origin = transfer_net.public_origin(address)
            code = transfer_pake.parse_code(code)
        except (setup.SetupError, transfer_pake.PakeError) as error:
            raise TransferError(str(error)) from None
        self.preflight()
        self._stack.enter_context(self.records.locked())
        try:
            try:
                self._socket = self._open_claim(origin)
            except transfer_net.Unavailable:
                raise TransferError(NO_OFFER) from None
            self._socket.send(handoff._json_bytes({
                "type": "hello", "protocol": transfer_pake.PROTOCOL_VERSION, "host_version": HOST_VERSION}))
            hello = _message(self._receive(15))
            share = _hex(hello.get("share"), transfer_pake.SHARE_BYTES)
            offer_id = hello.get("offer_id")
            if (hello.get("type") != "hello" or hello.get("protocol") != transfer_pake.PROTOCOL_VERSION
                    or share is None or type(offer_id) is not str
                    or transfer._HEX32.fullmatch(offer_id) is None):
                raise TransferError(VERSION_ERROR if hello.get("code") == "version"
                                    or hello.get("type") == "hello" else NO_OFFER)
            try:
                session = transfer_pake.Session(transfer_pake.ROLE_CLAIM, code, offer_id, origin)
                keys = session.finish(share)
            except transfer_pake.PakeError as error:
                raise TransferError(str(error)) from None
            self._socket.send(handoff._json_bytes({
                "type": "confirm", "share": session.share.hex(), "tag": keys.confirmation().hex()}))
            try:
                tag = _hex(_message(self._receive(20)).get("tag"), 32)
            except (transfer_net.Closed, _Stop):
                tag = None      # the old host drops a claim whose confirmation was wrong
            if tag is None or not keys.verify_confirmation(tag):
                raise TransferError(WRONG_CODE)
            self._keys, self._origin, self._transfer_id = keys, origin, offer_id
            self._control = transfer.ControlChannel(offer_id, keys.control_mac, transfer_pake.ROLE_CLAIM)
            card = _card(self._expect("card", 20).get("card"))
            if card["public_origin"] != origin:
                raise TransferError(transfer.FRAME_ERROR)
            self._card = card
            return card
        except BaseException as error:
            self._close()
            if isinstance(error, (transfer_net.Closed, _Stop, _Cancelled)):
                raise TransferError("The connection to the old PC was lost. Start again with a new code.") from None
            raise

    # -- the move ------------------------------------------------------------

    def accept(self, progress=lambda stage: None) -> dict:
        """"Move it here": mirror downloads, receive, stage, install disabled, wait for RELEASE.

        Returns {"state": "active", ...} when the host is running here, or
        {"state": "waiting"} when the old PC fell silent after this PC said
        STAGED: the pending copy then waits for the release code (activate),
        the emergency override, or a discard. A failure before STAGED, or an
        authenticated abort from the old PC at any time, erases what was
        staged and raises; the old PC restores itself.
        """
        if self._socket is None or self._card is None:
            raise TransferError("Connect to the old PC first.")
        card, state, staging = self._card, self.host.state, self.staging
        staged = False
        try:
            if _version(card["host_version"]) > _version(HOST_VERSION):
                raise TransferError(VERSION_ERROR)
            # Checked here, not trusted from a screen: once the old PC has
            # stopped, the 1.2 GB speech download must already be on this PC.
            try:
                prepared = self.host.staged_runtime_ready(staging)
            except (control.HostError, OSError):
                prepared = False
            if not prepared:
                raise TransferError("Speech recognition is not prepared on this PC yet. Go back to "
                                    "Receive a host, wait for This PC is ready, then start again with a new code.")
            if not self._port_free(card["port"]):
                raise TransferError("Port " + str(card["port"]) + " is in use on this PC. The moved host "
                                    "needs the same port; free it, then start again.")
            self._mirror(card, staging, progress)
            self._send("ready")
            progress("receiving")
            assembler = transfer.FrameAssembler(self._transfer_id, self._keys.control_mac)
            sealed = None
            while sealed is None:
                frame = self._receive(self.bundle_wait)
                if transfer.is_control_frame(frame):
                    if self._control.open(frame)["type"] == "abort":
                        raise _Abort("The old PC stopped the move.")
                    raise TransferError(transfer.FRAME_ERROR)
                sealed = assembler.feed(frame)
            bundle = transfer.decrypt_bundle(sealed, self._keys, self._transfer_id)
            del sealed
            manifest = bundle.manifest
            settings = manifest["settings"]
            # The bundle must be the host the operator confirmed on the card.
            if (manifest["artifacts"] != [_triple(entry) for entry in card["artifacts"]]
                    or settings["VEMALS_EARS_GUILD_ID"] != card["guild_id"]
                    or settings["VEMALS_EARS_GUILD_NAME"] != card["guild_name"]
                    or int(settings["VEMALS_EARS_PORT"]) != card["port"]
                    or transfer_net.public_origin(settings["VEMALS_EARS_PUBLIC_HTTP_URL"]) != card["public_origin"]
                    or bundle.blobs["secrets/discord-application-id"].decode("ascii") != card["application_id"]
                    or manifest["tunnel_enabled"] is not card["tunnel_enabled"]):
                raise TransferError(transfer.OPEN_ERROR)
            self._send("progress", stage="importing")
            progress("importing")
            # The receipt comes first: from this line on nothing here can start.
            self.records.receive(
                self._transfer_id, bytes.fromhex(manifest["release_commitment"]), sending_complete=True,
                staged_at=self._wall(), public_origin=card["public_origin"],
                tunnel_enabled=card["tunnel_enabled"], guild_name=card["guild_name"])
            transfer.import_state(state, bundle)
            del bundle
            transfer.publish_staging(state)
            self._send("progress", stage="services")
            progress("services")
            self.host.install_services(staged=True)
            self._send("staged")
            staged = True
            progress("staged")
            sealed_delta = b""
            while True:
                part, final = transfer.delta_part(self._expect("delta", self.delta_wait))
                sealed_delta += part
                if len(sealed_delta) > transfer.MAX_DELTA_BYTES + 48:
                    raise TransferError(transfer.FRAME_ERROR)
                if final:
                    break
            transfer.apply_profile_delta(state / transfer.DATABASE, transfer.open_delta(sealed_delta, self._keys))
            self._send("applied")
        except BaseException as error:
            self._close(abort=True)
            if staged and not isinstance(error, _Abort) and isinstance(error, Exception):
                # The old PC was told STAGED and may have recorded it, so the
                # operator there can still finish the move by hand. Keep the
                # copy; it cannot start, and only a person discards it.
                return {"state": "waiting"}
            self._discard_after(error)
            if isinstance(error, _Cancelled):
                raise TransferError("The move was cancelled. Nothing was kept on this PC.") from None
            if isinstance(error, (TransferError, _Stop, control.HostError)):
                raise TransferError(str(error) + " Nothing was kept on this PC.") from None
            if isinstance(error, (transfer_net.Closed, OSError)):
                raise TransferError("The connection to the old PC was lost. Nothing was kept on this PC.") from None
            raise
        # APPLIED is out: the old PC may release at any moment. From here the
        # copy is erased only on that PC's own authenticated abort.
        progress("waiting_release")
        try:
            secret = _hex(self._expect("release", self.release_wait).get("secret"),
                          transfer_pake.RELEASE_SECRET_BYTES)
            if secret is None:
                raise TransferError(transfer.FRAME_ERROR)
            self.records.activate(secret)
        except _Abort as error:
            # Authenticated, and only sent after the old PC erased R: this
            # copy can never be released, so it goes.
            self._close()
            self._discard_after(error)
            raise TransferError(str(error) + " Nothing was kept on this PC.") from None
        except (_Stop, _Cancelled, TransferError, transfer_net.Closed, OSError):
            self._close()
            return {"state": "waiting"}
        try:
            self._send("active")
        except (transfer_net.Closed, TransferError, OSError, _Cancelled):
            pass
        try:
            progress("activating")
            # The old relay closes this socket as it stops; the public
            # address going quiet is what the tunnel waits for.
            result = _finish_activation(self.host, self.records, public_state=self._public_state,
                                        clock=self._clock, sleep=self._sleep, quiet_wait=self.quiet_wait)
            progress("active")
            return result
        finally:
            self._close()

    def _discard_after(self, error: BaseException) -> None:
        self._stack.enter_context(self.records.locked())
        try:
            _discard(self.host, self.records, everything=False, reason=type(error).__name__)
        except TransferError:
            raise TransferError("The move failed and the staged copy could not be erased. "
                                "Choose Discard and start again.") from None
        finally:
            self._stack.close()

    def _mirror(self, card: dict, staging: Path, progress) -> None:
        """Every download the old host serves, fetched from its public address and verified."""
        downloads = staging / "downloads"
        try:
            downloads.mkdir(parents=True, exist_ok=True)
            wanted = {entry["name"].lower() for entry in card["artifacts"]}
            for leftover in downloads.iterdir():
                if leftover.name.lower() not in wanted:
                    leftover.unlink()
            for artifact in card["artifacts"]:
                self._send("progress", stage="downloads")
                progress("downloads")
                target = downloads / artifact["name"]
                if target.is_file() and transfer.describe_artifact(target) == _triple(artifact):
                    continue
                self._fetch(card["public_origin"] + artifact["path"], target, artifact["size"])
                if transfer.describe_artifact(target) != _triple(artifact):
                    target.unlink(missing_ok=True)
                    raise TransferError("A member download does not match the one the old host serves."
                                        + DOWNLOAD_HELP)
        except OSError:
            raise TransferError("A member download could not be copied from the old host."
                                + DOWNLOAD_HELP) from None


# ---------------------------------------------------------------------------
# What the wizard shows when it opens
# ---------------------------------------------------------------------------

def status(host, records=None, *, wall=time.time) -> dict:
    """This PC's place in a transfer, from the persisted receipt alone.

    "view" is one of: "" (nothing), "interrupted" (sending stopped half way:
    finish or keep), "released" (must finish stopping), "moved" (locked
    archive), "restore" (a cancelled move whose services are still disabled),
    "waiting" (received, not released), "activating" (released, not started
    yet). A damaged receipt raises TransferError; it is never "nothing".
    """
    records = _records(host, records)
    current = records.read()
    if not current:
        return {"view": "", "role": "", "state": ""}
    result = {"view": "", "role": current["role"], "state": current["state"],
              "transfer_id": current["transfer_id"]}
    details = current["details"]
    if current["role"] == transfer.SENDER:
        if current["state"] in ("offered", "sending"):
            result.update(view="interrupted",
                          can_finish=details.get("staged") is True or details.get("taken_over") is True,
                          taken_over=details.get("taken_over") is True)
        elif current["state"] in ("released", "moved"):
            result.update(view=current["state"], archive=str(transfer.archive_path(host.state)),
                          release_code=transfer_pake.release_code(records.release_secret()),
                          acknowledged=details.get("acknowledged") is True)
        elif current["state"] == "cancelled" and type(details.get("services")) is dict and host.state.exists():
            try:
                states = host.service_states()
            except control.HostError:
                states = {}
            if any(states.get(name, {}).get("start") == control.DISABLED and saved.get("start") != control.DISABLED
                   for name, saved in details["services"].items() if type(saved) is dict):
                result["view"] = "restore"
    elif current["state"] == "pending":
        result.update(view="waiting", code_failures=current["code_failures"],
                      staged=transfer.activation(host.state) == transfer.ACTIVATION_PENDING,
                      override=override_state(host, records, wall=wall))
    elif current["state"] == "active" and transfer.activation(host.state) == transfer.ACTIVATION_PENDING:
        result["view"] = "activating"
    return result
