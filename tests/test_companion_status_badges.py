# SPDX-License-Identifier: GPL-3.0-only
"""Status boxes keep the same footprint as real service states change."""

import tkinter as tk
from tkinter import font as tkfont
import unittest
from unittest import mock

from tests.support.companion import isolated_companion


class StatusBadgeSizeTests(unittest.TestCase):
    """Result and lifecycle boxes must align and fit their longest labels."""

    def setUp(self):
        self.app = self.enterContext(isolated_companion("full-local"))
        self.enterContext(mock.patch.object(
            self.app.Service, "start",
            side_effect=AssertionError("badge preview started a service"),
        ))
        self.enterContext(mock.patch.object(
            self.app, "guild_credential_store",
            side_effect=AssertionError("badge preview read pairing credentials"),
        ))
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
        # CompanionWindow maps the preview during construction. Make it
        # transparent first, then measure only widgets belonging to this root.
        self.root.attributes("-alpha", 0.0)
        self.original_scaling = float(self.root.tk.call("tk", "scaling"))
        self.root.tk.call("tk", "scaling", 96.0 / 72.0)
        self.window = self.app.CompanionWindow(
            self.root, edition=self.app.FULL_EDITION, preview=True,
        )
        # Current full/hosted editions select only job services. Exercise the
        # lifecycle renderer with its real service specifications explicitly;
        # comparing the default rows alone would compare two result boxes.
        self.lifecycles = [self.app.Service(spec=spec, window=self.window)
                           for spec in self.app.SERVICES
                           if spec.show_lifecycle_controls
                           and spec.separate_lifecycle_controls]
        for service in self.lifecycles:
            self.window.build_row(service)
        self.settle()
        self.results = [service for service in self.window.services
                        if service.result_badge is not None]
        self.assertTrue(self.results, "No result boxes were constructed")
        self.assertTrue(self.lifecycles, "No lifecycle boxes were constructed")
        self.assertTrue(all(service.lifecycle_badge is not None
                            for service in self.lifecycles))

    def close_preview(self):
        # Cancel pending animation/layout work before Tcl deletes its commands.
        # Cleanup also runs when construction or a geometry assertion fails.
        for callback in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(callback)
        if hasattr(self, "original_scaling"):
            self.root.tk.call("tk", "scaling", self.original_scaling)
        self.root.destroy()

    def settle(self):
        # The scroll canvas and responsive rows settle through Configure events.
        for _ in range(3):
            self.root.update()

    @staticmethod
    def footprint(badge):
        shell = badge.master
        return (badge.winfo_width(), badge.winfo_height(),
                badge.winfo_reqwidth(), badge.winfo_reqheight(),
                shell.winfo_width(), shell.winfo_height())

    def assert_label_fits(self, badge, label):
        self.assertTrue(badge.winfo_ismapped(), "The badge is not laid out")
        font = tkfont.Font(root=self.root, font=badge.cget("font"))
        horizontal_inset = sum(
            self.root.winfo_pixels(badge.cget(option))
            for option in ("padx", "borderwidth", "highlightthickness")
        )
        self.assertGreaterEqual(
            badge.winfo_width(), font.measure(label) + 2 * horizontal_inset,
            "Badge clips its status label: " + label,
        )

    def test_result_and_lifecycle_boxes_are_constructed_at_the_same_size(self):
        boxes = ([service.result_badge for service in self.results]
                 + [service.lifecycle_badge for service in self.lifecycles])
        self.assertEqual(
            1, len({self.footprint(badge) for badge in boxes}),
            "Result and lifecycle boxes have different pixel dimensions",
        )
        for badge in boxes:
            self.assert_label_fits(badge, str(badge.cget("text")))

    def test_every_result_state_fits_without_resizing_its_box(self):
        # Done intentionally survives queued/checking updates. Visit it last
        # so each transition exercises the real status setter without resetting
        # internal job state or weakening that separate behavior.
        states = sorted(self.app.JOB_BADGE_STYLES, key=lambda state: state == "done")
        for service in self.results:
            badge = service.result_badge
            initial = self.footprint(badge)
            for state in states:
                label, color = self.app.JOB_BADGE_STYLES[state]
                with self.subTest(service=service.spec.key, state=state):
                    service.set_status("Test result", color, job_state=state)
                    self.settle()
                    self.assertEqual(label, badge.cget("text"))
                    self.assertEqual(color, badge.cget("fg"))
                    self.assertEqual(initial, self.footprint(badge),
                                     "Result label changed its box size")
                    self.assert_label_fits(badge, label)

    def test_every_lifecycle_state_fits_without_resizing_its_box(self):
        for service in self.lifecycles:
            badge = service.lifecycle_badge
            initial = self.footprint(badge)
            for state, (label, color) in self.app.LIFECYCLE_BADGE_STYLES.items():
                with self.subTest(service=service.spec.key, state=state):
                    service.set_lifecycle_state(state)
                    self.settle()
                    self.assertEqual(label, badge.cget("text"))
                    self.assertEqual(color, badge.cget("fg"))
                    self.assertEqual(initial, self.footprint(badge),
                                     "Lifecycle label changed its box size")
                    self.assert_label_fits(badge, label)


if __name__ == "__main__":
    unittest.main()
