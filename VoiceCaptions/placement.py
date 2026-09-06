"""Interactive placement mode for the otherwise click-through overlay."""

from __future__ import annotations

import ctypes
import sys
import tkinter as tk
from ctypes import wintypes
from dataclasses import replace
from pathlib import Path
from tkinter import colorchooser
from tkinter import font as tkfont

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from VoiceCaptions.layout import (  # type: ignore
        BASE_FONT_SIZE,
        DEFAULT_METRICS,
        background_stipple,
        canvas_height_for_target,
        caption_font_key,
        caption_font_specs,
        clipped_text,
        metrics_for_target,
    )
    from VoiceCaptions.overlay import (  # type: ignore
        CHROMA_KEY,
        DANGER,
        FROST,
        MUTED,
        SLATE,
        SLATE_LIVE,
        TEXT,
        VOID,
        canvas_height_for_rows,
        resolve_anchor_rect,
        rounded_rectangle,
        top_level_handle,
    )
    from VoiceCaptions.settings import (  # type: ignore
        DEFAULT_SETTINGS,
        CAPTION_BACKGROUND_OPACITIES,
        MAX_CAPTION_FONT_SIZE,
        MIN_CAPTION_FONT_SIZE,
        default_settings_path,
        load_settings,
        position_from_settings,
        save_settings,
        settings_from_position,
    )
else:
    from .layout import (
        BASE_FONT_SIZE,
        DEFAULT_METRICS,
        background_stipple,
        canvas_height_for_target,
        caption_font_key,
        caption_font_specs,
        clipped_text,
        metrics_for_target,
    )
    from .overlay import (
        CHROMA_KEY,
        DANGER,
        FROST,
        MUTED,
        SLATE,
        SLATE_LIVE,
        TEXT,
        VOID,
        canvas_height_for_rows,
        resolve_anchor_rect,
        rounded_rectangle,
        top_level_handle,
    )
    from .settings import (
        DEFAULT_SETTINGS,
        CAPTION_BACKGROUND_OPACITIES,
        MAX_CAPTION_FONT_SIZE,
        MIN_CAPTION_FONT_SIZE,
        default_settings_path,
        load_settings,
        position_from_settings,
        save_settings,
        settings_from_position,
    )

WINDOW_WIDTH = DEFAULT_METRICS.canvas_width
FOOTER_GAP = 6
BOTTOM_REFERENCE_HEIGHT = canvas_height_for_rows(
    DEFAULT_SETTINGS.max_visible_speakers
)


def target_rect_for_settings(
    root: tk.Misc, settings
) -> tuple[int, int, int, int]:
    """Resolve the selected target for the movable preview.

    The anchor resolver falls through to known games when a selected surface
    is unavailable. The editable preview uses a local screen-sized work
    surface only when no anchor can be resolved, so its controls remain
    reachable without changing the overlay's saved target.
    """
    resolved = resolve_anchor_rect(settings)
    if resolved is not None:
        return resolved
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


def preview_surface(root: tk.Misc, settings, target_rect):
    """Return the exact live-overlay metrics and height for this target."""
    try:
        fallback_width = int(root.winfo_screenwidth())
        fallback_height = int(root.winfo_screenheight())
    except tk.TclError:
        fallback_width = None
        fallback_height = None
    metrics = metrics_for_target(
        settings.caption_font_size,
        target_rect,
        fallback_width=fallback_width,
    )
    height = canvas_height_for_target(
        settings.max_visible_speakers,
        settings.caption_font_size,
        target_rect,
        fallback_height=fallback_height,
    )
    return metrics, height


def control_panel_position(
    target_rect: tuple[int, int, int, int],
    preview_rect: tuple[int, int, int, int],
    panel_size: tuple[int, int],
) -> tuple[int, int]:
    """Keep the natural-size controls reachable beside the exact preview.

    The caption surface must remain the same size as the live overlay. A
    footer inside that window either makes the preview lie or pushes controls
    off a 1080p display at large settings. The controls therefore live in a
    small companion window: below the preview when it fits, above it next,
    and clamped to the target work area when the preview fills the display.
    """
    left, top, width, height = target_rect
    preview_x, preview_y, preview_width, preview_height = preview_rect
    panel_width, panel_height = panel_size
    right = left + max(int(width), 0)
    bottom = top + max(int(height), 0)

    if panel_width >= width:
        x = left
    else:
        centered = preview_x + (preview_width - panel_width) // 2
        x = min(max(centered, left), right - panel_width)

    below = preview_y + preview_height + FOOTER_GAP
    above = preview_y - FOOTER_GAP - panel_height
    if below + panel_height <= bottom:
        y = below
    elif above >= top:
        y = above
    elif panel_height >= height:
        y = top
    else:
        # Cover the oldest preview rows rather than the newest live row and
        # its privacy tally when the full-height preview leaves no clear edge.
        y = top
    return int(x), int(y)


