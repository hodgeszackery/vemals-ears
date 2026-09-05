import io
import json
import subprocess
import threading
import time
import unittest

from VoiceCaptions.events import CaptionEvent, SessionEvent
from VoiceCaptions.member_overlay import _OVERLAY_STOP, OverlaySink
from VoiceCaptions.voice_captions import CaptionRouter


class FakeSink:
    def __init__(self, succeeds=True):
        self.succeeds = succeeds
        self.events = []

    def send(self, event):
        self.events.append(event)
        return self.succeeds


class SupervisorPrivacyTests(unittest.TestCase):
    def test_caption_text_is_forwarded_but_never_returned_for_logging(self):
        sink = FakeSink()
        router = CaptionRouter(sink)
        secret_words = "This sentence must not reach the companion log"
        line = json.dumps(
            {
                "type": "caption",
                "speaker_id": "private-user-id",
                "speaker": "Mira",
                "character": "Mira",
                "role": "HEALER",
                "text": secret_words,
                "final": True,
                "sequence": 1,
                "timestamp": 1.0,
            }
        )
        public_status = router.route_line(line)
        self.assertIsNone(public_status)
        self.assertEqual(secret_words, sink.events[0].text)

    def test_session_message_is_replaced_with_public_constant(self):
        sink = FakeSink()
        router = CaptionRouter(sink)
        public_status = router.route_line(
            json.dumps(
                {
                    "type": "session",
                    "state": "paused",
                    "message": "Alice has not consented",
                }
            )
        )
        self.assertEqual("Captions paused - waiting for consent", public_status)
        self.assertNotIn("Alice", public_status)

    def test_invalid_input_is_dropped_without_echo(self):
        sink = FakeSink()
        router = CaptionRouter(sink)
        self.assertIsNone(router.route_line("token=never-print-me"))
        self.assertEqual([], sink.events)

    def test_overlay_failure_returns_generic_status(self):
        router = CaptionRouter(FakeSink(succeeds=False))
        line = json.dumps(
            {
                "type": "session",
                "state": "active",
            }
        )
        result = router.route_line(line)
        self.assertEqual("Error: Vemal's Ears overlay is unavailable", result)


class StalledOverlayPipe:
    def __init__(self):
        self.entered = threading.Event()
        self.resume = threading.Event()
        self.closed = False

    def write(self, _payload):
        self.entered.set()
        self.resume.wait(2)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class StalledOverlayProcess:
    def __init__(self):
        self.stdin = StalledOverlayPipe()
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("overlay", timeout)
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        self.returncode = -9


class OverlaySinkShutdownTests(unittest.TestCase):
    def test_writer_stops_when_the_queue_was_full_at_close(self):
        process = StalledOverlayProcess()
        sink = OverlaySink(process, max_pending=2)
        event = SessionEvent(state="active")
        self.assertTrue(sink.send(event))
        self.assertTrue(process.stdin.entered.wait(2))
        self.assertTrue(sink.send(event))
        self.assertTrue(sink.send(event))
        self.assertTrue(sink._pending.full())

        # A suspended overlay child that resumes while close() is still joining
        # is what parks the writer: it drains the backlog and blocks on its
        # untimed get() before the process is terminated, so terminating the
        # process can no longer release it.
        def resume_overlay():
            deadline = time.monotonic() + 0.05
            while time.monotonic() < deadline:
                if _OVERLAY_STOP in tuple(sink._pending.queue):
                    break
                time.sleep(0.005)
            process.stdin.resume.set()

        resumer = threading.Thread(target=resume_overlay)
        resumer.start()
        try:
            sink.close()
        finally:
            resumer.join(2)
        self.assertFalse(sink._writer.is_alive())




def caption(role, text="Stack on me"):
    return CaptionEvent(
        speaker_id="speaker-" + role.lower(),
        speaker="Mira",
        character="Mirala",
        role=role,
        text=text,
        final=True,
        sequence=1,
        timestamp=1.0,
    )


