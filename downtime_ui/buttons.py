"""Button suite and interactive switches for Downtime UI."""

from __future__ import annotations

import time
import tkinter as tk
from tkinter import font as tkfont
from collections.abc import Callable
from downtime_ui.effects import blend_colors, rounded_rectangle
from downtime_ui.theme import (
    ACCENT,
    ACCENT_HOVER,
    BG,
    BORDER,
    BORDER_STRONG,
    BUTTON_DARK_BLUE,
    BUTTON_DARK_BLUE_ACTIVE,
    BUTTON_DARK_BLUE_BORDER,
    BUTTON_DARK_BLUE_HOVER,
    BUTTON_SHINE,
    CLOCKFIRE,
    CLOCKFIRE_HOVER,
    DANGER,
    MUTED,
    PANEL,
    PANEL_ALT,
    PANEL_HOVER,
    PATREON_CORAL,
    PAYPAL_BLUE,
    SUBTLE,
    TEXT,
    EditionTheme,
)

GLYPHS = {
    "settings": ("\ue713", "*"),
    "minimize": ("\ue921", "-"),
    "maximize": ("\ue922", "[ ]"),
    "restore": ("\ue923", "[+]"),
    "close": ("\ue8bb", "X"),
}


class VelvetButton(tk.Button):
    """Rounded, quiet voice-app controls with native Tk text and activation.

    Only the surface and optional icon are rasterized. The label, disabled
    state, command, focus traversal and invoke API remain a native Button.
    Images are generated in memory and retained by this widget; no artwork
    files, animation loop or shared mutable palette are involved.
    """

    VARIANTS = frozenset(("secondary", "primary", "conversation", "quiet"))
    ICONS = frozenset((
        "play", "pause", "message", "chat", "link", "move", "shield", "arrow",
        "close", "check", "settings",
    ))

    def __init__(
        self,
        parent: tk.Misc,
        text: str,
        command: Callable[[], None] | None = None,
        *,
        theme: EditionTheme,
        variant: str = "secondary",
        compact: bool = False,
        font: tuple | str | None = None,
        text_color: str | None = None,
        icon: str | None = None,
        **kwargs,
    ) -> None:
        self._velvet_ready = False
        self._validate_choice(variant, icon)
        self.theme = theme
        self.variant = variant
        self.compact = bool(compact)
        self.icon = icon
        self.is_accent = variant == "primary"
        self._hovered = False
        self._pressed = False
        self._mouse_down = False
        self._key_down: str | None = None
        self._focused = False
        self._configured_active = kwargs.get("state") == "active"
        self._surface_image = None
        self._surface_size = (0, 0)
        self._allocated_size = (0, 0)
        self._natural_size = (0, 0)
        self._text_trace = None
        self._text_variable = None
        self._render_key = None
        self._padx = kwargs.pop("padx", 13 if compact else 20)
        self._pady = kwargs.pop("pady", 6 if compact else 10)
        self._fill_override = kwargs.pop("bg", kwargs.pop("background", None))
        self._hover_override = kwargs.pop("activebackground", None)
        self._foreground_override = kwargs.pop("fg", kwargs.pop("foreground", text_color))
        self._active_foreground_override = kwargs.pop("activeforeground", None)
        self._border_override = kwargs.pop("highlightbackground", None)
        self._focus_override = kwargs.pop("highlightcolor", None)
        self._disabled_foreground = kwargs.pop("disabledforeground", theme.muted)
        self._takefocus = kwargs.pop("takefocus", True)
        self._minimum_width = kwargs.pop("width", 0)
        self._minimum_height = kwargs.pop("height", 0)
        self._user_cursor = kwargs.pop("cursor", "hand2")
        for option in ("image", "compound", "relief", "overrelief", "borderwidth", "bd", "highlightthickness"):
            kwargs.pop(option, None)
        backdrop = self._backdrop(parent, theme.bg)
        super().__init__(
            parent,
            text=text,
            command=command,
            font=font or ("Segoe UI", 9 if compact else 10, "bold"),
            bg=backdrop,
            activebackground=backdrop,
            compound="center",
            relief="flat",
            overrelief="flat",
            borderwidth=0,
            highlightthickness=0,
            padx=0,
            pady=0,
            takefocus=self._takefocus,
            cursor=self._user_cursor,
            **kwargs,
        )
        self._velvet_ready = True
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<FocusIn>", lambda _e: self._set_focus(True))
        self.bind("<FocusOut>", lambda _e: self._set_focus(False))
        self.bind("<Configure>", self._on_resize)
        for key in ("Return", "space"):
            self.bind("<KeyPress-" + key + ">", self._on_key_press)
            self.bind("<KeyRelease-" + key + ">", self._on_key_release)
        self._sync_textvariable()
        self._render()

    @classmethod
    def _validate_choice(cls, variant, icon) -> None:
        if variant not in cls.VARIANTS:
            raise ValueError("unknown VelvetButton variant")
        if icon is not None and icon not in cls.ICONS:
            raise ValueError("unknown VelvetButton icon")

    @staticmethod
    def _backdrop(parent, fallback):
        try:
            return parent.cget("background")
        except (AttributeError, tk.TclError):
            return fallback

    def _enabled(self) -> bool:
        return str(tk.Button.cget(self, "state")) != "disabled"

    def _on_enter(self, _event=None) -> None:
        self._hovered = True
        self._pressed = self._mouse_down and self._enabled()
        self._render()

    def _on_leave(self, _event=None) -> None:
        self._hovered = False
        self._pressed = self._key_down is not None
        self._render()

    def _on_press(self, _event=None) -> None:
        if self._enabled():
            self.focus_set()
            self._mouse_down = True
            self._hovered = True
            self._pressed = True
            self._render()

    def _on_release(self, _event=None) -> None:
        self._mouse_down = False
        self._pressed = False
        self._render()

    def _on_key_press(self, event) -> str:
        if self._enabled() and self._key_down is None:
            self._key_down = event.keysym
            self._pressed = True
            self._render()
        return "break"

    def _on_key_release(self, event) -> str:
        accepted = self._key_down == event.keysym and self._enabled()
        self._key_down = None
        self._pressed = False
        self._render()
        if accepted:
            self.invoke()
        return "break"

    def _set_focus(self, focused: bool) -> None:
        self._focused = bool(focused)
        if not focused:
            self._key_down = None
            self._mouse_down = False
            self._pressed = False
        self._render()

    def _on_resize(self, event) -> None:
        size = (int(event.width), int(event.height))
        if size != self._allocated_size:
            self._allocated_size = size
            self._render()

    def _colors(self):
        theme = self.theme
        if self.variant == "primary":
            fill, hover, foreground, border = theme.accent, theme.accent_hover, theme.bg, theme.accent
        elif self.variant == "conversation":
            fill, hover, foreground, border = theme.conversation, theme.conversation_hover, theme.conversation_text, theme.conversation
        elif self.variant == "quiet":
            fill = self._backdrop(self.master, theme.bg)
            hover, foreground, border = theme.panel_hover, theme.muted, theme.border
        else:
            fill, hover, foreground, border = theme.panel_alt, theme.panel_hover, theme.text, theme.border_strong
        fill = self._fill_override or fill
        hover = self._hover_override or hover
        foreground = self._foreground_override or foreground
        border = self._border_override or border
        if not self._enabled():
            return theme.panel_alt, self._disabled_foreground, theme.border
        if self._pressed:
            fill = blend_colors(self._hex_color(hover), self._hex_color(theme.bg), 0.16)
        elif self._hovered or self._configured_active:
            fill = hover
        if self._hovered and self._active_foreground_override:
            foreground = self._active_foreground_override
        return fill, foreground, border

    def _hex_color(self, color) -> str:
        return "#%02x%02x%02x" % tuple(channel // 257 for channel in self.winfo_rgb(color))

    def _label(self) -> str:
        variable = str(tk.Button.cget(self, "textvariable"))
        if variable:
            try:
                return str(self.getvar(variable))
            except tk.TclError:
                pass
        return str(tk.Button.cget(self, "text"))

    def _sync_textvariable(self) -> None:
        variable = str(tk.Button.cget(self, "textvariable"))
        if variable == self._text_variable:
            return
        self._remove_text_trace()
        self._text_variable = variable
        if variable:
            self._text_trace = self.register(lambda *_args: self._render())
            self.tk.call("trace", "add", "variable", variable, "write", self._text_trace)

    def _remove_text_trace(self) -> None:
        if self._text_variable and self._text_trace:
            try:
                self.tk.call("trace", "remove", "variable", self._text_variable, "write", self._text_trace)
                self.deletecommand(self._text_trace)
            except tk.TclError:
                pass
        self._text_trace = None

    def _draw_icon(self, draw, box, color, scale) -> None:
        if self.icon is None:
            return
        x, y, size = box
        def point(px, py):
            return (round((x + px * size / 16) * scale), round((y + py * size / 16) * scale))
        width = max(1, round(1.6 * size / 16 * scale))
        def line(points):
            draw.line([point(*p) for p in points], fill=color, width=width, joint="curve")
        if self.icon == "play":
            draw.polygon([point(5, 3), point(13, 8), point(5, 13)], fill=color)
        elif self.icon == "pause":
            for left in (4, 10):
                draw.rounded_rectangle([point(left, 3), point(left + 2, 13)], radius=scale, fill=color)
        elif self.icon in ("message", "chat"):
            line([(2, 3), (14, 3), (14, 11), (7, 11), (3, 14), (3, 11), (2, 11), (2, 3)])
        elif self.icon == "link":
            line([(7, 4), (10, 1), (14, 1), (15, 5), (12, 8)])
            line([(9, 12), (6, 15), (2, 15), (1, 11), (4, 8)])
            line([(5, 11), (11, 5)])
        elif self.icon == "move":
            line([(8, 1), (8, 15)])
            line([(1, 8), (15, 8)])
            for points in ([(5, 4), (8, 1), (11, 4)], [(5, 12), (8, 15), (11, 12)],
                           [(4, 5), (1, 8), (4, 11)], [(12, 5), (15, 8), (12, 11)]):
                line(points)
        elif self.icon == "shield":
            line([(8, 1), (14, 4), (13, 10), (8, 15), (3, 10), (2, 4), (8, 1)])
            line([(5, 8), (7, 10), (11, 6)])
        elif self.icon == "arrow":
            line([(2, 8), (14, 8)])
            line([(9, 3), (14, 8), (9, 13)])
        elif self.icon == "close":
            line([(4, 4), (12, 12)])
            line([(12, 4), (4, 12)])
        elif self.icon == "check":
            line([(3, 8), (7, 12), (14, 4)])
        elif self.icon == "settings":
            for yy, knob in ((4, 6), (12, 10)):
                line([(2, yy), (14, yy)])
                draw.ellipse([point(knob - 2, yy - 2), point(knob + 2, yy + 2)], fill=color)

    def _render(self) -> None:
        if not self._velvet_ready:
            return
        from PIL import Image, ImageDraw, ImageTk

        fill, foreground, border = self._colors()
        backdrop = self._backdrop(self.master, self.theme.bg)
        font = tkfont.Font(root=self, font=tk.Button.cget(self, "font"))
        scale = max(1.0, min(float(self.tk.call("tk", "scaling")) / (96 / 72), 3.0))
        label_lines = self._label().split("\n")
        text_width = max((font.measure(line) for line in label_lines), default=0)
        text_height = font.metrics("linespace") * max(1, len(label_lines))
        pad_x = self.winfo_pixels(self._padx) * scale
        pad_y = self.winfo_pixels(self._pady) * scale
        icon_size = max(12, round(14 * scale)) if self.icon else 0
        icon_gap = round(7 * scale) if self.icon else 0
        natural = (
            max(round(54 * scale), round(text_width + 2 * pad_x + 2 * (icon_size + icon_gap) + 8), int(self._minimum_width)),
            max(round((30 if self.compact else 38) * scale), round(text_height + 2 * pad_y + 8), int(self._minimum_height)),
        )
        width, height = (
            max(natural[0], self._allocated_size[0]),
            max(natural[1], self._allocated_size[1]),
        )
        focused = self._focused and self._enabled()
        focus_color = self._focus_override or self.theme.accent
        key = (width, height, natural, fill, foreground, border, backdrop,
               focused, focus_color, self._pressed, self.variant, self.icon, text_width, icon_size)
        if key == self._render_key:
            return
        self._render_key = key
        self._natural_size = natural
        self._current_fill = fill
        self._surface_size = (width, height)
        sample = 3
        def raster_color(color):
            # Tk accepts Windows system colors that Pillow cannot parse.
            return tuple(channel // 257 for channel in self.winfo_rgb(color))

        image = Image.new("RGB", (width * sample, height * sample), raster_color(backdrop))
        draw = ImageDraw.Draw(image)
        radius = height / 2 - 4 if self.variant in ("primary", "conversation") else min(13 * scale, height / 2 - 4)
        if focused:
            draw.rounded_rectangle(
                (sample, sample, (width - 1) * sample, (height - 1) * sample),
                radius=(radius + 3) * sample,
                outline=raster_color(focus_color),
                width=2 * sample,
            )
        offset = 1 if self._pressed and self._enabled() else 0
        draw.rounded_rectangle(
            (4 * sample, (4 + offset) * sample, (width - 4) * sample, (height - 4) * sample),
            radius=radius * sample,
            fill=raster_color(fill),
            outline=raster_color(border),
            width=sample,
        )
        if self.icon:
            self._draw_icon(
                draw,
                (width / 2 - text_width / 2 - icon_gap - icon_size, (height - icon_size) / 2 + offset, icon_size),
                raster_color(foreground),
                sample,
            )
        image = image.resize((width, height), Image.Resampling.LANCZOS)
        self._surface_image = ImageTk.PhotoImage(image, master=self)
        tk.Button.configure(
            self,
            image=self._surface_image,
            width=natural[0],
            height=natural[1],
            bg=backdrop,
            activebackground=backdrop,
            fg=foreground,
            activeforeground=foreground,
            disabledforeground=self._disabled_foreground,
            relief="sunken" if self._pressed else "flat",
            cursor=self._user_cursor if self._enabled() else "",
            takefocus=self._takefocus if self._enabled() else 0,
        )

    def configure(self, cnf=None, **kwargs):
        if not getattr(self, "_velvet_ready", False):
            return tk.Button.configure(self, cnf, **kwargs)
        if isinstance(cnf, str) or (cnf is None and not kwargs):
            return tk.Button.configure(self, cnf, **kwargs)
        options = dict(cnf or {})
        options.update(kwargs)
        if "state" in options:
            self._configured_active = str(options["state"]) == "active"
        variant = options.pop("variant", self.variant)
        icon = options.pop("icon", self.icon)
        self._validate_choice(variant, icon)
        self.variant, self.icon = variant, icon
        self.is_accent = variant == "primary"
        self.theme = options.pop("theme", self.theme)
        self.compact = bool(options.pop("compact", self.compact))
        aliases = {
            "bg": "_fill_override", "background": "_fill_override",
            "fg": "_foreground_override", "foreground": "_foreground_override",
            "text_color": "_foreground_override",
            "activebackground": "_hover_override",
            "activeforeground": "_active_foreground_override",
            "highlightbackground": "_border_override",
            "highlightcolor": "_focus_override",
            "disabledforeground": "_disabled_foreground",
            "padx": "_padx", "pady": "_pady", "takefocus": "_takefocus",
            "cursor": "_user_cursor", "width": "_minimum_width", "height": "_minimum_height",
        }
        for name, field in aliases.items():
            if name in options:
                setattr(self, field, options.pop(name))
        for name in ("image", "compound", "relief", "overrelief", "borderwidth", "bd", "highlightthickness"):
            options.pop(name, None)
        result = tk.Button.configure(self, **options) if options else None
        if not self._enabled():
            self._pressed = False
            self._mouse_down = False
            self._key_down = None
        self._sync_textvariable()
        self._render_key = None
        self._render()
        return result

    config = configure

    def cget(self, key):
        if getattr(self, "_velvet_ready", False):
            if key in ("bg", "background"):
                return self._current_fill
            if key in ("padx", "pady"):
                return self._padx if key == "padx" else self._pady
            if key == "activebackground":
                return self._hover_override or (
                    self.theme.accent_hover if self.variant == "primary"
                    else self.theme.conversation_hover if self.variant == "conversation"
                    else self.theme.panel_hover
                )
            if key == "highlightbackground":
                return self._border_override or self._colors()[2]
            if key == "highlightcolor":
                return self._focus_override or self.theme.accent
        return tk.Button.cget(self, key)

    def destroy(self) -> None:
        self._remove_text_trace()
        self._velvet_ready = False
        self._surface_image = None
        super().destroy()


class PrimaryButton(tk.Button):
    """A prominent, filled action button with high visual hierarchy."""

    def __init__(
        self,
        parent: tk.Misc,
        text: str,
        command: Callable[[], None] | None = None,
        *,
        accent: str = ACCENT,
        accent_hover: str = ACCENT_HOVER,
        font: tuple | str | None = None,
        **kwargs,
    ) -> None:
        self.accent_color = accent
        self.hover_color = accent_hover
        button_font = font or ("Segoe UI", 10, "bold")

        super().__init__(
            parent,
            text=text,
            command=command,
            bg=accent,
            fg=BG,
            activebackground=accent_hover,
            activeforeground=BG,
            disabledforeground=SUBTLE,
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=accent,
            highlightcolor=TEXT,
            padx=14,
            pady=6,
            cursor="hand2",
            takefocus=True,
            font=button_font,
            **kwargs,
        )
        self.bind("<Enter>", lambda _e: self.config(bg=self.hover_color))
        self.bind("<Leave>", lambda _e: self.config(bg=self.accent_color))


class GlassButton(tk.Canvas):
    """A high-visibility dark-blue button with an interactive glass shine animation.

    - Resting state: High-contrast Dark Navy Blue background (#13233c) with steel-blue border (#355b94).
    - Hover state (<Enter>): Specular glass shine smoothly glides from the left edge to 50% width (halfway).
    - Click state (<ButtonPress-1>): Glass shine sweeps from 50% to 100% width (all the way).
    - Mouse leave (<Leave>): Glass shine smoothly retracts back to the left side (0% width).
    """

    def __init__(
        self,
        parent: tk.Misc,
        text: str = "",
        command: Callable[[], None] | None = None,
        *,
        font: tuple | str | None = None,
        bg_color: str = BUTTON_DARK_BLUE,
        hover_color: str = BUTTON_DARK_BLUE_HOVER,
        active_color: str = BUTTON_DARK_BLUE_ACTIVE,
        border_color: str = BUTTON_DARK_BLUE_BORDER,
        text_color: str = TEXT,
        padx: int = 14,
        pady: int = 6,
        width: int | None = None,
        height: int | None = None,
        **kwargs,
    ) -> None:
        self._command = command
        self._text = text
        self._font = font or ("Segoe UI", 10, "bold")
        self._bg_color = bg_color
        self._hover_color = hover_color
        self._active_color = active_color
        self._border_color = border_color
        self._text_color = text_color
        self._state = kwargs.pop("state", "normal")
        self._padx = padx
        self._pady = pady

        kwargs.pop("activebackground", None)
        kwargs.pop("activeforeground", None)
        kwargs.pop("disabledforeground", None)
        kwargs.pop("relief", None)
        kwargs.pop("borderwidth", None)
        kwargs.pop("bd", None)

        self._current_progress = 0.0
        self._target_progress = 0.0
        self._animating = False
        self._is_pressed = False
        self._is_hovered = False

        f = tkfont.Font(font=self._font)
        text_w = f.measure(text)
        text_h = f.metrics("linespace")

        calc_w = width or max(60, text_w + padx * 2)
        calc_h = height or max(28, text_h + pady * 2)

        super().__init__(
            parent,
            width=calc_w,
            height=calc_h,
            bg=BG,
            highlightthickness=0,
            cursor="hand2" if self._state != "disabled" else "",
            takefocus=True,
            **kwargs,
        )

        self.bind("<Configure>", self._on_resize)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<FocusIn>", lambda _e: self._redraw())
        self.bind("<FocusOut>", lambda _e: self._redraw())
        self.bind("<Return>", lambda _e: self.invoke())
        self.bind("<space>", lambda _e: self.invoke())

        self._redraw()

    def _on_resize(self, _e: tk.Event) -> None:
        self._redraw()

    def _on_enter(self, _e: tk.Event) -> None:
        if self._state == "disabled":
            return
        self._is_hovered = True
        self._target_progress = 0.5
        self._start_anim()

    def _on_leave(self, _e: tk.Event) -> None:
        if self._state == "disabled":
            return
        self._is_hovered = False
        self._is_pressed = False
        self._target_progress = 0.0
        self._start_anim()

    def _on_press(self, _e: tk.Event) -> None:
        if self._state == "disabled":
            return
        self._is_pressed = True
        self._target_progress = 1.0
        self._start_anim()

    def _on_release(self, e: tk.Event) -> None:
        if self._state == "disabled":
            return
        was_pressed = self._is_pressed
        self._is_pressed = False
        w = max(self.winfo_width(), int(self["width"]))
        h = max(self.winfo_height(), int(self["height"]))
        if 0 <= e.x <= w and 0 <= e.y <= h:
            self._target_progress = 0.5
            self._start_anim()
            if was_pressed and self._command:
                self._command()
        else:
            self._target_progress = 0.0
            self._start_anim()

    def _start_anim(self) -> None:
        if not self._animating:
            self._animating = True
            self._step_anim()

    def _step_anim(self) -> None:
        if not self.winfo_exists():
            return
        diff = self._target_progress - self._current_progress
        if abs(diff) > 0.01:
            step = diff * 0.28
            if abs(step) < 0.02:
                step = 0.02 if diff > 0 else -0.02
            self._current_progress += step
            if (diff > 0 and self._current_progress > self._target_progress) or \
               (diff < 0 and self._current_progress < self._target_progress):
                self._current_progress = self._target_progress
            self._redraw()
            self.after(16, self._step_anim)
        else:
            self._current_progress = self._target_progress
            self._animating = False
            self._redraw()

    def _redraw(self) -> None:
        self.delete("all")
        w = max(self.winfo_width(), int(self["width"]))
        h = max(self.winfo_height(), int(self["height"]))
        if w <= 1 or h <= 1:
            return

        is_disabled = (self._state == "disabled")
        if is_disabled:
            fill_bg = blend_colors(BG, self._bg_color, 0.4)
            border = SUBTLE
            fg = SUBTLE
        elif self._is_pressed:
            fill_bg = self._active_color
            border = ACCENT
            fg = TEXT
        elif self._is_hovered:
            fill_bg = self._hover_color
            border = ACCENT
            fg = TEXT
        else:
            fill_bg = self._bg_color
            border = self._border_color
            fg = self._text_color

        rounded_rectangle(self, 1, 1, w - 1, h - 1, radius=4, fill=fill_bg, outline=border, width=1, tags="bg")

        if not is_disabled and self._current_progress > 0.005:
            shine_w = int((w - 4) * self._current_progress)
            if shine_w > 0:
                shine_fill = blend_colors(fill_bg, "#70d8f7", 0.22)
                self.create_rectangle(
                    2, 2, 2 + shine_w, h - 2,
                    fill=shine_fill,
                    width=0,
                    tags="shine",
                )
                self.create_line(
                    2 + shine_w, 2, 2 + shine_w, h - 2,
                    fill="#ffffff",
                    width=1,
                    tags="shine_line",
                )
                half_h = max(2, (h - 4) // 2)
                self.create_rectangle(
                    2, 2, 2 + shine_w, 2 + half_h,
                    fill=blend_colors(shine_fill, "#ffffff", 0.18),
                    width=0,
                    tags="shine_wedge",
                )

        self.create_text(
            w // 2,
            h // 2,
            text=self._text,
            fill=fg,
            font=self._font,
            tags="label",
        )

    def configure(self, **kwargs) -> None:
        if "text" in kwargs:
            self._text = kwargs.pop("text")
        if "command" in kwargs:
            self._command = kwargs.pop("command")
        if "state" in kwargs:
            self._state = kwargs.pop("state")
            if self._state == "disabled":
                super().configure(cursor="")
            else:
                super().configure(cursor="hand2")
        if "bg" in kwargs:
            self._bg_color = kwargs.pop("bg")
        if "fg" in kwargs:
            self._text_color = kwargs.pop("fg")
        kwargs.pop("activebackground", None)
        kwargs.pop("activeforeground", None)
        kwargs.pop("disabledforeground", None)
        kwargs.pop("relief", None)
        kwargs.pop("borderwidth", None)
        kwargs.pop("bd", None)
        self._redraw()
        if kwargs:
            super().configure(**kwargs)

    config = configure

    def cget(self, key: str):
        if key == "text":
            return self._text
        if key == "state":
            return self._state
        if key == "command":
            return self._command
        return super().cget(key)

    def __getitem__(self, key: str):
        return self.cget(key)

    def __setitem__(self, key: str, value) -> None:
        self.configure(**{key: value})

    def invoke(self) -> None:
        if self._state != "disabled" and self._command:
            self._command()


class StandardButton(tk.Button):
    """A standard elevated dark-blue action button for general dialogs and controls."""

    def __init__(
        self,
        parent: tk.Misc,
        text: str,
        command: Callable[[], None] | None = None,
        *,
        font: tuple | str | None = None,
        bg: str = BUTTON_DARK_BLUE,
        fg: str = TEXT,
        **kwargs,
    ) -> None:
        button_font = font or ("Segoe UI", 10, "bold")
        super().__init__(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg=fg,
            activebackground=BUTTON_DARK_BLUE_ACTIVE,
            activeforeground=TEXT,
            disabledforeground=SUBTLE,
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=BUTTON_DARK_BLUE_BORDER,
            highlightcolor=ACCENT,
            padx=14,
            pady=6,
            cursor="hand2",
            takefocus=True,
            font=button_font,
            **kwargs,
        )
        self.bind("<Enter>", lambda _e: self.config(bg=BUTTON_DARK_BLUE_HOVER, highlightbackground=ACCENT))
        self.bind("<Leave>", lambda _e: self.config(bg=bg, highlightbackground=BUTTON_DARK_BLUE_BORDER))


class CompactButton(tk.Button):
    """A low-profile action button for card footers, tables, and secondary actions."""

    def __init__(
        self,
        parent: tk.Misc,
        text: str,
        command: Callable[[], None] | None = None,
        *,
        accent: bool = False,
        accent_color: str = ACCENT,
        text_color: str | None = None,
        font: tuple | str | None = None,
        **kwargs,
    ) -> None:
        self.is_accent = accent
        bg_color = PANEL_HOVER if accent else PANEL
        fg_color = text_color or (accent_color if accent else MUTED)
        border_color = accent_color if accent else BORDER
        hover_bg = BORDER_STRONG if accent else PANEL_HOVER
        hover_fg = TEXT if accent else TEXT
        button_font = font or ("Segoe UI", 9, "bold")

        super().__init__(
            parent,
            text=text,
            command=command,
            bg=bg_color,
            fg=fg_color,
            activebackground=hover_bg,
            activeforeground=TEXT,
            disabledforeground=SUBTLE,
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=border_color,
            highlightcolor=ACCENT,
            padx=10,
            pady=4,
            cursor="hand2",
            takefocus=True,
            font=button_font,
            **kwargs,
        )
        self.bind("<Enter>", lambda _e: self.config(bg=hover_bg, fg=hover_fg))
        self.bind("<Leave>", lambda _e: self.config(bg=bg_color, fg=fg_color))


class ChatButton(tk.Button):
    """The warm Clockfire action button dedicated to raid quick-chat messaging."""

    def __init__(
        self,
        parent: tk.Misc,
        text: str = "Message raid",
        command: Callable[[], None] | None = None,
        *,
        font: tuple | str | None = None,
        bg: str = CLOCKFIRE,
        **kwargs,
    ) -> None:
        button_font = font or ("Segoe UI", 10, "bold")
        super().__init__(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg=BG,
            activebackground=CLOCKFIRE_HOVER,
            activeforeground=BG,
            disabledforeground=SUBTLE,
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=bg,
            highlightcolor=TEXT,
            padx=14,
            pady=7,
            cursor="hand2",
            takefocus=True,
            font=button_font,
            **kwargs,
        )
        self.bind("<Enter>", lambda _e: self.config(bg=CLOCKFIRE_HOVER))
        self.bind("<Leave>", lambda _e: self.config(bg=CLOCKFIRE))


class DangerButton(tk.Button):
    """A red-accented button for destructive actions (Stop, Disconnect, Reset)."""

    def __init__(
        self,
        parent: tk.Misc,
        text: str,
        command: Callable[[], None] | None = None,
        *,
        font: tuple | str | None = None,
        **kwargs,
    ) -> None:
        button_font = font or ("Segoe UI", 9, "bold")
        super().__init__(
            parent,
            text=text,
            command=command,
            bg=PANEL,
            fg=DANGER,
            activebackground=DANGER,
            activeforeground=TEXT,
            disabledforeground=SUBTLE,
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=DANGER,
            highlightcolor=TEXT,
            padx=10,
            pady=4,
            cursor="hand2",
            takefocus=True,
            font=button_font,
            **kwargs,
        )
        self.bind("<Enter>", lambda _e: self.config(bg=DANGER, fg=BG))
        self.bind("<Leave>", lambda _e: self.config(bg=PANEL, fg=DANGER))


class SupportButton(tk.Button):
    """Branded micro-button for Patreon or PayPal support links."""

    def __init__(
        self,
        parent: tk.Misc,
        kind: str,
        label: str,
        command: Callable[[], None] | None = None,
        *,
        font: tuple | str | None = None,
        **kwargs,
    ) -> None:
        color = PATREON_CORAL if kind.lower() == "patreon" else PAYPAL_BLUE
        button_font = font or ("Segoe UI", 8, "bold")
        super().__init__(
            parent,
            text=label,
            command=command,
            bg=PANEL,
            fg=color,
            activebackground=color,
            activeforeground=BG,
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=color,
            highlightcolor=TEXT,
            padx=8,
            pady=3,
            cursor="hand2",
            takefocus=True,
            font=button_font,
            **kwargs,
        )
        self.bind("<Enter>", lambda _e: self.config(bg=color, fg=BG))
        self.bind("<Leave>", lambda _e: self.config(bg=PANEL, fg=color))


class IconButton(tk.Button):
    """A minimal window-chrome button supporting MDL2 Assets glyphs with ascii fallbacks."""

    def __init__(
        self,
        parent: tk.Misc,
        name: str,
        command: Callable[[], None] | None = None,
        *,
        danger: bool = False,
        chrome_font: tuple | str | None = None,
        **kwargs,
    ) -> None:
        glyph, fallback = GLYPHS.get(name, ("", "?"))
        display_text = glyph if chrome_font else fallback
        hover = DANGER if danger else PANEL_HOVER
        button_font = chrome_font or ("Segoe MDL2 Assets", 10)

        super().__init__(
            parent,
            text=display_text,
            command=command,
            bg=BG,
            fg=MUTED,
            activebackground=hover,
            activeforeground=TEXT,
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=BG,
            highlightcolor=ACCENT,
            font=button_font,
            width=4,
            cursor="hand2",
            takefocus=True,
            **kwargs,
        )
        self.bind("<Enter>", lambda _e: self.config(bg=hover, fg=TEXT))
        self.bind("<Leave>", lambda _e: self.config(bg=BG, fg=MUTED))

    def set_glyph(self, name: str, has_chrome_font: bool = True) -> None:
        """Update the button's displayed glyph."""
        glyph, fallback = GLYPHS.get(name, ("", "?"))
        self.config(text=glyph if has_chrome_font else fallback)


class ToggleSwitch(tk.Canvas):
    """An accessible, smooth 60fps canvas-drawn animated toggle switch.

    Renders a rounded pill track and sliding circular knob.
    Supports Spacebar / Enter keystrokes and click interaction.
    """

    def __init__(
        self,
        parent: tk.Misc,
        *,
        checked: bool = False,
        command: Callable[[bool], None] | None = None,
        accent: str = ACCENT,
        width: int = 42,
        height: int = 22,
        animate: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(
            parent,
            width=width,
            height=height,
            bg=PANEL_ALT,
            highlightthickness=0,
            cursor="hand2",
            takefocus=True,
            **kwargs,
        )
        self.width = width
        self.height = height
        self.radius = height // 2
        self.accent = accent
        self.command = command
        self.animate = animate

        self._checked = bool(checked)
        self._current_pos = 1.0 if self._checked else 0.0
        self._anim_timer: str | None = None

        self.bind("<Button-1>", self._on_click)
        self.bind("<space>", self._on_key)
        self.bind("<Return>", self._on_key)
        self.bind("<FocusIn>", lambda _e: self._redraw())
        self.bind("<FocusOut>", lambda _e: self._redraw())

        self._redraw()

    @property
    def is_checked(self) -> bool:
        return self._checked

    def set_state(self, checked: bool, invoke_command: bool = False) -> None:
        """Set the switch state, with optional command callback."""
        if self._checked == checked:
            return
        self._checked = bool(checked)
        if self.animate:
            self._start_animation()
        else:
            self._current_pos = 1.0 if self._checked else 0.0
            self._redraw()
        if invoke_command and self.command:
            self.command(self._checked)

    def _on_click(self, _event=None) -> None:
        self.focus_set()
        self.set_state(not self._checked, invoke_command=True)

    def _on_key(self, _event=None) -> str:
        self.set_state(not self._checked, invoke_command=True)
        return "break"

    def _start_animation(self) -> None:
        if self._anim_timer:
            self.after_cancel(self._anim_timer)
            self._anim_timer = None
        self._step_animation()

    def _step_animation(self) -> None:
        target = 1.0 if self._checked else 0.0
        delta = target - self._current_pos
        step = 0.25 if delta > 0 else -0.25

        if abs(delta) <= 0.25:
            self._current_pos = target
            self._redraw()
            self._anim_timer = None
        else:
            self._current_pos += step
            self._redraw()
            self._anim_timer = self.after(16, self._step_animation)

    def _redraw(self) -> None:
        self.delete("all")
        focused = self.focus_get() == self
        track_color = blend_colors(BORDER, self.accent, self._current_pos)
        border_color = self.accent if focused else track_color

        # Draw pill track
        rounded_rectangle(
            self,
            2,
            2,
            self.width - 2,
            self.height - 2,
            radius=self.radius - 2,
            fill=track_color,
            outline=border_color,
            width=1,
        )

        # Calculate knob position
        knob_radius = self.radius - 4
        min_x = 4 + knob_radius
        max_x = self.width - 4 - knob_radius
        knob_x = min_x + (max_x - min_x) * self._current_pos
        knob_y = self.height / 2

        # Draw knob
        self.create_oval(
            knob_x - knob_radius,
            knob_y - knob_radius,
            knob_x + knob_radius,
            knob_y + knob_radius,
            fill=TEXT,
            outline="",
        )
