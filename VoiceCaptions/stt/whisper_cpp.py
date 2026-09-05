"""Persistent loopback-only whisper.cpp server backend.

Each worker loads its model once. Requests upload an in-memory WAV to the
official ``/inference`` endpoint; ``--convert`` is deliberately omitted so
whisper.cpp never creates an ffmpeg temporary file. The server is bound only
to 127.0.0.1 and all response bodies are size-bounded.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import socket
import struct
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from .contracts import (
    AudioSegment,
    BackendCapability,
    BackendProtocolError,
    BackendUnavailable,
    CancellationToken,
    TranscriptionCancelled,
)

SERVER_ENV = "VOICE_CAPTIONS_WHISPER_SERVER"
FALLBACK_SERVER_ENV = "VOICE_CAPTIONS_WHISPER_FALLBACK_SERVER"
MODEL_ENV = "VOICE_CAPTIONS_WHISPER_MODEL"
DLL_ENV = "VOICE_CAPTIONS_WHISPER_DLL"
VAD_MODEL_ENV = "VOICE_CAPTIONS_WHISPER_VAD_MODEL"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
MAX_HTTP_HEADER_BYTES = 32_768
DEFAULT_MAX_RESPONSE_BYTES = 262_144
MIN_MODEL_BYTES = 1_048_576

_SECRET_ENV_NAME = re.compile(
    r"(?:authorization|credential|password|secret|token|api[_-]?key)", re.I
)
_LANGUAGE = re.compile(r"^(?:auto|[a-z]{2,3})$")


@dataclass(frozen=True, slots=True)
class WhisperCppConfig:
    server_path: Path | None = None
    model_path: Path | None = None
    fallback_server_path: Path | None = None
    vad_library_path: Path | None = None
    vad_model_path: Path | None = None
    worker_count: int = 2
    language: str = "en"
    # whisper-server defaults --beam-size to -1, which is greedy sampling with
    # --best-of 2. Beam search is the standard accuracy win over greedy and it
    # costs only decoder passes; the encoder, which dominates the time for a
    # 30-second segment, is unaffected. large-v3-turbo has a 4-layer decoder,
    # so the extra cost here is unusually cheap.
    #
    # It is configurable rather than hardcoded because the per-segment deadline
    # is 18 seconds for up to 30 seconds of audio, and the same command builds
    # the CPU fallback server as builds the CUDA one. On a machine slow enough
    # that the fallback is the only path, the headroom to spend may not exist.
    # Set it to 1 to get the previous greedy behaviour back exactly.
    beam_size: int = 5
    # Whether the PRIMARY server runs accelerated. Beam width above 1
    # is applied only there: a CPU server - the fallback, or a CPU-only
    # install's primary - always decodes greedy, because the encoder
    # alone consumes most of the 18-second segment deadline on CPU and
    # a TranscriptionTimeout fails the whole pipeline, on exactly the
    # machines the escape hatch is for.
    accelerated: bool = False
    start_timeout_seconds: float = 45.0
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES

    def __post_init__(self) -> None:
        if self.worker_count < 1 or self.worker_count > 4:
            raise ValueError("worker_count must be between 1 and 4")
        if self.beam_size < 1 or self.beam_size > 8:
            raise ValueError("beam_size must be between 1 and 8")
        if not _LANGUAGE.fullmatch(self.language):
            raise ValueError("language must be auto or a short language code")
        if self.start_timeout_seconds <= 0:
            raise ValueError("start_timeout_seconds must be positive")
        if self.max_response_bytes < 1_024 or self.max_response_bytes > 1_048_576:
            raise ValueError("max_response_bytes is outside the safe range")


def _path_from_setting(explicit: Path | None, env_name: str) -> Path | None:
    if explicit is not None:
        return Path(explicit).expanduser()
    value = os.environ.get(env_name, "").strip()
    return Path(os.path.expandvars(value)).expanduser() if value else None


def detect_whisper_cpp(
    config: WhisperCppConfig | None = None,
) -> BackendCapability:
    """Detect only local assets; never download, build, or start anything."""
    config = config or WhisperCppConfig()
    executable = _path_from_setting(config.server_path, SERVER_ENV)
    if executable is None:
        found = shutil.which("whisper-server") or shutil.which("whisper-server.exe")
        executable = Path(found) if found else None
    model = _path_from_setting(config.model_path, MODEL_ENV)

    gaps: list[str] = []
    if executable is None:
        gaps.append(
            "whisper-server is not on PATH and " + SERVER_ENV + " is not set"
        )
    elif not executable.is_file():
        gaps.append("configured whisper-server executable does not exist")
    if model is None:
        gaps.append(MODEL_ENV + " is not set")
    elif not model.is_file():
        gaps.append("configured whisper.cpp model does not exist")
    else:
        try:
            valid_model = (
                model.suffix.lower() == ".bin"
                and model.stat().st_size >= MIN_MODEL_BYTES
            )
        except OSError:
            valid_model = False
        if not valid_model:
            gaps.append("configured model is not a valid local ggml .bin asset")

    if gaps:
        return BackendCapability(
            backend="whisper.cpp",
            available=False,
            detail="; ".join(gaps),
            executable=executable,
            model=model,
        )
    assert executable is not None and model is not None
    return BackendCapability(
        backend="whisper.cpp",
        available=True,
        detail="local executable and model detected",
        executable=executable.resolve(),
        model=model.resolve(),
    )


class ManagedProcess(Protocol):
    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


ProcessLauncher = Callable[[Path, Path, int, str, int], ManagedProcess]


@dataclass(slots=True)
class _Worker:
    process: ManagedProcess
    port: int


@dataclass(frozen=True, slots=True)
class _MultipartRequest:
    boundary: str
    prefix: bytes
    suffix: bytes
    content_length: int


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: bytes


class WhisperHttpTransport(Protocol):
    """Injected loopback HTTP seam; tests can replace it without a socket."""

    async def health(self, port: int, max_response_bytes: int) -> HttpResponse:
        """Read the local server health endpoint."""

    async def infer(
        self,
        port: int,
        request: _MultipartRequest,
        pcm: bytearray,
        cancellation: CancellationToken,
        max_response_bytes: int,
    ) -> HttpResponse:
        """Post one WAV assembled from metadata plus the owned PCM buffer."""


def _multipart_request(segment: AudioSegment, prompt: str, language: str) -> _MultipartRequest:
    boundary = "VoiceCaptions" + secrets.token_hex(16)
    marker = ("--" + boundary + "\r\n").encode("ascii")
    file_headers = (
        'Content-Disposition: form-data; name="file"; '
        'filename="voice-segment.wav"\r\n'
        "Content-Type: audio/wav\r\n\r\n"
    ).encode("ascii")
    wav_header = _wav_header(len(segment.pcm), segment.sample_rate)
    prefix = marker + file_headers + wav_header

    fields = (
        ("temperature", "0.0"),
        ("response_format", "json"),
        ("language", language),
        ("prompt", prompt),
        ("carry_initial_prompt", "true"),
    )
    tail = bytearray(b"\r\n")
    for name, value in fields:
        tail.extend(marker)
        tail.extend(
            (
                'Content-Disposition: form-data; name="'
                + name
                + '"\r\n\r\n'
            ).encode("ascii")
        )
        tail.extend(value.encode("utf-8"))
        tail.extend(b"\r\n")
    tail.extend(("--" + boundary + "--\r\n").encode("ascii"))
    suffix = bytes(tail)
    return _MultipartRequest(
        boundary=boundary,
        prefix=prefix,
        suffix=suffix,
        content_length=len(prefix) + len(segment.pcm) + len(suffix),
    )


def _wav_header(pcm_bytes: int, sample_rate: int) -> bytes:
    byte_rate = sample_rate * 2
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + pcm_bytes,
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        sample_rate,
        byte_rate,
        2,
        16,
        b"data",
        pcm_bytes,
    )


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _child_environment() -> dict[str, str]:
    # whisper.cpp needs normal runtime/GPU environment values, but it never
    # needs credentials held by the companion process.
    return {
        key: value
        for key, value in os.environ.items()
        if not _SECRET_ENV_NAME.search(key)
    }


def _whisper_server_command(
    executable: Path,
    model: Path,
    port: int,
    language: str,
    beam_size: int = 1,
) -> list[str]:
    # b4938 hard-codes server no_context=true and does not expose a
    # --no-context flag. Keep temperature fallback off for short, independent
    # Discord utterances. VAD stays outside the server process.
    command = [
        str(executable),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--model",
        str(model),
        "--language",
        language,
        "--no-timestamps",
        "--no-fallback",
        "--suppress-nst",
    ]
    # Omitted entirely at 1 rather than passed as --beam-size 1, because the
    # server's own default of -1 selects greedy sampling with --best-of 2,
    # which is not the same decoder path as a width-1 beam.
    if beam_size > 1:
        command.extend(["--beam-size", str(beam_size)])
    return command


def _launch_whisper_server(
    executable: Path,
    model: Path,
    port: int,
    language: str,
    beam_size: int = 1,
) -> ManagedProcess:
    return subprocess.Popen(
        _whisper_server_command(executable, model, port, language, beam_size),
        cwd=str(executable.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=_child_environment(),
        creationflags=NO_WINDOW,
    )


async def _read_http_response(
    reader: asyncio.StreamReader,
    max_body_bytes: int,
) -> tuple[int, bytes]:
    try:
        header_blob = await reader.readuntil(b"\r\n\r\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError) as error:
        raise BackendProtocolError("local backend returned invalid HTTP") from error
    if len(header_blob) > MAX_HTTP_HEADER_BYTES:
        raise BackendProtocolError("local backend response headers are too large")
    lines = header_blob[:-4].split(b"\r\n")
    try:
        status = int(lines[0].split(b" ", 2)[1])
    except (IndexError, ValueError) as error:
        raise BackendProtocolError("local backend returned invalid HTTP status") from error

    headers: dict[bytes, bytes] = {}
    for line in lines[1:]:
        if b":" not in line:
            raise BackendProtocolError("local backend returned invalid HTTP headers")
        name, value = line.split(b":", 1)
        headers[name.strip().lower()] = value.strip().lower()

    if b"chunked" in headers.get(b"transfer-encoding", b""):
        body = await _read_chunked(reader, max_body_bytes)
    elif b"content-length" in headers:
        try:
            length = int(headers[b"content-length"])
        except ValueError as error:
            raise BackendProtocolError("local backend returned invalid body length") from error
        if length < 0 or length > max_body_bytes:
            raise BackendProtocolError("local backend response is too large")
        try:
            body = await reader.readexactly(length)
        except asyncio.IncompleteReadError as error:
            raise BackendProtocolError("local backend response was truncated") from error
    else:
        body = await reader.read(max_body_bytes + 1)
        if len(body) > max_body_bytes:
            raise BackendProtocolError("local backend response is too large")
    return status, body


async def _read_chunked(reader: asyncio.StreamReader, limit: int) -> bytes:
    body = bytearray()
    while True:
        line = await reader.readline()
        if not line or len(line) > 128:
            raise BackendProtocolError("local backend returned invalid chunking")
        try:
            size = int(line.split(b";", 1)[0].strip(), 16)
        except ValueError as error:
            raise BackendProtocolError("local backend returned invalid chunk size") from error
        if size == 0:
            await reader.readuntil(b"\r\n")
            return bytes(body)
        if len(body) + size > limit:
            raise BackendProtocolError("local backend response is too large")
        try:
            body.extend(await reader.readexactly(size))
            if await reader.readexactly(2) != b"\r\n":
                raise BackendProtocolError("local backend returned invalid chunking")
        except asyncio.IncompleteReadError as error:
            raise BackendProtocolError("local backend response was truncated") from error


class AsyncioLoopbackHttpTransport:
    """Minimal cancellable HTTP/1.1 transport fixed to 127.0.0.1."""

    async def health(self, port: int, max_response_bytes: int) -> HttpResponse:
        reader: asyncio.StreamReader
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", port, limit=MAX_HTTP_HEADER_BYTES
            )
            writer.write(
                (
                    "GET /health HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{port}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("ascii")
            )
            await writer.drain()
            status, body = await _read_http_response(reader, max_response_bytes)
            return HttpResponse(status=status, body=body)
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass

    async def infer(
        self,
        port: int,
        request: _MultipartRequest,
        pcm: bytearray,
        cancellation: CancellationToken,
        max_response_bytes: int,
    ) -> HttpResponse:
        cancellation.raise_if_cancelled()
        reader: asyncio.StreamReader
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", port, limit=MAX_HTTP_HEADER_BYTES
            )
            headers = (
                "POST /inference HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                "Connection: close\r\n"
                f"Content-Type: multipart/form-data; boundary={request.boundary}\r\n"
                f"Content-Length: {request.content_length}\r\n\r\n"
            ).encode("ascii")
            writer.write(headers)
            writer.write(request.prefix)
            await writer.drain()

            view = memoryview(pcm)
            try:
                # A worker that dies mid-upload leaves an asyncio fatal-error
                # traceback pinning this frame, and view.release() below frees
                # only the parent view, never a slice taken from it. The caller
                # must still be able to wipe its own PCM, so each chunk handed
                # to the transport is an owned copy rather than an export.
                for offset in range(0, len(view), 65_536):
                    cancellation.raise_if_cancelled()
                    writer.write(bytes(view[offset : offset + 65_536]))
                    await writer.drain()
            finally:
                view.release()
            writer.write(request.suffix)
            await writer.drain()
            cancellation.raise_if_cancelled()
            status, body = await _read_http_response(reader, max_response_bytes)
            return HttpResponse(status=status, body=body)
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass


class WhisperCppClient:
    """In-memory inference client independent from process management."""

    def __init__(self, transport: WhisperHttpTransport | None = None) -> None:
        self.transport = transport or AsyncioLoopbackHttpTransport()

    async def health(self, port: int) -> bool:
        response = await self.transport.health(port, 4_096)
        try:
            payload = json.loads(response.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return False
        return response.status == 200 and payload == {"status": "ok"}

    async def transcribe(
        self,
        port: int,
        segment: AudioSegment,
        *,
        prompt: str,
        language: str,
        cancellation: CancellationToken,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> str:
        request = _multipart_request(segment, prompt, language)
        response = await self.transport.infer(
            port,
            request,
            segment.pcm,
            cancellation,
            max_response_bytes,
        )
        if response.status != 200:
            raise BackendProtocolError("local whisper.cpp inference request failed")
        try:
            payload = json.loads(response.body)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise BackendProtocolError(
                "local whisper.cpp returned invalid JSON"
            ) from error
        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            raise BackendProtocolError(
                "local whisper.cpp response is missing transcript text"
            )
        return payload["text"]


class WhisperCppBackend:
    """Pool of persistent whisper-server processes with bounded concurrency."""

    def __init__(
        self,
        config: WhisperCppConfig | None = None,
        *,
        transport: WhisperHttpTransport | None = None,
        process_launcher: ProcessLauncher | None = None,
    ) -> None:
        self.config = config or WhisperCppConfig()
        self.client = WhisperCppClient(transport)
        self._process_launcher = process_launcher or _launch_whisper_server
        self._workers: list[_Worker] = []
        self._available: asyncio.Queue[_Worker] = asyncio.Queue()
        self._started = False

    @property
    def capability(self) -> BackendCapability:
        return detect_whisper_cpp(self.config)

    async def start(self) -> None:
        if self._started:
            return
        capability = self.capability
        if not capability.available:
            raise BackendUnavailable(capability.detail)
        assert capability.executable is not None and capability.model is not None
        executables = [capability.executable]
        fallback = _path_from_setting(
            self.config.fallback_server_path, FALLBACK_SERVER_ENV
        )
        try:
            if (
                fallback is not None
                and fallback.is_file()
                and fallback.resolve() != capability.executable.resolve()
            ):
                executables.append(fallback.resolve())
        except OSError:
            pass

        for index, executable in enumerate(executables):
            # Width above 1 only on an accelerated primary; every CPU
            # server decodes greedy. index 0 is the primary by
            # construction of the list above.
            beam_size = (
                self.config.beam_size
                if index == 0 and self.config.accelerated
                else 1
            )
            try:
                for _ in range(self.config.worker_count):
                    worker = await self._start_worker(
                        executable, capability.model, beam_size
                    )
                    self._workers.append(worker)
                    self._available.put_nowait(worker)
            except BackendUnavailable:
                await self.close()
                if index + 1 < len(executables):
                    continue
                raise
            except BaseException:
                await self.close()
                raise
            self._started = True
            return

    async def _start_worker(
        self, executable: Path, model: Path, beam_size: int
    ) -> _Worker:
        port = _free_loopback_port()
        try:
            process = self._process_launcher(
                executable,
                model,
                port,
                self.config.language,
                beam_size,
            )
        except OSError as error:
            raise BackendUnavailable("whisper-server could not start") from error

        worker = _Worker(process=process, port=port)
        try:
            deadline = time.monotonic() + self.config.start_timeout_seconds
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise BackendUnavailable(
                        "whisper-server stopped while loading"
                    )
                try:
                    healthy = await asyncio.wait_for(
                        self.client.health(port), timeout=0.75
                    )
                    if healthy:
                        return worker
                except (
                    OSError,
                    asyncio.TimeoutError,
                    json.JSONDecodeError,
                    BackendProtocolError,
                ):
                    pass
                await asyncio.sleep(0.1)
            raise BackendUnavailable("whisper-server model load timed out")
        except BaseException:
            self._stop_worker_now(worker)
            raise

    async def transcribe(
        self,
        segment: AudioSegment,
        *,
        prompt: str,
        cancellation: CancellationToken,
    ) -> str:
        if not self._started or any(
            worker.process.poll() is not None for worker in self._workers
        ):
            raise BackendUnavailable("whisper-server worker pool is unavailable")
        worker = await self._get_worker(cancellation)
        try:
            request = asyncio.create_task(
                self._infer(worker, segment, prompt, cancellation)
            )
            cancel_wait = asyncio.create_task(cancellation.wait())
            done, _ = await asyncio.wait(
                {request, cancel_wait}, return_when=asyncio.FIRST_COMPLETED
            )
            if cancellation.cancelled:
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)
                raise TranscriptionCancelled("transcription was cancelled")
            return await request
        except asyncio.CancelledError:
            cancellation.cancel()
            if "request" in locals():
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)
            raise
        finally:
            if "cancel_wait" in locals():
                cancel_wait.cancel()
                await asyncio.gather(cancel_wait, return_exceptions=True)
            self._available.put_nowait(worker)

    async def _get_worker(self, cancellation: CancellationToken) -> _Worker:
        get_worker = asyncio.create_task(self._available.get())
        cancel_wait = asyncio.create_task(cancellation.wait())
        try:
            done, _ = await asyncio.wait(
                {get_worker, cancel_wait}, return_when=asyncio.FIRST_COMPLETED
            )
            if cancellation.cancelled:
                await self._cancel_worker_get(get_worker)
                raise TranscriptionCancelled("transcription was cancelled")
            return await get_worker
        except asyncio.CancelledError:
            await self._cancel_worker_get(get_worker)
            raise
        finally:
            cancel_wait.cancel()
            await asyncio.gather(cancel_wait, return_exceptions=True)

    async def _cancel_worker_get(self, get_worker: asyncio.Task[_Worker]) -> None:
        if not get_worker.done():
            get_worker.cancel()
            await asyncio.gather(get_worker, return_exceptions=True)
            return
        if get_worker.cancelled():
            return
        try:
            worker = get_worker.result()
        except BaseException:
            return
        self._available.put_nowait(worker)

    async def _infer(
        self,
        worker: _Worker,
        segment: AudioSegment,
        prompt: str,
        cancellation: CancellationToken,
    ) -> str:
        return await self.client.transcribe(
            worker.port,
            segment,
            prompt=prompt,
            language=self.config.language,
            cancellation=cancellation,
            max_response_bytes=self.config.max_response_bytes,
        )

    async def close(self) -> None:
        workers = tuple(self._workers)
        self._workers.clear()
        while not self._available.empty():
            try:
                self._available.get_nowait()
            except asyncio.QueueEmpty:
                break
        await asyncio.gather(
            *(self._stop_worker(worker) for worker in workers),
            return_exceptions=True,
        )
        self._started = False

    async def _stop_worker(self, worker: _Worker) -> None:
        if worker.process.poll() is not None:
            return
        worker.process.terminate()
        try:
            await asyncio.wait_for(
                asyncio.to_thread(worker.process.wait), timeout=2.0
            )
        except asyncio.TimeoutError:
            worker.process.kill()
            worker.process.wait(timeout=2.0)
        except asyncio.CancelledError:
            worker.process.kill()
            worker.process.wait(timeout=2.0)
            raise

    @staticmethod
    def _stop_worker_now(worker: _Worker) -> None:
        if worker.process.poll() is not None:
            return
        worker.process.terminate()
        try:
            worker.process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            worker.process.kill()
            worker.process.wait(timeout=2.0)

    async def __aenter__(self) -> "WhisperCppBackend":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.close()
