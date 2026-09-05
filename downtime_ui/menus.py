"""Right-click context menu and dark popup flyouts for Downtime UI."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from downtime_ui.theme import (
    ACCENT,
    BORDER,
    BORDER_STRONG,
    DANGER,
    MUTED,
    PANEL,
    PANEL_ALT,
    PANEL_HOVER,
    SUBTLE,
    TEXT,
)


class MenuItem(tk.Frame):
    """An interactive menu item with icon, label, accelerator hint, and hover state."""

    def __init__(
        self,
        parent: tk.Misc,
        label: str,
        command: Callable[[], None] | None = None,
        *,
        accelerator: str = "",
        icon: str = "",
        danger: bool = False,
        enabled: bool = True,
        on_invoke: Callable[[], None] | None = None,
        font_scale: dict | None = None,
        **kwargs,
    ) -> None:
        super().__init__(parent, bg=PANEL, padx=12, pady=6, cursor="hand2", **kwargs)
        self.pack(fill="x")
        self.command = command
        self.on_invoke = on_invoke
        self.enabled = enabled
        self.danger = danger

        fonts = font_scale or {}
        item_font = fonts.get("body", ("Segoe UI", 9))
        accel_font = fonts.get("keycap", ("Consolas", 8))

        fg_color = DANGER if danger else (TEXT if enabled else SUBTLE)
        self.normal_bg = PANEL
        self.hover_bg = DANGER if danger else PANEL_HOVER

        if icon:
            self.icon_label = tk.Label(
                self, text=icon, bg=PANEL, fg=fg_color, font=item_font, width=2
            )
            self.icon_label.pack(side="left", padx=(0, 6))

        self.label = tk.Label(
            self, text=label, bg=PANEL, fg=fg_color, font=item_font, anchor="w"
        )
        self.label.pack(side="left", fill="x", expand=True)

        if accelerator:
            self.accel = tk.Label(
                self, text=accelerator, bg=PANEL, fg=MUTED, font=accel_font, anchor="e"
            )
            self.accel.pack(side="right", padx=(16, 0))

        if enabled:
            for w in (self, self.label) + ((self.icon_label,) if icon else ()):
                w.bind("<Enter>", self._on_enter)
                w.bind("<Leave>", self._on_leave)
                w.bind("<Button-1>", self._on_click)

    def _on_enter(self, _event=None) -> None:
        self.config(bg=self.hover_bg)
        self.label.config(bg=self.hover_bg)
        if hasattr(self, "icon_label"):
            self.icon_label.config(bg=self.hover_bg)
        if hasattr(self, "accel"):
            self.accel.config(bg=self.hover_bg)

    def _on_leave(self, _event=None) -> None:
        self.config(bg=self.normal_bg)
        self.label.config(bg=self.normal_bg)
        if hasattr(self, "icon_label"):
            self.icon_label.config(bg=self.normal_bg)
        if hasattr(self, "accel"):
            self.accel.config(bg=self.normal_bg)

    def _on_click(self, _event=None) -> None:
        if self.on_invoke:
            self.on_invoke()
        if self.command and self.enabled:
            self.command()


class ContextMenu(tk.Toplevel):
    """A floating, dark right-click context menu with desktop clamping and auto-dismiss."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        font_scale: dict | None = None,
        **kwargs,
    ) -> None:
        super().__init__(parent, **kwargs)
        self.withdraw()
        self.overrideredirect(True)
        self.config(bg=BORDER_STRONG, padx=1, pady=1)
        self.font_scale = font_scale or {}

        # Inner container
        self.inner = tk.Frame(self, bg=PANEL, padx=2, pady=3)
        self.inner.pack(fill="both", expand=True)

        self.bind("<Escape>", lambda _e: self.dismiss())
        self.bind("<FocusOut>", lambda _e: self.dismiss())

    def add_item(
        self,
        label: str,
        command: Callable[[], None] | None = None,
        *,
        accelerator: str = "",
        icon: str = "",
        danger: bool = False,
        enabled: bool = True,
    ) -> MenuItem:
        """Add a clickable menu item."""
        return MenuItem(
            self.inner,
            label,
            command,
            accelerator=accelerator,
            icon=icon,
            danger=danger,
            enabled=enabled,
            on_invoke=self.dismiss,
            font_scale=self.font_scale,
        )

    def add_divider(self) -> tk.Frame:
        """Add a 1px subtle divider between menu item groups."""
        div = tk.Frame(self.inner, bg=BORDER, height=1)
        div.pack(fill="x", padx=6, pady=4)
        return div

    def add_header(self, title: str) -> tk.Label:
        """Add a subdued group section header."""
        fonts = self.font_scale
        header_font = fonts.get("meta", ("Segoe UI", 8, "bold"))
        header = tk.Label(
            self.inner,
            text=title.upper(),
            bg=PANEL,
            fg=MUTED,
            font=header_font,
            anchor="w",
            padx=12,
            pady=3,
        )
        header.pack(fill="x")
        return header

    def post(self, x: int, y: int) -> None:
        """Display the menu at the specified screen coordinate, clamping to work area."""
        self.update_idletasks()
        width = self.winfo_reqwidth()
        height = self.winfo_reqheight()

        # Clamp against screen dimensions
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        target_x = max(0, min(x, screen_w - width - 10))
        target_y = max(0, min(y, screen_h - height - 10))

        self.geometry(f"+{target_x}+{target_y}")
        self.deiconify()
        self.lift()
        self.focus_force()

        # Bind global click outside to dismiss
        root = self.master.winfo_toplevel()
        root.bind("<Button-1>", self._on_root_click, add="+")

    def dismiss(self) -> None:
        """Hide the menu and unbind root listeners."""
        try:
            root = self.master.winfo_toplevel()
            root.unbind("<Button-1>")
        except Exception:
            pass
        self.withdraw()

    def _on_root_click(self, event: tk.Event) -> None:
        """Dismiss if user clicks anywhere outside this menu."""
        try:
            x, y = event.x_root, event.y_root
            left = self.winfo_rootx()
            top = self.winfo_rooty()
            right = left + self.winfo_width()
            bottom = top + self.winfo_height()
            if not (left <= x <= right and top <= y <= bottom):
                self.dismiss()
        except Exception:
            self.dismiss()

    def attach_to(self, widget: tk.Misc) -> None:
        """Convenience helper to attach this menu to a widget's right-click (<Button-3>)."""
        widget.bind("<Button-3>", lambda e: self.post(e.x_root, e.y_root))
