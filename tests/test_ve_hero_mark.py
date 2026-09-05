# SPDX-License-Identifier: GPL-3.0-only
"""Vemal's Ears wears its own logo on its own front page.

Shino, 2026-09-05, pointing at the circle at the top of the app: "can we
update this image in vemals ears to the new VE logo?"

The title bar and the taskbar already carried Windows/VemalsEars.png. The
hero card did not - it still drew the old listening mark, a ring with
three lines and a chevron, which is the mark that predates the artwork.
So the one place a member looks at longest was the only place still
showing the old identity.
"""

from __future__ import annotations

import tkinter as tk
import unittest
from unittest import mock

from tests.support.companion import isolated_companion


class VeHeroMarkTests(unittest.TestCase):
    def setUp(self):
        self.app = self.enterContext(isolated_companion("member"))
        toplevel_init = tk.Toplevel.__init__

        def transparent_toplevel(widget, *args, **kwargs):
            toplevel_init(widget, *args, **kwargs)
            widget.withdraw()
            widget.attributes("-alpha", 0.0)

        self.enterContext(mock.patch.object(
            tk.Toplevel, "__init__", transparent_toplevel,
        ))
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest("Native Tk is unavailable: " + str(error))
        self.addCleanup(self.close_preview)
        self.root.withdraw()
        self.root.attributes("-alpha", 0.0)

    def close_preview(self):
        for callback in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(callback)
        self.root.destroy()

    def member_window(self):
        window = self.app.CompanionWindow(
            self.root, edition=self.app.MEMBER_EDITION, preview=True,
        )
        self.root.update_idletasks()
        return window

    def hero_canvas(self, window):
        """The 68px canvas at the left of the hero card."""
        for canvas in self.canvases(window.main):
            if canvas.winfo_reqwidth() == 68 and canvas.winfo_reqheight() == 68:
                return canvas
        self.fail("the hero mark canvas was not found")

    def canvases(self, widget):
        found = []
        if isinstance(widget, tk.Canvas):
            found.append(widget)
        for child in widget.winfo_children():
            found.extend(self.canvases(child))
        return found

    def test_the_artwork_is_what_the_hero_card_shows(self):
        window = self.member_window()
        canvas = self.hero_canvas(window)
        kinds = {canvas.type(item) for item in canvas.find_all()}
        self.assertIn("image", kinds, "the hero card is not showing the logo")
        # The old mark was ovals, arcs and lines. None of it should remain.
        self.assertEqual(
            set(), kinds & {"oval", "arc", "line"},
            "the drawn listening mark is still on the hero card",
        )

    def test_the_image_is_held_so_tk_cannot_collect_it(self):
        """A PhotoImage nothing references renders as an empty square."""
        window = self.member_window()
        self.assertIsNotNone(window.hero_mark_image)
        self.assertEqual(64, window.hero_mark_image.width())

    def test_the_artwork_comes_from_the_ve_file(self):
        source = self.app.find_artwork(self.app.MEMBER_EDITION)
        self.assertIsNotNone(source, "the VE artwork is missing from the tree")
        self.assertEqual("VemalsEars.png", source.name)

    def test_a_bundle_without_artwork_still_gets_a_mark(self):
        """Missing art is a cosmetic problem, never an empty square."""
        with mock.patch.object(
            self.app.CompanionWindow, "load_mark_image", return_value=None,
        ):
            window = self.member_window()
        canvas = self.hero_canvas(window)
        kinds = {canvas.type(item) for item in canvas.find_all()}
        self.assertNotIn("image", kinds)
        self.assertTrue(
            kinds & {"oval", "arc", "line"},
            "the drawn fallback mark must still be there",
        )


if __name__ == "__main__":
    unittest.main()
