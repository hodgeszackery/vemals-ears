"""Private Discord PCM to local STT to overlay coordinator.

The Node adapter's stdout is a private JSONL transport. Lines from it are
never copied to the companion stdout or diagnostics. This module validates and
assembles bounded per-speaker PCM, sends owned buffers to local STT, then emits
only sanitized caption/session objects to the overlay sink.
"""

from __future__ import annotations

import array
import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Awaitable, Callable, Protocol

from .credentials import DiscordCredentials, validate_credentials
from .settings import default_consent_state_path, default_mapping_state_path

# Named to match the adapter's own reader, so the two halves of this wiring
# cannot drift apart silently.
CONSENT_STATE_PATH_ENV = "CAPTIONS_CONSENT_STATE_PATH"
MAPPING_STATE_PATH_ENV = "CAPTIONS_MAPPING_STATE_PATH"
from .events import CaptionEvent, ProtocolEvent, SessionEvent
from .prosody import NORMAL_STYLE, SpeechStyleDetector, speech_level_dbfs
from .stt import AudioSegment, TranscriptResult, TranscriptionService
from .stt.vocabulary import apply_pronunciations
from .stt.contracts import (
    MAX_PCM_BYTES,
    MAX_SEGMENT_ID,
    BackendProtocolError,
    DuplicateSegment,
    SpeechGateUnavailable,
    TranscriptionBusy,
    TranscriptionCancelled,
    TranscriptionTimeout,
)

ROOT = Path(__file__).resolve().parent
ADAPTER_ROOT = ROOT / "discord_adapter"
ADAPTER_ENTRYPOINT = ADAPTER_ROOT / "src" / "index.js"

MAX_ADAPTER_LINE_BYTES = 24 * 1024
MAX_NODE_COMMAND_BYTES = 2 * 1024
MAX_BASE64_CHUNK_BYTES = 16 * 1024
MAX_PCM_CHUNK_BYTES = 8 * 1024
MIN_TRANSCRIBE_PCM_BYTES = 3_200
MAX_ACTIVE_SEGMENTS = 12
MAX_TOTAL_BUFFERED_PCM_BYTES = MAX_ACTIVE_SEGMENTS * MAX_PCM_BYTES
MAX_SEGMENT_AGE_SECONDS = 32.0
# Streaming: the adapter only ends a segment after 750 ms of silence, so a
# 20-second callout used to put NOTHING on anyone's screen for 20 seconds
# and then a wall of text. Once a live segment has buffered this much PCM
# (5.0 s at 32,000 bytes/s) a part is cut and transcribed while the segment
# stays open; the overlay shows the growing text as partials. 0 disables.
STREAM_FLUSH_PCM_BYTES = 160_000
# The cut is looked for inside the last 400 ms of the buffer, in 20 ms
# frames stepped 5 ms apart, and lands mid-way through the quietest one so
# it falls in a pause rather than through a word.
_CUT_WINDOW_BYTES = 12_800
_CUT_FRAME_SAMPLES = 320
_CUT_STEP_SAMPLES = 80
# Caption sequence = global_sequence * 64 + part slot. Partial prefixes take
# slots 1..62 and the final 63, so every caption of one segment orders after
# every caption of the segment before it, and the overlay's strictly
# increasing per-speaker rule (events.CaptionState.ingest) keeps holding.
# 64 slots cover a 32 s segment cut every 5 s with room to spare.
_SEQUENCE_SLOTS = 64
_FINAL_SLOT = _SEQUENCE_SLOTS - 1
_MAX_PART_INDEX = _FINAL_SLOT - 1
# A dropped part is skipped, reported to the host as one enum word, and
# the pipeline lives. Each of these is one segment's misfortune - the
# queue was full, the deadline passed, whisper-server sent one bad reply.
# BackendUnavailable is deliberately NOT here: the server being gone is
# everyone's problem and still fails closed.
_PER_SEGMENT_STT_FAULTS: tuple[tuple[type[Exception], str], ...] = (
    (TranscriptionTimeout, "transcription_timeout"),
    (TranscriptionBusy, "transcription_busy"),
    (TranscriptionCancelled, "transcription_cancelled"),
    (DuplicateSegment, "duplicate_segment"),
    (BackendProtocolError, "backend_protocol_error"),
    (SpeechGateUnavailable, "speech_gate_unavailable"),
)
_PER_SEGMENT_STT_FAULT_TYPES = tuple(cls for cls, _code in _PER_SEGMENT_STT_FAULTS)
SEGMENT_DROP_REPORT_INTERVAL_SECONDS = 30.0
MIN_NODE_VERSION = (22, 12, 0)
CONSENT_STATE_PATH_ENV = "CAPTIONS_CONSENT_STATE_PATH"
MAX_CONSENT_STATE_PATH_BYTES = 1_024

_TYPE = re.compile(r"^[a-z][a-z0-9]*(?:\.[a-z0-9]+)*$")
_SEGMENT_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_USER_ID = re.compile(r"^[0-9]{1,20}$")
_SNOWFLAKE = re.compile(r"^[0-9]{17,20}$")
_NODE_VERSION = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")

_WOW_CLASSES = {
    "death-knight",
    "demon-hunter",
    "druid",
    "evoker",
    "hunter",
    "mage",
    "monk",
    "paladin",
    "priest",
    "rogue",
    "shaman",
    "warlock",
    "warrior",
}
_WOW_ROLES = {"damage", "healer", "tank"}
_AUDIO_TYPES = {
    "audio.segment.started",
    "audio.segment.chunk",
    "audio.segment.ended",
}
_KNOWN_TYPES = _AUDIO_TYPES | {
    "adapter.starting",
    "adapter.ready",
    "adapter.failed",
    "adapter.stopped",
    "health.changed",
    "session.started",
    "session.stopped",
    "session.status",
    "session.participants.changed",
    "consent.granted",
    "consent.declined",
    "consent.forgotten",
    "consent.panel.failed",
    # Emitted when the adapter could not finish closing consent state. Unknown
    # here, it turned an orderly shutdown into unknown_event and a failure
    # report.
    "consent.state.dirty",
    "mapping.changed",
    "mapping.panel.failed",
    "command.rejected",
    "notice.failed",
    "voice.connected",
    "voice.disconnected",
    "voice.reconnecting",
    "voice.reconnected",
    "speaker.segment.started",
    "speaker.segment.ended",
    "quickchat.result",
    "audience.snapshot",
    "viewer.link.requested",
    "viewer.enroll.requested",
}
_SAFE_CHILD_ENV = {
    "APPDATA",
    "COMSPEC",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "WINDIR",
}


class CoordinatorError(RuntimeError):
    """A private pipeline invariant failed; the public message stays generic."""

    def __init__(self, code: str) -> None:
        super().__init__("Vemal's Ears capture pipeline stopped safely.")
        self.code = code


@dataclass(frozen=True, slots=True)
class NodeAdapterCapability:
    available: bool
    detail: str
    node: Path | None = None
    adapter_root: Path = ADAPTER_ROOT


@dataclass(frozen=True, slots=True)
class AdapterEnvelope:
    sequence: int
    event_type: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SpeakerIdentity:
    user_id: str = field(repr=False)
    display_name: str
    character_name: str = ""
    realm: str = ""
    wow_class: str = ""
    role: str = "unknown"

    @property
    def overlay_role(self) -> str:
        return {
            "damage": "DAMAGE",
            "healer": "HEALER",
            "tank": "TANK",
        }.get(self.role, "UNKNOWN")

    @property
    def hints(self) -> tuple[str, ...]:
        values = [self.display_name, self.character_name, self.realm]
        if self.wow_class:
            values.append(self.wow_class.replace("-", " "))
        return tuple(value for value in values if value)


@dataclass(slots=True)
class CompletedSegment:
    global_sequence: int
    audience_generation: int
    audience_revision: int
    speaker: SpeakerIdentity
    pcm: bytearray = field(repr=False)
    # Which slice of the utterance this is. A segment that never reached
    # the streaming threshold is its own single, last part. The last part
    # may carry an empty pcm: the tail after the final cut was too short to
    # transcribe, but the segment's final caption still has to be emitted
    # from the parts before it.
    part_index: int = 1
    last: bool = True

    @property
    def sequence(self) -> int:
        """The composed caption sequence, also the STT dedup key."""
        slot = _FINAL_SLOT if self.last else self.part_index
        return self.global_sequence * _SEQUENCE_SLOTS + slot

    def wipe(self) -> None:
        _wipe_bytearray(self.pcm)