class FocusModeTests(unittest.TestCase):
    """Focus: only tank and healer captions reach the overlay.

    Strict by request - UNKNOWN is dropped too, so an unmapped speaker goes
    silent while focus is on. These tests pass a provider explicitly; the
    default provider reads the real settings file, which no test should.
    """

    def test_focus_off_lets_every_role_through(self):
        sink = FakeSink()
        router = CaptionRouter(sink, focus_provider=lambda: False)
        for role in ("TANK", "HEALER", "DAMAGE", "UNKNOWN"):
            router.route_event(caption(role))
        self.assertEqual(
            ["TANK", "HEALER", "DAMAGE", "UNKNOWN"],
            [event.role for event in sink.events],
        )

    def test_focus_on_keeps_exactly_tank_and_healer(self):
        sink = FakeSink()
        router = CaptionRouter(sink, focus_provider=lambda: True)
        outcomes = {
            role: router.route_event(caption(role))
            for role in ("TANK", "HEALER", "DAMAGE", "UNKNOWN")
        }
        self.assertEqual(
            ["TANK", "HEALER"], [event.role for event in sink.events]
        )
        # A drop is silent - not an error the companion would log.
        self.assertIsNone(outcomes["DAMAGE"])
        self.assertIsNone(outcomes["UNKNOWN"])

    def test_focus_never_touches_session_events(self):
        sink = FakeSink()
        router = CaptionRouter(sink, focus_provider=lambda: True)
        status = router.route_event(SessionEvent(state="paused"))
        self.assertEqual(1, len(sink.events))
        self.assertEqual("Captions paused - waiting for consent", status)

    def test_focus_flips_mid_stream_without_a_rebuild(self):
        sink = FakeSink()
        focused = {"value": False}
        router = CaptionRouter(sink, focus_provider=lambda: focused["value"])
        router.route_event(caption("DAMAGE", "before focus"))
        focused["value"] = True
        router.route_event(caption("DAMAGE", "during focus"))
        focused["value"] = False
        router.route_event(caption("DAMAGE", "after focus"))
        self.assertEqual(
            ["before focus", "after focus"],
            [event.text for event in sink.events],
        )

    def test_a_dropped_caption_never_reaches_the_sink_at_all(self):
        """Filtering after a failed send would still queue the text."""
        sink = FakeSink(succeeds=False)
        router = CaptionRouter(sink, focus_provider=lambda: True)
        result = router.route_event(caption("DAMAGE"))
        self.assertIsNone(result)
        self.assertEqual([], sink.events)


if __name__ == "__main__":
    unittest.main(verbosity=2)

import tempfile
from unittest import mock
from pathlib import Path
from VoiceCaptions import member_overlay


class OverlayChildStderrTests(unittest.TestCase):
    """The child's stderr lands in a readable file, never DEVNULL.

    One real outage was undiagnosable by design: the overlay child could
    die at import with its only words thrown away. The log is appended
    across launches and truncated at spawn once past the cap - and if
    the log itself cannot be opened, the overlay still starts.
    """

    def test_the_child_is_started_with_a_log_file_for_stderr(self):
        captured = {}

        class FakeProcess:
            pass

        def fake_popen(*args, **kwargs):
            captured.update(kwargs)
            return FakeProcess()

        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(
            member_overlay.__dict__, {}
        ), mock.patch.object(
            member_overlay.subprocess, "Popen", fake_popen
        ), mock.patch.dict(
            "os.environ", {"LOCALAPPDATA": folder}
        ), mock.patch.object(
            member_overlay.OverlaySink, "__init__", lambda self, process: None
        ):
            member_overlay.OverlaySink.start()

        stderr = captured.get("stderr")
        self.assertIsNot(stderr, member_overlay.subprocess.DEVNULL)
        self.assertTrue(str(getattr(stderr, "name", "")).endswith("overlay-child.log"))

    def test_an_oversized_log_is_truncated_at_spawn(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(
            "os.environ", {"LOCALAPPDATA": folder}
        ):
            path = member_overlay.overlay_child_log_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * (member_overlay.CHILD_LOG_CAP_BYTES + 1))
            handle = member_overlay._child_stderr_handle()
            try:
                self.assertTrue(str(handle.name).endswith("overlay-child.log"))
            finally:
                if handle is not member_overlay.subprocess.DEVNULL:
                    handle.close()
            self.assertLessEqual(
                path.stat().st_size, member_overlay.CHILD_LOG_CAP_BYTES
            )

    def test_an_unwritable_log_never_stops_the_overlay(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(
            "os.environ", {"LOCALAPPDATA": folder}
        ):
            # LOCALAPPDATA/WoWVoiceCaptions is a FILE: mkdir and open fail.
            (Path(folder) / "WoWVoiceCaptions").write_text("not a dir")
            handle = member_overlay._child_stderr_handle()
            self.assertIs(handle, member_overlay.subprocess.DEVNULL)
