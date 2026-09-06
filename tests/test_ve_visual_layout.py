"""Measure the real VE widgets without showing UI or starting live services.

The dimensions below are logical pixels; Windows DPI scaling grows both the
window and its fonts. Vertical scrolling is supported at the smaller size.
No screenshots, pairing credentials, or live caption services are involved.
"""

from contextlib import contextmanager
import itertools
import json
from pathlib import Path
import subprocess
import sys
import tkinter as tk
from tkinter import font as tkfont
import unittest
from unittest import mock

from tests.support.companion import isolated_companion, load_companion


SCALES = (1.0, 1.25, 1.5)
LOGICAL_SIZES = ((900, 760), (760, 640))
ROOT = Path(__file__).resolve().parents[1]


def buttons_under(widget):
    """Include every native button, not just the managed-card bookkeeping."""
    result = []
    for child in widget.winfo_children():
        if isinstance(child, tk.Button):
            result.append(child)
        result.extend(buttons_under(child))
    return result


def rectangle(widget):
    left, top = widget.winfo_rootx(), widget.winfo_rooty()
    return left, top, left + widget.winfo_width(), top + widget.winfo_height()


def relative_box(widget, root):
    left, top, right, bottom = rectangle(widget)
    return [left - root.winfo_rootx(), top - root.winfo_rooty(),
            right - left, bottom - top]


