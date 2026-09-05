import threading
import time
import unittest

from GuildHub.protocol import decode_message, encode_message
from GuildHub.publisher import PublisherError, RemoteCaptionSink, publisher_url
from VoiceCaptions.events import CaptionEvent


CAPTION = CaptionEvent(
    speaker_id="speaker-1",
    speaker="Mira",
    character="",
    role="HEALER",
    text="Stack on me.",
    final=True,
    sequence=1,
    timestamp=1.0,
)


class FakeConnection:
    def __init__(
        self,
        *,
        auth_type="authenticated",
        ack_cursor=1,
        auth_extra=False,
    ):
        self.sent = []
        authentication = {"type": auth_type, "cursor": 0}
        if auth_extra:
            authentication["unexpected"] = "field"
        self.responses = [encode_message(authentication)]
        self.ack_cursor = ack_cursor
        self.closed = False
        self.lock = threading.Lock()

    def send(self, message):
        with self.lock:
            self.sent.append(message)
            decoded = decode_message(message)
            if decoded.get("type") == "publish":
                self.responses.append(
                    encode_message({"type": "published", "cursor": self.ack_cursor})
                )
            elif decoded.get("type") == "audience_update":
                self.responses.append(
                    encode_message(
                        {
                            "type": "audience_updated",
                            "generation": decoded["generation"],
                            "revision": decoded["revision"],
                        }
                    )
                )
            elif decoded.get("type") == "viewer_link":
                self.responses.append(
                    encode_message(
                        {
                            "type": "viewer_linked",
                            "code": decoded["code"],
                            "linked": True,
                        }
                    )
                )
            elif decoded.get("type") == "viewer_enrollment":
                self.responses.append(
                    encode_message(
                        {
                            "type": "viewer_enrolled",
                            "code": decoded["code"],
                            "created": True,
                        }
                    )
                )

    def recv(self, timeout=None):
        deadline = time.monotonic() + (timeout or 1)
        while time.monotonic() < deadline:
            with self.lock:
                if self.responses:
                    return self.responses.pop(0)
            time.sleep(0.001)
        raise TimeoutError()

    def close(self, code=1000, reason=""):
        self.closed = True