@dataclass(slots=True)
class _SegmentParts:
    """Transcripts of one streamed utterance, in part order.

    Parts of one speaker transcribe in submission order at the service,
    but their tasks finish whenever they finish, and a dropped part
    finishes first of all. A None slot is a part still unknown; text is
    only ever shown as the longest prefix of known slots, so the live
    line never shows "part three" ahead of a missing part two.
    """

    slots: list[str | None] = field(default_factory=list)
    levels: list[float | None] = field(default_factory=list)
    total: int | None = None
    shown: int = 0
    speech_style: str | None = None

    def fill(
        self,
        part_index: int,
        text: str,
        *,
        level_dbfs: float | None,
        last: bool,
    ) -> None:
        while len(self.slots) < part_index:
            self.slots.append(None)
            self.levels.append(None)
        self.slots[part_index - 1] = text
        self.levels[part_index - 1] = level_dbfs
        if last:
            self.total = part_index

    def known_prefix(self) -> int:
        count = 0
        for text in self.slots:
            if text is None:
                break
            count += 1
        return count

    @property
    def complete(self) -> bool:
        return self.total is not None and self.known_prefix() >= self.total


def _join_parts(parts: list[str | None], maximum: int) -> str:
    """Concatenate known parts into the text a caption can carry.

    When a long utterance outgrows the caption, whole parts are dropped
    from the FRONT so the live line keeps showing the most recent speech
    rather than freezing on the first five seconds; the final is capped
    the same way, and _safe_transcript still clips the last word-level
    overflow. Losing the opening of a 40-second speech is the honest
    trade - the overlay showed it while it was current.
    """
    known = [text for text in parts if text]
    while len(known) > 1 and len(" ".join(known)) > maximum:
        known.pop(0)
    return " ".join(known)


@dataclass(frozen=True, slots=True)
class QuickChatResult:
    request_id: str
    status: str


@dataclass(slots=True)
class _ActiveSegment:
    segment_id: str
    global_sequence: int
    audience_generation: int
    audience_revision: int
    speaker: SpeakerIdentity
    pcm: bytearray = field(default_factory=bytearray, repr=False)
    next_chunk_sequence: int = 1
    started_at: float = 0.0
    parts_cut: int = 0
    # Bytes already handed to speech in earlier parts. The buffer only
    # holds the live tail after a cut, but the adapter counts the whole
    # utterance, and the end-of-segment total has to compare like with
    # like - see _end.
    pcm_flushed: int = 0

    def wipe(self) -> None:
        _wipe_bytearray(self.pcm)


class EventSink(Protocol):
    def __call__(self, event: ProtocolEvent) -> bool: ...


def _wipe_bytearray(buffer: bytearray) -> None:
    if buffer:
        buffer[:] = b"\x00" * len(buffer)
        buffer.clear()


def _quiet_cut_offset(pcm: bytearray) -> int:
    """Byte offset inside the last 400 ms where the audio is quietest.

    Cutting a live utterance at a fixed byte count splits words in half
    and whisper then guesses both halves. Scanning the tail for the
    lowest-energy 20 ms frame and cutting through the middle of it puts
    the seam in a breath instead. Pure Python on purpose - 6,400 samples
    every five seconds per speaker is nothing, and numpy is not a
    dependency of the companion.
    """
    window_start = max(0, len(pcm) - _CUT_WINDOW_BYTES)
    window_start -= window_start % 2
    samples = array.array("h")
    samples.frombytes(bytes(pcm[window_start:]))
    if sys.byteorder != "little":
        samples.byteswap()
    if len(samples) <= _CUT_FRAME_SAMPLES:
        return len(pcm)
    prefix = [0]
    total = 0
    for value in samples:
        total += value * value
        prefix.append(total)
    best_start = 0
    best_energy = None
    for start in range(0, len(samples) - _CUT_FRAME_SAMPLES + 1, _CUT_STEP_SAMPLES):
        energy = prefix[start + _CUT_FRAME_SAMPLES] - prefix[start]
        if best_energy is None or energy < best_energy:
            best_energy = energy
            best_start = start
    return window_start + (best_start + _CUT_FRAME_SAMPLES // 2) * 2


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError("non-finite JSON number")


def _validate_json_shape(value: Any) -> None:
    """Bound nesting/work and reject numeric values JSON cannot represent safely."""
    stack: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > 2_048 or depth > 32:
            raise ValueError("JSON structure limit exceeded")
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
        elif isinstance(current, float) and not math.isfinite(current):
            raise ValueError("non-finite JSON number")


def _positive_integer(value: Any, code: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > MAX_SEGMENT_ID
    ):
        raise CoordinatorError(code)
    return value


def _safe_transcript(value: Any, maximum: int, code: str) -> str:
    """Clean a transcript, clipping it rather than refusing it.

    _safe_text below raises when a field is too long, which is right for
    everything that arrives from the adapter: an oversized name is a
    protocol violation and failing closed is the safe answer.

    A transcript is not that. It comes from our own speech recognition,
    and somebody talking for more than a few seconds without pausing is
    ordinary raid behaviour, not an attack. Raising here took down the
    whole capture pipeline - the bot left the channel mid-sentence and
    every member lost captions - because one person said something long.

    Everything else still fails closed: a non-string, or control
    characters, is a real fault and is refused as before.
    """
    clean = _safe_text_body(value, code)
    if len(clean) <= maximum:
        return clean
    # Clip on a word boundary when there is one nearby, so the line ends
    # at a word rather than mid-syllable.
    clipped = clean[:maximum]
    boundary = clipped.rfind(" ")
    if boundary >= maximum - 24:
        clipped = clipped[:boundary]
    return clipped.rstrip()


def _safe_text_body(value: Any, code: str) -> str:
    """Type and control-character checks shared by both callers."""
    if not isinstance(value, str):
        raise CoordinatorError(code)
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value):
        raise CoordinatorError(code)
    normalized = unicodedata.normalize("NFKC", value)
    if any(
        unicodedata.category(char) in {"Cc", "Cf", "Cs"}
        for char in normalized
    ):
        raise CoordinatorError(code)
    clean = " ".join(normalized.split())
    if not clean:
        raise CoordinatorError(code)
    return clean


def _safe_text(value: Any, maximum: int, code: str) -> str:
    if not isinstance(value, str):
        raise CoordinatorError(code)
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value):
        raise CoordinatorError(code)
    normalized = unicodedata.normalize("NFKC", value)
    if any(
        unicodedata.category(char) in {"Cc", "Cf", "Cs"}
        for char in normalized
    ):
        raise CoordinatorError(code)
    clean = " ".join(normalized.split())
    if not clean or len(clean) > maximum:
        raise CoordinatorError(code)
    return clean


def _speaker(value: Any) -> SpeakerIdentity:
    if not isinstance(value, dict):
        raise CoordinatorError("invalid_speaker")
    user_id = value.get("userId")
    if not isinstance(user_id, str) or not _USER_ID.fullmatch(user_id):
        raise CoordinatorError("invalid_speaker")
    display_name = _safe_text(value.get("displayName"), 64, "invalid_speaker")
    character = value.get("character")
    if character is None:
        return SpeakerIdentity(user_id=user_id, display_name=display_name)
    if not isinstance(character, dict):
        raise CoordinatorError("invalid_character")
    character_name = _safe_text(
        character.get("characterName"), 24, "invalid_character"
    )
    realm_value = character.get("realm")
    realm = ""
    if realm_value is not None:
        realm = _safe_text(realm_value, 32, "invalid_character")
    wow_class = character.get("wowClass")
    role = character.get("role")
    if (
        not isinstance(wow_class, str)
        or not isinstance(role, str)
        or wow_class not in _WOW_CLASSES
        or role not in _WOW_ROLES
    ):
        raise CoordinatorError("invalid_character")
    return SpeakerIdentity(
        user_id=user_id,
        display_name=display_name,
        character_name=character_name,
        realm=realm,
        wow_class=wow_class,
        role=role,
    )


