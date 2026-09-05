"""Backend protocol for interchangeable memory-only local transcribers."""

from __future__ import annotations

from typing import Protocol

from .contracts import (
    AudioSegment,
    BackendCapability,
    CancellationToken,
)


class LocalTranscriptionBackend(Protocol):
    @property
    def capability(self) -> BackendCapability:
        """Describe readiness without starting capture or downloading assets."""

    async def start(self) -> None:
        """Load the local model and become ready for requests."""

    async def transcribe(
        self,
        segment: AudioSegment,
        *,
        prompt: str,
        cancellation: CancellationToken,
    ) -> str:
        """Transcribe one in-memory segment without recording its content."""

    async def close(self) -> None:
        """Cancel backend work and release model resources."""
