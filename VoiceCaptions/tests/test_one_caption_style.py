"""One card style, and a live line that shows only what is new.

Shino, 2026-09-06, with a screenshot of two speakers on screen: "two
different style text boxes are showing at the same time", "the spliced
text keeps appending rather than just showing the new streamed text",
and "the UI is better on the non spliced text".

The live card used to be a different shape from the settled one: a
larger face, a larger avatar, a coloured outline, a trailing dot, a
taller row. And the live line carried every streamed part of the
utterance joined together, so a long sentence grew on screen five
seconds at a time. The settled shape is now the only shape, and the
live line carries only the part that just arrived; the whole utterance
still lands once, as the final.
"""

from __future__ import annotations

import unittest

from VoiceCaptions import overlay
from VoiceCaptions.coordinator import (
    _FINAL_SLOT,
    _SEQUENCE_SLOTS,
    VoiceCaptionCoordinator,
)
from VoiceCaptions.events import CaptionEvent
from VoiceCaptions.overlay import CaptionOverlay
from VoiceCaptions.tests.test_coordinator import (
    ConcurrentFakeSTT,
    EventLines,
    FakeSink,
    chunk,
    ended,
    speaker,
    started,
)
from VoiceCaptions.tests.test_layout import FakeCanvas, drawing_subject, line


class OneCardStyleTests(unittest.TestCase):
    def drawn(self, final: bool):
        canvas = FakeCanvas()
        subject = drawing_subject(canvas)
        row = line("MIRA", final=final)
        card = CaptionOverlay.measure_card(subject, row)
        CaptionOverlay.draw_line(subject, row, 0, card["height"], card)
        return subject, canvas, card

    def test_a_live_card_and_a_settled_card_are_the_same_shape(self):
        live_subject, live_canvas, live_card = self.drawn(final=False)
        settled_subject, settled_canvas, settled_card = self.drawn(final=True)
        self.assertEqual(settled_card["height"], live_card["height"])
        self.assertEqual(settled_card["width"], live_card["width"])
        # Each subject owns its own fonts; the face is the same one by name.
        self.assertIs(live_subject.fonts["previous"], live_card["caption_font"])
        self.assertIs(settled_subject.fonts["previous"], settled_card["caption_font"])
        # Same body outline, same avatar geometry, same name treatment.
        self.assertEqual(
            settled_canvas.polygon_calls[0]["outline"],
            live_canvas.polygon_calls[0]["outline"],
        )
        self.assertEqual(overlay.SLATE, live_canvas.polygon_calls[0]["outline"])
        live_ovals = [call for call in live_canvas.oval_calls]
        settled_ovals = [call for call in settled_canvas.oval_calls]
        self.assertEqual(len(settled_ovals), len(live_ovals), "no trailing live dot")
        self.assertEqual(
            [oval["args"] for oval in settled_ovals],
            [oval["args"] for oval in live_ovals],
            "the avatar is one size",
        )
        live_name = next(c for c in live_canvas.text_calls if c.get("text") == "MIRA")
        settled_name = next(
            c for c in settled_canvas.text_calls if c.get("text") == "MIRA"
        )
        self.assertEqual(settled_name["fill"], live_name["fill"])

    def test_the_role_accent_bar_survives_on_both(self):
        """The colour a reader filters on was never the outline."""

        class BarCanvas(FakeCanvas):
            def __init__(self):
                super().__init__()
                self.rectangle_calls = []

            def create_rectangle(self, *args, **kwargs):
                self.rectangle_calls.append({"args": args, **kwargs})
                return 0

        for final in (False, True):
            with self.subTest(final=final):
                canvas = BarCanvas()
                subject = drawing_subject(canvas)
                row = line("MIRA", final=final)
                card = CaptionOverlay.measure_card(subject, row)
                CaptionOverlay.draw_line(subject, row, 0, card["height"], card)
                bars = [
                    call for call in canvas.rectangle_calls
                    if call.get("fill") == row.event.color
                ]
                self.assertEqual(1, len(bars), "one accent bar in the role colour")


class LiveLineShowsOnlyWhatIsNewTests(unittest.IsolatedAsyncioTestCase):
    async def utterance(self, chunks: int, *, flush_bytes: int):
        lines = EventLines()
        sink = FakeSink()
        instance = VoiceCaptionCoordinator(
            ConcurrentFakeSTT(), sink, stream_flush_pcm_bytes=flush_bytes
        )
        identity = speaker("111111111111111111", "Mira")
        pcm = bytes(3_200)
        await instance.feed_line(
            lines.line("audio.segment.started", started("one", 1, identity))
        )
        for index in range(1, chunks + 1):
            await instance.feed_line(
                lines.line("audio.segment.chunk", chunk("one", 1, index, pcm))
            )
        await instance.feed_line(
            lines.line(
                "audio.segment.ended",
                ended("one", 1, identity, chunks, len(pcm) * chunks),
            )
        )
        await instance.drain()
        captions = [e for e in sink.events if isinstance(e, CaptionEvent)]
        await instance.shutdown()
        return captions

    async def test_each_partial_carries_only_the_part_that_just_arrived(self):
        # 12 chunks of 3,200 bytes with a 16,000-byte flush: two cuts and a
        # final tail, so two partials then one final.
        captions = await self.utterance(12, flush_bytes=16_000)
        partials = [e for e in captions if not e.final]
        finals = [e for e in captions if e.final]
        self.assertEqual(1, len(finals))
        self.assertGreaterEqual(len(partials), 2)
        # The fake STT names each part after its slot, so a partial that
        # joined earlier parts would carry more than one name.
        for event in partials:
            self.assertEqual(
                1, len(event.text.split()),
                "a partial must show only the new part: %r" % event.text,
            )
        self.assertEqual(
            [f"caption-{1 * _SEQUENCE_SLOTS + index}" for index in range(1, len(partials) + 1)],
            [event.text for event in partials],
        )

    async def test_the_final_still_carries_the_whole_utterance(self):
        captions = await self.utterance(12, flush_bytes=16_000)
        partials = [e for e in captions if not e.final]
        final = next(e for e in captions if e.final)
        expected = [event.text for event in partials] + [
            f"caption-{1 * _SEQUENCE_SLOTS + _FINAL_SLOT}"
        ]
        self.assertEqual(" ".join(expected), final.text)

    async def test_a_short_utterance_is_one_final_and_nothing_else(self):
        captions = await self.utterance(2, flush_bytes=0)
        self.assertEqual([True], [e.final for e in captions])


if __name__ == "__main__":
    unittest.main()
