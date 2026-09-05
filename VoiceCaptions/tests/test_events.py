import json
import re
import unittest
from dataclasses import replace
from pathlib import Path

from VoiceCaptions.events import (
    CLASS_COLORS,
    MAX_OVERLAY_ROWS,
    ROLE_COLORS,
    CaptionBoard,
    CaptionEvent,
    CaptionProtocolError,
    ClearEvent,
    SessionEvent,
    event_to_json,
    parse_event,
    parse_event_line,
)


def caption(
    speaker_id: str,
    sequence: int,
    text: str,
    *,
    final: bool = False,
    role: str = "DAMAGE",
) -> CaptionEvent:
    return CaptionEvent(
        speaker_id=speaker_id,
        speaker=speaker_id.title(),
        character=speaker_id.title(),
        role=role,
        text=text,
        final=final,
        sequence=sequence,
        timestamp=float(sequence),
    )


class ProtocolTests(unittest.TestCase):
    def test_caption_round_trip(self):
        original = caption("mira", 7, "Spread, then stack.", role="HEALER")
        parsed = parse_event_line(event_to_json(original))
        self.assertEqual(original, parsed)
        self.assertEqual("#4fd182", parsed.color)

    def test_private_audience_routing_never_enters_viewer_json(self):
        routed = replace(
            caption("mira", 7, "Spread.", role="HEALER"),
            audience_generation=4,
            audience_revision=9,
        )
        payload = json.loads(event_to_json(routed))
        self.assertNotIn("audience_generation", payload)
        self.assertNotIn("audience_revision", payload)
        self.assertNotIn("audienceGeneration", payload)
        self.assertNotIn("audienceRevision", payload)

    def test_session_and_clear_round_trip(self):
        self.assertEqual(
            SessionEvent("paused", "Waiting for consent"),
            parse_event_line(
                event_to_json(SessionEvent("paused", "Waiting for consent"))
            ),
        )
        self.assertIsInstance(parse_event_line(event_to_json(ClearEvent())), ClearEvent)

    def test_receiver_unknown_session_round_trips_for_local_overlay(self):
        self.assertEqual(
            SessionEvent("unknown", "Listening status unavailable"),
            parse_event_line(
                event_to_json(
                    SessionEvent("unknown", "Listening status unavailable")
                )
            ),
        )

    def test_control_characters_are_removed_and_whitespace_collapses(self):
        payload = {
            "type": "caption",
            "speaker_id": "user-1",
            "speaker": "Mi\u0007ra",
            "character": "Mira",
            "role": "healer",
            "text": "  spread\n  then\tstack  ",
            "final": False,
            "sequence": 1,
            "timestamp": 1.0,
        }
        parsed = parse_event_line(json.dumps(payload))
        self.assertEqual("Mira", parsed.speaker)
        self.assertEqual("spread then stack", parsed.text)

    def test_bad_and_oversized_events_fail_closed(self):
        with self.assertRaises(CaptionProtocolError):
            parse_event_line("not json")
        with self.assertRaises(CaptionProtocolError):
            parse_event_line(json.dumps({"type": "caption"}))
        with self.assertRaises(CaptionProtocolError):
            parse_event_line(" " * 17_000)


