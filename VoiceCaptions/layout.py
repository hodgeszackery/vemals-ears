"""Pure layout helpers for the compact raid caption overlay."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Iterable

from .events import DisplayLine

# How many lines one caption may occupy before it is finally clipped.
#
# A raid instruction is commonly 100-150 characters and one line of this
# card holds about 70, so a single line deleted the half that carried the
# instruction - and the tail is where the instruction lives ("...stack on
# me", "...do not dispel it"). The reader has no way to ask what was cut,
# because for them the caption IS the sentence.
MAX_TEXT_LINES = 4

# The caption font size the default metric heights were tuned for. Other
# sizes scale every vertical dimension from this, so a bigger font is a
# bigger overlay rather than text overflowing its row.
BASE_FONT_SIZE = 10

BACKGROUND_STIPPLES = {
    25: "gray25",
    50: "gray50",
    75: "gray75",
    100: "",
}


@dataclass(frozen=True)
class OverlayMetrics:
    canvas_width: int = 540
    max_card_width: int = 520
    min_card_width: int = 250
    live_height: int = 38
    final_height: int = 28
    # Added per extra wrapped line of caption text.
    text_line_height: int = 17
    row_gap: int = 4
    vertical_padding: int = 4
    bottom_safe_ratio: float = 0.12
    minimum_bottom_safe: int = 112

    def row_height(self, line: DisplayLine, text_lines: int = 1) -> int:
        # One card style. A live line used to stand taller than a settled
        # one, so two speakers on screen were two different shapes - Shino,
        # 2026-09-06: "two different style text boxes are showing at the
        # same time", and "the UI is better on the non spliced text". The
        # settled shape is now the only shape. live_height still sizes the
        # canvas for its worst case, so nothing moves on screen.
        del line
        return self.final_height + max(0, text_lines - 1) * self.text_line_height

    def stack_height(self, lines: Iterable[DisplayLine]) -> int:
        values = list(lines)
        if not values:
            return 0
        return (
            sum(self.row_height(line) for line in values)
            + self.row_gap * (len(values) - 1)
            + self.vertical_padding * 2
        )

    def stacked(self, heights: Iterable[int]) -> int:
        """Total height for rows whose individual heights are known.

        Wrapping makes a row's height depend on font measurement, which
        this module deliberately cannot do, so the renderer measures and
        hands the numbers back.
        """
        values = list(heights)
        if not values:
            return 0
        return (
            sum(values)
            + self.row_gap * (len(values) - 1)
            + self.vertical_padding * 2
        )

    def for_font_size(self, size: int) -> "OverlayMetrics":
        """A copy scaled to a caption font size other than the base 10pt.

        Only the vertical dimensions and the card width scale: heights
        must track the font's line height or text overflows its row, and
        the card widens so a larger font is not wrapped to shreds inside a
        fixed box. Gaps and padding scale too so the whole thing stays in
        proportion. Nothing here needs a live font, so placement and the
        overlay agree on the geometry without measuring.
        """
        factor = size / BASE_FONT_SIZE
        if factor == 1.0:
            return self

        def scale(value: int) -> int:
            return max(1, round(value * factor))

        return OverlayMetrics(
            canvas_width=scale(self.canvas_width),
            max_card_width=scale(self.max_card_width),
            min_card_width=scale(self.min_card_width),
            live_height=scale(self.live_height),
            final_height=scale(self.final_height),
            text_line_height=scale(self.text_line_height),
            row_gap=scale(self.row_gap),
            vertical_padding=scale(self.vertical_padding),
            bottom_safe_ratio=self.bottom_safe_ratio,
            minimum_bottom_safe=self.minimum_bottom_safe,
        )

    def fitted_to_width(self, available: int) -> "OverlayMetrics":
        """Cap the card widths at what the display can actually show.

        Width scales with the font, so a large size wants a card wider than
        a 1920-wide screen - and the window, anchored at the left edge with
        its cards centered in a canvas wider than the monitor, would push
        them off the right side. Capping the width rather than shrinking
        the font keeps the size the member asked for and lets the text wrap
        instead, which is the failure they can read.
        """
        if available <= 0 or self.canvas_width <= available:
            return self
        canvas_width = available
        # Keep the card's inset inside the canvas proportional to the base
        # layout, so a capped overlay still has a margin rather than cards
        # butting against the screen edge.
        inset = max(1, round(canvas_width * (1 - self.max_card_width / self.canvas_width)))
        max_card_width = max(1, canvas_width - inset)
        return replace(
            self,
            canvas_width=canvas_width,
            max_card_width=max_card_width,
            min_card_width=min(self.min_card_width, max_card_width),
        )

    def tallest_stack(self, rows: int, text_lines: int = MAX_TEXT_LINES) -> int:
        """The canvas has to be tall enough for the worst case.

        The window cannot resize per frame without the overlay jumping
        around mid-pull, so it is sized once for the tallest stack and the
        cards are drawn bottom-anchored. Unused space above them is fully
        transparent, so an empty overlay looks exactly as it did before.
        """
        extra = max(0, text_lines - 1) * self.text_line_height
        # Every visible slot can contain a live speaker at the same time.
        # Treating all but one as the shorter final style clipped the top of
        # wrapped cross-talk, especially once more than two rows were allowed.
        tall = [self.live_height + extra] * max(0, rows)
        return self.stacked(tall)


DEFAULT_METRICS = OverlayMetrics()


def metrics_for_target(
    font_size: int,
    target_rect: tuple[int, int, int, int] | None,
    *,
    fallback_width: int | None = None,
) -> OverlayMetrics:
    """Build the one width model used by both preview and live overlay."""
    metrics = DEFAULT_METRICS.for_font_size(font_size)
    available = (
        target_rect[2]
        if target_rect is not None and target_rect[2] > 0
        else fallback_width
    )
    if available is None:
        return metrics
    return metrics.fitted_to_width(int(available))


def canvas_height_for_target(
    rows: int,
    font_size: int,
    target_rect: tuple[int, int, int, int] | None,
    *,
    fallback_height: int | None = None,
) -> int:
    """Cap the caption surface against the selected target's work area.

    Win32 can report a different work area than Tk's primary-screen query,
    especially on a 1080p secondary monitor or with per-monitor DPI scaling.
    Passing the resolved target keeps the editable preview and live overlay on
    the same dimensions.
    """
    ideal = DEFAULT_METRICS.for_font_size(font_size).tallest_stack(rows)
    available = (
        target_rect[3]
        if target_rect is not None and target_rect[3] > 0
        else fallback_height
    )
    if available is None or available <= 0:
        return ideal
    return max(1, min(ideal, int(int(available) * 0.95)))


def caption_font_specs(size: int, family: str) -> dict[str, dict[str, object]]:
    """Return shared Tk font specifications without requiring a Tk root."""
    factor = size / BASE_FONT_SIZE

    def px(base: int) -> int:
        return max(1, round(base * factor))

    return {
        "speaker": {"family": family, "size": px(9), "weight": "bold"},
        "caption": {"family": family, "size": px(10)},
        "caption_whisper": {
            "family": family,
            "size": px(10),
            "slant": "italic",
        },
        "caption_shout": {
            "family": family,
            "size": px(10),
            "weight": "bold",
        },
        "previous": {"family": family, "size": px(9)},
        "previous_whisper": {
            "family": family,
            "size": px(9),
            "slant": "italic",
        },
        "previous_shout": {
            "family": family,
            "size": px(9),
            "weight": "bold",
        },
        "initial": {"family": family, "size": px(8), "weight": "bold"},
        "status": {"family": family, "size": px(8), "weight": "bold"},
    }


def caption_font_key(live: bool, speech_style: str) -> str:
    """Return the font face for one caption without changing its text.

    The same face whether the line is live or settled: the larger live
    face was the other half of the two-shapes problem row_height names.
    """
    del live
    base = "previous"
    if speech_style in {"whisper", "shout"}:
        return base + "_" + speech_style
    return base


def background_stipple(opacity: int) -> str:
    """Map the supported backdrop opacity to Tk's stable bitmap fills."""
    return BACKGROUND_STIPPLES.get(opacity, BACKGROUND_STIPPLES[50])