def own_control_panel(panel: tk.Misc, owner: tk.Misc) -> bool:
    """Make a borderless sidecar follow its owner without losing focus.

    Tk's normal transient relationship works on other platforms. On Windows,
    combining ``transient`` with ``overrideredirect`` produces a WS_CHILD
    wrapper that cannot activate the panel's controls, so establish the native
    owner relationship on the recreated top-level wrapper instead.
    """
    if sys.platform != "win32":
        try:
            panel.transient(owner)
            panel.lift(owner)
            return True
        except tk.TclError:
            return False
    try:
        panel.transient("")
        panel.update_idletasks()
        panel_hwnd = top_level_handle(panel)
        owner_hwnd = top_level_handle(owner)
        if not panel_hwnd or not owner_hwnd:
            return False
        user32 = ctypes.windll.user32
        set_owner = user32.SetWindowLongPtrW
        set_owner.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        set_owner.restype = ctypes.c_ssize_t
        get_window = user32.GetWindow
        get_window.argtypes = [wintypes.HWND, ctypes.c_uint]
        get_window.restype = wintypes.HWND
        set_position = user32.SetWindowPos
        set_position.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        set_position.restype = wintypes.BOOL
        set_owner(panel_hwnd, -8, owner_hwnd)  # GWLP_HWNDPARENT
        if int(get_window(panel_hwnd, 4) or 0) != owner_hwnd:  # GW_OWNER
            return False
        # HWND_TOP; SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE. Do not map a
        # panel whose owner is still withdrawn during preview construction.
        set_position(
            panel_hwnd,
            0,
            0,
            0,
            0,
            0,
            0x0001 | 0x0002 | 0x0010,
        )
        return True
    except Exception:
        return False


