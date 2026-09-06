"""Earlier streamed parts take the rows nobody else needs.

Shino, 2026-09-06: "can the streaming text take up as many free spaces
as is possible?"

A live line shows only the part that just arrived (PR #7). On a quiet
overlay that left rows standing empty while the earlier parts of the
same sentence had already gone. Now the part that was live moves up to a
free row when the next part arrives, directly above the speaker's live
line, oldest first, and the final collapses them all into one line. Never
at another speaker's expense: only rows that would otherwise be empty.
"""

from __future__ import annotations

import unittest

from VoiceCaptions.events import CaptionBoard, CaptionEvent


def part(speaker_id: str, sequence: int, text: str, *, final: bool = False) -> CaptionEvent:
    return CaptionEvent(
        speaker_id=speaker_id,
        speaker=speaker_id.title(),
        character=speaker_id.title(),
        role="DAMAGE",
        text=text,
        final=final,
        sequence=sequence,
        timestamp=0.0,
    )


def texts(rows) -> list[str]:
    return [line.event.text for line in rows]


class StreamedPartsFillFreeRowsTests(unittest.TestCase):
    def test_a_long_sentence_reads_down_the_overlay_part_by_part(self):
        # A twelve-second caption duration: long enough that three parts
        # five seconds apart are all still within their hold.
        board = CaptionBoard(max_rows=3, final_hold_seconds=12.0)
        board.ingest(part("loakka", 1, "It's going,"), now=0.0)
        board.ingest(part("loakka", 2, "it's going,"), now=5.0)
        board.ingest(part("loakka", 3, "it's going."), now=10.0)
        self.assertEqual(
            ["It's going,", "it's going,", "it's going."],
            texts(board.visible(now=10.1)),
        )

    def test_the_live_part_is_the_bottom_row(self):
        board = CaptionBoard(max_rows=3)
        board.ingest(part("loakka", 1, "first"), now=0.0)
        board.ingest(part("loakka", 2, "second"), now=5.0)
        rows = board.visible(now=5.1)
        self.assertEqual(["first", "second"], texts(rows))
        self.assertTrue(rows[-1].live)

    def test_only_rows_nobody_needs_are_taken(self):
        board = CaptionBoard(max_rows=2)
        board.ingest(part("loakka", 1, "first"), now=0.0)
        board.ingest(part("shino", 1, "hello"), now=1.0)
        board.ingest(part("loakka", 2, "second"), now=5.0)
        # Two rows, two live speakers: Loakka's earlier part has no room.
        self.assertEqual(["hello", "second"], texts(board.visible(now=5.1)))

    def test_a_row_freed_by_a_final_is_taken_by_an_earlier_part(self):
        board = CaptionBoard(max_rows=2, final_hold_seconds=1.0)
        board.ingest(part("loakka", 1, "first"), now=3.0)
        board.ingest(part("shino", 1, "hello"), now=3.5)
        board.ingest(part("loakka", 2, "second"), now=5.0)
        board.ingest(part("shino", 2, "hello there", final=True), now=5.5)
        # Shino's final takes the second row while it is read...
        self.assertEqual(["hello there", "second"], texts(board.visible(now=5.6)))
        # ...and once released, the row goes to Loakka's earlier part.
        self.assertEqual(["first", "second"], texts(board.visible(now=7.0)))

    def test_the_final_collapses_the_parts_into_one_line(self):
        board = CaptionBoard(max_rows=3)
        board.ingest(part("loakka", 1, "first"), now=0.0)
        board.ingest(part("loakka", 2, "second"), now=5.0)
        board.ingest(part("loakka", 3, "first second third", final=True), now=10.0)
        self.assertEqual(["first second third"], texts(board.visible(now=10.1)))

    def test_earlier_parts_age_out_oldest_first(self):
        board = CaptionBoard(max_rows=3, final_hold_seconds=3.0)
        hold = board.partial_stale_seconds
        board.ingest(part("loakka", 1, "first"), now=0.0)
        board.ingest(part("loakka", 2, "second"), now=2.0)
        board.ingest(part("loakka", 3, "third"), now=4.0)
        self.assertEqual(["first", "second", "third"], texts(board.visible(now=4.1)))
        self.assertEqual(["second", "third"], texts(board.visible(now=hold + 0.1)))
        self.assertEqual(["third"], texts(board.visible(now=2.0 + hold + 0.1)))

    def test_parts_never_outlive_their_live_line(self):
        board = CaptionBoard(max_rows=3, partial_stale_seconds=1.0)
        board.ingest(part("loakka", 1, "first"), now=0.0)
        board.ingest(part("loakka", 2, "second"), now=0.5)
        # Both are stale together; nothing lingers as an orphan.
        self.assertEqual([], texts(board.visible(now=2.0)))
        self.assertEqual({}, board.parts)

    def test_parts_are_bounded_per_speaker(self):
        board = CaptionBoard(max_rows=2)
        for index in range(1, 40):
            board.ingest(part("loakka", index, "part %d" % index), now=float(index))
        self.assertLessEqual(len(board.parts["loakka"]), board.max_rows - 1)
        self.assertEqual(["part 38", "part 39"], texts(board.visible(now=39.1)))

    def test_shrinking_the_overlay_trims_kept_parts(self):
        board = CaptionBoard(max_rows=4)
        for index in range(1, 5):
            board.ingest(part("loakka", index, "part %d" % index), now=float(index))
        board.configure_display(max_rows=2, final_hold_seconds=3.6)
        self.assertEqual(1, len(board.parts["loakka"]))

    def test_a_muted_speaker_takes_their_parts_with_them(self):
        board = CaptionBoard(max_rows=3)
        board.ingest(part("loakka", 1, "first"), now=0.0)
        board.ingest(part("loakka", 2, "second"), now=1.0)
        board.discard(part("loakka", 3, "third"))
        self.assertEqual([], texts(board.visible(now=1.1)))

    def test_a_single_row_overlay_behaves_exactly_as_before(self):
        board = CaptionBoard(max_rows=1)
        board.ingest(part("loakka", 1, "first"), now=0.0)
        board.ingest(part("loakka", 2, "second"), now=5.0)
        self.assertEqual(["second"], texts(board.visible(now=5.1)))


if __name__ == "__main__":
    unittest.main()
