"""Native accessibility and interaction contracts for VE's rounded controls."""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from types import SimpleNamespace
import unittest
from unittest import mock

from PIL import Image, ImageTk

from downtime_ui import VelvetButton, VEMALS_EARS_THEME


class VelvetButtonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
            cls.root.withdraw()
        except tk.TclError as error:
            raise unittest.SkipTest("Tk display unavailable: " + str(error))

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def setUp(self):
        self.parent = tk.Frame(self.root, bg=VEMALS_EARS_THEME.panel)
        self.parent.pack(fill="both", expand=True)
        self.addCleanup(self.parent.destroy)

    def button(self, text="Continue", **kwargs):
        button = VelvetButton(self.parent, text, theme=VEMALS_EARS_THEME, **kwargs)
        button.pack()
        return button

    def test_native_text_command_and_disabled_invoke_are_preserved(self):
        calls = []
        button = self.button("Message Group", command=lambda: calls.append("first"))
        self.assertIsInstance(button, tk.Button)
        self.assertEqual("Button", button.winfo_class())
        self.assertEqual("Message Group", button.cget("text"))
        self.assertEqual("center", button.cget("compound"))
        button.invoke()
        button.configure(text="Sending", state="disabled")
        button.invoke()
        self.assertEqual(["first"], calls)
        self.assertEqual("Sending", button.cget("text"))
        self.assertEqual(0, int(button.cget("takefocus")))
        button.configure(state="normal", command=lambda: calls.append("second"))
        button.invoke()
        self.assertEqual(["first", "second"], calls)
        self.assertEqual(1, int(button.cget("takefocus")))

    def test_variants_keep_distinct_fills_and_native_foregrounds(self):
        theme = VEMALS_EARS_THEME
        expected = {
            "primary": (theme.accent, theme.bg),
            "conversation": (theme.conversation, theme.conversation_text),
            "secondary": (theme.panel_alt, theme.text),
            "quiet": (theme.panel, theme.muted),
        }
        for variant, (fill, foreground) in expected.items():
            with self.subTest(variant=variant):
                button = self.button(variant=variant)
                self.assertEqual(fill, button.cget("bg"))
                self.assertEqual(foreground, button.cget("fg"))
                self.assertEqual(variant == "primary", button.is_accent)
                button.destroy()

    def test_background_has_rounded_corners_and_is_never_saved(self):
        with mock.patch.object(Image.Image, "save", side_effect=AssertionError("image written")):
            button = self.button(variant="primary")
            raster = ImageTk.getimage(button._surface_image).convert("RGB")
            corner = tuple(int(VEMALS_EARS_THEME.panel[index:index + 2], 16) for index in (1, 3, 5))
            fill = tuple(int(VEMALS_EARS_THEME.accent[index:index + 2], 16) for index in (1, 3, 5))
            self.assertEqual(corner, raster.getpixel((0, 0)))
            self.assertEqual(fill, raster.getpixel((raster.width // 2, raster.height // 2)))
            self.assertNotEqual(corner, raster.getpixel((raster.width // 2, 5)))

    def test_hover_press_focus_and_disabled_states_are_visibly_distinct(self):
        button = self.button(variant="primary")
        normal = ImageTk.getimage(button._surface_image).tobytes()
        button._on_enter()
        hover = ImageTk.getimage(button._surface_image).tobytes()
        self.assertEqual(VEMALS_EARS_THEME.accent_hover, button.cget("bg"))
        button._on_press()
        pressed = ImageTk.getimage(button._surface_image).tobytes()
        button._on_release()
        button._on_leave()
        button._set_focus(True)
        focus = ImageTk.getimage(button._surface_image).tobytes()
        self.assertNotEqual(normal, hover)
        self.assertNotEqual(hover, pressed)
        self.assertNotEqual(normal, focus)
        button.configure(state="disabled")
        disabled = ImageTk.getimage(button._surface_image).tobytes()
        self.assertFalse(button._pressed)
        button._on_enter()
        self.assertEqual(disabled, ImageTk.getimage(button._surface_image).tobytes())

    def test_return_and_space_activate_once_and_never_when_disabled(self):
        calls = []
        button = self.button(command=lambda: calls.append("send"))
        for key in ("Return", "space"):
            with self.subTest(key=key):
                event = SimpleNamespace(keysym=key)
                before = len(calls)
                self.assertEqual("break", button._on_key_press(event))
                button._on_key_press(event)  # Windows key repeat.
                self.assertEqual("break", button._on_key_release(event))
                button._on_key_release(event)
                self.assertEqual(before + 1, len(calls))
                button.configure(state="disabled")
                button._on_key_press(event)
                button._on_key_release(event)
                self.assertEqual(before + 1, len(calls))
                button.configure(state="normal")

    def test_losing_focus_cancels_a_pending_keyboard_action(self):
        command = mock.Mock()
        button = self.button(command=command)
        event = SimpleNamespace(keysym="Return")
        button._on_key_press(event)
        button._set_focus(False)
        button._on_key_release(event)
        command.assert_not_called()
        self.assertFalse(button._pressed)

    def test_text_font_and_padding_updates_resize_without_losing_native_label(self):
        button = self.button("Open", compact=True)
        short = button._natural_size
        button.config(text="Connect to your guild")
        long = button._natural_size
        self.assertGreater(long[0], short[0])
        self.assertEqual("Connect to your guild", button.cget("text"))
        bigger = tkfont.Font(root=self.root, family="Segoe UI", size=18, weight="bold")
        button.configure(font=bigger, padx=22, pady=14)
        self.assertGreater(button._natural_size[0], long[0])
        self.assertGreater(button._natural_size[1], long[1])
        self.assertEqual(22, button.cget("padx"))
        self.assertEqual(14, button.cget("pady"))
        self.assertEqual(str(bigger), str(button.cget("font")))

    def test_allocated_width_does_not_become_the_requested_minimum(self):
        button = self.button("Move")
        natural = button._natural_size
        button._on_resize(SimpleNamespace(width=600, height=natural[1]))
        self.assertEqual(600, button._surface_size[0])
        self.assertEqual(natural[0], int(tk.Button.cget(button, "width")))
        button._on_resize(SimpleNamespace(width=natural[0], height=natural[1]))
        self.assertEqual(natural, button._surface_size)

    def test_stringvariable_resizes_and_trace_is_removed_on_destroy(self):
        label = tk.StringVar(master=self.root, value="Join")
        button = self.button(textvariable=label)
        short = button._natural_size[0]
        label.set("Connect to Moonwell Raiders")
        self.assertGreater(button._natural_size[0], short)
        self.assertEqual("Connect to Moonwell Raiders", button._label())
        custom_trace = button._text_trace
        self.assertTrue(any(custom_trace in str(trace) for trace in label.trace_info()))
        button.destroy()
        self.assertFalse(any(custom_trace in str(trace) for trace in label.trace_info()))
        label.set("Safe after destruction")

    def test_configure_aliases_preserve_color_updates_and_icon_geometry(self):
        button = self.button("Message Group", icon="chat")
        with_icon = button._natural_size[0]
        button.configure(icon=None, background="#332244", foreground="#ffffff",
                         activebackground="#554466", highlightbackground="#9988aa")
        self.assertLess(button._natural_size[0], with_icon)
        self.assertEqual("#332244", button.cget("background"))
        self.assertEqual("#ffffff", button.cget("foreground"))
        button._on_enter()
        self.assertEqual("#554466", button.cget("bg"))
        button._on_leave()
        self.assertEqual("#332244", button.cget("bg"))
        self.assertEqual("Message Group", button.cget("text"))

    def test_all_vector_icons_draw_without_changing_the_native_name(self):
        for icon in sorted(VelvetButton.ICONS):
            with self.subTest(icon=icon):
                button = self.button("Action", icon=icon)
                self.assertEqual("Action", button.cget("text"))
                self.assertIsNotNone(button._surface_image)
                button.destroy()

    def test_invalid_variant_does_not_replace_a_working_configuration(self):
        button = self.button()
        with self.assertRaises(ValueError):
            button.configure(variant="unknown")
        self.assertEqual("secondary", button.variant)

    def test_named_tk_colors_work_in_pressed_surfaces_and_config_queries(self):
        button = self.button(background="navy", activebackground="red",
                             highlightbackground="white", highlightcolor="gold")
        button._on_press()
        self.assertIsNotNone(button._surface_image)
        self.assertEqual("red", button.cget("activebackground"))
        self.assertEqual("white", button.cget("highlightbackground"))
        self.assertEqual("gold", button.cget("highlightcolor"))
        button._on_release()
        button._on_leave()
        self.assertEqual("navy", button.cget("background"))

    def test_native_active_state_does_not_leave_hover_stuck_after_mouse_exit(self):
        button = self.button(variant="primary")
        button._on_enter()
        # Tk's Button class runs after the instance Enter/Leave bindings.
        tk.Button.configure(button, state="active")
        button._on_leave()
        self.assertEqual(VEMALS_EARS_THEME.accent, button.cget("bg"))
        tk.Button.configure(button, state="normal")
        button.configure(state="active")
        button._on_leave()
        self.assertEqual(VEMALS_EARS_THEME.accent_hover, button.cget("bg"))


if __name__ == "__main__":
    unittest.main()
