"""One sentence, in one place.

Shino, 2026-09-05, looking at the Raid tools screen: "there are two text
boxes appearing for the same text". Boss Talents' card said "Watching for
a build request" and the Activity strip underneath said "Boss Talents:
Watching for a build request" - the same words, derived twice from the
same marker table, two inches apart.

The card is the one that survives, because it says which service without
having to name it. These tests build the real window and read what is
actually on it, rather than trusting that a widget was never created.
"""

from __future__ import annotations

import tkinter as tk
import unittest

from tests.support.companion import load_companion


def label_texts(widget) -> list[str]:
    """Every piece of text under a widget, including its own."""
    found = []
    try:
        text = widget.cget("text")
    except tk.TclError:
        text = ""
    if isinstance(text, str) and text.strip():
        found.append(text.strip())
    for child in widget.winfo_children():
        found.extend(label_texts(child))
    return found


class ActivityHeaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.window = self.companion.CompanionWindow(
            self.root,
            edition=self.companion.FULL_EDITION,
            preview=True,
        )
        self.root.update_idletasks()

    def tearDown(self):
        self.root.destroy()

    def test_the_header_names_itself_and_says_nothing_else(self):
        texts = label_texts(self.window.activity_shell)
        self.assertIn("Activity", texts)
        self.assertNotIn("Recent activity will appear here.", texts)

    def test_there_is_no_summary_widget_to_write_to(self):
        self.assertIsNone(self.window.activity_summary)

    def test_a_status_sentence_lands_on_the_card_and_not_in_the_header(self):
        """The regression, end to end.

        A Boss Talents line that the marker table recognises has to change
        the Boss Talents card and leave the Activity header alone. Before
        this, both moved and the member read the same words twice.
        """
        talents = next(
            service for service in self.window.services
            if service.spec.key == "talents"
        )
        marker, sentence, _color, _state = talents.spec.markers[0]
        process = object()
        talents.process = process
        talents.lines.put((process, marker + "\n"))
        talents.drain()
        self.root.update_idletasks()

        self.assertEqual(sentence, talents.status.cget("text"))
        header_text = " ".join(label_texts(self.window.activity_shell))
        self.assertNotIn(sentence, header_text)
        self.assertNotIn("Boss Talents:", header_text)

    def test_the_clear_and_show_buttons_are_still_reachable(self):
        """Removing the middle of a row is a chance to lose its right end."""
        texts = label_texts(self.window.activity_shell)
        self.assertIn("Clear", texts)
        self.assertIn("Show", texts)


if __name__ == "__main__":
    unittest.main()
