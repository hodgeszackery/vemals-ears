import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from VoiceCaptions.stt.contracts import (
    AudioSegment,
    BackendProtocolError,
    CancellationToken,
)
from VoiceCaptions.stt.whisper_cpp import (
    AsyncioLoopbackHttpTransport,
    HttpResponse,
    WhisperCppBackend,
    WhisperCppClient,
    WhisperCppConfig,
    _multipart_request,
    _whisper_server_command,
    detect_whisper_cpp,
)


class FakeHttpTransport:
    def __init__(self, response: HttpResponse | None = None):
        self.response = response or HttpResponse(
            200, json.dumps({"text": "Stack on Mira."}).encode("utf-8")
        )
        self.health_calls: list[int] = []
        self.inference = None

    async def health(self, port: int, max_response_bytes: int) -> HttpResponse:
        self.health_calls.append(port)
        return HttpResponse(200, b'{"status":"ok"}')

    async def infer(
        self,
        port,
        request,
        pcm,
        cancellation,
        max_response_bytes,
    ) -> HttpResponse:
        cancellation.raise_if_cancelled()
        self.inference = {
            "port": port,
            "request": request,
            "pcm_identity": id(pcm),
            "pcm_bytes": len(pcm),
            "limit": max_response_bytes,
        }
        return self.response


class FakeProcess:
    def __init__(self):
        self.stopped = False

    def poll(self):
        return 0 if self.stopped else None

    def terminate(self):
        self.stopped = True

    def kill(self):
        self.stopped = True

    def wait(self, timeout=None):
        self.stopped = True
        return 0


class DyingWriter:
    """A writer that dies mid-upload and keeps every chunk it was handed.

    Retaining the chunks stands in for asyncio's fatal-error handling, which
    stores an exception whose traceback pins the frame that wrote the chunk.
    """

    def __init__(self, fail_on_drain: int):
        self.fail_on_drain = fail_on_drain
        self.chunks: list[object] = []
        self.drains = 0
        self.closed = False

    def write(self, data):
        self.chunks.append(data)

    async def drain(self):
        self.drains += 1
        if self.drains == self.fail_on_drain:
            raise ConnectionResetError("whisper-server died mid-upload")

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None


class WhisperCppCapabilityTests(unittest.TestCase):
    def test_missing_local_assets_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            capability = detect_whisper_cpp(
                WhisperCppConfig(
                    server_path=root / "missing-whisper-server.exe",
                    model_path=root / "missing-model.bin",
                )
            )
        self.assertFalse(capability.available)
        self.assertIn("executable does not exist", capability.detail)
        self.assertIn("model does not exist", capability.detail)