class AdapterProtocolDecoder:
    """Validate the outer JSONL envelope and strict process-wide ordering."""

    def __init__(self) -> None:
        self.last_sequence = 0

    def parse_line(self, raw_line: bytes | str) -> AdapterEnvelope:
        try:
            if isinstance(raw_line, str):
                encoded = raw_line.encode("utf-8", errors="strict")
            elif isinstance(raw_line, bytes):
                encoded = raw_line
            else:
                raise CoordinatorError("invalid_line")
        except UnicodeError:
            raise CoordinatorError("invalid_json") from None
        if not encoded or len(encoded) > MAX_ADAPTER_LINE_BYTES:
            raise CoordinatorError("line_limit")
        try:
            payload = json.loads(
                encoded.decode("utf-8", errors="strict"),
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            )
            _validate_json_shape(payload)
        except (UnicodeError, ValueError, RecursionError):
            raise CoordinatorError("invalid_json") from None
        if not isinstance(payload, dict):
            raise CoordinatorError("invalid_envelope")
        version = payload.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version != 1:
            raise CoordinatorError("invalid_envelope")
        sequence = _positive_integer(payload.get("sequence"), "invalid_order")
        if sequence != self.last_sequence + 1:
            raise CoordinatorError("invalid_order")
        event_type = payload.get("type")
        if (
            not isinstance(event_type, str)
            or len(event_type) > 64
            or not _TYPE.fullmatch(event_type)
            or event_type not in _KNOWN_TYPES
        ):
            raise CoordinatorError("unknown_event")
        event_payload = payload.get("payload")
        if not isinstance(event_payload, dict):
            raise CoordinatorError("invalid_envelope")
        timestamp = payload.get("time")
        if not isinstance(timestamp, str) or len(timestamp) > 40:
            raise CoordinatorError("invalid_envelope")
        try:
            parsed_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            raise CoordinatorError("invalid_envelope") from None
        if parsed_time.tzinfo is None:
            raise CoordinatorError("invalid_envelope")
        self.last_sequence = sequence
        return AdapterEnvelope(sequence, event_type, event_payload)


class SegmentAssembler:
    """Bounded interleaved segment assembly with one segment per speaker."""

    def __init__(
        self,
        *,
        max_active_segments: int = MAX_ACTIVE_SEGMENTS,
        max_total_pcm_bytes: int = MAX_TOTAL_BUFFERED_PCM_BYTES,
        max_segment_age_seconds: float = MAX_SEGMENT_AGE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        stream_flush_pcm_bytes: int = STREAM_FLUSH_PCM_BYTES,
    ) -> None:
        if max_active_segments < 1 or max_active_segments > 32:
            raise ValueError("max_active_segments must be between 1 and 32")
        if max_total_pcm_bytes < MAX_PCM_CHUNK_BYTES:
            raise ValueError("max_total_pcm_bytes is too small")
        if max_segment_age_seconds <= 0:
            raise ValueError("max_segment_age_seconds must be positive")
        if (
            isinstance(stream_flush_pcm_bytes, bool)
            or not isinstance(stream_flush_pcm_bytes, int)
            or stream_flush_pcm_bytes < 0
            or 0 < stream_flush_pcm_bytes < _CUT_WINDOW_BYTES + MIN_TRANSCRIBE_PCM_BYTES
        ):
            raise ValueError("stream_flush_pcm_bytes must be 0 or at least 16000")
        self.max_active_segments = max_active_segments
        self.max_total_pcm_bytes = max_total_pcm_bytes
        self.max_segment_age_seconds = float(max_segment_age_seconds)
        self.stream_flush_pcm_bytes = stream_flush_pcm_bytes
        self.clock = clock
        self.active: dict[str, _ActiveSegment] = {}
        self.active_by_speaker: dict[str, str] = {}
        self.last_global_sequence = 0
        self.total_pcm_bytes = 0
        # Segments dropped mid-stream (an oversize chunk, a blown
        # budget). The adapter keeps sending their remaining chunks and
        # the final end frame, and those must be swallowed silently -
        # without this, dropping a segment just converts chunk_limit
        # into unknown_segment one frame later, which stops the whole
        # pipeline anyway. Bounded FIFO so a hostile stream cannot grow
        # it without limit.
        self.dropped_segments: dict[str, int] = {}

    def accept(self, envelope: AdapterEnvelope) -> CompletedSegment | None:
        self.check_deadlines()
        if envelope.event_type == "audio.segment.started":
            self._start(envelope.payload)
            return None
        if envelope.event_type == "audio.segment.chunk":
            return self._chunk(envelope.payload)
        if envelope.event_type == "audio.segment.ended":
            return self._end(envelope.payload)
        return None

    def _segment_key(self, payload: dict[str, Any]) -> tuple[str, int]:
        segment_id = payload.get("segmentId")
        if not isinstance(segment_id, str) or not _SEGMENT_ID.fullmatch(segment_id):
            raise CoordinatorError("invalid_segment")
        global_sequence = _positive_integer(
            payload.get("globalSequence"), "invalid_segment"
        )
        return segment_id, global_sequence

    def _start(self, payload: dict[str, Any]) -> None:
        segment_id, global_sequence = self._segment_key(payload)
        audience_generation = _positive_integer(
            payload.get("audienceGeneration"), "invalid_audience"
        )
        audience_revision = _positive_integer(
            payload.get("audienceRevision"), "invalid_audience"
        )
        if global_sequence != self.last_global_sequence + 1:
            raise CoordinatorError("invalid_segment_order")
        # The composed caption sequence multiplies this by 64 and must
        # still fit the STT segment_id and the overlay's sequence bound.
        # A counter that climbs from 1 per adapter run never gets near
        # this; refusing here keeps the arithmetic honest anyway.
        if global_sequence > MAX_SEGMENT_ID // _SEQUENCE_SLOTS:
            raise CoordinatorError("invalid_segment")
        if segment_id in self.active or len(self.active) >= self.max_active_segments:
            raise CoordinatorError("active_segment_limit")
        speaker = _speaker(payload.get("speaker"))
        if speaker.user_id in self.active_by_speaker:
            raise CoordinatorError("speaker_overlap")
        audio_format = payload.get("format")
        if (
            not isinstance(audio_format, dict)
            or set(audio_format) != {"sampleRateHz", "channels", "encoding"}
            or isinstance(audio_format.get("sampleRateHz"), bool)
            or not isinstance(audio_format.get("sampleRateHz"), int)
            or audio_format.get("sampleRateHz") != 16_000
            or isinstance(audio_format.get("channels"), bool)
            or not isinstance(audio_format.get("channels"), int)
            or audio_format.get("channels") != 1
            or audio_format.get("encoding") != "pcm_s16le"
        ):
            raise CoordinatorError("invalid_audio_format")
        self.active[segment_id] = _ActiveSegment(
            segment_id=segment_id,
            global_sequence=global_sequence,
            audience_generation=audience_generation,
            audience_revision=audience_revision,
            speaker=speaker,
            started_at=self.clock(),
        )
        self.active_by_speaker[speaker.user_id] = segment_id
        self.last_global_sequence = global_sequence

    def _matching_segment(self, payload: dict[str, Any]) -> _ActiveSegment:
        segment_id, global_sequence = self._segment_key(payload)
        segment = self.active.get(segment_id)
        if segment is None or segment.global_sequence != global_sequence:
            raise CoordinatorError("unknown_segment")
        if (
            _positive_integer(
                payload.get("audienceGeneration"), "invalid_audience"
            )
            != segment.audience_generation
            or _positive_integer(
                payload.get("audienceRevision"), "invalid_audience"
            )
            != segment.audience_revision
        ):
            raise CoordinatorError("audience_mismatch")
        return segment

    MAX_DROPPED_TOMBSTONES = 128

    def _drop_segment(self, segment: "_ActiveSegment") -> None:
        """One misbehaving segment is wiped and forgotten, alone.

        The same judgement _end already applies to a failed segment: one
        speaker's bad audio is an honest per-speaker event, not a
        protocol violation, and raising here turned one oversize chunk
        into "capture pipeline stopped" for the whole raid.
        """
        self.active.pop(segment.segment_id, None)
        self.active_by_speaker.pop(segment.speaker.user_id, None)
        self.total_pcm_bytes -= len(segment.pcm)
        segment.wipe()
        self.dropped_segments[segment.segment_id] = segment.global_sequence
        while len(self.dropped_segments) > self.MAX_DROPPED_TOMBSTONES:
            self.dropped_segments.pop(next(iter(self.dropped_segments)))

    def _is_dropped(self, payload: dict[str, Any]) -> bool:
        segment_id, global_sequence = self._segment_key(payload)
        return self.dropped_segments.get(segment_id) == global_sequence

    def _chunk(self, payload: dict[str, Any]) -> CompletedSegment | None:
        if self._is_dropped(payload):
            return None
        segment = self._matching_segment(payload)
        chunk_sequence = _positive_integer(
            payload.get("chunkSequence"), "invalid_chunk_order"
        )
        if chunk_sequence != segment.next_chunk_sequence:
            raise CoordinatorError("invalid_chunk_order")
        encoded = payload.get("pcmBase64")
        if (
            not isinstance(encoded, str)
            or not encoded
            or len(encoded.encode("ascii", errors="replace")) > MAX_BASE64_CHUNK_BYTES
        ):
            self._drop_segment(segment)
            return None
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise CoordinatorError("invalid_chunk") from None
        if (
            not decoded
            or len(decoded) > MAX_PCM_CHUNK_BYTES
            or len(decoded) % 2
            or len(segment.pcm) + len(decoded) > MAX_PCM_BYTES
            or self.total_pcm_bytes + len(decoded) > self.max_total_pcm_bytes
        ):
            # Dropping the segment also releases every byte it was
            # holding, which is the relief valve the total budget needs.
            self._drop_segment(segment)
            return None
        segment.pcm.extend(decoded)
        self.total_pcm_bytes += len(decoded)
        segment.next_chunk_sequence += 1
        if (
            self.stream_flush_pcm_bytes
            and len(segment.pcm) >= self.stream_flush_pcm_bytes
            # Past the last partial slot the rest of the utterance waits
            # for the end frame. MAX_PCM_BYTES makes this unreachable at
            # the default threshold; it is here so a small threshold in a
            # test cannot overflow the slot arithmetic.
            and segment.parts_cut < _MAX_PART_INDEX - 1
        ):
            return self._cut_part(segment)
        return None

    def _cut_part(self, segment: _ActiveSegment) -> CompletedSegment:
        """Hand the buffered head to STT now; the tail stays live."""
        offset = _quiet_cut_offset(segment.pcm)
        if offset < MIN_TRANSCRIBE_PCM_BYTES:
            offset = len(segment.pcm)
        head = segment.pcm
        tail = bytearray(head[offset:])
        # The moved bytes are zeroed before the buffer is shortened so no
        # copy of the tail lingers in the part's allocation.
        head[offset:] = b"\x00" * (len(head) - offset)
        del head[offset:]
        segment.pcm = tail
        self.total_pcm_bytes -= offset
        segment.pcm_flushed += offset
        segment.parts_cut += 1
        return CompletedSegment(
            global_sequence=segment.global_sequence,
            audience_generation=segment.audience_generation,
            audience_revision=segment.audience_revision,
            speaker=segment.speaker,
            pcm=head,
            part_index=segment.parts_cut,
            last=False,
        )

    def _end(self, payload: dict[str, Any]) -> CompletedSegment | None:
        if self._is_dropped(payload):
            # The dropped segment's stream is over; the tombstone has
            # done its job and can go.
            self.dropped_segments.pop(self._segment_key(payload)[0], None)
            return None
        segment = self._matching_segment(payload)
        if _speaker(payload.get("speaker")) != segment.speaker:
            raise CoordinatorError("speaker_mismatch")
        status = payload.get("status")
        if not isinstance(status, str) or status not in {"completed", "failed"}:
            raise CoordinatorError("invalid_segment_status")
        chunks = _positive_integer(
            payload.get("chunks"), "invalid_segment_size", allow_zero=True
        )
        pcm_bytes = _positive_integer(
            payload.get("pcmBytes"), "invalid_segment_size", allow_zero=True
        )
        # Against everything the segment received, not just what is still
        # buffered. A long utterance is cut into parts as it streams and
        # each cut hands its head to speech and shortens the buffer, so
        # comparing the adapter's total against the tail alone failed
        # every utterance long enough to be cut - the session ended, the
        # bot left the voice channel, and the log said invalid_segment_size.
        # Live worker log, 2026-09-04.
        if (
            chunks != segment.next_chunk_sequence - 1
            or pcm_bytes != len(segment.pcm) + segment.pcm_flushed
        ):
            raise CoordinatorError("invalid_segment_size")

        self.active.pop(segment.segment_id)
        self.active_by_speaker.pop(segment.speaker.user_id, None)
        self.total_pcm_bytes -= len(segment.pcm)
        if segment.parts_cut:
            # Earlier parts of this utterance are already at STT and their
            # text may already be on screen as a partial. Whatever the
            # tail holds - a failed status, sub-100 ms of leftover, or a
            # real last phrase - the coordinator must learn the segment
            # is over so it can close the live line with a final. An
            # empty pcm says "nothing more to transcribe".
            tail = segment.pcm
            if status == "failed" or len(tail) < MIN_TRANSCRIBE_PCM_BYTES:
                segment.wipe()
                tail = bytearray()
            return CompletedSegment(
                global_sequence=segment.global_sequence,
                audience_generation=segment.audience_generation,
                audience_revision=segment.audience_revision,
                speaker=segment.speaker,
                pcm=tail,
                part_index=segment.parts_cut + 1,
                last=True,
            )
        if status == "failed":
            # The adapter reporting one segment failed is an honest
            # account of one speaker's bad audio, not a protocol
            # violation - the same class as the sub-100ms segments two
            # lines down, and it gets the same treatment: wiped and
            # dropped. Raising here turned one corrupt opus packet into
            # "capture pipeline stopped" for the whole raid, three
            # restarts in twenty minutes on a real machine. The protocol
            # violations above (speaker mismatch, sequence gaps) still
            # fail closed.
            segment.wipe()
            return None
        # Discord may open a valid speaking segment without delivering a usable
        # Opus packet. Empty and sub-100 ms segments are harmless noise, not a
        # protocol failure, and are wiped without invoking STT.
        if len(segment.pcm) < MIN_TRANSCRIBE_PCM_BYTES:
            segment.wipe()
            return None
        return CompletedSegment(
            global_sequence=segment.global_sequence,
            audience_generation=segment.audience_generation,
            audience_revision=segment.audience_revision,
            speaker=segment.speaker,
            pcm=segment.pcm,
        )

    def abort(self) -> None:
        for segment in self.active.values():
            segment.wipe()
        self.active.clear()
        self.active_by_speaker.clear()
        self.total_pcm_bytes = 0

    def check_deadlines(self) -> None:
        now = self.clock()
        if any(
            now - segment.started_at > self.max_segment_age_seconds
            for segment in self.active.values()
        ):
            self.abort()
            raise CoordinatorError("segment_timeout")


