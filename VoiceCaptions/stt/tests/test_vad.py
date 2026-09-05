import asyncio
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from VoiceCaptions.stt.contracts import (
    AudioSegment,
    CancellationToken,
    SpeechGateUnavailable,
    TranscriptionCancelled,
)
from VoiceCaptions.stt.vad import (
    MIN_VAD_MODEL_BYTES,
    WhisperCppVadGate,
    WhisperVadConfig,
    _WhisperVadContextParams,
    _WhisperVadParams,
)
import VoiceCaptions.stt.vad as vad_module


class FakeFunction:
    def __init__(self, implementation):
        self.implementation = implementation
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.implementation(*args)


class FakeVadLibrary:
    def __init__(
        self,
        *,
        fail_load=False,
        fail_load_after=None,
        fail_detect=False,
        hold=False,
        hold_init_after=None,
        backend_devices=1,
    ):
        self.fail_load = fail_load
        self.fail_load_after = fail_load_after
        self.fail_detect = fail_detect
        self.hold = hold
        self.hold_init_after = hold_init_after
        self.backend_devices = backend_devices
        self.release = threading.Event()
        if not hold:
            self.release.set()
        self.init_entered = threading.Event()
        self.init_release = threading.Event()
        if hold_init_after is None:
            self.init_release.set()
        self.lock = threading.Lock()
        self.next_context = 100
        self.initialized_contexts = []
        self.initializing = 0
        self.concurrent_free = False
        self.active = 0
        self.max_active = 0
        self.detected_samples = []
        self.segment_counts = {}
        self.freed_contexts = []
        self.freed_segments = []
        self.last_params = []
        self.backend_load_calls = 0
        self.backend_load_paths = []

        self.ggml_backend_load_all_from_path = FakeFunction(self._load_backends)
        self.ggml_backend_dev_count = FakeFunction(lambda: self.backend_devices)
        self.ggml_log_set = FakeFunction(lambda _callback, _user_data: None)
        self.whisper_log_set = FakeFunction(lambda _callback, _user_data: None)
        self.whisper_vad_default_context_params = FakeFunction(
            lambda: _WhisperVadContextParams(2, False, 0)
        )
        self.whisper_vad_default_params = FakeFunction(
            lambda: _WhisperVadParams(0.5, 250, 100, 30.0, 30, 0.1)
        )
        self.whisper_vad_init_from_file_with_params = FakeFunction(
            self._initialize
        )
        self.whisper_vad_detect_speech = FakeFunction(self._detect)
        self.whisper_vad_segments_from_probs = FakeFunction(self._segments)
        self.whisper_vad_segments_n_segments = FakeFunction(
            lambda segments: self.segment_counts[int(segments)]
        )
        self.whisper_vad_free_segments = FakeFunction(
            lambda segments: self.freed_segments.append(int(segments))
        )
        self.whisper_vad_free = FakeFunction(
            self._free_context
        )

    def _load_backends(self, path):
        self.backend_load_calls += 1
        self.backend_load_paths.append(path)

    def _initialize(self, _model, _params):
        if self.fail_load or (
            self.fail_load_after is not None
            and len(self.initialized_contexts) >= self.fail_load_after
        ):
            return 0
        if (
            self.hold_init_after is not None
            and len(self.initialized_contexts) >= self.hold_init_after
        ):
            with self.lock:
                self.initializing += 1
                self.init_entered.set()
            try:
                self.init_release.wait(2)
            finally:
                with self.lock:
                    self.initializing -= 1
        context = self.next_context
        self.next_context += 1
        self.initialized_contexts.append(context)
        return context

    def _free_context(self, context):
        with self.lock:
            if self.initializing:
                self.concurrent_free = True
            self.freed_contexts.append(context.value)

    def _detect(self, context, samples, count):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            values = [float(samples[index]) for index in range(count)]
            self.detected_samples.append(values)
            self.segment_counts[context.value] = int(
                max((abs(value) for value in values), default=0.0) >= 0.1
            )
            self.release.wait(2)
            return not self.fail_detect
        finally:
            with self.lock:
                self.active -= 1

    def _segments(self, context, params):
        self.last_params.append(
            (
                float(params.threshold),
                int(params.min_speech_duration_ms),
                int(params.min_silence_duration_ms),
            )
        )
        return context.value


