"""Data tables, responsive list views, and empty state cards for Downtime UI."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from downtime_ui.buttons import CompactButton
from downtime_ui.indicators import JobBadge
from downtime_ui.theme import (
    ACCENT,
    BORDER,
    MUTED,
    PANEL,
    PANEL_ALT,
    PANEL_HOVER,
    SUBTLE,
    TEXT,
)


class TableHeader(tk.Frame):
    """Header row defining columns and proportional widths."""

    def __init__(
        self,
        parent: tk.Misc,
        columns: list[tuple[str, int]],  # [(Column Title, pixel width), ...]
        *,
        background: str = PANEL,
        font_scale: dict | None = None,
        **kwargs,
    ) -> None:
        super().__init__(parent, bg=background, **kwargs)
        self.pack(fill="x")
        fonts = font_scale or {}
        header_font = fonts.get("meta", ("Segoe UI", 9, "bold"))

        for title, width in columns:
            col = tk.Label(
                self,
                text=title.upper(),
                bg=background,
                fg=MUTED,
                font=header_font,
                anchor="w",
                width=width if width < 50 else 0,
            )
            if width >= 50:
                col.pack(side="left", padx=8, pady=6)
                col.config(width=width // 8)
            else:
                col.pack(side="left", padx=8, pady=6)


class TableRow(tk.Frame):
    """An interactive table row with alternating background and hover elevation."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        is_alt: bool = False,
        on_click: Callable[[], None] | None = None,
        **kwargs,
    ) -> None:
        self.normal_bg = PANEL_ALT if is_alt else PANEL
        self.hover_bg = PANEL_HOVER
        super().__init__(parent, bg=self.normal_bg, **kwargs)
        self.pack(fill="x", pady=1)

        self.bind("<Enter>", lambda _e: self._set_bg(self.hover_bg))
        self.bind("<Leave>", lambda _e: self._set_bg(self.normal_bg))
        if on_click:
            self.bind("<Button-1>", lambda _e: on_click())
            self.config(cursor="hand2")

    def _set_bg(self, color: str) -> None:
        self.config(bg=color)
        for child in self.winfo_children():
            if isinstance(child, tk.Label):
                child.config(bg=color)
            elif isinstance(child, tk.Frame) and not isinstance(child, JobBadge):
                child.config(bg=color)


class DataTable(tk.Frame):
    """A high-performance dark data table with column headers and alternating rows."""

    def __init__(
        self,
        parent: tk.Misc,
        columns: list[tuple[str, int]],
        *,
        font_scale: dict | None = None,
        **kwargs,
    ) -> None:
        super().__init__(parent, bg=BORDER, padx=1, pady=1, **kwargs)
        self.columns = columns
        self.font_scale = font_scale or {}

        self.container = tk.Frame(self, bg=PANEL)
        self.container.pack(fill="both", expand=True)

        self.header = TableHeader(self.container, columns, font_scale=self.font_scale)
        self.body = tk.Frame(self.container, bg=PANEL)
        self.body.pack(fill="both", expand=True)

        self.rows: list[TableRow] = []

    def clear(self) -> None:
        """Clear all data rows."""
        for row in self.rows:
            row.destroy()
        self.rows.clear()

    def add_row(
        self,
        values: list[str],
        *,
        badge_state: str | None = None,
        on_click: Callable[[], None] | None = None,
    ) -> TableRow:
        """Append a data row with cell values and optional status badge."""
        is_alt = len(self.rows) % 2 == 1
        row = TableRow(self.body, is_alt=is_alt, on_click=on_click)

        fonts = self.font_scale
        cell_font = fonts.get("body", ("Segoe UI", 9))

        for idx, (val, (title, width)) in enumerate(zip(values, self.columns)):
            col_width = width // 8 if width >= 50 else width
            cell = tk.Label(
                row,
                text=str(val),
                bg=row.normal_bg,
                fg=TEXT,
                font=cell_font,
                anchor="w",
                width=col_width,
            )
            cell.pack(side="left", padx=8, pady=5)

        if badge_state:
            badge = JobBadge(row, badge_state, font=fonts.get("meta"))
            badge.pack(side="right", padx=8, pady=5)

        self.rows.append(row)
        return row


class EmptyStateView(tk.Frame):
    """A helpful zero-data placeholder view with an icon, title, and action button."""

    def __init__(
        self,
        parent: tk.Misc,
        title: str,
        message: str = "",
        *,
        button_label: str | None = None,
        button_command: Callable[[], None] | None = None,
        font_scale: dict | None = None,
        **kwargs,
    ) -> None:
        super().__init__(parent, bg=PANEL, padx=24, pady=32, **kwargs)
        self.pack(fill="both", expand=True)

        fonts = font_scale or {}
        heading_font = fonts.get("heading", ("Segoe UI", 12, "bold"))
        meta_font = fonts.get("meta", ("Segoe UI", 9))

        self.icon_label = tk.Label(
            self, text="⚙", bg=PANEL, fg=SUBTLE, font=("Segoe UI", 28)
        )
        self.icon_label.pack()

        self.title_label = tk.Label(
            self, text=title, bg=PANEL, fg=TEXT, font=heading_font
        )
        self.title_label.pack(pady=(8, 4))

        if message:
            self.message_label = tk.Label(
                self,
                text=message,
                bg=PANEL,
                fg=MUTED,
                font=meta_font,
                justify="center",
                wraplength=380,
            )
            self.message_label.pack(pady=(0, 12))

        if button_label and button_command:
            self.button = CompactButton(
                self, button_label, button_command, accent=True
            )
            self.button.pack()
