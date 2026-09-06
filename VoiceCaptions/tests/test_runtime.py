import asyncio
import contextlib
import io
import os
import queue
import subprocess
import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from VoiceCaptions.coordinator import NodeAdapterCapability
from VoiceCaptions.credentials import DiscordCredentials
from VoiceCaptions.events import CaptionEvent, SessionEvent
from VoiceCaptions.member_overlay import _OVERLAY_STOP, OverlaySink
from VoiceCaptions.stt import BackendCapability, WhisperCppConfig
from VoiceCaptions.voice_captions import (
    RuntimeReadiness,
    check_runtime_readiness,
    installed_stt_config,
    run_watch,
)
import VoiceCaptions.voice_captions as voice_captions


READY_NODE = NodeAdapterCapability(
    True, "ready", Path("C:/node.exe"), Path("C:/adapter")
)
READY_STT = BackendCapability("fake", True, "ready")
CONFIG = WhisperCppConfig(
    server_path=Path("C:/whisper-server.exe"),
    model_path=Path("C:/model.bin"),
    worker_count=1,
)
CREDENTIALS = DiscordCredentials(
    "123456789012345678", "not-a-real-token." + "x" * 40
)


class FakeStore:
    def __init__(self, credentials=CREDENTIALS):
        self.credentials = credentials
        self.loads = 0

    def load(self):
        self.loads += 1
        return self.credentials


class FakeOverlay:
    def __init__(self):
        self.closed = False
        self.events = []
        self.events_at_close = None

    def send(self, event):
        self.events.append(event)
        return True

    def alive(self):
        return not self.closed

    def close(self):
        self.events_at_close = tuple(self.events)
        self.closed = True


class FakeHostedOverlay(FakeOverlay):
    def __init__(self):
        super().__init__()
        self.audiences = []
        self.links = []

    def update_audience(self, generation, revision, tokens):
        self.audiences.append((generation, revision, tokens))
        return True

    def link_viewer(self, code, token):
        self.links.append((code, token))
        return True

    def enroll_viewer(self, code, token, ttl_seconds):
        self.enrollments.append((code, token, ttl_seconds))
        return True


class CaptionForwarderTests(unittest.TestCase):
    def test_hosted_routing_controls_stay_attached_to_forwarder(self):
        sink = FakeHostedOverlay()
        sink.enrollments = []
        router = voice_captions.CaptionRouter(sink, focus_provider=lambda: False)
        forward = voice_captions._caption_forwarder(router)

        self.assertTrue(forward.update_audience(2, 3, ("a" * 24,)))
        self.assertTrue(forward.link_viewer("AB12CD34", "a" * 24))
        self.assertTrue(forward.enroll_viewer("ghe_" + "A" * 32, "a" * 24, 1800))
        self.assertEqual([(2, 3, ("a" * 24,))], sink.audiences)
        self.assertEqual([("AB12CD34", "a" * 24)], sink.links)
        self.assertEqual([("ghe_" + "A" * 32, "a" * 24, 1800)], sink.enrollments)

    def test_the_production_stack_carries_every_control_the_coordinator_looks_up(self):
        """The bug that shipped in 0.4.5: enroll_viewer existed on the
        publisher and the coordinator looked it up dynamically, but the
        two layers between them - HealthReportingSink and this forwarder
        - did not carry it, so every pairing invitation was dropped
        silently. This builds the REAL layers around a publisher-shaped
        fake and checks every name the coordinator reaches for.
        """
        import re
        import tempfile
        from pathlib import Path

        from GuildHub.host import HealthReportingSink, WorkerHealthMarker
        from VoiceCaptions import coordinator as coordinator_module

        source = Path(coordinator_module.__file__).read_text(encoding="utf-8")
        looked_up = set(re.findall(r'getattr\(self\.event_sink, "(\w+)"', source))
        self.assertIn("enroll_viewer", looked_up, "the probe itself has drifted")

        class PublisherShaped:
            def alive(self):
                return True

            def send(self, event, **_kw):
                return True

            def close(self):
                pass

            def update_audience(self, *_a):
                return True

            def link_viewer(self, *_a):
                return True

            def enroll_viewer(self, *_a):
                return True

        with tempfile.TemporaryDirectory() as folder:
            marker = WorkerHealthMarker(Path(folder) / "worker.state")
            health = HealthReportingSink(PublisherShaped(), marker)
            router = voice_captions.CaptionRouter(health, focus_provider=lambda: False)
            forward = voice_captions._caption_forwarder(router)
            missing = sorted(
                name for name in looked_up if not callable(getattr(forward, name, None))
            )
        self.assertEqual([], missing, f"the coordinator would silently skip: {missing}")