def contrast_ratio(first, second, root):
    def luminance(color):
        channels = [value / 65535 for value in root.winfo_rgb(color)]
        linear = [value / 12.92 if value <= 0.04045
                  else ((value + 0.055) / 1.055) ** 2.4 for value in channels]
        return sum(value * weight for value, weight in
                   zip(linear, (0.2126, 0.7152, 0.0722)))

    light, dark = sorted((luminance(first), luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


class VemalsVisualLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = load_companion()

    @contextmanager
    def preview(self, scale=1.0, size=(900, 760), edition="member"):
        with isolated_companion(edition):
            try:
                root = tk.Tk()
            except tk.TclError as error:
                self.skipTest("Native Tk is unavailable: " + str(error))
            root.withdraw()
            # Alpha is set before CompanionWindow's preview maps the root. Tests
            # inspect only their own widget geometry, never the user's desktop.
            root.attributes("-alpha", 0.0)
            original_scaling = float(root.tk.call("tk", "scaling"))
            root.tk.call("tk", "scaling", (96.0 / 72.0) * scale)
            toplevel_init = tk.Toplevel.__init__

            def transparent_toplevel(widget, *args, **kwargs):
                toplevel_init(widget, *args, **kwargs)
                widget.withdraw()
                widget.attributes("-alpha", 0.0)

            try:
                with mock.patch.object(self.app.Service, "start", side_effect=AssertionError("preview started a service")), \
                        mock.patch.object(self.app, "guild_credential_store", side_effect=AssertionError("preview read pairing")), \
                        mock.patch.object(tk.Toplevel, "__init__", transparent_toplevel):
                    window = self.app.CompanionWindow(root, edition=edition, preview=True)
                    root.geometry(f"{round(size[0] * scale)}x{round(size[1] * scale)}+0+0")
                    self.settle(root)
                    yield root, window
            finally:
                root.tk.call("tk", "scaling", original_scaling)
                root.destroy()

    @staticmethod
    def settle(root):
        # Configure events drive responsive rows and the scroll canvas width.
        for _ in range(3):
            root.update()

    def assert_buttons_fit(self, surface):
        buttons = [button for button in buttons_under(surface) if button.winfo_ismapped()]
        self.assertTrue(buttons, "The measured surface has no visible native buttons")
        for button in buttons:
            label = str(button.cget("text"))
            with self.subTest(button=label):
                left, _top, right, _bottom = rectangle(button)
                ancestor = button.master
                while ancestor is not None:
                    parent_left, _parent_top, parent_right, _parent_bottom = rectangle(ancestor)
                    self.assertGreaterEqual(left, parent_left - 1, label + " clipped at left edge")
                    self.assertLessEqual(right, parent_right + 1, label + " clipped at right edge")
                    if ancestor is surface:
                        break
                    ancestor = ancestor.master
                self.assertGreaterEqual(button.winfo_width() + 1, button.winfo_reqwidth(),
                                        label + " allocated less than its required width")
                font = tkfont.Font(root=surface, font=button.cget("font"))
                if len(label) > 2:
                    self.assertGreaterEqual(font.actual("size"), 9,
                                            label + " text was made smaller to fit")
                    self.assertGreaterEqual(button.winfo_width(), font.measure(label) + 4,
                                            label + " does not have room for its text")
                if str(button.cget("state")) != "disabled":
                    self.assertIn(str(button.cget("takefocus")), ("1", "true"))
        for first, second in itertools.combinations(buttons, 2):
            if first.master is not second.master:
                continue
            a, b = rectangle(first), rectangle(second)
            overlap_width = min(a[2], b[2]) - max(a[0], b[0])
            overlap_height = min(a[3], b[3]) - max(a[1], b[1])
            self.assertFalse(
                overlap_width > 1 and overlap_height > 1,
                "Overlapping buttons: " + str(first.cget("text")) + " / " + str(second.cget("text")),
            )
        return buttons

    def assert_velvet(self, buttons):
        from downtime_ui.buttons import VelvetButton

        for button in buttons:
            self.assertIsInstance(button, VelvetButton, str(button.cget("text")))

    def main_capability_buttons(self, window):
        return [window.hero_connect_button, window.member_caption_button,
                window.caption_target_button, window.speaker_mute_button,
                *[button for card in window.managed_cards.values() for button in card["buttons"]]]

    def test_main_and_profile_buttons_fit_at_supported_sizes_and_scales(self):
        measurements = []
        for scale, size in itertools.product(SCALES, LOGICAL_SIZES):
            with self.subTest(scale=scale, logical_size=size):
                with self.preview(scale, size) as (root, window):
                    collected = self.assert_buttons_fit(root)
                    capabilities = self.main_capability_buttons(window)
                    self.assertTrue(all(button in collected for button in capabilities))
                    self.assert_velvet(capabilities)
                    # Exercise longer live labels, not only the preview's
                    # shorter connected/paused states.
                    window.member_caption_button.configure(text="Resume caption feed")
                    window.hero_connect_button.configure(text="Connect guild")
                    self.settle(root)
                    self.assert_buttons_fit(root)
                    measurements.append({
                        "scale": scale, "logical_size": list(size),
                        "window": relative_box(root, root),
                        "content_height": window.main.winfo_height(),
                        "lowest_capability": max(rectangle(button)[3] - root.winfo_rooty()
                                                 for button in capabilities),
                    })
                    window.open_settings()
                    # Toplevels do not inherit the root's alpha setting.
                    window.menu.attributes("-alpha", 0.0)
                    self.settle(root)
                    profile_buttons = self.assert_buttons_fit(window.menu)
                    self.assertIn(window.menu.message_name_save, profile_buttons)
                    self.assertIn(window.menu.hotkey_change, profile_buttons)
                    self.assert_velvet([window.menu.message_name_save, window.menu.hotkey_change])
                    window.menu.hotkey_change.configure(text="Cancel")
                    window.menu.hotkey_value.configure(text="Press shortcut")
                    self.settle(root)
                    self.assert_buttons_fit(window.menu)
                    window.close_menu()
        # Diagnostic, deliberately not a brittle total-height assertion:
        # smaller displays keep the normal vertical scrolling fallback.
        print("VE layout measurements: " + json.dumps(measurements, sort_keys=True))

    def test_message_action_retains_readable_text_in_every_state(self):
        with self.preview() as (root, window):
            button = window.hero_chat_button
            self.assert_velvet([button])
            action = mock.Mock()
            button.configure(command=action)
            for event in ("<Leave>", "<Enter>", "<ButtonPress-1>"):
                with self.subTest(event=event):
                    button.event_generate(event, x=4, y=4)
                    self.settle(root)
                    self.assertGreaterEqual(
                        contrast_ratio(button.cget("fg"), button.cget("bg"), root), 4.5,
                    )
            # Release outside the button so this state check cannot invoke
            # the quick-chat command.
            button.event_generate("<ButtonRelease-1>", x=-1, y=-1)
            button.event_generate("<Leave>")
            self.settle(root)
            button.configure(state="disabled")
            action.reset_mock()
            button.invoke()
            action.assert_not_called()
            self.assertEqual("disabled", str(button.cget("state")))
            button.configure(state="normal")
            self.assert_buttons_fit(root)

    def test_overlay_preview_helper_starts_and_exits(self):
        if sys.platform != "win32":
            self.skipTest("The caption preview helper is Windows-only.")
        completed = subprocess.run(
            [
                sys.executable,
                "-E",
                "-s",
                "-B",
                str(ROOT / "packaging" / "preview_caption_ui.py"),
                "overlay",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(
            0,
            completed.returncode,
            (completed.stderr or "") + (completed.stdout or ""),
        )



if __name__ == "__main__":
    unittest.main()
