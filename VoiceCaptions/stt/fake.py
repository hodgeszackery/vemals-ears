"""Deterministic, model-free backend used by tests and the demo harness."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Mapping

from .contracts import (
    AudioSegment,
    BackendCapability,
    BackendUnavailable,
    CancellationToken,
    TranscriptionCancelled,
)


class FakeSpeechActivityGate:
    """Scripted in-memory gate for pipeline tests without native assets."""

    def __init__(
        self,
        decisions: Mapping[tuple[str, int], bool] | None = None,
        *,
        default: bool = True,
        failure: Exception | None = None,
        hold: bool = False,
        available: bool = True,
    ) -> None:
        self._decisions = dict(decisions or {})
        self._default = bool(default)
        self._failure = failure
        self._release = asyncio.Event()
        if not hold:
            self._release.set()
        self._changed = asyncio.Event()
        self._available = available
        self._started = False
        self.calls: list[tuple[str, int]] = []
        self.active: set[tuple[str, int]] = set()
        self.max_active = 0

    @property
    def capability(self) -> BackendCapability:
        return BackendCapability(
            backend="fake VAD",
            available=self._available,
            detail="deterministic model-free test speech gate",
        )

    async def start(self) -> None:
        if not self._available:
            raise BackendUnavailable("fake speech gate is unavailable")
        self._started = True

    async def has_speech(
        self,
        segment: AudioSegment,
        *,
        cancellation: CancellationToken,
    ) -> bool:
        if not self._started:
            raise BackendUnavailable("fake speech gate is not active")
        key = (segment.speaker_id, segment.segment_id)
        self.calls.append(key)
        self.active.add(key)
        self.max_active = max(self.max_active, len(self.active))
        self._changed.set()
        release_wait = asyncio.create_task(self._release.wait())
        cancel_wait = asyncio.create_task(cancellation.wait())
        try:
            done, _ = await asyncio.wait(
                {release_wait, cancel_wait}, return_when=asyncio.FIRST_COMPLETED
            )
            if cancel_wait in done:
                raise TranscriptionCancelled("transcription was cancelled")
            if self._failure is not None:
                raise self._failure
            return self._decisions.get(key, self._default)
        finally:
            release_wait.cancel()
            cancel_wait.cancel()
            await asyncio.gather(
                release_wait,
                cancel_wait,
                return_exceptions=True,
            )
            self.active.discard(key)
            self._changed.set()

    async def close(self) -> None:
        self._release.set()
        self._started = False

    def release(self) -> None:
        self._release.set()

    async def wait_until_active(self, count: int, timeout: float = 1.0) -> None:
        async def wait() -> None:
            while len(self.active) < count:
                self._changed.clear()
                if len(self.active) >= count:
                    return
                await self._changed.wait()

        await asyncio.wait_for(wait(), timeout=timeout)


@dataclass(frozen=True, slots=True)
class FakeCall:
    speaker_id: str
    segment_id: int
    byte_count: int


class FakeTranscriptionBackend:
    """Return scripted text without reading audio content.

    Call records contain identity and byte counts only. They intentionally do
    not retain PCM or transcript output.
    """

    def __init__(
        self,
        responses: Mapping[tuple[str, int], str] | None = None,
        *,
        hold: bool = False,
    ) -> None:
        self._responses = dict(responses or {})
        self._release = asyncio.Event()
        if not hold:
            self._release.set()
        self._changed = asyncio.Event()
        self._started = False
        self.calls: list[FakeCall] = []
        self.prompts: dict[tuple[str, int], str] = {}
        self.active: set[tuple[str, int]] = set()
        self.max_active = 0
        self.max_active_by_speaker: dict[str, int] = {}

    @property
    def capability(self) -> BackendCapability:
        return BackendCapability(
            backend="fake",
            available=True,
            detail="deterministic model-free test backend",
        )

    async def start(self) -> None:
        self._started = True

    async def transcribe(
        self,
        segment: AudioSegment,
        *,
        prompt: str,
        cancellation: CancellationToken,
    ) -> str:
        if not self._started:
            raise BackendUnavailable("fake backend is not active")
        key = (segment.speaker_id, segment.segment_id)
        self.calls.append(
            FakeCall(
                speaker_id=segment.speaker_id,
                segment_id=segment.segment_id,
                byte_count=len(segment.pcm),
            )
        )
        self.prompts[key] = prompt
        self.active.add(key)
        self.max_active = max(self.max_active, len(self.active))
        same_speaker = sum(
            1 for active_key in self.active if active_key[0] == segment.speaker_id
        )
        self.max_active_by_speaker[segment.speaker_id] = max(
            self.max_active_by_speaker.get(segment.speaker_id, 0), same_speaker
        )
        self._changed.set()
        try:
            release_wait = asyncio.create_task(self._release.wait())
            cancel_wait = asyncio.create_task(cancellation.wait())
            done, _ = await asyncio.wait(
                {release_wait, cancel_wait}, return_when=asyncio.FIRST_COMPLETED
            )
            if cancel_wait in done:
                raise TranscriptionCancelled("transcription was cancelled")
            return self._responses.get(key, "")
        finally:
            release_wait.cancel()
            cancel_wait.cancel()
            await asyncio.gather(
                release_wait, cancel_wait, return_exceptions=True
            )
            self.active.discard(key)
            self._changed.set()

    async def close(self) -> None:
        self._release.set()
        self._started = False

    def release(self) -> None:
        self._release.set()

    async def wait_until_active(self, count: int, timeout: float = 1.0) -> None:
        async def wait() -> None:
            while len(self.active) < count:
                self._changed.clear()
                if len(self.active) >= count:
                    return
                await self._changed.wait()

        await asyncio.wait_for(wait(), timeout=timeout)