# The bot's own presence in the voice channel. Not session state - the
# session can be perfectly healthy across a reconnect - but the thing an
# operator watching Discord actually sees happen.
_VOICE_STATE_TYPES = frozenset({
    "voice.disconnected",
    "voice.reconnecting",
    "voice.reconnected",
})


def _session_event(envelope: AdapterEnvelope) -> SessionEvent | None:
    if envelope.event_type == "adapter.ready":
        return SessionEvent("ready")
    if envelope.event_type == "adapter.failed":
        return SessionEvent("error")
    if envelope.event_type == "adapter.stopped":
        return SessionEvent("stopped")
    # Session and health events already express stopped/paused state. A voice
    # disconnect commonly follows session.stopped and must not overwrite it.
    # Reported separately - see _VOICE_STATE_TYPES - so the operator can
    # still see the bot leaving and rejoining the channel.
    if envelope.event_type in _VOICE_STATE_TYPES:
        return None
    if envelope.event_type == "health.changed":
        if envelope.payload.get("component") != "capture_pipeline":
            return None
        status = envelope.payload.get("status")
        if status == "ready":
            return SessionEvent("ready")
        if status == "blocked":
            return SessionEvent("error")
        raise CoordinatorError("invalid_health")
    if envelope.event_type not in {
        "session.started",
        "session.stopped",
        "session.status",
        "session.participants.changed",
        "consent.granted",
        "consent.declined",
        "consent.forgotten",
    }:
        return None
    session = envelope.payload.get("session")
    # Withdrawing or forgetting consent must not require a live session, or the
    # only moment you could take it back would be the moment it is running. With
    # no session there is no phase for the overlay to show, so there is no state
    # for the overlay to change.
    if session is None and envelope.event_type in {
        "session.status",
        "consent.declined",
        "consent.forgotten",
    }:
        return None
    if not isinstance(session, dict):
        raise CoordinatorError("invalid_session")
    phase = session.get("phase")
    mapping = {
        "active": "active",
        "awaiting_consent": "paused",
        "paused": "paused",
        "stopped": "stopped",
    }
    if not isinstance(phase, str) or phase not in mapping:
        raise CoordinatorError("invalid_session")
    return SessionEvent(mapping[phase])


# Why a failure happened, in one word, safely.
#
# Shino, 2026-09-04: "vemals ears keeps crashing". The worker log said
# "capture pipeline stopped (transcription_failed)" ten times and
# "(adapter_failed)" twelve times over one evening, and that was the
# whole of what anyone could learn: the transcription path threw its
# cause away with `from None`, and the adapter's own reason word was
# never carried into the code. Both are appended now, so the next
# failure names itself.
#
# An ALLOWLIST, never a copy: a component's reason field is not trusted
# to be an enum word - test_coordinator.py feeds it a Discord user id to
# prove exactly that - so anything that is not a short lowercase
# identifier is dropped and the code stays as it was. Class names of
# exceptions are our own identifiers, so those pass a matching test.
_SAFE_REASON = re.compile(r"^[a-z][a-z0-9_]{0,48}$")
_SAFE_FAULT = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,48}$")


