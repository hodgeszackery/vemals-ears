"""The overlay draw honors the growth direction.

Unbound-method tests, the same idiom the companion window uses: the draw
math is pure geometry over self attributes, so a SimpleNamespace stands
in for the Tk overlay and the cursor positions handed to draw_line are
the observable.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from VoiceCaptions.layout import DEFAULT_METRICS
from VoiceCaptions.overlay import CaptionOverlay
from VoiceCaptions.settings import OverlaySettings


class DrawGrowthTests(unittest.TestCase):
    def drawn_rows(self, growth: str, cards: int = 3) -> list[int]:
        positions: list[int] = []
        fake = SimpleNamespace(
            metrics=DEFAULT_METRICS,
            canvas_height=400,
            settings=OverlaySettings(growth=growth),
            recording_state="",
            adapter_recording="",
            measure_card=lambda line, recording=False: {"height": 30},
            draw_line=lambda line, y1, height, card=None: positions.append(y1),
        )
        CaptionOverlay.draw_lines(fake, ["line-%d" % index for index in range(cards)])
        return positions

    def test_a_standing_feed_is_bottom_anchored(self):
        positions = self.drawn_rows("up")
        stack = DEFAULT_METRICS.stacked([30, 30, 30])
        self.assertEqual(
            400 - stack + DEFAULT_METRICS.vertical_padding, positions[0]
        )

    def test_a_hanging_feed_starts_at_the_top_of_the_canvas(self):
        """What lets captions sit as high as the screen allows: the first
        card at the top edge, cross-talk extending downward."""
        positions = self.drawn_rows("down")
        self.assertEqual(DEFAULT_METRICS.vertical_padding, positions[0])
        self.assertLess(positions[0], positions[1])

    def test_row_spacing_is_identical_in_both_modes(self):
        up = self.drawn_rows("up")
        down = self.drawn_rows("down")
        up_gaps = [b - a for a, b in zip(up, up[1:])]
        down_gaps = [b - a for a, b in zip(down, down[1:])]
        self.assertEqual(up_gaps, down_gaps)

    def test_the_move_preview_mirrors_both_modes(self):
        """Source pin, honestly labelled: the preview is a Tk window with
        fonts and a canvas, too heavy to fake here, and a preview that
        kept bottom-anchoring while the overlay hung would show the
        member a lie at the exact moment they are choosing a position."""
        from pathlib import Path as _Path
        import VoiceCaptions.placement as placement_module
        source = _Path(placement_module.__file__).read_text(encoding="utf-8")
        self.assertIn('self.settings.growth == "down"', source)
        self.assertIn("cursor_y = metrics.vertical_padding", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
