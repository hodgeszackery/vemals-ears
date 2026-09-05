"""Status-card geometry without a Tk window or a live caption connection."""

import unittest
from types import SimpleNamespace

from VoiceCaptions.layout import BASE_FONT_SIZE, DEFAULT_METRICS
from VoiceCaptions.overlay import CaptionOverlay, DANGER, SLATE, TEXT
from VoiceCaptions.settings import OverlaySettings


PAUSE_MESSAGE = (
    "Captions paused - you left the voice channel or have not linked your "
    "Vemal's Ears application."
)


class StatusFont:
    def __init__(self, size):
        self.char_width = max(1, round(7 * size / BASE_FONT_SIZE))
        self.line_height = max(1, round(13 * size / BASE_FONT_SIZE))

    def measure(self, text):
        return len(text) * self.char_width

    def metrics(self, name):
        assert name == "linespace"
        return self.line_height


class StatusCanvas:
    def __init__(self):
        self.cards = []
        self.texts = []
        self.dots = []

    def create_polygon(self, points, **options):
        self.cards.append((points, options))
        return 1

    def create_oval(self, *coords, **options):
        self.dots.append(coords)
        return 2

    def create_text(self, x, y, **options):
        self.texts.append((x, y, options))
        return len(self.texts) + 2

    def tag_lower(self, *_args):
        pass


def draw_status(*, size=10, width=540, height=None, growth="up", message=PAUSE_MESSAGE,
                state="paused"):
    metrics = DEFAULT_METRICS.for_font_size(size).fitted_to_width(width)
    subject = SimpleNamespace(
        canvas=StatusCanvas(), metrics=metrics,
        canvas_height=metrics.tallest_stack(2) if height is None else height,
        fonts={"status": StatusFont(size)}, font_size=size,
        settings=OverlaySettings(growth=growth),
        session_state=state, session_message=message,
    )
    CaptionOverlay.draw_status(subject)
    return subject


class StatusLayoutTests(unittest.TestCase):
    def foreground(self, subject):
        return [entry for entry in subject.canvas.texts if entry[2]["fill"] == TEXT]

    def bounds(self, subject):
        points = subject.canvas.cards[0][0]
        return min(points[::2]), min(points[1::2]), max(points[::2]), max(points[1::2])

    def assert_content_inside(self, subject):
        x1, y1, x2, y2 = self.bounds(subject)
        self.assertGreaterEqual(x1, 0)
        self.assertLessEqual(x2, subject.metrics.canvas_width)
        self.assertGreaterEqual(y1, 0)
        self.assertLessEqual(y2, subject.canvas_height)
        font = subject.fonts["status"]
        for x, y, options in self.foreground(subject):
            self.assertGreaterEqual(x, x1)
            self.assertLessEqual(x + font.measure(options["text"]), x2)
            self.assertGreaterEqual(y, y1)
            self.assertLessEqual(y + font.line_height, y2)
        for left, top, right, bottom in subject.canvas.dots:
            self.assertTrue(x1 <= left < right <= x2)
            self.assertTrue(y1 <= top < bottom <= y2)

    def test_full_rejoin_instruction_wraps_at_default_and_large_font_sizes(self):
        for size, width in ((10, 540), (20, 900), (36, 1440), (10, 220)):
            with self.subTest(size=size, width=width):
                subject = draw_status(size=size, width=width)
                labels = [options["text"] for _x, _y, options in self.foreground(subject)]
                self.assertEqual(PAUSE_MESSAGE, " ".join(labels))
                self.assertGreater(len(labels), 1)
                self.assert_content_inside(subject)

    def test_status_uses_the_selected_growth_edge(self):
        for growth in ("up", "down"):
            with self.subTest(growth=growth):
                subject = draw_status(growth=growth)
                _x1, y1, _x2, y2 = self.bounds(subject)
                padding = subject.metrics.vertical_padding
                self.assertEqual(padding if growth == "down" else subject.canvas_height - padding,
                                 y1 if growth == "down" else y2)
                self.assert_content_inside(subject)

    def test_status_card_and_content_stay_within_a_small_canvas(self):
        subject = draw_status(width=220, height=60)
        self.assert_content_inside(subject)

    def test_short_error_stays_compact_dark_and_uses_the_error_outline(self):
        subject = draw_status(message="Captions unavailable", state="error")
        x1, y1, x2, y2 = self.bounds(subject)
        self.assertLess(x2 - x1, 350)
        self.assertLessEqual(y2 - y1, 40)
        self.assertEqual(SLATE, subject.canvas.cards[0][1]["fill"])
        self.assertEqual(DANGER, subject.canvas.cards[0][1]["outline"])
        self.assertEqual(["Captions unavailable"],
                         [options["text"] for _x, _y, options in self.foreground(subject)])
        self.assert_content_inside(subject)


if __name__ == "__main__":
    unittest.main()
