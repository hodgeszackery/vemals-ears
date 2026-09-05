"""Local, memory-only speech-to-text boundary for Voice Captions."""

from .backend import LocalTranscriptionBackend
from .contracts import (
    AudioSegment,
    BackendCapability,
    BackendProtocolError,
    BackendUnavailable,
    CancellationToken,
    DuplicateSegment,
    SpeechGateUnavailable,
    STTError,
    TranscriptResult,
    TranscriptionBusy,
    TranscriptionCancelled,
    TranscriptionTimeout,
)
from .fake import FakeSpeechActivityGate, FakeTranscriptionBackend
from .service import TranscriptionService
from .whisper_cpp import (
    AsyncioLoopbackHttpTransport,
    HttpResponse,
    ManagedProcess,
    ProcessLauncher,
    WhisperCppBackend,
    WhisperCppClient,
    WhisperCppConfig,
    WhisperHttpTransport,
    detect_whisper_cpp,
)
from .vad import (
    SpeechActivityGate,
    WhisperCppVadGate,
    WhisperVadConfig,
)

__all__ = [
    "AsyncioLoopbackHttpTransport",
    "AudioSegment",
    "BackendCapability",
    "BackendProtocolError",
    "BackendUnavailable",
    "CancellationToken",
    "DuplicateSegment",
    "FakeSpeechActivityGate",
    "FakeTranscriptionBackend",
    "HttpResponse",
    "LocalTranscriptionBackend",
    "ManagedProcess",
    "ProcessLauncher",
    "SpeechActivityGate",
    "SpeechGateUnavailable",
    "STTError",
    "TranscriptResult",
    "TranscriptionBusy",
    "TranscriptionCancelled",
    "TranscriptionService",
    "TranscriptionTimeout",
    "WhisperCppBackend",
    "WhisperCppClient",
    "WhisperCppConfig",
    "WhisperCppVadGate",
    "WhisperHttpTransport",
    "WhisperVadConfig",
    "detect_whisper_cpp",
]
