"""Legacy private worker connection retained for protocol compatibility.

Current relays never send typed bodies to this connection. It remains so a
deployed worker can authenticate during mixed-version upgrades and so older
bounded command frames receive only redacted terminal outcomes. The adapter has
no Discord sink for those bodies.
"""

from __future__ import annotations

import hashlib
import queue
import threading
import time
from typing import Callable, Protocol

from .protocol import (
    MAX_WIRE_BYTES,
    QUICK_CHAT_WORKER_PATH,
    SUBPROTOCOL,
    GuildHubProtocolError,
    QuickChatCommand,
    decode_message,
    encode_message,
    parse_quick_chat_command,
    quick_chat_delivery,
)
from .publisher import AUTH_TIMEOUT_SECONDS, PublisherError, publisher_url

try:
    from websockets.sync.client import connect as websocket_connect
except ImportError:  # pragma: no cover - deployment readiness covers this.
    websocket_connect = None


COMMAND_QUEUE_SIZE = 32
RESULT_QUEUE_SIZE = 64
POLL_SECONDS = 0.2
RECONNECT_SECONDS = 0.5
RECONNECT_GRACE_SECONDS = 20.0
COMPLETED_COMMAND_LIMIT = 512


class WorkerConnection(Protocol):
    def send(self, message: str) -> None: ...

    def recv(self, timeout: float | None = None) -> str | bytes: ...

    def close(self, code: int = 1000, reason: str = "") -> None: ...


def quick_chat_worker_url(server_url: str) -> str:
    base = publisher_url(server_url)
    return base.removesuffix("/v1/publish") + QUICK_CHAT_WORKER_PATH