class PublisherTests(unittest.TestCase):
    def test_remote_plaintext_is_rejected_but_loopback_is_allowed(self):
        self.assertEqual(
            "ws://127.0.0.1:8765/v1/publish",
            publisher_url("ws://127.0.0.1:8765"),
        )
        with self.assertRaises(PublisherError):
            publisher_url("ws://captions.example.test")

    def test_authenticates_and_releases_caption_after_ack(self):
        connection = FakeConnection()
        calls = []

        def connect(url, **options):
            calls.append((url, options))
            return connection

        token = "p" * 40
        sink = RemoteCaptionSink.start(
            "ws://localhost:8765", token, connector=connect, timeout=1
        )
        self.assertTrue(sink.send(CAPTION))
        sink._pending.join()
        sink.close()

        self.assertEqual("ws://localhost:8765/v1/publish", calls[0][0])
        self.assertEqual(["guild-hub.v1"], calls[0][1]["subprotocols"])
        messages = [decode_message(value) for value in connection.sent]
        self.assertEqual("authenticate", messages[0]["type"])
        self.assertEqual(token, messages[0]["publisher_token"])
        self.assertEqual("publish", messages[1]["type"])
        self.assertEqual("Stack on me.", messages[1]["event"]["text"])
        self.assertNotIn(token, repr(sink.__dict__))

    def test_bad_authentication_fails_without_raising_secret_text(self):
        connection = FakeConnection(auth_type="error")
        token = "private-" + ("x" * 40)
        with self.assertRaises(PublisherError) as caught:
            RemoteCaptionSink.start(
                "ws://localhost:8765",
                token,
                connector=lambda *_args, **_kwargs: connection,
                timeout=1,
            )
        self.assertNotIn(token, str(caught.exception))

    def test_authentication_response_rejects_extra_fields(self):
        connection = FakeConnection(auth_extra=True)
        with self.assertRaises(PublisherError):
            RemoteCaptionSink.start(
                "ws://localhost:8765",
                "p" * 40,
                connector=lambda *_args, **_kwargs: connection,
                timeout=1,
            )

    def test_repeated_ack_fails_closed(self):
        connection = FakeConnection(ack_cursor=0)
        sink = RemoteCaptionSink.start(
            "ws://localhost:8765",
            "p" * 40,
            connector=lambda *_args, **_kwargs: connection,
            timeout=1,
        )
        self.assertTrue(sink.send(CAPTION))
        sink._pending.join()
        deadline = time.monotonic() + 1
        while not sink._failed.is_set() and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertFalse(sink.alive())
        sink.close()

    def test_ack_cursor_gap_fails_closed(self):
        connection = FakeConnection(ack_cursor=2)
        sink = RemoteCaptionSink.start(
            "ws://localhost:8765",
            "p" * 40,
            connector=lambda *_args, **_kwargs: connection,
            timeout=1,
        )
        self.assertTrue(sink.send(CAPTION))
        sink._pending.join()
        deadline = time.monotonic() + 1
        while not sink._failed.is_set() and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertFalse(sink.alive())
        sink.close()

    def test_an_invitation_round_trip_keeps_the_publisher_alive(self):
        """The bug that shipped in 0.4.5 and took captions down.

        The invitation frame was sent, the relay answered, and the writer
        had no branch for the answer - so it fell through to "an invalid
        caption entered the publish queue", marked the publisher failed,
        and every caption after the first /vem setup was lost. A frame
        kind is not done until its acknowledgement is read here.
        """
        connection = FakeConnection()
        sink = RemoteCaptionSink.start(
            "ws://localhost:8765",
            "p" * 40,
            connector=lambda *_args, **_kwargs: connection,
            timeout=1,
        )
        code = "ghe_" + "A" * 32
        token = "0123456789abcdef01234567"
        self.assertTrue(sink.enroll_viewer(code, token, 1800))
        # A caption AFTER the invitation is the real test: it only
        # arrives if the invitation's acknowledgement was accepted.
        self.assertTrue(sink.send(CAPTION))
        # Never join the queue here: a writer that dies on the first frame
        # leaves the second one un-acknowledged and a join hangs forever.
        # Wait for either outcome with a deadline instead.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with connection.lock:
                delivered = len(connection.sent) >= 3
            if delivered or sink._failed.is_set():
                break
            time.sleep(0.001)
        self.assertTrue(sink.alive(), "the invitation acknowledgement killed the publisher")
        self.assertTrue(delivered, "the caption after the invitation was never sent")
        sink.close()

        messages = [decode_message(value) for value in connection.sent[1:]]
        self.assertEqual([m["type"] for m in messages], ["viewer_enrollment", "publish"])
        self.assertEqual(messages[0]["code"], code)
        self.assertEqual(messages[0]["viewer_token"], token)
        self.assertEqual(messages[0]["ttl_seconds"], 1800)

    def test_a_refused_invitation_is_counted_and_captions_keep_flowing(self):
        """One person's refused invitation must not drop the whole raid's
        captions, but it must not vanish either: the sink counts it so a
        status line can say it happened."""
        connection = FakeConnection()
        original = connection.send

        def send(message):
            original(message)
            decoded = decode_message(message)
            if decoded.get("type") == "viewer_enrollment":
                with connection.lock:
                    connection.responses[-1] = encode_message(
                        {"type": "viewer_enrolled", "code": decoded["code"], "created": False}
                    )

        connection.send = send
        sink = RemoteCaptionSink.start(
            "ws://localhost:8765",
            "p" * 40,
            connector=lambda *_args, **_kwargs: connection,
            timeout=1,
        )
        self.assertTrue(sink.enroll_viewer("ghe_" + "A" * 32, "0123456789abcdef01234567", 1800))
        self.assertTrue(sink.send(CAPTION))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with connection.lock:
                delivered = len(connection.sent) >= 3
            if delivered or sink._failed.is_set():
                break
            time.sleep(0.001)
        self.assertTrue(sink.alive(), "a refused invitation killed the publisher")
        self.assertTrue(delivered)
        self.assertEqual(sink.refused_invitations, 1)
        sink.close()

    def test_a_wrong_invitation_acknowledgement_still_fails_closed(self):
        """Protocol drift is fatal on purpose; the fix must not loosen that."""
        connection = FakeConnection()
        original = connection.send

        def send(message):
            original(message)
            decoded = decode_message(message)
            if decoded.get("type") == "viewer_enrollment":
                with connection.lock:
                    connection.responses[-1] = encode_message(
                        {"type": "viewer_enrolled", "code": "ghe_" + "B" * 32, "created": True}
                    )

        connection.send = send
        sink = RemoteCaptionSink.start(
            "ws://localhost:8765",
            "p" * 40,
            connector=lambda *_args, **_kwargs: connection,
            timeout=1,
        )
        self.assertTrue(sink.enroll_viewer("ghe_" + "A" * 32, "0123456789abcdef01234567", 1800))
        sink._pending.join()
        deadline = time.monotonic() + 1
        while not sink._failed.is_set() and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertFalse(sink.alive(), "an acknowledgement for a different code was accepted")
        sink.close()

    def test_audience_link_and_caption_frames_keep_strict_queue_order(self):
        connection = FakeConnection()
        sink = RemoteCaptionSink.start(
            "ws://localhost:8765",
            "p" * 40,
            connector=lambda *_args, **_kwargs: connection,
            timeout=1,
        )
        token = "0123456789abcdef01234567"
        self.assertTrue(sink.update_audience(9, 3, [token]))
        self.assertTrue(sink.link_viewer("29CFGHJK", token))
        self.assertTrue(
            sink.send(
                CAPTION,
                audience_generation=9,
                audience_revision=3,
            )
        )
        sink._pending.join()
        self.assertTrue(sink.alive())
        sink.close()

        messages = [decode_message(value) for value in connection.sent[1:]]
        self.assertEqual(
            ["audience_update", "viewer_link", "publish"],
            [message["type"] for message in messages],
        )
        self.assertEqual([token], messages[0]["viewer_tokens"])
        self.assertEqual("29CFGHJK", messages[1]["code"])
        self.assertEqual(9, messages[2]["audience_generation"])
        self.assertEqual(3, messages[2]["audience_revision"])

    def test_invalid_audience_identity_fails_the_sink_closed(self):
        connection = FakeConnection()
        sink = RemoteCaptionSink.start(
            "ws://localhost:8765",
            "p" * 40,
            connector=lambda *_args, **_kwargs: connection,
            timeout=1,
        )
        self.assertFalse(
            sink.update_audience(1, 0, ["discord-user-123456789"])
        )
        self.assertFalse(sink.alive())
        sink.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
