import asyncio
import array
import base64
import contextlib
import hashlib
import hmac
import io
import json
import math
import subprocess
import tempfile
import threading
import os
import unittest
from unittest import mock
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from VoiceCaptions import coordinator
from VoiceCaptions.coordinator import (
    MAX_ADAPTER_LINE_BYTES,
    _FINAL_SLOT,
    _SEQUENCE_SLOTS,
    AdapterProtocolDecoder,
    CompletedSegment,
    CoordinatorError,
    NodeAdapterCapability,
    SpeakerIdentity,
    VoiceCaptionCoordinator,
    build_node_environment,
    public_pairing_address,
    detect_node_adapter,
    node_adapter_command,
    run_node_capture,
    spawn_node_adapter,
)
from VoiceCaptions.credentials import DiscordCredentials
from VoiceCaptions.events import CaptionEvent, SessionEvent
from VoiceCaptions.stt import TranscriptResult
from VoiceCaptions.stt.contracts import BackendUnavailable


class EventLines:
    def __init__(self):
        self.sequence = 0

    def line(self, event_type, payload):
        self.sequence += 1
        return (
            json.dumps(
                {
                    "version": 1,
                    "sequence": self.sequence,
                    "time": "2026-08-29T12:00:00.000Z",
                    "type": event_type,
                    "payload": payload,
                },
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )


def speaker(user_id, name, *, mapped=False):
    character = None
    if mapped:
        character = {
            "characterName": name,
            "realm": "Area 52",
            "wowClass": "priest",
            "role": "healer",
        }
    return {"userId": user_id, "displayName": name, "character": character}


def started(
    segment_id,
    global_sequence,
    identity,
    *,
    audience_generation=1,
    audience_revision=1,
):
    return {
        "segmentId": segment_id,
        "globalSequence": global_sequence,
        "audienceGeneration": audience_generation,
        "audienceRevision": audience_revision,
        "speaker": identity,
        "format": {
            "sampleRateHz": 16_000,
            "channels": 1,
            "encoding": "pcm_s16le",
        },
    }


def chunk(
    segment_id,
    global_sequence,
    chunk_sequence,
    pcm,
    *,
    audience_generation=1,
    audience_revision=1,
):
    return {
        "segmentId": segment_id,
        "globalSequence": global_sequence,
        "audienceGeneration": audience_generation,
        "audienceRevision": audience_revision,
        "chunkSequence": chunk_sequence,
        "pcmBase64": base64.b64encode(pcm).decode("ascii"),
    }


def ended(
    segment_id,
    global_sequence,
    identity,
    chunks,
    pcm_bytes,
    status="completed",
    *,
    audience_generation=1,
    audience_revision=1,
):
    return {
        "segmentId": segment_id,
        "globalSequence": global_sequence,
        "audienceGeneration": audience_generation,
        "audienceRevision": audience_revision,
        "speaker": identity,
        "status": status,
        "chunks": chunks,
        "pcmBytes": pcm_bytes,
    }


def tone_pcm(amplitude, *, milliseconds=100):
    samples = 16_000 * milliseconds // 1_000
    wave = array.array(
        "h",
        (
            int(amplitude * math.sin(2.0 * math.pi * index / 40.0))
            for index in range(samples)
        ),
    )
    return wave.tobytes()


class FakeSink:
    def __init__(self, succeeds=True):
        self.succeeds = succeeds
        self.events = []

    def __call__(self, event):
        self.events.append(event)
        return self.succeeds


class HostedFakeSink(FakeSink):
    def __init__(self, succeeds=True):
        super().__init__(succeeds)
        self.audiences = []
        self.links = []

    def update_audience(self, generation, revision, tokens):
        self.audiences.append((generation, revision, tokens))
        return self.succeeds

    def link_viewer(self, code, token):
        self.links.append((code, token))
        return self.succeeds


class ConcurrentFakeSTT:
    def __init__(self, hold=False):
        self.hold = hold
        self.release = asyncio.Event()
        if not hold:
            self.release.set()
        self.changed = asyncio.Event()
        self.active = set()
        self.max_active = 0
        self.segments = []
        self.closed = False

    async def start(self):
        return None

    async def transcribe(self, audio):
        self.segments.append(audio)
        key = (audio.speaker_id, audio.segment_id)
        self.active.add(key)
        self.max_active = max(self.max_active, len(self.active))
        self.changed.set()
        try:
            await self.release.wait()
            return TranscriptResult(
                audio.speaker_id,
                audio.segment_id,
                f"caption-{audio.segment_id}",
                1,
            )
        finally:
            self.active.discard(key)
            self.changed.set()

    async def wait_active(self, count):
        while len(self.active) < count:
            self.changed.clear()
            if len(self.active) >= count:
                return
            await asyncio.wait_for(self.changed.wait(), 1)

    async def close(self):
        self.closed = True
        self.release.set()


class SlowCloseSTT(ConcurrentFakeSTT):
    def __init__(self):
        super().__init__()
        self.close_started = asyncio.Event()
        self.finish_close = asyncio.Event()

    async def close(self):
        self.close_started.set()
        await self.finish_close.wait()
        self.closed = True


class BlockingStartSTT(ConcurrentFakeSTT):
    def __init__(self):
        super().__init__()
        self.start_started = asyncio.Event()
        self.hold_start = asyncio.Event()

    async def start(self):
        self.start_started.set()
        await self.hold_start.wait()


class FakeStdin:
    """The adapter's shutdown channel, as a pipe that can be closed.

    Closing stdin is how the coordinator asks the adapter to stop, since
    terminate() on Windows delivers no signal the adapter can act on. A
    fake that ignores the close models an adapter that is wedged, which
    is what the stop-failure cases are about.
    """

    def __init__(self, on_close=None):
        self.closed = False
        self._on_close = on_close
        self.writes = []

    def is_closing(self):
        return self.closed

    def close(self):
        self.closed = True
        if self._on_close is not None:
            self._on_close()

    def write(self, payload):
        if self.closed:
            raise OSError("stdin is closed")
        self.writes.append(payload)

    async def drain(self):
        return None


class FakeNodeProcess:
    def __init__(self, lines, *, stop_failure="", exits_on_stdin_close=None):
        self.stdout = asyncio.StreamReader()
        for line in lines:
            self.stdout.feed_data(line)
        self.returncode = None
        self.stop_failure = stop_failure
        # A healthy adapter exits when its stdin closes, so that is the
        # default. A process configured to defy terminate() is wedged by
        # definition, and would not answer a polite request either - so
        # those cases still fall through to terminate and kill, which is
        # what they were written to exercise.
        if exits_on_stdin_close is None:
            exits_on_stdin_close = not stop_failure
        self.stdin = FakeStdin(
            on_close=(self._exit_cleanly if exits_on_stdin_close else None)
        )

    def _exit_cleanly(self):
        self.returncode = 0

    def terminate(self):
        if self.stop_failure == "terminate":
            raise ProcessLookupError()
        if self.stop_failure != "kill":
            self.returncode = 0

    def kill(self):
        if self.stop_failure == "kill":
            raise ProcessLookupError()
        self.returncode = -9

    async def wait(self):
        if self.stop_failure == "kill" and self.returncode is None:
            raise asyncio.TimeoutError()
        while self.returncode is None:
            await asyncio.sleep(0)
        return self.returncode


class QuickChatCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_result_is_strict_and_never_becomes_an_overlay_event(self):
        lines = EventLines()
        stt = ConcurrentFakeSTT()
        sink = FakeSink()
        instance = VoiceCaptionCoordinator(stt, sink)

        result = await instance.feed_line(
            lines.line(
                "quickchat.result",
                {
                    "requestId": "worker_request_123456",
                    "status": "delivered",
                },
            )
        )
        self.assertEqual("worker_request_123456", result.request_id)
        self.assertEqual("delivered", result.status)
        self.assertEqual([], sink.events)
        await instance.shutdown()

    async def test_a_bad_result_is_dropped_and_does_not_stop_captions(self):
        """A malformed acknowledgement is about ONE typed message.

        Shino, 2026-09-05: "when a message is sent via the 'Message Group'
        button VE disconnects from discord after showing the text." This
        raise was one of the ways that happened - a bad reply about a line
        somebody typed stopped the capture pipeline, which takes the voice
        session and everybody's captions with it. The strictness stays;
        only the blast radius changes.
        """
        reasons = []
        lines = EventLines()
        invalid = VoiceCaptionCoordinator(ConcurrentFakeSTT(), FakeSink())
        invalid.on_segment_dropped = reasons.append

        result = await invalid.feed_line(
            lines.line(
                "quickchat.result",
                {
                    "requestId": "worker_request_123456",
                    "status": "delivered",
                    "text": "must not cross the result boundary",
                },
            )
        )

        self.assertIsNone(result, "a malformed result must not be believed")
        self.assertEqual(["invalid_quick_chat_result"], reasons)
        invalid.raise_if_failed()

        # And the very next line still works: the pipeline is alive.
        good = await invalid.feed_line(
            lines.line(
                "quickchat.result",
                {
                    "requestId": "worker_request_123457",
                    "status": "delivery_failed",
                },
            )
        )
        self.assertEqual("worker_request_123457", good.request_id)
        await invalid.shutdown()

    async def test_a_bad_status_word_is_dropped_too(self):
        for payload in (
            {"requestId": "worker_request_123456", "status": "sent"},
            {"requestId": "worker_request_123456", "status": []},
            {"requestId": "worker_request_123456", "status": {}},
            {"requestId": "no", "status": "delivered"},
            {"requestId": "worker_request_123456"},
        ):
            with self.subTest(payload=payload):
                reasons = []
                instance = VoiceCaptionCoordinator(
                    ConcurrentFakeSTT(), FakeSink()
                )
                instance.on_segment_dropped = reasons.append
                lines = EventLines()
                self.assertIsNone(
                    await instance.feed_line(
                        lines.line("quickchat.result", payload)
                    )
                )
                self.assertEqual(["invalid_quick_chat_result"], reasons)
                instance.raise_if_failed()
                good = await instance.feed_line(
                    lines.line(
                        "quickchat.result",
                        {
                            "requestId": "worker_request_123457",
                            "status": "delivered",
                        },
                    )
                )
                self.assertEqual("worker_request_123457", good.request_id)
                self.assertEqual("delivered", good.status)
                await instance.shutdown()

    async def test_capture_loop_forwards_command_and_returns_delivery(self):
        stop = threading.Event()

        class QuickSink(FakeSink):
            def __init__(self):
                super().__init__()
                self.commands = [
                    SimpleNamespace(
                        request_id="worker_request_123456",
                        speaker="Vemal",
                        wow_class="paladin",
                        text="Stack on me",
                    )
                ]
                self.results = []

            def next_quick_chat(self):
                return self.commands.pop(0) if self.commands else None

            def complete_quick_chat(self, request_id, status):
                self.results.append((request_id, status))
                stop.set()
                return True

            def alive(self):
                return True

        lines = EventLines()
        process = FakeNodeProcess(
            [
                lines.line(
                    "quickchat.result",
                    {
                        "requestId": "worker_request_123456",
                        "status": "delivered",
                    },
                )
            ]
        )
        sink = QuickSink()

        async def spawner(*_args):
            return process

        await run_node_capture(
            DiscordCredentials(
                "123456789012345678", "not-a-real-token." + "x" * 40
            ),
            NodeAdapterCapability(True, "ready", Path("C:/node.exe")),
            ConcurrentFakeSTT(),
            sink,
            stop,
            spawner=spawner,
        )

        self.assertEqual(
            [("worker_request_123456", "delivered")], sink.results
        )
        self.assertEqual(1, len(process.stdin.writes))
        command = json.loads(process.stdin.writes[0])
        self.assertEqual("quick_chat.command", command["type"])
        self.assertEqual("Stack on me", command["text"])
        self.assertNotIn("device_token", command)


class GracefulStopTests(unittest.IsolatedAsyncioTestCase):
    """Closing stdin is the shutdown request; terminate is the fallback.

    This matters beyond tidiness: the adapter's consent ledger clears its
    dirty marker in the shutdown chain, and on Windows terminate() runs
    none of it. Every stop was therefore unclean, and consent persistence
    - the whole point of remembering an answer - silently did nothing.
    """

    async def test_a_healthy_adapter_stops_by_having_its_stdin_closed(self):
        process = FakeNodeProcess([], exits_on_stdin_close=True)
        await coordinator._stop_process(process)
        self.assertTrue(process.stdin.closed)
        self.assertEqual(0, process.returncode, "it should exit cleanly")

    async def test_a_wedged_adapter_is_still_terminated(self):
        process = FakeNodeProcess([], exits_on_stdin_close=False)
        await coordinator._stop_process(process)
        self.assertTrue(process.stdin.closed, "the polite request comes first")
        self.assertIsNotNone(process.returncode, "but it does not get to hang")

    async def test_an_already_dead_process_is_left_alone(self):
        process = FakeNodeProcess([])
        process.returncode = 0
        await coordinator._stop_process(process)
        self.assertFalse(process.stdin.closed)


class DecoderTests(unittest.TestCase):
    def test_private_panel_failures_are_known_diagnostics(self):
        decoder = AdapterProtocolDecoder()
        lines = EventLines()
        for event_type in ("consent.panel.failed", "mapping.panel.failed"):
            with self.subTest(event_type=event_type):
                event = decoder.parse_line(lines.line(event_type, {"reason": "private"}))
                self.assertEqual(event_type, event.event_type)

    def test_voice_reconnect_lifecycle_events_are_known(self):
        decoder = AdapterProtocolDecoder()
        lines = EventLines()
        for event_type in ("voice.reconnecting", "voice.reconnected"):
            event = decoder.parse_line(lines.line(event_type, {"guildId": "1"}))
            self.assertEqual(event_type, event.event_type)

    def test_global_jsonl_order_is_strict(self):
        decoder = AdapterProtocolDecoder()
        lines = EventLines()
        decoder.parse_line(lines.line("adapter.starting", {}))
        skipped = json.loads(lines.line("adapter.ready", {}))
        skipped["sequence"] += 1
        with self.assertRaises(CoordinatorError) as caught:
            decoder.parse_line(json.dumps(skipped))
        self.assertEqual("invalid_order", caught.exception.code)
        self.assertNotIn("payload", str(caught.exception))

    def test_oversized_and_unknown_lines_fail_closed(self):
        decoder = AdapterProtocolDecoder()
        with self.assertRaises(CoordinatorError):
            decoder.parse_line(b"x" * (MAX_ADAPTER_LINE_BYTES + 1))
        lines = EventLines()
        with self.assertRaises(CoordinatorError) as caught:
            decoder.parse_line(lines.line("private.future.event", {}))
        self.assertEqual("unknown_event", caught.exception.code)

    def test_hostile_json_shapes_fail_closed_without_echoing_input(self):
        private_marker = "private-transcript-marker"
        prefix = (
            b'{"version":1,"sequence":1,'
            b'"time":"2026-08-29T12:00:00Z",'
            b'"type":"adapter.starting","payload":'
        )
        samples = (
            b"\xff" + private_marker.encode("ascii"),
            prefix + b"[" * 1_500 + b"]" * 1_500 + b"}",
            (
                b'{"version":1,"version":1,"sequence":1,'
                b'"time":"2026-08-29T12:00:00Z",'
                b'"type":"adapter.starting","payload":{}}'
            ),
            (
                b'{"version":1,"sequence":'
                + b"9" * 5_000
                + b',"time":"2026-08-29T12:00:00Z",'
                b'"type":"adapter.starting","payload":{}}'
            ),
            (
                b'{"version":1,"sequence":NaN,'
                b'"time":"2026-08-29T12:00:00Z",'
                b'"type":"adapter.starting","payload":{}}'
            ),
        )
        for sample in samples:
            with self.subTest(size=len(sample)):
                with self.assertRaises(CoordinatorError) as caught:
                    AdapterProtocolDecoder().parse_line(sample)
                self.assertEqual("invalid_json", caught.exception.code)
                self.assertNotIn(private_marker, str(caught.exception))

        with self.assertRaises(CoordinatorError) as caught:
            AdapterProtocolDecoder().parse_line("\ud800")
        self.assertEqual("invalid_json", caught.exception.code)

    def test_version_requires_the_exact_integer_one(self):
        for invalid_version in (True, 1.0, "1"):
            with self.subTest(version=invalid_version):
                line = json.dumps(
                    {
                        "version": invalid_version,
                        "sequence": 1,
                        "time": "2026-08-29T12:00:00Z",
                        "type": "adapter.starting",
                        "payload": {},
                    }
                )
                with self.assertRaises(CoordinatorError) as caught:
                    AdapterProtocolDecoder().parse_line(line)
                self.assertEqual("invalid_envelope", caught.exception.code)


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_caption_style_uses_each_speakers_recent_voice_level(self):
        lines = EventLines()
        sink = FakeSink()
        stt = ConcurrentFakeSTT()
        instance = VoiceCaptionCoordinator(
            stt,
            sink,
            identity_key=b"test-key" * 4,
        )
        identity = speaker("111111111111111111", "Mira")

        async def speak(sequence, amplitude):
            pcm = tone_pcm(amplitude)
            segment_id = f"part-{sequence}"
            await instance.feed_line(
                lines.line(
                    "audio.segment.started",
                    started(segment_id, sequence, identity),
                )
            )
            await instance.feed_line(
                lines.line(
                    "audio.segment.chunk",
                    chunk(segment_id, sequence, 1, pcm),
                )
            )
            await instance.feed_line(
                lines.line(
                    "audio.segment.ended",
                    ended(segment_id, sequence, identity, 1, len(pcm)),
                )
            )
            await instance.drain()

        for sequence in range(1, 4):
            await speak(sequence, 5_000)
        await speak(4, 900)
        await speak(5, 20_000)

        styles = [
            event.speech_style
            for event in sink.events
            if isinstance(event, CaptionEvent)
        ]
        self.assertEqual(
            ["normal", "normal", "normal", "whisper", "shout"],
            styles,
        )
        await instance.shutdown()

    async def test_consent_withdrawal_cannot_recreate_style_state_from_inflight_audio(self):
        lines = EventLines()
        sink = HostedFakeSink()
        stt = ConcurrentFakeSTT(hold=True)
        identity_key = b"prosody-consent-key".ljust(32, b"!")
        instance = VoiceCaptionCoordinator(stt, sink, identity_key=identity_key)
        user_id = "111111111111111111"
        identity = speaker(user_id, "Mira")
        pcm = tone_pcm(5_000)
        await instance.feed_line(
            lines.line(
                "audience.snapshot",
                {"generation": 1, "revision": 1, "participantIds": [user_id]},
            )
        )
        await instance.feed_line(
            lines.line("audio.segment.started", started("one", 1, identity))
        )
        await instance.feed_line(
            lines.line("audio.segment.chunk", chunk("one", 1, 1, pcm))
        )
        await instance.feed_line(
            lines.line(
                "audio.segment.ended",
                ended("one", 1, identity, 1, len(pcm)),
            )
        )
        await stt.wait_active(1)

        token = instance._speaker_token(user_id)
        await instance.feed_line(
            lines.line(
                "consent.declined",
                {"userId": user_id, "session": {"phase": "paused"}},
            )
        )
        stt.release.set()
        await instance.drain()

        self.assertNotIn(token, instance._speech_styles._speakers)
        caption_event = next(
            event for event in sink.events if isinstance(event, CaptionEvent)
        )
        self.assertEqual("normal", caption_event.speech_style)

        await instance.feed_line(
            lines.line(
                "consent.granted",
                {"userId": user_id, "session": {"phase": "active"}},
            )
        )
        await instance.feed_line(
            lines.line("audio.segment.started", started("two", 2, identity))
        )
        await instance.feed_line(
            lines.line("audio.segment.chunk", chunk("two", 2, 1, pcm))
        )
        await instance.feed_line(
            lines.line(
                "audio.segment.ended",
                ended("two", 2, identity, 1, len(pcm)),
            )
        )
        await instance.drain()
        self.assertIn(token, instance._speech_styles._speakers)
        await instance.shutdown()

    async def test_departed_speaker_cannot_recreate_style_state_from_inflight_audio(self):
        lines = EventLines()
        sink = HostedFakeSink()
        stt = ConcurrentFakeSTT(hold=True)
        identity_key = b"prosody-roster-key".ljust(32, b"!")
        instance = VoiceCaptionCoordinator(stt, sink, identity_key=identity_key)
        alice = "111111111111111111"
        bob = "222222222222222222"
        identity = speaker(alice, "Mira")
        pcm = tone_pcm(5_000)
        await instance.feed_line(
            lines.line(
                "audience.snapshot",
                {"generation": 1, "revision": 1, "participantIds": [alice, bob]},
            )
        )
        await instance.feed_line(
            lines.line("audio.segment.started", started("one", 1, identity))
        )
        await instance.feed_line(
            lines.line("audio.segment.chunk", chunk("one", 1, 1, pcm))
        )
        await instance.feed_line(
            lines.line(
                "audio.segment.ended",
                ended("one", 1, identity, 1, len(pcm)),
            )
        )
        await stt.wait_active(1)

        await instance.feed_line(
            lines.line(
                "audience.snapshot",
                {"generation": 1, "revision": 2, "participantIds": [bob]},
            )
        )
        stt.release.set()
        await instance.drain()

        self.assertNotIn(
            instance._speaker_token(alice),
            instance._speech_styles._speakers,
        )
        await instance.shutdown()

    async def test_empty_stream_part_cannot_style_the_spoken_parts(self):
        sink = FakeSink()
        identity = SpeakerIdentity(
            user_id="111111111111111111",
            display_name="Mira",
        )
        instance = VoiceCaptionCoordinator(
            ConcurrentFakeSTT(),
            sink,
            identity_key=b"prosody-parts-key".ljust(32, b"!"),
        )
        token = instance._speaker_token(identity.user_id)
        for level in (-21.0, -19.5, -20.0):
            instance._speech_styles.classify_and_observe(token, level)

        instance._complete_part(
            CompletedSegment(1, 1, 1, identity, bytearray(), 1, False),
            token,
            "",
            -40.0,
        )
        instance._complete_part(
            CompletedSegment(1, 1, 1, identity, bytearray(), 2, True),
            token,
            "spoken words",
            -10.0,
        )

        caption_event = next(
            event for event in sink.events if isinstance(event, CaptionEvent)
        )
        self.assertEqual("shout", caption_event.speech_style)
        await instance.shutdown()

    async def test_audience_and_link_ids_are_pseudonymized_before_host_calls(self):
        lines = EventLines()
        sink = HostedFakeSink()
        stt = ConcurrentFakeSTT()
        identity_key = b"viewer-test-key".ljust(32, b"!")
        instance = VoiceCaptionCoordinator(stt, sink, identity_key=identity_key)
        alice = "111111111111111111"
        bob = "222222222222222222"

        await instance.feed_line(
            lines.line(
                "audience.snapshot",
                {"generation": 4, "revision": 9, "participantIds": [alice, bob]},
            )
        )
        await instance.feed_line(
            lines.line(
                "viewer.link.requested",
                {"userId": alice, "code": "AB12CD34"},
            )
        )

        token = lambda raw: hmac.new(
            identity_key, raw.encode("ascii"), hashlib.sha256
        ).hexdigest()[:24]
        self.assertEqual(
            [(4, 9, (token(alice), token(bob)))],
            sink.audiences,
        )
        self.assertEqual([("AB12CD34", token(alice))], sink.links)
        self.assertNotIn(alice, repr(sink.audiences) + repr(sink.links))
        self.assertNotIn(bob, repr(sink.audiences) + repr(sink.links))
        await instance.shutdown()

    async def test_invalid_audience_or_link_payload_fails_closed(self):
        samples = (
            (
                "audience.snapshot",
                {"generation": 1, "revision": 1, "participantIds": ["1", "1"]},
                "invalid_audience",
            ),
            (
                "viewer.link.requested",
                {"userId": "1", "code": "lower123"},
                "invalid_viewer_link",
            ),
        )
        for event_type, payload, code in samples:
            with self.subTest(event_type=event_type):
                stt = ConcurrentFakeSTT()
                instance = VoiceCaptionCoordinator(stt, HostedFakeSink())
                with self.assertRaises(CoordinatorError) as caught:
                    await instance.feed_line(EventLines().line(event_type, payload))
                self.assertEqual(code, caught.exception.code)
                self.assertTrue(stt.closed)

    async def test_routing_revision_is_stamped_at_speech_start(self):
        lines = EventLines()
        sink = FakeSink()
        stt = ConcurrentFakeSTT()
        instance = VoiceCaptionCoordinator(stt, sink, identity_key=b"test-key" * 4)
        identity = speaker("111111111111111111", "Mira")
        pcm = bytes(3_200)
        routing = {"audience_generation": 7, "audience_revision": 11}

        await instance.feed_line(
            lines.line("audio.segment.started", started("one", 1, identity, **routing))
        )
        await instance.feed_line(
            lines.line("audio.segment.chunk", chunk("one", 1, 1, pcm, **routing))
        )
        await instance.feed_line(
            lines.line(
                "audio.segment.ended",
                ended("one", 1, identity, 1, len(pcm), **routing),
            )
        )
        await instance.drain()

        caption_event = next(event for event in sink.events if isinstance(event, CaptionEvent))
        self.assertEqual(7, caption_event.audience_generation)
        self.assertEqual(11, caption_event.audience_revision)
        await instance.shutdown()

    async def test_routing_revision_cannot_change_mid_segment(self):
        lines = EventLines()
        stt = ConcurrentFakeSTT()
        instance = VoiceCaptionCoordinator(stt, FakeSink())
        identity = speaker("111111111111111111", "Mira")
        await instance.feed_line(
            lines.line("audio.segment.started", started("one", 1, identity))
        )
        with self.assertRaises(CoordinatorError) as caught:
            await instance.feed_line(
                lines.line(
                    "audio.segment.chunk",
                    chunk("one", 1, 1, bytes(3_200), audience_revision=2),
                )
            )
        self.assertEqual("audience_mismatch", caught.exception.code)
        self.assertTrue(stt.closed)

    def test_identity_key_must_be_exactly_32_bytes(self):
        with self.assertRaises(ValueError):
            VoiceCaptionCoordinator(ConcurrentFakeSTT(), FakeSink(), identity_key=b"short")
        with self.assertRaises(TypeError):
            VoiceCaptionCoordinator(ConcurrentFakeSTT(), FakeSink(), identity_key="x" * 32)

    async def test_private_panel_failures_do_not_stop_live_captions(self):
        lines = EventLines()
        sink = FakeSink()
        stt = ConcurrentFakeSTT()
        instance = VoiceCaptionCoordinator(stt, sink)

        await instance.feed_line(
            lines.line("consent.panel.failed", {"reason": "private"})
        )
        await instance.feed_line(
            lines.line("mapping.panel.failed", {"reason": "private"})
        )

        self.assertEqual([], sink.events)
        self.assertFalse(stt.closed)
        await instance.shutdown()

    async def test_a_spoken_name_is_corrected_before_it_reaches_the_overlay(self):
        """The wiring, not the rule.

        vocabulary.apply_pronunciations is unit-tested on its own. What this
        pins is that the caption path actually CALLS it - the failure mode
        where a correct helper sits unreferenced and every caption still
        carries the wrong spelling.
        """
        class SpokenNameSTT(ConcurrentFakeSTT):
            async def transcribe(self, audio):
                self.segments.append(audio)
                return TranscriptResult(
                    audio.speaker_id,
                    audio.segment_id,
                    "key hoss take the frontal",
                    1,
                )

        lines = EventLines()
        sink = FakeSink()
        stt = SpokenNameSTT()
        instance = VoiceCaptionCoordinator(stt, sink, identity_key=b"test-key" * 4)
        identity = speaker("111111111111111111", "Mira")
        pcm = bytes(3_200)

        await instance.feed_line(
            lines.line("audio.segment.started", started("one", 1, identity))
        )
        await instance.feed_line(
            lines.line("audio.segment.chunk", chunk("one", 1, 1, pcm))
        )
        await instance.feed_line(
            lines.line("audio.segment.ended", ended("one", 1, identity, 1, len(pcm)))
        )
        await instance.drain()

        caption = next(e for e in sink.events if isinstance(e, CaptionEvent))
        self.assertEqual("Keihas take the frontal", caption.text)
        await instance.shutdown()

    def test_correcting_after_the_join_catches_a_name_split_across_parts(self):
        """Why the call sits after _join_parts rather than per part.

        A streamed utterance is transcribed in pieces, and a name can land
        with its halves in different pieces. Correcting each piece alone
        would never see "key" and "hoss" adjacent.
        """
        parts = ["stack on key", "hoss now"]
        self.assertEqual(
            "stack on Keihas now",
            coordinator.apply_pronunciations(coordinator._join_parts(parts, 320)),
        )
        per_part = " ".join(coordinator.apply_pronunciations(p) for p in parts)
        self.assertEqual(
            "stack on key hoss now",
            per_part,
            "per-part correction was expected to MISS the split name",
        )

    async def test_interleaved_speakers_transcribe_concurrently_and_stay_isolated(self):
        lines = EventLines()
        sink = FakeSink()
        stt = ConcurrentFakeSTT(hold=True)
        coordinator = VoiceCaptionCoordinator(
            stt, sink, identity_key=b"test-key" * 4, clock=lambda: 123.0
        )
        mira = speaker("111111111111111111", "Mira", mapped=True)
        drex = speaker("222222222222222222", "Drex")
        mira_pcm = bytes([1]) * 3_200
        drex_pcm = bytes([2]) * 3_200

        private_stdout = io.StringIO()
        with contextlib.redirect_stdout(private_stdout):
            await coordinator.feed_line(lines.line("audio.segment.started", started("one", 1, mira)))
            await coordinator.feed_line(lines.line("audio.segment.started", started("two", 2, drex)))
            await coordinator.feed_line(lines.line("audio.segment.chunk", chunk("one", 1, 1, mira_pcm)))
            await coordinator.feed_line(lines.line("audio.segment.chunk", chunk("two", 2, 1, drex_pcm)))
            await coordinator.feed_line(lines.line("audio.segment.ended", ended("one", 1, mira, 1, 3_200)))
            await coordinator.feed_line(lines.line("audio.segment.ended", ended("two", 2, drex, 1, 3_200)))
            await stt.wait_active(2)
            stt.release.set()
            await coordinator.drain()

        captions = [event for event in sink.events if isinstance(event, CaptionEvent)]
        self.assertEqual(2, len(captions))
        # An utterance is now streamed to the STT in parts, so the segment
        # id handed over is a composed one - the speaker's global sequence
        # times the slot count, plus the part's slot. Both utterances here
        # are short enough to be a single final part. Derived rather than
        # written out, so this cannot silently drift from the constants.
        final = {
            sequence * _SEQUENCE_SLOTS + _FINAL_SLOT for sequence in (1, 2)
        }
        self.assertEqual(
            {f"caption-{value}" for value in final},
            {event.text for event in captions},
        )
        self.assertEqual({"Mira", "Drex"}, {event.speaker for event in captions})
        self.assertEqual({"HEALER", "UNKNOWN"}, {event.role for event in captions})
        self.assertEqual(2, stt.max_active)
        self.assertEqual(2, len({event.speaker_id for event in captions}))
        self.assertNotIn("111111111111111111", {event.speaker_id for event in captions})
        self.assertEqual("", private_stdout.getvalue())
        self.assertTrue(all(segment.wiped for segment in stt.segments))
        await coordinator.shutdown()

    async def test_empty_and_sub_100ms_segments_are_harmless_discards(self):
        lines = EventLines()
        sink = FakeSink()
        stt = ConcurrentFakeSTT()
        coordinator = VoiceCaptionCoordinator(stt, sink)
        identity = speaker("111111111111111111", "Mira")

        await coordinator.feed_line(lines.line("audio.segment.started", started("empty", 1, identity)))
        await coordinator.feed_line(lines.line("audio.segment.ended", ended("empty", 1, identity, 0, 0)))
        short_pcm = bytes(640)
        await coordinator.feed_line(lines.line("audio.segment.started", started("short", 2, identity)))
        await coordinator.feed_line(lines.line("audio.segment.chunk", chunk("short", 2, 1, short_pcm)))
        await coordinator.feed_line(lines.line("audio.segment.ended", ended("short", 2, identity, 1, 640)))
        await coordinator.drain()

        self.assertEqual([], stt.segments)
        self.assertEqual([], [event for event in sink.events if isinstance(event, CaptionEvent)])
        self.assertEqual(0, coordinator.assembler.total_pcm_bytes)
        await coordinator.shutdown()

    async def test_reordered_chunk_wipes_every_active_buffer_and_closes_stt(self):
        lines = EventLines()
        stt = ConcurrentFakeSTT()
        coordinator = VoiceCaptionCoordinator(stt, FakeSink())
        identity = speaker("111111111111111111", "Mira")
        await coordinator.feed_line(lines.line("audio.segment.started", started("one", 1, identity)))
        await coordinator.feed_line(lines.line("audio.segment.chunk", chunk("one", 1, 1, bytes(3_200))))
        with self.assertRaises(CoordinatorError) as caught:
            await coordinator.feed_line(lines.line("audio.segment.chunk", chunk("one", 1, 3, bytes(640))))
        self.assertEqual("invalid_chunk_order", caught.exception.code)
        self.assertEqual({}, coordinator.assembler.active)
        self.assertEqual(0, coordinator.assembler.total_pcm_bytes)
        self.assertTrue(stt.closed)

    async def test_mismatched_end_counts_fail_closed(self):
        lines = EventLines()
        stt = ConcurrentFakeSTT()
        coordinator = VoiceCaptionCoordinator(stt, FakeSink())
        identity = speaker("111111111111111111", "Mira")
        await coordinator.feed_line(lines.line("audio.segment.started", started("one", 1, identity)))
        await coordinator.feed_line(lines.line("audio.segment.chunk", chunk("one", 1, 1, bytes(3_200))))
        with self.assertRaises(CoordinatorError):
            await coordinator.feed_line(
                lines.line("audio.segment.ended", ended("one", 1, identity, 2, 3_200, "completed"))
            )
        self.assertTrue(stt.closed)
        self.assertEqual(0, coordinator.assembler.total_pcm_bytes)

    async def test_a_failed_segment_is_dropped_and_the_pipeline_lives(self):
        """One speaker's bad audio costs that segment, never the pipeline.

        This used to raise adapter_audio_failed, which the watch loop
        treats as fatal: one corrupt opus packet became "capture pipeline
        stopped" for the whole raid, three restarts in twenty minutes on
        a real machine. A failed segment is an honest per-segment report,
        the same class as the sub-100ms noise segments - wiped, dropped,
        and the very next segment still transcribes.
        """
        lines = EventLines()
        stt = ConcurrentFakeSTT()
        coordinator = VoiceCaptionCoordinator(stt, FakeSink())
        identity = speaker("111111111111111111", "Mira")
        await coordinator.feed_line(lines.line("audio.segment.started", started("one", 1, identity)))
        await coordinator.feed_line(lines.line("audio.segment.chunk", chunk("one", 1, 1, bytes(3_200))))
        await coordinator.feed_line(
            lines.line("audio.segment.ended", ended("one", 1, identity, 1, 3_200, "failed"))
        )
        # Wiped: no retained audio, no active segment, STT untouched.
        self.assertEqual({}, coordinator.assembler.active)
        self.assertEqual(0, coordinator.assembler.total_pcm_bytes)
        self.assertEqual(0, len(stt.segments))
        # And the pipeline is alive: the next segment goes all the way
        # through to STT.
        await coordinator.feed_line(lines.line("audio.segment.started", started("two", 2, identity)))
        await coordinator.feed_line(lines.line("audio.segment.chunk", chunk("two", 2, 1, bytes(6_400))))
        await coordinator.feed_line(
            lines.line("audio.segment.ended", ended("two", 2, identity, 1, 6_400, "completed"))
        )
        await coordinator.drain()
        self.assertEqual(1, len(stt.segments))
        await coordinator.shutdown()

    async def test_an_oversized_chunk_costs_one_segment_never_the_pipeline(self):
        """The chunk_limit twin of the failed-segment rule above.

        An oversize PCM chunk used to raise CoordinatorError("chunk_limit"),
        which the watch loop treats as fatal - one misbehaving stream
        became "capture pipeline stopped" for the whole raid (seen live,
        2026-09-01 evening logs). The proportionate cost is that one
        segment: wiped, tombstoned so its remaining chunks and end frame
        are swallowed instead of resurfacing as unknown_segment, while
        every other speaker keeps transcribing.
        """
        lines = EventLines()
        stt = ConcurrentFakeSTT()
        coordinator = VoiceCaptionCoordinator(stt, FakeSink())
        mira = speaker("111111111111111111", "Mira")
        drex = speaker("222222222222222222", "Drex")

        await coordinator.feed_line(lines.line("audio.segment.started", started("one", 1, mira)))
        await coordinator.feed_line(lines.line("audio.segment.started", started("two", 2, drex)))
        await coordinator.feed_line(lines.line("audio.segment.chunk", chunk("one", 1, 1, bytes(3_200))))
        # The violation: a single chunk past the per-chunk safety bound.
        await coordinator.feed_line(
            lines.line("audio.segment.chunk", chunk("one", 1, 2, bytes(8 * 1024 + 2)))
        )
        # The dropped segment is gone and its bytes are released.
        self.assertNotIn("one", coordinator.assembler.active)
        # Its stream keeps arriving; every frame is swallowed silently.
        await coordinator.feed_line(lines.line("audio.segment.chunk", chunk("one", 1, 3, bytes(3_200))))
        await coordinator.feed_line(
            lines.line("audio.segment.ended", ended("one", 1, mira, 3, 9_600))
        )
        self.assertEqual({}, coordinator.assembler.dropped_segments)

        # The OTHER speaker never noticed.
        await coordinator.feed_line(lines.line("audio.segment.chunk", chunk("two", 2, 1, bytes(6_400))))
        await coordinator.feed_line(
            lines.line("audio.segment.ended", ended("two", 2, drex, 1, 6_400))
        )
        await coordinator.drain()
        self.assertEqual(1, len(stt.segments))
        self.assertEqual(0, coordinator.assembler.total_pcm_bytes)
        await coordinator.shutdown()

    async def test_lifecycle_payload_ids_never_reach_overlay_event(self):
        lines = EventLines()
        sink = FakeSink()
        coordinator = VoiceCaptionCoordinator(ConcurrentFakeSTT(), sink)
        await coordinator.feed_line(
            lines.line(
                "session.started",
                {
                    "userId": "111111111111111111",
                    "session": {"phase": "awaiting_consent"},
                },
            )
        )
        self.assertEqual([SessionEvent("paused")], sink.events)
        self.assertNotIn("111111111111111111", repr(sink.events))
        await coordinator.shutdown()

    async def test_voice_disconnect_does_not_overwrite_stopped_state(self):
        lines = EventLines()
        sink = FakeSink()
        coordinator = VoiceCaptionCoordinator(ConcurrentFakeSTT(), sink)
        await coordinator.feed_line(
            lines.line("session.stopped", {"session": {"phase": "stopped"}})
        )
        await coordinator.feed_line(
            lines.line("voice.disconnected", {"guildId": "111111111111111111"})
        )
        self.assertEqual([SessionEvent("stopped")], sink.events)
        await coordinator.shutdown()

    async def test_voice_reconnect_events_do_not_overwrite_session_state(self):
        lines = EventLines()
        sink = FakeSink()
        instance = VoiceCaptionCoordinator(ConcurrentFakeSTT(), sink)
        await instance.feed_line(
            lines.line("session.started", {"session": {"phase": "active"}})
        )
        await instance.feed_line(lines.line("voice.reconnecting", {"guildId": "1"}))
        await instance.feed_line(lines.line("voice.reconnected", {"guildId": "1"}))
        self.assertEqual([SessionEvent("active")], sink.events)
        await instance.shutdown()

    async def test_consent_withdrawal_without_a_session_keeps_the_pipeline_alive(self):
        lines = EventLines()
        sink = FakeSink()
        stt = ConcurrentFakeSTT()
        coordinator = VoiceCaptionCoordinator(stt, sink)
        await coordinator.feed_line(lines.line("consent.declined", {"session": None}))
        await coordinator.feed_line(lines.line("consent.forgotten", {"session": None}))
        self.assertEqual([], sink.events)
        self.assertFalse(stt.closed)

        await coordinator.feed_line(
            lines.line("consent.forgotten", {"session": {"phase": "awaiting_consent"}})
        )
        self.assertEqual([SessionEvent("paused")], sink.events)
        await coordinator.shutdown()

    async def test_blocked_capture_health_emits_error_then_restarts_safely(self):
        lines = EventLines()
        sink = FakeSink()
        stt = ConcurrentFakeSTT()
        coordinator = VoiceCaptionCoordinator(stt, sink)
        private_marker = "111111111111111111-private-reason"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with self.assertRaises(CoordinatorError) as caught:
                await coordinator.feed_line(
                    lines.line(
                        "health.changed",
                        {
                            "component": "capture_pipeline",
                            "status": "blocked",
                            "reason": private_marker,
                        },
                    )
                )
        self.assertEqual("adapter_failed", caught.exception.code)
        self.assertEqual([SessionEvent("error")], sink.events)
        self.assertNotIn(private_marker, str(caught.exception))
        self.assertNotIn(private_marker, output.getvalue())
        self.assertTrue(stt.closed)

    async def test_expected_startup_block_waits_for_adapter_ready(self):
        lines = EventLines()
        sink = FakeSink()
        stt = ConcurrentFakeSTT()
        coordinator = VoiceCaptionCoordinator(stt, sink)

        await coordinator.feed_line(lines.line("adapter.starting", {}))
        await coordinator.feed_line(
            lines.line(
                "health.changed",
                {
                    "component": "capture_pipeline",
                    "status": "blocked",
                    "reason": "startup",
                },
            )
        )
        self.assertEqual([], sink.events)
        self.assertFalse(stt.closed)

        await coordinator.feed_line(lines.line("adapter.ready", {}))
        self.assertEqual([SessionEvent("ready")], sink.events)
        await coordinator.shutdown()

    async def test_startup_block_after_ready_fails_closed(self):
        lines = EventLines()
        sink = FakeSink()
        stt = ConcurrentFakeSTT()
        coordinator = VoiceCaptionCoordinator(stt, sink)
        await coordinator.feed_line(lines.line("adapter.ready", {}))

        with self.assertRaises(CoordinatorError) as caught:
            await coordinator.feed_line(
                lines.line(
                    "health.changed",
                    {
                        "component": "capture_pipeline",
                        "status": "blocked",
                        "reason": "startup",
                    },
                )
            )
        self.assertEqual("invalid_health", caught.exception.code)
        self.assertTrue(stt.closed)

    async def test_completed_task_cap_wipes_burst_and_fails_closed(self):
        lines = EventLines()
        stt = ConcurrentFakeSTT(hold=True)
        coordinator = VoiceCaptionCoordinator(
            stt, FakeSink(), max_transcription_tasks=2
        )
        identities = [
            speaker("111111111111111111", "One"),
            speaker("222222222222222222", "Two"),
            speaker("333333333333333333", "Three"),
        ]
        for index, identity in enumerate(identities, start=1):
            segment_id = f"segment-{index}"
            await coordinator.feed_line(
                lines.line(
                    "audio.segment.started",
                    started(segment_id, index, identity),
                )
            )
            await coordinator.feed_line(
                lines.line(
                    "audio.segment.chunk",
                    chunk(segment_id, index, 1, bytes([index]) * 3_200),
                )
            )
            if index < 3:
                await coordinator.feed_line(
                    lines.line(
                        "audio.segment.ended",
                        ended(segment_id, index, identity, 1, 3_200),
                    )
                )
                continue
            with self.assertRaises(CoordinatorError) as caught:
                await coordinator.feed_line(
                    lines.line(
                        "audio.segment.ended",
                        ended(segment_id, index, identity, 1, 3_200),
                    )
                )
        self.assertEqual("transcription_task_limit", caught.exception.code)
        self.assertTrue(stt.closed)
        self.assertEqual({}, coordinator.assembler.active)
        self.assertEqual(0, coordinator.assembler.total_pcm_bytes)
        self.assertTrue(all(audio.wiped for audio in stt.segments))

    async def test_cancel_before_task_first_timeslice_still_wipes_completed_pcm(self):
        stt = ConcurrentFakeSTT(hold=True)
        coordinator = VoiceCaptionCoordinator(stt, FakeSink())
        completed = CompletedSegment(
            global_sequence=1,
            audience_generation=1,
            audience_revision=1,
            speaker=SpeakerIdentity("111111111111111111", "Mira"),
            pcm=bytearray(b"private-voice" * 300),
        )
        coordinator._schedule_transcription(completed)
        await coordinator.shutdown()
        self.assertEqual(bytearray(), completed.pcm)
        self.assertEqual({}, coordinator._segments_by_task)

    async def test_shutdown_cancellation_does_not_cancel_private_cleanup(self):
        stt = SlowCloseSTT()
        coordinator = VoiceCaptionCoordinator(stt, FakeSink())
        shutdown = asyncio.create_task(coordinator.shutdown())
        await asyncio.wait_for(stt.close_started.wait(), 1)
        shutdown.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await shutdown
        self.assertFalse(stt.closed)

        stt.finish_close.set()
        await asyncio.wait_for(coordinator.shutdown(), 1)
        self.assertTrue(stt.closed)

    async def test_system_exit_from_sink_is_not_swallowed(self):
        def exit_sink(_event):
            raise SystemExit(4)

        coordinator = VoiceCaptionCoordinator(ConcurrentFakeSTT(), exit_sink)
        with self.assertRaises(SystemExit) as caught:
            coordinator._emit(SessionEvent("ready"))
        self.assertEqual(4, caught.exception.code)
        await coordinator.shutdown()

    async def test_hostile_unhashable_values_fail_closed_and_clean_up(self):
        cases = []
        invalid_character = speaker("111111111111111111", "Mira", mapped=True)
        invalid_character["character"]["wowClass"] = []
        cases.append(
            (
                "audio.segment.started",
                started("bad-character", 1, invalid_character),
            )
        )
        cases.append(("session.started", {"session": {"phase": []}}))

        for event_type, payload in cases:
            with self.subTest(event_type=event_type):
                stt = ConcurrentFakeSTT()
                coordinator = VoiceCaptionCoordinator(stt, FakeSink())
                with self.assertRaises(CoordinatorError):
                    await coordinator.feed_line(EventLines().line(event_type, payload))
                self.assertTrue(stt.closed)
                self.assertEqual(0, coordinator.assembler.total_pcm_bytes)

        lines = EventLines()
        identity = speaker("111111111111111111", "Mira")
        stt = ConcurrentFakeSTT()
        coordinator = VoiceCaptionCoordinator(stt, FakeSink())
        await coordinator.feed_line(
            lines.line("audio.segment.started", started("bad-status", 1, identity))
        )
        invalid_end = ended("bad-status", 1, identity, 0, 0)
        invalid_end["status"] = []
        with self.assertRaises(CoordinatorError):
            await coordinator.feed_line(
                lines.line("audio.segment.ended", invalid_end)
            )
        self.assertTrue(stt.closed)

    async def test_audio_format_rejects_bool_and_float_number_lookalikes(self):
        identity = speaker("111111111111111111", "Mira")
        for field, value in (("channels", True), ("sampleRateHz", 16_000.0)):
            with self.subTest(field=field):
                payload = started("invalid-format", 1, identity)
                payload["format"][field] = value
                stt = ConcurrentFakeSTT()
                coordinator = VoiceCaptionCoordinator(stt, FakeSink())
                with self.assertRaises(CoordinatorError) as caught:
                    await coordinator.feed_line(
                        EventLines().line("audio.segment.started", payload)
                    )
                self.assertEqual("invalid_audio_format", caught.exception.code)
                self.assertTrue(stt.closed)

    async def test_escaped_surrogates_never_reach_overlay_metadata(self):
        invalid_speakers = [
            speaker("111111111111111111", "\ud800"),
            speaker("111111111111111111", "Mira", mapped=True),
        ]
        invalid_speakers[1]["character"]["characterName"] = "\udfff"
        for identity in invalid_speakers:
            stt = ConcurrentFakeSTT()
            coordinator = VoiceCaptionCoordinator(stt, FakeSink())
            with self.assertRaises(CoordinatorError):
                await coordinator.feed_line(
                    EventLines().line(
                        "audio.segment.started", started("surrogate", 1, identity)
                    )
                )
            self.assertTrue(stt.closed)


class CaptureLoopTests(unittest.IsolatedAsyncioTestCase):
    async def _wait_for_pcm(self, holder):
        while not holder or holder["coordinator"].assembler.total_pcm_bytes == 0:
            await asyncio.sleep(0)

    async def test_silent_incomplete_segment_hits_active_deadline_and_wipes(self):
        lines = EventLines()
        identity = speaker("111111111111111111", "Mira")
        pcm = bytes(3_200)
        process = FakeNodeProcess(
            [
                lines.line(
                    "audio.segment.started", started("silent", 1, identity)
                ),
                lines.line(
                    "audio.segment.chunk", chunk("silent", 1, 1, pcm)
                ),
            ]
        )
        now = [0.0]
        holder = {}
        stt = ConcurrentFakeSTT()

        def factory(service, sink):
            coordinator = VoiceCaptionCoordinator(
                service,
                sink,
                max_segment_age_seconds=1,
                segment_clock=lambda: now[0],
            )
            holder["coordinator"] = coordinator
            return coordinator

        async def spawner(*_args):
            return process

        task = asyncio.create_task(
            run_node_capture(
                DiscordCredentials(
                    "123456789012345678", "not-a-real-token." + "x" * 40
                ),
                NodeAdapterCapability(True, "ready", Path("C:/node.exe")),
                stt,
                FakeSink(),
                threading.Event(),
                spawner=spawner,
                coordinator_factory=factory,
            )
        )
        await asyncio.wait_for(self._wait_for_pcm(holder), 1)
        active_pcm = next(iter(holder["coordinator"].assembler.active.values())).pcm
        now[0] = 2.0
        with self.assertRaises(CoordinatorError) as caught:
            await asyncio.wait_for(task, 1)
        self.assertEqual("segment_timeout", caught.exception.code)
        self.assertEqual(bytearray(), active_pcm)
        self.assertTrue(stt.closed)

    async def test_cancellation_during_stt_start_still_closes_service(self):
        stt = BlockingStartSTT()
        spawner_called = False

        async def spawner(*_args):
            nonlocal spawner_called
            spawner_called = True
            return FakeNodeProcess([])

        task = asyncio.create_task(
            run_node_capture(
                DiscordCredentials(
                    "123456789012345678", "not-a-real-token." + "x" * 40
                ),
                NodeAdapterCapability(True, "ready", Path("C:/node.exe")),
                stt,
                FakeSink(),
                threading.Event(),
                spawner=spawner,
            )
        )
        await asyncio.wait_for(stt.start_started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(spawner_called)
        self.assertTrue(stt.closed)

    async def test_process_stop_races_cannot_skip_pcm_or_stt_cleanup(self):
        for stop_failure in ("terminate", "kill"):
            with self.subTest(stop_failure=stop_failure):
                lines = EventLines()
                identity = speaker("111111111111111111", "Mira")
                process = FakeNodeProcess(
                    [
                        lines.line(
                            "audio.segment.started", started("pending", 1, identity)
                        ),
                        lines.line(
                            "audio.segment.chunk",
                            chunk("pending", 1, 1, bytes(3_200)),
                        ),
                    ],
                    stop_failure=stop_failure,
                )
                holder = {}
                stop = threading.Event()
                stt = ConcurrentFakeSTT()

                def factory(service, sink):
                    coordinator = VoiceCaptionCoordinator(service, sink)
                    holder["coordinator"] = coordinator
                    return coordinator

                async def spawner(*_args):
                    return process

                task = asyncio.create_task(
                    run_node_capture(
                        DiscordCredentials(
                            "123456789012345678",
                            "not-a-real-token." + "x" * 40,
                        ),
                        NodeAdapterCapability(
                            True, "ready", Path("C:/node.exe")
                        ),
                        stt,
                        FakeSink(),
                        stop,
                        spawner=spawner,
                        coordinator_factory=factory,
                    )
                )
                # Ceilings, not measurements: wait_for returns the moment
                # the work completes, so a wide bound costs nothing when
                # green. 1 second was calibrated to a fast dev box and the
                # 'terminate' variant walks a stop-grace path that a loaded
                # shared CI runner cannot finish inside it.
                await asyncio.wait_for(self._wait_for_pcm(holder), 10)
                active_pcm = next(
                    iter(holder["coordinator"].assembler.active.values())
                ).pcm
                stop.set()
                await asyncio.wait_for(task, 10)
                self.assertEqual(bytearray(), active_pcm)
                self.assertTrue(stt.closed)


class NodeBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def test_child_is_told_where_consent_persists(self):
        """The companion must name the ledger path, or persistence no-ops.

        The adapter builds a memory-only ledger when this is absent, and every
        call site there is an optional-chain, so the failure is silent: the
        group re-consents every session while nothing errors and every test
        stays green. That is exactly how it shipped. An absolute path is
        required because the adapter's working directory is not ours to assume.
        """
        credentials = DiscordCredentials(
            "123456789012345678", "not-a-real-token." + "x" * 40
        )
        environment = build_node_environment(credentials, {"PATH": "C:\\Windows"})
        consent_path = environment.get("CAPTIONS_CONSENT_STATE_PATH", "")
        self.assertTrue(consent_path, "the adapter was never told where consent lives")
        self.assertTrue(
            Path(consent_path).is_absolute(),
            "consent state path must be absolute: " + consent_path,
        )
        self.assertNotIn(
            credentials.bot_token, consent_path, "the path must not carry the token"
        )
        # And the mapping ledger, or /vem map dies with every restart -
        # which under strict focus mode mutes the whole group.
        mapping_path = environment.get("CAPTIONS_MAPPING_STATE_PATH", "")
        self.assertTrue(mapping_path, "the adapter was never told where mappings live")
        self.assertTrue(Path(mapping_path).is_absolute())
        self.assertNotEqual(
            consent_path, mapping_path, "consent and mappings are separate records"
        )

    def test_credentials_exist_only_in_child_environment(self):
        credentials = DiscordCredentials(
            "123456789012345678", "not-a-real-token." + "x" * 40
        )
        environment = build_node_environment(
            credentials,
            {
                "PATH": "C:\\Windows",
                "OTHER_SECRET": "must-not-pass",
                "DISCORD_TOKEN": "old-token-must-not-pass",
            },
        )
        capability = NodeAdapterCapability(
            True,
            "ready",
            Path("C:/Program Files/nodejs/node.exe"),
            Path("C:/VoiceCaptions/discord_adapter"),
        )
        command = node_adapter_command(capability)
        self.assertEqual(credentials.bot_token, environment["DISCORD_TOKEN"])
        self.assertEqual(credentials.application_id, environment["DISCORD_APPLICATION_ID"])
        self.assertNotIn("OTHER_SECRET", environment)
        self.assertNotIn(credentials.bot_token, " ".join(command))
        self.assertNotIn(credentials.application_id, " ".join(command))

    def test_only_valid_absolute_consent_state_path_reaches_node(self):
        credentials = DiscordCredentials(
            "123456789012345678", "not-a-real-token." + "x" * 40
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            consent_path = str(Path(temp_dir).resolve() / "consent.json")
            environment = build_node_environment(
                credentials,
                {"CAPTIONS_CONSENT_STATE_PATH": consent_path},
            )
        self.assertEqual(
            consent_path,
            environment["CAPTIONS_CONSENT_STATE_PATH"],
        )

        for invalid in ("consent.json", consent_path + "\n"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(CoordinatorError):
                    build_node_environment(
                        credentials,
                        {"CAPTIONS_CONSENT_STATE_PATH": invalid},
                    )

    async def test_spawn_uses_private_stdout_and_no_credential_arguments(self):
        credentials = DiscordCredentials(
            "123456789012345678", "not-a-real-token." + "x" * 40
        )
        capability = NodeAdapterCapability(
            True, "ready", Path("C:/node.exe"), Path("C:/adapter")
        )
        captured = {}
        sentinel = object()

        async def create(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = {**kwargs, "env": kwargs["env"].copy()}
            return sentinel

        with patch(
            "VoiceCaptions.coordinator.asyncio.create_subprocess_exec",
            side_effect=create,
        ):
            result = await spawn_node_adapter(capability, credentials)
        self.assertIs(sentinel, result)
        self.assertNotIn(credentials.bot_token, " ".join(captured["args"]))
        self.assertNotIn(credentials.application_id, " ".join(captured["args"]))
        self.assertEqual(credentials.bot_token, captured["kwargs"]["env"]["DISCORD_TOKEN"])
        # stdout stays private: it carries the caption protocol, so it is
        # piped to the coordinator and never inherited into a shared log.
        self.assertEqual(asyncio.subprocess.PIPE, captured["kwargs"]["stdout"])
        # stderr is INHERITED, not discarded. It used to be DEVNULL, which
        # is why an adapter that could not reach Discord looped 530 times
        # in an hour telling the operator only "source is unavailable".
        # The adapter's contract is that credentials and audio never go to
        # stderr - that contract is what makes this safe, and it is
        # asserted on the adapter side, not guessed at here.
        self.assertIsNone(captured["kwargs"]["stderr"])

    def test_node_capability_requires_version_and_all_dependencies(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            node = root / "node.exe"
            node.touch()
            required = (
                root / "src" / "index.js",
                root / "package-lock.json",
                root / "node_modules" / "discord.js" / "package.json",
                root / "node_modules" / "@discordjs" / "voice" / "package.json",
                root / "node_modules" / "@snazzah" / "davey" / "package.json",
                root / "node_modules" / "opusscript" / "package.json",
            )
            unavailable = detect_node_adapter(root, node_path=node, probe=lambda *_: ("v24.18.0", True))
            self.assertFalse(unavailable.available)
            for path in required:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            available = detect_node_adapter(root, node_path=node, probe=lambda *_: ("v24.18.0", True))
            old = detect_node_adapter(root, node_path=node, probe=lambda *_: ("v20.0.0", True))
            required[4].unlink()
            missing_dave = detect_node_adapter(
                root, node_path=node, probe=lambda *_: ("v24.18.0", True)
            )
        self.assertTrue(available.available)
        self.assertFalse(old.available)
        self.assertFalse(missing_dave.available)



class InvitedPairingCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    """The coordinator turns a Discord request into a bound invitation."""

    class Sink(FakeSink):
        def __init__(self):
            super().__init__()
            self.enrollments = []
            self.answer = True

        def enroll_viewer(self, code, viewer_token, ttl_seconds):
            self.enrollments.append((code, viewer_token, ttl_seconds))
            return self.answer

    async def test_the_invitation_carries_a_token_and_never_the_user_id(self):
        lines = EventLines()
        sink = self.Sink()
        coordinator = VoiceCaptionCoordinator(
            ConcurrentFakeSTT(), sink, identity_key=b"test-key" * 4
        )
        code = "ghe_" + "B" * 32
        await coordinator.feed_line(lines.line("viewer.enroll.requested", {
            "userId": "111111111111111111",
            "code": code,
            "ttlSeconds": 1800,
        }))
        self.assertEqual(len(sink.enrollments), 1)
        sent_code, token, ttl = sink.enrollments[0]
        self.assertEqual(sent_code, code)
        self.assertEqual(ttl, 1800)
        # The relay learns a keyed hash, never the Discord id itself.
        self.assertRegex(token, r"^[0-9a-f]{24}$")
        self.assertNotIn("111111111111111111", token)
        # And it is the SAME token the audience is built from, or presence
        # could never match a device to the person who left.
        self.assertEqual(token, coordinator._speaker_token("111111111111111111"))
        await coordinator.shutdown()

    async def test_a_damaged_request_is_refused_rather_than_guessed(self):
        lines = EventLines()
        sink = self.Sink()
        coordinator = VoiceCaptionCoordinator(ConcurrentFakeSTT(), sink)
        for payload in (
            {"userId": "nope", "code": "ghe_" + "B" * 32, "ttlSeconds": 1800},
            {"userId": "111111111111111111", "code": "short", "ttlSeconds": 1800},
            {"userId": "111111111111111111", "code": "ghe_" + "B" * 32, "ttlSeconds": 0},
            {"userId": "111111111111111111", "code": "ghe_" + "B" * 32},
            {
                "userId": "111111111111111111",
                "code": "ghe_" + "B" * 32,
                "ttlSeconds": 1800,
                "guildId": "222222222222222222",
            },
        ):
            with self.assertRaises(CoordinatorError):
                await coordinator.feed_line(lines.line("viewer.enroll.requested", payload))
        self.assertEqual(sink.enrollments, [])
        await coordinator.shutdown()

    async def test_a_relay_that_refuses_is_reported_not_swallowed(self):
        lines = EventLines()
        sink = self.Sink()
        sink.answer = False
        coordinator = VoiceCaptionCoordinator(ConcurrentFakeSTT(), sink)
        with self.assertRaises(CoordinatorError):
            await coordinator.feed_line(lines.line("viewer.enroll.requested", {
                "userId": "111111111111111111",
                "code": "ghe_" + "B" * 32,
                "ttlSeconds": 1800,
            }))
        await coordinator.shutdown()


class PairingAddressTests(unittest.TestCase):
    """/vem setup hands out a link, so it needs the guild's address.

    Shipped 0.4.5 with the adapter reading CAPTIONS_JOIN_BASE_URL and
    nothing setting it, so every invitation said "no public pairing
    address configured". The address is already recorded on the guild
    host, which is the only machine the adapter runs on, so it is read
    from there rather than configured twice.
    """

    def test_an_explicit_https_address_wins(self):
        self.assertEqual(
            public_pairing_address({"CAPTIONS_JOIN_BASE_URL": "https://ears.example/"}),
            "https://ears.example",
        )
        # The relay's own public address is the fallback.
        self.assertEqual(
            public_pairing_address({"VEMALS_EARS_PUBLIC_HTTP_URL": "https://relay.example"}),
            "https://relay.example",
        )

    def test_an_insecure_address_is_never_used(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "GuildHub.relay_env.load", return_value={}
        ):
            for value in ("http://plain.example", "ears.example", "", "   "):
                self.assertEqual(
                    public_pairing_address({"CAPTIONS_JOIN_BASE_URL": value}), ""
                )

    def test_the_guild_host_needs_no_extra_configuration(self):
        with mock.patch(
            "GuildHub.relay_env.load",
            return_value={"VEMALS_EARS_PUBLIC_HTTP_URL": "https://host.example/"},
        ):
            self.assertEqual(public_pairing_address({}), "https://host.example")

    def test_a_member_bundle_without_a_relay_says_nothing(self):
        with mock.patch("GuildHub.relay_env.load", side_effect=OSError("no relay here")):
            self.assertEqual(public_pairing_address({}), "")

    def test_the_adapter_is_actually_given_the_address(self):
        """The bug that shipped: the adapter read a variable nobody set."""
        credentials = DiscordCredentials(
            application_id="123456789012345678",
            bot_token="x" * 59,
        )
        with mock.patch(
            "GuildHub.relay_env.load",
            return_value={"VEMALS_EARS_PUBLIC_HTTP_URL": "https://host.example"},
        ):
            environment = build_node_environment(credentials, {"PATH": "C:/Windows"})
        self.assertEqual(environment["CAPTIONS_JOIN_BASE_URL"], "https://host.example")

    def test_no_address_leaves_the_variable_absent_rather_than_empty(self):
        credentials = DiscordCredentials(
            application_id="123456789012345678",
            bot_token="x" * 59,
        )
        with mock.patch("GuildHub.relay_env.load", return_value={}):
            environment = build_node_environment(credentials, {"PATH": "C:/Windows"})
        self.assertNotIn("CAPTIONS_JOIN_BASE_URL", environment)


class InvitationWiringTests(unittest.IsolatedAsyncioTestCase):
    """A sink that cannot carry an invitation must say so, not shrug."""

    async def test_a_sink_without_enroll_viewer_fails_loudly(self):
        lines = EventLines()
        coordinator = VoiceCaptionCoordinator(ConcurrentFakeSTT(), FakeSink())
        with self.assertRaises(CoordinatorError) as caught:
            await coordinator.feed_line(lines.line("viewer.enroll.requested", {
                "userId": "111111111111111111",
                "code": "ghe_" + "B" * 32,
                "ttlSeconds": 1800,
            }))
        # The public message stays generic; the reason rides on .code and
        # is what reaches the worker log.
        self.assertEqual(caught.exception.code, "viewer_enrollment_unsupported")
        await coordinator.shutdown()

if __name__ == "__main__":
    unittest.main(verbosity=2)


class TranscriptClippingTests(unittest.TestCase):
    """A long sentence is speech, not a protocol violation.

    The transcript went through the same validator as names from the
    adapter, which RAISES when a field is too long. That is right for a
    name: an oversized one is a fault and failing closed is safe. For a
    transcript it meant one person talking for a while took down the
    capture pipeline - the bot left mid-sentence and every member lost
    captions.
    """

    def test_a_long_sentence_is_clipped_not_refused(self):
        spoken = " ".join(["everyone stack behind the pillar"] * 40)
        self.assertGreater(len(spoken), 320)
        clipped = coordinator._safe_transcript(spoken, 320, "invalid_transcript")
        self.assertLessEqual(len(clipped), 320)
        self.assertTrue(spoken.startswith(clipped.rstrip()))

    def test_it_clips_on_a_word_boundary_when_one_is_near(self):
        clipped = coordinator._safe_transcript(
            " ".join(["alpha"] * 200), 320, "invalid_transcript"
        )
        self.assertFalse(clipped.endswith("alph"))
        self.assertTrue(clipped.endswith("alpha"))

    def test_an_ordinary_line_is_untouched(self):
        for text in ("Interrupt now", "Stack on me", "a"):
            self.assertEqual(
                text, coordinator._safe_transcript(text, 320, "bad")
            )

    def test_real_faults_still_fail_closed(self):
        """Only the length rule relaxed; everything else still refuses."""
        for value in (None, 42, b"bytes", "", "   ", "has\u0007control"):
            with self.assertRaises(CoordinatorError, msg=repr(value)):
                coordinator._safe_transcript(value, 320, "invalid_transcript")

    def test_names_from_the_adapter_still_refuse_oversize(self):
        """The strict validator must keep its behaviour: an oversized
        name really is a protocol fault."""
        with self.assertRaises(CoordinatorError):
            coordinator._safe_text("x" * 65, 64, "invalid_speaker")


class FailureReasonTests(unittest.IsolatedAsyncioTestCase):
    """A stopped pipeline says WHY, in one safe word.

    Shino, 2026-09-04: "vemals ears keeps crashing". The worker log held
    twelve "(adapter_failed)" and ten "(transcription_failed)" lines for
    one evening and not one of them named a cause: the transcription
    path discarded its exception with `from None`, and the adapter's own
    reason word never reached the code. Both are carried now - through
    an allowlist, because a component's reason field is not trusted to
    be an enum word (see the private-marker test above, which must keep
    passing).
    """

    async def test_the_adapters_reason_reaches_the_operator(self):
        lines = EventLines()
        stt = ConcurrentFakeSTT()
        instance = VoiceCaptionCoordinator(stt, FakeSink())
        with self.assertRaises(CoordinatorError) as caught:
            await instance.feed_line(
                lines.line("adapter.failed", {"reason": "discord_start_failed"})
            )
        self.assertEqual("adapter_failed:discord_start_failed", caught.exception.code)
        # The member-facing sentence stays generic either way.
        self.assertNotIn("discord", str(caught.exception).lower())

    async def test_a_blocked_pipeline_carries_its_reason_too(self):
        lines = EventLines()
        instance = VoiceCaptionCoordinator(ConcurrentFakeSTT(), FakeSink())
        with self.assertRaises(CoordinatorError) as caught:
            await instance.feed_line(
                lines.line(
                    "health.changed",
                    {
                        "component": "capture_pipeline",
                        "status": "blocked",
                        "reason": "voice_disconnected",
                    },
                )
            )
        self.assertEqual("adapter_failed:voice_disconnected", caught.exception.code)

    async def test_a_reason_that_is_not_an_enum_word_is_dropped(self):
        """Anything but a short lowercase identifier is left out - a
        reason field can carry a user id, a path, or free text."""
        for unsafe in (
            "111111111111111111-private-reason",
            "C:/Users/Shino/secret.txt",
            "Discord said: no",
            "x" * 60,
            "UPPER",
            "",
            None,
            {"nested": "thing"},
        ):
            with self.subTest(reason=str(unsafe)[:24]):
                lines = EventLines()
                instance = VoiceCaptionCoordinator(ConcurrentFakeSTT(), FakeSink())
                with self.assertRaises(CoordinatorError) as caught:
                    await instance.feed_line(
                        lines.line("adapter.failed", {"reason": unsafe})
                    )
                self.assertEqual("adapter_failed", caught.exception.code)

    async def test_the_speech_backend_going_away_names_itself(self):
        """The real case behind the evening's ten failures: the whisper
        backend is not a per-segment fault, so it stops the pipeline -
        and now it says which fault stopped it."""

        class MissingBackendSTT(ConcurrentFakeSTT):
            async def transcribe(self, audio):
                raise BackendUnavailable("whisper-server is not answering")

        lines = EventLines()
        stt = MissingBackendSTT()
        instance = VoiceCaptionCoordinator(stt, FakeSink())
        identity = speaker("111111111111111111", "Mira")
        pcm = bytes(3_200)
        await instance.feed_line(
            lines.line("audio.segment.started", started("one", 1, identity))
        )
        await instance.feed_line(
            lines.line("audio.segment.chunk", chunk("one", 1, 1, pcm))
        )
        await instance.feed_line(
            lines.line("audio.segment.ended", ended("one", 1, identity, 1, len(pcm)))
        )
        with self.assertRaises(CoordinatorError) as caught:
            await instance.drain()
        self.assertEqual(
            "transcription_failed:BackendUnavailable", caught.exception.code
        )
        # The backend's own words - which can name a host or a path -
        # never travel with it.
        self.assertNotIn("whisper-server", str(caught.exception))
        self.assertNotIn("whisper-server", caught.exception.code)
        await instance.shutdown()

    def test_only_our_own_identifiers_are_carried(self):
        self.assertEqual("adapter_failed", coordinator._failure_code("adapter_failed", ""))
        self.assertEqual(
            "transcription_failed:BackendUnavailable",
            coordinator._failure_code(
                "transcription_failed", coordinator._fault_word(BackendUnavailable("x"))
            ),
        )
        # A class name is ours; a reason field is not, and they are
        # judged by different patterns on purpose.
        self.assertEqual("", coordinator._reason_word({"reason": "Startup"}))
        self.assertEqual("startup", coordinator._reason_word({"reason": "startup"}))
        self.assertEqual("", coordinator._reason_word("not a mapping"))


class VoiceStateReportingTests(unittest.IsolatedAsyncioTestCase):
    """The bot leaving and rejoining the channel is now on the record.

    Shino, 2026-09-04: "it keeps logging in and out of channels and
    causing disruptions". These envelopes deliberately produce no SESSION
    event - a disconnect commonly follows session.stopped and must not
    overwrite it - which meant the single most visible thing the bot does
    never reached the log at all.
    """

    async def test_each_voice_change_is_reported_without_touching_state(self):
        seen = []
        lines = EventLines()
        sink = FakeSink()
        instance = VoiceCaptionCoordinator(
            ConcurrentFakeSTT(), sink, on_voice_state=seen.append
        )
        await instance.feed_line(
            lines.line("session.started", {"session": {"phase": "active"}})
        )
        for kind in ("voice.disconnected", "voice.reconnecting", "voice.reconnected"):
            await instance.feed_line(lines.line(kind, {"guildId": "1"}))
        self.assertEqual(
            ["voice.disconnected", "voice.reconnecting", "voice.reconnected"], seen
        )
        # Still exactly one session event: the reconnect did not restate
        # or overwrite the session's own state.
        self.assertEqual([SessionEvent("active")], sink.events)
        await instance.shutdown()

    async def test_the_hook_is_found_on_the_sink_like_the_segment_one(self):
        seen = []
        sink = FakeSink()
        sink.voice_state = seen.append
        lines = EventLines()
        instance = VoiceCaptionCoordinator(ConcurrentFakeSTT(), sink)
        await instance.feed_line(lines.line("voice.disconnected", {"guildId": "1"}))
        self.assertEqual(["voice.disconnected"], seen)
        await instance.shutdown()

    async def test_a_reporting_hook_that_raises_never_costs_the_session(self):
        def explode(_kind):
            raise RuntimeError("no")

        lines = EventLines()
        sink = FakeSink()
        instance = VoiceCaptionCoordinator(
            ConcurrentFakeSTT(), sink, on_voice_state=explode
        )
        await instance.feed_line(lines.line("voice.disconnected", {"guildId": "1"}))
        await instance.feed_line(
            lines.line("session.started", {"session": {"phase": "active"}})
        )
        self.assertEqual([SessionEvent("active")], sink.events)
        await instance.shutdown()

    async def test_no_hook_at_all_is_simply_quiet(self):
        lines = EventLines()
        instance = VoiceCaptionCoordinator(ConcurrentFakeSTT(), FakeSink())
        await instance.feed_line(lines.line("voice.reconnected", {"guildId": "1"}))
        await instance.shutdown()


class InnerFailureCodeTests(unittest.IsolatedAsyncioTestCase):
    """Our own error keeps its own word.

    The live worker log read "transcription_failed:CoordinatorError" -
    the transcription catch-all swallowed a CoordinatorError (it is a
    RuntimeError) and relabelled it with the name of its own wrapper,
    throwing away the one word that said what actually broke.
    """

    async def feed_one_segment(self, sink, stt):
        lines = EventLines()
        instance = VoiceCaptionCoordinator(stt, sink)
        identity = speaker("111111111111111111", "Mira")
        pcm = bytes(3_200)
        await instance.feed_line(
            lines.line("audio.segment.started", started("one", 1, identity))
        )
        await instance.feed_line(
            lines.line("audio.segment.chunk", chunk("one", 1, 1, pcm))
        )
        await instance.feed_line(
            lines.line("audio.segment.ended", ended("one", 1, identity, 1, len(pcm)))
        )
        return instance

    async def test_an_overlay_that_will_not_take_a_caption_says_so(self):
        """The real case: the overlay is gone, so nothing can be shown -
        and the log used to blame transcription for it."""

        class DeadSink(FakeSink):
            def __call__(self, event):
                super().__call__(event)
                return False

        instance = await self.feed_one_segment(DeadSink(), ConcurrentFakeSTT())
        with self.assertRaises(CoordinatorError) as caught:
            await instance.drain()
        self.assertEqual("overlay_unavailable", caught.exception.code)
        await instance.shutdown()

    async def test_a_transcript_that_is_not_text_costs_one_segment_only(self):
        """One malformed reply from the speech backend is one segment's
        misfortune. It used to end the session, so the bot left the voice
        channel and every member lost captions."""

        class BadTypeSTT(ConcurrentFakeSTT):
            async def transcribe(self, audio):
                return TranscriptResult(audio.speaker_id, audio.segment_id, None, 1)

        dropped = []
        sink = FakeSink()
        sink.segment_dropped = dropped.append
        instance = await self.feed_one_segment(sink, BadTypeSTT())
        await instance.drain()
        self.assertEqual(["invalid_transcript"], dropped)
        self.assertEqual([], [e for e in sink.events if isinstance(e, CaptionEvent)])
        await instance.shutdown()

    async def test_an_empty_transcript_is_nothing_to_say_not_a_fault(self):
        """The assembler's own comment calls an empty last part normal -
        the tail after the final cut was too short to be speech. The
        validator refused it, which is right for a NAME and wrong here,
        and the whole capture pipeline came down on it."""

        class SilentSTT(ConcurrentFakeSTT):
            async def transcribe(self, audio):
                return TranscriptResult(audio.speaker_id, audio.segment_id, "   ", 1)

        sink = FakeSink()
        instance = await self.feed_one_segment(sink, SilentSTT())
        await instance.drain()
        self.assertEqual([], [e for e in sink.events if isinstance(e, CaptionEvent)])
        await instance.shutdown()

    async def test_a_real_caption_still_arrives(self):
        sink = FakeSink()
        instance = await self.feed_one_segment(sink, ConcurrentFakeSTT())
        await instance.drain()
        captions = [e for e in sink.events if isinstance(e, CaptionEvent)]
        self.assertEqual(1, len(captions), sink.events)
        self.assertTrue(captions[0].text)
        await instance.shutdown()

    async def test_control_characters_are_still_refused(self):
        """Everything that was a real fault stays one."""

        class ControlSTT(ConcurrentFakeSTT):
            async def transcribe(self, audio):
                return TranscriptResult(
                    audio.speaker_id, audio.segment_id, "pull\u0007now", 1
                )

        instance = await self.feed_one_segment(FakeSink(), ControlSTT())
        with self.assertRaises(CoordinatorError) as caught:
            await instance.drain()
        self.assertEqual("invalid_transcript", caught.exception.code)
        await instance.shutdown()

    async def test_a_foreign_exception_is_still_wrapped_and_named(self):
        class OddSTT(ConcurrentFakeSTT):
            async def transcribe(self, audio):
                raise ZeroDivisionError("C:/models/whisper.bin is missing")

        instance = await self.feed_one_segment(FakeSink(), OddSTT())
        with self.assertRaises(CoordinatorError) as caught:
            await instance.drain()
        self.assertEqual("transcription_failed:ZeroDivisionError", caught.exception.code)
        self.assertNotIn("whisper.bin", caught.exception.code)
        await instance.shutdown()


class LongUtteranceTests(unittest.IsolatedAsyncioTestCase):
    """An utterance long enough to be cut still adds up at the end.

    Streaming cuts hand the head to speech and shorten the live buffer.
    The end-of-segment check compared the adapter's total against the
    tail alone, so every long sentence ended the session: the bot left
    the voice channel and the log said invalid_segment_size.
    """

    async def speak(self, chunks, *, flush_bytes):
        lines = EventLines()
        sink = FakeSink()
        instance = VoiceCaptionCoordinator(
            ConcurrentFakeSTT(), sink, stream_flush_pcm_bytes=flush_bytes
        )
        identity = speaker("111111111111111111", "Mira")
        pcm = bytes(3_200)
        await instance.feed_line(
            lines.line("audio.segment.started", started("one", 1, identity))
        )
        for index in range(1, chunks + 1):
            await instance.feed_line(
                lines.line("audio.segment.chunk", chunk("one", 1, index, pcm))
            )
        await instance.feed_line(
            lines.line(
                "audio.segment.ended",
                ended("one", 1, identity, chunks, len(pcm) * chunks),
            )
        )
        return instance, sink

    async def test_a_cut_utterance_ends_without_failing_its_own_check(self):
        # Small enough that several chunks trigger cuts.
        instance, sink = await self.speak(12, flush_bytes=16_000)
        await instance.drain()
        captions = [event for event in sink.events if isinstance(event, CaptionEvent)]
        self.assertTrue(captions, "the utterance should have produced captions")
        await instance.shutdown()

    async def test_an_uncut_utterance_is_unaffected(self):
        instance, sink = await self.speak(2, flush_bytes=0)
        await instance.drain()
        self.assertTrue(
            [event for event in sink.events if isinstance(event, CaptionEvent)]
        )
        await instance.shutdown()

    async def test_a_total_that_really_is_wrong_still_fails_closed(self):
        """The check still does its job: this is two components
        disagreeing about what was received, and a caption built on
        incomplete audio would be wrong."""
        lines = EventLines()
        instance = VoiceCaptionCoordinator(
            ConcurrentFakeSTT(), FakeSink(), stream_flush_pcm_bytes=16_000
        )
        identity = speaker("111111111111111111", "Mira")
        pcm = bytes(3_200)
        await instance.feed_line(
            lines.line("audio.segment.started", started("one", 1, identity))
        )
        for index in range(1, 13):
            await instance.feed_line(
                lines.line("audio.segment.chunk", chunk("one", 1, index, pcm))
            )
        with self.assertRaises(CoordinatorError) as caught:
            await instance.feed_line(
                lines.line(
                    "audio.segment.ended",
                    ended("one", 1, identity, 12, len(pcm) * 12 + 1),
                )
            )
        self.assertEqual("invalid_segment_size", caught.exception.code)
        await instance.shutdown()