def _reason_word(payload: Any) -> str:
    value = payload.get("reason") if isinstance(payload, dict) else None
    if isinstance(value, str) and _SAFE_REASON.fullmatch(value):
        return value
    return ""


def _fault_word(error: BaseException) -> str:
    name = type(error).__name__
    return name if _SAFE_FAULT.fullmatch(name) else ""


def _failure_code(base: str, detail: str) -> str:
    return base + ":" + detail if detail else base


_SAFE_CLOSE_CODE = range(0, 10_000)


def _failure_detail(payload: Any) -> str:
    """The reason word plus what the adapter knew about a failed voice join.

    Fifty-two joins failed in a row on 2026-09-06 and every line said
    voice_connection_failed and nothing else, because nothing carried the
    connection's state across. Only enum-shaped words and a Discord close
    code pass; anything else in the payload is not a word for the log.
    """
    words = [_reason_word(payload)]
    if isinstance(payload, dict):
        state = payload.get("voiceState")
        if isinstance(state, str) and _SAFE_REASON.fullmatch(state.lower()):
            words.append(state.lower())
        code = payload.get("closeCode")
        if isinstance(code, int) and not isinstance(code, bool) and code in _SAFE_CLOSE_CODE:
            words.append(str(code))
        cause = payload.get("cause")
        if isinstance(cause, str) and _SAFE_FAULT.fullmatch(cause):
            words.append(cause)
    return ":".join(word for word in words if word)


def _is_expected_startup_block(envelope: AdapterEnvelope) -> bool:
    """Recognize the adapter's intentional pre-login health announcement."""
    return (
        envelope.event_type == "health.changed"
        and envelope.payload.get("component") == "capture_pipeline"
        and envelope.payload.get("status") == "blocked"
        and envelope.payload.get("reason") == "startup"
    )


