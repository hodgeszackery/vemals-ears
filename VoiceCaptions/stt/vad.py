"""Local Silero voice activity gate backed by bundled whisper.cpp libraries.

The gate runs before transcription. It converts owned PCM16 samples to a
short-lived float buffer required by whisper.cpp, wipes that temporary buffer
after every native call, and never writes audio to disk.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from .contracts import (
    AudioSegment,
    BackendCapability,
    CancellationToken,
    SpeechGateUnavailable,
    TranscriptionCancelled,
)

VAD_LIBRARY_ENV = "VOICE_CAPTIONS_WHISPER_DLL"
VAD_MODEL_ENV = "VOICE_CAPTIONS_WHISPER_VAD_MODEL"
VAD_MODEL_NAME = "ggml-silero-v6.2.0.bin"
MIN_VAD_MODEL_BYTES = 100_000


class SpeechActivityGate(Protocol):
    """Memory-only speech detector required before local transcription."""

    @property
    def capability(self) -> BackendCapability:
        """Describe readiness without loading native assets."""

    async def start(self) -> None:
        """Load local speech detection resources."""

    async def has_speech(
        self,
        segment: AudioSegment,
        *,
        cancellation: CancellationToken,
    ) -> bool:
        """Return true only when the owned segment contains speech."""

    async def close(self) -> None:
        """Release local speech detection resources."""


@dataclass(frozen=True, slots=True)
class WhisperVadConfig:
    library_path: Path | None = None
    model_path: Path | None = None
    worker_count: int = 2
    threshold: float = 0.7
    min_speech_duration_ms: int = 200
    min_silence_duration_ms: int = 300
    max_speech_duration_seconds: float = 30.0
    speech_pad_ms: int = 30
    samples_overlap_seconds: float = 0.1

    def __post_init__(self) -> None:
        if self.worker_count < 1 or self.worker_count > 4:
            raise ValueError("worker_count must be between 1 and 4")
        if not 0.0 < self.threshold < 1.0:
            raise ValueError("threshold must be between 0 and 1")
        if self.min_speech_duration_ms < 50 or self.min_speech_duration_ms > 2_000:
            raise ValueError("min_speech_duration_ms is outside the safe range")
        if self.min_silence_duration_ms < 20 or self.min_silence_duration_ms > 2_000:
            raise ValueError("min_silence_duration_ms is outside the safe range")
        if not 1.0 <= self.max_speech_duration_seconds <= 30.0:
            raise ValueError("max_speech_duration_seconds is outside the safe range")
        if self.speech_pad_ms < 0 or self.speech_pad_ms > 1_000:
            raise ValueError("speech_pad_ms is outside the safe range")
        if not 0.0 <= self.samples_overlap_seconds <= 1.0:
            raise ValueError("samples_overlap_seconds is outside the safe range")


@dataclass(frozen=True, slots=True)
class _NativeLibraryLayout:
    whisper: Path
    backend: Path
    base: Path
    backend_modules: Path


def _native_library_layout(library_path: Path) -> _NativeLibraryLayout:
    """Resolve the pinned whisper.cpp library family on each host platform."""
    path = Path(library_path)
    name = path.name.casefold()
    if name.endswith(".dll"):
        backend_name = "ggml.dll"
        base_name = "ggml-base.dll"
        backend_modules = path.parent
    elif ".dylib" in name:
        backend_name = "libggml.dylib"
        base_name = "libggml-base.dylib"
        backend_modules = path.parent.parent / "bin"
    else:
        backend_name = "libggml.so"
        base_name = "libggml-base.so"
        backend_modules = path.parent.parent / "bin"
    return _NativeLibraryLayout(
        whisper=path,
        backend=path.with_name(backend_name),
        base=path.with_name(base_name),
        backend_modules=backend_modules,
    )


def _default_whisper_library_name() -> str:
    if os.name == "nt":
        return "whisper.dll"
    if sys.platform == "darwin":
        return "libwhisper.dylib"
    return "libwhisper.so"


class _WhisperVadContextParams(ctypes.Structure):
    _fields_ = [
        ("n_threads", ctypes.c_int),
        ("use_gpu", ctypes.c_bool),
        ("gpu_device", ctypes.c_int),
    ]


class _WhisperVadParams(ctypes.Structure):
    _fields_ = [
        ("threshold", ctypes.c_float),
        ("min_speech_duration_ms", ctypes.c_int),
        ("min_silence_duration_ms", ctypes.c_int),
        ("max_speech_duration_s", ctypes.c_float),
        ("speech_pad_ms", ctypes.c_int),
        ("samples_overlap", ctypes.c_float),
    ]


LibraryLoader = Callable[[str], object]
_NativeLogCallback = ctypes.CFUNCTYPE(
    None,
    ctypes.c_int,
    ctypes.c_char_p,
    ctypes.c_void_p,
)


def _discard_native_log(_level: int, _text: bytes, _user_data: int) -> None:
    return


def _configured_path(explicit: Path | None, env_name: str) -> Path | None:
    if explicit is not None:
        return Path(explicit).expanduser()
    raw = os.environ.get(env_name, "").strip()
    return Path(os.path.expandvars(raw)).expanduser() if raw else None


def _configure_library(library: object) -> None:
    """Apply the pinned whisper.cpp b4938 C ABI to one loaded library."""
    try:
        library.whisper_vad_default_context_params.argtypes = []
        library.whisper_vad_default_context_params.restype = _WhisperVadContextParams
        library.whisper_vad_default_params.argtypes = []
        library.whisper_vad_default_params.restype = _WhisperVadParams
        library.whisper_vad_init_from_file_with_params.argtypes = [
            ctypes.c_char_p,
            _WhisperVadContextParams,
        ]
        library.whisper_vad_init_from_file_with_params.restype = ctypes.c_void_p
        library.whisper_vad_detect_speech.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_int,
        ]
        library.whisper_vad_detect_speech.restype = ctypes.c_bool
        library.whisper_vad_segments_from_probs.argtypes = [
            ctypes.c_void_p,
            _WhisperVadParams,
        ]
        library.whisper_vad_segments_from_probs.restype = ctypes.c_void_p
        library.whisper_vad_segments_n_segments.argtypes = [ctypes.c_void_p]
        library.whisper_vad_segments_n_segments.restype = ctypes.c_int
        library.whisper_vad_free_segments.argtypes = [ctypes.c_void_p]
        library.whisper_vad_free_segments.restype = None
        library.whisper_vad_free.argtypes = [ctypes.c_void_p]
        library.whisper_vad_free.restype = None
        library.whisper_log_set.argtypes = [_NativeLogCallback, ctypes.c_void_p]
        library.whisper_log_set.restype = None
    except (AttributeError, TypeError) as error:
        raise SpeechGateUnavailable("local speech gate ABI is unavailable") from error


def _configure_backend_library(library: object) -> None:
    try:
        library.ggml_backend_load_all_from_path.argtypes = [ctypes.c_char_p]
        library.ggml_backend_load_all_from_path.restype = None
        library.ggml_backend_dev_count.argtypes = []
        library.ggml_backend_dev_count.restype = ctypes.c_size_t
    except (AttributeError, TypeError) as error:
        raise SpeechGateUnavailable("local speech gate backend ABI is unavailable") from error


def _configure_base_library(library: object) -> None:
    try:
        library.ggml_log_set.argtypes = [_NativeLogCallback, ctypes.c_void_p]
        library.ggml_log_set.restype = None
    except (AttributeError, TypeError) as error:
        raise SpeechGateUnavailable("local speech gate logging ABI is unavailable") from error


def _wipe_float_buffer(samples: ctypes.Array[ctypes.c_float]) -> None:
    ctypes.memset(ctypes.addressof(samples), 0, ctypes.sizeof(samples))


def _pcm16_float_buffer(pcm: bytearray) -> ctypes.Array[ctypes.c_float]:
    sample_count = len(pcm) // 2
    samples = (ctypes.c_float * sample_count)()
    try:
        for index, (sample,) in enumerate(struct.iter_unpack("<h", pcm)):
            samples[index] = sample / 32768.0
    except BaseException:
        _wipe_float_buffer(samples)
        raise
    return samples


async def _finish_native_task(task: asyncio.Task) -> None:
    """Do not release a native context while its worker thread still uses it."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    await asyncio.gather(task, return_exceptions=True)


