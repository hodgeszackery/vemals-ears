"""A tab strip for switching between top-level pages of one window.

The companion grew a second page when the full Downtime edition took on
everything the Vemal's Ears member app does: raid tools on one page,
captions and guild conversation on the other. A tab is the plainest way
to say "two places, one window" without a second window to manage or a
scroll the length of both.

Built from the kit's own vocabulary rather than ttk: a resting tab wears
the compact button's colours, the selected tab wears the accent and a
3px underline - the same rail idiom the service cards use down their
right edge. Keyboard reachable, because everything here is.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Sequence

from .theme import ACCENT, BG, BORDER, MUTED, PANEL, PANEL_HOVER, TEXT

TAB_FONT = ("Segoe UI Semibold", 10)
RAIL_THICKNESS = 3


class TabBar(tk.Frame):
    """A row of page tabs, one selected at a time.

    on_select is called with the index whenever the selection changes by
    click or key - not when select() is called in code, so a caller can
    drive the strip without re-entering its own handler.
    """

    def __init__(
        self,
        parent: tk.Misc,
        tabs: Sequence[str],
        *,
        selected: int = 0,
        on_select: Callable[[int], None] | None = None,
        accent: str = ACCENT,
        background: str = BG,
        font: tuple[str, int] | None = None,
    ) -> None:
        super().__init__(parent, bg=background, highlightthickness=0)
        if not tabs:
            raise ValueError("a tab bar needs at least one tab")
        self.accent = accent
        self.background = background
        self.on_select = on_select
        self.labels: list[str] = list(tabs)
        self.buttons: list[tk.Button] = []
        self.rails: list[tk.Frame] = []
        self.selected = max(0, min(int(selected), len(self.labels) - 1))
        for index, label in enumerate(self.labels):
            column = tk.Frame(self, bg=background, highlightthickness=0)
            column.pack(side="left", padx=(0, 2))
            button = tk.Button(
                column,
                text=label,
                bd=0,
                relief="flat",
                cursor="hand2",
                padx=16,
                pady=7,
                font=font or TAB_FONT,
                activeforeground=TEXT,
                activebackground=PANEL_HOVER,
                highlightthickness=1,
                highlightbackground=background,
                highlightcolor=accent,
                takefocus=1,
                command=lambda position=index: self._choose(position),
            )
            button.pack(fill="x")
            rail = tk.Frame(column, bg=background, height=RAIL_THICKNESS)
            rail.pack(fill="x")
            rail.pack_propagate(False)
            button.bind("<Enter>", lambda _e, position=index: self._hover(position, True))
            button.bind("<Leave>", lambda _e, position=index: self._hover(position, False))
            button.bind("<Left>", lambda _e: self.step(-1))
            button.bind("<Right>", lambda _e: self.step(1))
            self.buttons.append(button)
            self.rails.append(rail)
        self._paint()

    # -- state ---------------------------------------------------------
    def select(self, index: int) -> None:
        """Move the selection without announcing it back to the caller."""
        index = max(0, min(int(index), len(self.labels) - 1))
        if index == self.selected:
            self._paint()
            return
        self.selected = index
        self._paint()

    def _choose(self, index: int) -> None:
        if index == self.selected:
            return
        self.select(index)
        if self.on_select is not None:
            self.on_select(index)

    def step(self, delta: int) -> str:
        """Move the selection by delta, wrapping - the arrow-key path."""
        self._choose((self.selected + delta) % len(self.labels))
        try:
            self.buttons[self.selected].focus_set()
        except tk.TclError:
            pass
        return "break"

    # -- paint ---------------------------------------------------------
    def _hover(self, index: int, entering: bool) -> None:
        if index == self.selected:
            return
        button = self.buttons[index]
        try:
            button.configure(
                bg=PANEL_HOVER if entering else PANEL,
                fg=TEXT if entering else MUTED,
            )
        except tk.TclError:
            pass

    def _paint(self) -> None:
        for index, button in enumerate(self.buttons):
            chosen = index == self.selected
            try:
                button.configure(
                    bg=PANEL_HOVER if chosen else PANEL,
                    fg=self.accent if chosen else MUTED,
                    highlightbackground=self.accent if chosen else BORDER,
                )
                self.rails[index].configure(bg=self.accent if chosen else self.background)
            except tk.TclError:
                pass

    def set_badge(self, index: int, text: str | None) -> None:
        """Append a short marker to a tab label, or clear it.

        Used for a page that wants attention while it is not the visible
        one - captions disconnected, an update waiting.
        """
        index = max(0, min(int(index), len(self.labels) - 1))
        label = self.labels[index]
        try:
            self.buttons[index].configure(text=f"{label}  {text}" if text else label)
        except tk.TclError:
            pass