class WhisperCppClientTests(unittest.IsolatedAsyncioTestCase):
    def test_server_command_is_hardened_for_independent_utterances(self):
        command = _whisper_server_command(
            Path("C:/runtime/whisper-server.exe"),
            Path("C:/models/turbo.bin"),
            9123,
            "en",
            1,
        )

        self.assertEqual(
            [
                "C:\\runtime\\whisper-server.exe",
                "--host",
                "127.0.0.1",
                "--port",
                "9123",
                "--model",
                "C:\\models\\turbo.bin",
                "--language",
                "en",
                "--no-timestamps",
                "--no-fallback",
                "--suppress-nst",
            ],
            command,
        )
        self.assertNotIn("--vad", command)
        # At width 1 the flag is omitted rather than passed, so the server
        # keeps its own -1 default: greedy sampling with --best-of 2. Passing
        # --beam-size 1 would select a different decoder path.
        self.assertNotIn("--beam-size", command)

    def test_beam_search_is_requested_when_configured(self):
        """Beam search is the accuracy win the greedy default leaves on the table.

        whisper-server defaults --beam-size to -1. The encoder cost, which
        dominates a 30-second segment, does not change with beam width, and
        large-v3-turbo's decoder is only four layers - so this buys accuracy
        at a smaller latency cost than the same change on full large-v3.
        """
        command = _whisper_server_command(
            Path("C:/runtime/whisper-server.exe"),
            Path("C:/models/turbo.bin"),
            9123,
            "en",
            5,
        )

        self.assertEqual(["--beam-size", "5"], command[-2:])
        # The hardening flags must survive alongside it.
        for flag in ("--no-timestamps", "--no-fallback", "--suppress-nst"):
            self.assertIn(flag, command)

    def test_beam_size_defaults_to_beam_search_and_is_bounded(self):
        """The default must actually be beam search, or the win is unshipped.

        The bound exists because the per-segment deadline is 18 seconds for up
        to 30 seconds of audio, and the same command launches the CPU fallback
        server. An unbounded width would let a config change turn every
        segment on a slow machine into a TranscriptionTimeout.
        """
        self.assertEqual(5, WhisperCppConfig().beam_size)
        self.assertEqual(1, WhisperCppConfig(beam_size=1).beam_size)
        for bad in (0, -1, 9):
            with self.assertRaises(ValueError):
                WhisperCppConfig(beam_size=bad)

    async def test_injected_transport_receives_in_memory_wav(self):
        transport = FakeHttpTransport()
        client = WhisperCppClient(transport)
        audio = AudioSegment("mira", 7, bytearray([3] * 640))
        result = await client.transcribe(
            9123,
            audio,
            prompt="World of Warcraft terms: Mira, Bloodlust.",
            language="en",
            cancellation=CancellationToken(),
        )
        self.assertEqual("Stack on Mira.", result)
        self.assertIsNotNone(transport.inference)
        captured = transport.inference
        request = captured["request"]
        self.assertEqual(id(audio.pcm), captured["pcm_identity"])
        self.assertEqual(640, captured["pcm_bytes"])
        self.assertIn(b"RIFF", request.prefix)
        self.assertIn(b"WAVE", request.prefix)
        self.assertIn(b"Mira, Bloodlust", request.suffix)
        self.assertEqual(
            len(request.prefix) + len(audio.pcm) + len(request.suffix),
            request.content_length,
        )
        self.assertFalse(audio.wiped)
        audio.wipe()

    async def test_full_backend_uses_injected_transport_without_server_or_model(self):
        transport = FakeHttpTransport()
        launched = []

        def launch(executable, model, port, language, beam_size):
            process = FakeProcess()
            launched.append((executable, model, port, language, process, beam_size))
            return process

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            executable = root / "whisper-server.exe"
            executable.touch()
            model = root / "ggml-test.bin"
            with model.open("wb") as stream:
                stream.truncate(1_048_576)
            backend = WhisperCppBackend(
                WhisperCppConfig(
                    server_path=executable,
                    model_path=model,
                    worker_count=1,
                ),
                transport=transport,
                process_launcher=launch,
            )
            await backend.start()
            audio = AudioSegment("mira", 2, bytearray(640))
            result = await backend.transcribe(
                audio,
                prompt="World of Warcraft.",
                cancellation=CancellationToken(),
            )
            await backend.close()

        self.assertEqual("Stack on Mira.", result)
        self.assertEqual(1, len(launched))
        self.assertEqual("en", launched[0][3])
        self.assertTrue(launched[0][4].stopped)
        # The configured width has to reach the launcher, not just the
        # command builder. This config does not claim acceleration, so the
        # width must be forced to 1: a CPU server spends most of the
        # 18-second deadline in the encoder, and beam width on top of that
        # turns segments into TranscriptionTimeout on exactly the machines
        # the fallback exists for.
        self.assertEqual(1, launched[0][5])
        self.assertIsNotNone(transport.inference)
        audio.wipe()

    async def test_backend_uses_verified_cpu_server_when_primary_cannot_start(self):
        transport = FakeHttpTransport()
        launched = []

        def launch(executable, model, port, language, beam_size):
            process = FakeProcess()
            # Resolved comparison: the backend hands the launcher a resolved
            # path, and on a CI runner whose temp dir aliases through a DOS
            # 8.3 short name the unresolved fixture path never matches - the
            # primary then never "fails" and the fallback goes untested.
            if Path(executable).resolve() == primary.resolve():
                process.stopped = True
            launched.append((executable, process))
            return process

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            primary = root / "cuda" / "whisper-server.exe"
            fallback = root / "cpu" / "whisper-server.exe"
            primary.parent.mkdir()
            fallback.parent.mkdir()
            primary.touch()
            fallback.touch()
            model = root / "turbo.bin"
            with model.open("wb") as stream:
                stream.truncate(1_048_576)
            backend = WhisperCppBackend(
                WhisperCppConfig(
                    server_path=primary,
                    fallback_server_path=fallback,
                    model_path=model,
                    worker_count=1,
                ),
                transport=transport,
                process_launcher=launch,
            )
            await backend.start()
            await backend.close()

        self.assertEqual([primary.resolve(), fallback.resolve()], [item[0] for item in launched])
        self.assertTrue(launched[-1][1].stopped)

    async def test_health_and_response_validation_are_offline(self):
        transport = FakeHttpTransport(HttpResponse(200, b'{"missing":"text"}'))
        client = WhisperCppClient(transport)
        self.assertTrue(await client.health(8123))
        audio = AudioSegment("drex", 1, bytearray(640))
        with self.assertRaises(BackendProtocolError):
            await client.transcribe(
                8123,
                audio,
                prompt="World of Warcraft.",
                language="en",
                cancellation=CancellationToken(),
            )
        audio.wipe()


class AsyncioLoopbackHttpTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_death_mid_upload_leaves_caller_pcm_wipeable(self):
        audio = AudioSegment("mira", 11, bytearray(196_608))
        request = _multipart_request(audio, "World of Warcraft.", "en")
        writer = DyingWriter(fail_on_drain=3)

        async def fake_open_connection(host, port, limit=None):
            return object(), writer

        transport = AsyncioLoopbackHttpTransport()
        with mock.patch.object(asyncio, "open_connection", fake_open_connection):
            with self.assertRaises(ConnectionResetError):
                await transport.infer(
                    9123,
                    request,
                    audio.pcm,
                    CancellationToken(),
                    4_096,
                )

        self.assertTrue(writer.closed)
        audio.wipe()
        self.assertTrue(audio.wiped)
        self.assertEqual([], [c for c in writer.chunks if isinstance(c, memoryview)])




class BeamAccelerationGateTests(unittest.IsolatedAsyncioTestCase):
    async def _launch_widths(self, *, accelerated, with_fallback):
        transport = FakeHttpTransport()
        launched = []

        def launch(executable, model, port, language, beam_size):
            process = FakeProcess()
            if with_fallback and "primary" in str(executable):
                process.stopped = True
            launched.append((Path(executable).name, beam_size))
            return process

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            primary = root / "primary-whisper-server.exe"
            primary.touch()
            fallback = root / "fallback-whisper-server.exe"
            fallback.touch()
            model = root / "ggml-test.bin"
            with model.open("wb") as stream:
                stream.truncate(1_048_576)
            backend = WhisperCppBackend(
                WhisperCppConfig(
                    server_path=primary,
                    model_path=model,
                    fallback_server_path=fallback if with_fallback else None,
                    worker_count=1,
                    accelerated=accelerated,
                ),
                transport=transport,
                process_launcher=launch,
            )
            await backend.start()
            await backend.close()
        return launched

    async def test_accelerated_primary_gets_the_configured_width(self):
        launched = await self._launch_widths(accelerated=True, with_fallback=False)
        self.assertEqual([("primary-whisper-server.exe", 5)], launched)

    async def test_cpu_fallback_decodes_greedy_even_when_config_is_accelerated(self):
        """The escape hatch this gate exists for.

        A CUDA machine whose primary refuses to start lands on the CPU
        fallback. The configured width was chosen for a 41 ms encoder; the
        fallback's is thirteen seconds, against an 18 second deadline.
        """
        launched = await self._launch_widths(accelerated=True, with_fallback=True)
        self.assertEqual(("primary-whisper-server.exe", 5), launched[0])
        self.assertIn(("fallback-whisper-server.exe", 1), launched)

    async def test_cpu_only_install_decodes_greedy_everywhere(self):
        launched = await self._launch_widths(accelerated=False, with_fallback=False)
        self.assertEqual([("primary-whisper-server.exe", 1)], launched)


if __name__ == "__main__":
    unittest.main(verbosity=2)
