"""Status indicators, live badges, pulse orbs, and progress meters for Downtime UI."""

from __future__ import annotations

import time
import tkinter as tk
from downtime_ui.effects import blend_colors, rounded_rectangle
from downtime_ui.theme import (
    ACCENT,
    BORDER,
    DANGER,
    MUTED,
    PANEL,
    PANEL_ALT,
    SUBTLE,
    SUCCESS,
    TEXT,
    WARNING,
)

JOB_BADGE_STYLES = {
    "ready": ("Ready", ACCENT),
    "queued": ("Queued", "#78a7ff"),
    "checking": ("Checking", WARNING),
    "done": ("Done", SUCCESS),
    "stopped": ("Stopped", MUTED),
    "attention": ("Needs attention", DANGER),
}


class StatusOrb(tk.Canvas):
    """A high-visibility canvas-drawn status orb with a soft radial halo."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        size: int = 18,
        color: str = WARNING,
        background: str = PANEL_ALT,
        animate: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(
            parent,
            width=size,
            height=size,
            bg=background,
            highlightthickness=0,
            **kwargs,
        )
        self.size = size
        self.color = color
        self.background_color = background
        self.animate = animate
        self._pulse_timer: str | None = None
        self._pulse_phase = 0.0

        # Elements: outer ambient glow and inner core dot
        mid = size / 2
        self.halo = self.create_oval(
            mid - 7, mid - 7, mid + 7, mid + 7,
            fill="", outline=color, width=1
        )
        self.core = self.create_oval(
            mid - 4, mid - 4, mid + 4, mid + 4,
            fill=color, outline=""
        )

        if self.animate:
            self._start_pulse()

    def set_color(self, color: str) -> None:
        """Update the orb color."""
        self.color = color
        self.itemconfig(self.core, fill=color)
        self.itemconfig(self.halo, outline=color)

    def show(self, color: str) -> None:
        """Compatibility alias for set_color."""
        self.set_color(color)

    def set_state(self, state: str) -> None:
        """Update orb color from a standard state string."""
        mapping = {
            "running": SUCCESS,
            "connected": SUCCESS,
            "starting": WARNING,
            "checking": WARNING,
            "paused": ACCENT,
            "stopped": MUTED,
            "error": DANGER,
            "failed": DANGER,
        }
        self.set_color(mapping.get(state.lower(), MUTED))

    def _start_pulse(self) -> None:
        if self._pulse_timer:
            self.after_cancel(self._pulse_timer)
        self._step_pulse()

    def _step_pulse(self) -> None:
        self._pulse_phase = (self._pulse_phase + 0.08) % (2 * 3.14159)
        # Pulse alpha/spread when active or in error
        if self.color in (SUCCESS, DANGER, WARNING):
            mid = self.size / 2
            spread = 6 + math_sin_positive(self._pulse_phase) * 2.5
            self.coords(self.halo, mid - spread, mid - spread, mid + spread, mid + spread)
        self._pulse_timer = self.after(50, self._step_pulse)

    def destroy(self) -> None:
        if self._pulse_timer:
            try:
                self.after_cancel(self._pulse_timer)
            except Exception:
                pass
            self._pulse_timer = None
        super().destroy()


def math_sin_positive(x: float) -> float:
    import math
    return (math.sin(x) + 1.0) / 2.0


class RecordingIndicator(tk.Frame):
    """An unmistakable privacy badge indicating when voice audio is being captured."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        background: str = PANEL,
        font: tuple | str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(parent, bg=background, **kwargs)
        self.active = False
        self.badge_font = font or ("Segoe UI", 9, "bold")

        # Container pill
        self.shell = tk.Frame(
            self,
            bg=BORDER,
            padx=1,
            pady=1,
        )
        self.shell.pack(side="left")

        self.pill = tk.Frame(
            self.shell,
            bg=PANEL_ALT,
            padx=8,
            pady=3,
        )
        self.pill.pack(fill="both")

        self.dot = tk.Canvas(
            self.pill,
            width=8,
            height=8,
            bg=PANEL_ALT,
            highlightthickness=0,
        )
        self.dot.pack(side="left", padx=(0, 6))
        self.dot_core = self.dot.create_oval(1, 1, 7, 7, fill=MUTED, outline="")

        self.label = tk.Label(
            self.pill,
            text="REC OFF",
            bg=PANEL_ALT,
            fg=MUTED,
            font=self.badge_font,
        )
        self.label.pack(side="left")

    def set_recording(self, active: bool, paused: bool = False) -> None:
        """Update recording status."""
        self.active = active
        if paused:
            self.dot.itemconfig(self.dot_core, fill=WARNING)
            self.label.config(text="REC PAUSED", fg=WARNING)
            self.shell.config(bg=WARNING)
        elif active:
            self.dot.itemconfig(self.dot_core, fill=DANGER)
            self.label.config(text="LIVE REC", fg=DANGER)
            self.shell.config(bg=DANGER)
        else:
            self.dot.itemconfig(self.dot_core, fill=MUTED)
            self.label.config(text="REC OFF", fg=MUTED)
            self.shell.config(bg=BORDER)


