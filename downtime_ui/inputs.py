"""Inputs, text entries, hotkey recorders, and scrollable surfaces for Downtime UI."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from downtime_ui.buttons import CompactButton
from downtime_ui.theme import (
    ACCENT,
    BG,
    BORDER,
    BORDER_STRONG,
    MUTED,
    PANEL,
    PANEL_ALT,
    PANEL_HOVER,
    SUBTLE,
    TEXT,
)


class StyledEntry(tk.Frame):
    """A dark, modern text entry with an accent focus border and placeholder support."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        placeholder: str = "",
        width: int = 24,
        font_scale: dict | None = None,
        on_change: Callable[[str], None] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(parent, bg=BORDER, padx=1, pady=1, **kwargs)
        self.placeholder = placeholder
        self.on_change = on_change
        fonts = font_scale or {}
        entry_font = fonts.get("body", ("Segoe UI", 10))

        self.inner = tk.Frame(self, bg=PANEL, padx=8, pady=5)
        self.inner.pack(fill="both", expand=True)

        self.entry = tk.Entry(
            self.inner,
            bg=PANEL,
            fg=TEXT,
            insertbackground=ACCENT,
            selectbackground=BORDER_STRONG,
            selectforeground=TEXT,
            relief="flat",
            borderwidth=0,
            width=width,
            font=entry_font,
        )
        self.entry.pack(fill="both", expand=True)

        self._is_placeholder = False
        if placeholder:
            self._show_placeholder()

        self.entry.bind("<FocusIn>", self._on_focus_in)
        self.entry.bind("<FocusOut>", self._on_focus_out)
        self.entry.bind("<KeyRelease>", self._on_key_release)

    def _show_placeholder(self) -> None:
        if not self.entry.get():
            self._is_placeholder = True
            self.entry.insert(0, self.placeholder)
            self.entry.config(fg=SUBTLE)

    def _hide_placeholder(self) -> None:
        if self._is_placeholder:
            self.entry.delete(0, "end")
            self.entry.config(fg=TEXT)
            self._is_placeholder = False

    def _on_focus_in(self, _event=None) -> None:
        self.config(bg=ACCENT)
        self._hide_placeholder()

    def _on_focus_out(self, _event=None) -> None:
        self.config(bg=BORDER)
        if not self.entry.get():
            self._show_placeholder()

    def _on_key_release(self, _event=None) -> None:
        if self.on_change and not self._is_placeholder:
            self.on_change(self.entry.get())

    def get(self) -> str:
        if self._is_placeholder:
            return ""
        return self.entry.get()

    def set_text(self, text: str) -> None:
        self._hide_placeholder()
        self.entry.delete(0, "end")
        self.entry.insert(0, text)
        if not text and self.placeholder:
            self._show_placeholder()

    def focus_set(self) -> None:
        self.entry.focus_set()


class HotkeyInput(tk.Frame):
    """An interactive shortcut capture input with styled keycap badges."""

    def __init__(
        self,
        parent: tk.Misc,
        current_hotkey: str = "Ctrl+Shift+Space",
        on_hotkey_changed: Callable[[str], None] | None = None,
        *,
        font_scale: dict | None = None,
        **kwargs,
    ) -> None:
        super().__init__(parent, bg=BORDER, padx=1, pady=1, **kwargs)
        self.hotkey = current_hotkey
        self.on_hotkey_changed = on_hotkey_changed
        self.recording = False

        fonts = font_scale or {}
        key_font = fonts.get("keycap", ("Consolas", 10, "bold"))
        meta_font = fonts.get("meta", ("Segoe UI", 9))

        self.inner = tk.Frame(self, bg=PANEL, padx=10, pady=5, cursor="hand2")
        self.inner.pack(fill="both", expand=True)

        self.keycap = tk.Label(
            self.inner,
            text=self.hotkey,
            bg=PANEL_ALT,
            fg=ACCENT,
            font=key_font,
            padx=8,
            pady=3,
        )
        self.keycap.pack(side="left")

        self.hint = tk.Label(
            self.inner,
            text="Click to record",
            bg=PANEL,
            fg=MUTED,
            font=meta_font,
        )
        self.hint.pack(side="right", padx=(10, 0))

        for w in (self, self.inner, self.keycap, self.hint):
            w.bind("<Button-1>", self._start_recording)

    def _start_recording(self, _event=None) -> None:
        self.recording = True
        self.config(bg=ACCENT)
        self.keycap.config(text="Press keys...", fg=TEXT, bg=BORDER_STRONG)
        self.hint.config(text="Esc to cancel")
        self.focus_set()
        self.bind("<Key>", self._on_key)

    def _on_key(self, event: tk.Event) -> str:
        if not self.recording:
            return ""

        key = event.keysym
        if key == "Escape":
            self._cancel_recording()
            return "break"

        modifiers = []
        state = event.state
        if state & 0x0004:  # Control
            modifiers.append("Ctrl")
        if state & 0x0001:  # Shift
            modifiers.append("Shift")
        if state & 0x20000 or state & 0x0008:  # Alt
            modifiers.append("Alt")

        if key in ("Control_L", "Control_R", "Shift_L", "Shift_R", "Alt_L", "Alt_R"):
            return "break"

        parts = modifiers + [key.capitalize()]
        new_hotkey = "+".join(parts)
        self.set_hotkey(new_hotkey)
        self.unbind("<Key>")
        self.recording = False
        self.config(bg=BORDER)
        if self.on_hotkey_changed:
            self.on_hotkey_changed(new_hotkey)
        return "break"

    def _cancel_recording(self) -> None:
        self.recording = False
        self.config(bg=BORDER)
        self.set_hotkey(self.hotkey)
        self.unbind("<Key>")

    def set_hotkey(self, hotkey: str) -> None:
        self.hotkey = hotkey
        self.keycap.config(text=hotkey, fg=ACCENT, bg=PANEL_ALT)
        self.hint.config(text="Click to record")


class ScrollSurface(tk.Frame):
    """A smooth, dark scrollable container frame with a subtle scrollbar."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        background: str = BG,
        **kwargs,
    ) -> None:
        super().__init__(parent, bg=background, **kwargs)

        self.canvas = tk.Canvas(
            self,
            bg=background,
            borderwidth=0,
            highlightthickness=0,
        )
        self.scrollbar = tk.Scrollbar(
            self,
            orient="vertical",
            command=self.canvas.yview,
            bg=PANEL,
            troughcolor=BG,
            activebackground=PANEL_HOVER,
            relief="flat",
            width=8,
        )
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.content = tk.Frame(self.canvas, bg=background)
        self.canvas_window = self.canvas.create_window(
            (0, 0), window=self.content, anchor="nw"
        )

        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        self.content.bind(
            "<Configure>",
            lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas.bind(
            "<Configure>",
            lambda e: self.canvas.itemconfig(self.canvas_window, width=e.width),
        )

        # Mouse wheel support
        self.bind_all("<MouseWheel>", self._on_mousewheel, add="+")

    def _on_mousewheel(self, event: tk.Event) -> None:
        try:
            if self.winfo_ismapped():
                self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        except Exception:
            pass