class RemoteQuickChatWorker:
    """One memory-only control channel owned by the guild worker."""

    def __init__(
        self,
        server_url: str,
        publisher_token: str,
        *,
        connector: Callable[..., WorkerConnection] | None = None,
    ) -> None:
        if not isinstance(publisher_token, str) or len(publisher_token.strip()) < 32:
            raise PublisherError("The Vemal's Ears publisher credential is missing.")
        if any(character.isspace() for character in publisher_token):
            raise PublisherError("The Vemal's Ears publisher credential is invalid.")
        if connector is None:
            if websocket_connect is None:
                raise PublisherError("The Vemal's Ears network runtime is not installed.")
            connector = websocket_connect
        self.url = quick_chat_worker_url(server_url)
        self._token = publisher_token
        self._connector = connector
        self._commands: queue.Queue[QuickChatCommand] = queue.Queue(
            maxsize=COMMAND_QUEUE_SIZE
        )
        self._results: queue.Queue[object] = queue.Queue(maxsize=RESULT_QUEUE_SIZE)
        # A reconnect makes the relay resend every unresolved command. Keep
        # only a digest and redacted status here so a duplicate cannot occupy
        # another queue slot or turn a full queue into a false failure.
        self._command_states: dict[str, tuple[bytes, str | None]] = {}
        self._command_state_lock = threading.Lock()
        self._ready = threading.Event()
        self._connected = threading.Event()
        self._disconnected_since: float | None = None
        self._failed = threading.Event()
        self._closing = threading.Event()
        self._connection: WorkerConnection | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="VemalsEarsQuickChat",
            daemon=True,
        )
        self._thread.start()

    @classmethod
    def start(
        cls,
        server_url: str,
        publisher_token: str,
        *,
        connector: Callable[..., WorkerConnection] | None = None,
        timeout: float = AUTH_TIMEOUT_SECONDS,
    ) -> "RemoteQuickChatWorker":
        worker = cls(server_url, publisher_token, connector=connector)
        if worker._ready.wait(timeout) and worker.alive():
            return worker
        worker.close()
        raise PublisherError("The Vemal's Ears quick-chat relay is unavailable.")

    def alive(self) -> bool:
        base_alive = (
            self._ready.is_set()
            and not self._failed.is_set()
            and not self._closing.is_set()
            and self._thread.is_alive()
        )
        if not base_alive:
            return False
        if self._connected.is_set():
            return True
        disconnected_since = self._disconnected_since
        return (
            disconnected_since is None
            or time.monotonic() - disconnected_since < RECONNECT_GRACE_SECONDS
        )

    def next_quick_chat(self) -> QuickChatCommand | None:
        with self._command_state_lock:
            try:
                return self._commands.get_nowait()
            except queue.Empty:
                return None

    def complete_quick_chat(self, request_id: str, status: str) -> bool:
        if not self.alive():
            return False
        try:
            result = quick_chat_delivery(request_id, status)
        except GuildHubProtocolError:
            return False
        with self._command_state_lock:
            state = self._command_states.get(request_id)
            if state is None:
                return False
            fingerprint, previous_status = state
            if previous_status is not None:
                return previous_status == status
            try:
                self._results.put_nowait(result)
            except queue.Full:
                return False
            # Reinsert completed entries at the end so pruning removes the
            # oldest redacted result, never an in-flight command.
            self._command_states.pop(request_id, None)
            self._command_states[request_id] = (fingerprint, status)
            self._prune_completed_commands_locked()
            return True

    def _run(self) -> None:
        try:
            while not self._closing.is_set():
                connection: WorkerConnection | None = None
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
                            {
                                "type": "authenticate",
                                "publisher_token": self._token,
                            }
                        )
                    )
                    authenticated = decode_message(
                        connection.recv(timeout=AUTH_TIMEOUT_SECONDS)
                    )
                    if authenticated != {"type": "authenticated"}:
                        raise PublisherError(
                            "The Vemal's Ears server returned an unexpected response."
                        )
                    self._connected.set()
                    self._disconnected_since = None
                    self._ready.set()
                    while not self._closing.is_set():
                        self._flush_results(connection)
                        try:
                            raw = connection.recv(timeout=POLL_SECONDS)
                        except TimeoutError:
                            continue
                        command = parse_quick_chat_command(decode_message(raw))
                        self._accept_command(connection, command)
                except Exception:
                    # Network errors can include typed request details. Never
                    # render them; reconnect with the same trusted credential.
                    pass
                finally:
                    self._connected.clear()
                    if (
                        self._ready.is_set()
                        and not self._closing.is_set()
                        and self._disconnected_since is None
                    ):
                        self._disconnected_since = time.monotonic()
                    self._connection = None
                    if connection is not None:
                        try:
                            connection.close()
                        except Exception:
                            pass
                if not self._closing.wait(RECONNECT_SECONDS):
                    continue
        except BaseException:
            self._failed.set()
        finally:
            self._ready.set()
            self._token = ""
            self._connected.clear()
            self._disconnected_since = None
            self._connection = None

    def _flush_results(self, connection: WorkerConnection) -> None:
        while True:
            try:
                result = self._results.get_nowait()
            except queue.Empty:
                return
            try:
                if not isinstance(result, dict):
                    raise PublisherError("Invalid quick-chat delivery result.")
                connection.send(encode_message(result))
            except Exception:
                # The socket can fail after Node has already delivered the
                # message. Preserve the redacted receipt for the replacement
                # connection; server and Discord nonce idempotency make an
                # uncertain resend safe.
                try:
                    self._results.put_nowait(result)
                except queue.Full:
                    pass
                raise
            finally:
                result = None
                self._results.task_done()

    def _accept_command(
        self,
        connection: WorkerConnection,
        command: QuickChatCommand,
    ) -> None:
        fingerprint = self._command_fingerprint(command)
        replay_status: str | None = None
        with self._command_state_lock:
            state = self._command_states.get(command.request_id)
            if state is not None:
                previous_fingerprint, previous_status = state
                if previous_fingerprint != fingerprint:
                    replay_status = "delivery_failed"
                elif previous_status is None:
                    # The original is queued or already with Discord. Its one
                    # eventual result will answer every relay resend.
                    return
                else:
                    replay_status = previous_status
            else:
                self._command_states[command.request_id] = (fingerprint, None)
                try:
                    self._commands.put_nowait(command)
                except queue.Full:
                    self._command_states.pop(command.request_id, None)
                    replay_status = "delivery_failed"
                else:
                    return
        connection.send(
            encode_message(
                quick_chat_delivery(command.request_id, replay_status)
            )
        )

    @staticmethod
    def _command_fingerprint(command: QuickChatCommand) -> bytes:
        return hashlib.sha256(
            (
                command.request_id
                + "\0"
                + command.speaker
                + "\0"
                + command.wow_class
                + "\0"
                + command.text
            ).encode("utf-8")
        ).digest()

    def _prune_completed_commands_locked(self) -> None:
        completed = sum(
            status is not None for _fingerprint, status in self._command_states.values()
        )
        if completed <= COMPLETED_COMMAND_LIMIT:
            return
        for request_id, (_fingerprint, status) in tuple(
            self._command_states.items()
        ):
            if status is not None:
                self._command_states.pop(request_id, None)
                return

    def close(self) -> None:
        self._closing.set()
        self._token = ""
        connection = self._connection
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        self._thread.join(timeout=3)
        while True:
            try:
                self._results.get_nowait()
            except queue.Empty:
                break
            else:
                self._results.task_done()
        while True:
            try:
                self._commands.get_nowait()
            except queue.Empty:
                break
        with self._command_state_lock:
            self._command_states.clear()