def pcm(sample: int, count: int = 640) -> bytearray:
    return bytearray(struct.pack("<" + "h" * count, *([sample] * count)))


class WhisperCppVadGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.library_path = root / "whisper.dll"
        self.backend_library_path = root / "ggml.dll"
        self.base_library_path = root / "ggml-base.dll"
        self.model_path = root / "ggml-silero-v6.2.0.bin"
        self.library_path.write_bytes(b"fake")
        self.backend_library_path.write_bytes(b"fake")
        self.base_library_path.write_bytes(b"fake")
        self.model_path.write_bytes(b"v" * MIN_VAD_MODEL_BYTES)

    def tearDown(self):
        self.temporary.cleanup()

    def gate(self, library, *, workers=2):
        return WhisperCppVadGate(
            WhisperVadConfig(
                library_path=self.library_path,
                model_path=self.model_path,
                worker_count=workers,
            ),
            library_loader=lambda _path: library,
        )

    async def test_posix_library_family_is_detected_and_loaded(self):
        install = self.library_path.parent / "whisper-install"
        library_root = install / "lib"
        backend_root = install / "bin"
        library_root.mkdir(parents=True)
        backend_root.mkdir()
        whisper = library_root / "libwhisper.so"
        backend = library_root / "libggml.so"
        base = library_root / "libggml-base.so"
        for path in (whisper, backend, base):
            path.write_bytes(b"fake")
        loaded = []
        library = FakeVadLibrary()

        def load(path):
            loaded.append(Path(path).name)
            return library

        gate = WhisperCppVadGate(
            WhisperVadConfig(
                library_path=whisper,
                model_path=self.model_path,
                worker_count=1,
            ),
            library_loader=load,
        )
        self.assertTrue(gate.capability.available)
        await gate.start()
        await gate.close()
        self.assertEqual(
            ["libggml-base.so", "libggml.so", "libwhisper.so"],
            loaded,
        )
        # resolve() both sides: a CI runner's temp directory aliases through
        # a DOS 8.3 short name (RUNNER~1), so the unresolved fixture path and
        # the resolved path the loader records are different spellings of the
        # same directory.
        self.assertEqual(
            [str(backend_root.resolve()).encode()], library.backend_load_paths
        )

    async def test_silence_and_low_noise_return_zero_segments(self):
        library = FakeVadLibrary()
        gate = self.gate(library)
        await gate.start()
        silence = AudioSegment("mira", 1, pcm(0))
        noise = AudioSegment("mira", 2, pcm(100))
        try:
            self.assertFalse(
                await gate.has_speech(
                    silence,
                    cancellation=CancellationToken(),
                )
            )
            self.assertFalse(
                await gate.has_speech(
                    noise,
                    cancellation=CancellationToken(),
                )
            )
        finally:
            silence.wipe()
            noise.wipe()
            await gate.close()
        self.assertEqual(2, len(library.detected_samples))
        self.assertEqual(2, len(library.freed_segments))

    async def test_injected_speech_segment_is_allowed(self):
        library = FakeVadLibrary()
        gate = self.gate(library)
        await gate.start()
        speech = AudioSegment("mira", 1, pcm(8_000))
        try:
            self.assertTrue(
                await gate.has_speech(
                    speech,
                    cancellation=CancellationToken(),
                )
            )
        finally:
            speech.wipe()
            await gate.close()
        self.assertEqual([(200, 300)], [params[1:] for params in library.last_params])
        self.assertAlmostEqual(0.7, library.last_params[0][0], places=6)

    async def test_native_inference_failure_is_closed_and_float_copy_is_wiped(self):
        library = FakeVadLibrary(fail_detect=True)
        gate = self.gate(library, workers=1)
        await gate.start()
        speech = AudioSegment("mira", 1, pcm(8_000))
        wiped = []
        real_wipe = vad_module._wipe_float_buffer

        def tracked_wipe(samples):
            real_wipe(samples)
            wiped.append(all(float(sample) == 0.0 for sample in samples))

        try:
            with patch(
                "VoiceCaptions.stt.vad._wipe_float_buffer",
                side_effect=tracked_wipe,
            ):
                with self.assertRaises(SpeechGateUnavailable):
                    await gate.has_speech(
                        speech,
                        cancellation=CancellationToken(),
                    )
        finally:
            speech.wipe()
            await gate.close()
        self.assertEqual([True], wiped)
        self.assertEqual([], library.freed_segments)

    async def test_model_load_failure_releases_partial_state_and_fails_closed(self):
        library = FakeVadLibrary(fail_load_after=1)
        gate = self.gate(library)
        with self.assertRaises(SpeechGateUnavailable):
            await gate.start()
        self.assertFalse(gate._started)
        self.assertEqual([], gate._all_contexts)
        self.assertEqual([100], library.freed_contexts)
        await gate.close()

    async def test_missing_backend_device_fails_before_model_context_load(self):
        library = FakeVadLibrary(backend_devices=0)
        gate = self.gate(library)
        with self.assertRaises(SpeechGateUnavailable):
            await gate.start()
        self.assertEqual([], library.initialized_contexts)
        self.assertEqual([], library.freed_contexts)
        await gate.close()

    async def test_close_releases_every_context_and_is_idempotent(self):
        library = FakeVadLibrary()
        gate = self.gate(library, workers=2)
        await gate.start()
        await gate.close()
        await gate.close()
        self.assertEqual([100, 101], library.freed_contexts)

    async def test_two_calls_use_distinct_native_contexts_concurrently(self):
        library = FakeVadLibrary(hold=True)
        gate = self.gate(library, workers=2)
        await gate.start()
        first = AudioSegment("mira", 1, pcm(8_000))
        second = AudioSegment("drex", 1, pcm(8_000))
        tasks = [
            asyncio.create_task(
                gate.has_speech(first, cancellation=CancellationToken())
            ),
            asyncio.create_task(
                gate.has_speech(second, cancellation=CancellationToken())
            ),
        ]
        try:
            for _ in range(100):
                if library.max_active == 2:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(2, library.max_active)
            library.release.set()
            self.assertEqual([True, True], await asyncio.gather(*tasks))
        finally:
            library.release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            first.wipe()
            second.wipe()
            await gate.close()

    async def test_cancel_and_context_ready_race_returns_consumed_context(self):
        library = FakeVadLibrary()
        gate = self.gate(library, workers=1)
        await gate.start()
        assert gate._contexts is not None
        held_context = gate._contexts.get_nowait()
        audio = AudioSegment("mira", 1, pcm(8_000))
        token = CancellationToken()
        task = asyncio.create_task(
            gate.has_speech(audio, cancellation=token)
        )
        await asyncio.sleep(0)
        gate._contexts.put_nowait(held_context)
        token.cancel()
        try:
            with self.assertRaises(TranscriptionCancelled):
                await task
            self.assertEqual(1, gate._contexts.qsize())
        finally:
            audio.wipe()
            await gate.close()

    async def test_caller_cancel_and_context_ready_race_returns_context(self):
        library = FakeVadLibrary()
        gate = self.gate(library, workers=1)
        await gate.start()
        assert gate._contexts is not None
        held_context = gate._contexts.get_nowait()
        audio = AudioSegment("mira", 1, pcm(8_000))
        task = asyncio.create_task(
            gate.has_speech(audio, cancellation=CancellationToken())
        )
        await asyncio.sleep(0)
        gate._contexts.put_nowait(held_context)
        task.cancel()
        try:
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(1, gate._contexts.qsize())
        finally:
            audio.wipe()
            await gate.close()

    async def test_cancel_inside_cleanup_returns_context_and_lets_close_finish(self):
        library = FakeVadLibrary()
        gate = self.gate(library, workers=1)
        await gate.start()
        assert gate._contexts is not None
        audio = AudioSegment("mira", 1, pcm(8_000))

        class CleanupCancellingToken(CancellationToken):
            """Cancel the caller while has_speech awaits its cleanup gather."""

            def __init__(self):
                super().__init__()
                self.operation = None

            async def wait(self):
                try:
                    await super().wait()
                except asyncio.CancelledError:
                    if self.operation is not None:
                        self.operation.cancel()
                    raise

        token = CleanupCancellingToken()
        operation = asyncio.create_task(gate.has_speech(audio, cancellation=token))
        token.operation = operation
        try:
            with self.assertRaises(asyncio.CancelledError):
                await operation
            self.assertEqual(1, gate._contexts.qsize())
            self.assertEqual(0, gate._active_calls)
            await asyncio.wait_for(gate.close(), 1)
            self.assertEqual([100], library.freed_contexts)
        finally:
            await asyncio.gather(operation, return_exceptions=True)
            audio.wipe()
            # close() waits on the idle event, so a regression that leaks the
            # busy count has to fail this test rather than hang the whole run.
            if gate._idle_calls.is_set() and (gate._started or gate._all_contexts):
                await gate.close()

    async def test_close_waits_for_native_call_before_freeing_context(self):
        library = FakeVadLibrary(hold=True)
        gate = self.gate(library, workers=1)
        await gate.start()
        audio = AudioSegment("mira", 1, pcm(8_000))
        operation = asyncio.create_task(
            gate.has_speech(audio, cancellation=CancellationToken())
        )
        for _ in range(100):
            if library.max_active == 1:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(1, library.max_active)
        close = asyncio.create_task(gate.close())
        await asyncio.sleep(0)
        self.assertFalse(close.done())
        self.assertEqual([], library.freed_contexts)
        library.release.set()
        try:
            self.assertTrue(await operation)
            await close
        finally:
            library.release.set()
            await asyncio.gather(operation, close, return_exceptions=True)
            audio.wipe()
        self.assertEqual([100], library.freed_contexts)

    async def test_cancelled_start_and_close_wait_for_native_loader_cleanup(self):
        library = FakeVadLibrary(hold_init_after=1)
        gate = self.gate(library, workers=2)
        startup = asyncio.create_task(gate.start())
        entered = await asyncio.to_thread(library.init_entered.wait, 1)
        self.assertTrue(entered)

        startup.cancel()
        close = asyncio.create_task(gate.close())
        await asyncio.sleep(0)
        self.assertFalse(startup.done())
        self.assertFalse(close.done())
        self.assertEqual([], library.freed_contexts)
        self.assertFalse(library.concurrent_free)

        library.init_release.set()
        try:
            with self.assertRaises(asyncio.CancelledError):
                await startup
            await close
        finally:
            library.init_release.set()
            await asyncio.gather(startup, close, return_exceptions=True)

        self.assertEqual([100, 101], library.initialized_contexts)
        self.assertEqual([100, 101], sorted(library.freed_contexts))
        self.assertEqual(2, len(library.freed_contexts))
        self.assertFalse(library.concurrent_free)
        self.assertEqual([], gate._all_contexts)

    async def test_cancelled_close_cleans_up_before_restart(self):
        library = FakeVadLibrary(hold=True)
        gate = self.gate(library, workers=1)
        await gate.start()
        first = AudioSegment("mira", 1, pcm(8_000))
        second = AudioSegment("mira", 2, pcm(8_000))
        detection = asyncio.create_task(
            gate.has_speech(first, cancellation=CancellationToken())
        )
        closing = None
        restart = None
        try:
            for _ in range(100):
                if library.max_active == 1:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(1, library.max_active)

            closing = asyncio.create_task(gate.close())
            for _ in range(100):
                if gate._closing:
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(gate._closing)
            closing.cancel()
            restart = asyncio.create_task(gate.start())
            await asyncio.sleep(0)

            self.assertFalse(closing.done())
            self.assertFalse(restart.done())
            self.assertEqual([], library.freed_contexts)
            self.assertEqual(1, gate._active_calls)

            library.release.set()
            self.assertTrue(await asyncio.wait_for(detection, 1))
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(closing), 1)
            await asyncio.wait_for(restart, 1)

            self.assertTrue(gate._started)
            self.assertFalse(gate._closing)
            self.assertEqual(0, gate._active_calls)
            self.assertEqual([100], library.freed_contexts)
            assert gate._contexts is not None
            self.assertEqual(1, gate._contexts.qsize())

            self.assertTrue(
                await gate.has_speech(
                    second,
                    cancellation=CancellationToken(),
                )
            )
            self.assertEqual(0, gate._active_calls)
            self.assertEqual(1, gate._contexts.qsize())
            await asyncio.wait_for(gate.close(), 1)

            self.assertEqual([100, 101], library.initialized_contexts)
            self.assertEqual([100, 101], library.freed_contexts)
            self.assertEqual(2, len(set(library.freed_contexts)))
            self.assertFalse(library.concurrent_free)
        finally:
            library.release.set()
            pending = [task for task in (detection, closing, restart) if task]
            await asyncio.gather(*pending, return_exceptions=True)
            if gate._started or gate._all_contexts:
                await gate.close()
            first.wipe()
            second.wipe()


if __name__ == "__main__":
    unittest.main(verbosity=2)
