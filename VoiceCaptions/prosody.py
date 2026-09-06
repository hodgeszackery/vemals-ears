"""Bounded, privacy-safe speech emphasis detection.

The caption pipeline already holds PCM long enough to transcribe it. This
module derives one loudness number while that owned buffer is alive, then
keeps only a short per-speaker baseline. Audio and text never enter the
detector, and no acoustic value is logged or put on the wire.

Whispering and shouting are not absolute volume levels: microphones, Discord
gain, and distance all change the signal. Classification therefore compares
an utterance with the same speaker's recent normal voice. Until that baseline
exists, only extreme levels are styled; uncertain speech remains normal.
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass, field
from typing import Final


SPEECH_STYLES: Final[frozenset[str]] = frozenset({"normal", "whisper", "shout"})
NORMAL_STYLE: Final[str] = "normal"
WHISPER_STYLE: Final[str] = "whisper"
SHOUT_STYLE: Final[str] = "shout"

_SAMPLE_MAX = 32_768.0
_FRAME_SAMPLES = 320  # 20 ms at the pipeline's fixed 16 kHz sample rate.
_MIN_DBFS = -96.0
_BASELINE_WINDOW = 12


def speech_level_dbfs(pcm: bytes | bytearray | memoryview) -> float | None:
    """Return a stable speech-level estimate for mono signed PCM16-LE.

    The median of the louder half of 20 ms frame RMS values avoids letting
    the normal trailing silence turn every sentence into a whisper. Invalid,
    empty, and fully silent input has no level and is classified as normal.
    """

    if not isinstance(pcm, (bytes, bytearray, memoryview)):
        return None
    try:
        raw = memoryview(pcm).cast("B")
    except (TypeError, ValueError):
        return None
    if not raw or len(raw) % 2:
        return None

    # VE is Windows-only and its PCM contract is signed 16-bit little-endian,
    # so a native short view reads the owned buffer without making another
    # audio copy that would outlive the explicit wipe in the coordinator.
    try:
        samples = raw.cast("h")
    except (TypeError, ValueError):
        return None
    if not samples:
        return None

    frame_levels: list[float] = []
    for start in range(0, len(samples), _FRAME_SAMPLES):
        frame = samples[start : start + _FRAME_SAMPLES]
        if not frame:
            continue
        mean_square = sum(float(value) * float(value) for value in frame) / len(frame)
        if mean_square <= 0:
            continue
        rms = math.sqrt(mean_square)
        frame_levels.append(max(_MIN_DBFS, 20.0 * math.log10(rms / _SAMPLE_MAX)))

    if not frame_levels:
        return None
    frame_levels.sort()
    louder_half = frame_levels[len(frame_levels) // 2 :]
    return float(statistics.median(louder_half))


@dataclass(slots=True)
class _SpeakerBaseline:
    levels: deque[float] = field(
        default_factory=lambda: deque(maxlen=_BASELINE_WINDOW)
    )
    shifted_style: str | None = None
    shifted_levels: list[float] = field(default_factory=list)


class SpeechStyleDetector:
    """Classify utterance loudness relative to one speaker's recent voice."""

    def __init__(
        self,
        *,
        minimum_baseline_samples: int = 3,
        whisper_delta_db: float = -10.0,
        shout_delta_db: float = 8.0,
        bootstrap_whisper_dbfs: float = -46.0,
        bootstrap_shout_dbfs: float = -7.0,
        rebase_after_samples: int = 5,
        rebase_max_spread_db: float = 3.0,
        maximum_speakers: int = 64,
    ) -> None:
        if not 1 <= minimum_baseline_samples <= _BASELINE_WINDOW:
            raise ValueError(
                "minimum_baseline_samples must fit the baseline window"
            )
        if whisper_delta_db >= 0 or shout_delta_db <= 0:
            raise ValueError("relative thresholds must straddle zero")
        if rebase_after_samples < 3 or rebase_after_samples > _BASELINE_WINDOW:
            raise ValueError("rebase_after_samples must be between 3 and 12")
        if (
            isinstance(rebase_max_spread_db, bool)
            or not isinstance(rebase_max_spread_db, (int, float))
            or not math.isfinite(rebase_max_spread_db)
            or rebase_max_spread_db <= 0
        ):
            raise ValueError("rebase_max_spread_db must be positive")
        if maximum_speakers < 1:
            raise ValueError("maximum_speakers must be positive")
        self.minimum_baseline_samples = minimum_baseline_samples
        self.whisper_delta_db = float(whisper_delta_db)
        self.shout_delta_db = float(shout_delta_db)
        self.bootstrap_whisper_dbfs = float(bootstrap_whisper_dbfs)
        self.bootstrap_shout_dbfs = float(bootstrap_shout_dbfs)
        self.rebase_after_samples = rebase_after_samples
        self.rebase_max_spread_db = float(rebase_max_spread_db)
        self.maximum_speakers = maximum_speakers
        self._speakers: dict[str, _SpeakerBaseline] = {}

    def _observe_shift(
        self,
        baseline: _SpeakerBaseline,
        style: str,
        level: float,
    ) -> bool:
        """Return true after a stable run proves the input gain moved.

        One loud or quiet utterance remains emphasis and never changes the
        normal reference. A run of tightly clustered outliers is different:
        that is also the shape of a microphone gain or Discord normalization
        change, so the detector eventually adopts it as the new reference.
        """

        candidate = baseline.shifted_levels
        if (
            baseline.shifted_style != style
            or (
                candidate
                and max(max(candidate), level) - min(min(candidate), level)
                > self.rebase_max_spread_db
            )
        ):
            baseline.shifted_style = style
            candidate.clear()
        candidate.append(level)
        if len(candidate) < self.rebase_after_samples:
            return False
        baseline.levels.clear()
        baseline.levels.extend(candidate)
        baseline.shifted_style = None
        candidate.clear()
        return True

    @staticmethod
    def _clear_shift(baseline: _SpeakerBaseline) -> None:
        baseline.shifted_style = None
        baseline.shifted_levels.clear()

    def classify_and_observe(self, speaker_id: str, level_dbfs: float | None) -> str:
        """Classify one new utterance and update the bounded normal baseline."""

        if (
            not isinstance(speaker_id, str)
            or not speaker_id
            or level_dbfs is None
            or isinstance(level_dbfs, bool)
            or not isinstance(level_dbfs, (int, float))
            or not math.isfinite(level_dbfs)
        ):
            return NORMAL_STYLE

        level = float(level_dbfs)
        baseline = self._speakers.get(speaker_id)
        if baseline is None:
            if len(self._speakers) >= self.maximum_speakers:
                # Dict insertion order makes this a bounded least-recently
                # created cache. No user identifier is retained indefinitely.
                self._speakers.pop(next(iter(self._speakers)))
            baseline = self._speakers[speaker_id] = _SpeakerBaseline()

        if len(baseline.levels) >= self.minimum_baseline_samples:
            reference = float(statistics.median(baseline.levels))
            difference = level - reference
            if difference <= self.whisper_delta_db:
                style = WHISPER_STYLE
                return (
                    NORMAL_STYLE
                    if self._observe_shift(baseline, style, level)
                    else style
                )
            if difference >= self.shout_delta_db:
                style = SHOUT_STYLE
                return (
                    NORMAL_STYLE
                    if self._observe_shift(baseline, style, level)
                    else style
                )
        else:
            if level <= self.bootstrap_whisper_dbfs:
                style = WHISPER_STYLE
                return (
                    NORMAL_STYLE
                    if self._observe_shift(baseline, style, level)
                    else style
                )
            if level >= self.bootstrap_shout_dbfs:
                style = SHOUT_STYLE
                return (
                    NORMAL_STYLE
                    if self._observe_shift(baseline, style, level)
                    else style
                )

        # A normal sample ends a possible gain-shift run and updates the
        # rolling reference.
        self._clear_shift(baseline)
        baseline.levels.append(level)
        return NORMAL_STYLE

    def forget(self, speaker_id: str) -> None:
        """Drop one speaker's derived baseline when their session ends."""

        self._speakers.pop(speaker_id, None)

    def retain(self, speaker_ids: tuple[str, ...]) -> None:
        """Drop baselines for speakers outside an authoritative roster."""

        retained = set(speaker_ids)
        for speaker_id in tuple(self._speakers):
            if speaker_id not in retained:
                self._speakers.pop(speaker_id, None)

    def clear(self) -> None:
        self._speakers.clear()
