import asyncio
import unittest

from VoiceCaptions.stt.contracts import (
    AudioSegment,
    BackendUnavailable,
    SpeechGateUnavailable,
    TranscriptionBusy,
    TranscriptionCancelled,
    TranscriptionTimeout,
)
from VoiceCaptions.stt.fake import FakeSpeechActivityGate, FakeTranscriptionBackend
from VoiceCaptions.stt.service import TranscriptionService


def segment(speaker: str, sequence: int, *, hint: str = "") -> AudioSegment:
    hints = (hint,) if hint else ()
    return AudioSegment(speaker, sequence, bytearray([sequence] * 640), hints=hints)


def service(
    backend: FakeTranscriptionBackend,
    *,
    gate: FakeSpeechActivityGate | None = None,
    **options,
) -> TranscriptionService:
    return TranscriptionService(
        backend,
        speech_gate=gate or FakeSpeechActivityGate(),
        **options,
    )


class TranscriptionServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_multiple_concurrent_speakers_remain_isolated(self):
        backend = FakeTranscriptionBackend(
            {("mira", 1): "Stack on me.", ("drex", 1): "Boss left."},
            hold=True,
        )
        transcription = service(backend, max_concurrent_speakers=2)
        await transcription.start()
        mira = segment("mira", 1, hint="Mira")
        drex = segment("drex", 1, hint="Drex")
        tasks = [
            asyncio.create_task(transcription.transcribe(mira)),
            asyncio.create_task(transcription.transcribe(drex)),
        ]
        try:
            await backend.wait_until_active(2)
            self.assertEqual({("mira", 1), ("drex", 1)}, backend.active)
            backend.release()
            results = await asyncio.gather(*tasks)
        finally:
            backend.release()
            await asyncio.gather(*tasks, return_exceptions=True)
            await transcription.close()
        self.assertEqual(
            {"mira": "Stack on me.", "drex": "Boss left."},
            {result.speaker_id: result.text for result in results},
        )
        self.assertEqual(2, backend.max_active)
        self.assertTrue(mira.wiped)
        self.assertTrue(drex.wiped)
        self.assertIn("Mira", backend.prompts[("mira", 1)])
        self.assertNotIn("Mira", backend.prompts[("drex", 1)])

    async def test_same_speaker_segments_never_overlap(self):
        backend = FakeTranscriptionBackend(
            {("mira", 1): "First", ("mira", 2): "Second"}, hold=True
        )
        transcription = service(backend, max_concurrent_speakers=2)
        await transcription.start()
        first = segment("mira", 1)
        second = segment("mira", 2)
        tasks = [
            asyncio.create_task(transcription.transcribe(first)),
            asyncio.create_task(transcription.transcribe(second)),
        ]
        await backend.wait_until_active(1)
        await asyncio.sleep(0)
        self.assertEqual(1, len(backend.active))
        backend.release()
        results = await asyncio.gather(*tasks)
        await transcription.close()
        self.assertEqual(["First", "Second"], [result.text for result in results])
        self.assertEqual(1, backend.max_active_by_speaker["mira"])

    async def test_one_speaker_backlog_never_starves_another(self):
        backend = FakeTranscriptionBackend(
            {
                ("mira", 1): "First",
                ("mira", 2): "Second",
                ("drex", 1): "Boss left.",
            },
            hold=True,
        )
        transcription = service(backend, max_concurrent_speakers=2)
        await transcription.start()
        first = segment("mira", 1)
        second = segment("mira", 2)
        other = segment("drex", 1)
        tasks = [
            asyncio.create_task(transcription.transcribe(first)),
            asyncio.create_task(transcription.transcribe(second)),
            asyncio.create_task(transcription.transcribe(other)),
        ]
        try:
            await backend.wait_until_active(2)
            self.assertEqual({("mira", 1), ("drex", 1)}, backend.active)
            backend.release()
            results = await asyncio.gather(*tasks)
        finally:
            backend.release()
            await asyncio.gather(*tasks, return_exceptions=True)
            await transcription.close()
        self.assertEqual(
            {"First", "Second", "Boss left."},
            {result.text for result in results},
        )
        self.assertEqual(1, backend.max_active_by_speaker["mira"])

    async def test_explicit_cancel_wipes_audio(self):
        backend = FakeTranscriptionBackend({("mira", 1): "Never"}, hold=True)
        transcription = service(backend)
        await transcription.start()
        audio = segment("mira", 1)
        task = asyncio.create_task(transcription.transcribe(audio))
        await backend.wait_until_active(1)
        self.assertTrue(transcription.cancel("mira", 1))
        with self.assertRaises(TranscriptionCancelled):
            await task
        self.assertTrue(audio.wiped)
        self.assertFalse(transcription.cancel("mira", 1))
        await transcription.close()

    async def test_timeout_includes_inference_and_wipes_audio(self):
        backend = FakeTranscriptionBackend({("mira", 1): "Never"}, hold=True)
        transcription = service(backend)
        await transcription.start()
        audio = segment("mira", 1)
        with self.assertRaises(TranscriptionTimeout):
            await transcription.transcribe(audio, timeout_seconds=0.01)
        self.assertTrue(audio.wiped)
        await transcription.close()

    async def test_caller_task_cancellation_wipes_audio(self):
        backend = FakeTranscriptionBackend({("mira", 1): "Never"}, hold=True)
        transcription = service(backend)
        await transcription.start()
        audio = segment("mira", 1)
        task = asyncio.create_task(transcription.transcribe(audio))
        await backend.wait_until_active(1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(audio.wiped)
        self.assertEqual(set(), backend.active)
        await transcription.close()

    async def test_pending_queue_is_bounded(self):
        backend = FakeTranscriptionBackend({("mira", 1): "One"}, hold=True)
        transcription = service(
            backend, max_concurrent_speakers=1, max_pending_segments=1
        )
        await transcription.start()
        first = segment("mira", 1)
        first_task = asyncio.create_task(transcription.transcribe(first))
        await backend.wait_until_active(1)
        rejected = segment("drex", 1)
        with self.assertRaises(TranscriptionBusy):
            await transcription.transcribe(rejected)
        self.assertTrue(rejected.wiped)
        transcription.cancel("mira", 1)
        await asyncio.gather(first_task, return_exceptions=True)
        await transcription.close()

    async def test_silence_and_noise_are_suppressed_before_backend(self):
        backend = FakeTranscriptionBackend(
            {("mira", 1): "invented silence", ("drex", 1): "invented noise"}
        )
        gate = FakeSpeechActivityGate(
            {("mira", 1): False, ("drex", 1): False}
        )
        transcription = service(backend, gate=gate)
        await transcription.start()
        silence = segment("mira", 1)
        noise = segment("drex", 1)
        try:
            results = await asyncio.gather(
                transcription.transcribe(silence),
                transcription.transcribe(noise),
            )
        finally:
            await transcription.close()
        self.assertEqual(["", ""], [result.text for result in results])
        self.assertEqual([], backend.calls)
        self.assertEqual({("mira", 1), ("drex", 1)}, set(gate.calls))
        self.assertTrue(silence.wiped)
        self.assertTrue(noise.wiped)

    async def test_detected_speech_reaches_backend(self):
        backend = FakeTranscriptionBackend({("mira", 1): "Stack now."})
        gate = FakeSpeechActivityGate({("mira", 1): True})
        transcription = service(backend, gate=gate)
        await transcription.start()
        speech = segment("mira", 1)
        try:
            result = await transcription.transcribe(speech)
        finally:
            await transcription.close()
        self.assertEqual("Stack now.", result.text)
        self.assertEqual(1, len(backend.calls))
        self.assertTrue(speech.wiped)

    async def test_gate_failure_fails_closed_and_wipes_audio(self):
        backend = FakeTranscriptionBackend({("mira", 1): "Must not run"})
        gate = FakeSpeechActivityGate(failure=RuntimeError("native failure"))
        transcription = service(backend, gate=gate)
        await transcription.start()
        audio = segment("mira", 1)
        try:
            with self.assertRaises(SpeechGateUnavailable):
                await transcription.transcribe(audio)
        finally:
            await transcription.close()
        self.assertEqual([], backend.calls)
        self.assertTrue(audio.wiped)

    async def test_unavailable_gate_prevents_service_start(self):
        backend = FakeTranscriptionBackend()
        transcription = service(
            backend,
            gate=FakeSpeechActivityGate(available=False),
        )
        with self.assertRaises(SpeechGateUnavailable):
            await transcription.start()
        audio = segment("mira", 1)
        with self.assertRaises(BackendUnavailable):
            await transcription.transcribe(audio)
        self.assertTrue(audio.wiped)
        self.assertEqual([], backend.calls)
        await transcription.close()

    async def test_cancel_while_waiting_for_gate_never_calls_backend(self):
        backend = FakeTranscriptionBackend({("mira", 1): "Must not run"})
        gate = FakeSpeechActivityGate(hold=True)
        transcription = service(backend, gate=gate)
        await transcription.start()
        audio = segment("mira", 1)
        task = asyncio.create_task(transcription.transcribe(audio))
        await gate.wait_until_active(1)
        self.assertTrue(transcription.cancel("mira", 1))
        with self.assertRaises(TranscriptionCancelled):
            await task
        self.assertEqual([], backend.calls)
        self.assertTrue(audio.wiped)
        await transcription.close()

    async def test_gate_preserves_two_speaker_concurrency(self):
        backend = FakeTranscriptionBackend(
            {("mira", 1): "One", ("drex", 1): "Two"}
        )
        gate = FakeSpeechActivityGate(hold=True)
        transcription = service(
            backend,
            gate=gate,
            max_concurrent_speakers=2,
        )
        await transcription.start()
        first = segment("mira", 1)
        second = segment("drex", 1)
        tasks = [
            asyncio.create_task(transcription.transcribe(first)),
            asyncio.create_task(transcription.transcribe(second)),
        ]
        try:
            await gate.wait_until_active(2)
            self.assertEqual(2, gate.max_active)
            gate.release()
            results = await asyncio.gather(*tasks)
        finally:
            gate.release()
            await asyncio.gather(*tasks, return_exceptions=True)
            await transcription.close()
        self.assertEqual({"One", "Two"}, {result.text for result in results})


if __name__ == "__main__":
    unittest.main(verbosity=2)