class BoardTests(unittest.TestCase):
    def test_remove_speakers_purges_display_but_retains_sequence_history(self):
        board = CaptionBoard(max_rows=2)
        board.ingest(caption("mira", 5, "live"), now=0.0)
        board.ingest(caption("drex", 3, "done", final=True), now=0.1)
        board.visible(0.2)

        self.assertTrue(board.remove_speakers({"mira", "drex"}))
        self.assertEqual([], board.visible(0.3))
        self.assertEqual({"mira": 5, "drex": 3}, board.last_sequence)
        self.assertFalse(board.remove_speakers({"mira", "drex"}))
        self.assertFalse(board.ingest(caption("mira", 5, "replay"), now=0.4))

    def test_discard_advances_order_and_removes_existing_speaker_rows(self):
        board = CaptionBoard(max_rows=2)
        board.ingest(caption("mira", 1, "visible"), now=0.0)
        board.visible(0.1)

        self.assertTrue(board.discard(caption("mira", 2, "muted")))
        self.assertEqual([], board.visible(0.2))
        self.assertEqual(2, board.last_sequence["mira"])
        self.assertFalse(board.discard(caption("mira", 2, "duplicate")))
        self.assertFalse(board.ingest(caption("mira", 1, "old"), now=0.3))

    def test_two_concurrent_speakers_keep_independent_rows(self):
        board = CaptionBoard(max_rows=2)
        self.assertTrue(board.ingest(caption("mira", 1, "Spread"), now=0.0))
        self.assertTrue(board.ingest(caption("drex", 1, "Boss left"), now=0.1))
        rows = board.visible(0.2)
        self.assertEqual(["mira", "drex"], [row.event.speaker_id for row in rows])
        self.assertEqual(["Spread", "Boss left"], [row.event.text for row in rows])

    def test_per_speaker_sequence_prevents_reordered_overwrite(self):
        board = CaptionBoard()
        self.assertTrue(board.ingest(caption("mira", 5, "New text"), now=0.0))
        self.assertFalse(board.ingest(caption("mira", 4, "Old text"), now=0.1))
        self.assertEqual("New text", board.visible(0.2)[0].event.text)

    def test_new_speech_replaces_the_same_speakers_older_final(self):
        board = CaptionBoard(max_rows=2, final_hold_seconds=10.0)
        board.ingest(caption("mira", 1, "First thought", final=True), now=0.0)
        self.assertEqual(
            ["First thought"],
            [line.event.text for line in board.visible(0.1)],
        )

        board.ingest(caption("mira", 2, "Second thought"), now=0.2)
        self.assertEqual([], list(board.finals))
        self.assertEqual(
            ["Second thought"],
            [line.event.text for line in board.visible(0.3)],
        )

        board.ingest(
            caption("mira", 3, "Second thought complete", final=True),
            now=0.4,
        )
        rows = board.visible(0.5)
        self.assertEqual(["mira"], [line.event.speaker_id for line in rows])
        self.assertEqual(
            ["Second thought complete"],
            [line.event.text for line in rows],
        )
        self.assertEqual(1, len(board.finals))

    def test_same_speaker_replacement_preserves_other_speakers_backlog(self):
        board = CaptionBoard(max_rows=1, final_hold_seconds=10.0)
        board.ingest(caption("mira", 1, "Mira first", final=True), now=0.0)
        board.visible(0.01)
        board.ingest(caption("drex", 1, "Drex callout", final=True), now=0.1)

        board.ingest(caption("mira", 2, "Mira again"), now=0.2)
        self.assertEqual(
            ["drex"],
            [line.event.speaker_id for line in board.finals],
        )
        self.assertEqual(
            ["Mira again"],
            [line.event.text for line in board.visible(0.3)],
        )

        board.ingest(caption("mira", 3, "Mira done", final=True), now=0.4)
        self.assertEqual(
            ["drex", "mira"],
            [line.event.speaker_id for line in board.finals],
        )
        self.assertEqual(
            ["Drex callout"],
            [line.event.text for line in board.visible(0.5)],
        )
        self.assertFalse(
            board.ingest(caption("mira", 2, "Reordered"), now=0.6)
        )
        self.assertEqual(
            ["Mira done"],
            [line.event.text for line in board.visible(2.51)],
        )

    def test_third_speaker_final_waits_without_growing_the_overlay(self):
        board = CaptionBoard(
            max_rows=2,
            partial_stale_seconds=1.0,
            final_hold_seconds=2.0,
            queued_final_seconds=5.0,
        )
        board.ingest(caption("one", 1, "One"), now=0.0)
        board.ingest(caption("two", 1, "Two"), now=0.1)
        board.ingest(caption("three", 1, "Three", final=True), now=0.2)
        self.assertEqual(2, len(board.visible(0.3)))
        self.assertNotIn(
            "three", [line.event.speaker_id for line in board.visible(0.3)]
        )
        rows = board.visible(1.2)
        self.assertEqual(["three"], [line.event.speaker_id for line in rows])

    def test_three_live_speakers_do_not_make_rows_swap_on_each_partial(self):
        board = CaptionBoard(max_rows=2)
        board.ingest(caption("one", 1, "One"), now=0.0)
        board.ingest(caption("two", 1, "Two"), now=0.1)
        self.assertEqual(
            ["one", "two"],
            [line.event.speaker_id for line in board.visible(0.2)],
        )
        board.ingest(caption("three", 1, "Three"), now=0.3)
        board.ingest(caption("three", 2, "Three again"), now=0.4)
        self.assertEqual(
            ["one", "two"],
            [line.event.speaker_id for line in board.visible(0.5)],
        )
        board.ingest(caption("one", 2, "One done", final=True), now=0.6)
        self.assertEqual(
            ["two", "three"],
            [line.event.speaker_id for line in board.visible(0.7)],
        )

    def test_final_clause_disappears_after_its_hold(self):
        board = CaptionBoard(final_hold_seconds=1.0)
        board.ingest(caption("mira", 1, "Done", final=True), now=0.0)
        self.assertEqual(1, len(board.visible(0.1)))
        self.assertEqual([], board.visible(1.11))

    def test_display_preferences_change_without_losing_caption_state(self):
        board = CaptionBoard(max_rows=4, final_hold_seconds=1.0)
        for index, speaker in enumerate(("one", "two", "three", "four")):
            board.ingest(caption(speaker, 1, speaker), now=index / 10)
        self.assertEqual(4, len(board.visible(0.4)))

        board.configure_display(max_rows=1, final_hold_seconds=6.0)

        rows = board.visible(0.5)
        self.assertEqual(["four"], [row.event.speaker_id for row in rows])
        self.assertEqual(
            {"one": 1, "two": 1, "three": 1, "four": 1},
            board.last_sequence,
        )

    def test_runtime_hold_change_applies_to_an_already_visible_final(self):
        board = CaptionBoard(final_hold_seconds=1.0)
        board.ingest(caption("mira", 1, "Done", final=True), now=0.0)
        self.assertEqual(1, len(board.visible(0.1)))
        board.configure_display(max_rows=2, final_hold_seconds=6.0)
        self.assertEqual(1, len(board.visible(5.9)))
        self.assertEqual([], board.visible(6.11))

    def test_slow_single_row_does_not_drop_the_next_speaker(self):
        board = CaptionBoard(
            max_rows=1,
            final_hold_seconds=10.0,
            queued_final_seconds=8.0,
        )
        speakers = ("one", "two")
        for index, speaker in enumerate(speakers):
            board.ingest(
                caption(speaker, 1, speaker, final=True),
                now=index / 10,
            )

        shown = []
        for now in (0.2, 10.21):
            rows = board.visible(now)
            self.assertEqual(1, len(rows))
            shown.append(rows[0].event.speaker_id)
        self.assertEqual(list(speakers), shown)

    def test_runtime_display_preferences_reject_invalid_values(self):
        board = CaptionBoard()
        for rows in (0, MAX_OVERLAY_ROWS + 1):
            with self.assertRaises(ValueError):
                board.configure_display(
                    max_rows=rows, final_hold_seconds=3.6
                )
        # The widened ceiling is a valid row count, not a rejected one.
        board.configure_display(max_rows=MAX_OVERLAY_ROWS, final_hold_seconds=3.6)
        for hold in (0, -1, float("nan"), True):
            with self.assertRaises(ValueError):
                board.configure_display(max_rows=2, final_hold_seconds=hold)

    def test_clear_removes_transcript_state(self):
        board = CaptionBoard()
        board.ingest(caption("mira", 1, "Private words"), now=0.0)
        board.clear()
        self.assertEqual([], board.visible(0.1))
        self.assertEqual({}, board.last_sequence)




