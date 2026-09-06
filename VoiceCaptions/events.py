"""Validated caption events and the compact caption display model.

The Discord receiver, transcriber, and overlay are separate processes. They
only exchange JSON lines, and this module is the narrow contract between
them. Transcript text stays in memory and is never written here.
"""

from __future__ import annotations

import json
import math
import re
from collections import deque
from dataclasses import dataclass
from typing import Any

from .prosody import NORMAL_STYLE, SPEECH_STYLES

MAX_EVENT_BYTES = 16_384
MAX_SPEAKER_ID = 96
MAX_NAME = 48
MAX_TEXT = 320
# The most caption cards the overlay will ever stack. Lives here, the
# lowest module, so layout and the overlay can share it without an import
# cycle (layout imports events, not the other way round). Twenty is well
# past anything readable at a glance - it exists for whoever wants it, and
# the overlay clamps its own height to the screen so a tall stack cannot
# run off the top.
MAX_OVERLAY_ROWS = 20
MAX_SEQUENCE = 9_007_199_254_740_991

ROLE_COLORS = {
    "HEALER": "#4fd182",
    "TANK": "#4b8fe8",
    "DAMAGE": "#d9935b",
    "UNKNOWN": "#879cb3",
}

# Blizzard's own class colours, which every WoW player already reads
# without thinking. A raider recognises a name in Rogue yellow faster than
# they can read the name itself, and for someone whose only channel is
# this overlay that recognition is worth having.
#
# Keys match the adapter's WOW_CLASSES exactly; an unknown value falls
# back to the role colour rather than guessing.
CLASS_COLORS = {
    "death-knight": "#c41e3a",
    "demon-hunter": "#a330c9",
    "druid": "#ff7c0a",
    "evoker": "#33937f",
    "hunter": "#aad372",
    "mage": "#3fc7eb",
    "monk": "#00ff98",
    "paladin": "#f48cba",
    "priest": "#ffffff",
    "rogue": "#fff468",
    "shaman": "#0070dd",
    "warlock": "#8788ee",
    "warrior": "#c69b6d",
}

_SPACE = re.compile(r"\s+")


class CaptionProtocolError(ValueError):
    """An input line is not a safe caption protocol event."""


def _clean_text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise CaptionProtocolError(field + " must be text")
    # Preserve word boundaries carried by normal whitespace, but drop other
    # terminal/control characters before this reaches a log or Tk.
    clean = "".join(
        " " if char in "\t\r\n" else char
        for char in value
        if char >= " " or char in "\t\r\n"
        if char != "\x7f"
    )
    clean = _SPACE.sub(" ", clean).strip()
    if not clean:
        raise CaptionProtocolError(field + " cannot be empty")
    if len(clean) > limit:
        clean = clean[:limit].rstrip()
    return clean