class PlacementWindow:
    def __init__(self, root: tk.Tk, *, preview: bool = False) -> None:
        self.root = root
        self.preview = bool(preview)
        self.drag_origin: tuple[int, int, int, int] | None = None
        self.saved_label = "Drag the preview or use arrow keys"
        self.settings_path = None if self.preview else default_settings_path()
        self.settings_mtime_ns = -1
        self.settings = (
            DEFAULT_SETTINGS
            if self.preview
            else load_settings(self.settings_path)
        )
        self.target_rect = target_rect_for_settings(root, self.settings)
        self.metrics, self.canvas_height = preview_surface(
            root,
            self.settings,
            self.target_rect,
        )
        self.overlay_width = self.metrics.canvas_width
        self.window_width = self.overlay_width
        self.preview_offset_x = 0
        self.window_height = self.canvas_height

        root.title("Move Vemal's Ears")
        root.configure(bg=CHROMA_KEY)
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        try:
            root.wm_attributes("-transparentcolor", CHROMA_KEY)
        except tk.TclError:
            pass

        self.canvas = tk.Canvas(
            root,
            width=self.window_width,
            height=self.canvas_height,
            bg=CHROMA_KEY,
            highlightthickness=0,
            borderwidth=0,
            cursor="fleur",
            takefocus=True,
        )
        self.canvas.pack(fill="both", expand=True)
        self.fonts = self.preview_fonts(
            self.settings.caption_font_size,
            self.settings.caption_font_family,
        )
        self.build_footer()

        overlay_x, y = position_from_settings(
            self.target_rect,
            (self.overlay_width, self.canvas_height),
            self.settings,
            bottom_reference_height=BOTTOM_REFERENCE_HEIGHT,
        )
        x = overlay_x
        root.geometry(f"{self.window_width}x{self.window_height}+{x}+{y}")
        self.position_controls((x, y))
        self.draw()

        self.canvas.bind("<ButtonPress-1>", self.press)
        self.canvas.bind("<B1-Motion>", self.drag)
        self.canvas.bind("<ButtonRelease-1>", self.release)
        self.canvas.tag_bind("caption-preview", "<ButtonPress-1>", self.press_card)
        self.canvas.tag_bind("caption-preview", "<B1-Motion>", self.drag_card)
        self.canvas.tag_bind("caption-preview", "<ButtonRelease-1>", self.release_card)
        root.bind("<Escape>", lambda _event: root.destroy())
        root.bind("<Left>", lambda event: self.key_nudge(event, -1, 0))
        root.bind("<Right>", lambda event: self.key_nudge(event, 1, 0))
        root.bind("<Up>", lambda event: self.key_nudge(event, 0, -1))
        root.bind("<Down>", lambda event: self.key_nudge(event, 0, 1))
        root.bind("<Shift-Left>", lambda event: self.key_nudge(event, -10, 0))
        root.bind("<Shift-Right>", lambda event: self.key_nudge(event, 10, 0))
        root.bind("<Shift-Up>", lambda event: self.key_nudge(event, 0, -10))
        root.bind("<Shift-Down>", lambda event: self.key_nudge(event, 0, 10))
        root.bind("<Map>", self.sync_control_visibility, add="+")
        root.bind("<Unmap>", self.sync_control_visibility, add="+")
        root.focus_force()
        self.sync_control_visibility()
        if not self.preview:
            root.after(250, self.refresh_settings)

    @staticmethod
    def preview_fonts(
        size: int,
        family: str = "Segoe UI",
        control_font=None,
    ) -> dict:
        """Preview text scaled like the overlay's, so sizes read true.

        The control font is deliberately NOT scaled. Placement controls are
        real Tk buttons, so they retain readable labels and keyboard focus
        without growing alongside the combat captions.
        """
        fonts = {
            name: tkfont.Font(**spec)
            for name, spec in caption_font_specs(size, family).items()
        }
        fonts["control"] = control_font or tkfont.Font(
            family="Segoe UI Variable", size=9, weight="bold"
        )
        return fonts

    def update_control_bounds(self) -> None:
        """Re-measure and re-anchor controls after size or DPI changes."""
        self.position_controls()

    def position_controls(
        self,
        preview_position: tuple[int, int] | None = None,
    ) -> None:
        controls = getattr(self, "controls", None)
        if controls is None:
            return
        try:
            controls.update_idletasks()
            panel_size = (
                max(1, int(controls.winfo_reqwidth())),
                max(1, int(controls.winfo_reqheight())),
            )
            if preview_position is None:
                preview_position = (
                    int(self.root.winfo_x()),
                    int(self.root.winfo_y()),
                )
            panel_x, panel_y = control_panel_position(
                self.target_rect,
                (
                    int(preview_position[0]),
                    int(preview_position[1]),
                    int(self.overlay_width),
                    int(self.canvas_height),
                ),
                panel_size,
            )
            controls.geometry(
                f"{panel_size[0]}x{panel_size[1]}+{panel_x}+{panel_y}"
            )
            controls.lift()
        except tk.TclError:
            return

    def sync_control_visibility(self, event: tk.Event | None = None) -> None:
        """Map and withdraw the sidecar with the caption preview root."""
        if event is not None and getattr(event, "widget", None) is not self.root:
            return
        controls = getattr(self, "controls", None)
        if controls is None:
            return
        try:
            visible = (
                self.root.state() == "normal"
                and bool(self.root.winfo_viewable())
            )
            if visible:
                self.position_controls()
                controls.deiconify()
                controls.lift()
            else:
                controls.withdraw()
        except tk.TclError:
            return

    def build_footer(self) -> None:
        controls_window = tk.Toplevel(self.root)
        controls_window.withdraw()
        controls_window.title("Caption appearance")
        controls_window.configure(bg="#2a3c52")
        controls_window.overrideredirect(True)
        controls_window.bind("<Escape>", lambda _event: self.root.destroy())
        controls_window.protocol("WM_DELETE_WINDOW", self.root.destroy)
        self.controls = controls_window
        self.controls_owned = own_control_panel(controls_window, self.root)
        shell = tk.Frame(
            controls_window,
            bg="#2a3c52",
        )
        self.footer_shell = shell
        shell.pack(fill="both", expand=True)
        footer = tk.Frame(shell, bg=SLATE)
        footer.pack(fill="both", expand=True, padx=1, pady=1)
        top = tk.Frame(footer, bg=SLATE)
        top.pack(fill="x", padx=12, pady=(8, 5))
        tk.Label(
            top,
            text="Move captions",
            bg=SLATE,
            fg=TEXT,
            font=self.fonts["control"],
            anchor="w",
        ).pack(side="left")
        self.footer_status = tk.Label(
            top,
            text=self.saved_label,
            bg=SLATE,
            fg=FROST,
            font=self.fonts["control"],
            anchor="e",
        )
        self.footer_status.pack(side="right")

        appearance = tk.Frame(footer, bg=SLATE)
        appearance.pack(fill="x", padx=12, pady=(0, 5))
        tk.Label(
            appearance,
            text="Font",
            bg=SLATE,
            fg=MUTED,
            font=self.fonts["control"],
        ).pack(side="left", padx=(0, 5))
        families = set(tkfont.families(self.root))
        families.add(self.settings.caption_font_family)
        self.font_family_var = tk.StringVar(
            value=self.settings.caption_font_family
        )
        self.font_family_menu = tk.OptionMenu(
            appearance,
            self.font_family_var,
            *sorted(families, key=str.casefold),
            command=self.set_font_family,
        )
        self.style_option_menu(self.font_family_menu, width=19)
        self.font_family_menu.pack(side="left")

        tk.Label(
            appearance,
            text="Size",
            bg=SLATE,
            fg=MUTED,
            font=self.fonts["control"],
        ).pack(side="left", padx=(10, 5))
        self.font_size_var = tk.StringVar(
            value=str(self.settings.caption_font_size)
        )
        self.font_size_spinbox = tk.Spinbox(
            appearance,
            from_=MIN_CAPTION_FONT_SIZE,
            to=MAX_CAPTION_FONT_SIZE,
            width=4,
            textvariable=self.font_size_var,
            command=self.set_font_size,
            bg=SLATE_LIVE,
            fg=TEXT,
            buttonbackground=SLATE_LIVE,
            insertbackground=TEXT,
            relief="flat",
            highlightthickness=1,
            highlightbackground="#334a63",
            highlightcolor=FROST,
            font=self.fonts["control"],
        )
        self.font_size_spinbox.pack(side="left")
        self.font_size_spinbox.bind("<Return>", self.set_font_size)
        self.font_size_spinbox.bind("<FocusOut>", self.set_font_size)
        self.font_size_spinbox.bind("<KeyRelease>", self.preview_font_size)

        tk.Label(
            appearance,
            text="Backdrop",
            bg=SLATE,
            fg=MUTED,
            font=self.fonts["control"],
        ).pack(side="left", padx=(10, 5))
        self.opacity_var = tk.StringVar(
            value=f"{self.settings.caption_background_opacity}%"
        )
        opacity_values = [f"{value}%" for value in CAPTION_BACKGROUND_OPACITIES]
        self.opacity_menu = tk.OptionMenu(
            appearance,
            self.opacity_var,
            *opacity_values,
            command=self.set_background_opacity,
        )
        self.style_option_menu(self.opacity_menu, width=5)
        self.opacity_menu.pack(side="left")

        colours = tk.Frame(footer, bg=SLATE)
        colours.pack(fill="x", padx=12, pady=(0, 5))
        tk.Label(
            colours,
            text="Colours",
            bg=SLATE,
            fg=MUTED,
            font=self.fonts["control"],
        ).pack(side="left", padx=(0, 5))
        self.text_color_button = self.make_color_button(
            colours,
            "Text",
            "caption_text_color",
        )
        self.text_color_button.pack(side="left")
        self.background_color_button = self.make_color_button(
            colours,
            "Background",
            "caption_background_color",
        )
        self.background_color_button.pack(side="left", padx=(8, 0))

        controls = tk.Frame(footer, bg=SLATE)
        controls.pack(fill="x", padx=12, pady=(0, 8))
        self.save_button = self.make_button(
            controls,
            "Save position",
            self.save,
            primary=True,
        )
        self.save_button.pack(side="left")
        self.reset_button = self.make_button(
            controls,
            "Use safe default",
            self.reset,
        )
        self.reset_button.pack(side="left", padx=(8, 0))
        self.done_button = self.make_button(
            controls,
            "Done",
            self.root.destroy,
        )
        self.done_button.pack(side="right")

    def style_option_menu(self, menu: tk.OptionMenu, *, width: int) -> None:
        menu.configure(
            width=width,
            bg=SLATE_LIVE,
            fg=TEXT,
            activebackground="#203044",
            activeforeground=TEXT,
            relief="flat",
            highlightthickness=1,
            highlightbackground="#334a63",
            highlightcolor=FROST,
            font=self.fonts["control"],
        )
        menu["menu"].configure(
            bg=SLATE_LIVE,
            fg=TEXT,
            activebackground="#203044",
            activeforeground=TEXT,
            font=self.fonts["control"],
        )

    @staticmethod
    def contrasting_text(colour: str) -> str:
        red = int(colour[1:3], 16)
        green = int(colour[3:5], 16)
        blue = int(colour[5:7], 16)
        return VOID if red * 299 + green * 587 + blue * 114 > 150000 else TEXT

    def make_color_button(
        self,
        parent: tk.Misc,
        label: str,
        field: str,
    ) -> tk.Button:
        colour = getattr(self.settings, field)
        return tk.Button(
            parent,
            text=f"{label}  {colour.upper()}",
            command=lambda: self.choose_color(label, field),
            bg=colour,
            fg=self.contrasting_text(colour),
            activebackground=colour,
            activeforeground=self.contrasting_text(colour),
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground="#334a63",
            highlightcolor=FROST,
            padx=10,
            pady=4,
            takefocus=True,
            cursor="hand2",
            font=self.fonts["control"],
        )

    def apply_appearance(self, field: str, value) -> bool:
        current = (
            self.settings
            if self.preview
            else load_settings(self.settings_path)
        )
        updated = replace(current, **{field: value})
        target_changed = (
            updated.anchor_mode != self.settings.anchor_mode
            or updated.anchor_executable != self.settings.anchor_executable
            or updated.anchor_monitor != self.settings.anchor_monitor
        )
        if not self.preview and not save_settings(updated, self.settings_path):
            self.flash("Could not save appearance")
            self.sync_appearance_controls()
            return False
        if target_changed:
            self.target_rect = target_rect_for_settings(self.root, updated)
        self.apply_settings(updated, reposition=target_changed)
        self.sync_appearance_controls()
        self.flash("Preview updated")
        return True

    def set_font_family(self, value: str) -> None:
        self.apply_appearance("caption_font_family", value)

    def set_font_size(self, _event=None) -> str | None:
        try:
            value = int(self.font_size_var.get().strip())
        except (TypeError, ValueError):
            self.font_size_var.set(str(self.settings.caption_font_size))
            return "break" if _event is not None else None
        value = max(MIN_CAPTION_FONT_SIZE, min(MAX_CAPTION_FONT_SIZE, value))
        self.apply_appearance("caption_font_size", value)
        return "break" if _event is not None else None

    def preview_font_size(self, _event=None) -> None:
        try:
            value = int(self.font_size_var.get().strip())
        except (TypeError, ValueError):
            return
        if MIN_CAPTION_FONT_SIZE <= value <= MAX_CAPTION_FONT_SIZE:
            if value != self.settings.caption_font_size:
                self.apply_appearance("caption_font_size", value)

    def set_background_opacity(self, value: str) -> None:
        try:
            opacity = int(value.rstrip("%"))
        except (AttributeError, ValueError):
            opacity = self.settings.caption_background_opacity
        self.apply_appearance("caption_background_opacity", opacity)

    def choose_color(self, label: str, field: str) -> None:
        current = getattr(self.settings, field)
        _rgb, colour = colorchooser.askcolor(
            color=current,
            parent=getattr(self, "controls", self.root),
            title=f"Caption {label.lower()} colour",
        )
        if colour:
            colour = colour.lower()
            if colour == CHROMA_KEY:
                self.flash("Choose a visible colour")
                return
            self.apply_appearance(field, colour)

    def sync_appearance_controls(self) -> None:
        if hasattr(self, "font_family_var"):
            self.font_family_var.set(self.settings.caption_font_family)
        if hasattr(self, "font_size_var"):
            self.font_size_var.set(str(self.settings.caption_font_size))
        if hasattr(self, "opacity_var"):
            self.opacity_var.set(
                f"{self.settings.caption_background_opacity}%"
            )
        for attribute, label, field in (
            ("text_color_button", "Text", "caption_text_color"),
            (
                "background_color_button",
                "Background",
                "caption_background_color",
            ),
        ):
            button = getattr(self, attribute, None)
            if button is None:
                continue
            colour = getattr(self.settings, field)
            foreground = self.contrasting_text(colour)
            button.configure(
                text=f"{label}  {colour.upper()}",
                bg=colour,
                fg=foreground,
                activebackground=colour,
                activeforeground=foreground,
            )

    def make_button(
        self,
        parent: tk.Frame,
        label: str,
        command,
        *,
        primary: bool = False,
    ) -> tk.Button:
        normal = FROST if primary else SLATE_LIVE
        foreground = VOID if primary else TEXT
        button = tk.Button(
            parent,
            text=label,
            command=command,
            bg=normal,
            fg=foreground,
            activebackground="#70d8f7" if primary else "#203044",
            activeforeground=VOID if primary else TEXT,
            relief="flat",
            borderwidth=0,
            highlightthickness=2,
            highlightbackground="#334a63",
            highlightcolor=FROST,
            padx=12,
            pady=10,
            takefocus=True,
            cursor="hand2",
            font=self.fonts["control"],
        )
        return button

    def apply_settings(self, updated, *, reposition: bool) -> None:
        """Apply one saved setting while preserving the member's drag."""
        old_height = self.canvas_height
        old_metrics = getattr(
            self,
            "metrics",
            metrics_for_target(
                self.settings.caption_font_size,
                self.target_rect,
                fallback_width=WINDOW_WIDTH,
            ),
        )
        old_overlay_width = getattr(
            self, "overlay_width", old_metrics.canvas_width
        )
        old_window_width = getattr(self, "window_width", old_overlay_width)
        desired_metrics, desired_height = preview_surface(
            self.root,
            updated,
            self.target_rect,
        )
        desired_overlay_width = desired_metrics.canvas_width
        desired_window_width = desired_overlay_width
        x = self.root.winfo_x()
        y = self.root.winfo_y()
        resized = desired_height != old_height
        rewidth = desired_window_width != old_window_width
        overlay_rewidth = desired_overlay_width != old_overlay_width
        refonted = (
            updated.caption_font_size != self.settings.caption_font_size
            or updated.caption_font_family
            != self.settings.caption_font_family
        )
        repainted = (
            updated.caption_text_color != self.settings.caption_text_color
            or updated.caption_background_color
            != self.settings.caption_background_color
            or updated.caption_background_opacity
            != self.settings.caption_background_opacity
        )
        self.settings = updated
        self.metrics = desired_metrics
        self.overlay_width = desired_overlay_width
        self.window_width = desired_window_width
        self.preview_offset_x = 0
        if refonted:
            self.fonts = self.preview_fonts(
                updated.caption_font_size,
                updated.caption_font_family,
                self.fonts.get("control"),
            )
        if resized:
            self.canvas_height = desired_height
            self.window_height = self.canvas_height
            self.update_control_bounds()
            self.canvas.configure(height=self.canvas_height)
            self.drag_origin = None
        if rewidth:
            self.canvas.configure(width=self.window_width)
        if reposition:
            overlay_x, y = position_from_settings(
                self.target_rect,
                (self.overlay_width, self.canvas_height),
                updated,
                bottom_reference_height=BOTTOM_REFERENCE_HEIGHT,
            )
            x = overlay_x
        else:
            if rewidth or overlay_rewidth:
                # Keep the caption surface centred on the member's drag.
                x += (old_overlay_width - desired_overlay_width) // 2
            if resized and self.settings.growth != "down":
                # Preserve the dragged caption baseline while the stack grows.
                y += old_height - desired_height
        if reposition or resized or rewidth or overlay_rewidth:
            self.root.geometry(
                f"{self.window_width}x{self.window_height}+{x}+{y}"
            )
        self.position_controls((x, y))
        if resized or rewidth or overlay_rewidth or refonted or repainted:
            self.draw()

    def refresh_settings(self) -> None:
        """Keep the preview matched to display changes made beside it."""
        if self.settings_path is None:
            return
        try:
            modified = self.settings_path.stat().st_mtime_ns
        except OSError:
            modified = 0
        if modified != self.settings_mtime_ns:
            self.settings_mtime_ns = modified
            updated = load_settings(self.settings_path)
            # Never reposition from the file while this window is open:
            # a drag in progress is the member's newest intent, and the
            # saved anchor is exactly what they are here to replace.
            # Repositioning on a row-count change meant changing "Visible
            # speakers" in the gear menu snapped the preview back to the
            # spot the member had just dragged it away from - and Save
            # then wrote that old spot over their new one. The resized
            # branch inside apply_settings keeps the caption baseline
            # where the drag left it.
            target_changed = (
                updated.anchor_mode != self.settings.anchor_mode
                or updated.anchor_executable != self.settings.anchor_executable
                or updated.anchor_monitor != self.settings.anchor_monitor
            )
            if target_changed:
                self.target_rect = target_rect_for_settings(self.root, updated)
            self.apply_settings(updated, reposition=target_changed)
            sync_controls = getattr(self, "sync_appearance_controls", None)
            if callable(sync_controls):
                sync_controls()
        self.root.after(250, self.refresh_settings)

    @staticmethod
    def inside(bounds: tuple[int, int, int, int], x: int, y: int) -> bool:
        x1, y1, x2, y2 = bounds
        return x1 <= x <= x2 and y1 <= y <= y2

    def draw(self) -> None:
        self.canvas.delete("all")
        # The preview stack scales with the caption font, because the size
        # of the footprint is exactly what the member is here to judge -
        # dragging a two-row-looking box and then getting a wall of tall
        # cards is the mistake this window exists to prevent. Metrics are
        # shared with the live overlay and fitted to this selected target.
        metrics = getattr(
            self,
            "metrics",
            metrics_for_target(
                self.settings.caption_font_size,
                getattr(self, "target_rect", None),
                fallback_width=WINDOW_WIDTH,
            ),
        )
        examples = (
            (330, "Tari", "Adds are spawning.", "#a330c9", "whisper"),
            (370, "Lena", "Move out, then back in.", "#fff468", "normal"),
            (360, "Drex", "Boss turning left.", "#4b8fe8", "shout"),
            (430, "Mira", "Spread for circles, then stack on me.", "#4fd182", "normal"),
        )
        # More rows than there are examples: repeat them so the preview
        # shows the real count rather than silently capping at four.
        wanted = max(1, self.settings.max_visible_speakers)
        shown = [examples[index % len(examples)] for index in range(wanted)]
        # WYSIWYG with the overlay: every card is the settled shape now.
        heights = [metrics.final_height] * len(shown)
        while (
            len(shown) > 1
            and metrics.stacked(heights) > self.canvas_height
        ):
            shown.pop(0)
            heights.pop(0)
        if self.settings.growth == "down":
            # WYSIWYG with the live overlay: dropped in the top half, the
            # feed hangs from its top edge, so the preview must too.
            cursor_y = metrics.vertical_padding
        else:
            cursor_y = (
                self.canvas_height
                - metrics.stacked(heights)
                + metrics.vertical_padding
            )
        for index, (width, speaker, text, color, speech_style) in enumerate(shown):
            live = index == len(shown) - 1
            height = heights[index]
            scaled_width = round(
                width
                * self.settings.caption_font_size
                / BASE_FONT_SIZE
            )
            self.draw_caption(
                y1=cursor_y,
                height=height,
                width=max(
                    metrics.min_card_width,
                    min(scaled_width, metrics.max_card_width),
                ),
                speaker=speaker,
                text=text,
                color=color,
                live=live,
                speech_style=speech_style,
            )
            cursor_y += height + metrics.row_gap

    def draw_caption(
        self,
        *,
        y1: int,
        height: int,
        width: int,
        speaker: str,
        text: str,
        color: str,
        live: bool,
        speech_style: str = "normal",
    ) -> None:
        canvas_width = getattr(self, "window_width", WINDOW_WIDTH)
        x1 = (canvas_width - width) // 2
        x2, y2 = x1 + width, y1 + height
        rounded_rectangle(
            self.canvas,
            x1,
            y1,
            x2,
            y2,
            8,
            fill=self.settings.caption_background_color,
            outline=SLATE,
            stipple=background_stipple(
                self.settings.caption_background_opacity
            ),
            tags=("caption-preview",),
        )
        self.canvas.create_rectangle(
            x1 + 1,
            y1 + 6,
            x1 + 4,
            y2 - 6,
            fill=color,
            outline="",
            tags=("caption-preview",),
        )
        # Proportional to the card rather than fixed, so the initial does
        # not sit as a dot in the corner of a large-font row.
        name_x = x1 + 14
        text_y = y1 + height // 2
        self.canvas.create_text(
            name_x,
            text_y,
            text=speaker,
            fill=color,
            font=self.fonts["speaker"],
            anchor="w",
            tags=("caption-preview",),
        )
        name_column = min(
            round(96 * self.settings.caption_font_size / BASE_FONT_SIZE),
            width // 3,
        )
        caption_x = name_x + name_column + 10
        tally_width = self.recording_tally_size()[0] if live else 0
        reserve = tally_width + 12 if live else 9
        available = max(x2 - caption_x - reserve, 0)
        caption_font = self.fonts.get(
            caption_font_key(live, speech_style),
            self.fonts["previous"],
        )
        self.canvas.create_text(
            caption_x,
            text_y,
            text=clipped_text(text, available, caption_font.measure),
            fill=self.settings.caption_text_color,
            font=caption_font,
            anchor="w",
            tags=("caption-preview",),
        )
        if live:
            self.draw_recording_tally(right=x2 - 6, center_y=text_y)

    def recording_tally_size(self) -> tuple[int, int]:
        """Size the preview tally exactly around its scaled dot and label."""
        font = self.fonts["status"]
        try:
            line_height = int(font.metrics("linespace"))
        except (AttributeError, TypeError, ValueError, tk.TclError):
            line_height = max(
                10,
                round(
                    12
                    * self.settings.caption_font_size
                    / BASE_FONT_SIZE
                ),
            )
        dot_size = max(
            5,
            round(
                6
                * self.settings.caption_font_size
                / BASE_FONT_SIZE
            ),
        )
        # Both side pads are included explicitly. At large font sizes the
        # dot grows substantially; omitting it from the width lets REC escape
        # the badge and the caption card even though the text was reserved.
        width = 7 + dot_size + 4 + font.measure("REC") + 7
        return max(38, width), max(18, line_height + 6, dot_size + 8)

    def draw_recording_tally(
        self,
        *,
        right: int,
        center_y: int,
    ) -> None:
        """Draw the active privacy tally beside the newest preview name."""
        width, height = self.recording_tally_size()
        left = right - width
        top = center_y - height // 2
        rounded_rectangle(
            self.canvas,
            left,
            top,
            left + width,
            top + height,
            max(5, height // 3),
            fill=VOID,
            outline=DANGER,
            tags=("privacy-tally",),
        )
        dot_size = max(
            5,
            round(
                6
                * self.settings.caption_font_size
                / BASE_FONT_SIZE
            ),
        )
        dot_left = left + 7
        dot_top = top + (height - dot_size) // 2
        self.canvas.create_oval(
            dot_left,
            dot_top,
            dot_left + dot_size,
            dot_top + dot_size,
            fill=DANGER,
            outline="",
            tags=("privacy-tally",),
        )
        self.canvas.create_text(
            dot_left + dot_size + 4,
            top + height // 2,
            text="REC",
            fill=DANGER,
            font=self.fonts["status"],
            anchor="w",
            tags=("privacy-tally",),
        )

    def press(self, event: tk.Event) -> None:
        focus = getattr(self.canvas, "focus_set", None)
        if callable(focus):
            focus()
        self.drag_origin = (
            event.x_root,
            event.y_root,
            self.root.winfo_x(),
            self.root.winfo_y(),
        )

    def drag(self, event: tk.Event) -> None:
        if self.drag_origin is None:
            return
        start_x, start_y, window_x, window_y = self.drag_origin
        x = window_x + event.x_root - start_x
        y = window_y + event.y_root - start_y
        self.root.geometry(f"+{x}+{y}")
        self.position_controls((x, y))

    def release(self, _event: tk.Event) -> None:
        self.drag_origin = None

    def press_card(self, event: tk.Event) -> str:
        """Start a drag when the pointer is directly over card text."""
        PlacementWindow.press(self, event)
        return "break"

    def drag_card(self, event: tk.Event) -> str:
        PlacementWindow.drag(self, event)
        return "break"

    def release_card(self, event: tk.Event) -> str:
        PlacementWindow.release(self, event)
        return "break"

    def nudge(self, dx: int, dy: int) -> str:
        x = self.root.winfo_x() + int(dx)
        y = self.root.winfo_y() + int(dy)
        self.root.geometry(f"+{x}+{y}")
        self.position_controls((x, y))
        return "break"

    def key_nudge(self, event: tk.Event, dx: int, dy: int) -> str | None:
        """Let child controls keep their normal editing/navigation keys."""
        if getattr(event, "widget", None) not in {self.root, self.canvas}:
            return None
        return self.nudge(dx, dy)

    def save(self) -> None:
        # Re-read rather than reusing the copy loaded at startup: this
        # window can sit open while the companion's focus toggle changes
        # the file, and saving a position must not undo that.
        current = self.settings if getattr(self, "preview", False) else load_settings()
        desired_metrics, desired_height = preview_surface(
            self.root,
            current,
            self.target_rect,
        )
        current_overlay_width = getattr(
            self,
            "overlay_width",
            getattr(self, "metrics", desired_metrics).canvas_width,
        )
        if (
            desired_height != self.canvas_height
            or desired_metrics.canvas_width != current_overlay_width
        ):
            self.apply_settings(current, reposition=False)
            # apply_settings just issued a geometry() call, and winfo_x/y
            # report the OLD position until the window manager processes
            # it. Reading them stale here saved an anchor up to a few
            # hundred pixels below where the member actually put the
            # window - a Save that quietly moved their captions.
            self.root.update_idletasks()
        else:
            self.settings = current
        overlay_width = getattr(self, "overlay_width", WINDOW_WIDTH)
        settings = settings_from_position(
            self.target_rect,
            (overlay_width, self.canvas_height),
            (self.root.winfo_x(), self.root.winfo_y()),
            current=current,
            bottom_reference_height=BOTTOM_REFERENCE_HEIGHT,
        )
        if getattr(self, "preview", False) or save_settings(settings):
            self.settings = settings
            self.flash("Position saved")
        else:
            self.flash("Could not save")

    def reset(self) -> None:
        current = self.settings if getattr(self, "preview", False) else load_settings()
        reset_settings = replace(
            current,
            anchor_x=DEFAULT_SETTINGS.anchor_x,
            anchor_y=DEFAULT_SETTINGS.anchor_y,
        )
        if not getattr(self, "preview", False) and not save_settings(reset_settings):
            self.flash("Could not restore default")
            return
        self.apply_settings(reset_settings, reposition=True)
        self.flash("Safe default restored")

    def flash(self, text: str) -> None:
        self.saved_label = text
        status = getattr(self, "footer_status", None)
        if status is not None:
            status.config(text=text, fg=FROST)
        elif hasattr(self, "draw"):
            self.draw()
        self.root.after(1300, self.restore_label)

    def restore_label(self) -> None:
        self.saved_label = "Drag the preview or use arrow keys"
        status = getattr(self, "footer_status", None)
        if status is not None:
            status.config(text=self.saved_label, fg=FROST)
        elif hasattr(self, "draw"):
            self.draw()


def main() -> int:
    try:
        root = tk.Tk()
    except tk.TclError:
        return 2
    PlacementWindow(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
