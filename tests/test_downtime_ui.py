"""Comprehensive unit test suite for Downtime UI framework components."""

from __future__ import annotations

import tkinter as tk
import unittest

from downtime_ui import (
    TabBar,
    ACCENT_DOWNTIME,
    ACCENT_LITE,
    ACCENT_VEMALS,
    BG,
    BORDER,
    BUTTON_DARK_BLUE,
    CLOCKFIRE,
    DANGER,
    DOWNTIME_LITE_THEME,
    DOWNTIME_THEME,
    MUTED,
    PANEL,
    PANEL_ALT,
    SUCCESS,
    VEMALS_EARS_THEME,
    WARNING,
    AlertBannerCard,
    CardShell,
    ChatButton,
    CompactButton,
    ContextMenu,
    DangerButton,
    GlassButton,
    DrawerHeader,
    EditionBadge,
    EmptyStateView,
    HeaderDivider,
    HeroAudienceHeader,
    HotkeyInput,
    IconButton,
    JobBadge,
    MenuItem,
    PanelSurface,
    PrimaryButton,
    ProgressBar,
    RecordingIndicator,
    ScrollSurface,
    SectionHeader,
    ServiceCard,
    SignalBar,
    StatMetricCard,
    StatusOrb,
    StyledEntry,
    SupportButton,
    ToggleSwitch,
    WindowTitleBar,
    blend_colors,
    create_font_scale,
    draw_drop_shadow,
    draw_glow_ring,
    get_theme_for_edition,
    hex_to_rgb,
    rgb_to_hex,
    rounded_rectangle,
)
from downtime_ui.tables import DataTable, TableHeader, TableRow


class TestDowntimeUITheme(unittest.TestCase):
    """Verify design tokens and edition configurations."""

    def test_theme_resolutions(self):
        self.assertEqual(get_theme_for_edition("full-local"), DOWNTIME_THEME)
        self.assertEqual(get_theme_for_edition("member"), VEMALS_EARS_THEME)
        self.assertEqual(get_theme_for_edition("downtime-hosted"), DOWNTIME_LITE_THEME)
        self.assertEqual(get_theme_for_edition("unknown"), DOWNTIME_THEME)

    def test_theme_properties(self):
        self.assertEqual(DOWNTIME_THEME.badge_label, "LOCAL")
        self.assertEqual(DOWNTIME_THEME.accent, ACCENT_DOWNTIME)
        self.assertEqual(VEMALS_EARS_THEME.badge_label, "Voice & text")
        self.assertEqual(VEMALS_EARS_THEME.accent, ACCENT_VEMALS)
        self.assertEqual(DOWNTIME_LITE_THEME.badge_label, "HOSTED")
        self.assertEqual(DOWNTIME_LITE_THEME.accent, ACCENT_LITE)


class TestDowntimeUIEffects(unittest.TestCase):
    """Verify color blending and canvas graphics helpers."""

    def test_hex_rgb_conversions(self):
        self.assertEqual(hex_to_rgb("#000000"), (0, 0, 0))
        self.assertEqual(hex_to_rgb("#ffffff"), (255, 255, 255))
        self.assertEqual(hex_to_rgb("#ff0000"), (255, 0, 0))
        self.assertEqual(hex_to_rgb("#35c6f4"), (53, 198, 244))
        self.assertEqual(rgb_to_hex(53, 198, 244), "#35c6f4")
        self.assertEqual(rgb_to_hex(0, 0, 0), "#000000")
        self.assertEqual(rgb_to_hex(300, -5, 255), "#ff00ff")

    def test_color_blending(self):
        self.assertEqual(blend_colors("#000000", "#ffffff", 0.0), "#000000")
        self.assertEqual(blend_colors("#000000", "#ffffff", 1.0), "#ffffff")
        blended = blend_colors("#000000", "#ffffff", 0.5)
        self.assertEqual(blended, "#7f7f7f")


