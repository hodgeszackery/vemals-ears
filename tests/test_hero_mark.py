"""The clock has to arrive whole, at whatever size it is asked for.

Shino, 2026-09-05: "the Logo is messed up somehow". It was. The hero mark
and the title-bar mark are the 256px DowntimeIcon.png scaled down, and the
scaling was Pillow's. His installed bundle carried a runtime staged before
Pillow was pinned, so the code fell through to the plain Tk path - which
handed the image back at its full 256px and let the canvas clip it. A 68px
canvas showed the middle 68 pixels of the artwork: the orange hands, no
gear, no blue ring.

Nothing here needs Pillow, on purpose. These tests exercise the path taken
when it is absent, because that is the path that was wrong.
"""

from __future__ import annotations

import math
import tkinter as tk
import unittest

from tests.support.companion import load_companion


class ScaleFactorTests(unittest.TestCase):
    """The arithmetic, without a display."""

    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    def factors(self, source, size):
        return self.companion.photo_scale_factors(source, size)

    def test_the_two_sizes_the_companion_actually_asks_for(self):
        # The hero mark: an exact quarter, so no zoom is needed at all.
        self.assertEqual((1, 4), self.factors(256, 64))
        # The title bar: 20/256 reduces to 5/64, which Tk can do.
        self.assertEqual((5, 64), self.factors(256, 20))

    def test_the_factors_land_on_the_requested_size(self):
        for source in (16, 20, 64, 100, 256, 512, 1024, 1253, 1254):
            for size in (16, 20, 24, 32, 48, 64, 68):
                with self.subTest(source=source, size=size):
                    zoom, shrink = self.factors(source, size)
                    self.assertEqual(
                        size,
                        source * zoom // shrink,
                        "zoom then subsample must be exact, never nearly",
                    )

    def test_the_fraction_is_fully_reduced(self):
        """An unreduced fraction is the same picture through a bigger
        intermediate. 20/256 unreduced would zoom to 5120px first."""
        for source, size in ((256, 20), (256, 64), (512, 48)):
            with self.subTest(source=source, size=size):
                zoom, shrink = self.factors(source, size)
                self.assertEqual(1, math.gcd(zoom, shrink))

    def test_nonsense_sizes_do_not_divide_by_zero(self):
        for source, size in ((0, 64), (256, 0), (-8, 64), (256, -8)):
            with self.subTest(source=source, size=size):
                self.assertEqual((1, 1), self.factors(source, size))


