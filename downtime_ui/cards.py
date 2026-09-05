"""Card components, service cards, and alert banners for Downtime UI."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from downtime_ui.backgrounds import CardShell
from downtime_ui.buttons import CompactButton, PrimaryButton
from downtime_ui.indicators import StatusOrb
from downtime_ui.theme import (
    ACCENT,
    BORDER,
    MUTED,
    PANEL,
    PANEL_ALT,
    SUBTLE,
    TEXT,
    WARNING,
)


class ServiceCard(CardShell):
    """A standard service card representing an engine or background worker.

    Includes:
      - Accent side rail
      - Header with StatusOrb, title, status label, and primary toggle button
      - Descriptive note
      - Secondary action buttons row
    """

    def __init__(
        self,
        parent: tk.Misc,
        title: str,
        *,
        description: str = "",
        rail_color: str = SUBTLE,
        accent: str = ACCENT,
        button_label: str | None = None,
        button_command: Callable[[], None] | None = None,
        font_scale: dict | None = None,
        **kwargs,
    ) -> None:
        super().__init__(
            parent,
            border_color=BORDER,
            background=PANEL_ALT,
            has_rail=True,
            rail_color=rail_color,
            padx=16,
            pady=13,
            **kwargs,
        )
        self.accent = accent
        fonts = font_scale or {}
        heading_font = fonts.get("heading", ("Segoe UI", 12, "bold"))
        meta_font = fonts.get("meta", ("Segoe UI", 9))

        # Top row: Status orb + title + status label + control button
        self.top_row = tk.Frame(self.content, bg=PANEL_ALT)
        self.top_row.pack(fill="x")

        self.dot = StatusOrb(self.top_row, background=PANEL_ALT)
        self.dot.pack(side="left", padx=(0, 9))

        self.copy_frame = tk.Frame(self.top_row, bg=PANEL_ALT)
        self.copy_frame.pack(side="left", fill="x", expand=True)

        self.title_label = tk.Label(
            self.copy_frame,
            text=title,
            bg=PANEL_ALT,
            fg=TEXT,
            anchor="w",
            font=heading_font,
        )
        self.title_label.pack(fill="x")

        self.status_label = tk.Label(
            self.copy_frame,
            text="Starting",
            bg=PANEL_ALT,
            fg=MUTED,
            anchor="w",
            font=meta_font,
        )
        self.status_label.pack(fill="x", pady=(2, 0))

        self.button: tk.Button | None = None
        if button_label is not None:
            self.button = CompactButton(
                self.top_row,
                button_label,
                button_command,
                accent=True,
                accent_color=self.accent,
            )
            self.button.pack(side="right", padx=(14, 0))

        # Middle: Description
        self.description_label = tk.Label(
            self.content,
            text=description,
            bg=PANEL_ALT,
            fg=MUTED,
            anchor="w",
            justify="left",
            wraplength=480,
            font=meta_font,
        )
        self.description_label.pack(fill="x", padx=(27, 0), pady=(8, 0))

        # Bottom: Actions row
        self.actions_row = tk.Frame(self.content, bg=PANEL_ALT)
        self.actions_row.pack(fill="x", padx=(27, 0), pady=(11, 0))

    def add_action(
        self,
        label: str,
        command: Callable[[], None],
        *,
        accent: bool = False,
    ) -> CompactButton:
        """Add an action button to the card's actions row."""
        btn = CompactButton(
            self.actions_row,
            label,
            command,
            accent=accent,
            accent_color=self.accent,
        )
        btn.pack(side="left", padx=(0, 7))
        return btn

    def set_status(self, text: str, color: str | None = None) -> None:
        """Update the status text and orb color."""
        self.status_label.config(text=text)
        if color:
            self.dot.set_color(color)
            self.status_label.config(fg=color if color in (WARNING, MUTED) else MUTED)


class AlertBannerCard(CardShell):
    """An attention card for updates, critical pairing notices, or system alerts."""

    def __init__(
        self,
        parent: tk.Misc,
        message: str = "",
        *,
        button_label: str = "Update now",
        button_command: Callable[[], None] | None = None,
        font_scale: dict | None = None,
        **kwargs,
    ) -> None:
        super().__init__(
            parent,
            border_color=BORDER,
            background=PANEL_ALT,
            padx=(14, 12),
            pady=10,
            **kwargs,
        )
        fonts = font_scale or {}
        body_font = fonts.get("body", ("Segoe UI", 10))

        self.message_label = tk.Label(
            self.content,
            text=message,
            bg=PANEL_ALT,
            fg=TEXT,
            anchor="w",
            justify="left",
            wraplength=520,
            font=body_font,
        )
        self.message_label.pack(side="left", fill="x", expand=True, padx=(0, 10))

        self.button = PrimaryButton(
            self.content,
            button_label,
            button_command,
        )
        self.button.pack(side="right")

    def set_message(self, message: str) -> None:
        self.message_label.config(text=message)


class StatMetricCard(CardShell):
    """A card summarizing a key metric, count, or system status."""

    def __init__(
        self,
        parent: tk.Misc,
        label: str,
        value: str,
        *,
        subtext: str = "",
        font_scale: dict | None = None,
        **kwargs,
    ) -> None:
        super().__init__(
            parent,
            border_color=BORDER,
            background=PANEL_ALT,
            padx=14,
            pady=12,
            **kwargs,
        )
        fonts = font_scale or {}
        hero_font = fonts.get("hero", ("Segoe UI", 18, "bold"))
        heading_font = fonts.get("heading", ("Segoe UI", 11, "bold"))
        meta_font = fonts.get("meta", ("Segoe UI", 9))

        self.label = tk.Label(
            self.content,
            text=label.upper(),
            bg=PANEL_ALT,
            fg=MUTED,
            anchor="w",
            font=meta_font,
        )
        self.label.pack(fill="x")

        self.value_label = tk.Label(
            self.content,
            text=value,
            bg=PANEL_ALT,
            fg=TEXT,
            anchor="w",
            font=hero_font,
        )
        self.value_label.pack(fill="x", pady=(4, 2))

        self.subtext_label = tk.Label(
            self.content,
            text=subtext,
            bg=PANEL_ALT,
            fg=MUTED,
            anchor="w",
            font=meta_font,
        )
        self.subtext_label.pack(fill="x")

    def set_value(self, value: str, subtext: str | None = None) -> None:
        self.value_label.config(text=value)
        if subtext is not None:
            self.subtext_label.config(text=subtext)
