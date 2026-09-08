import queue
import re
import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from VoiceCaptions import overlay, placement
from VoiceCaptions.events import (
    CLASS_COLORS,
    ROLE_COLORS,
    CaptionBoard,
    CaptionEvent,
    DisplayLine,
    SessionEvent,
)
from VoiceCaptions.layout import (
    BASE_FONT_SIZE,
    DEFAULT_METRICS,
    MAX_TEXT_LINES,
    background_stipple,
    canvas_height_for_target,
    caption_font_key,
    caption_font_specs,
    clipped_text,
    metrics_for_target,
    safe_overlay_position,
    wrap_text,
)
from VoiceCaptions.overlay import CaptionOverlay
from VoiceCaptions.settings import (
    DEFAULT_SETTINGS,
    MAX_CAPTION_FONT_SIZE,
    MAX_VISIBLE_SPEAKERS,
    MIN_CAPTION_FONT_SIZE,
    OverlaySettings,
    load_settings,
    save_settings,
)


def line(speaker: str, *, final: bool) -> DisplayLine:
    return DisplayLine(
        event=CaptionEvent(
            speaker_id=speaker,
            speaker=speaker,
            character=speaker,
            role="DAMAGE",
            text="Text",
            final=final,
            sequence=1,
            timestamp=0.0,
        ),
        updated_at=0.0,
    )


class FakeFont:
    def __init__(self, per_character: int) -> None:
        self.per_character = per_character

    def measure(self, text: str) -> int:
        return len(text) * self.per_character

    def metrics(self, name: str) -> int:
        if name != "linespace":
            raise KeyError(name)
        return 12


class FakeCanvas:
    """Records the drawn strings so a row can be checked without a display."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.text_calls: list[dict] = []
        self.oval_calls: list[dict] = []
        self.polygon_calls: list[dict] = []

    def create_polygon(self, *args, **kwargs) -> int:
        self.polygon_calls.append({"args": args, **kwargs})
        return 0

    def create_oval(self, *args, **kwargs) -> int:
        self.oval_calls.append({"args": args, **kwargs})
        return 0

    def create_rectangle(self, *args, **kwargs) -> int:
        return 0

    def create_text(self, *args, **kwargs) -> int:
        self.texts.append(kwargs.get("text", ""))
        self.text_calls.append({"args": args, **kwargs})
        return 0

    def delete(self, *args, **kwargs) -> None:
        self.texts.clear()
        self.text_calls.clear()
        self.oval_calls.clear()


class CoordinateCanvas(FakeCanvas):
    """Also records Tk's positional arguments for collision checks."""

    def create_oval(self, *args, **kwargs) -> int:
        self.oval_calls.append({"coords": args, **kwargs})
        return 0

    def create_text(self, *args, **kwargs) -> int:
        self.texts.append(kwargs.get("text", ""))
        self.text_calls.append({"coords": args, **kwargs})
        return 0


class FakeRoot:
    # A 1440p-class virtual display, so the screen clamp in
    # canvas_height_on_screen runs for real in these tests instead of
    # being skipped over a missing attribute.
    SCREEN_WIDTH = 2560
    SCREEN_HEIGHT = 1440

    def __init__(self, x: int = 100, y: int = 200) -> None:
        self.x = x
        self.y = y
        self.pointer = (0, 0)
        self.geometries: list[str] = []
        self.scheduled: list[tuple[int, object]] = []

    def winfo_pointerxy(self) -> tuple[int, int]:
        return self.pointer

    def winfo_screenwidth(self) -> int:
        return self.SCREEN_WIDTH

    def winfo_screenheight(self) -> int:
        return self.SCREEN_HEIGHT

    def update_idletasks(self) -> None:
        """The real fix under test: after a geometry() call, winfo_x/y
        report the old position until the window manager catches up.
        Save must flush before reading, or it writes a stale anchor."""
        if self.geometries:
            tail = self.geometries[-1].split("+")
            if len(tail) == 3:
                self.x, self.y = int(tail[1]), int(tail[2])

    def winfo_x(self) -> int:
        return self.x

    def winfo_y(self) -> int:
        return self.y

    def geometry(self, value: str) -> None:
        self.geometries.append(value)
        match = re.search(r"\+(-?\d+)\+(-?\d+)$", value)
        if match:
            self.x = int(match.group(1))
            self.y = int(match.group(2))

    def after(self, delay: int, callback) -> None:
        self.scheduled.append((delay, callback))


def geometry_parts(value: str) -> tuple[int, int, int, int]:
    match = re.match(r"^(\d+)x(\d+)\+?(-?\d+)\+?(-?\d+)$", value)
    if match is None:
        raise AssertionError(f"unexpected Tk geometry: {value}")
    return tuple(int(part) for part in match.groups())


def drawing_subject(canvas: FakeCanvas) -> SimpleNamespace:
    return SimpleNamespace(
        canvas=canvas,
        # measure_card and draw_line read self.metrics since font scaling
        # landed; without it this stand-in raises AttributeError rather
        # than exercising the drawing it exists to test.
        metrics=DEFAULT_METRICS,
        # draw_lines and the tally read the growth direction now, the
        # same way the real overlay does.
        settings=OverlaySettings(),
        fonts={
            "speaker": FakeFont(9),
            "caption": FakeFont(8),
            "previous": FakeFont(8),
            "initial": FakeFont(8),
            "status": FakeFont(7),
        },
        font_size=BASE_FONT_SIZE,
    )


class LayoutTests(unittest.TestCase):
    def test_two_live_rows_stay_under_ninety_pixels(self):
        height = DEFAULT_METRICS.stack_height(
            [line("one", final=False), line("two", final=False)]
        )
        self.assertLessEqual(height, 90)

    def test_live_and_settled_rows_are_the_same_height(self):
        """Two speakers on screen are two cards of one shape."""
        mixed = DEFAULT_METRICS.stack_height(
            [line("old", final=True), line("live", final=False)]
        )
        settled = DEFAULT_METRICS.stack_height(
            [line("old", final=True), line("older", final=True)]
        )
        self.assertEqual(settled, mixed)
        self.assertEqual(68, mixed)

    def test_text_is_ellipsized_to_one_measured_line(self):
        measure = lambda value: len(value) * 10
        result = clipped_text("one two three four", 100, measure)
        self.assertTrue(result.endswith("..."))
        self.assertLessEqual(measure(result), 100)

    def test_short_text_is_untouched(self):
        self.assertEqual("Stack", clipped_text("Stack", 100, lambda value: len(value)))

    def test_a_wide_speaker_name_does_not_erase_the_caption(self):
        canvas = FakeCanvas()
        overlay = drawing_subject(canvas)
        row = line("W" * 48, final=True)
        # Measured through the class for the same reason draw_line is:
        # the namespace stands in for a real overlay without building a
        # Tk window.
        card = CaptionOverlay.measure_card(overlay, row)
        CaptionOverlay.draw_line(overlay, row, 0, card["height"], card)
        self.assertIn("Text", canvas.texts)
        self.assertTrue(any(text.endswith("...") for text in canvas.texts))

    def test_character_names_use_class_colour_without_changing_unmapped_rows(self):
        for wow_class in ("rogue", ""):
            for final in (False, True):
                with self.subTest(wow_class=wow_class, final=final):
                    canvas = FakeCanvas()
                    subject = drawing_subject(canvas)
                    row = DisplayLine(
                        event=CaptionEvent(
                            speaker_id="mira",
                            speaker="Mira",
                            character="Miradan" if wow_class else "",
                            role="HEALER",
                            text="Interrupt now",
                            final=final,
                            sequence=1,
                            timestamp=0.0,
                            wow_class=wow_class,
                        ),
                        updated_at=0.0,
                    )
                    card = CaptionOverlay.measure_card(subject, row)
                    CaptionOverlay.draw_line(subject, row, 0, card["height"], card)
                    display_name = "MIRADAN" if wow_class else "MIRA"
                    name = next(
                        call
                        for call in canvas.text_calls
                        if call.get("text") == display_name
                    )
                    # A mapped name carries its class colour on every
                    # row; an unmapped one is muted on every row. Live no
                    # longer changes the answer.
                    expected_name = (
                        CLASS_COLORS["rogue"] if wow_class else overlay.MUTED
                    )
                    self.assertEqual(expected_name, name.get("fill"))
                    caption = next(
                        call
                        for call in canvas.text_calls
                        if call.get("text") == "Interrupt now"
                    )
                    # Caption copy follows the member's selected text colour;
                    # the name still carries the role or class accent.
                    expected_caption = subject.settings.caption_text_color
                    self.assertEqual(expected_caption, caption.get("fill"))

    def test_whispers_are_italic_and_shouts_are_bold_in_live_and_history_rows(self):
        canvas = FakeCanvas()
        subject = drawing_subject(canvas)
        subject.fonts.update(
            {
                "caption_whisper": FakeFont(7),
                "caption_shout": FakeFont(10),
                "previous_whisper": FakeFont(6),
                "previous_shout": FakeFont(9),
            }
        )
        for final, style, expected in (
            (False, "whisper", "previous_whisper"),
            (False, "shout", "previous_shout"),
            (True, "whisper", "previous_whisper"),
            (True, "shout", "previous_shout"),
            (False, "normal", "previous"),
        ):
            with self.subTest(final=final, style=style):
                event = CaptionEvent(
                    speaker_id="mira",
                    speaker="Mira",
                    character="Mira",
                    role="HEALER",
                    text="Move left",
                    final=final,
                    sequence=1,
                    timestamp=0.0,
                    speech_style=style,
                )
                card = CaptionOverlay.measure_card(
                    subject,
                    DisplayLine(event=event, updated_at=0.0),
                )
                self.assertIs(subject.fonts[expected], card["caption_font"])

    def test_position_is_centered_and_above_the_bottom_safe_area(self):
        position = safe_overlay_position((100, 200, 1920, 1080), (540, 86))
        self.assertEqual(790, position[0])
        self.assertLess(position[1] + 86, 200 + 1080)

    def test_position_clamps_inside_a_small_window(self):
        self.assertEqual(
            (10, 20), safe_overlay_position((10, 20, 300, 100), (540, 86))
        )


class AnchorWindowTests(unittest.TestCase):
    """The anchor test must fail open. Rejecting the real client hides every
    caption, which is far worse than anchoring to a stray window."""

    @staticmethod
    def window(image):
        user32 = SimpleNamespace(
            GetWindowThreadProcessId=lambda hwnd, out: None
        )
        with mock.patch.object(overlay, "process_image_name", lambda pid: image):
            return overlay.window_belongs_to_wow(1, user32)

    def test_the_game_is_accepted(self):
        self.assertTrue(self.window("wow.exe"))
        self.assertTrue(self.window("WoW.exe".lower()))

    def test_explorer_titled_for_the_install_folder_is_rejected(self):
        self.assertFalse(self.window("explorer.exe"))

    def test_an_unreadable_owner_is_accepted(self):
        self.assertTrue(self.window(None))


class WrapTests(unittest.TestCase):
    """A caption must not lose its tail.

    The overlay used to clip every caption to a single line, so a raid
    call like "stack behind the pillar for the knockback, then spread to
    your marks" arrived complete and was drawn as "stack behind the
    pillar for the kno...". For a deaf reader the caption IS the
    sentence: the words that were dropped are simply gone, and nothing on
    screen says how many.
    """

    # A monospace-ish stand-in so the arithmetic in these tests is exact
    # and does not depend on a font being installed.
    CHAR = 7

    def measure(self, text):
        return len(text) * self.CHAR

    def wrap(self, text, chars, max_lines=MAX_TEXT_LINES):
        return wrap_text(text, chars * self.CHAR, self.measure, max_lines)

    def test_a_short_caption_stays_on_one_line(self):
        self.assertEqual(["Interrupt now"], self.wrap("Interrupt now", 40))

    def test_a_long_caption_wraps_instead_of_losing_its_tail(self):
        text = ("Everyone stack behind the pillar for the knockback, "
                "then spread to your marks before the second cast")
        lines = self.wrap(text, 40)
        self.assertGreater(len(lines), 1)
        # Every word survives, in order, and nothing was truncated.
        self.assertEqual(text.split(), " ".join(lines).split())
        for chunk in lines:
            self.assertLessEqual(self.measure(chunk), 40 * self.CHAR)
            self.assertNotIn("...", chunk)

    def test_it_breaks_on_word_boundaries(self):
        lines = self.wrap("alpha bravo charlie delta echo foxtrot", 20)
        for chunk in lines:
            self.assertEqual(chunk, chunk.strip())
            self.assertFalse(chunk.startswith(" "))
        self.assertTrue(all(" " in chunk or len(chunk.split()) == 1
                            for chunk in lines))

    def test_it_never_exceeds_the_line_budget(self):
        lines = self.wrap(" ".join(["word"] * 200), 20)
        self.assertEqual(MAX_TEXT_LINES, len(lines))

    def test_text_beyond_the_budget_is_marked_as_cut(self):
        """When something really is dropped, say so.

        The ellipsis has to mean "there was more" - if it appeared on
        text that fit, it would train the reader to distrust captions
        that are in fact complete.
        """
        lines = self.wrap(" ".join(["word"] * 200), 20)
        self.assertTrue(lines[-1].endswith("..."))
        fits = self.wrap("alpha bravo charlie", 20)
        self.assertFalse(any(chunk.endswith("...") for chunk in fits))

    def test_a_single_unbreakable_word_still_advances(self):
        """A word wider than the card cannot be wrapped, only clipped -
        and it must not spin the wrapper forever."""
        lines = self.wrap("x" * 400 + " tail", 10)
        self.assertTrue(lines)
        self.assertLessEqual(len(lines), MAX_TEXT_LINES)
        for chunk in lines:
            self.assertLessEqual(self.measure(chunk), 10 * self.CHAR)

    def test_degenerate_inputs_do_not_raise(self):
        self.assertEqual([], wrap_text("anything", 0, self.measure))
        self.assertEqual([], wrap_text("anything", 100, self.measure, 0))
        self.assertEqual([""], self.wrap("", 40))
        self.assertEqual([""], self.wrap("   ", 40))