class BacklogDrainTests(unittest.TestCase):
    """Under backlog the board drains faster instead of discarding.

    The review proved the old design by execution: at the menu's slow
    hold with one row, the third speaker's finished callout was thrown
    away eight seconds before a row could ever exist for it, and the
    overlay then sat blank over the words it discarded. The member who
    picks the slow hold is the one who reads slowly - the last person
    who should silently lose callouts.
    """

    def drain(self, board, until, step=0.05):
        shown, now = {}, 0.0
        while now <= until:
            for line in board.visible(now):
                shown.setdefault(line.event.speaker_id, now)
            now += step
        return shown

    def test_the_reviewed_failure_three_speakers_slow_hold_one_row(self):
        board = CaptionBoard(max_rows=1, final_hold_seconds=10.0)
        for index, speaker in enumerate(("one", "two", "three")):
            board.ingest(
                caption(speaker, 1, "callout " + speaker, final=True),
                now=index * 0.5,
            )
        shown = self.drain(board, 30.0)
        self.assertEqual({"one", "two", "three"}, set(shown),
                         "a queued callout was silently discarded")

    def test_a_burst_of_six_all_reach_the_reader_at_the_slowest_setting(self):
        board = CaptionBoard(max_rows=2, final_hold_seconds=10.0)
        speakers = [f"s{i}" for i in range(6)]
        for index, speaker in enumerate(speakers):
            board.ingest(
                caption(speaker, 1, "callout", final=True), now=index * 0.3
            )
        shown = self.drain(board, 40.0)
        self.assertEqual(set(speakers), set(shown))

    def test_backlog_never_lags_more_than_a_pull_can_tolerate(self):
        """Preserving everything must not become a late transcript."""
        board = CaptionBoard(max_rows=1, final_hold_seconds=10.0)
        for index in range(3):
            board.ingest(
                caption(f"s{index}", 1, "go", final=True), now=index * 0.5
            )
        shown = self.drain(board, 30.0)
        worst = max(shown[f"s{i}"] - i * 0.5 for i in range(3))
        self.assertLess(worst, 8.0, f"worst wait was {worst:.1f}s")

    def test_a_quiet_board_honours_the_full_slow_hold(self):
        """Pressure drain only fires under pressure: with nobody waiting
        the member's chosen hold is exactly what they get."""
        board = CaptionBoard(max_rows=2, final_hold_seconds=10.0)
        board.ingest(caption("solo", 1, "short line", final=True), now=0.0)
        # Seat it at once: first_shown_at starts the hold clock.
        self.assertEqual(1, len(board.visible(0.0)))
        self.assertEqual(1, len(board.visible(9.9)))
        self.assertEqual(0, len(board.visible(10.05)))

    def test_overflow_keeps_the_newest_callouts(self):
        """When the queue truly overfills, the oldest die: mid-pull the
        newest instruction is the one that still matters."""
        board = CaptionBoard(max_rows=1, final_hold_seconds=10.0)
        board.ingest(caption("shown", 1, "first", final=True), now=0.0)
        board.visible(0.01)  # seat it so the rest queue behind it
        for index in range(5):
            board.ingest(
                caption(f"q{index}", 1, "queued", final=True),
                now=0.1 + index * 0.01,
            )
        pending = [l.event.speaker_id for l in board.finals
                   if l.first_shown_at is None]
        self.assertEqual(["q2", "q3", "q4"], pending[-3:])
        shown = self.drain(board, 30.0)
        self.assertIn("q4", shown, "the newest callout must survive overflow")

    def test_starved_by_pinned_live_rows_the_queue_finally_gives_up(self):
        """Live partials pin rows; a callout twenty seconds stale is dead
        raid information and may be dropped - but only then."""
        board = CaptionBoard(max_rows=1, final_hold_seconds=3.6)
        board.ingest(caption("talker", 1, "still talking"), now=0.0)
        board.ingest(caption("done", 1, "finished words", final=True), now=0.1)
        now = 0.0
        while now < 25.0:
            board.ingest(caption("talker", int(now * 10) + 2, "still talking"),
                         now=now)
            board.visible(now)
            now += 0.5
        pending = [l for l in board.finals if l.first_shown_at is None]
        self.assertEqual([], pending, "a 20s-stale callout should be released")

    def test_longer_captions_hold_longer(self):
        """The hold was flat while wrapping made captions four lines
        tall - under a second of reading time per line at the default."""
        board = CaptionBoard(max_rows=2, final_hold_seconds=3.6)
        board.ingest(caption("wall", 1, "word " * 56, final=True), now=0.0)
        self.assertEqual(1, len(board.visible(0.0)))
        self.assertEqual(1, len(board.visible(3.7)),
                         "a four-line caption vanished at the one-line hold")
        self.assertEqual(0, len(board.visible(14.5)))


