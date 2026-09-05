"""Run a deterministic two-speaker STT boundary check with no model/network."""

from __future__ import annotations

import asyncio
import json

from .contracts import AudioSegment
from .fake import FakeSpeechActivityGate, FakeTranscriptionBackend
from .service import TranscriptionService


def silent_pcm(milliseconds: int = 40) -> bytearray:
    samples = 16_000 * milliseconds // 1000
    return bytearray(samples * 2)


async def run() -> int:
    backend = FakeTranscriptionBackend(
        {
            ("mira", 1): "Spread for circles, then stack on me.",
            ("drex", 1): "Boss turning left.",
        },
        hold=True,
    )
    service = TranscriptionService(
        backend,
        speech_gate=FakeSpeechActivityGate(),
        max_concurrent_speakers=2,
    )
    await service.start()
    segments = (
        AudioSegment("mira", 1, silent_pcm(), hints=("Mira",)),
        AudioSegment("drex", 1, silent_pcm(), hints=("Drex",)),
    )
    tasks = [asyncio.create_task(service.transcribe(segment)) for segment in segments]
    await backend.wait_until_active(2)
    backend.release()
    results = await asyncio.gather(*tasks)
    await service.close()

    expected = {"mira": "Spread for circles, then stack on me.", "drex": "Boss turning left."}
    actual = {result.speaker_id: result.text for result in results}
    ok = actual == expected and backend.max_active == 2 and all(
        segment.wiped for segment in segments
    )
    print(
        json.dumps(
            {
                "status": "ok" if ok else "failed",
                "concurrent_speakers": backend.max_active,
                "speakers": sorted(actual),
                "buffers_wiped": all(segment.wiped for segment in segments),
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )
    )
    return 0 if ok else 1


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