class EditionBadge(tk.Label):
    """A clean pill badge identifying the companion application tier."""

    def __init__(
        self,
        parent: tk.Misc,
        edition_label: str = "LOCAL",
        *,
        accent: str = ACCENT,
        background: str = PANEL_ALT,
        font: tuple | str | None = None,
        **kwargs,
    ) -> None:
        badge_font = font or ("Segoe UI", 9, "bold")
        super().__init__(
            parent,
            text=edition_label,
            bg=background,
            fg=accent,
            padx=8,
            pady=3,
            font=badge_font,
            **kwargs,
        )


class JobBadge(tk.Frame):
    """Status pill for background and hosted simulations."""

    def __init__(
        self,
        parent: tk.Misc,
        state: str = "ready",
        *,
        font: tuple | str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(parent, bg=BORDER, padx=1, pady=1, **kwargs)
        self.badge_font = font or ("Segoe UI", 9, "bold")
        self.inner = tk.Frame(self, bg=PANEL_ALT, padx=7, pady=2)
        self.inner.pack(fill="both")
        self.label = tk.Label(self.inner, bg=PANEL_ALT, font=self.badge_font)
        self.label.pack()
        self.set_state(state)

    def set_state(self, state: str) -> None:
        label_text, color = JOB_BADGE_STYLES.get(state.lower(), (state.capitalize(), MUTED))
        self.config(bg=color)
        self.label.config(text=label_text, fg=color)


class ProgressBar(tk.Canvas):
    """Dual-tone progress bar with smooth fill and optional percentage label."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        width: int = 240,
        height: int = 14,
        accent: str = ACCENT,
        background: str = PANEL,
        **kwargs,
    ) -> None:
        super().__init__(
            parent,
            width=width,
            height=height,
            bg=background,
            highlightthickness=0,
            **kwargs,
        )
        self.width = width
        self.height = height
        self.accent = accent
        self.background_color = background
        self.progress = 0.0

        # Background track
        rounded_rectangle(self, 0, 0, width, height, radius=height // 2, fill=BORDER)
        self.fill_tag = "fill_rect"

    def set_progress(self, progress: float) -> None:
        """Set progress from 0.0 to 1.0."""
        self.progress = max(0.0, min(1.0, float(progress)))
        self.delete(self.fill_tag)
        fill_width = int(self.width * self.progress)
        if fill_width > 4:
            rounded_rectangle(
                self,
                0,
                0,
                fill_width,
                self.height,
                radius=self.height // 2,
                fill=self.accent,
                tags=self.fill_tag,
            )


class SignalBar(tk.Canvas):
    """Segmented horizontal health/connectivity meter."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        segments: int = 4,
        width: int = 160,
        height: int = 6,
        gap: int = 4,
        background: str = PANEL,
        **kwargs,
    ) -> None:
        super().__init__(
            parent,
            width=width,
            height=height,
            bg=background,
            highlightthickness=0,
            **kwargs,
        )
        self.segments = max(1, segments)
        self.width = width
        self.height = height
        self.gap = gap
        self.items: list[int] = []

        total_gaps = gap * (self.segments - 1)
        seg_width = (width - total_gaps) / self.segments
        for i in range(self.segments):
            x1 = i * (seg_width + gap)
            item = self.create_rectangle(
                x1, 0, x1 + seg_width, height, fill=SUBTLE, outline=""
            )
            self.items.append(item)

    def set_segment(self, index: int, color: str) -> None:
        """Set the color of a specific segment."""
        if 0 <= index < len(self.items):
            self.itemconfig(self.items[index], fill=color)

    def set_all(self, colors: list[str]) -> None:
        """Update all segment colors simultaneously."""
        for index, color in enumerate(colors):
            if index < len(self.items):
                self.itemconfig(self.items[index], fill=color)