def _finite_time(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CaptionProtocolError("timestamp must be a number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise CaptionProtocolError("timestamp must be finite and non-negative")
    return result


def _sequence(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CaptionProtocolError("sequence must be an integer")
    if value < 0 or value > MAX_SEQUENCE:
        raise CaptionProtocolError("sequence is out of range")
    return value


@dataclass(frozen=True)
class CaptionEvent:
    speaker_id: str
    speaker: str
    character: str
    role: str
    text: str
    final: bool
    sequence: int
    timestamp: float
    wow_class: str = ""
    speech_style: str = NORMAL_STYLE
    # Private relay routing metadata. It is stamped at speech start and is
    # deliberately omitted by event_to_json, so a viewer can never inspect or
    # replay another audience revision.
    audience_generation: int = 0
    audience_revision: int = 0

    @property
    def display_name(self) -> str:
        return self.character or self.speaker

    @property
    def color(self) -> str:
        """The card's accent: role, because that is what focus mode filters
        on and what the reader is deciding by."""
        return ROLE_COLORS[self.role]

    @property
    def name_color(self) -> str:
        """The speaker's own colour: their class, when they have mapped one.

        Falls back to the role colour, so an unmapped speaker looks exactly
        as they did before rather than turning some default grey.
        """
        return CLASS_COLORS.get(self.wow_class, self.color)


@dataclass(frozen=True)
class SessionEvent:
    state: str
    message: str = ""


@dataclass(frozen=True)
class ClearEvent:
    pass


ProtocolEvent = CaptionEvent | SessionEvent | ClearEvent


def parse_event(payload: Any) -> ProtocolEvent:
    """Validate one already-decoded protocol object."""
    if not isinstance(payload, dict):
        raise CaptionProtocolError("event must be an object")
    event_type = payload.get("type")
    if event_type == "caption":
        final = payload.get("final")
        if not isinstance(final, bool):
            raise CaptionProtocolError("final must be true or false")
        role = str(payload.get("role", "UNKNOWN")).upper()
        if role == "DPS":
            role = "DAMAGE"
        if role not in ROLE_COLORS:
            role = "UNKNOWN"
        speaker = _clean_text(payload.get("speaker", "Unknown"), "speaker", MAX_NAME)
        character_value = payload.get("character", "")
        character = ""
        if character_value:
            character = _clean_text(character_value, "character", MAX_NAME)
        # Anything not in the table reads as unmapped, which falls back to
        # the role colour. An older host simply omits the field.
        wow_class = str(payload.get("wow_class", "") or "").lower()
        if wow_class not in CLASS_COLORS:
            wow_class = ""
        speech_style = payload.get("speech_style", NORMAL_STYLE)
        if not isinstance(speech_style, str) or speech_style not in SPEECH_STYLES:
            raise CaptionProtocolError("speech_style is invalid")
        return CaptionEvent(
            speaker_id=_clean_text(
                payload.get("speaker_id"), "speaker_id", MAX_SPEAKER_ID
            ),
            speaker=speaker,
            character=character,
            role=role,
            text=_clean_text(payload.get("text"), "text", MAX_TEXT),
            final=final,
            sequence=_sequence(payload.get("sequence")),
            timestamp=_finite_time(payload.get("timestamp")),
            wow_class=wow_class,
            speech_style=speech_style,
        )
    if event_type == "session":
        state = _clean_text(payload.get("state"), "state", 32).lower()
        if state not in {
            "active",
            "paused",
            "ready",
            "stopped",
            "error",
            # Receiver-local conservative state: the guild connection is
            # unavailable, so remote capture may still be active.
            "unknown",
        }:
            raise CaptionProtocolError("unknown session state")
        message_value = payload.get("message", "")
        message = ""
        if message_value:
            message = _clean_text(message_value, "message", 160)
        return SessionEvent(state=state, message=message)
    if event_type == "clear":
        return ClearEvent()
    raise CaptionProtocolError("unknown event type")


def parse_event_line(line: str) -> ProtocolEvent:
    if not isinstance(line, str):
        raise CaptionProtocolError("event line must be text")
    if len(line.encode("utf-8", errors="replace")) > MAX_EVENT_BYTES:
        raise CaptionProtocolError("event line is too large")
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as error:
        raise CaptionProtocolError("event is not valid JSON") from error
    return parse_event(payload)


@dataclass
class DisplayLine:
    event: CaptionEvent
    updated_at: float
    first_shown_at: float | None = None

    @property
    def live(self) -> bool:
        return not self.event.final


class CaptionBoard:
    """Memory-only state for a compact overlay.

    Live partials always take priority. Final clauses wait in a short FIFO
    until a row is free, which prevents a third simultaneous speaker from
    expanding the overlay across encounter mechanics.
    """

    def __init__(
        self,
        max_rows: int = 2,
        partial_stale_seconds: float | None = None,
        final_hold_seconds: float = 3.6,
        queued_final_seconds: float = 8.0,
        max_queued_finals: int = 24,
    ) -> None:
        if max_rows < 1 or max_rows > MAX_OVERLAY_ROWS:
            raise ValueError("max_rows must be between 1 and %d" % MAX_OVERLAY_ROWS)
        self.max_rows = max_rows
        # A caller that names a partial hold keeps it (the tests do, to
        # drive the board through its states quickly). The overlay never
        # does: it follows the member's caption duration, below.
        self._explicit_partial_hold = partial_stale_seconds is not None
        self.partial_stale_seconds = (
            float(partial_stale_seconds)
            if partial_stale_seconds is not None
            else self._partial_hold(final_hold_seconds)
        )
        self.final_hold_seconds = final_hold_seconds
        self.minimum_queued_final_seconds = queued_final_seconds
        self.queued_final_seconds = max(
            queued_final_seconds,
            final_hold_seconds + 1.0,
        )
        self.max_queued_finals = max_queued_finals
        self.partials: dict[str, DisplayLine] = {}
        # Earlier streamed parts of each speaker's CURRENT utterance,
        # oldest first. They take rows nobody else needs, directly above
        # that speaker's live line - Shino, 2026-09-06: "can the
        # streaming text take up as many free spaces as is possible?"
        # A long sentence then reads down the overlay part by part until
        # its final collapses them into one line. Bounded per speaker so
        # a monologue cannot grow memory: at most max_rows - 1 kept.
        self.parts: dict[str, list[DisplayLine]] = {}
        self.finals: deque[DisplayLine] = deque()
        self.last_sequence: dict[str, int] = {}
        self.live_slots: list[str] = []

    def configure_display(
        self,
        *,
        max_rows: int,
        final_hold_seconds: float,
    ) -> None:
        """Apply live display preferences without dropping caption state."""
        if max_rows < 1 or max_rows > MAX_OVERLAY_ROWS:
            raise ValueError("max_rows must be between 1 and %d" % MAX_OVERLAY_ROWS)
        if (
            isinstance(final_hold_seconds, bool)
            or not isinstance(final_hold_seconds, (int, float))
            or not math.isfinite(float(final_hold_seconds))
            or final_hold_seconds <= 0
        ):
            raise ValueError("final_hold_seconds must be positive")
        if max_rows != self.max_rows:
            # Reassign from the freshest active speakers on the next frame.
            # This avoids keeping an arbitrary old subset when the member
            # deliberately shrinks the overlay.
            self.live_slots.clear()
        self.max_rows = max_rows
        for speaker_id in list(self.parts):
            self._trim_parts(speaker_id)
        self.final_hold_seconds = float(final_hold_seconds)
        if not self._explicit_partial_hold:
            self.partial_stale_seconds = self._partial_hold(self.final_hold_seconds)
        self.queued_final_seconds = max(
            self.minimum_queued_final_seconds,
            self.final_hold_seconds + 1.0,
        )

    # A long utterance reaches the overlay as streamed parts, one about
    # every STREAM_PART_SECONDS while the speaker keeps talking. A part
    # that vanishes before its successor arrives reads as a caption that
    # blinked off mid-sentence.
    STREAM_PART_SECONDS = 5.0

    @classmethod
    def _partial_hold(cls, final_hold_seconds: float) -> float:
        """How long a streamed part stays when nothing replaces it.

        Shino, 2026-09-06: "the split captions don't respect the caption
        duration". They held 2.4 seconds, a constant nothing ever set
        from the member's caption duration, while a new part arrives
        only every five seconds - so each part flashed and was gone
        before the next one came. A part now stays at least as long as
        a settled caption, and never less than the streaming cadence
        plus a margin, so a sentence in progress reads as one.
        """
        return max(float(final_hold_seconds), cls.STREAM_PART_SECONDS + 1.0)

    def _trim_parts(self, speaker_id: str) -> None:
        kept = self.parts.get(speaker_id, [])[-max(self.max_rows - 1, 0):]
        if kept:
            self.parts[speaker_id] = kept
        else:
            self.parts.pop(speaker_id, None)

    def clear(self) -> None:
        self.partials.clear()
        self.parts.clear()
        self.finals.clear()
        self.last_sequence.clear()
        self.live_slots.clear()

    def remove_speakers(self, ids: Any) -> bool:
        """Purge speakers from display state while retaining replay history."""
        speaker_ids = set(ids)
        if not speaker_ids:
            return False
        changed = False
        for speaker_id in speaker_ids:
            if speaker_id in self.partials:
                self.partials.pop(speaker_id, None)
                changed = True
            if speaker_id in self.parts:
                self.parts.pop(speaker_id, None)
                changed = True
        kept = deque(
            line for line in self.finals if line.event.speaker_id not in speaker_ids
        )
        if len(kept) != len(self.finals):
            self.finals = kept
            changed = True
        live = [speaker_id for speaker_id in self.live_slots if speaker_id not in speaker_ids]
        if len(live) != len(self.live_slots):
            self.live_slots = live
            changed = True
        return changed

    def discard(self, event: CaptionEvent) -> bool:
        """Consume ordering for a hidden caption and purge its visible state."""
        previous = self.last_sequence.get(event.speaker_id, -1)
        if event.sequence <= previous:
            return False
        self.last_sequence[event.speaker_id] = event.sequence
        self.remove_speakers((event.speaker_id,))
        return True

    def ingest(self, event: CaptionEvent, now: float) -> bool:
        """Accept a fresh per-speaker event; ignore repeats and reordering."""
        previous = self.last_sequence.get(event.speaker_id, -1)
        if event.sequence <= previous:
            return False
        self.last_sequence[event.speaker_id] = event.sequence
        line = DisplayLine(event=event, updated_at=now)
        # A speaker owns one card. When they begin a new clause, their older
        # completed clause must leave both the visible rows and the backlog;
        # otherwise that final reappears beside the new final when they stop.
        self.finals = deque(
            existing
            for existing in self.finals
            if existing.event.speaker_id != event.speaker_id
        )
        if event.final:
            # The whole utterance, once: its streamed parts leave with it.
            self.partials.pop(event.speaker_id, None)
            self.parts.pop(event.speaker_id, None)
            if event.speaker_id in self.live_slots:
                self.live_slots.remove(event.speaker_id)
            self.finals.append(line)
            while len(self.finals) > self.max_queued_finals:
                self.finals.popleft()
        else:
            previous = self.partials.get(event.speaker_id)
            if previous is not None:
                # The part that was live becomes an earlier part of the
                # same utterance, keeping its own timestamp so it ages
                # from when it was said, not from now.
                self.parts.setdefault(event.speaker_id, []).append(previous)
                self._trim_parts(event.speaker_id)
            self.partials[event.speaker_id] = line
        return True

    # Under backlog the board drains at the menu's own fastest hold
    # instead of letting the queue silently discard what has not been
    # shown. The alternative designs both failed a confirmed review: a
    # flat queue TTL threw away callouts while a slower drain left rows
    # idle for them (the member who chose the slow hold - the one who
    # reads slowly - lost MORE), and an unbounded queue turned the
    # overlay into a transcript running half a minute behind the pull.
    PRESSURE_HOLD_SECONDS = 2.0
    # A queued callout this old is dead raid information even if a row
    # finally frees; only reachable when live speakers pin every row.
    ABANDON_SECONDS = 20.0
    # Roughly one wrapped line of caption at the overlay's fonts.
    CHARS_PER_LINE = 70

    def _length_factor(self, text: str, cap: int) -> float:
        """Longer captions hold longer: reading time scales with length.

        The hold was a flat number of seconds while wrapping made a
        caption up to four lines tall - a four-line call at the default
        hold got under a second per line. The overlay cannot measure
        fonts from here, so this estimates by character count.
        """
        return float(min(1 + (max(len(text), 1) - 1) // self.CHARS_PER_LINE, cap))

    def _release_shown(self, now: float) -> None:
        """Free rows whose lines have been read, faster under backlog."""
        stale = [
            speaker_id
            for speaker_id, line in self.partials.items()
            if now - line.updated_at >= self.partial_stale_seconds
        ]
        for speaker_id in stale:
            self.partials.pop(speaker_id, None)
            if speaker_id in self.live_slots:
                self.live_slots.remove(speaker_id)
        for speaker_id in list(self.parts):
            fresh = [
                line
                for line in self.parts[speaker_id]
                if now - line.updated_at < self.partial_stale_seconds
            ]
            # Earlier parts belong beside a live line; with no live line
            # left for this speaker there is nothing for them to sit above.
            if fresh and speaker_id in self.partials:
                self.parts[speaker_id] = fresh
            else:
                self.parts.pop(speaker_id, None)

        backlog = any(line.first_shown_at is None for line in self.finals)
        kept: deque[DisplayLine] = deque()
        for line in self.finals:
            if line.first_shown_at is None:
                kept.append(line)
                continue
            if backlog:
                # People are waiting: drain at the fastest readable hold
                # rather than dropping what they said. The length factor
                # is capped lower here so a wall of text cannot starve
                # the queue it created.
                hold = self.PRESSURE_HOLD_SECONDS * self._length_factor(
                    line.event.text, 2
                )
                hold = min(hold, self.final_hold_seconds * self._length_factor(
                    line.event.text, 4
                ))
            else:
                hold = self.final_hold_seconds * self._length_factor(
                    line.event.text, 4
                )
            if now - line.first_shown_at < hold:
                kept.append(line)
        self.finals = kept

    def _prune_pending(self, now: float) -> None:
        """Bound what waits, AFTER seating - never in the same frame a
        row stood free for it."""
        pending_limit = self.max_rows * 3
        pending = [line for line in self.finals if line.first_shown_at is None]
        drop: set[int] = set()
        # Oldest go first on overflow: the newest callout is the one that
        # still matters mid-pull.
        for line in pending[: max(len(pending) - pending_limit, 0)]:
            drop.add(id(line))
        for line in pending:
            if now - line.updated_at >= self.ABANDON_SECONDS:
                drop.add(id(line))
        if drop:
            self.finals = deque(
                line for line in self.finals if id(line) not in drop
            )

    def _expire(self, now: float) -> None:
        # Kept for callers and older tests; seating-sensitive pruning
        # lives in visible() where free rows are known.
        self._release_shown(now)
        self._prune_pending(now)

    def visible(self, now: float) -> list[DisplayLine]:
        """Return at most max_rows, ordered old-to-new from top to bottom."""
        self._release_shown(now)
        self.live_slots = [
            speaker_id
            for speaker_id in self.live_slots
            if speaker_id in self.partials
        ]
        capacity = self.max_rows - len(self.live_slots)
        if capacity > 0:
            waiting = [
                line
                for speaker_id, line in self.partials.items()
                if speaker_id not in self.live_slots
            ]
            # Pick the most recently active waiting speakers, but place the
            # older one above the newer one. Once assigned, slots stay pinned
            # until that speaker finishes or goes stale, avoiding rapid row
            # swapping when three people talk at the same time.
            chosen = sorted(waiting, key=lambda line: line.updated_at, reverse=True)[
                :capacity
            ]
            chosen.sort(key=lambda line: line.updated_at)
            self.live_slots.extend(line.event.speaker_id for line in chosen)
        live = [self.partials[speaker_id] for speaker_id in self.live_slots]
        slots = self.max_rows - len(live)
        live_speakers = {line.event.speaker_id for line in live}

        shown_finals: list[DisplayLine] = []
        if slots:
            # Already-visible final lines keep their place before new queued
            # ones, as long as a live row from the same speaker did not begin.
            candidates = [
                line
                for line in self.finals
                if line.event.speaker_id not in live_speakers
            ]
            candidates.sort(
                key=lambda line: (
                    line.first_shown_at is None,
                    line.first_shown_at if line.first_shown_at is not None else line.updated_at,
                )
            )
            for line in candidates[:slots]:
                if line.first_shown_at is None:
                    line.first_shown_at = now
                shown_finals.append(line)
        # Prune only what STILL could not seat. Ordering is the fix for a
        # confirmed failure: expiry used to run before seating, so a
        # caption could be discarded in the very frame a row stood free
        # for it, and the overlay then sat blank over the words it threw
        # away.
        self._prune_pending(now)

        # Finished clauses sit faintly above live text. The newest live
        # speaker is the bottom row, nearest the player's action bars.
        shown_finals.sort(key=lambda line: line.updated_at)

        # Rows nobody needs go to the newest earlier parts of the live
        # utterances, so a long sentence reads down the overlay part by
        # part. Never at the expense of another speaker: only rows that
        # would otherwise stand empty, and each part sits directly above
        # its own live line, oldest first.
        free = self.max_rows - len(shown_finals) - len(live)
        chosen_parts: dict[str, list[DisplayLine]] = {}
        if free > 0:
            pool = [
                line
                for speaker_id in live_speakers
                for line in self.parts.get(speaker_id, [])
            ]
            pool.sort(key=lambda line: line.updated_at, reverse=True)
            for line in pool[:free]:
                chosen_parts.setdefault(line.event.speaker_id, []).append(line)
        rows: list[DisplayLine] = list(shown_finals)
        for line in live:
            earlier = chosen_parts.get(line.event.speaker_id, [])
            earlier.sort(key=lambda part: part.updated_at)
            rows.extend(earlier)
            rows.append(line)
        return rows[-self.max_rows :]


def event_to_json(event: ProtocolEvent) -> str:
    """Encode events generated by local trusted components."""
    if isinstance(event, CaptionEvent):
        payload = {
            "type": "caption",
            "speaker_id": event.speaker_id,
            "speaker": event.speaker,
            "character": event.character,
            "role": event.role,
            "text": event.text,
            "final": event.final,
            "sequence": event.sequence,
            "timestamp": event.timestamp,
        }
        # Omitted when unmapped, so nothing changes on the wire for a
        # speaker who has not run the mapping.
        if event.wow_class:
            payload["wow_class"] = event.wow_class
        # Normal stays byte-for-byte compatible with the previous caption
        # shape. Only speech that needs emphasis pays for the optional field.
        if event.speech_style != NORMAL_STYLE:
            if event.speech_style not in SPEECH_STYLES:
                raise CaptionProtocolError("speech_style is invalid")
            payload["speech_style"] = event.speech_style
    elif isinstance(event, SessionEvent):
        payload = {"type": "session", "state": event.state}
        if event.message:
            payload["message"] = event.message
    else:
        payload = {"type": "clear"}
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