class ClassColorTests(unittest.TestCase):
    """A speaker's name is drawn in their WoW class colour.

    Every raider already reads those colours without thinking, and for
    someone whose only channel is this overlay that recognition arrives
    faster than the name itself does.
    """

    def caption(self, **overrides):
        fields = dict(
            speaker_id="1",
            speaker="Mira",
            character="Miradan",
            role="HEALER",
            text="Interrupt now",
            final=True,
            sequence=1,
            timestamp=1.0,
        )
        fields.update(overrides)
        return CaptionEvent(**fields)

    def test_a_mapped_class_colours_the_name(self):
        event = self.caption(wow_class="rogue")
        self.assertEqual(CLASS_COLORS["rogue"], event.name_color)

    def test_the_card_accent_stays_the_role_colour(self):
        """Focus mode filters on role, so the card must keep saying role."""
        event = self.caption(wow_class="rogue", role="HEALER")
        self.assertEqual(ROLE_COLORS["HEALER"], event.color)
        self.assertNotEqual(event.color, event.name_color)

    def test_an_unmapped_speaker_looks_exactly_as_before(self):
        event = self.caption()
        self.assertEqual(event.color, event.name_color)

    def test_every_class_the_adapter_accepts_has_a_colour(self):
        """The adapter's WOW_CLASSES is the contract; a class it accepts
        but this table lacks would silently fall back to a role colour."""
        source = (
            Path(__file__).resolve().parents[2]
            / "VoiceCaptions" / "discord_adapter" / "src" / "session_manager.js"
        ).read_text(encoding="utf-8")
        block = source.split("const WOW_CLASSES = new Set([", 1)[1]
        block = block.split("]);", 1)[0]
        accepted = set(re.findall(r'"([a-z-]+)"', block))
        self.assertTrue(accepted, "could not read the adapter's class list")
        self.assertEqual(set(), accepted - set(CLASS_COLORS))

    def test_the_class_survives_the_wire_in_both_directions(self):
        event = self.caption(wow_class="mage")
        restored = parse_event_line(event_to_json(event))
        self.assertEqual("mage", restored.wow_class)
        self.assertEqual(CLASS_COLORS["mage"], restored.name_color)

    def test_an_older_host_that_omits_the_class_still_parses(self):
        payload = json.loads(event_to_json(self.caption(wow_class="mage")))
        payload.pop("wow_class")
        restored = parse_event(payload)
        self.assertEqual("", restored.wow_class)
        self.assertEqual(restored.color, restored.name_color)

    def test_a_junk_class_never_reaches_a_colour_lookup(self):
        for junk in ("not-a-class", "", None, 42, "../../etc", ["rogue"]):
            payload = json.loads(event_to_json(self.caption()))
            payload["wow_class"] = junk
            restored = parse_event(payload)
            self.assertEqual("", restored.wow_class, repr(junk))
            self.assertEqual(restored.color, restored.name_color)

    def test_casing_is_forgiven_because_the_value_is_whitelisted_anyway(self):
        """Deliberate leniency, not an accident.

        The wire contract is the adapter's lowercase kebab spelling, but
        anything that is not in the table already falls back to the role
        colour, so accepting "Rogue" costs nothing and losing a speaker's
        colour over a capital letter would be silly.
        """
        for spelling in ("Rogue", "ROGUE", "rogue"):
            payload = json.loads(event_to_json(self.caption()))
            payload["wow_class"] = spelling
            self.assertEqual("rogue", parse_event(payload).wow_class)


if __name__ == "__main__":
    unittest.main(verbosity=2)