class WrappedHeightTests(unittest.TestCase):
    def test_a_wrapped_row_is_taller_by_exactly_its_extra_lines(self):
        row = line("MIRA", final=False)
        one = DEFAULT_METRICS.row_height(row, 1)
        three = DEFAULT_METRICS.row_height(row, 3)
        self.assertEqual(one + 2 * DEFAULT_METRICS.text_line_height, three)

    def test_the_canvas_fits_the_tallest_stack_it_can_draw(self):
        """The window is sized once, so it has to fit the worst case or a
        wrapped caption would be drawn off the top of the overlay."""
        rows = [line("A", final=False), line("B", final=True)]
        heights = [
            DEFAULT_METRICS.row_height(rows[0], MAX_TEXT_LINES),
            DEFAULT_METRICS.row_height(rows[1], MAX_TEXT_LINES),
        ]
        self.assertLessEqual(
            DEFAULT_METRICS.stacked(heights), overlay.CANVAS_HEIGHT)

    def test_a_single_line_stack_is_unchanged(self):
        rows = [line("A", final=False), line("B", final=True)]
        self.assertEqual(
            DEFAULT_METRICS.stack_height(rows),
            DEFAULT_METRICS.stacked(
                [DEFAULT_METRICS.row_height(r, 1) for r in rows]))

    def test_every_allowed_all_live_stack_fits_its_canvas(self):
        previous = 0
        for count in range(1, MAX_VISIBLE_SPEAKERS + 1):
            rows = [line(str(index), final=False) for index in range(count)]
            heights = [
                DEFAULT_METRICS.row_height(row, MAX_TEXT_LINES)
                for row in rows
            ]
            required = DEFAULT_METRICS.stacked(heights)
            canvas_height = overlay.canvas_height_for_rows(count)
            self.assertGreaterEqual(canvas_height, required)
            self.assertGreater(canvas_height, previous)
            self.assertGreaterEqual(canvas_height - required, 0)
            previous = canvas_height


class CardTransparencyTests(unittest.TestCase):
    """Card bodies at half opacity; the words and the REC tally solid.

    Tk has no per-pixel alpha, so a gray50 stipple dithers the card fill
    against the chroma key - alternating transparent pixels that read as
    a translucent backdrop over the game. The caption TEXT never carries
    a stipple: words must not fade with their backdrop. Neither does the
    REC privacy tally - an indicator at half strength reads half true.
    """

    def drawn(self, session_state="active"):
        canvas = FakeCanvas()
        subject = drawing_subject(canvas)
        subject.session_state = session_state
        subject.canvas_height = overlay.CANVAS_HEIGHT
        subject.recording_tally_size = lambda: (
            CaptionOverlay.recording_tally_size(subject)
        )
        subject.draw_recording_tally = lambda **kwargs: (
            CaptionOverlay.draw_recording_tally(subject, **kwargs)
        )
        subject.measure_card = lambda item, **kwargs: (
            CaptionOverlay.measure_card(subject, item, **kwargs)
        )
        subject.draw_line = lambda item, y, height, card: (
            CaptionOverlay.draw_line(subject, item, y, height, card)
        )
        CaptionOverlay.draw_lines(subject, [line("MIRA", final=False)])
        return canvas

    def test_caption_card_bodies_are_stippled_half_transparent(self):
        canvas = self.drawn()
        carded = [
            call for call in canvas.polygon_calls
            if call.get("stipple") == "gray50"
        ]
        self.assertTrue(carded, "no card body carries the gray50 stipple")

    def test_the_rec_tally_stays_fully_opaque(self):
        canvas = self.drawn()
        # The tally is the LAST polygon drawn (draw_lines then the tally
        # beside the newest card). It must carry no stipple.
        tally = canvas.polygon_calls[-1]
        self.assertNotEqual("gray50", tally.get("stipple", ""),
                            "the privacy tally faded with the cards")

    def test_no_text_item_ever_carries_a_stipple(self):
        canvas = self.drawn()
        for call in canvas.text_calls:
            self.assertNotIn("stipple", call)

    def test_each_saved_opacity_maps_to_the_expected_card_stipple(self):
        self.assertEqual("gray25", background_stipple(25))
        self.assertEqual("gray50", background_stipple(50))
        self.assertEqual("gray75", background_stipple(75))
        self.assertEqual("", background_stipple(100))

    def test_custom_text_and_background_do_not_replace_role_accents(self):
        canvas = FakeCanvas()
        subject = drawing_subject(canvas)
        subject.settings = OverlaySettings(
            caption_text_color="#abcdef",
            caption_background_color="#123456",
            caption_background_opacity=75,
        )
        row = line("MIRA", final=False)
        card = CaptionOverlay.measure_card(subject, row)
        CaptionOverlay.draw_line(subject, row, 0, card["height"], card)

        body = canvas.polygon_calls[0]
        caption = next(
            call for call in canvas.text_calls if call.get("text") == "Text"
        )
        name = next(
            call for call in canvas.text_calls if call.get("text") == "MIRA"
        )
        self.assertEqual("#123456", body["fill"])
        self.assertEqual("gray75", body["stipple"])
        self.assertEqual("#abcdef", caption["fill"])
        # One card style: a live card's outline is the same slate as a
        # settled one, and an unmapped name is muted on both. The role
        # colour stays on the accent bar at the left of every card.
        self.assertEqual(overlay.MUTED, name["fill"])
        self.assertEqual(overlay.SLATE, body["outline"])


class RecordingTallyTests(unittest.TestCase):
    def subject(self, session_state: str):
        canvas = FakeCanvas()
        subject = drawing_subject(canvas)
        subject.session_state = session_state
        subject.canvas_height = overlay.CANVAS_HEIGHT
        subject.recording_tally_size = lambda: (
            CaptionOverlay.recording_tally_size(subject)
        )
        subject.draw_recording_tally = lambda **kwargs: (
            CaptionOverlay.draw_recording_tally(subject, **kwargs)
        )
        subject.measure_card = lambda item, **kwargs: (
            CaptionOverlay.measure_card(subject, item, **kwargs)
        )
        subject.draw_line = lambda item, y, height, card: (
            CaptionOverlay.draw_line(subject, item, y, height, card)
        )
        return subject, canvas

    def test_active_session_draws_one_rec_beside_scrolling_names(self):
        subject, canvas = self.subject("active")
        CaptionOverlay.draw_lines(
            subject,
            [line("MIRA", final=False), line("DREX", final=False)],
        )
        self.assertIn("MIRA", canvas.texts)
        self.assertIn("DREX", canvas.texts)
        self.assertEqual(1, canvas.texts.count("REC"))
        self.assertEqual(overlay.DANGER, canvas.oval_calls[-1]["fill"])

    def test_paused_session_draws_no_rec(self):
        subject, canvas = self.subject("paused")
        CaptionOverlay.draw_lines(subject, [line("MIRA", final=False)])
        self.assertNotIn("REC", canvas.texts)

    def test_unknown_session_draws_rec_question_beside_names(self):
        subject, canvas = self.subject("unknown")
        CaptionOverlay.draw_lines(subject, [line("MIRA", final=False)])
        self.assertEqual(1, canvas.texts.count("REC?"))
        self.assertNotIn("REC", canvas.texts)

    def test_cold_unknown_draws_no_tally_beside_scrolling_names(self):
        subject, canvas = self.subject("unknown")
        subject.recording_state = ""
        CaptionOverlay.draw_lines(subject, [line("MIRA", final=False)])
        self.assertIn("MIRA", canvas.texts)
        self.assertNotIn("REC", canvas.texts)
        self.assertNotIn("REC?", canvas.texts)

    def test_max_font_tally_content_stays_inside_reserved_badge(self):
        subject, _canvas = self.subject("active")
        subject.font_size = MAX_CAPTION_FONT_SIZE
        subject.metrics = DEFAULT_METRICS.for_font_size(MAX_CAPTION_FONT_SIZE)
        subject.fonts["status"] = FakeFont(36)
        width, height = CaptionOverlay.recording_tally_size(subject)
        dot_size = round(6 * MAX_CAPTION_FONT_SIZE / BASE_FONT_SIZE)
        text_right = 7 + dot_size + 4 + subject.fonts["status"].measure("REC")
        self.assertLessEqual(text_right + 7, width)
        self.assertGreaterEqual(height, dot_size + 8)

        card = CaptionOverlay.measure_card(
            subject,
            line("MIRA", final=True),
            recording=True,
        )
        self.assertGreaterEqual(card["height"], height + 4)
        self.assertLessEqual(card["width"], subject.metrics.max_card_width)

    def test_capped_downward_feed_keeps_newest_caption_and_tally_visible(self):
        subject, _canvas = self.subject("active")
        canvas = CoordinateCanvas()
        subject.canvas = canvas
        subject.font_size = MAX_CAPTION_FONT_SIZE
        subject.metrics = metrics_for_target(
            MAX_CAPTION_FONT_SIZE,
            (0, 0, 1920, 1080),
        )
        subject.canvas_height = canvas_height_for_target(
            MAX_VISIBLE_SPEAKERS,
            MAX_CAPTION_FONT_SIZE,
            (0, 0, 1920, 1080),
        )
        subject.settings = OverlaySettings(
            caption_font_size=MAX_CAPTION_FONT_SIZE,
            max_visible_speakers=MAX_VISIBLE_SPEAKERS,
            growth="down",
        )
        rows = [
            line(f"SPEAKER{index}", final=False)
            for index in range(MAX_VISIBLE_SPEAKERS)
        ]

        CaptionOverlay.draw_lines(subject, rows)

        # Whatever the row height, the oldest rows are the ones dropped
        # and the newest is always there: the drawn names are a suffix
        # of the feed, and it is a proper suffix at this font size.
        drawn = []
        for name in canvas.texts:
            if name.startswith("SPEAKER") and name not in drawn:
                drawn.append(name)
        names = [f"SPEAKER{index}" for index in range(MAX_VISIBLE_SPEAKERS)]
        self.assertEqual(names[-len(drawn):], drawn)
        self.assertLess(len(drawn), MAX_VISIBLE_SPEAKERS, "nothing was capped")
        self.assertIn("SPEAKER19", canvas.texts)
        tally = next(
            call for call in canvas.text_calls if call.get("text") == "REC"
        )
        self.assertGreaterEqual(tally["coords"][1], 0)
        self.assertLessEqual(tally["coords"][1], subject.canvas_height)