class FakePipe:
    def __init__(self, *, blocking=False):
        self.blocking = blocking
        self.entered = threading.Event()
        self.release = threading.Event()
        self.broken = False
        self.closed = False
        self.lines = []

    def write(self, _payload):
        self.lines.append(_payload)
        if self.blocking:
            self.entered.set()
            self.release.wait(2)
        if self.broken:
            raise BrokenPipeError()

    def flush(self):
        if self.broken:
            raise BrokenPipeError()

    def close(self):
        self.closed = True


class FakeOverlayProcess:
    def __init__(
        self,
        *,
        blocking=False,
        terminate_exits=True,
        terminate_error=False,
        kill_error=False,
    ):
        self.stdin = FakePipe(blocking=blocking)
        self.returncode = None
        self.terminate_exits = terminate_exits
        self.terminate_error = terminate_error
        self.kill_error = kill_error
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("overlay", timeout)
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1
        if self.terminate_error:
            raise OSError("transient terminate failure")
        if self.terminate_exits:
            self.returncode = 1
            self.stdin.broken = True
            self.stdin.release.set()

    def kill(self):
        self.kill_calls += 1
        if self.kill_error:
            raise OSError("transient kill failure")
        self.returncode = -9
        self.stdin.broken = True
        self.stdin.release.set()


