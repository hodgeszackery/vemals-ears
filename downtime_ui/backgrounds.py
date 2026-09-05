"""Background surfaces, elevated card shells, and structural frames for Downtime UI."""

from __future__ import annotations

import tkinter as tk
from downtime_ui.theme import BG, BORDER, BORDER_STRONG, PANEL, PANEL_ALT, SUBTLE


class CardShell(tk.Frame):
    """An elevated card shell featuring a 1px border and optional side-rail indicator.

    Layout structure:
      CardShell (outer border frame, 1px pad)
        -> Inner card container (PANEL_ALT)
          -> (Optional) Accent rail (width=4)
          -> Content frame (packed for callers to populate)
    """

    def __init__(
        self,
        parent: tk.Misc,
        *,
        border_color: str = BORDER,
        background: str = PANEL_ALT,
        has_rail: bool = False,
        rail_color: str = SUBTLE,
        rail_width: int = 4,
        rail_side: str = "left",
        padx: int | tuple[int, int] = 0,
        pady: int | tuple[int, int] = 0,
        **kwargs,
    ) -> None:
        super().__init__(parent, bg=border_color, padx=1, pady=1, **kwargs)
        self.normal_border = border_color
        self.active_border = BORDER_STRONG
        self.background_color = background

        self.card = tk.Frame(self, bg=background)
        self.card.pack(fill="both", expand=True)

        self.rail: tk.Frame | None = None
        if has_rail:
            if rail_side not in {"left", "right"}:
                raise ValueError("rail_side must be 'left' or 'right'")
            self.rail = tk.Frame(self.card, bg=rail_color, width=rail_width)
            self.rail.pack(side=rail_side, fill="y")

        self.content = tk.Frame(self.card, bg=background)
        self.content.pack(side="left", fill="both", expand=True, padx=padx, pady=pady)

    def set_border_highlight(self, active: bool) -> None:
        """Switch between standard and emphasized border styling."""
        self.config(bg=self.active_border if active else self.normal_border)

    def set_rail_color(self, color: str) -> None:
        """Update the side-rail accent indicator color."""
        if self.rail is not None:
            self.rail.config(bg=color)


class PanelSurface(tk.Frame):
    """An inset or elevated panel container for grouping widgets, charts, or logs."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        background: str = PANEL,
        border_color: str = "",
        border_width: int = 0,
        **kwargs,
    ) -> None:
        params = {"bg": background}
        if border_color and border_width > 0:
            params["highlightbackground"] = border_color
            params["highlightthickness"] = border_width
        super().__init__(parent, **params, **kwargs)


class WindowSurface(tk.Frame):
    """The root background container for borderless companion applications."""

    def __init__(self, parent: tk.Misc, *, background: str = BG, **kwargs) -> None:
        super().__init__(parent, bg=background, **kwargs)
        self.pack(fill="both", expand=True)


class HeaderDivider(tk.Frame):
    """A crisp 1px horizontal rule separating functional groups or headers."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        color: str = BORDER,
        height: int = 1,
        pady: int | tuple[int, int] = (8, 8),
        **kwargs,
    ) -> None:
        super().__init__(parent, bg=color, height=height, **kwargs)
        self.pack(fill="x", pady=pady)