class SessionStateDrainTests(unittest.TestCase):
    def subject(self, state="ready"):
        return SimpleNamespace(
            events=queue.Queue(),
            board=CaptionBoard(max_rows=2, final_hold_seconds=3.6),
            root=SimpleNamespace(quit=lambda: None),
            session_state=state,
            session_message="",
            settings=DEFAULT_SETTINGS,
        )

    @staticmethod
    def spoken_caption():
        return CaptionEvent(
            speaker_id="discord-mira",
            speaker="Mira",
            character="Mira",
            role="HEALER",
            text="Move left",
            final=True,
            sequence=1,
            timestamp=0.0,
        )

    def test_spoken_caption_repairs_a_missing_active_snapshot(self):
        subject = self.subject("unknown")
        subject.events.put(self.spoken_caption())
        self.assertTrue(CaptionOverlay.drain_events(subject, 0.0))
        self.assertEqual("active", subject.session_state)
        self.assertEqual("active", subject.recording_state)

    def test_cold_unknown_does_not_show_a_recording_warning(self):
        subject = self.subject("ready")
        subject.events.put(SessionEvent("unknown", "Status unavailable"))

        self.assertTrue(CaptionOverlay.drain_events(subject, 0.0))

        self.assertEqual("unknown", subject.session_state)
        self.assertEqual("", subject.recording_state)
        self.assertEqual("", overlay._effective_recording_state(subject))

    def test_active_then_unknown_keeps_the_uncertain_privacy_warning(self):
        subject = self.subject("ready")
        subject.events.put(SessionEvent("active"))
        subject.events.put(SessionEvent("unknown", "Status unavailable"))

        self.assertTrue(CaptionOverlay.drain_events(subject, 0.0))

        self.assertEqual("unknown", subject.session_state)
        self.assertEqual("unknown", subject.recording_state)

    def test_late_speech_cannot_override_explicit_inactive_state(self):
        for state in ("ready", "paused", "stopped", "error"):
            with self.subTest(state=state):
                subject = self.subject("ready")
                subject.events.put(SessionEvent(state))
                subject.events.put(
                    SessionEvent("unknown", "Status unavailable")
                )
                subject.events.put(self.spoken_caption())

                self.assertTrue(CaptionOverlay.drain_events(subject, 0.0))

                self.assertEqual("unknown", subject.session_state)
                self.assertEqual("", subject.recording_state)
                self.assertTrue(subject.session_state_confirmed)

    def test_typed_caption_does_not_claim_audio_is_active(self):
        subject = self.subject("paused")
        typed = self.spoken_caption()
        typed = CaptionEvent(
            **{**typed.__dict__, "speaker_id": "quick-chat-123"}
        )
        subject.events.put(typed)
        self.assertTrue(CaptionOverlay.drain_events(subject, 0.0))
        self.assertEqual("paused", subject.session_state)

    def test_cold_unknown_quick_chat_does_not_claim_audio_is_active(self):
        subject = self.subject("ready")
        typed = self.spoken_caption()
        typed = CaptionEvent(
            **{**typed.__dict__, "speaker_id": "quick-chat-123"}
        )
        subject.events.put(SessionEvent("unknown"))
        subject.events.put(typed)

        self.assertTrue(CaptionOverlay.drain_events(subject, 0.0))

        self.assertEqual("unknown", subject.session_state)
        self.assertEqual("", subject.recording_state)

    def test_a_locally_muted_voice_never_reaches_the_caption_board(self):
        subject = self.subject("active")
        speaker_id = "a" * 24
        subject.settings = OverlaySettings(muted_speaker_ids=(speaker_id,))
        subject.events.put(
            CaptionEvent(
                speaker_id=speaker_id,
                speaker="Mira",
                character="Mira",
                role="HEALER",
                text="Private local mute test",
                final=True,
                sequence=1,
                timestamp=0.0,
            )
        )

        CaptionOverlay.drain_events(subject, 0.0)

        self.assertEqual([], subject.board.visible(0.0))
        self.assertEqual(1, subject.board.last_sequence[speaker_id])


class PlacementRecordingTallyTests(unittest.TestCase):
    @staticmethod
    def subject(*, size: int = BASE_FONT_SIZE, rows: int = 3):
        canvas = CoordinateCanvas()
        scale = size / BASE_FONT_SIZE

        def font(base: int) -> FakeFont:
            return FakeFont(max(1, round(base * scale)))

        settings = OverlaySettings(
            caption_font_size=size,
            max_visible_speakers=rows,
        )
        subject = SimpleNamespace(
            canvas=canvas,
            canvas_height=overlay.canvas_height_for_rows(rows, size),
            settings=settings,
            fonts={
                "speaker": font(9),
                "caption": font(8),
                "previous": font(8),
                "status": font(7),
            },
        )
        subject.recording_tally_size = lambda: (
            placement.PlacementWindow.recording_tally_size(subject)
        )
        subject.draw_recording_tally = lambda **kwargs: (
            placement.PlacementWindow.draw_recording_tally(subject, **kwargs)
        )
        subject.draw_caption = lambda **kwargs: (
            placement.PlacementWindow.draw_caption(subject, **kwargs)
        )
        return subject, canvas

    def test_preview_draws_one_rec_on_the_newest_row(self):
        subject, canvas = self.subject(rows=3)

        placement.PlacementWindow.draw(subject)

        self.assertEqual(1, canvas.texts.count("REC"))
        newest = next(
            call for call in canvas.text_calls if call.get("text") == "Drex"
        )
        rec = next(
            call for call in canvas.text_calls if call.get("text") == "REC"
        )
        self.assertEqual(newest["coords"][1], rec["coords"][1])
        self.assertEqual(placement.DANGER, rec["fill"])
        self.assertEqual(placement.DANGER, canvas.oval_calls[-1]["fill"])

    def test_large_preview_reserves_the_full_tally_before_caption_text(self):
        subject, canvas = self.subject(size=MAX_CAPTION_FONT_SIZE, rows=1)
        width = 520
        x2 = (placement.WINDOW_WIDTH - width) // 2 + width

        placement.PlacementWindow.draw_caption(
            subject,
            y1=0,
            height=DEFAULT_METRICS.for_font_size(
                MAX_CAPTION_FONT_SIZE
            ).final_height,
            width=width,
            speaker="Mira",
            text="Move out of the dangerous ground effect immediately",
            color="#4fd182",
            live=True,
        )

        caption = next(
            call
            for call in canvas.text_calls
            if call.get("font") is subject.fonts["previous"]
        )
        tally_width, _height = subject.recording_tally_size()
        tally_left = x2 - 6 - tally_width
        caption_right = (
            caption["coords"][0]
            + subject.fonts["previous"].measure(caption["text"])
        )
        self.assertLessEqual(caption_right, tally_left - 6)
        self.assertTrue(caption["text"].endswith("..."))

    def test_large_tally_contains_its_scaled_dot_and_label(self):
        subject, canvas = self.subject(size=MAX_CAPTION_FONT_SIZE, rows=1)
        width, height = subject.recording_tally_size()
        dot_size = max(
            5,
            round(6 * MAX_CAPTION_FONT_SIZE / BASE_FONT_SIZE),
        )
        required_width = (
            7
            + dot_size
            + 4
            + subject.fonts["status"].measure("REC")
            + 7
        )
        self.assertGreaterEqual(width, required_width)
        self.assertGreaterEqual(height, dot_size + 8)

        subject.draw_recording_tally(right=500, center_y=100)

        rec = next(
            call for call in canvas.text_calls if call.get("text") == "REC"
        )
        self.assertLessEqual(
            rec["coords"][0] + subject.fonts["status"].measure("REC"),
            500 - 7,
        )
        dot = canvas.oval_calls[-1]["coords"]
        self.assertGreaterEqual(dot[0], 500 - width)
        self.assertLessEqual(dot[2], 500)


class FontSizeScalingTests(unittest.TestCase):
    """A larger caption font makes a larger overlay, not overflowing text."""

    def test_base_size_returns_the_same_metrics(self):
        self.assertIs(
            DEFAULT_METRICS, DEFAULT_METRICS.for_font_size(BASE_FONT_SIZE)
        )

    def test_vertical_dimensions_scale_with_the_font(self):
        doubled = DEFAULT_METRICS.for_font_size(BASE_FONT_SIZE * 2)
        self.assertEqual(DEFAULT_METRICS.live_height * 2, doubled.live_height)
        self.assertEqual(
            DEFAULT_METRICS.text_line_height * 2, doubled.text_line_height
        )
        self.assertEqual(
            DEFAULT_METRICS.max_card_width * 2, doubled.max_card_width
        )
        # The bottom-safe reference is a placement guard, not a glyph size,
        # so it stays put while the card grows.
        self.assertEqual(
            DEFAULT_METRICS.minimum_bottom_safe, doubled.minimum_bottom_safe
        )

    def test_bigger_font_needs_a_taller_canvas_for_the_same_rows(self):
        base = overlay.canvas_height_for_rows(4, BASE_FONT_SIZE)
        larger = overlay.canvas_height_for_rows(4, BASE_FONT_SIZE * 2)
        self.assertGreater(larger, base)

    def test_row_ceiling_is_a_valid_count_and_beyond_it_is_rejected(self):
        # Settings clamp the speaker count to this ceiling before it ever
        # reaches the canvas, so the ceiling itself must size and anything
        # past it is a guard against a value that cannot legitimately occur.
        self.assertGreater(overlay.canvas_height_for_rows(MAX_VISIBLE_SPEAKERS), 0)
        with self.assertRaises(ValueError):
            overlay.canvas_height_for_rows(MAX_VISIBLE_SPEAKERS + 1)
        with self.assertRaises(ValueError):
            overlay.canvas_height_for_rows(0)

    def test_instance_height_never_exceeds_the_screen(self):
        # A big font and many rows can want more height than any monitor
        # has; the overlay caps at the screen rather than running off it.
        subject = SimpleNamespace(root=SimpleNamespace(winfo_screenheight=lambda: 1080))
        height = CaptionOverlay._canvas_height_for(
            subject, MAX_VISIBLE_SPEAKERS, MAX_CAPTION_FONT_SIZE
        )
        self.assertLessEqual(height, int(1080 * 0.95))
        self.assertGreaterEqual(height, 1)

    def test_small_font_stays_readable_and_positive(self):
        height = overlay.canvas_height_for_rows(1, MIN_CAPTION_FONT_SIZE)
        self.assertGreater(height, 0)

    def test_width_fits_the_screen_so_cards_cannot_drift_off_the_edge(self):
        """Width scales with the font too.

        The window is anchored at the client's left edge once it is wider
        than the client, and its cards are centered in the canvas - so an
        uncapped canvas pushed the captions off the right of the screen.
        Capping the width wraps the text instead, which the member can
        read.
        """
        huge = DEFAULT_METRICS.for_font_size(MAX_CAPTION_FONT_SIZE)
        self.assertGreater(huge.canvas_width, 1920)
        fitted = huge.fitted_to_width(1920)
        self.assertEqual(1920, fitted.canvas_width)
        self.assertLess(fitted.max_card_width, fitted.canvas_width)
        self.assertLessEqual(fitted.min_card_width, fitted.max_card_width)
        # Heights are untouched: the member asked for a big font and gets
        # one; only the card's width gives way.
        self.assertEqual(huge.live_height, fitted.live_height)
        self.assertEqual(huge.text_line_height, fitted.text_line_height)

    def test_width_that_already_fits_is_left_exactly_alone(self):
        self.assertIs(DEFAULT_METRICS, DEFAULT_METRICS.fitted_to_width(1920))
        # A nonsense screen width must not produce a zero-width overlay.
        self.assertIs(DEFAULT_METRICS, DEFAULT_METRICS.fitted_to_width(0))

    def test_overlay_builds_its_metrics_through_the_screen_fit(self):
        subject = SimpleNamespace(
            root=SimpleNamespace(winfo_screenwidth=lambda: 1920)
        )
        metrics = CaptionOverlay._metrics_for(subject, MAX_CAPTION_FONT_SIZE)
        self.assertLessEqual(metrics.canvas_width, 1920)

    def test_preview_and_live_overlay_share_selected_1080p_geometry(self):
        target = (2560, 0, 1920, 1040)
        root = FakeRoot()
        root.SCREEN_WIDTH = 5120
        root.SCREEN_HEIGHT = 1440
        settings = OverlaySettings(
            caption_font_size=24,
            max_visible_speakers=8,
            anchor_mode="monitor",
            anchor_monitor=1,
        )

        preview_metrics, preview_height = placement.preview_surface(
            root, settings, target
        )
        live = SimpleNamespace(root=root, target_rect=target)
        live_metrics = CaptionOverlay._metrics_for(
            live, settings.caption_font_size
        )
        live_height = CaptionOverlay._canvas_height_for(
            live,
            settings.max_visible_speakers,
            settings.caption_font_size,
        )

        self.assertEqual(live_metrics, preview_metrics)
        self.assertEqual(live_height, preview_height)
        self.assertEqual(
            int(target[3] * 0.95),
            preview_height,
            "the primary monitor height leaked into a 1080p target",
        )

    def test_target_helpers_use_the_chosen_font_family_and_width(self):
        specs = caption_font_specs(20, "Verdana")
        self.assertEqual("Verdana", specs["caption"]["family"])
        self.assertEqual(20, specs["caption"]["size"])
        self.assertEqual(18, specs["speaker"]["size"])
        self.assertEqual("italic", specs["caption_whisper"]["slant"])
        self.assertEqual("bold", specs["caption_shout"]["weight"])
        self.assertEqual("italic", specs["previous_whisper"]["slant"])
        self.assertEqual("bold", specs["previous_shout"]["weight"])
        # The same face whether the line is live or settled.
        self.assertEqual("previous_whisper", caption_font_key(True, "whisper"))
        self.assertEqual("previous_shout", caption_font_key(False, "shout"))
        metrics = metrics_for_target(48, (0, 0, 1920, 1080))
        self.assertEqual(1920, metrics.canvas_width)
        self.assertEqual(
            int(1080 * 0.95),
            canvas_height_for_target(20, 48, (0, 0, 1920, 1080)),
        )


