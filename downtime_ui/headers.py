"""Header components, window chrome title bars, and audience hero blocks for Downtime UI."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from downtime_ui.backgrounds import CardShell, HeaderDivider
from downtime_ui.buttons import ChatButton, CompactButton, IconButton, SupportButton
from downtime_ui.indicators import EditionBadge, SignalBar
from downtime_ui.theme import (
    ACCENT,
    BG,
    BORDER,
    CLOCKFIRE,
    MUTED,
    PANEL,
    PANEL_ALT,
    TEXT,
    EditionTheme,
)


class WindowTitleBar(tk.Frame):
    """Custom dark title bar for borderless companion application windows."""

    def __init__(
        self,
        parent: tk.Misc,
        title: str,
        *,
        on_close: Callable[[], None] | None = None,
        on_minimize: Callable[[], None] | None = None,
        on_maximize: Callable[[], None] | None = None,
        on_settings: Callable[[], None] | None = None,
        font_scale: dict | None = None,
        chrome_font: tuple | str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(parent, bg=BG, height=32, **kwargs)
        self.pack(fill="x")
        self.pack_propagate(False)

        fonts = font_scale or {}
        title_font = fonts.get("status", ("Segoe UI", 9, "bold"))

        # Left: Optional icon + window title
        self.left_box = tk.Frame(self, bg=BG)
        self.left_box.pack(side="left", padx=(10, 0))

        self.title_label = tk.Label(
            self.left_box,
            text=title,
            bg=BG,
            fg=TEXT,
            font=title_font,
        )
        self.title_label.pack(side="left", padx=(6, 0))

        # Right: Chrome control buttons
        self.controls = tk.Frame(self, bg=BG)
        self.controls.pack(side="right")

        self.btn_settings: IconButton | None = None
        if on_settings:
            self.btn_settings = IconButton(
                self.controls, "settings", on_settings, chrome_font=chrome_font
            )
            self.btn_settings.pack(side="left")

        self.btn_minimize = IconButton(
            self.controls, "minimize", on_minimize, chrome_font=chrome_font
        )
        self.btn_minimize.pack(side="left")

        self.btn_maximize = IconButton(
            self.controls, "maximize", on_maximize, chrome_font=chrome_font
        )
        self.btn_maximize.pack(side="left")

        self.btn_close = IconButton(
            self.controls, "close", on_close, danger=True, chrome_font=chrome_font
        )
        self.btn_close.pack(side="left")


class HeroAudienceHeader(CardShell):
    """The signature hero header tailored to each companion application edition."""

    def __init__(
        self,
        parent: tk.Misc,
        theme: EditionTheme,
        *,
        on_chat_click: Callable[[], None] | None = None,
        hotkey_text: str = "",
        services_count: int = 4,
        font_scale: dict | None = None,
        **kwargs,
    ) -> None:
        super().__init__(
            parent,
            border_color=BORDER,
            background=PANEL,
            padx=18,
            pady=18,
            **kwargs,
        )
        self.theme = theme
        fonts = font_scale or {}
        hero_font = fonts.get("hero", ("Segoe UI", 20, "bold"))
        heading_font = fonts.get("heading", ("Segoe UI", 13, "bold"))
        meta_font = fonts.get("meta", ("Segoe UI", 9))
        keycap_font = fonts.get("keycap", ("Consolas", 9, "bold"))

        # Left: Brand canvas mark
        self.mark = tk.Canvas(
            self.content,
            width=68,
            height=68,
            bg=PANEL,
            highlightthickness=0,
        )
        self.mark.pack(side="left", padx=(0, 16))
        self._draw_default_mark(self.mark, theme.accent)

        # Center: Product name, edition badge, taglines
        self.center_box = tk.Frame(self.content, bg=PANEL)
        self.center_box.pack(side="left", fill="both", expand=True)

        self.identity_row = tk.Frame(self.center_box, bg=PANEL)
        self.identity_row.pack(fill="x")

        self.product_name_label = tk.Label(
            self.identity_row,
            text=theme.name,
            bg=PANEL,
            fg=TEXT,
            font=hero_font,
            anchor="w",
        )
        self.product_name_label.pack(side="left")

        self.badge = EditionBadge(
            self.identity_row,
            theme.badge_label,
            accent=theme.accent,
            background=PANEL_ALT,
            font=meta_font,
        )
        self.badge.pack(side="left", padx=(10, 0))

        self.tagline_label = tk.Label(
            self.center_box,
            text=theme.tagline,
            bg=PANEL,
            fg=TEXT,
            font=heading_font,
            anchor="w",
        )
        self.tagline_label.pack(fill="x", pady=(6, 0))

        self.detail_label = tk.Label(
            self.center_box,
            text=theme.detail,
            bg=PANEL,
            fg=MUTED,
            font=meta_font,
            anchor="w",
            justify="left",
            wraplength=420,
        )
        self.detail_label.pack(fill="x", pady=(2, 0))

        # Right: Quick chat trigger, hotkey, and service signal bars
        self.right_box = tk.Frame(self.content, bg=PANEL)
        self.right_box.pack(side="right", fill="y", padx=(16, 0))

        self.chat_button: ChatButton | None = None
        if on_chat_click is not None:
            self.chat_button = ChatButton(
                self.right_box,
                "Message raid",
                on_chat_click,
                font=fonts.get("button"),
            )
            self.chat_button.pack(fill="x", pady=(0, 4))

        self.hotkey_label = tk.Label(
            self.right_box,
            text=hotkey_text,
            bg=PANEL,
            fg=MUTED,
            anchor="e",
            font=keycap_font,
        )
        self.hotkey_label.pack(fill="x")

        # Bottom-right: Capability signal health meter
        self.signal = SignalBar(
            self.right_box,
            segments=services_count,
            width=160,
            height=6,
            background=PANEL,
        )
        self.signal.pack(side="bottom", anchor="e", pady=(8, 0))

    def _draw_default_mark(self, canvas: tk.Canvas, accent: str) -> None:
        """Draw a fallback clock/signal glyph on the canvas mark."""
        canvas.create_oval(6, 6, 62, 62, outline=accent, width=2)
        canvas.create_line(34, 34, 51, 20, fill=CLOCKFIRE, width=3)
        canvas.create_oval(29, 29, 39, 39, fill=CLOCKFIRE, outline="")


class SectionHeader(tk.Frame):
    """A clean section title bar with optional right-aligned actions."""

    def __init__(
        self,
        parent: tk.Misc,
        title: str,
        *,
        subtitle: str = "",
        font_scale: dict | None = None,
        theme: EditionTheme | None = None,
        **kwargs,
    ) -> None:
        background = theme.bg if theme is not None else BG
        foreground = theme.text if theme is not None else TEXT
        muted = theme.muted if theme is not None else MUTED
        super().__init__(parent, bg=background, **kwargs)
        self.pack(fill="x", pady=(14, 6))

        fonts = font_scale or {}
        heading_font = fonts.get("heading", ("Segoe UI", 12, "bold"))
        meta_font = fonts.get("meta", ("Segoe UI", 9))

        left = tk.Frame(self, bg=background)
        left.pack(side="left", fill="x", expand=True)

        self.title_label = tk.Label(
            left, text=title, bg=background, fg=foreground, font=heading_font, anchor="w"
        )
        self.title_label.pack(fill="x")

        if subtitle:
            self.sub_label = tk.Label(
                left, text=subtitle, bg=background, fg=muted, font=meta_font, anchor="w"
            )
            self.sub_label.pack(fill="x", pady=(1, 0))

        self.actions = tk.Frame(self, bg=background)
        self.actions.pack(side="right")


class DrawerHeader(tk.Frame):
    """An interactive header for collapsible drawers (e.g. Activity Log)."""

    def __init__(
        self,
        parent: tk.Misc,
        title: str,
        *,
        badge_count: int = 0,
        on_toggle: Callable[[], None] | None = None,
        on_clear: Callable[[], None] | None = None,
        font_scale: dict | None = None,
        **kwargs,
    ) -> None:
        super().__init__(parent, bg=PANEL, padx=12, pady=9, **kwargs)
        self.pack(fill="x")
        self.on_toggle = on_toggle

        fonts = font_scale or {}
        heading_font = fonts.get("subheading", ("Segoe UI", 10, "bold"))
        meta_font = fonts.get("meta", ("Segoe UI", 9))

        self.chevron = tk.Label(
            self, text="▶", bg=PANEL, fg=MUTED, font=meta_font, cursor="hand2"
        )
        self.chevron.pack(side="left", padx=(0, 6))

        self.title_label = tk.Label(
            self, text=title, bg=PANEL, fg=TEXT, font=heading_font, cursor="hand2"
        )
        self.title_label.pack(side="left")

        self.badge = tk.Label(
            self,
            text=str(badge_count),
            bg=PANEL_ALT,
            fg=ACCENT,
            font=meta_font,
            padx=6,
            pady=1,
        )
        self.badge.pack(side="left", padx=(8, 0))

        if on_toggle:
            for w in (self, self.chevron, self.title_label, self.badge):
                w.bind("<Button-1>", lambda _e: on_toggle())

        if on_clear:
            self.clear_btn = CompactButton(self, "Clear", on_clear)
            self.clear_btn.pack(side="right")

    def set_expanded(self, expanded: bool) -> None:
        self.chevron.config(text="▼" if expanded else "▶")

    def set_count(self, count: int) -> None:
        self.badge.config(text=str(count))