def wrap_text(
    text: str,
    width: int,
    measure: Callable[[str], int],
    max_lines: int = MAX_TEXT_LINES,
) -> list[str]:
    """Break a caption across lines instead of deleting its tail.

    Greedy word wrapping. The final line is clipped only when the text
    genuinely does not fit in max_lines, so an ellipsis means "there was
    more" rather than "one line was all you were getting".
    """
    if width <= 0 or max_lines <= 0:
        return []
    words = text.split()
    if not words:
        return [""]

    lines: list[str] = []
    current = ""
    remaining = list(words)
    while remaining and len(lines) < max_lines:
        word = remaining[0]
        candidate = word if not current else current + " " + word
        if measure(candidate) <= width:
            current = candidate
            remaining.pop(0)
            continue
        if not current:
            # A single word wider than the whole card. Clip it and move
            # on, or the loop never advances.
            lines.append(clipped_text(word, width, measure))
            remaining.pop(0)
            continue
        lines.append(current)
        current = ""
    if current and len(lines) < max_lines:
        lines.append(current)
        current = ""

    leftover = " ".join(([current] if current else []) + remaining).strip()
    if leftover:
        # More than the card can hold. Put the ellipsis on the last line
        # so the reader knows something followed.
        tail = (lines[-1] + " " + leftover) if lines else leftover
        clipped = clipped_text(tail, width, measure)
        if lines:
            lines[-1] = clipped
        else:
            lines.append(clipped)
    return lines


def clipped_text(text: str, width: int, measure: Callable[[str], int]) -> str:
    """Fit one caption line, clipping with an ellipsis when it overflows."""
    if width <= 0:
        return ""
    if measure(text) <= width:
        return text
    suffix = "..."
    if measure(suffix) > width:
        return ""
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if measure(text[:middle].rstrip() + suffix) <= width:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip() + suffix


def safe_overlay_position(
    client_rect: tuple[int, int, int, int],
    overlay_size: tuple[int, int],
    metrics: OverlayMetrics = DEFAULT_METRICS,
) -> tuple[int, int]:
    """Lower-center placement above action bars, clamped to the client."""
    left, top, width, height = client_rect
    overlay_width, overlay_height = overlay_size
    x = left + max((width - overlay_width) // 2, 0)
    bottom_safe = max(metrics.minimum_bottom_safe, int(height * metrics.bottom_safe_ratio))
    y = top + height - bottom_safe - overlay_height
    x = max(left, min(x, left + max(width - overlay_width, 0)))
    y = max(top, min(y, top + max(height - overlay_height, 0)))
    return x, y
