import queue
import re
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
        self.geometries: list[str] = []
        self.scheduled: list[tuple[int, object]] = []

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

    def test_active_and_previous_rows_are_even_smaller(self):
        height = DEFAULT_METRICS.stack_height(
            [line("old", final=True), line("live", final=False)]
        )
        self.assertEqual(78, height)

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
                    expected_name = (
                        CLASS_COLORS["rogue"]
                        if wow_class
                        else (overlay.MUTED if final else ROLE_COLORS["HEALER"])
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
            (False, "whisper", "caption_whisper"),
            (False, "shout", "caption_shout"),
            (True, "whisper", "previous_whisper"),
            (True, "shout", "previous_shout"),
            (False, "normal", "caption"),
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
        self.assertEqual(row.event.name_color, name["fill"])
        self.assertEqual(row.event.color, body["outline"])


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
            ).live_height,
            width=width,
            speaker="Mira",
            text="Move out of the dangerous ground effect immediately",
            color="#4fd182",
            live=True,
        )

        caption = next(
            call
            for call in canvas.text_calls
            if call.get("font") is subject.fonts["caption"]
        )
        tally_width, _height = subject.recording_tally_size()
        tally_left = x2 - 6 - tally_width
        caption_right = (
            caption["coords"][0]
            + subject.fonts["caption"].measure(caption["text"])
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
        self.assertEqual("caption_whisper", caption_font_key(True, "whisper"))
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
            footer_top=canvas_height + placement.FOOTER_GAP,
            window_height=(
                canvas_height
                + placement.FOOTER_GAP
                + placement.FOOTER_HEIGHT
            ),
            target_rect=target_rect,
            canvas=mock.Mock(),
            root=FakeRoot(x, y),
            draw=mock.Mock(),
            drag_origin=None,
            flash=lambda _value: None,
        )
        subject.update_control_bounds = lambda: (
            placement.PlacementWindow.update_control_bounds(subject)
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
    def test_hidden_move_window_previews_appearance_immediately(self):
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        try:
            with mock.patch.object(
                placement,
                "resolve_anchor_rect",
                return_value=(0, 0, 1920, 1040),
            ):
                view = placement.PlacementWindow(root, preview=True)
            root.withdraw()
            root.update_idletasks()

            footer = view.footer_shell.winfo_children()[0]
            self.assertLessEqual(
                footer.winfo_reqheight() + 2,
                placement.FOOTER_HEIGHT,
            )
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
