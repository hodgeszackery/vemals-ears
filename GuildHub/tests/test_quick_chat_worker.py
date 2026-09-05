import threading
import time
import unittest
from unittest import mock

from GuildHub.protocol import decode_message, encode_message, quick_chat_command
from GuildHub.quick_chat_worker import (
    COMMAND_QUEUE_SIZE,
    RemoteQuickChatWorker,
    quick_chat_worker_url,
)


TOKEN = "publisher_" + ("p" * 40)


class FakeConnection:
    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.sent = []
        self.closed = threading.Event()

    def send(self, message):
        if self.closed.is_set():
            raise OSError("closed")
        self.sent.append(decode_message(message))

    def recv(self, timeout=None):
        if self.incoming:
            return self.incoming.pop(0)
        if self.closed.wait(min(timeout or 0.01, 0.01)):
            raise OSError("closed")
        raise TimeoutError()

    def close(self, code=1000, reason=""):
        self.closed.set()


class FailDeliveryConnection(FakeConnection):
    def send(self, message):
        decoded = decode_message(message)
        if decoded.get("type") == "quick_chat_delivery":
            raise OSError("connection lost")
        super().send(message)


class DropAfterAuthenticationConnection(FakeConnection):
    def recv(self, timeout=None):
        if self.incoming:
            return self.incoming.pop(0)
        raise OSError("control socket dropped")


class DropWhenDrainedConnection(FakeConnection):
    def recv(self, timeout=None):
        if self.incoming:
            return self.incoming.pop(0)
        raise OSError("control socket dropped")


