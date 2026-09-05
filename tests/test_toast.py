"""The notice the companion places itself.

Shino, 2026-09-04: "can we make our notification a little higher? It's
blocking other elements" - about the message shown when Vemal's Ears
minimises to the tray. That was a Windows tray balloon, and Windows
decides where a balloon goes; an application cannot move it. So the
notice is now our own small window, and these pin where it lands.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from tests.support.companion import load_companion


class ToastPositionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    def test_it_sits_bottom_right_but_lifted_clear_of_the_taskbar(self):
        # A 1920x1040 work area: 1080 tall screen with a 40px taskbar.
        x, y = self.companion.toast_position((0, 0, 1920, 1040), (320, 90))
        self.assertEqual(1920 - 320 - self.companion.TOAST_SIDE_MARGIN, x)
        self.assertEqual(1040 - 90 - self.companion.TOAST_BOTTOM_MARGIN, y)
        # The whole point: well above the bottom of the usable area.
        self.assertLess(y + 90, 1040 - 60)

    def test_the_margin_is_the_knob_that_raises_it(self):
        low = self.companion.toast_position((0, 0, 1920, 1040), (320, 90), bottom_margin=0)
        high = self.companion.toast_position((0, 0, 1920, 1040), (320, 90), bottom_margin=300)
        self.assertGreater(low[1], high[1], "a bigger margin means higher up")
        self.assertEqual(1040 - 90, low[1])

    def test_it_follows_the_monitor_it_is_given(self):
        # A second screen to the left of the primary, and one below.
        x, y = self.companion.toast_position((-1920, 0, 1920, 1040), (320, 90))
        self.assertLess(x, 0)
        self.assertGreaterEqual(x, -1920)
        x, y = self.companion.toast_position((0, 1080, 2560, 1400), (320, 90))
        self.assertGreaterEqual(y, 1080)

    def test_it_never_leaves_the_screen_however_odd_the_numbers(self):
        for rect, size in (
            ((0, 0, 1920, 1040), (320, 4000)),   # taller than the screen
            ((0, 0, 200, 100), (320, 90)),       # wider than the screen
            ((0, 0, 1920, 1040), (320, 990)),    # margin would push it off the top
        ):
            with self.subTest(rect=rect, size=size):
                x, y = self.companion.toast_position(rect, size)
                left, top, width, height = rect
                self.assertGreaterEqual(x, left)
                self.assertGreaterEqual(y, top)
                self.assertLessEqual(y, top + height)


class NotifyTests(unittest.TestCase):
    """Which channel a notice takes, and that it always takes one."""

    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    def test_a_real_window_uses_the_toast_and_can_place_it(self):
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        try:
            window = self.companion.CompanionWindow(
                root, edition=self.companion.FULL_EDITION, preview=True,
            )
            root.update_idletasks()
            window.preview = False          # preview deliberately stays quiet
            window.tray = mock.Mock()
            self.assertTrue(window.notify("Downtime", "Still running down here."))
            root.update_idletasks()
            toast = window.toast
            self.assertIsNotNone(toast)
            self.assertTrue(toast.winfo_exists())
            window.tray.notify.assert_not_called()

            # Where it landed matches the geometry function, on whichever
            # monitor the window is on.
            expected = self.companion.toast_position(
                self.companion.work_area(int(getattr(window, "hwnd", 0) or 0), root),
                (toast.winfo_reqwidth(), toast.winfo_reqheight()),
            )
            self.assertEqual(expected[0], toast.winfo_x())
            self.assertEqual(expected[1], toast.winfo_y())

            # A second notice replaces the first rather than stacking.
            first = toast
            window.notify("Downtime", "Another one.")
            root.update_idletasks()
            self.assertFalse(first.winfo_exists())
            self.assertIsNotNone(window.toast)

            window.toast.dismiss()
            self.assertIsNone(window.toast)
        finally:
            # Replacing and dismissing the notices destroys their widgets,
            # but their six-second Tcl timers outlive those commands.
            for timer in root.tk.call("after", "info"):
                root.after_cancel(timer)
            root.destroy()

    def test_without_tk_it_falls_back_to_the_tray_balloon(self):
        """Badly placed beats absent: a member who never learns their
        message failed to send is worse off than one who reads it in the
        wrong corner."""
        tray = mock.Mock()
        tray.notify.return_value = True
        window = SimpleNamespace(preview=False, tray=tray, toast=None)
        self.assertTrue(
            self.companion.CompanionWindow.notify(window, "Title", "Body")
        )
        tray.notify.assert_called_once_with("Title", "Body")
        self.assertIsNone(window.toast)

    def test_preview_windows_stay_silent(self):
        tray = mock.Mock()
        window = SimpleNamespace(preview=True, tray=tray, toast=None)
        self.assertFalse(
            self.companion.CompanionWindow.notify(window, "Title", "Body")
        )
        tray.notify.assert_not_called()

    def test_the_minimise_notice_goes_through_it(self):
        for product_name in ("Downtime", "Vemal's Ears", "Downtime Lite"):
            with self.subTest(product_name=product_name):
                # Keep the real hide/close_menu methods, with no Tk window,
                # service or native tray handle needed for this transition.
                window = object.__new__(self.companion.CompanionWindow)
                window.root = mock.Mock(spec=["withdraw"])
                window.menu = mock.Mock(spec=["dismiss"])
                window.tray = mock.Mock(spec=["notify"])
                window.notify = mock.Mock(return_value=True)
                window.product_name = product_name
                window.hidden = False
                window.announced_tray = False

                window.hide()

                self.assertTrue(window.hidden)
                self.assertTrue(window.announced_tray)
                window.menu.dismiss.assert_called_once_with()
                window.root.withdraw.assert_called_once_with()
                window.notify.assert_called_once_with(
                    product_name,
                    "Still running down here. Right click for Exit.",
                )
                window.tray.notify.assert_not_called()

                # Reopening and hiding again must still hide the window,
                # while the one-time explanation stays silent.
                window.hidden = False
                window.hide()

                self.assertTrue(window.hidden)
                self.assertEqual(window.root.withdraw.call_count, 2)
                self.assertEqual(window.menu.dismiss.call_count, 2)
                window.notify.assert_called_once_with(
                    product_name,
                    "Still running down here. Right click for Exit.",
                )
                window.tray.notify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