class ReadinessTests(unittest.TestCase):
    def test_prerequisites_are_checked_before_credentials_are_loaded(self):
        store = FakeStore()
        missing_node = check_runtime_readiness(
            node_detector=lambda: NodeAdapterCapability(False, "missing"),
            credential_store=store,
        )
        self.assertFalse(missing_node.ready)
        self.assertEqual(0, store.loads)

        missing_stt = check_runtime_readiness(
            node_detector=lambda: READY_NODE,
            stt_config_loader=lambda: CONFIG,
            stt_detector=lambda _: BackendCapability("fake", False, "missing"),
            credential_store=store,
        )
        self.assertFalse(missing_stt.ready)
        self.assertEqual(1, store.loads)

    def test_successful_runtime_verification_is_cached_until_assets_change(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest = root / "active.json"
            server = root / "whisper-server.exe"
            model = root / "model.bin"
            manifest.write_text("{}", encoding="ascii")
            server.write_bytes(b"server")
            model.write_bytes(b"model")
            calls = []

            def environment():
                calls.append(True)
                return {
                    "VOICE_CAPTIONS_WHISPER_SERVER": str(server),
                    "VOICE_CAPTIONS_WHISPER_MODEL": str(model),
                }

            voice_captions._STT_CONFIG_CACHE = None
            with patch(
                "VoiceCaptions.runtime_installer.default_runtime_root",
                return_value=root,
            ), patch(
                "VoiceCaptions.runtime_installer.stt_environment",
                side_effect=environment,
            ):
                first = installed_stt_config()
                second = installed_stt_config()
                model.write_bytes(b"model-changed")
                third = installed_stt_config()
            voice_captions._STT_CONFIG_CACHE = None

        self.assertEqual(first, second)
        self.assertEqual(first, third)
        self.assertEqual(2, len(calls))

    def test_managed_runtime_exposes_fallback_and_vad_assets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest = root / "active.json"
            manifest.write_text("{}", encoding="ascii")
            paths = {
                "VOICE_CAPTIONS_WHISPER_SERVER": root / "cuda-server.exe",
                "VOICE_CAPTIONS_WHISPER_FALLBACK_SERVER": root / "cpu-server.exe",
                "VOICE_CAPTIONS_WHISPER_MODEL": root / "turbo.bin",
                "VOICE_CAPTIONS_WHISPER_DLL": root / "whisper.dll",
                "VOICE_CAPTIONS_WHISPER_VAD_MODEL": root / "silero.bin",
            }
            for path in paths.values():
                path.write_bytes(b"asset")

            voice_captions._STT_CONFIG_CACHE = None
            with patch(
                "VoiceCaptions.runtime_installer.default_runtime_root",
                return_value=root,
            ), patch(
                "VoiceCaptions.runtime_installer.stt_environment",
                return_value={key: str(path) for key, path in paths.items()},
            ):
                config = installed_stt_config()
            voice_captions._STT_CONFIG_CACHE = None

        self.assertEqual(paths["VOICE_CAPTIONS_WHISPER_SERVER"], config.server_path)
        self.assertEqual(
            paths["VOICE_CAPTIONS_WHISPER_FALLBACK_SERVER"],
            config.fallback_server_path,
        )
        self.assertEqual(paths["VOICE_CAPTIONS_WHISPER_MODEL"], config.model_path)
        self.assertEqual(paths["VOICE_CAPTIONS_WHISPER_DLL"], config.vad_library_path)
        self.assertEqual(
            paths["VOICE_CAPTIONS_WHISPER_VAD_MODEL"], config.vad_model_path
        )

    def test_explicit_environment_is_used_without_a_managed_runtime(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = {
                "VOICE_CAPTIONS_WHISPER_SERVER": root / "whisper-server",
                "VOICE_CAPTIONS_WHISPER_FALLBACK_SERVER": root / "fallback-server",
                "VOICE_CAPTIONS_WHISPER_MODEL": root / "turbo.bin",
                "VOICE_CAPTIONS_WHISPER_DLL": root / "libwhisper.so",
                "VOICE_CAPTIONS_WHISPER_VAD_MODEL": root / "silero.bin",
            }
            for path in paths.values():
                path.write_bytes(b"asset")

            voice_captions._STT_CONFIG_CACHE = None
            with patch(
                "VoiceCaptions.runtime_installer.default_runtime_root",
                return_value=root,
            ), patch(
                "VoiceCaptions.runtime_installer.stt_environment",
                return_value={},
            ), patch.dict(
                os.environ,
                {key: str(path) for key, path in paths.items()},
                clear=False,
            ):
                config = installed_stt_config()
            voice_captions._STT_CONFIG_CACHE = None

        self.assertEqual(paths["VOICE_CAPTIONS_WHISPER_SERVER"], config.server_path)
        self.assertEqual(
            paths["VOICE_CAPTIONS_WHISPER_FALLBACK_SERVER"],
            config.fallback_server_path,
        )
        self.assertEqual(paths["VOICE_CAPTIONS_WHISPER_MODEL"], config.model_path)
        self.assertEqual(paths["VOICE_CAPTIONS_WHISPER_DLL"], config.vad_library_path)
        self.assertEqual(
            paths["VOICE_CAPTIONS_WHISPER_VAD_MODEL"], config.vad_model_path
        )

    def test_runtime_hash_cache_has_a_bounded_integrity_recheck(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest = root / "active.json"
            server = root / "whisper-server.exe"
            model = root / "model.bin"
            manifest.write_text("{}", encoding="ascii")
            server.write_bytes(b"server")
            model.write_bytes(b"model")
            original = model.stat()
            calls = []

            def environment():
                calls.append(True)
                return {
                    "VOICE_CAPTIONS_WHISPER_SERVER": str(server),
                    "VOICE_CAPTIONS_WHISPER_MODEL": str(model),
                }

            voice_captions._STT_CONFIG_CACHE = None
            with patch(
                "VoiceCaptions.runtime_installer.default_runtime_root",
                return_value=root,
            ), patch(
                "VoiceCaptions.runtime_installer.stt_environment",
                side_effect=environment,
            ), patch(
                "VoiceCaptions.voice_captions.time.monotonic",
                side_effect=(0.0, 1.0, 31.0),
            ):
                installed_stt_config()
                model.write_bytes(b"other")
                os.utime(model, ns=(original.st_atime_ns, original.st_mtime_ns))
                installed_stt_config()
                installed_stt_config()
            voice_captions._STT_CONFIG_CACHE = None

        self.assertEqual(2, len(calls))

    def test_all_three_prerequisites_are_required(self):
        missing_store = FakeStore(None)
        missing_credentials = check_runtime_readiness(
            node_detector=lambda: READY_NODE,
            stt_config_loader=lambda: CONFIG,
            stt_detector=lambda _: READY_STT,
            credential_store=missing_store,
        )
        self.assertFalse(missing_credentials.ready)
        self.assertEqual("Setup required: connect Discord", missing_credentials.public_status)

        ready = check_runtime_readiness(
            node_detector=lambda: READY_NODE,
            stt_config_loader=lambda: CONFIG,
            stt_detector=lambda _: READY_STT,
            speech_gate_detector=lambda _: READY_STT,
            credential_store=FakeStore(),
        )
        self.assertTrue(ready.ready)
        self.assertIs(CREDENTIALS, ready.credentials)

    def test_missing_speech_gate_never_reports_ready(self):
        readiness = check_runtime_readiness(
            node_detector=lambda: READY_NODE,
            stt_config_loader=lambda: CONFIG,
            stt_detector=lambda _: READY_STT,
            speech_gate_detector=lambda _: BackendCapability(
                "speech gate", False, "missing"
            ),
            credential_store=FakeStore(),
        )

        self.assertFalse(readiness.ready)
        self.assertEqual(
            "Setup required: update local speech tools",
            readiness.public_status,
        )

    def test_watch_does_not_start_overlay_or_capture_before_ready(self):
        stop = threading.Event()
        overlay_calls = []
        capture_calls = []

        def readiness():
            stop.set()
            return RuntimeReadiness(False, "Setup required: connect Discord")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = run_watch(
                stop,
                readiness_check=readiness,
                overlay_factory=lambda: overlay_calls.append(True),
                capture_runner=lambda *args: capture_calls.append(args),
                retry_seconds=0,
            )
        self.assertEqual(0, result)
        self.assertEqual([], overlay_calls)
        self.assertEqual([], capture_calls)
        self.assertNotIn(CREDENTIALS.bot_token, output.getvalue())


class OverlaySinkTests(unittest.TestCase):
    def test_supervisor_reuses_member_overlay_sink(self):
        self.assertIs(OverlaySink, voice_captions.OverlaySink)

    def test_member_overlay_child_isolated_from_python_environment(self):
        source = (Path(__file__).resolve().parents[1] / "member_overlay.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            '[child_python(), "-E", "-s", "-B", "-u", str(OVERLAY), "--exit-on-eof"]',
            source,
        )

    def test_writer_releases_last_caption_while_waiting_for_more(self):
        process = FakeOverlayProcess()
        sink = OverlaySink(process)
        event = CaptionEvent(
            speaker_id="speaker-token",
            speaker="Mira",
            character="",
            role="HEALER",
            text="private caption",
            final=True,
            sequence=1,
            timestamp=1.0,
        )
        self.assertTrue(sink.send(event))
        sink._pending.join()
        self.assertIsNone(sink._active_payload)
        sink.close()

    def test_a_replay_burst_larger_than_the_queue_loses_nothing(self):
        """256 replayed captions must survive a 32-slot queue.

        The relay replays its full buffer back-to-back on reconnect. With
        put_nowait, the 33rd caption latched _failed, the client treated one
        drop as fatal, reconnected, met the same replay and failed the same
        way - no captions for the rest of the fight. With bounded blocking,
        a healthy child absorbs the burst as backpressure and every caption
        arrives; only a child that stops reading entirely still fails closed,
        which the fails-closed test above continues to pin.
        """
        process = FakeOverlayProcess()
        sink = OverlaySink(process)
        events = [
            CaptionEvent(
                speaker_id="speaker-token",
                speaker="Mira",
                character="",
                role="HEALER",
                text="replayed caption %d" % index,
                final=True,
                sequence=index,
                timestamp=float(index),
            )
            for index in range(1, 257)
        ]
        results = [sink.send(event) for event in events]
        sink._pending.join()
        sink.close()
        self.assertTrue(all(results), "every send in the burst must succeed")
        written = "".join(process.stdin.lines)
        for index in (1, 33, 128, 256):
            self.assertIn(
                "replayed caption %d" % index, written,
                "caption %d was dropped by the burst" % index,
            )

    def test_non_reading_overlay_fails_closed_when_queue_is_full(self):
        process = FakeOverlayProcess(blocking=True)
        sink = OverlaySink(process, max_pending=1)
        event = CaptionEvent(
            speaker_id="speaker-token",
            speaker="Mira",
            character="",
            role="HEALER",
            text="private caption",
            final=True,
            sequence=1,
            timestamp=1.0,
        )
        self.assertTrue(sink.send(event))
        self.assertTrue(process.stdin.entered.wait(1))
        self.assertTrue(sink.send(event))
        self.assertFalse(sink.send(event))

        sink.close()
        self.assertGreaterEqual(process.terminate_calls, 1)
        self.assertFalse(sink._writer.is_alive())
        self.assertTrue(process.stdin.closed)

    def test_close_kills_overlay_after_both_waits_expire(self):
        process = FakeOverlayProcess(terminate_exits=False)
        sink = OverlaySink(process)
        sink.close()
        self.assertEqual(1, process.terminate_calls)
        self.assertEqual(1, process.kill_calls)
        self.assertEqual(-9, process.returncode)

    def test_terminate_oserror_still_attempts_kill_fallback(self):
        process = FakeOverlayProcess(
            terminate_exits=False,
            terminate_error=True,
        )
        sink = OverlaySink(process)
        sink.close()
        self.assertEqual(1, process.terminate_calls)
        self.assertEqual(1, process.kill_calls)
        self.assertEqual(-9, process.returncode)

    def test_kill_oserror_is_contained_after_pipe_is_closed(self):
        process = FakeOverlayProcess(
            terminate_exits=False,
            kill_error=True,
        )
        sink = OverlaySink(process)
        sink.close()
        self.assertEqual(1, process.terminate_calls)
        self.assertEqual(1, process.kill_calls)
        self.assertTrue(process.stdin.closed)

    def test_ready_watch_preserves_capture_and_overlay_lifecycle(self):
        stop = threading.Event()
        overlay = FakeOverlay()
        calls = []
        readiness = RuntimeReadiness(
            True,
            "Vemal's Ears host ready",
            node=READY_NODE,
            stt_config=CONFIG,
            credentials=CREDENTIALS,
        )

        async def capture(*args):
            calls.append(args)
            stop.set()

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = run_watch(
                stop,
                readiness_check=lambda: readiness,
                overlay_factory=lambda: overlay,
                capture_runner=capture,
                retry_seconds=0,
            )
        self.assertEqual(0, result)
        self.assertEqual(1, len(calls))
        self.assertTrue(overlay.closed)
        self.assertEqual(
            (SessionEvent("stopped"),),
            overlay.events_at_close,
            "a successful service stop must reach viewers before the publisher closes",
        )
        self.assertIn("Vemal's Ears host ready", output.getvalue())
        self.assertNotIn(CREDENTIALS.bot_token, output.getvalue())




class OverlaySinkSentinelTests(unittest.TestCase):
    """The fix that would otherwise vanish when this branch merges.

    The base branch fixed OverlaySink.close() inside voice_captions.py. This
    branch deletes that class from that file and re-creates it here, which git
    cannot rename-match, so merging the base conflicts on a block this branch
    removed - and resolving that toward this branch drops the fix into the one
    copy that ships to members. Nothing else would have caught it.
    """

    def test_close_evicts_to_make_room_for_its_stop_sentinel(self):
        """A full queue must not swallow the sentinel.

        The writer's only exit from an untimed get() is the sentinel or a dead
        process. If close() drops the sentinel because the queue was full and
        the writer then drains that queue successfully, it parks forever with
        _closing already set, so nothing can ever enqueue again. watch_captions
        builds a fresh sink per reconnect, so a raid night of relay blips leaks
        one parked daemon thread and one unclosed pipe handle each time.
        """

        class RecordingQueue(queue.Queue):
            def __init__(self, maxsize=0):
                super().__init__(maxsize)
                self.put_payloads = []

            def put_nowait(self, item):
                # super() first: a put that raises Full must NOT be recorded,
                # or the recorder logs attempts rather than deliveries and the
                # test passes against the very bug it exists to catch.
                super().put_nowait(item)
                self.put_payloads.append(item)

        process = FakeOverlayProcess(blocking=True)
        sink = OverlaySink(process, max_pending=1)
        # The writer parked on the original queue the moment it started, so
        # swapping in the recorder detaches it. That is deliberate: it makes
        # "the queue is full when close() runs" a fact rather than a race.
        recording = RecordingQueue(maxsize=1)
        sink._pending = recording

        event = CaptionEvent(
            speaker_id="speaker-token",
            speaker="Mira",
            character="",
            role="HEALER",
            text="stack behind the pillar",
            final=True,
            sequence=1,
            timestamp=1.0,
        )
        self.assertTrue(sink.send(event))
        self.assertTrue(recording.full())

        sink.close()

        self.assertIn(
            _OVERLAY_STOP,
            recording.put_payloads,
            "close() dropped its stop sentinel on a full queue, so the writer "
            "would never be released from its untimed get()",
        )


class WorkerLogTests(unittest.TestCase):
    """The worker log is the only window an operator has on the bot.

    Shino, 2026-09-04: "check the logs it keeps logging in and out of
    channels and causing disruptions". It could not answer that: no
    times, and the voice channel changes were never written at all.
    """

    def test_every_line_carries_the_time(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            voice_captions.status("Captions active")
        written = output.getvalue().rstrip("\n")
        self.assertRegex(written, r"^\d\d:\d\d:\d\d  Captions active$")
        # A PREFIX, so the companion window's substring matching - and
        # the tests that assert on these sentences - keep working.
        self.assertIn("Captions active", written)

    def test_the_bot_leaving_and_rejoining_reaches_the_log(self):
        sink = FakeOverlay()
        router = voice_captions.CaptionRouter(sink, focus_provider=lambda: False)
        forward = voice_captions._caption_forwarder(router)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            forward.voice_state("voice.disconnected")
            forward.voice_state("voice.reconnecting")
            forward.voice_state("voice.reconnected")
            forward.voice_state("voice.something.newer")
        written = output.getvalue()
        self.assertIn("left the voice channel", written)
        self.assertIn("is rejoining the voice channel", written)
        self.assertIn("rejoined the voice channel", written)
        # An event type this build does not know stays out of the log
        # rather than printing a raw protocol word at the operator.
        self.assertNotIn("something.newer", written)

    def test_the_coordinator_finds_the_hook_on_the_real_forwarder(self):
        sink = FakeOverlay()
        router = voice_captions.CaptionRouter(sink, focus_provider=lambda: False)
        forward = voice_captions._caption_forwarder(router)
        self.assertTrue(callable(getattr(forward, "voice_state", None)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