class VoiceCaptionCoordinator:
    """Coordinate concurrent final captions without exposing private input."""

    def __init__(
        self,
        stt_service: TranscriptionService,
        event_sink: EventSink,
        *,
        clock: Callable[[], float] = time.time,
        identity_key: bytes | None = None,
        max_transcription_tasks: int | None = None,
        max_segment_age_seconds: float = MAX_SEGMENT_AGE_SECONDS,
        segment_clock: Callable[[], float] = time.monotonic,
        stream_flush_pcm_bytes: int = STREAM_FLUSH_PCM_BYTES,
        on_segment_dropped: Callable[[str], object] | None = None,
        on_voice_state: Callable[[str], object] | None = None,
    ) -> None:
        self.stt_service = stt_service
        self.event_sink = event_sink
        self.clock = clock
        self.segment_clock = segment_clock
        # The worker log only carries what voice_captions.status() prints,
        # so a skipped segment has to travel as a callback to reach the
        # operator; a LOGGER line would vanish. The forwarder built by
        # voice_captions carries it as segment_dropped, so the default
        # wiring is to look it up on the sink the same way the other
        # host controls are found. Absent both, it is a no-op.
        # The bot dropping out of the voice channel and coming back is
        # the single most visible thing Vemal's Ears does, and it was the
        # one thing the log never mentioned: these envelopes deliberately
        # produce no SESSION event (a disconnect commonly follows
        # session.stopped and must not overwrite it), so they vanished
        # entirely. Shino, 2026-09-04: "it keeps logging in and out of
        # channels and causing disruptions" - with no record of it. They
        # are reported here as their own line, still without touching
        # session state. Found the same way as the segment hook.
        if on_voice_state is None:
            on_voice_state = getattr(self.event_sink, "voice_state", None)
        self.on_voice_state = on_voice_state if callable(on_voice_state) else None
        if on_segment_dropped is None:
            on_segment_dropped = getattr(self.event_sink, "segment_dropped", None)
        self.on_segment_dropped = (
            on_segment_dropped if callable(on_segment_dropped) else None
        )
        self._drop_reported_at: dict[str, float] = {}
        if identity_key is None:
            self.identity_key = secrets.token_bytes(32)
        elif not isinstance(identity_key, (bytes, bytearray, memoryview)):
            raise TypeError("identity_key must be 32 bytes")
        elif len(identity_key) != 32:
            raise ValueError("identity_key must be exactly 32 bytes")
        else:
            self.identity_key = bytes(identity_key)
        service_limit = getattr(stt_service, "max_pending_segments", MAX_ACTIVE_SEGMENTS)
        self.max_transcription_tasks = (
            int(service_limit)
            if max_transcription_tasks is None
            else int(max_transcription_tasks)
        )
        if self.max_transcription_tasks < 1 or self.max_transcription_tasks > 32:
            raise ValueError("max_transcription_tasks must be between 1 and 32")
        self.decoder = AdapterProtocolDecoder()
        self.assembler = SegmentAssembler(
            max_segment_age_seconds=max_segment_age_seconds,
            clock=segment_clock,
            stream_flush_pcm_bytes=stream_flush_pcm_bytes,
        )
        self._tasks: set[asyncio.Task[None]] = set()
        self._segments_by_task: dict[asyncio.Task[None], CompletedSegment] = {}
        # Keyed by global_sequence, which is unique across every speaker.
        self._parts: dict[int, _SegmentParts] = {}
        self._speech_styles = SpeechStyleDetector()
        self._speech_style_speakers: set[str] | None = None
        self._speech_style_suppressed: set[str] = set()
        self._failure: CoordinatorError | None = None
        self._failure_event = asyncio.Event()
        self._closed = False
        self._shutdown_task: asyncio.Task[None] | None = None
        self._adapter_starting = False
        self._adapter_ready = False

    async def feed_line(
        self, raw_line: bytes | str
    ) -> QuickChatResult | None:
        if self._closed:
            raise CoordinatorError("coordinator_closed")
        self.raise_if_failed()
        try:
            self.assembler.check_deadlines()
            envelope = self.decoder.parse_line(raw_line)
            if envelope.event_type == "quickchat.result":
                # A malformed reply about ONE typed message is dropped, not
                # raised. Raising here stopped the whole capture pipeline,
                # so a bad acknowledgement about a line somebody typed
                # ended captions for everybody listening. The relay is
                # already built for a result that never arrives: it retries,
                # then gives up on that message alone.
                request_id = envelope.payload.get("requestId")
                status = envelope.payload.get("status")
                if (
                    set(envelope.payload) != {"requestId", "status"}
                    or not isinstance(request_id, str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", request_id)
                    or not isinstance(status, str)
                    or status not in {
                        "delivered",
                        "no_active_session",
                        "delivery_failed",
                    }
                ):
                    self._report_drop_reason("invalid_quick_chat_result")
                    return None
                return QuickChatResult(request_id, status)
            if envelope.event_type == "audience.snapshot":
                self._update_audience(envelope.payload)
                return None
            if envelope.event_type == "viewer.link.requested":
                self._link_viewer(envelope.payload)
            if envelope.event_type == "viewer.enroll.requested":
                self._enroll_viewer(envelope.payload)
                return None
            if envelope.event_type in _AUDIO_TYPES:
                completed = self.assembler.accept(envelope)
                if completed is not None:
                    self._schedule_transcription(completed)
                return None
            if envelope.event_type == "adapter.starting":
                self._adapter_starting = True
            if _is_expected_startup_block(envelope):
                if not self._adapter_starting or self._adapter_ready:
                    raise CoordinatorError("invalid_health")
                # The Node adapter deliberately reports capture as blocked
                # before Discord login completes. This is a startup state,
                # not a failed source. A later adapter.failed or non-startup
                # blocked health event still fails closed below.
                return
            if envelope.event_type == "adapter.ready":
                self._adapter_starting = False
                self._adapter_ready = True
            if envelope.event_type in _VOICE_STATE_TYPES:
                self._report_voice_state(envelope.event_type)
            if envelope.event_type in {
                "consent.declined",
                "consent.forgotten",
            }:
                user_id = envelope.payload.get("userId")
                if isinstance(user_id, str) and _USER_ID.fullmatch(user_id):
                    speaker_id = self._speaker_token(user_id)
                    self._speech_styles.forget(speaker_id)
                    if (
                        self._speech_style_speakers is not None
                        and speaker_id in self._speech_style_speakers
                    ):
                        # An STT task already in flight can finish after the
                        # withdrawal event. Keep it from recreating the
                        # derived baseline until this person grants again.
                        self._speech_style_suppressed.add(speaker_id)
            elif envelope.event_type == "consent.granted":
                user_id = envelope.payload.get("userId")
                if isinstance(user_id, str) and _USER_ID.fullmatch(user_id):
                    self._speech_style_suppressed.discard(
                        self._speaker_token(user_id)
                    )
            event = _session_event(envelope)
            if event is not None:
                if event.state == "stopped":
                    self._speech_styles.clear()
                    self._speech_style_speakers = set()
                    self._speech_style_suppressed.clear()
                self._emit(event)
            capture_blocked = (
                envelope.event_type == "health.changed"
                and envelope.payload.get("component") == "capture_pipeline"
                and envelope.payload.get("status") == "blocked"
            )
            if envelope.event_type == "adapter.failed" or capture_blocked:
                raise CoordinatorError(
                    _failure_code("adapter_failed", _failure_detail(envelope.payload))
                )
            return None
        except CoordinatorError:
            await self.shutdown()
            raise

    def _schedule_transcription(self, completed: CompletedSegment) -> None:
        if len(self._tasks) >= self.max_transcription_tasks:
            completed.wipe()
            raise CoordinatorError("transcription_task_limit")
        task = asyncio.create_task(self._transcribe(completed))
        self._tasks.add(task)
        self._segments_by_task[task] = completed
        task.add_done_callback(self._task_done)

    def _speaker_token(self, user_id: str) -> str:
        return hmac.new(
            self.identity_key,
            user_id.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()[:24]

    def _update_audience(self, payload: dict[str, Any]) -> None:
        if set(payload) != {"generation", "revision", "participantIds"}:
            raise CoordinatorError("invalid_audience")
        generation = _positive_integer(payload.get("generation"), "invalid_audience")
        revision = _positive_integer(payload.get("revision"), "invalid_audience")
        participant_ids = payload.get("participantIds")
        if (
            not isinstance(participant_ids, list)
            or len(participant_ids) > 512
            or any(
                not isinstance(user_id, str) or not _USER_ID.fullmatch(user_id)
                for user_id in participant_ids
            )
            or len(set(participant_ids)) != len(participant_ids)
        ):
            raise CoordinatorError("invalid_audience")
        tokens = tuple(self._speaker_token(user_id) for user_id in participant_ids)
        self._speech_styles.retain(tokens)
        self._speech_style_speakers = set(tokens)
        self._speech_style_suppressed.intersection_update(tokens)
        update = getattr(self.event_sink, "update_audience", None)
        if not callable(update):
            return
        try:
            updated = update(generation, revision, tokens)
        except Exception:
            updated = False
        if updated is not True:
            raise CoordinatorError("audience_update_failed")

    def _enroll_viewer(self, payload: dict[str, Any]) -> None:
        """Bind a Discord-issued pairing invitation to the person who asked.

        Shino, 2026-09-01: captions should follow voice presence with no
        separate linking step. The identity has to come from somewhere, so
        it comes from the invitation: the adapter shows the person a
        pairing link privately, and this hands the relay the same code with
        the requester's viewer token attached. The device that claims it is
        theirs from the first second.
        """
        if set(payload) != {"userId", "code", "ttlSeconds"}:
            raise CoordinatorError("invalid_viewer_enrollment")
        user_id = payload.get("userId")
        code = payload.get("code")
        ttl = payload.get("ttlSeconds")
        if (
            not isinstance(user_id, str)
            or not _USER_ID.fullmatch(user_id)
            or not isinstance(code, str)
            or not re.fullmatch(r"ghe_[A-Za-z0-9_-]{32,64}", code)
            or isinstance(ttl, bool)
            or not isinstance(ttl, int)
            or not 0 < ttl <= 7 * 24 * 60 * 60
        ):
            raise CoordinatorError("invalid_viewer_enrollment")
        enroll = getattr(self.event_sink, "enroll_viewer", None)
        if not callable(enroll):
            # Never a silent return. A person was just shown a link, and a
            # sink with no way to carry it means the link is already dead;
            # the worker log must say so rather than the app saying
            # "rejected" twenty seconds later with nothing behind it.
            raise CoordinatorError("viewer_enrollment_unsupported")
        try:
            created = enroll(code, self._speaker_token(user_id), ttl)
        except Exception:
            created = False
        if created is not True:
            raise CoordinatorError("viewer_enrollment_failed")

    def _link_viewer(self, payload: dict[str, Any]) -> None:
        if set(payload) != {"userId", "code"}:
            raise CoordinatorError("invalid_viewer_link")
        user_id = payload.get("userId")
        code = payload.get("code")
        if (
            not isinstance(user_id, str)
            or not _USER_ID.fullmatch(user_id)
            or not isinstance(code, str)
            or not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{8}", code)
        ):
            raise CoordinatorError("invalid_viewer_link")
        link = getattr(self.event_sink, "link_viewer", None)
        if not callable(link):
            return
        try:
            linked = link(code, self._speaker_token(user_id))
        except Exception:
            linked = False
        if linked is not True:
            raise CoordinatorError("viewer_link_failed")

    async def _transcribe(self, completed: CompletedSegment) -> None:
        audio: AudioSegment | None = None
        try:
            speaker_id = self._speaker_token(completed.speaker.user_id)
            text = ""
            level_dbfs = speech_level_dbfs(completed.pcm)
            # An empty last part is the assembler saying the tail after
            # the final cut was too short to be speech; nothing to send.
            if completed.pcm:
                audio = AudioSegment(
                    speaker_id=speaker_id,
                    segment_id=completed.sequence,
                    pcm=completed.pcm,
                    hints=completed.speaker.hints,
                )
                try:
                    result: TranscriptResult = await self.stt_service.transcribe(audio)
                except _PER_SEGMENT_STT_FAULT_TYPES as fault:
                    # One part is lost; it counts as "" so a streamed
                    # segment still gets its final. Before this, any
                    # STT exception became transcription_failed, the
                    # watch loop tore the whole stack down - overlay,
                    # two model loads, a Node start, the bot leaving the
                    # channel - and rebuilt it on a 2..60 s backoff. The
                    # live worker log shows it happening for one 18 s
                    # timeout. BackendUnavailable is not in the tuple and
                    # still fails closed below.
                    self._report_dropped(fault)
                else:
                    if not isinstance(result.text, str):
                        # One malformed reply from the speech backend is
                        # one segment's misfortune, exactly like the
                        # faults above - "whisper-server sent one bad
                        # reply" is already on that list. It used to take
                        # the whole session down instead: the live worker
                        # log for 2026-09-04 shows a raid losing captions
                        # and the bot leaving the channel over a single
                        # transcript whose text was not a string.
                        self._report_drop_reason("invalid_transcript")
                    else:
                        text = result.text
            self._complete_part(completed, speaker_id, text, level_dbfs)
        except asyncio.CancelledError:
            raise
        except CoordinatorError:
            # Ours already, and already carrying the word that says what
            # happened - invalid_transcript, overlay_unavailable. It is a
            # RuntimeError, so the catch-all below swallowed it and
            # relabelled it "transcription_failed", which is how the live
            # log came to read "transcription_failed:CoordinatorError":
            # the one useful word replaced by the name of its own wrapper.
            raise
        except Exception as fault:
            # The class name only. Its message can carry a path, a URL or
            # a fragment of what was said, and none of that belongs in a
            # log line the operator reads out loud.
            raise CoordinatorError(
                _failure_code("transcription_failed", _fault_word(fault))
            ) from None
        finally:
            if audio is not None:
                audio.wipe()
            else:
                completed.wipe()

    def _report_voice_state(self, event_type: str) -> None:
        """Say the bot left or rejoined; never let it cost the pipeline."""
        if self.on_voice_state is None:
            return
        try:
            self.on_voice_state(event_type)
        except Exception:  # noqa: BLE001 - reporting is never worth a session
            pass

    def _report_dropped(self, fault: Exception) -> None:
        """Tell the host one part was skipped - one line per reason per 30 s.

        Reasons are enum words from _PER_SEGMENT_STT_FAULTS, never audio
        or text. A busy raid can drop a part every few seconds under a
        full queue; without the throttle the log would be nothing but
        this line.
        """
        self._report_drop_reason(next(
            code for cls, code in _PER_SEGMENT_STT_FAULTS if isinstance(fault, cls)
        ))

    def _report_drop_reason(self, reason: str) -> None:
        """One skipped part, by name, throttled per reason."""
        if self.on_segment_dropped is None:
            return
        now = self.segment_clock()
        last = self._drop_reported_at.get(reason)
        if last is not None and now - last < SEGMENT_DROP_REPORT_INTERVAL_SECONDS:
            return
        self._drop_reported_at[reason] = now
        try:
            self.on_segment_dropped(reason)
        except Exception:
            # A status line that cannot be printed must not cost the
            # segment that survived.
            pass

    def _complete_part(
        self,
        completed: CompletedSegment,
        speaker_id: str,
        text: str,
        level_dbfs: float | None,
    ) -> None:
        parts = self._parts.get(completed.global_sequence)
        if parts is None:
            parts = self._parts[completed.global_sequence] = _SegmentParts()
        parts.fill(
            completed.part_index,
            text,
            level_dbfs=level_dbfs,
            last=completed.last,
        )
        if parts.complete:
            self._parts.pop(completed.global_sequence, None)
            final = True
            prefix = parts.total or 0
            first_new = 0
        else:
            final = False
            prefix = parts.known_prefix()
            if prefix <= parts.shown or prefix > _MAX_PART_INDEX:
                return
            # The live line shows what is NEW. It used to show every
            # part so far joined, so a long utterance grew on screen five
            # seconds at a time - Shino, 2026-09-06: "the spliced text
            # keeps appending rather than just showing the new streamed
            # text". The whole utterance still arrives once, as the
            # final, which is the line that stays.
            first_new = parts.shown
            parts.shown = prefix
        # Names are corrected after the join and before the cap: a name can
        # be split across two streamed parts, so the halves are only adjacent
        # once the whole line exists, and a correction that changes the length
        # has to happen before anything measures that length.
        # Nothing to say is not a fault. _safe_text_body refuses an empty
        # string - correct for a speaker's NAME, where empty is a protocol
        # violation - and this call shared it, so every segment that came
        # out empty raised invalid_transcript and took the whole capture
        # pipeline down with it: the bot left the voice channel and every
        # member lost captions. Empty is ordinary here. It is the tail the
        # assembler says was too short to be speech, and it is what a
        # skipped part leaves behind - the "one part is lost; it counts as
        # empty" tolerance a few lines up could never actually work while
        # this raised. Live worker log, 2026-09-04: sessions dying seconds
        # after someone spoke, over and over.
        joined = apply_pronunciations(_join_parts(parts.slots[first_new:prefix], 320))
        if not isinstance(joined, str) or not joined.strip():
            return
        # Everything else still fails closed: a non-string, or control
        # characters, is a real fault and is refused as before.
        transcript = _safe_transcript(joined, 320, "invalid_transcript")
        if not transcript:
            return
        if parts.speech_style is None:
            utterance_level = next(
                (
                    level
                    for text, level in zip(
                        parts.slots[:prefix],
                        parts.levels[:prefix],
                    )
                    if text and text.strip() and level is not None
                ),
                None,
            )
            style_allowed = (
                speaker_id not in self._speech_style_suppressed
                and (
                    self._speech_style_speakers is None
                    or speaker_id in self._speech_style_speakers
                )
            )
            parts.speech_style = (
                self._speech_styles.classify_and_observe(
                    speaker_id,
                    utterance_level,
                )
                if style_allowed
                else NORMAL_STYLE
            )
        self._emit(
            CaptionEvent(
                speaker_id=speaker_id,
                speaker=completed.speaker.display_name[:48],
                character=completed.speaker.character_name[:48],
                role=completed.speaker.overlay_role,
                text=transcript,
                final=final,
                sequence=completed.global_sequence * _SEQUENCE_SLOTS
                + (_FINAL_SLOT if final else prefix),
                timestamp=self.clock(),
                wow_class=completed.speaker.wow_class,
                speech_style=parts.speech_style,
                audience_generation=completed.audience_generation,
                audience_revision=completed.audience_revision,
            )
        )

    def _emit(self, event: ProtocolEvent) -> None:
        try:
            sent = self.event_sink(event)
        except Exception:
            sent = False
        if sent is not True:
            raise CoordinatorError("overlay_unavailable")

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        completed = self._segments_by_task.pop(task, None)
        if completed is not None:
            completed.wipe()
        if task.cancelled():
            return
        try:
            error = task.exception()
        except Exception:
            error = CoordinatorError("transcription_failed")
        if error is not None and self._failure is None:
            self._failure = (
                error
                if isinstance(error, CoordinatorError)
                else CoordinatorError(
                    _failure_code("transcription_failed", _fault_word(error))
                )
            )
            self._failure_event.set()

    def raise_if_failed(self) -> None:
        if self._failure is not None:
            raise self._failure

    def check_deadlines(self) -> None:
        self.raise_if_failed()
        self.assembler.check_deadlines()

    async def wait_for_failure(self) -> None:
        await self._failure_event.wait()
        self.raise_if_failed()

    async def drain(self) -> None:
        if self.assembler.active:
            self.assembler.abort()
            raise CoordinatorError("truncated_segment")
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        self.raise_if_failed()

    async def shutdown(self) -> None:
        if self._shutdown_task is None:
            self._closed = True
            self._shutdown_task = asyncio.create_task(self._cleanup())
        await asyncio.shield(self._shutdown_task)

    async def _cleanup(self) -> None:
        self.assembler.abort()
        tasks = tuple(self._tasks)
        completed_segments = tuple(self._segments_by_task.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for completed in completed_segments:
            completed.wipe()
        self._segments_by_task.clear()
        self._parts.clear()
        self._speech_styles.clear()
        self._speech_style_speakers = set()
        self._speech_style_suppressed.clear()
        await self.stt_service.close()


NodeProbe = Callable[[Path, Path], tuple[str, bool]]


def _probe_node(node: Path, adapter_root: Path) -> tuple[str, bool]:
    probe_environment = {
        name: os.environ[name]
        for name in _SAFE_CHILD_ENV
        if name in os.environ and isinstance(os.environ[name], str)
    }
    try:
        version = subprocess.run(
            [str(node), "--version"],
            cwd=str(adapter_root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="ascii",
            errors="replace",
            timeout=5,
            check=False,
            env=probe_environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        probe = subprocess.run(
            [
                str(node),
                "--input-type=module",
                "--eval",
                (
                    "await import('discord.js');"
                    "await import('@discordjs/voice');"
                    "await import('@snazzah/davey');"
                    "await import('opusscript');"
                ),
            ],
            cwd=str(adapter_root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
            env=probe_environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return "", False
    return version.stdout.strip() if version.returncode == 0 else "", probe.returncode == 0


def detect_node_adapter(
    adapter_root: Path = ADAPTER_ROOT,
    *,
    node_path: Path | None = None,
    probe: NodeProbe = _probe_node,
) -> NodeAdapterCapability:
    adapter_root = Path(adapter_root)
    if node_path is None:
        found = shutil.which("node") or shutil.which("node.exe")
        node_path = Path(found) if found else None
    if node_path is None or not Path(node_path).is_file():
        return NodeAdapterCapability(False, "Node.js is not installed", None, adapter_root)
    required = (
        adapter_root / "src" / "index.js",
        adapter_root / "package-lock.json",
        adapter_root / "node_modules" / "discord.js" / "package.json",
        adapter_root / "node_modules" / "@discordjs" / "voice" / "package.json",
        adapter_root / "node_modules" / "@snazzah" / "davey" / "package.json",
        adapter_root / "node_modules" / "opusscript" / "package.json",
    )
    if not all(path.is_file() for path in required):
        return NodeAdapterCapability(
            False, "Discord adapter dependencies are not installed", Path(node_path), adapter_root
        )
    version_text, imports_ready = probe(Path(node_path), adapter_root)
    match = _NODE_VERSION.fullmatch(version_text)
    if not match or tuple(int(value) for value in match.groups()) < MIN_NODE_VERSION:
        return NodeAdapterCapability(
            False, "Node.js 22.12.0 or newer is required", Path(node_path), adapter_root
        )
    if not imports_ready:
        return NodeAdapterCapability(
            False, "Discord adapter dependencies failed their local check", Path(node_path), adapter_root
        )
    return NodeAdapterCapability(
        True, "Discord adapter is ready", Path(node_path).resolve(), adapter_root.resolve()
    )


def node_adapter_command(capability: NodeAdapterCapability) -> tuple[str, str]:
    if not capability.available or capability.node is None:
        raise CoordinatorError("node_unavailable")
    return (
        str(capability.node),
        str(capability.adapter_root / "src" / "index.js"),
    )


def public_pairing_address(source: Mapping[str, str] | None = None) -> str:
    """Where a pairing invitation should point, or "" if nothing says.

    /vem setup and /vem invite hand out a link, so the adapter needs
    the guild's public address. It only ever runs on the guild host, and
    that host already records the address in relay.env - so the honest
    default is to read it from there rather than ask anyone to configure
    the same value twice. An explicit CAPTIONS_JOIN_BASE_URL in the
    environment wins, for a host running behind a different name.
    """
    source = os.environ if source is None else source
    for name in ("CAPTIONS_JOIN_BASE_URL", "VEMALS_EARS_PUBLIC_HTTP_URL"):
        value = str(source.get(name, "") or "").strip()
        if value.startswith("https://"):
            return value.rstrip("/")
    try:
        from GuildHub import relay_env

        value = str(relay_env.load().get("VEMALS_EARS_PUBLIC_HTTP_URL", "") or "").strip()
    except Exception:
        # A member bundle has no relay env, and no reason to build links.
        return ""
    return value.rstrip("/") if value.startswith("https://") else ""


def build_node_environment(
    credentials: DiscordCredentials,
    base_environment: dict[str, str] | None = None,
) -> dict[str, str]:
    checked = validate_credentials(
        credentials.application_id,
        credentials.bot_token,
    )
    source = os.environ if base_environment is None else base_environment
    environment = {
        name: source[name]
        for name in _SAFE_CHILD_ENV
        if name in source and isinstance(source[name], str)
    }
    join_base = public_pairing_address(source)
    if join_base:
        environment["CAPTIONS_JOIN_BASE_URL"] = join_base
    guild_id = source.get("DISCORD_GUILD_ID", "").strip()
    if guild_id and _SNOWFLAKE.fullmatch(guild_id):
        environment["DISCORD_GUILD_ID"] = guild_id
    def state_path(env_name: str, default_path) -> str:
        # A caller-supplied path is honoured after validation - the guild
        # host mounts its own state volume - and the companion default
        # fills the gap for the local pipeline. The default must never
        # clobber a validated override: that is precisely the bug the
        # merge of these two behaviours briefly had.
        raw = source.get(env_name, "")
        value = raw.strip()
        if raw:
            if (
                raw != value
                or not value
                or len(value.encode("utf-8")) > MAX_CONSENT_STATE_PATH_BYTES
                or not os.path.isabs(value)
                or any(ord(character) < 32 for character in value)
            ):
                raise CoordinatorError(
                    "invalid_" + env_name.removeprefix("CAPTIONS_").lower()
                )
            return value
        return str(default_path)

    environment.update(
        {
            "DISCORD_TOKEN": checked.bot_token,
            "DISCORD_APPLICATION_ID": checked.application_id,
            "CAPTIONS_MAX_SEGMENT_MS": "30000",
            # Without these the adapter builds memory-only ledgers, so consent
            # choices and character mappings disappear on a process restart.
            CONSENT_STATE_PATH_ENV: state_path(
                CONSENT_STATE_PATH_ENV, default_consent_state_path()
            ),
            MAPPING_STATE_PATH_ENV: state_path(
                MAPPING_STATE_PATH_ENV, default_mapping_state_path()
            ),
        }
    )
    return environment


async def spawn_node_adapter(
    capability: NodeAdapterCapability,
    credentials: DiscordCredentials,
) -> asyncio.subprocess.Process:
    command = node_adapter_command(capability)
    environment = build_node_environment(credentials)
    try:
        return await asyncio.create_subprocess_exec(
            *command,
            cwd=str(capability.adapter_root),
            # A pipe rather than DEVNULL so closing it is a shutdown request
            # the adapter can see. It also carries bounded quick-chat
            # commands; credentials still arrive only in the environment.
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            # Inherited, not discarded. The adapter's stderr is its only
            # diagnostic channel, and its README requires that credentials
            # and audio never appear there - so the one thing throwing it
            # away buys is silence. It cost an evening: the worker looped
            # 530 times in an hour reporting "source is unavailable", the
            # public wording for any adapter error, with the actual reason
            # sent to DEVNULL every single time.
            stderr=None,
            env=environment,
            limit=MAX_ADAPTER_LINE_BYTES + 1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        raise CoordinatorError("adapter_start_failed") from None
    finally:
        environment["DISCORD_TOKEN"] = ""
        environment.clear()


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    # Ask before compelling. terminate() on Windows is TerminateProcess,
    # which delivers no signal and skips the adapter's final ledger save and
    # synced run-marker cleanup. Closing stdin is a shutdown request Node can
    # observe on every platform.
    if process.stdin is not None and not process.stdin.is_closing():
        try:
            process.stdin.close()
            await asyncio.wait_for(process.wait(), timeout=3.0)
            return
        except (asyncio.TimeoutError, OSError, ProcessLookupError):
            pass
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            return
        await process.wait()


def _node_quick_chat_command(command: object) -> tuple[str, str]:
    """Encode one trusted relay command for the private Node stdin pipe.

    The returned request ID is kept separately so a pipe failure can be
    acknowledged without ever putting the message body in an exception.
    """
    request_id = getattr(command, "request_id", None)
    if (
        not isinstance(request_id, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", request_id)
    ):
        raise CoordinatorError("invalid_quick_chat_command")
    speaker = _safe_text(
        getattr(command, "speaker", None), 32, "invalid_quick_chat_command"
    )
    text = _safe_text(
        getattr(command, "text", None), 320, "invalid_quick_chat_command"
    )
    wow_class = getattr(command, "wow_class", "")
    if not isinstance(wow_class, str) or (
        wow_class and wow_class not in _WOW_CLASSES
    ):
        raise CoordinatorError("invalid_quick_chat_command")
    encoded = json.dumps(
        {
            "version": 1,
            "type": "quick_chat.command",
            "requestId": request_id,
            "speaker": speaker,
            "wowClass": wow_class,
            "text": text,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ) + "\n"
    if len(encoded.encode("utf-8")) > MAX_NODE_COMMAND_BYTES:
        raise CoordinatorError("invalid_quick_chat_command")
    return request_id, encoded


async def _forward_quick_chat_commands(
    process: asyncio.subprocess.Process,
    source,
    complete,
) -> None:
    """Move a bounded batch of typed lines into Node without blocking audio."""
    if not callable(source):
        return
    for _attempt in range(8):
        command = source()
        if command is None:
            return
        request_id = getattr(command, "request_id", "")
        try:
            request_id, encoded = _node_quick_chat_command(command)
            if process.stdin is None or process.stdin.is_closing():
                raise OSError("adapter input unavailable")
            process.stdin.write(encoded.encode("utf-8"))
            drain = getattr(process.stdin, "drain", None)
            if callable(drain):
                await drain()
        except (CoordinatorError, OSError, RuntimeError):
            if callable(complete) and isinstance(request_id, str) and request_id:
                complete(request_id, "delivery_failed")


async def run_node_capture(
    credentials: DiscordCredentials,
    capability: NodeAdapterCapability,
    stt_service: TranscriptionService,
    event_sink: EventSink,
    stop_event,
    *,
    spawner: Callable[
        [NodeAdapterCapability, DiscordCredentials],
        Awaitable[asyncio.subprocess.Process],
    ] = spawn_node_adapter,
    coordinator_factory: Callable[
        [TranscriptionService, EventSink], VoiceCaptionCoordinator
    ] = VoiceCaptionCoordinator,
) -> None:
    """Run the private pipeline until stopped or any component fails closed."""
    coordinator = coordinator_factory(stt_service, event_sink)
    quick_chat_source = getattr(event_sink, "next_quick_chat", None)
    quick_chat_complete = getattr(event_sink, "complete_quick_chat", None)
    process: asyncio.subprocess.Process | None = None
    read_task: asyncio.Task[bytes] | None = None
    failure_task: asyncio.Task[None] | None = None
    try:
        await stt_service.start()
        process = await spawner(capability, credentials)
        if process.stdout is None:
            raise CoordinatorError("adapter_output_unavailable")
        failure_task = asyncio.create_task(coordinator.wait_for_failure())
        while not stop_event.is_set():
            coordinator.check_deadlines()
            await _forward_quick_chat_commands(
                process, quick_chat_source, quick_chat_complete
            )
            sink_alive = getattr(event_sink, "alive", None)
            if callable(sink_alive) and sink_alive() is not True:
                raise CoordinatorError("overlay_unavailable")
            if read_task is None:
                read_task = asyncio.create_task(process.stdout.readline())
            done, _ = await asyncio.wait(
                {read_task, failure_task},
                timeout=0.2,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if failure_task in done:
                await failure_task
            if read_task not in done:
                continue
            try:
                line = await read_task
            except (ValueError, asyncio.LimitOverrunError):
                raise CoordinatorError("line_limit") from None
            read_task = None
            if not line:
                await coordinator.drain()
                await process.wait()
                if not stop_event.is_set():
                    raise CoordinatorError("adapter_stopped")
                return
            result = await coordinator.feed_line(line)
            if result is not None and callable(quick_chat_complete):
                quick_chat_complete(result.request_id, result.status)
            coordinator.raise_if_failed()
    finally:
        try:
            if read_task is not None:
                read_task.cancel()
                await asyncio.gather(read_task, return_exceptions=True)
            if failure_task is not None:
                failure_task.cancel()
                await asyncio.gather(failure_task, return_exceptions=True)
        finally:
            try:
                if process is not None:
                    await _stop_process(process)
            finally:
                await coordinator.shutdown()