class WhisperCppVadGate:
    """Pooled Silero VAD contexts loaded from the local whisper.cpp runtime."""

    def __init__(
        self,
        config: WhisperVadConfig | None = None,
        *,
        library_loader: LibraryLoader = ctypes.CDLL,
    ) -> None:
        self.config = config or WhisperVadConfig()
        self._library_loader = library_loader
        self._library: object | None = None
        self._base_library: object | None = None
        self._backend_library: object | None = None
        self._contexts: asyncio.Queue[ctypes.c_void_p] | None = None
        self._all_contexts: list[ctypes.c_void_p] = []
        self._dll_directory = None
        self._log_callback = _NativeLogCallback(_discard_native_log)
        self._started = False
        self._closing = False
        self._active_calls = 0
        self._idle_calls = asyncio.Event()
        self._idle_calls.set()
        self._close_complete = asyncio.Event()
        self._close_complete.set()
        self._lifecycle_lock = asyncio.Lock()

    @classmethod
    def from_whisper_config(cls, config: object) -> "WhisperCppVadGate":
        server_path = getattr(config, "server_path", None)
        stt_model_path = getattr(config, "model_path", None)
        explicit_library = getattr(config, "vad_library_path", None)
        explicit_model = getattr(config, "vad_model_path", None)
        library_path = explicit_library
        if library_path is None and server_path is not None:
            library_path = (
                Path(server_path).parent / _default_whisper_library_name()
            )
        model_path = explicit_model
        if model_path is None and stt_model_path is not None:
            model_path = Path(stt_model_path).parent / VAD_MODEL_NAME
        return cls(
            WhisperVadConfig(
                library_path=library_path,
                model_path=model_path,
                worker_count=int(getattr(config, "worker_count", 2)),
            )
        )

    @property
    def capability(self) -> BackendCapability:
        library = _configured_path(self.config.library_path, VAD_LIBRARY_ENV)
        model = _configured_path(self.config.model_path, VAD_MODEL_ENV)
        gaps: list[str] = []
        if library is None:
            gaps.append(VAD_LIBRARY_ENV + " is not set")
        elif not library.is_file():
            gaps.append("configured whisper.cpp VAD library does not exist")
        else:
            layout = _native_library_layout(library)
            if not layout.backend.is_file():
                gaps.append("bundled ggml backend library does not exist")
            if not layout.base.is_file():
                gaps.append("bundled ggml base library does not exist")
        if model is None:
            gaps.append(VAD_MODEL_ENV + " is not set")
        elif not model.is_file():
            gaps.append("configured Silero VAD model does not exist")
        else:
            try:
                valid_model = (
                    model.suffix.casefold() == ".bin"
                    and model.stat().st_size >= MIN_VAD_MODEL_BYTES
                )
            except OSError:
                valid_model = False
            if not valid_model:
                gaps.append("configured Silero VAD model is invalid")
        if gaps:
            return BackendCapability(
                backend="whisper.cpp VAD",
                available=False,
                detail="; ".join(gaps),
                executable=library,
                model=model,
            )
        assert library is not None and model is not None
        return BackendCapability(
            backend="whisper.cpp VAD",
            available=True,
            detail="local whisper.cpp Silero VAD detected",
            executable=library.resolve(),
            model=model.resolve(),
        )

    async def start(self) -> None:
        async with self._lifecycle_lock:
            await self._start_serialized()

    async def _start_serialized(self) -> None:
        if self._started:
            return
        if self._closing:
            raise SpeechGateUnavailable("local speech gate is closing")
        capability = self.capability
        if not capability.available:
            raise SpeechGateUnavailable(capability.detail)
        assert capability.executable is not None and capability.model is not None
        loader = asyncio.create_task(
            asyncio.to_thread(
                self._load_contexts,
                capability.executable,
                capability.model,
            )
        )
        try:
            await asyncio.shield(loader)
        except asyncio.CancelledError:
            await _finish_native_task(loader)
            cleanup = asyncio.create_task(self._release_resources())
            await _finish_native_task(cleanup)
            raise
        except SpeechGateUnavailable:
            await self._release_resources()
            raise
        except (OSError, RuntimeError, TypeError, ValueError):
            await self._release_resources()
            raise SpeechGateUnavailable("local speech gate could not start") from None
        self._contexts = asyncio.Queue(maxsize=len(self._all_contexts))
        for context in self._all_contexts:
            self._contexts.put_nowait(context)
        self._started = True

    def _load_contexts(self, library_path: Path, model_path: Path) -> None:
        layout = _native_library_layout(library_path)
        if os.name == "nt" and hasattr(os, "add_dll_directory"):
            self._dll_directory = os.add_dll_directory(str(layout.whisper.parent))
        try:
            base_library = self._library_loader(str(layout.base))
            _configure_base_library(base_library)
            base_library.ggml_log_set(self._log_callback, None)
            self._base_library = base_library
            backend_library = self._library_loader(str(layout.backend))
            _configure_backend_library(backend_library)
            backend_library.ggml_backend_load_all_from_path(
                os.fsencode(str(layout.backend_modules))
            )
            if int(backend_library.ggml_backend_dev_count()) < 1:
                raise SpeechGateUnavailable("local speech gate backend could not load")
            self._backend_library = backend_library
            library = self._library_loader(str(layout.whisper))
            _configure_library(library)
            library.whisper_log_set(self._log_callback, None)
            self._library = library
            params = library.whisper_vad_default_context_params()
            params.use_gpu = False
            model_bytes = os.fsencode(str(model_path))
            for _ in range(self.config.worker_count):
                context = library.whisper_vad_init_from_file_with_params(
                    model_bytes,
                    params,
                )
                if not context:
                    raise SpeechGateUnavailable("local speech gate model could not load")
                self._all_contexts.append(ctypes.c_void_p(context))
        except BaseException:
            self._release_resources_sync()
            raise

    async def has_speech(
        self,
        segment: AudioSegment,
        *,
        cancellation: CancellationToken,
    ) -> bool:
        if not self._started or self._closing or self._contexts is None:
            raise SpeechGateUnavailable("local speech gate is not active")
        cancellation.raise_if_cancelled()
        self._active_calls += 1
        self._idle_calls.clear()
        get_context = asyncio.create_task(self._contexts.get())
        cancel_wait = asyncio.create_task(cancellation.wait())
        context: ctypes.c_void_p | None = None
        native_task: asyncio.Task[bool] | None = None
        try:
            done, _ = await asyncio.wait(
                {get_context, cancel_wait}, return_when=asyncio.FIRST_COMPLETED
            )
            if cancellation.cancelled:
                raise TranscriptionCancelled("transcription was cancelled")
            if get_context not in done:
                raise SpeechGateUnavailable("local speech gate context is unavailable")
            context = await get_context
            if self._closing:
                raise SpeechGateUnavailable("local speech gate is closing")
            native_task = asyncio.create_task(
                asyncio.to_thread(self._detect_speech, context, segment.pcm)
            )
            done, _ = await asyncio.wait(
                {native_task, cancel_wait}, return_when=asyncio.FIRST_COMPLETED
            )
            if cancellation.cancelled:
                await _finish_native_task(native_task)
                raise TranscriptionCancelled("transcription was cancelled")
            if native_task not in done:
                raise SpeechGateUnavailable("local speech gate did not finish")
            return await native_task
        except asyncio.CancelledError:
            cancellation.cancel()
            if native_task is not None:
                await _finish_native_task(native_task)
            raise
        except (SpeechGateUnavailable, TranscriptionCancelled):
            raise
        except Exception:
            raise SpeechGateUnavailable("local speech gate inference failed") from None
        finally:
            get_context.cancel()
            cancel_wait.cancel()
            try:
                await asyncio.gather(get_context, cancel_wait, return_exceptions=True)
            finally:
                # A cancellation delivered during the gather above must not skip
                # the pool return or the busy count, or the checked out context
                # is lost and close() waits forever on a call that has ended.
                if (
                    context is None
                    and get_context.done()
                    and not get_context.cancelled()
                ):
                    try:
                        context = get_context.result()
                    except Exception:
                        context = None
                if context is not None and self._contexts is not None:
                    self._contexts.put_nowait(context)
                self._active_calls -= 1
                if self._active_calls == 0:
                    self._idle_calls.set()

    def _detect_speech(self, context: ctypes.c_void_p, pcm: bytearray) -> bool:
        library = self._library
        if library is None:
            raise SpeechGateUnavailable("local speech gate library is unavailable")
        samples = _pcm16_float_buffer(pcm)
        segments = None
        try:
            if not library.whisper_vad_detect_speech(
                context,
                samples,
                len(samples),
            ):
                raise SpeechGateUnavailable("local speech gate inference failed")
            params = library.whisper_vad_default_params()
            params.threshold = self.config.threshold
            params.min_speech_duration_ms = self.config.min_speech_duration_ms
            params.min_silence_duration_ms = self.config.min_silence_duration_ms
            params.max_speech_duration_s = self.config.max_speech_duration_seconds
            params.speech_pad_ms = self.config.speech_pad_ms
            params.samples_overlap = self.config.samples_overlap_seconds
            segments = library.whisper_vad_segments_from_probs(context, params)
            if not segments:
                raise SpeechGateUnavailable("local speech gate returned no result")
            count = int(library.whisper_vad_segments_n_segments(segments))
            if count < 0:
                raise SpeechGateUnavailable("local speech gate returned invalid output")
            return count > 0
        finally:
            try:
                if segments:
                    library.whisper_vad_free_segments(segments)
            finally:
                _wipe_float_buffer(samples)

    async def close(self) -> None:
        async with self._lifecycle_lock:
            await self._close_serialized()

    async def _close_serialized(self) -> None:
        if self._closing:
            await self._close_complete.wait()
            return
        self._closing = True
        self._close_complete.clear()
        cleanup = asyncio.create_task(self._close_resources_when_idle())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await _finish_native_task(cleanup)
            raise
        finally:
            self._started = False
            self._closing = False
            self._close_complete.set()

    async def _close_resources_when_idle(self) -> None:
        await self._idle_calls.wait()
        await self._release_resources()

    async def _release_resources(self) -> None:
        await asyncio.to_thread(self._release_resources_sync)
        self._contexts = None

    def _release_resources_sync(self) -> None:
        library = self._library
        if library is not None:
            for context in self._all_contexts:
                try:
                    library.whisper_vad_free(context)
                except (AttributeError, OSError, TypeError, ValueError):
                    pass
        self._all_contexts.clear()
        self._library = None
        self._backend_library = None
        self._base_library = None
        if self._dll_directory is not None:
            try:
                self._dll_directory.close()
            except OSError:
                pass
            self._dll_directory = None

    async def __aenter__(self) -> "WhisperCppVadGate":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.close()
