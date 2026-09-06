"""A streamed part holds like a caption, and one bad frame cannot stop the overlay.

Shino, 2026-09-06: "the split captions don't respect the caption
duration. Also, sometimes it freezes."

The first is measured: a streamed part was held 2.4 seconds by a constant
nothing ever set from the member's caption duration, while the next part
arrives only every five seconds. The second is not yet explained - the
child log was empty - so the overlay's frame loop now survives a fault,
logs which stage failed, and logs any frame slower than half a second.
The next freeze leaves a line behind.
"""

from __future__ import annotations

import io
import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest import mock

from VoiceCaptions import overlay
from VoiceCaptions.events import CaptionBoard, CaptionEvent
from VoiceCaptions.overlay import CaptionOverlay


def partial(speaker_id: str, sequence: int) -> CaptionEvent:
    return CaptionEvent(
        speaker_id=speaker_id,
        speaker=speaker_id.title(),
        character=speaker_id.title(),
        role="DAMAGE",
        text="It's going",
        final=False,
        sequence=sequence,
        timestamp=0.0,
    )


class PartHoldTests(unittest.TestCase):
    def test_a_part_outlives_the_streaming_cadence_by_default(self):
        board = CaptionBoard()
        self.assertGreaterEqual(
            board.partial_stale_seconds,
            CaptionBoard.STREAM_PART_SECONDS + 1.0,
        )

    def test_the_caption_duration_is_what_a_part_holds(self):
        board = CaptionBoard()
        board.configure_display(max_rows=2, final_hold_seconds=12.0)
        self.assertEqual(12.0, board.partial_stale_seconds)

    def test_a_shorter_duration_never_drops_below_the_cadence(self):
        board = CaptionBoard()
        board.configure_display(max_rows=2, final_hold_seconds=1.0)
        self.assertEqual(CaptionBoard.STREAM_PART_SECONDS + 1.0, board.partial_stale_seconds)

    def test_an_explicit_partial_hold_is_kept_as_given(self):
        """The board's own tests drive it fast; that contract stands."""
        board = CaptionBoard(partial_stale_seconds=1.0, final_hold_seconds=2.0)
        self.assertEqual(1.0, board.partial_stale_seconds)
        board.configure_display(max_rows=2, final_hold_seconds=9.0)
        self.assertEqual(1.0, board.partial_stale_seconds)

    def test_a_part_is_still_on_screen_when_the_next_one_arrives(self):
        """The regression itself: five seconds later, the line is there."""
        board = CaptionBoard()
        board.ingest(partial("loakka", 1), now=0.0)
        self.assertEqual(1, len(board.visible(now=5.0)))
        # And the member's longer duration is honoured beyond that.
        board.configure_display(max_rows=2, final_hold_seconds=9.0)
        board.ingest(partial("loakka", 2), now=10.0)
        self.assertEqual(1, len(board.visible(now=18.9)))
        self.assertEqual(0, len(board.visible(now=19.1)))


class FrameLoopTests(unittest.TestCase):
    def subject(self, *, render, exists=True):
        root = mock.Mock()
        root.winfo_exists.return_value = exists
        subject = SimpleNamespace(
            root=root,
            refresh_settings=lambda: None,
            drain_events=lambda now: None,
            refresh_target=lambda now: None,
            render=render,
        )
        # tick() re-arms itself by handing self.tick to after().
        subject.tick = lambda: None
        return subject

    def test_a_fault_in_render_still_arms_the_next_frame(self):
        def render(now):
            raise KeyError("the caption text must not be logged")

        subject = self.subject(render=render)
        with mock.patch.object(overlay.sys, "stderr", io.StringIO()) as err:
            overlay._child_log_count = 0
            CaptionOverlay.tick(subject)
        subject.root.after.assert_called_once_with(overlay.TICK_MS, subject.tick)
        logged = err.getvalue()
        self.assertIn("frame fault in render: KeyError", logged)
        self.assertNotIn("caption text", logged, "only the class name, never the text")

    def test_a_destroyed_window_ends_the_loop_quietly(self):
        def render(now):
            raise tk.TclError("application has been destroyed")

        subject = self.subject(render=render, exists=False)
        with mock.patch.object(overlay.sys, "stderr", io.StringIO()) as err:
            overlay._child_log_count = 0
            CaptionOverlay.tick(subject)
        subject.root.after.assert_not_called()
        self.assertEqual("", err.getvalue())

    def test_a_slow_frame_is_logged_with_its_stage(self):
        clock = iter([100.0, 100.9])

        subject = self.subject(render=lambda now: None)
        with mock.patch.object(overlay.time, "monotonic", lambda: next(clock)), \
                mock.patch.object(overlay.sys, "stderr", io.StringIO()) as err:
            overlay._child_log_count = 0
            CaptionOverlay.tick(subject)
        self.assertIn("slow frame: 0.90s, last stage render", err.getvalue())
        subject.root.after.assert_called_once()

    def test_a_stand_in_root_without_tk_still_ticks(self):
        """Other tests drive tick() with a bare stub; they must keep working."""
        subject = self.subject(render=lambda now: None)
        subject.root = SimpleNamespace(after=mock.Mock())
        CaptionOverlay.tick(subject)
        subject.root.after.assert_called_once()

    def test_the_child_log_is_bounded(self):
        with mock.patch.object(overlay.sys, "stderr", io.StringIO()) as err:
            overlay._child_log_count = 0
            for _ in range(overlay._CHILD_LOG_LIMIT + 50):
                overlay._child_log("again")
        self.assertEqual(overlay._CHILD_LOG_LIMIT, err.getvalue().count("again"))


if __name__ == "__main__":
    unittest.main()
