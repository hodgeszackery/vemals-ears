"""Privacy-first contracts for local speech-to-text.

The public boundary accepts owned, mutable PCM buffers. The service wipes and
releases those buffers on every terminal path. No contract accepts a file path
for audio, which keeps accidental disk-backed transcription out of the design.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path

SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2
MAX_SEGMENT_SECONDS = 30
MAX_PCM_BYTES = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH_BYTES * MAX_SEGMENT_SECONDS
MAX_SEGMENT_ID = 9_007_199_254_740_991
MAX_TRANSCRIPT_CHARS = 320

_SPEAKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$")
_SPACE = re.compile(r"\s+")
_ZERO_BLOCK = b"\x00" * 65_536


class STTError(RuntimeError):
    """Base class for local transcription failures safe to show as status."""


class BackendUnavailable(STTError):
    """The requested local backend is not ready, so capture must stay off."""


class TranscriptionBusy(STTError):
    """The bounded in-memory queue is full."""


class DuplicateSegment(STTError):
    """A speaker/segment pair is already being processed."""


class TranscriptionCancelled(STTError):
    """A segment was explicitly cancelled before a result was returned."""


class TranscriptionTimeout(STTError):
    """A segment exceeded its queue-plus-inference deadline."""


class BackendProtocolError(STTError):
    """A local backend returned an invalid or unsafe response."""


class SpeechGateUnavailable(STTError):
    """The required local speech detector failed, so STT must stay closed."""


@dataclass(frozen=True, slots=True)
class BackendCapability:
    backend: str
    available: bool
    detail: str
    executable: Path | None = None
    model: Path | None = None


@dataclass(slots=True)
class AudioSegment:
    """One speaker's owned PCM16 mono segment.

    ``pcm`` must be a bytearray, not immutable bytes. Ownership transfers to
    the transcription service at submission. Callers must not retain or reuse
    it. This makes prompt wiping deterministic at the Python boundary.
    """

    speaker_id: str
    segment_id: int
    pcm: bytearray = field(repr=False)
    hints: tuple[str, ...] = ()
    sample_rate: int = SAMPLE_RATE
    channels: int = CHANNELS
    sample_width_bytes: int = SAMPLE_WIDTH_BYTES
    _wiped: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.speaker_id, str) or not _SPEAKER_ID.fullmatch(
            self.speaker_id
        ):
            raise ValueError("speaker_id is invalid")
        if (
            isinstance(self.segment_id, bool)
            or not isinstance(self.segment_id, int)
            or self.segment_id < 0
            or self.segment_id > MAX_SEGMENT_ID
        ):
            raise ValueError("segment_id is out of range")
        if not isinstance(self.pcm, bytearray):
            raise TypeError("pcm must be an owned bytearray")
        if self.sample_rate != SAMPLE_RATE:
            raise ValueError("PCM sample rate must be 16000 Hz")
        if self.channels != CHANNELS:
            raise ValueError("PCM must be mono")
        if self.sample_width_bytes != SAMPLE_WIDTH_BYTES:
            raise ValueError("PCM samples must be signed 16-bit little-endian")
        if not self.pcm or len(self.pcm) > MAX_PCM_BYTES:
            raise ValueError("PCM segment is empty or longer than 30 seconds")
        if len(self.pcm) % SAMPLE_WIDTH_BYTES:
            raise ValueError("PCM segment ends inside a sample")
        if not isinstance(self.hints, tuple) or not all(
            isinstance(term, str) for term in self.hints
        ):
            raise TypeError("hints must be a tuple of text terms")

    @property
    def duration_seconds(self) -> float:
        return len(self.pcm) / (
            self.sample_rate * self.channels * self.sample_width_bytes
        )

    @property
    def wiped(self) -> bool:
        return self._wiped

    def wipe(self) -> None:
        """Overwrite the owned buffer, then release its allocation."""
        if self._wiped:
            return
        for offset in range(0, len(self.pcm), len(_ZERO_BLOCK)):
            size = min(len(_ZERO_BLOCK), len(self.pcm) - offset)
            self.pcm[offset : offset + size] = _ZERO_BLOCK[:size]
        self.pcm.clear()
        self._wiped = True


@dataclass(frozen=True, slots=True)
class TranscriptResult:
    speaker_id: str
    segment_id: int
    text: str
    elapsed_ms: int


class CancellationToken:
    """Cooperative cancellation shared by service and backend."""

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = asyncio.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    async def wait(self) -> None:
        await self._event.wait()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise TranscriptionCancelled("transcription was cancelled")


def clean_transcript(value: object) -> str:
    """Normalize backend text without logging or retaining the source body."""
    if not isinstance(value, str):
        raise BackendProtocolError("local backend returned non-text output")
    clean = "".join(
        " " if char in "\t\r\n" else char
        for char in value
        if char >= " " or char in "\t\r\n"
        if char != "\x7f"
    )
    return _SPACE.sub(" ", clean).strip()[:MAX_TRANSCRIPT_CHARS].rstrip()