class TestDowntimeUIComponents(unittest.TestCase):
    """Test widget instantiation and interactive behaviors in a hidden Tk root."""

    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
            cls.root.withdraw()
        except tk.TclError:
            cls.root = None

    @classmethod
    def tearDownClass(cls):
        if cls.root:
            cls.root.destroy()

    def setUp(self):
        if not self.root:
            self.skipTest("Tkinter display not available in this environment")

    def test_font_scale(self):
        fonts = create_font_scale(self.root)
        self.assertIn("hero", fonts)
        self.assertIn("heading", fonts)
        self.assertIn("button", fonts)
        self.assertIn("keycap", fonts)

    def test_canvas_primitives(self):
        canvas = tk.Canvas(self.root, width=100, height=100)
        rect_id = rounded_rectangle(canvas, 10, 10, 90, 90, radius=8, fill="#ffffff")
        self.assertIsNotNone(rect_id)

        shadows = draw_drop_shadow(canvas, 10, 10, 90, 90, radius=8, layers=3)
        self.assertEqual(len(shadows), 3)

        glows = draw_glow_ring(canvas, 10, 10, 90, 90, radius=8, glow_width=2)
        self.assertEqual(len(glows), 2)
        canvas.destroy()

    def test_backgrounds(self):
        shell = CardShell(self.root, has_rail=True, rail_color="#ff0000")
        self.assertIsNotNone(shell.rail)
        shell.set_rail_color(SUCCESS)
        self.assertEqual(shell.rail.cget("bg"), SUCCESS)
        shell.set_border_highlight(True)
        shell.destroy()

        right_rail = CardShell(
            self.root,
            has_rail=True,
            rail_color=WARNING,
            rail_side="right",
        )
        self.assertEqual("right", right_rail.rail.pack_info()["side"])
        right_rail.destroy()

        panel = PanelSurface(self.root, background=PANEL)
        self.assertEqual(panel.cget("bg"), PANEL)
        panel.destroy()

        divider = HeaderDivider(self.root)
        divider.destroy()

    def test_buttons(self):
        clicked = []
        btn = PrimaryButton(self.root, "Click Me", command=lambda: clicked.append(1))
        btn.invoke()
        self.assertEqual(clicked, [1])
        btn.destroy()

        compact_std = CompactButton(self.root, "Compact", accent=False)
        self.assertFalse(compact_std.is_accent)
        compact_std.destroy()

        compact_acc = CompactButton(self.root, "Manage consent", accent=True)
        self.assertTrue(compact_acc.is_accent)
        compact_acc.destroy()

        chat = ChatButton(self.root, "Message raid")
        self.assertEqual(chat.cget("bg"), CLOCKFIRE)
        chat.destroy()

        danger = DangerButton(self.root, "Stop")
        self.assertEqual(danger.cget("fg"), DANGER)
        danger.destroy()

        patreon = SupportButton(self.root, "patreon", "Patreon")
        self.assertIn("Patreon", patreon.cget("text"))
        patreon.destroy()

        icon = IconButton(self.root, "close", danger=True)
        icon.set_glyph("close", has_chrome_font=False)
        self.assertEqual(icon.cget("text"), "X")
        icon.destroy()

    def test_glass_button_interactivity(self):
        clicked = []
        btn = GlassButton(self.root, "Glass Test", command=lambda: clicked.append(1))
        self.assertEqual(btn._target_progress, 0.0)
        self.assertEqual(btn._bg_color, BUTTON_DARK_BLUE)

        # Simulate hover event (<Enter>)
        event = tk.Event()
        btn._on_enter(event)
        self.assertEqual(btn._target_progress, 0.5)

        # Simulate press event (<ButtonPress-1>)
        btn._on_press(event)
        self.assertEqual(btn._target_progress, 1.0)

        # Simulate release within bounds
        event.x = 10
        event.y = 10
        btn._on_release(event)
        self.assertEqual(clicked, [1])
        self.assertEqual(btn._target_progress, 0.5)

        # Simulate leave event (<Leave>) -> retracts to 0.0
        btn._on_leave(event)
        self.assertEqual(btn._target_progress, 0.0)

        # Configure state
        btn.config(state="disabled")
        self.assertEqual(btn.cget("state"), "disabled")
        btn.destroy()

    def test_toggle_switch(self):
        state_changes = []
        toggle = ToggleSwitch(
            self.root,
            checked=False,
            command=lambda state: state_changes.append(state),
            animate=False,
        )
        self.assertFalse(toggle.is_checked)

        toggle.set_state(True, invoke_command=True)
        self.assertTrue(toggle.is_checked)
        self.assertEqual(state_changes, [True])

        # Test simulated click
        toggle._on_click()
        self.assertFalse(toggle.is_checked)
        self.assertEqual(state_changes, [True, False])
        toggle.destroy()

    def test_indicators(self):
        orb = StatusOrb(self.root, size=16, animate=False)
        orb.set_state("running")
        self.assertEqual(orb.color, SUCCESS)
        orb.set_state("error")
        self.assertEqual(orb.color, DANGER)
        orb.destroy()

        rec = RecordingIndicator(self.root)
        self.assertIn("REC OFF", rec.label.cget("text"))
        rec.set_recording(True, paused=False)
        self.assertIn("LIVE REC", rec.label.cget("text"))
        rec.set_recording(True, paused=True)
        self.assertIn("REC PAUSED", rec.label.cget("text"))
        rec.destroy()

        badge = EditionBadge(self.root, "MEMBER")
        self.assertEqual(badge.cget("text"), "MEMBER")
        badge.destroy()

        job_badge = JobBadge(self.root, "ready")
        self.assertEqual(job_badge.label.cget("text"), "Ready")
        job_badge.set_state("done")
        self.assertEqual(job_badge.label.cget("text"), "Done")
        job_badge.destroy()

        prog = ProgressBar(self.root, width=200, height=10)
        prog.set_progress(0.75)
        self.assertEqual(prog.progress, 0.75)
        prog.destroy()

        sig = SignalBar(self.root, segments=3)
        sig.set_segment(0, SUCCESS)
        sig.set_all([SUCCESS, WARNING, DANGER])
        sig.destroy()

    def test_cards(self):
        card = ServiceCard(
            self.root,
            "Voice Captions",
            description="Live accessibility captions for Discord.",
            button_label="Stop",
        )
        self.assertIsNotNone(card.button)
        card.set_status("Running", SUCCESS)
        self.assertEqual(card.status_label.cget("text"), "Running")
        action = card.add_action("Move", lambda: None)
        self.assertIsInstance(action, CompactButton)
        card.destroy()

        alert = AlertBannerCard(self.root, "An update is ready.")
        alert.set_message("New version 0.3.8 available.")
        self.assertIn("0.3.8", alert.message_label.cget("text"))
        alert.destroy()

        metric = StatMetricCard(self.root, "Sims Completed", "142")
        metric.set_value("143", "Updated just now")
        self.assertEqual(metric.value_label.cget("text"), "143")
        metric.destroy()

    def test_headers(self):
        titlebar = WindowTitleBar(self.root, "Downtime")
        self.assertEqual(titlebar.title_label.cget("text"), "Downtime")
        titlebar.destroy()

        hero = HeroAudienceHeader(
            self.root,
            DOWNTIME_THEME,
            on_chat_click=lambda: None,
            hotkey_text="Ctrl+Shift+V",
        )
        self.assertEqual(hero.product_name_label.cget("text"), "Downtime")
        self.assertEqual(hero.badge.cget("text"), "LOCAL")
        hero.destroy()

        sec = SectionHeader(self.root, "Services", subtitle="Background workers")
        self.assertEqual(sec.title_label.cget("text"), "Services")
        sec.destroy()

        drawer = DrawerHeader(
            self.root, "Activity", badge_count=5, on_toggle=lambda: None
        )
        drawer.set_expanded(True)
        self.assertEqual(drawer.chevron.cget("text"), "▼")
        drawer.set_count(12)
        self.assertEqual(drawer.badge.cget("text"), "12")
        drawer.destroy()

    def test_tables(self):
        cols = [("Job", 120), ("Status", 80)]
        table = DataTable(self.root, cols)
        row1 = table.add_row(["Great Vault Sim", "Active"], badge_state="checking")
        self.assertEqual(len(table.rows), 1)
        table.clear()
        self.assertEqual(len(table.rows), 0)
        table.destroy()

        empty = EmptyStateView(self.root, "No active jobs", "Run a sim in-game.")
        self.assertEqual(empty.title_label.cget("text"), "No active jobs")
        empty.destroy()

    def test_context_menu(self):
        menu = ContextMenu(self.root)
        invoked = []
        item = menu.add_item(
            "Copy", command=lambda: invoked.append("copy"), accelerator="Ctrl+C"
        )
        menu.add_divider()
        menu.add_header("Options")
        self.assertIsInstance(item, MenuItem)

        item._on_click()
        self.assertEqual(invoked, ["copy"])

        label = tk.Label(self.root, text="Right click me")
        menu.attach_to(label)
        menu.destroy()
        label.destroy()

    def test_inputs(self):
        entry = StyledEntry(self.root, placeholder="Type here...")
        self.assertEqual(entry.get(), "")
        entry.set_text("Hello World")
        self.assertEqual(entry.get(), "Hello World")
        entry.destroy()

        hotkey = HotkeyInput(self.root, current_hotkey="Ctrl+Alt+V")
        self.assertEqual(hotkey.hotkey, "Ctrl+Alt+V")
        hotkey.set_hotkey("Ctrl+Shift+Space")
        self.assertEqual(hotkey.hotkey, "Ctrl+Shift+Space")
        hotkey.destroy()

        scroll = ScrollSurface(self.root)
        self.assertIsNotNone(scroll.content)
        scroll.destroy()

    def test_tab_bar(self):
        chosen = []
        bar = TabBar(
            self.root,
            ("Raid tools", "Vemal's Ears"),
            on_select=chosen.append,
        )
        self.assertEqual(bar.selected, 0)
        self.assertEqual(len(bar.buttons), 2)
        # Clicking the other tab moves the selection and announces it once.
        bar.buttons[1].invoke()
        self.assertEqual(bar.selected, 1)
        self.assertEqual(chosen, [1])
        # Clicking the selected tab again announces nothing.
        bar.buttons[1].invoke()
        self.assertEqual(chosen, [1])
        # select() drives the strip WITHOUT calling back, so a caller can
        # sync the bar to a page change it made itself.
        bar.select(0)
        self.assertEqual(bar.selected, 0)
        self.assertEqual(chosen, [1])
        # The selected tab wears the accent and its rail; the other does not.
        self.assertEqual(bar.buttons[0].cget("fg"), bar.accent)
        self.assertEqual(str(bar.rails[0].cget("bg")), bar.accent)
        self.assertNotEqual(bar.buttons[1].cget("fg"), bar.accent)
        # Arrow keys move between tabs and wrap, and are bound to both.
        self.assertTrue(bar.buttons[0].bind("<Left>"))
        self.assertTrue(bar.buttons[0].bind("<Right>"))
        bar.step(-1)
        self.assertEqual(bar.selected, 1)
        self.assertEqual(chosen, [1, 1])
        bar.set_badge(1, "!")
        self.assertIn("!", bar.buttons[1].cget("text"))
        bar.set_badge(1, None)
        self.assertEqual(bar.buttons[1].cget("text"), "Vemal's Ears")
        with self.assertRaises(ValueError):
            TabBar(self.root, ())
        bar.destroy()


if __name__ == "__main__":
    unittest.main()