class BoundedImageScalingTests(unittest.TestCase):
    """Track Tk's intermediate allocations without allocating large images."""

    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    class Image:
        def __init__(self, width, height, allocations):
            self._width = width
            self._height = height
            self.allocations = allocations

        def width(self):
            return self._width

        def height(self):
            return self._height

        def resized(self, width, height):
            self.allocations.append((width, height))
            return type(self)(width, height, self.allocations)

        def zoom(self, x, y=None):
            return self.resized(self._width * x, self._height * (x if y is None else y))

        def subsample(self, x, y=None):
            y = x if y is None else y
            # Tk retains a last partial sample, so this is ceiling division.
            return self.resized((self._width + x - 1) // x, (self._height + y - 1) // y)

    def test_large_and_coprime_sources_have_bounded_intermediates(self):
        for source in (16, 127, 256, 509, 1253, 1254, 4093):
            for size in (20, 64, 68):
                with self.subTest(source=source, size=size):
                    allocations = []
                    original = self.Image(source, source, allocations)
                    fitted = self.companion.fit_photo_image(original, size)
                    self.assertEqual((size, size), (fitted.width(), fitted.height()))
                    # Pre-reduction bounds each side below twice the target;
                    # one-axis scaling then bounds area, not just the width.
                    self.assertLessEqual(max(w * h for w, h in allocations), 4 * size ** 3)

    def test_invalid_target_sizes_leave_the_source_untouched(self):
        for size in (0, -8):
            with self.subTest(size=size):
                allocations = []
                original = self.Image(1254, 1254, allocations)
                self.assertIs(original, self.companion.fit_photo_image(original, size))
                self.assertEqual([], allocations)


class FitPhotoImageTests(unittest.TestCase):
    """The behaviour, against a real Tk image."""

    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        self.root.destroy()

    def square(self, size):
        image = tk.PhotoImage(width=size, height=size, master=self.root)
        image.put("#ff9d00", to=(0, 0, size, size))
        return image

    def test_it_reaches_the_requested_size(self):
        for source, size in ((256, 64), (256, 20), (128, 16), (16, 64)):
            with self.subTest(source=source, size=size):
                fitted = self.companion.fit_photo_image(
                    self.square(source), size
                )
                self.assertEqual(size, fitted.width())
                self.assertEqual(size, fitted.height())

    def test_large_non_power_of_two_artwork_is_scaled_whole(self):
        for source, size in ((1254, 64), (1254, 20), (1253, 64), (127, 64)):
            with self.subTest(source=source, size=size):
                original = self.square(source)
                middle = source // 2
                original.put("#00aaff", to=(middle, 0, source, middle))
                original.put("#00cc66", to=(0, middle, middle, source))
                original.put("#cc0044", to=(middle, middle, source, source))
                fitted = self.companion.fit_photo_image(original, size)
                self.assertEqual((size, size), (fitted.width(), fitted.height()))
                for x, y, colour in (
                    (0, 0, (255, 157, 0)),
                    (size - 1, 0, (0, 170, 255)),
                    (0, size - 1, (0, 204, 102)),
                    (size - 1, size - 1, (204, 0, 68)),
                ):
                    with self.subTest(pixel=(x, y)):
                        self.assertEqual(colour, fitted.get(x, y))

    def test_an_image_already_the_right_size_is_left_alone(self):
        original = self.square(64)
        self.assertIs(original, self.companion.fit_photo_image(original, 64))

    def test_the_artwork_survives_the_trip(self):
        """Scaled, not cropped: the corner pixel has to still be there.

        This is the regression itself. Unscaled art in a small canvas
        keeps its middle and loses its edges, so a test that only checked
        the centre would have passed while the bug was live.
        """
        fitted = self.companion.fit_photo_image(self.square(256), 20)
        for x, y in ((0, 0), (19, 0), (0, 19), (19, 19), (10, 10)):
            with self.subTest(pixel=(x, y)):
                self.assertEqual((255, 157, 0), fitted.get(x, y))


class LoadMarkImageTests(unittest.TestCase):
    """What the window asks for is what the window gets."""

    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        self.root.destroy()

    def test_the_tk_path_returns_the_size_it_was_asked_for(self):
        """Pillow is pushed out of the way so the fallback is what runs.

        The companion imports PIL inside the method, so an import that
        raises is enough to take the branch a Pillow-less runtime takes.
        """
        import builtins
        from unittest.mock import patch

        real_import = builtins.__import__

        def refuse_pillow(name, *args, **kwargs):
            if name == "PIL" or name.startswith("PIL."):
                raise ImportError("no Pillow in this runtime")
            return real_import(name, *args, **kwargs)

        window = type("W", (), {})()
        window.root = self.root
        with patch.object(builtins, "__import__", refuse_pillow):
            for edition in (
                self.companion.MEMBER_EDITION,
            ):
                window.edition = edition
                window.member_edition = edition == self.companion.MEMBER_EDITION
                with self.subTest(edition=edition):
                    self.assertIsNotNone(self.companion.find_artwork(edition))
                for size in (64, 20):
                    with self.subTest(edition=edition, size=size):
                        image = self.companion.CompanionWindow.load_mark_image(
                            window, size
                        )
                        self.assertIsNotNone(image)
                        self.assertEqual(size, image.width())
                        self.assertEqual(size, image.height())


if __name__ == "__main__":
    unittest.main()