class QuickChatWorkerTests(unittest.TestCase):
    def test_url_uses_the_private_worker_endpoint(self):
        self.assertEqual(
            "wss://ears.example.test/v1/quick-chat-worker",
            quick_chat_worker_url("wss://ears.example.test"),
        )
        self.assertEqual(
            "ws://127.0.0.1:8770/v1/quick-chat-worker",
            quick_chat_worker_url("ws://127.0.0.1:8770"),
        )

    def test_authenticated_worker_moves_commands_and_redacted_results(self):
        private_text = "Private raid line"
        connection = FakeConnection(
            [
                encode_message({"type": "authenticated"}),
                encode_message(
                    quick_chat_command(
                        "worker_request_123456",
                        "Vemal",
                        "paladin",
                        private_text,
                    )
                ),
            ]
        )
        calls = []

        def connector(url, **options):
            calls.append((url, options))
            return connection

        worker = RemoteQuickChatWorker.start(
            "ws://127.0.0.1:8770", TOKEN, connector=connector
        )
        try:
            deadline = time.monotonic() + 1
            command = None
            while command is None and time.monotonic() < deadline:
                command = worker.next_quick_chat()
                time.sleep(0.005)
            self.assertIsNotNone(command)
            self.assertEqual(private_text, command.text)
            self.assertNotIn(private_text, repr(command))
            self.assertTrue(
                worker.complete_quick_chat(command.request_id, "delivered")
            )

            while len(connection.sent) < 2 and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertEqual("authenticate", connection.sent[0]["type"])
            self.assertEqual("quick_chat_delivery", connection.sent[1]["type"])
            self.assertNotIn("text", connection.sent[1])
            self.assertNotIn(private_text, repr(connection.sent[1]))
            self.assertEqual(
                "ws://127.0.0.1:8770/v1/quick-chat-worker", calls[0][0]
            )
        finally:
            worker.close()

    def test_bad_authentication_never_becomes_ready(self):
        connection = FakeConnection(
            [encode_message({"type": "error", "code": "authentication_failed"})]
        )
        with self.assertRaises(RuntimeError) as caught:
            RemoteQuickChatWorker.start(
                "ws://127.0.0.1:8770",
                TOKEN,
                connector=lambda *_args, **_kwargs: connection,
                timeout=0.2,
            )
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_delivery_receipt_survives_a_worker_reconnect(self):
        command = quick_chat_command(
            "worker_request_123456",
            "Vemal",
            "paladin",
            "Reconnect safely",
        )
        first = FailDeliveryConnection(
            [
                encode_message({"type": "authenticated"}),
                encode_message(command),
            ]
        )
        second = FakeConnection([encode_message({"type": "authenticated"})])
        connections = [first, second]

        def connector(_url, **_options):
            if connections:
                return connections.pop(0)
            return second

        with mock.patch("GuildHub.quick_chat_worker.RECONNECT_SECONDS", 0.01):
            worker = RemoteQuickChatWorker.start(
                "ws://127.0.0.1:8770", TOKEN, connector=connector
            )
            try:
                deadline = time.monotonic() + 1
                received = None
                while received is None and time.monotonic() < deadline:
                    received = worker.next_quick_chat()
                    time.sleep(0.005)
                self.assertIsNotNone(received)
                self.assertTrue(
                    worker.complete_quick_chat(received.request_id, "delivered")
                )

                while len(second.sent) < 2 and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertEqual("authenticate", second.sent[0]["type"])
                self.assertEqual("quick_chat_delivery", second.sent[1]["type"])
                self.assertEqual(received.request_id, second.sent[1]["request_id"])
                self.assertTrue(worker.alive())
            finally:
                worker.close()

    def test_reconnect_resends_do_not_fill_queue_or_fail_originals(self):
        commands = [
            quick_chat_command(
                f"worker_reconnect_{index:03d}",
                "Vemal",
                "paladin",
                "Queued " + str(index),
            )
            for index in range(COMMAND_QUEUE_SIZE)
        ]
        first = DropWhenDrainedConnection(
            [
                encode_message({"type": "authenticated"}),
                *(encode_message(command) for command in commands),
            ]
        )
        second = FakeConnection(
            [
                encode_message({"type": "authenticated"}),
                *(encode_message(command) for command in commands),
            ]
        )
        connections = [first, second]
        connection_count = 0

        def connector(_url, **_options):
            nonlocal connection_count
            connection_count += 1
            if connections:
                return connections.pop(0)
            return second

        with mock.patch("GuildHub.quick_chat_worker.RECONNECT_SECONDS", 0.01):
            worker = RemoteQuickChatWorker.start(
                "ws://127.0.0.1:8770", TOKEN, connector=connector
            )
            try:
                deadline = time.monotonic() + 1
                while (
                    (connection_count < 2 or second.incoming)
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.005)
                self.assertGreaterEqual(connection_count, 2)
                self.assertFalse(second.incoming)
                queued = [worker.next_quick_chat() for _ in commands]
                self.assertTrue(all(command is not None for command in queued))
                self.assertIsNone(worker.next_quick_chat())
                duplicate_failures = [
                    message
                    for message in second.sent
                    if message.get("type") == "quick_chat_delivery"
                ]
                self.assertEqual([], duplicate_failures)
            finally:
                worker.close()

    def test_worker_becomes_unhealthy_after_reconnect_grace_expires(self):
        first = DropAfterAuthenticationConnection(
            [encode_message({"type": "authenticated"})]
        )
        calls = 0

        def connector(_url, **_options):
            nonlocal calls
            calls += 1
            if calls == 1:
                return first
            raise OSError("server unavailable")

        # The grace is 50x the reconnect interval, not 10x. This asserts
        # the worker both DIED and RETRIED, and on the CI runner - 8
        # workers, heavily loaded - the worker thread need not be
        # scheduled at all inside a 50ms grace, so it died after a single
        # attempt and "calls > 1" failed with "1 not greater than 1".
        # Widening the ratio leaves room for several attempts even when
        # the thread is starved; the loop still exits the moment the
        # worker dies, so this stays fast in practice.
        with mock.patch("GuildHub.quick_chat_worker.RECONNECT_SECONDS", 0.01), mock.patch(
            "GuildHub.quick_chat_worker.RECONNECT_GRACE_SECONDS", 0.5
        ):
            worker = RemoteQuickChatWorker.start(
                "ws://127.0.0.1:8770", TOKEN, connector=connector
            )
            try:
                deadline = time.monotonic() + 10
                while worker.alive() and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertFalse(worker.alive())
                self.assertGreater(calls, 1)
            finally:
                worker.close()


if __name__ == "__main__":
    unittest.main()