class LiveDisplaySettingTests(unittest.TestCase):
    @staticmethod
    def placement_subject(
        settings: OverlaySettings,
        *,
        target_rect: tuple[int, int, int, int] = (0, 0, 1920, 1080),
        x: int = 100,
        y: int = 200,
    ):
        canvas_height = overlay.canvas_height_for_rows(
            settings.max_visible_speakers
        )
        subject = SimpleNamespace(
            settings_path=Path("unused-settings.json"),
            settings_mtime_ns=-1,
            settings=settings,
            canvas_height=canvas_height,
            window_height=canvas_height,
            overlay_width=placement.WINDOW_WIDTH,
            window_width=placement.WINDOW_WIDTH,
            preview_offset_x=0,
            target_rect=target_rect,
            canvas=mock.Mock(),
            root=FakeRoot(x, y),
            draw=mock.Mock(),
            drag_origin=None,
            drag_moved=False,
            drag_bounds=None,
            panel_offset=None,
            panel_size=None,
            work_areas=(),
            shield=None,
            flash=lambda _value: None,
        )
        subject.update_control_bounds = lambda: (
            placement.PlacementWindow.update_control_bounds(subject)
        )
        subject.position_controls = lambda *_args, **_kwargs: None
        subject.sync_shield = lambda *_args, **_kwargs: None
        # Stubbed like the panel and the grab layer: the tracking
        # tests below drive the real method on their own subject,
        # so these stay independent of the machine's monitors.
        subject.refresh_target = lambda *_args, **_kwargs: None
        subject.place_preview = lambda x, y, resize=False, settle=False: (
            placement.PlacementWindow.place_preview(
                subject, x, y, resize=resize, settle=settle
            )
        )
        subject.begin_drag = lambda event: (
            placement.PlacementWindow.begin_drag(subject, event)
        )
        subject.rebase_drag = lambda x, y: (
            placement.PlacementWindow.rebase_drag(subject, x, y)
        )
        subject.drag = lambda event: (
            placement.PlacementWindow.drag(subject, event)
        )
        subject.release = lambda event: (
            placement.PlacementWindow.release(subject, event)
        )
        subject.panel_press = lambda event: (
            placement.PlacementWindow.panel_press(subject, event)
        )
        subject.panel_drag = lambda event: (
            placement.PlacementWindow.panel_drag(subject, event)
        )
        subject.panel_release = lambda event: (
            placement.PlacementWindow.panel_release(subject, event)
        )
        subject.apply_settings = lambda updated, reposition: (
            placement.PlacementWindow.apply_settings(
                subject, updated, reposition=reposition
            )
        )
        subject.refresh_settings = lambda: (
            placement.PlacementWindow.refresh_settings(subject)
        )
        return subject

    def test_overlay_applies_rows_and_hold_without_replacing_the_board(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "overlay-settings.json"
            updated = OverlaySettings(
                caption_hold_seconds=6.0,
                max_visible_speakers=4,
            )
            self.assertTrue(save_settings(updated, path))
            board = mock.Mock()
            canvas = mock.Mock()
            subject = SimpleNamespace(
                settings_path=path,
                settings_mtime_ns=-1,
                settings=DEFAULT_SETTINGS,
                font_size=DEFAULT_SETTINGS.caption_font_size,
                metrics=DEFAULT_METRICS,
                board=board,
                canvas_height=overlay.CANVAS_HEIGHT,
                canvas=canvas,
                last_signature=("old",),
                # A tall virtual screen so the height clamp is a no-op here;
                # the clamp itself is exercised in FontSizeScalingTests.
                root=SimpleNamespace(
                    winfo_screenheight=lambda: 100000,
                    winfo_screenwidth=lambda: 100000,
                ),
            )
            subject._canvas_height_for = lambda rows, size: (
                CaptionOverlay._canvas_height_for(subject, rows, size)
            )
            subject._metrics_for = lambda size: (
                CaptionOverlay._metrics_for(subject, size)
            )
            subject._make_fonts = lambda size: (
                CaptionOverlay._make_fonts(subject, size)
            )

            CaptionOverlay.refresh_settings(subject)

            board.configure_display.assert_called_once_with(
                max_rows=4,
                final_hold_seconds=6.0,
            )
            self.assertIs(subject.board, board)
            self.assertEqual(4, subject.settings.max_visible_speakers)
            self.assertEqual(
                overlay.canvas_height_for_rows(4), subject.canvas_height
            )
            canvas.configure.assert_called_once_with(height=subject.canvas_height)
            self.assertIsNone(subject.last_signature)

            board.reset_mock()
            CaptionOverlay.refresh_settings(subject)
            board.configure_display.assert_not_called()

    def test_a_new_specific_mute_purges_that_speaker_immediately(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "overlay-settings.json"
            speaker_id = "b" * 24
            self.assertTrue(
                save_settings(
                    OverlaySettings(muted_speaker_ids=(speaker_id,)),
                    path,
                )
            )
            board = mock.Mock()
            subject = SimpleNamespace(
                settings_path=path,
                settings_mtime_ns=-1,
                settings=DEFAULT_SETTINGS,
                font_size=DEFAULT_SETTINGS.caption_font_size,
                metrics=DEFAULT_METRICS,
                board=board,
                canvas_height=overlay.CANVAS_HEIGHT,
                canvas=mock.Mock(),
                last_signature=("old",),
                root=SimpleNamespace(
                    winfo_screenheight=lambda: 100000,
                    winfo_screenwidth=lambda: 100000,
                ),
            )
            subject._canvas_height_for = lambda rows, size: (
                CaptionOverlay._canvas_height_for(subject, rows, size)
            )
            subject._metrics_for = lambda size: (
                CaptionOverlay._metrics_for(subject, size)
            )
            subject._make_fonts = lambda size: {"caption": FakeFont(size)}

            CaptionOverlay.refresh_settings(subject)

            board.remove_speakers.assert_called_once_with({speaker_id})

    def test_drag_and_nudge_can_leave_the_selected_target(self):
        subject = self.placement_subject(
            DEFAULT_SETTINGS,
            target_rect=(0, 0, 1280, 720),
            x=40,
            y=30,
        )
        subject.drag_origin = (200, 180, subject.root.x, subject.root.y)
        placement.PlacementWindow.drag(
            subject, SimpleNamespace(x_root=-100, y_root=-120)
        )
        self.assertEqual((-260, -270), (subject.root.x, subject.root.y))

        placement.PlacementWindow.nudge(subject, -20, -15)
        self.assertEqual((-280, -285), (subject.root.x, subject.root.y))

    class FakePanel:
        """Just enough of a Toplevel for position_controls to drive."""

        def __init__(self, width: int = 481, height: int = 153) -> None:
            self.width = width
            self.height = height
            self.geometries: list[str] = []
            self.lifts = 0

        def update_idletasks(self) -> None:
            pass

        def winfo_reqwidth(self) -> int:
            return self.width

        def winfo_reqheight(self) -> int:
            return self.height

        def geometry(self, value: str) -> None:
            self.geometries.append(value)

        def lift(self) -> None:
            self.lifts += 1

        def origin(self) -> tuple[int, int]:
            size, position = self.geometries[-1].split("+", 1)
            x, y = position.split("+")
            return int(x), int(y)

    def test_the_panel_translates_with_the_captions_instead_of_re_solving(self):
        """The 1080p desync, at the level that actually decides it.

        panel_rigid_position being correct is not enough: position_controls
        has to USE it while the captions move, rather than re-deriving the
        panel's place from a rule with a 354 pixel cliff in it.
        """
        subject = self.placement_subject(DEFAULT_SETTINGS, x=690, y=758)
        panel = self.FakePanel()
        subject.controls = panel
        subject.target_rect = (0, 0, 1920, 1080)
        subject.work_areas = ((0, 0, 1920, 1080),)
        subject.overlay_width = 540
        subject.canvas_height = 190
        subject.position_controls = lambda *args, **kwargs: (
            placement.PlacementWindow.position_controls(subject, *args, **kwargs)
        )

        subject.position_controls((690, 758), settle=True)
        settled = panel.origin()
        self.assertEqual((29, -159), subject.panel_offset)

        # One pixel across the line the shipped rule flipped at.
        subject.position_controls((690, 731))
        before = panel.origin()
        subject.position_controls((690, 732))
        after = panel.origin()
        self.assertEqual(1, after[1] - before[1])
        self.assertEqual(settled[0], after[0])

        # A hundred pixels of caption travel moves the panel a hundred.
        subject.position_controls((790, 858))
        self.assertEqual((settled[0] + 100, settled[1] + 100), panel.origin())
        # And the latched offset is never rewritten by a tracked move.
        self.assertEqual((29, -159), subject.panel_offset)

    def test_a_live_drag_cannot_be_re_solved_out_from_under_the_pointer(self):
        subject = self.placement_subject(DEFAULT_SETTINGS, x=690, y=758)
        panel = self.FakePanel()
        subject.controls = panel
        subject.target_rect = (0, 0, 1920, 1080)
        subject.work_areas = ((0, 0, 1920, 1080),)
        subject.overlay_width = 540
        subject.canvas_height = 190
        subject.position_controls = lambda *args, **kwargs: (
            placement.PlacementWindow.position_controls(subject, *args, **kwargs)
        )
        subject.position_controls((690, 758), settle=True)
        lifts = panel.lifts

        subject.drag_origin = (0, 0, 690, 758)
        # Every settle caller - the poll, a live appearance change, a
        # resize - is downgraded to a track while a drag is live.
        subject.position_controls((690, 732), settle=True)
        self.assertEqual(lifts, panel.lifts)
        self.assertEqual((29, -159), subject.panel_offset)

    def test_releasing_a_drag_refits_the_preview_immediately(self):
        subject = self.placement_subject(DEFAULT_SETTINGS, x=100, y=200)
        subject.last_target_check = 999.0
        calls = []
        subject.refresh_target = lambda: calls.append(1)
        placement.PlacementWindow.release(subject, SimpleNamespace())
        self.assertEqual(-placement.TARGET_REFRESH_SECONDS, subject.last_target_check)
        self.assertEqual(1, len(calls))

        # A subject without target tracking survives release untouched -
        # the shape several existing drag tests use.
        bare = self.placement_subject(DEFAULT_SETTINGS, x=100, y=200)
        del bare.refresh_target
        placement.PlacementWindow.release(bare, SimpleNamespace())
        self.assertIsNone(bare.drag_origin)

    def test_save_without_a_drag_writes_back_the_anchor_it_loaded(self):
        """Opening Move and pressing Save must not move anybody's captions.

        Today Save re-derives anchor, anchor_y and growth from pixels
        every time. Measured: growth "down" with anchor_y 0.10 at 20
        rows came back as "up" with 0.918056, which moves the live
        overlay from y=13 to y=60 on a 1080p.
        """
        loaded = OverlaySettings(
            max_visible_speakers=20, growth="down", anchor_x=0.25, anchor_y=0.10
        )
        subject = self.placement_subject(loaded, x=100, y=200)
        subject.preview = False
        subject.position_dirty = False
        saved = []
        with mock.patch.object(placement, "load_settings", return_value=loaded), \
                mock.patch.object(
                    placement, "save_settings",
                    side_effect=lambda value, *a, **k: saved.append(value) or True,
                ):
            placement.PlacementWindow.save(subject)
        self.assertEqual(1, len(saved))
        self.assertEqual("down", saved[0].growth)
        self.assertEqual(0.10, saved[0].anchor_y)
        self.assertEqual(0.25, saved[0].anchor_x)

    def test_save_after_a_drag_measures_the_display_the_captions_are_on(self):
        loaded = OverlaySettings()
        subject = self.placement_subject(loaded, x=5660, y=800)
        subject.preview = False
        subject.position_dirty = True
        subject.target_rect = (5120, 0, 1920, 1080)
        saved = []
        with mock.patch.object(placement, "load_settings", return_value=loaded), \
                mock.patch.object(
                    placement, "save_settings",
                    side_effect=lambda value, *a, **k: saved.append(value) or True,
                ):
            placement.PlacementWindow.save(subject)
        self.assertEqual(1, len(saved))
        self.assertAlmostEqual(0.421875, saved[0].anchor_x, places=6)
        # And it round-trips back to exactly where it was dropped.
        self.assertEqual(
            (5660, 800),
            placement.position_from_settings(
                (5120, 0, 1920, 1080),
                (subject.overlay_width, subject.canvas_height),
                saved[0],
                bottom_reference_height=placement.BOTTOM_REFERENCE_HEIGHT,
            ),
        )
        # Against the primary - the rect Move used before this change -
        # the same drop saved 1.158203 and landed off the panel.
        stale = placement.settings_from_position(
            (0, 0, 5120, 1440),
            (subject.overlay_width, subject.canvas_height),
            (5660, 800),
            current=loaded,
            bottom_reference_height=placement.BOTTOM_REFERENCE_HEIGHT,
        )
        self.assertAlmostEqual(1.158203, stale.anchor_x, places=6)

    def test_a_press_on_a_panel_control_does_not_start_a_drag(self):
        """Save, Done, the spinner and the dropdowns are not drag handles.

        A binding on the panel toplevel would have been one line and
        would have made all of them drag, because their class scripts do
        not return break.
        """
        subject = self.placement_subject(DEFAULT_SETTINGS, x=100, y=200)

        def press_on(widget_class):
            widget = SimpleNamespace(winfo_class=lambda: widget_class)
            return subject.panel_press(
                SimpleNamespace(x_root=250, y_root=260, widget=widget)
            )

        self.assertEqual("break", press_on("Frame"))
        self.assertIsNotNone(subject.drag_origin)

        for widget_class in ("Button", "Spinbox", "Menubutton", "Menu"):
            subject.drag_origin = None
            self.assertIsNone(press_on(widget_class))
            self.assertIsNone(subject.drag_origin)
            subject.root.geometries.clear()
            self.assertIsNone(
                subject.panel_drag(SimpleNamespace(x_root=350, y_root=310))
            )
            self.assertEqual([], subject.root.geometries)

    def test_panel_drag_moves_the_preview_without_taking_focus(self):
        subject = self.placement_subject(DEFAULT_SETTINGS, x=100, y=200)
        widget = SimpleNamespace(winfo_class=lambda: "Frame")
        with mock.patch.object(placement, "monitor_work_areas", return_value=()):
            self.assertEqual(
                "break",
                subject.panel_press(
                    SimpleNamespace(x_root=250, y_root=260, widget=widget)
                ),
            )
            self.assertEqual(
                "break",
                subject.panel_drag(SimpleNamespace(x_root=350, y_root=310)),
            )
        self.assertEqual((200, 250), (subject.root.x, subject.root.y))
        self.assertEqual(
            "break", subject.panel_release(SimpleNamespace(x_root=350, y_root=310))
        )
        self.assertIsNone(subject.drag_origin)
        # A panel press must not pull focus out of a half-typed size,
        # whose FocusOut commits it.
        subject.canvas.focus_set.assert_not_called()

        # The card surface still does take focus, so arrow-key nudging
        # keeps working after a drag on the captions themselves.
        placement.PlacementWindow.press_card(
            subject, SimpleNamespace(x_root=250, y_root=260)
        )
        subject.canvas.focus_set.assert_called()

    def test_a_press_that_does_not_move_writes_no_geometry(self):
        subject = self.placement_subject(DEFAULT_SETTINGS, x=100, y=200)
        with mock.patch.object(placement, "monitor_work_areas", return_value=()):
            subject.begin_drag(SimpleNamespace(x_root=250, y_root=260))
        subject.root.geometries.clear()

        subject.drag(SimpleNamespace(x_root=252, y_root=260))
        self.assertEqual([], subject.root.geometries)
        self.assertEqual((100, 200), (subject.root.x, subject.root.y))

        # Once it trips, the FULL delta lands, not the delta minus the
        # threshold - otherwise the captions trail the pointer forever.
        subject.drag(SimpleNamespace(x_root=254, y_root=260))
        self.assertEqual(1, len(subject.root.geometries))
        self.assertEqual((104, 200), (subject.root.x, subject.root.y))

        # Latched: a further single pixel now moves.
        subject.drag(SimpleNamespace(x_root=255, y_root=260))
        self.assertEqual((105, 200), (subject.root.x, subject.root.y))

    def test_a_resize_mid_drag_rebases_the_drag_instead_of_dropping_it(self):
        subject = self.placement_subject(DEFAULT_SETTINGS, x=100, y=200)
        with mock.patch.object(placement, "monitor_work_areas", return_value=()):
            subject.begin_drag(SimpleNamespace(x_root=250, y_root=260))
        subject.drag(SimpleNamespace(x_root=300, y_root=300))
        moved = (subject.root.x, subject.root.y)

        subject.root.pointer = (400, 380)
        subject.apply_settings(
            OverlaySettings(max_visible_speakers=6), reposition=False
        )
        self.assertIsNotNone(subject.drag_origin)
        self.assertEqual(
            (400, 380), (subject.drag_origin[0], subject.drag_origin[1])
        )
        self.assertEqual(
            (subject.root.x, subject.root.y),
            (subject.drag_origin[2], subject.drag_origin[3]),
        )

        # Ten more pixels of pointer travel move the captions ten pixels,
        # with none of the accumulated delta replayed.
        before = (subject.root.x, subject.root.y)
        subject.drag(SimpleNamespace(x_root=410, y_root=380))
        self.assertEqual((before[0] + 10, before[1]), (subject.root.x, subject.root.y))

        # A root that cannot report a pointer ends the drag cleanly
        # rather than leaving it inconsistent.
        with mock.patch.object(placement, "monitor_work_areas", return_value=()):
            subject.begin_drag(SimpleNamespace(x_root=250, y_root=260))
        subject.root.winfo_pointerxy = mock.Mock(side_effect=tk.TclError("gone"))
        subject.rebase_drag(1, 2)
        self.assertIsNone(subject.drag_origin)
        self.assertEqual(moved, moved)

    def test_no_pointer_gesture_writes_settings(self):
        """A drag the member has not saved must not touch their anchor."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "overlay-settings.json"
            self.assertTrue(save_settings(DEFAULT_SETTINGS, path))
            before = path.read_bytes()
            before_mtime = path.stat().st_mtime_ns

            subject = self.placement_subject(DEFAULT_SETTINGS, x=100, y=200)
            subject.settings_path = path
            widget = SimpleNamespace(winfo_class=lambda: "Label")
            with mock.patch.object(
                placement, "monitor_work_areas", return_value=()
            ):
                subject.panel_press(
                    SimpleNamespace(x_root=250, y_root=260, widget=widget)
                )
                subject.panel_drag(SimpleNamespace(x_root=350, y_root=310))
                subject.panel_release(SimpleNamespace(x_root=350, y_root=310))
                placement.PlacementWindow.press_card(
                    subject, SimpleNamespace(x_root=250, y_root=260)
                )
                placement.PlacementWindow.drag_card(
                    subject, SimpleNamespace(x_root=400, y_root=400)
                )
                placement.PlacementWindow.release_card(
                    subject, SimpleNamespace(x_root=400, y_root=400)
                )

            self.assertEqual(before, path.read_bytes())
            self.assertEqual(before_mtime, path.stat().st_mtime_ns)

    def test_drag_starting_on_caption_text_moves_the_preview_once(self):
        subject = self.placement_subject(DEFAULT_SETTINGS, x=100, y=200)
        event = SimpleNamespace(x_root=250, y_root=260)

        self.assertEqual(
            "break", placement.PlacementWindow.press_card(subject, event)
        )
        self.assertEqual(
            "break",
            placement.PlacementWindow.drag_card(
                subject,
                SimpleNamespace(x_root=350, y_root=310),
            ),
        )
        self.assertEqual((200, 250), (subject.root.x, subject.root.y))
        self.assertEqual(
            "break", placement.PlacementWindow.release_card(subject, event)
        )
        self.assertIsNone(subject.drag_origin)

    def test_appearance_write_merges_settings_saved_since_the_last_poll(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "overlay-settings.json"
            stale = OverlaySettings(
                focus_mode=False,
                max_visible_speakers=2,
                muted=False,
            )
            latest = OverlaySettings(
                focus_mode=True,
                max_visible_speakers=8,
                muted=True,
            )
            self.assertTrue(save_settings(latest, path))
            subject = SimpleNamespace(
                preview=False,
                settings_path=path,
                settings=stale,
                target_rect=(0, 0, 1920, 1080),
                root=FakeRoot(),
                apply_settings=mock.Mock(),
                sync_appearance_controls=mock.Mock(),
                flash=mock.Mock(),
            )

            self.assertTrue(
                placement.PlacementWindow.apply_appearance(
                    subject,
                    "caption_font_size",
                    18,
                )
            )

            saved = load_settings(path)
            self.assertTrue(saved.focus_mode)
            self.assertTrue(saved.muted)
            self.assertEqual(8, saved.max_visible_speakers)
            self.assertEqual(18, saved.caption_font_size)
            subject.apply_settings.assert_called_once_with(
                saved,
                reposition=False,
            )

    def test_a_font_size_change_rescales_metrics_and_grows_the_canvas(self):
        """Changing the size in the companion has to reach the live overlay.

        The size is polled out of the settings file, so if refresh_settings
        does not rebuild the metrics and fonts, a member types a bigger
        number, watches the field accept it, and the captions never change
        - which reads as the setting being broken rather than unapplied.
        """
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "overlay-settings.json"
            self.assertTrue(
                save_settings(
                    OverlaySettings(caption_font_size=20, max_visible_speakers=2),
                    path,
                )
            )
            canvas = mock.Mock()
            rebuilt = []
            subject = SimpleNamespace(
                settings_path=path,
                settings_mtime_ns=-1,
                settings=DEFAULT_SETTINGS,
                font_size=DEFAULT_SETTINGS.caption_font_size,
                metrics=DEFAULT_METRICS,
                board=mock.Mock(),
                canvas_height=overlay.CANVAS_HEIGHT,
                canvas=canvas,
                last_signature=("old",),
                root=SimpleNamespace(
                    winfo_screenheight=lambda: 100000,
                    winfo_screenwidth=lambda: 100000,
                ),
            )
            subject._canvas_height_for = lambda rows, size: (
                CaptionOverlay._canvas_height_for(subject, rows, size)
            )
            subject._metrics_for = lambda size: (
                CaptionOverlay._metrics_for(subject, size)
            )
            subject._make_fonts = lambda size: (
                rebuilt.append(size) or {"caption": FakeFont(size)}
            )

            CaptionOverlay.refresh_settings(subject)

            self.assertEqual(20, subject.font_size)
            self.assertEqual([20], rebuilt)
            self.assertEqual(
                DEFAULT_METRICS.for_font_size(20), subject.metrics
            )
            self.assertGreater(
                subject.canvas_height,
                overlay.canvas_height_for_rows(2, BASE_FONT_SIZE),
            )
            canvas.configure.assert_called_once_with(height=subject.canvas_height)

    def test_the_dragged_bottom_edge_survives_any_font_size(self):
        """The invariant the whole placement scheme rests on.

        The member drags the preview to sit clear of their action bars and
        presses Save. Whatever font and row count they later choose, the
        overlay's BOTTOM edge has to land on that same line - the stack
        grows upward into empty screen, never downward over the bars they
        deliberately avoided. Preview and overlay can be different heights
        (they are, at any non-default font) precisely because both measure
        from the same fixed bottom reference.
        """
        client = (0, 0, 1920, 1080)
        for font_size in (MIN_CAPTION_FONT_SIZE, 10, 18, MAX_CAPTION_FONT_SIZE):
            for rows in (1, 2, 8, MAX_VISIBLE_SPEAKERS):
                settings = OverlaySettings(
                    caption_font_size=font_size, max_visible_speakers=rows
                )
                preview_height = overlay.canvas_height_on_screen(
                    FakeRoot(), rows, font_size
                )
                # An ordinary in-target placement keeps this baseline
                # invariant independent of the now-unbounded drag range.
                dragged_y = max(0, 1080 - preview_height - 120)
                saved = placement.settings_from_position(
                    client,
                    (placement.WINDOW_WIDTH, preview_height),
                    (400, dragged_y),
                    current=settings,
                    bottom_reference_height=placement.BOTTOM_REFERENCE_HEIGHT,
                )
                overlay_height = overlay.canvas_height_on_screen(
                    FakeRoot(), rows, font_size
                )
                _x, overlay_y = placement.position_from_settings(
                    client,
                    (DEFAULT_METRICS.canvas_width, overlay_height),
                    saved,
                    bottom_reference_height=overlay.CANVAS_HEIGHT,
                )
                self.assertEqual(
                    dragged_y + preview_height,
                    overlay_y + overlay_height,
                    msg=(
                        "bottom edge moved at font %d with %d rows"
                        % (font_size, rows)
                    ),
                )

    def test_placement_preview_grows_with_the_font(self):
        small = overlay.canvas_height_on_screen(FakeRoot(), 2, 10)
        large = overlay.canvas_height_on_screen(FakeRoot(), 2, 20)
        self.assertGreater(large, small)
        # The preview's own text scales too, so the box the member drags
        # looks like the captions they will read. Asserted on the source
        # because building a tkfont needs a live Tk root, which these
        # tests deliberately do without.
        source = (
            Path(placement.__file__).read_text(encoding="utf-8")
        )
        self.assertIn("def preview_fonts", source)
        self.assertIn("caption_font_specs(size, family)", source)
        self.assertIn("updated.caption_font_family", source)

    def test_placement_reset_changes_only_the_position(self):
        current = OverlaySettings(
            anchor_x=0.2,
            anchor_y=0.3,
            focus_mode=True,
            caption_hold_seconds=10.0,
            max_visible_speakers=4,
        )
        saved = []
        subject = self.placement_subject(
            current,
            target_rect=(0, 0, 1280, 720),
        )
        with mock.patch.object(placement, "load_settings", return_value=current), \
                mock.patch.object(
                    placement,
                    "save_settings",
                    side_effect=lambda value: saved.append(value) or True,
                ):
            placement.PlacementWindow.reset(subject)

        self.assertEqual(1, len(saved))
        reset = saved[0]
        self.assertEqual(DEFAULT_SETTINGS.anchor_x, reset.anchor_x)
        self.assertEqual(DEFAULT_SETTINGS.anchor_y, reset.anchor_y)
        self.assertTrue(reset.focus_mode)
        self.assertEqual(10.0, reset.caption_hold_seconds)
        self.assertEqual(4, reset.max_visible_speakers)
        self.assertEqual(reset, subject.settings)
        self.assertTrue(subject.root.geometries)
        expected = placement.position_from_settings(
            subject.target_rect,
            (placement.WINDOW_WIDTH, subject.canvas_height),
            reset,
            bottom_reference_height=placement.BOTTOM_REFERENCE_HEIGHT,
        )
        self.assertEqual(expected, (subject.root.x, subject.root.y))

    def test_open_placement_preview_tracks_a_row_count_change(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "overlay-settings.json"
            updated = OverlaySettings(max_visible_speakers=4)
            self.assertTrue(save_settings(updated, path))
            subject = self.placement_subject(DEFAULT_SETTINGS)
            subject.settings_path = path

            placement.PlacementWindow.refresh_settings(subject)

            self.assertEqual(4, subject.settings.max_visible_speakers)
            self.assertEqual(
                overlay.canvas_height_for_rows(4), subject.canvas_height
            )
            subject.canvas.configure.assert_called_once_with(
                height=subject.canvas_height
            )
            subject.draw.assert_called_once_with()
            self.assertTrue(subject.root.geometries)
            self.assertEqual(250, subject.root.scheduled[0][0])

    def test_save_repairs_a_row_change_that_beats_the_poll_timer(self):
        current = DEFAULT_SETTINGS
        updated = OverlaySettings(max_visible_speakers=4)
        subject = self.placement_subject(current, x=200, y=300)
        old_bottom = subject.root.y + subject.canvas_height
        saved = []
        with mock.patch.object(placement, "load_settings", return_value=updated), \
                mock.patch.object(
                    placement,
                    "save_settings",
                    side_effect=lambda value: saved.append(value) or True,
                ):
            placement.PlacementWindow.save(subject)

        self.assertEqual(overlay.canvas_height_for_rows(4), subject.canvas_height)
        self.assertEqual(old_bottom, subject.root.y + subject.canvas_height)
        self.assertEqual(4, saved[0].max_visible_speakers)


class PlacementControlWindowTests(unittest.TestCase):
    TARGET = (2560, 0, 1920, 1040)

    def make_hidden_window(self, *, scaling: float | None = None):
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        if scaling is not None:
            root.tk.call("tk", "scaling", scaling)
        with mock.patch.object(
            placement,
            "resolve_anchor_rect",
            return_value=self.TARGET,
        ), mock.patch.object(
            placement,
            "monitor_work_areas",
            return_value=(self.TARGET,),
        ):
            view = placement.PlacementWindow(root, preview=True)
        root.update_idletasks()
        view.controls.update_idletasks()
        return root, view

    def test_the_built_font_menu_fits_on_the_display(self):
        """Real Tk: the menu Move actually builds, against a real list."""
        root, view = self.make_hidden_window()
        try:
            inner = view.font_family_menu["menu"]
            inner.update_idletasks()
            entries = inner.index("end")
            self.assertIsNotNone(entries)
            self.assertEqual(len(view.font_families), entries + 1)
            for family in view.font_families:
                self.assertFalse(family.startswith("@") and family != view.settings.caption_font_family)
            # Column breaks are set, and the first entry never carries
            # one (that would open the menu with an empty column).
            breaks = [
                index
                for index in range(entries + 1)
                if str(inner.entrycget(index, "columnbreak")) not in ("0", "")
            ]
            self.assertNotIn(0, breaks)
            if len(view.font_families) > 60:
                self.assertTrue(breaks, "a long list must break into columns")
            self.assertLessEqual(
                inner.winfo_reqheight(), self.TARGET[3]
            )
        finally:
            root.destroy()

    def test_only_inert_panel_chrome_carries_the_drag_binding(self):
        root, view = self.make_hidden_window()
        try:
            bound = []
            pending = list(view.controls.winfo_children())
            while pending:
                widget = pending.pop()
                pending.extend(widget.winfo_children())
                has_press = "<Button-1>" in widget.bind()
                if widget.winfo_class() in placement.DRAG_SURFACE_CLASSES:
                    self.assertTrue(
                        has_press,
                        "%s should drag" % widget.winfo_class(),
                    )
                    bound.append(widget)
                else:
                    self.assertFalse(
                        has_press,
                        "%s must not drag" % widget.winfo_class(),
                    )
            # The toplevel itself carries no pointer binding: one there
            # fires for a press on every descendant, so Save, Done, the
            # spinner and both dropdowns would all start a drag.
            self.assertNotIn("<Button-1>", view.controls.bind())
            self.assertIn(view.heading_label, bound)
            self.assertIn(view.font_label, bound)
            self.assertEqual("fleur", str(view.heading_label.cget("cursor")))
            self.assertEqual("fleur", str(view.font_label.cget("cursor")))
        finally:
            root.destroy()

    def test_the_shield_is_transparent_by_alpha_and_never_by_colour_key(self):
        if sys.platform != "win32":
            self.skipTest("colour-key transparency is a Windows behaviour")
        root, view = self.make_hidden_window()
        try:
            self.assertIsNotNone(view.shield)
            self.assertTrue(view.shield_ready)
            self.assertLessEqual(
                float(view.shield.wm_attributes("-alpha")),
                placement.SHIELD_ALPHA + 1e-6,
            )
            # The single most valuable assertion here. A colour key on
            # the grab layer would silently restore the whole defect:
            # every keyed pixel goes click-through again and half the
            # card stops responding at the default backdrop.
            self.assertEqual("", str(view.shield.wm_attributes("-transparentcolor")))
            self.assertEqual(placement.VOID, str(view.shield.cget("bg")))
            self.assertNotEqual(placement.CHROMA_KEY, str(view.shield.cget("bg")))
            self.assertTrue(view.shield.overrideredirect())
        finally:
            root.destroy()

    def test_the_shield_tracks_the_preview_and_stays_under_the_panel(self):
        root, view = self.make_hidden_window()
        try:
            if view.shield is None:
                self.skipTest("no grab layer on this platform")

            # Position from the requested geometry, not winfo_*:
            # nothing here is mapped, and an unmapped window reports 0,0
            # for its position and its default 200x200 for its size,
            # whatever was requested. Position is what the tracking
            # defect is about - a grab layer that drifts off the cards.
            def origin(window):
                window.update_idletasks()
                return window.geometry().split("+", 1)[1]

            def geometries():
                return origin(view.shield)

            def preview():
                return origin(view.root)

            self.assertEqual(preview(), geometries())
            view.apply_settings(
                OverlaySettings(caption_font_size=24, max_visible_speakers=8),
                reposition=True,
            )
            self.assertEqual(preview(), geometries())
            view.place_preview(
                view.root.winfo_x() + 40, view.root.winfo_y() + 25
            )
            self.assertEqual(preview(), geometries())

            order = [str(name) for name in root.tk.call("wm", "stackorder", ".")]
            shield_name = str(view.shield)
            controls_name = str(view.controls)
            if shield_name in order and controls_name in order:
                self.assertLess(
                    order.index(shield_name), order.index(controls_name)
                )
        finally:
            root.destroy()

    def test_the_shield_is_absent_rather_than_visible_when_alpha_fails(self):
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        real_attributes = tk.Toplevel.attributes

        def refuse_alpha(self, *args, **kwargs):
            if args and args[0] == "-alpha":
                raise tk.TclError("alpha unavailable")
            return real_attributes(self, *args, **kwargs)

        try:
            with mock.patch.object(
                placement, "resolve_anchor_rect", return_value=self.TARGET
            ), mock.patch.object(
                placement, "monitor_work_areas", return_value=(self.TARGET,)
            ), mock.patch.object(tk.Toplevel, "attributes", refuse_alpha):
                view = placement.PlacementWindow(root, preview=True)
            root.update_idletasks()
            # No grab layer at all, rather than a near-opaque slab left
            # sitting over the raid.
            self.assertIsNone(view.shield)
            self.assertFalse(view.shield_ready)
            # And the caption cards still drag, exactly as they did
            # before this change.
            self.assertTrue(view.canvas.find_withtag("caption-preview"))
            moved = view.root.geometry()
            placement.PlacementWindow.press_card(
                view, SimpleNamespace(x_root=5, y_root=5)
            )
            placement.PlacementWindow.drag_card(
                view, SimpleNamespace(x_root=45, y_root=35)
            )
            root.update_idletasks()
            self.assertNotEqual(moved, view.root.geometry())
        finally:
            root.destroy()

    def test_the_shield_changes_no_card_pixel(self):
        root, view = self.make_hidden_window()
        try:
            cards = view.canvas.find_withtag("caption-preview")
            self.assertEqual(8, len(cards))
            self.assertEqual(3, len(view.canvas.find_withtag("privacy-tally")))
            stipples = {
                str(view.canvas.itemcget(item, "stipple"))
                for item in cards
                if str(view.canvas.itemcget(item, "stipple"))
            }
            self.assertIn("gray50", stipples)
            view.set_background_opacity("25%")
            stipples = {
                str(view.canvas.itemcget(item, "stipple"))
                for item in view.canvas.find_withtag("caption-preview")
                if str(view.canvas.itemcget(item, "stipple"))
            }
            self.assertIn("gray25", stipples)
            # No solid catcher was drawn over the cards: that would make
            # the Backdrop control lie, showing a slab in Move and a
            # see-through card in game.
            for item in view.canvas.find_withtag("caption-preview"):
                self.assertNotEqual(
                    placement.CHROMA_KEY,
                    str(view.canvas.itemcget(item, "fill")),
                )
        finally:
            root.destroy()

    def test_hidden_move_window_previews_appearance_immediately(self):
        root, view = self.make_hidden_window()
        try:
            self.assertEqual("withdrawn", view.controls.state())
            self.assertTrue(view.canvas.find_withtag("caption-preview"))

            view.font_size_var.set("18")
            view.set_font_size()
            view.set_background_opacity("75%")

            self.assertEqual(18, view.settings.caption_font_size)
            self.assertEqual(75, view.settings.caption_background_opacity)
            self.assertEqual(
                18,
                int(view.fonts["caption"].cget("size")),
            )
            self.assertGreater(view.overlay_width, placement.WINDOW_WIDTH)
        finally:
            root.destroy()

    def test_control_panel_uses_natural_size_at_common_dpi_scales(self):
        # Tk scaling is pixels per point: 1.667, 2.0 and 2.667 correspond
        # approximately to Windows 125%, 150% and 200% display scaling.
        for scaling in (1.667, 2.0, 2.667):
            with self.subTest(scaling=scaling):
                root, view = self.make_hidden_window(scaling=scaling)
                try:
                    try:
                        root.attributes("-alpha", 0.0)
                        view.controls.attributes("-alpha", 0.0)
                    except tk.TclError:
                        pass
                    root.deiconify()
                    root.update()
                    width, height, _x, _y = geometry_parts(
                        view.controls.geometry()
                    )
                    self.assertGreaterEqual(
                        width,
                        view.controls.winfo_reqwidth(),
                    )
                    self.assertGreaterEqual(
                        height,
                        view.controls.winfo_reqheight(),
                    )
                finally:
                    root.withdraw()
                    root.destroy()

    def test_spinbox_arrow_changes_size_without_moving_the_preview(self):
        root, view = self.make_hidden_window()
        try:
            try:
                root.attributes("-alpha", 0.0)
                view.controls.attributes("-alpha", 0.0)
            except tk.TclError:
                pass
            root.deiconify()
            view.controls.deiconify()
            root.update()
            view.nudge = mock.Mock(return_value="break")
            view.font_size_spinbox.focus_force()
            root.update()

            self.assertEqual("10", view.font_size_var.get())
            view.font_size_spinbox.event_generate("<Up>")
            root.update()

            self.assertEqual("11", view.font_size_var.get())
            view.nudge.assert_not_called()
        finally:
            root.withdraw()
            root.destroy()

    def test_control_panel_follows_root_visibility_and_close_protocol(self):
        root, view = self.make_hidden_window()
        destroyed = []
        controls_destroyed = []
        root.bind(
            "<Destroy>",
            lambda event: destroyed.append(event.widget),
            add="+",
        )
        view.controls.bind(
            "<Destroy>",
            lambda event: controls_destroyed.append(event.widget),
            add="+",
        )
        try:
            self.assertTrue(view.controls_owned)
            try:
                root.attributes("-alpha", 0.0)
                view.controls.attributes("-alpha", 0.0)
            except tk.TclError:
                pass
            self.assertEqual("withdrawn", view.controls.state())

            root.deiconify()
            root.update()
            self.assertEqual("normal", view.controls.state())

            root.withdraw()
            root.update()
            self.assertEqual("withdrawn", view.controls.state())

            close_command = view.controls.protocol("WM_DELETE_WINDOW")
            self.assertTrue(close_command)
            view.controls.tk.call(close_command)
            self.assertIn(root, destroyed)
            self.assertIn(view.controls, controls_destroyed)
        finally:
            try:
                root.destroy()
            except tk.TclError:
                pass

    def test_extreme_preview_stays_exact_while_controls_remain_on_1080p(self):
        root, view = self.make_hidden_window()
        try:
            try:
                root.attributes("-alpha", 0.0)
                view.controls.attributes("-alpha", 0.0)
            except tk.TclError:
                pass
            root.deiconify()
            view.controls.deiconify()
            root.update()
            for font_size, rows in ((24, 8), (48, 20)):
                with self.subTest(font_size=font_size, rows=rows):
                    settings = OverlaySettings(
                        caption_font_size=font_size,
                        max_visible_speakers=rows,
                        growth="down",
                    )
                    view.apply_settings(settings, reposition=True)
                    root.update()
                    view.controls.update_idletasks()

                    metrics, height = placement.preview_surface(
                        root,
                        settings,
                        self.TARGET,
                    )
                    root_width, root_height, _root_x, _root_y = geometry_parts(
                        root.geometry()
                    )
                    self.assertEqual(metrics.canvas_width, root_width)
                    self.assertEqual(height, root_height)

                    panel_width, panel_height, panel_x, panel_y = geometry_parts(
                        view.controls.geometry()
                    )
                    left, top, target_width, target_height = self.TARGET
                    self.assertGreaterEqual(panel_x, left)
                    self.assertGreaterEqual(panel_y, top)
                    self.assertLessEqual(panel_x + panel_width, left + target_width)
                    self.assertLessEqual(panel_y + panel_height, top + target_height)
                    tally_bounds = view.canvas.bbox("privacy-tally")
                    self.assertIsNotNone(tally_bounds)
                    self.assertGreaterEqual(tally_bounds[1], 0)
                    self.assertLessEqual(tally_bounds[3], root_height)
        finally:
            root.destroy()


class ControlPanelGeometryTests(unittest.TestCase):
    # A 1080p work area, a default-sized preview and the panel this
    # window really builds. There is no 1080p display on the machine
    # this was written on, so these are the pure rules exercised with a
    # 1080p rectangle, not an observation of one.
    SCREEN = (0, 0, 1920, 1080)
    PREVIEW_SIZE = (540, 190)
    PANEL = (481, 153)

    def latched(self, bounds, preview_rect, panel):
        solved = placement.control_panel_position(bounds, preview_rect, panel)
        return (solved[0] - preview_rect[0], solved[1] - preview_rect[1])

    def test_panel_holds_its_offset_across_the_1080p_flip_band(self):
        """Shino: "moving text desyncs the text from the box on 1080p".

        The shipped rule flips the panel from below the captions to above
        them the instant they cross one line: measured here, 927 to 573
        for a single pixel of caption movement. Held rigid, the panel
        moves exactly as far as the captions did.
        """
        width, height = self.PREVIEW_SIZE
        start = (690, 758, width, height)
        offset = self.latched(self.SCREEN, start, self.PANEL)
        self.assertEqual((29, -159), offset)

        previous = None
        for preview_y in range(600, 900):
            x, y, _clamped = placement.panel_rigid_position(
                self.SCREEN, (690, preview_y), self.PANEL, offset
            )
            self.assertGreaterEqual(x, self.SCREEN[0])
            self.assertGreaterEqual(y, self.SCREEN[1])
            self.assertLessEqual(x + self.PANEL[0], self.SCREEN[0] + self.SCREEN[2])
            self.assertLessEqual(y + self.PANEL[1], self.SCREEN[1] + self.SCREEN[3])
            if previous is not None:
                self.assertLessEqual(abs(y - previous), 1)
            previous = y

        # The rule this replaces, at the same crossing.
        before = placement.control_panel_position(
            self.SCREEN, (690, 731, width, height), self.PANEL
        )
        after = placement.control_panel_position(
            self.SCREEN, (690, 732, width, height), self.PANEL
        )
        self.assertGreater(abs(after[1] - before[1]), 300)

    def test_full_height_preview_never_freezes_the_panel(self):
        """A tall caption stack used to pin the panel, then leap.

        Measured with the shipped rule: panel_y stays at 0 across a
        264 pixel band of caption movement and then jumps 927.
        """
        preview_size = (540, 1026)
        offset = self.latched(
            self.SCREEN, (690, -78) + preview_size, self.PANEL
        )
        seen = []
        for preview_y in range(-40, -140, -1):
            _x, y, _clamped = placement.panel_rigid_position(
                self.SCREEN, (690, preview_y), self.PANEL, offset
            )
            seen.append(y)
        steps = [abs(b - a) for a, b in zip(seen, seen[1:])]
        self.assertTrue(steps)
        self.assertLessEqual(max(steps), 1)
        # It does hold still for part of this sweep, because the panel
        # has reached the top of the screen and must not leave it. That
        # is a clamp, not the old dead band: the shipped rule sat in the
        # same place and then leapt most of the screen height.
        self.assertTrue(any(step == 1 for step in steps))
        shipped = [
            placement.control_panel_position(
                self.SCREEN, (690, preview_y) + preview_size, self.PANEL
            )[1]
            for preview_y in range(-40, -140, -1)
        ]
        shipped_steps = [abs(b - a) for a, b in zip(shipped, shipped[1:])]
        self.assertGreater(max(shipped_steps), 300)

    def test_work_area_for_rect_picks_the_display_under_the_caption(self):
        left = (0, 0, 1920, 1080)
        right = (1920, 0, 1920, 1080)
        areas = (left, right)
        width, height = self.PREVIEW_SIZE

        self.assertEqual(
            right,
            placement.work_area_for_rect(
                areas, (3090, 758, width, height), left
            ),
        )
        # Dragged to the second monitor, the panel goes too.
        offset = (29, -159)
        x, _y, _clamped = placement.panel_rigid_position(
            right, (3090, 758), self.PANEL, offset
        )
        self.assertEqual(3119, x)
        # Where the shipped clamp would have left it: on the first.
        stranded = placement.control_panel_position(
            left, (3090, 758, width, height), self.PANEL
        )
        self.assertEqual(1439, stranded[0])

        # Straddling: the display showing more of the captions wins.
        self.assertEqual(
            left,
            placement.work_area_for_rect(
                areas, (1800, 100, 200, 100), right
            ),
        )
        self.assertEqual(
            right,
            placement.work_area_for_rect(
                areas, (1880, 100, 200, 100), left
            ),
        )
        # No monitor list at all - off win32, or the enumeration failed.
        fallback = (7, 7, 70, 70)
        self.assertIs(
            fallback,
            placement.work_area_for_rect((), (0, 0, 10, 10), fallback),
        )

    def test_panel_never_leaves_the_containing_work_area(self):
        offset = (29, -159)
        cases = (
            (0, 0, 1920, 1080),
            (1920, 0, 1920, 1080),
            (-218, -1440, 2048, 1104),
        )
        for bounds in cases:
            for preview_x in (-4000, bounds[0], bounds[0] + 500, 9000):
                for preview_y in (-4000, bounds[1], bounds[1] + 500, 9000):
                    x, y, clamped = placement.panel_rigid_position(
                        bounds, (preview_x, preview_y), self.PANEL, offset
                    )
                    self.assertGreaterEqual(x, bounds[0])
                    self.assertGreaterEqual(y, bounds[1])
                    self.assertLessEqual(x + self.PANEL[0], bounds[0] + bounds[2])
                    self.assertLessEqual(y + self.PANEL[1], bounds[1] + bounds[3])
                    wanted = (preview_x + offset[0], preview_y + offset[1])
                    self.assertEqual(clamped, (x, y) != wanted)

        # A work area is shorter than its monitor. The panel must settle
        # above the taskbar, not behind it.
        taskbar = (0, 0, 5120, 1392)
        _x, y, _clamped = placement.panel_rigid_position(
            taskbar, (600, 1091), self.PANEL, offset
        )
        self.assertLessEqual(y + self.PANEL[1], 1392)

        # A panel larger than the space pins to the near edge rather
        # than inverting into a negative position.
        tiny = (100, 100, 300, 200)
        x, y, clamped = placement.panel_rigid_position(
            tiny, (0, 0), self.PANEL, offset
        )
        self.assertEqual((100, 100), (x, y))
        self.assertTrue(clamped)

    def tracking_subject(self, **overrides):
        """A subject that drives the real refresh_target."""
        subject = SimpleNamespace(
            root=FakeRoot(5200, 100),
            settings=DEFAULT_SETTINGS,
            overlay_width=540,
            canvas_height=190,
            target_rect=(5120, 0, 1920, 1080),
            target_resolved=False,
            display_bounds=((0, 0, 5120, 1440), (5120, 0, 1920, 1080)),
            drag_origin=None,
            position_dirty=False,
            last_target_check=0.0,
            apply_settings=mock.Mock(),
        )
        for name, value in overrides.items():
            setattr(subject, name, value)
        subject.preview_rect = lambda: placement.PlacementWindow.preview_rect(subject)
        subject.adopt_target = lambda target: (
            placement.PlacementWindow.adopt_target(subject, target)
        )
        return subject

    def refresh(self, subject, *, now):
        with mock.patch.object(placement.time, "monotonic", return_value=now), \
                mock.patch.object(
                    placement, "monitor_display_bounds",
                    return_value=subject.display_bounds,
                ):
            placement.PlacementWindow.refresh_target(subject)

    def test_the_preview_adopts_a_game_rect_that_appears_while_move_is_open(self):
        """Launching the game with Move open used to leave it stale."""
        subject = self.tracking_subject()
        with mock.patch.object(
            placement, "resolve_anchor_rect", return_value=(5120, 0, 1920, 1080)
        ):
            self.refresh(subject, now=100.0)
        self.assertEqual((5120, 0, 1920, 1080), subject.target_rect)
        self.assertTrue(subject.target_resolved)
        subject.apply_settings.assert_called_once()
        self.assertTrue(subject.apply_settings.call_args.kwargs["reposition"])

    def test_a_live_drag_is_never_retargeted_under_the_pointer(self):
        subject = self.tracking_subject(drag_origin=(1, 2, 3, 4))
        with mock.patch.object(
            placement, "resolve_anchor_rect", return_value=(0, 0, 800, 600)
        ):
            self.refresh(subject, now=100.0)
        self.assertEqual((5120, 0, 1920, 1080), subject.target_rect)
        subject.apply_settings.assert_not_called()

    def test_a_dragged_preview_keeps_its_pixel_when_the_target_changes(self):
        for dirty, expected in ((True, False), (False, True)):
            subject = self.tracking_subject(position_dirty=dirty)
            with mock.patch.object(
                placement, "resolve_anchor_rect", return_value=(0, 0, 800, 600)
            ):
                self.refresh(subject, now=100.0)
            subject.apply_settings.assert_called_once()
            self.assertEqual(
                expected, subject.apply_settings.call_args.kwargs["reposition"]
            )

    def test_the_preview_re_resolves_on_the_overlays_own_cadence(self):
        """One cadence, imported - never a second one declared here."""
        subject = self.tracking_subject()
        resolver = mock.Mock(return_value=(0, 0, 800, 600))
        with mock.patch.object(placement, "resolve_anchor_rect", resolver):
            self.refresh(subject, now=100.0)
            self.refresh(
                subject, now=100.0 + placement.TARGET_REFRESH_SECONDS / 2
            )
            self.assertEqual(1, resolver.call_count)
            self.refresh(
                subject, now=100.0 + placement.TARGET_REFRESH_SECONDS + 0.01
            )
            self.assertEqual(2, resolver.call_count)

    def test_preview_on_a_1080p_secondary_matches_the_live_overlay(self):
        """The WYSIWYG contract, on the display the captions land on.

        None of the caps bind at default settings - font 10 with two
        rows is 540x190 on every rect - which is exactly how this
        shipped past a green suite. Font 48 with 20 rows binds both.
        """
        settings = OverlaySettings(caption_font_size=48, max_visible_speakers=20)
        root = FakeRoot(0, 0)
        root.pointer = (6000, 500)
        displays = ((0, 0, 5120, 1440), (5120, 0, 1920, 1080))
        with mock.patch.object(placement, "resolve_anchor_rect", return_value=None):
            target = placement.preview_target(
                root, settings, displays=displays
            ).rect
        self.assertEqual((5120, 0, 1920, 1080), target)
        metrics, height = placement.preview_surface(root, settings, target)
        # What the live overlay computes for that same client rect.
        self.assertEqual(
            metrics_for_target(settings.caption_font_size, target).canvas_width,
            metrics.canvas_width,
        )
        self.assertEqual(
            canvas_height_for_target(
                settings.max_visible_speakers, settings.caption_font_size, target
            ),
            height,
        )
        self.assertEqual(1920, metrics.canvas_width)
        self.assertEqual(1026, height)
        # Against the primary, which is what Move used before, the box
        # was 672 px wider than the panel it claimed to be previewing.
        stale_metrics, stale_height = placement.preview_surface(
            root, settings, (0, 0, 5120, 1440)
        )
        self.assertEqual(2592, stale_metrics.canvas_width)
        self.assertEqual(1368, stale_height)

    def test_the_font_list_drops_the_vertical_writing_duplicates(self):
        """Shino, 2026-09-08: "the font box stretches extremely high".

        Windows reports an "@"-prefixed copy of every CJK face - the
        same font rotated for vertical writing. On his machine that was
        28 of 243 entries, and none of them is what somebody picking a
        caption font wants.
        """
        raw = ("Segoe UI", "@MS Gothic", "MS Gothic", "Arial", "@Malgun Gothic")
        self.assertEqual(
            ("Arial", "MS Gothic", "Segoe UI"),
            placement.caption_font_choices(raw, "Segoe UI"),
        )
        # Sorted without case deciding it, and de-duplicated.
        self.assertEqual(
            ("arial", "Arial B", "Zapf"),
            placement.caption_font_choices(
                ("Zapf", "arial", "Arial B", "Zapf"), "Zapf"
            ),
        )
        # The member's own family survives even when it is a vertical
        # copy or is no longer installed, so opening Move can never
        # silently change the font they chose.
        self.assertIn(
            "@MS Gothic",
            placement.caption_font_choices(raw, "@MS Gothic"),
        )
        self.assertIn(
            "A Font They Uninstalled",
            placement.caption_font_choices(raw, "A Font They Uninstalled"),
        )
        # Junk in the list from Tk does not become an entry.
        self.assertEqual(
            ("Arial",),
            placement.caption_font_choices(("Arial", "", None, 7), "Arial"),
        )

    def test_the_font_menu_never_asks_to_be_taller_than_the_screen(self):
        """A Tk menu is one column and does not scroll.

        Measured before this change on a 1392 px work area: 243
        families in a single column asked for 4619 px, so most of the
        list could not be reached at all.
        """
        # 215 real families at 21 px, on the work area of his primary.
        per_column = placement.menu_column_size(215, 21, 1392)
        self.assertEqual(66, per_column)
        columns = -(-215 // per_column)
        self.assertEqual(4, columns)
        self.assertLessEqual(per_column * 21, 1392)

        # A 1080p work area breaks sooner, and still fits.
        per_column = placement.menu_column_size(215, 21, 1040)
        self.assertLessEqual(per_column * 21, 1040)
        self.assertGreater(per_column, 1)

        # Degenerate inputs cannot produce a zero or negative column,
        # which would make range(start, stop, step) raise.
        for entries, row, height in (
            (0, 21, 1392),
            (10, 0, 1392),
            (10, 21, 0),
            (10, 21, -5),
            (10, 5000, 1392),
        ):
            self.assertGreaterEqual(
                placement.menu_column_size(entries, row, height), 1
            )
        # Never more entries per column than there are entries.
        self.assertEqual(3, placement.menu_column_size(3, 21, 99999))

    def test_panel_drag_surface_accepts_only_inert_chrome(self):
        self.assertEqual(("Frame", "Label"), placement.DRAG_SURFACE_CLASSES)
        for widget_class in ("Frame", "Label"):
            self.assertTrue(placement.panel_drag_surface(widget_class))
        for widget_class in (
            "Button",
            "Spinbox",
            "Menubutton",
            "Menu",
            "Toplevel",
            "Entry",
            "TCombobox",
            "Canvas",
        ):
            self.assertFalse(placement.panel_drag_surface(widget_class))

    def test_panel_is_clamped_inside_positive_and_negative_monitor_origins(self):
        panel = (841, 239)
        for target, preview in (
            ((2560, 0, 1920, 1040), (2560, -72, 1920, 988)),
            ((-1920, -160, 1920, 1040), (-1920, -232, 1920, 988)),
        ):
            with self.subTest(target=target):
                x, y = placement.control_panel_position(target, preview, panel)
                left, top, width, height = target
                self.assertGreaterEqual(x, left)
                self.assertGreaterEqual(y, top)
                self.assertLessEqual(x + panel[0], left + width)
                self.assertLessEqual(y + panel[1], top + height)


class MuteRenderTests(unittest.TestCase):
    """Muted hides captions but never hides an active privacy tally."""

    def overlay_with(self, *, muted, session_state="active"):
        from VoiceCaptions.events import CaptionBoard
        canvas = FakeCanvas()
        visibility = {"hidden": 0, "shown": 0}
        view = SimpleNamespace(
            settings=OverlaySettings(muted=muted),
            board=CaptionBoard(max_rows=2, final_hold_seconds=3.6),
            canvas=canvas,
            target_rect=(0, 0, 1920, 1080),
            last_signature=None,
            session_state=session_state,
            session_message="",
            hide=lambda: visibility.__setitem__(
                "hidden", visibility["hidden"] + 1
            ),
            show=lambda: visibility.__setitem__("shown", visibility["shown"] + 1),
            metrics=DEFAULT_METRICS,
            canvas_height=overlay.CANVAS_HEIGHT,
            font_size=BASE_FONT_SIZE,
            root=FakeRoot(),
            fonts={
                "speaker": SimpleNamespace(measure=lambda s: len(s) * 7),
                "caption": SimpleNamespace(measure=lambda s: len(s) * 7),
                "previous": SimpleNamespace(measure=lambda s: len(s) * 7),
                "initial": SimpleNamespace(measure=lambda s: len(s) * 7),
                "status": SimpleNamespace(measure=lambda s: len(s) * 7),
            },
        )
        view.recording_tally_size = lambda: CaptionOverlay.recording_tally_size(view)
        view.draw_recording_tally = lambda **kwargs: (
            CaptionOverlay.draw_recording_tally(view, **kwargs)
        )
        return view, canvas, visibility

    def caption(self, speaker, text):
        return CaptionEvent(
            speaker_id=speaker, speaker=speaker, character=speaker,
            role="DAMAGE", text=text, final=True, sequence=1, timestamp=0.0,
        )

    def test_muted_hides_caption_text_but_keeps_active_rec(self):
        view, canvas, visibility = self.overlay_with(muted=True)
        view.board.ingest(self.caption("mira", "Interrupt now"), now=0.0)
        CaptionOverlay.render(view, 0.1)
        self.assertNotIn("Interrupt now", canvas.texts)
        self.assertEqual(["REC"], canvas.texts)
        self.assertEqual(0, visibility["hidden"])
        self.assertEqual(1, visibility["shown"])
        self.assertEqual(overlay.DANGER, canvas.oval_calls[0]["fill"])

    def test_muted_unknown_session_keeps_rec_question_visible(self):
        view, canvas, visibility = self.overlay_with(
            muted=True,
            session_state="unknown",
        )
        CaptionOverlay.render(view, 0.1)
        self.assertEqual(["REC?"], canvas.texts)
        self.assertEqual(0, visibility["hidden"])
        self.assertEqual(1, visibility["shown"])

    def test_muted_active_to_paused_removes_tally(self):
        view, canvas, visibility = self.overlay_with(muted=True)
        CaptionOverlay.render(view, 0.1)
        self.assertEqual(["REC"], canvas.texts)
        view.session_state = "paused"
        CaptionOverlay.render(view, 0.2)
        self.assertEqual([], canvas.texts)
        self.assertEqual(1, visibility["hidden"])

        view.session_state = "active"
        CaptionOverlay.render(view, 0.3)
        self.assertEqual(["REC"], canvas.texts)
        self.assertEqual(2, visibility["shown"])

    def test_muted_inactive_session_hides_everything(self):
        view, canvas, visibility = self.overlay_with(
            muted=True,
            session_state="ready",
        )
        CaptionOverlay.render(view, 0.1)
        self.assertEqual([], canvas.texts)
        self.assertEqual(1, visibility["hidden"])
        self.assertEqual(0, visibility["shown"])

    def test_unmuted_active_silence_draws_standalone_rec(self):
        view, canvas, visibility = self.overlay_with(muted=False)
        CaptionOverlay.render(view, 0.1)
        self.assertEqual(["REC"], canvas.texts)
        self.assertEqual(1, visibility["shown"])

    def test_an_unmuted_overlay_does_not_take_the_mute_shortcut(self):
        """Unmuted, render falls through to the real drawing path. The
        full path needs a Tk-shaped overlay the rest of this suite
        already exercises; here it is enough that it does NOT hide via the
        muted branch - it fails reaching for canvas_height instead, which
        only the real path touches."""
        view, _canvas, visibility = self.overlay_with(muted=False)
        view.board.ingest(self.caption("mira", "Interrupt now"), now=0.0)
        with self.assertRaises(AttributeError):
            CaptionOverlay.render(view, 0.1)
        self.assertEqual(0, visibility["hidden"], "unmuted overlay hid via mute")

    def test_muting_does_not_leave_a_backlog_for_unmute(self):
        """The board is aged while muted, so on unmute only live captions
        show - not everything that arrived during the mute."""
        view, canvas, _visibility = self.overlay_with(muted=True)
        view.board.ingest(self.caption("old", "stale callout"), now=0.0)
        # Time passes while muted; render ages the board past the hold.
        for now in (0.1, 4.0, 8.0):
            CaptionOverlay.render(view, now)
        self.assertNotIn("stale callout", canvas.texts)
        self.assertEqual([], view.board.visible(8.0),
                         "a caption survived the whole mute as a backlog")

    def test_overlay_fits_the_active_wow_client_not_the_monitor(self):
        subject = SimpleNamespace(
            root=SimpleNamespace(
                winfo_screenwidth=lambda: 2560,
                winfo_screenheight=lambda: 1440,
            ),
            target_rect=(100, 100, 900, 500),
        )
        metrics = CaptionOverlay._metrics_for(subject, MAX_CAPTION_FONT_SIZE)
        height = CaptionOverlay._canvas_height_for(
            subject,
            MAX_VISIBLE_SPEAKERS,
            MAX_CAPTION_FONT_SIZE,
        )
        self.assertLessEqual(metrics.canvas_width, 900)
        self.assertLessEqual(height, 500)

    def test_placement_controls_are_real_named_keyboard_controls(self):
        source = Path(placement.__file__).read_text(encoding="utf-8")
        for label in ("Save position", "Use safe default", "Done"):
            self.assertIn('"' + label + '"', source)
        self.assertIn("takefocus=True", source)
        self.assertIn('root.bind("<Left>"', source)
        self.assertIn('root.bind("<Shift-Left>"', source)
        self.assertNotIn('self.draw_button(self.save_bounds, "SAVE")', source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
