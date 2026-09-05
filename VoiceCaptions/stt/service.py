"""Bounded, per-speaker coordinator for memory-only transcription."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Iterable

from .backend import LocalTranscriptionBackend
from .contracts import (
    AudioSegment,
    BackendUnavailable,
    CancellationToken,
    DuplicateSegment,
    SpeechGateUnavailable,
    TranscriptResult,
    TranscriptionBusy,
    TranscriptionCancelled,
    TranscriptionTimeout,
    clean_transcript,
)
from .vad import SpeechActivityGate
from .vocabulary import build_wow_prompt, normalize_terms


@dataclass(slots=True)
class _Pending:
    token: CancellationToken
    operation: asyncio.Task[str]


class TranscriptionService:
    """Keep speakers isolated while bounding total live audio in memory.

    Segments from the same speaker are serialized. Different speakers may run
    concurrently up to ``max_concurrent_speakers``. The pending cap includes
    queued and active work so an unhealthy backend cannot grow memory without
    bound.
    """

    def __init__(
        self,
        backend: LocalTranscriptionBackend,
        *,
        speech_gate: SpeechActivityGate,
        max_concurrent_speakers: int = 2,
        max_pending_segments: int = 12,
        default_timeout_seconds: float = 18.0,
        vocabulary: Iterable[str] = (),
    ) -> None:
        if max_concurrent_speakers < 1 or max_concurrent_speakers > 4:
            raise ValueError("max_concurrent_speakers must be between 1 and 4")
        if max_pending_segments < max_concurrent_speakers:
            raise ValueError("max_pending_segments cannot be below concurrency")
        if default_timeout_seconds <= 0:
            raise ValueError("default_timeout_seconds must be positive")
        self.backend = backend
        self.speech_gate = speech_gate
        self.max_concurrent_speakers = max_concurrent_speakers
        self.max_pending_segments = max_pending_segments
        self.default_timeout_seconds = float(default_timeout_seconds)
        self.vocabulary = normalize_terms(vocabulary)
        self._slots = asyncio.Semaphore(max_concurrent_speakers)
        self._speaker_locks: dict[str, asyncio.Lock] = {}
        self._pending: dict[tuple[str, int], _Pending] = {}
        self._started = False
        self._closing = False

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    async def start(self) -> None:
        if self._started:
            return
        if self._closing:
            raise BackendUnavailable("transcription service is closing")
        capability = self.backend.capability
        if not capability.available:
            raise BackendUnavailable(capability.detail)
        gate_capability = self.speech_gate.capability
        if not gate_capability.available:
            raise SpeechGateUnavailable(gate_capability.detail)
        await self.speech_gate.start()
        try:
            await self.backend.start()
        except BaseException:
            await self.speech_gate.close()
            raise
        self._started = True

    async def transcribe(
        self,
        segment: AudioSegment,
        *,
        timeout_seconds: float | None = None,
    ) -> TranscriptResult:
        """Transcribe one owned segment and wipe it on every terminal path."""
        timeout = (
            self.default_timeout_seconds
            if timeout_seconds is None
            else float(timeout_seconds)
        )
        if timeout <= 0:
            segment.wipe()
            raise ValueError("timeout_seconds must be positive")
        if not self._started or self._closing:
            segment.wipe()
            raise BackendUnavailable("local transcription is not active")

        key = (segment.speaker_id, segment.segment_id)
        if key in self._pending:
            segment.wipe()
            raise DuplicateSegment("speaker segment is already pending")
        if len(self._pending) >= self.max_pending_segments:
            segment.wipe()
            raise TranscriptionBusy("transcription queue is full")

        started_at = time.monotonic()
        token = CancellationToken()
        prompt = build_wow_prompt((*self.vocabulary, *segment.hints))
        operation = asyncio.create_task(self._run(segment, prompt, token))
        self._pending[key] = _Pending(token=token, operation=operation)
        cancel_wait = asyncio.create_task(token.wait())
        try:
            done, _ = await asyncio.wait(
                {operation, cancel_wait},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if token.cancelled:
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
                raise TranscriptionCancelled("transcription was cancelled")
            if operation in done:
                text = clean_transcript(await operation)
                elapsed_ms = max(0, round((time.monotonic() - started_at) * 1000))
                return TranscriptResult(
                    speaker_id=segment.speaker_id,
                    segment_id=segment.segment_id,
                    text=text,
                    elapsed_ms=elapsed_ms,
                )

            token.cancel()
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            raise TranscriptionTimeout("transcription deadline expired")
        except asyncio.CancelledError:
            token.cancel()
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            raise
        finally:
            # Draining the helper tasks suspends, so a CancelledError delivered
            # in that window would otherwise skip the wipe and leave raw PCM
            # plus a permanent pending entry behind. The nested finally keeps
            # the documented "wipe on every terminal path" contract intact.
            try:
                cancel_wait.cancel()
                await asyncio.gather(cancel_wait, return_exceptions=True)
                if not operation.done():
                    operation.cancel()
                    await asyncio.gather(operation, return_exceptions=True)
            finally:
                segment.wipe()
                self._pending.pop(key, None)
                if not any(pending_key[0] == key[0] for pending_key in self._pending):
                    self._speaker_locks.pop(key[0], None)

    async def _run(
        self,
        segment: AudioSegment,
        prompt: str,
        token: CancellationToken,
    ) -> str:
        # The per-speaker lock is taken before the concurrency slot because a
        # segment queued behind its own speaker does no work, and holding a
        # bounded slot while it waits would starve every other speaker.
        lock = self._speaker_locks.setdefault(segment.speaker_id, asyncio.Lock())
        async with lock:
            async with self._slots:
                token.raise_if_cancelled()
                try:
                    has_speech = await self.speech_gate.has_speech(
                        segment,
                        cancellation=token,
                    )
                except (SpeechGateUnavailable, TranscriptionCancelled):
                    raise
                except Exception:
                    raise SpeechGateUnavailable(
                        "local speech gate inference failed"
                    ) from None
                if not has_speech:
                    return ""
                token.raise_if_cancelled()
                return await self.backend.transcribe(
                    segment,
                    prompt=prompt,
                    cancellation=token,
                )

    def cancel(self, speaker_id: str, segment_id: int) -> bool:
        pending = self._pending.get((speaker_id, segment_id))
        if pending is None:
            return False
        pending.token.cancel()
        return True

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        pending = tuple(self._pending.values())
        for item in pending:
            item.token.cancel()
        if pending:
            await asyncio.sleep(0)
            await asyncio.gather(
                *(item.operation for item in pending), return_exceptions=True
            )
        try:
            await self.backend.close()
        finally:
            await self.speech_gate.close()
            self._started = False

    async def __aenter__(self) -> "TranscriptionService":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.close()
