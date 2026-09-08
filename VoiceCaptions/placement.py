"""Interactive placement mode for the otherwise click-through overlay."""

from __future__ import annotations

import ctypes
import sys
import time
import tkinter as tk
from ctypes import wintypes
from dataclasses import replace
from typing import NamedTuple
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
        TARGET_REFRESH_SECONDS,
        VOID,
        canvas_height_for_rows,
        enumerate_caption_monitors,
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
        TARGET_REFRESH_SECONDS,
        VOID,
        canvas_height_for_rows,
        enumerate_caption_monitors,
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

# Only inert chrome drags the captions. A binding on the panel TOPLEVEL
# would fire for a press on every descendant - Button, Menubutton and
# Spinbox class scripts do not return break - so Save, Done, the colour
# swatches and the size spinner would each start a drag. Binding the
# inert widgets instead means a control press has no delivery path to a
# drag handler at all, rather than one that is filtered afterwards.
DRAG_SURFACE_CLASSES = ("Frame", "Label")

# A press that never travels this far is a click, not a drag. Without it
# a shaky click on the "Move captions" heading shoves the overlay a
# couple of pixels and the member has to put it back.
DRAG_THRESHOLD = 3

# The grab layer is transparent by ALPHA, never by colour key. A layered
# window at 1/255 is still hit-tested across its whole area; one at 0 is
# not. Colour-key transparency is the defect being fixed - it makes
# "invisible" and "click-through" the same pixel property.
SHIELD_ALPHA = 1 / 255

DEFAULT_STATUS_TEXT = "Drag the captions or this panel"

# How much of the preview must be on a display for it to count as
# reachable. Less than this and there is nothing a member can grab.
PREVIEW_RESCUE_MARGIN = 24


class PreviewTarget(NamedTuple):
    """The rect the preview is measured against, and where it came from."""

    rect: tuple[int, int, int, int]
    resolved: bool


def preview_target(
    root: tk.Misc,
    settings,
    *,
    probe_rect: tuple[int, int, int, int] | None = None,
    displays: tuple[tuple[int, int, int, int], ...] | None = None,
) -> PreviewTarget:
    """The rectangle the preview must mirror, and whether an anchor gave it.

    A resolved anchor always wins and is returned untouched: a member
    whose game is running already gets a preview sized to their game
    window, and that case is pixel-exact today.

    With nothing resolved - the ordinary case for somebody setting up
    before a raid - this used to hand back the PRIMARY monitor, whatever
    display the member was actually looking at. Shino, 2026-09-08, on a
    1080p secondary: that rect sized the preview, positioned it, and was
    the frame Save measured a dragged position against, so a caption
    dropped on the 1080p was saved as a fraction of the ultrawide and
    redrawn 1413 pixels to the right, off the panel entirely.

    Full monitor BOUNDS, not the work area, for two reasons: on a
    primary display the bounds are bit-identical to the screen rect this
    returned before, so a single-monitor member does not move by a
    pixel; and this is a prediction of a game client rect, which for a
    fullscreen or borderless client is the whole monitor.
    """
    resolved = resolve_anchor_rect(settings)
    if resolved is not None:
        return PreviewTarget(resolved, True)
    screen = (0, 0, root.winfo_screenwidth(), root.winfo_screenheight())
    if probe_rect is None:
        probe_rect = pointer_probe_rect(root)
    if displays is None:
        displays = monitor_display_bounds()
    if probe_rect is None or not displays:
        return PreviewTarget(screen, False)
    return PreviewTarget(
        display_bounds_for_rect(displays, probe_rect, screen), False
    )


def target_rect_for_settings(
    root: tk.Misc,
    settings,
    *,
    probe_rect: tuple[int, int, int, int] | None = None,
    displays: tuple[tuple[int, int, int, int], ...] | None = None,
) -> tuple[int, int, int, int]:
    """The preview's target rectangle. See preview_target for the rule."""
    return preview_target(
        root, settings, probe_rect=probe_rect, displays=displays
    ).rect


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


def caption_font_choices(
    families, current: str
) -> tuple[str, ...]:
    """The families worth offering for a caption, sorted and de-duplicated.

    Windows reports a second, "@"-prefixed copy of every CJK face - the
    same font rotated for vertical writing. They are never what somebody
    picking a caption font wants, and on this machine they were a
    seventh of the list. The member's current family is always kept,
    even if it is one of those or is no longer installed, so opening
    Move can never silently change their font.
    """
    keep = {
        name
        for name in families
        if isinstance(name, str) and name and not name.startswith("@")
    }
    if isinstance(current, str) and current:
        keep.add(current)
    return tuple(sorted(keep, key=str.casefold))


def menu_column_size(
    entries: int, row_height: int, available_height: int
) -> int:
    """How many entries fit in one column before it must break.

    A Tk menu does not scroll. With every family in one column the list
    asked for 4619 pixels on a 1392 pixel work area, so most of it could
    not be reached at all. Breaking into columns keeps every entry on
    screen, which is also how Windows itself shows a long menu.
    """
    if entries <= 0:
        return 1
    usable = max(int(available_height), 1)
    height = max(int(row_height), 1)
    per_column = max(1, usable // height)
    return min(per_column, entries)


def panel_drag_surface(widget_class: str) -> bool:
    """Whether a press on this widget class should move the captions."""
    return widget_class in DRAG_SURFACE_CLASSES


def monitor_work_areas() -> tuple[tuple[int, int, int, int], ...]:
    """Every selectable monitor's WORK area, in the coordinates Tk uses.

    Work area, not full bounds: the panel must not settle behind the
    taskbar. These processes are DPI-unaware, and enumerate_caption_monitors
    already reports the same virtualised space winfo_x/winfo_y do, so no
    scaling is applied to either side of a comparison.
    """
    try:
        return tuple(
            (int(monitor.left), int(monitor.top), int(monitor.width), int(monitor.height))
            for monitor in enumerate_caption_monitors()
        )
    except Exception:  # noqa: BLE001 - a lost monitor list must not lose the panel
        return ()


def monitor_display_bounds() -> tuple[tuple[int, int, int, int], ...]:
    """Every selectable monitor's FULL rectangle, taskbar included.

    The work area is right for placing a panel. This is right for
    predicting where the live overlay will draw, because a fullscreen or
    borderless game client rect is the whole monitor, not the work area.
    Same enumeration, same DPI-unaware coordinates.
    """
    try:
        return tuple(
            (
                int(monitor.display_bounds[0]),
                int(monitor.display_bounds[1]),
                int(monitor.display_bounds[2]),
                int(monitor.display_bounds[3]),
            )
            for monitor in enumerate_caption_monitors()
        )
    except Exception:  # noqa: BLE001 - a lost monitor list must not lose the preview
        return ()


_NO_MATCH = object()


def _best_containing_rect(rects, probe_rect, fallback):
    """The rect a probe is on: largest overlap, then the one holding its centre."""
    if not rects:
        return fallback
    left, top, width, height = probe_rect
    right, bottom = left + width, top + height
    best = None
    best_area = 0
    for area in rects:
        area_left, area_top, area_width, area_height = area
        overlap_width = min(right, area_left + area_width) - max(left, area_left)
        overlap_height = min(bottom, area_top + area_height) - max(top, area_top)
        if overlap_width <= 0 or overlap_height <= 0:
            continue
        covered = overlap_width * overlap_height
        if covered > best_area:
            best, best_area = area, covered
    if best is not None:
        return best
    centre_x, centre_y = left + width // 2, top + height // 2
    for area in rects:
        area_left, area_top, area_width, area_height = area
        if (
            area_left <= centre_x < area_left + area_width
            and area_top <= centre_y < area_top + area_height
        ):
            return area
    return fallback


def work_area_for_rect(
    work_areas: tuple[tuple[int, int, int, int], ...],
    preview_rect: tuple[int, int, int, int],
    fallback: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    """The display the captions are actually on, not the one Move opened on.

    Shino, 2026-09-07: "moving text desyncs the text from the box on
    1080p monitors". Half of that is the panel being clamped into a
    rectangle resolved ONCE when the window opened, while the captions
    move freely - so dragging them to a second monitor stranded the panel
    on the first. Largest overlap wins; a caption straddling two
    displays keeps its panel on the one showing more of it.
    """
    return _best_containing_rect(work_areas, preview_rect, fallback)


def display_bounds_for_rect(
    bounds: tuple[tuple[int, int, int, int], ...],
    probe_rect: tuple[int, int, int, int],
    fallback: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    """The display a probe is on, or the nearest one when it is off them all.

    Monitor bounds tile the desktop and Windows keeps the cursor inside
    their union, so a pointer probe always lands on one. A PREVIEW probe
    need not: a member whose captions were pushed off the desktop by the
    old bug has a preview rect that overlaps nothing. Falling back to the
    primary there would put their window somewhere they are not looking,
    so the nearest display wins instead.
    """
    if not bounds:
        return fallback
    found = _best_containing_rect(bounds, probe_rect, _NO_MATCH)
    if found is not _NO_MATCH:
        return found
    left, top, width, height = probe_rect
    centre_x, centre_y = left + width // 2, top + height // 2
    nearest = None
    nearest_distance = None
    for area in bounds:
        area_left, area_top, area_width, area_height = area
        clamped_x = min(max(centre_x, area_left), area_left + area_width - 1)
        clamped_y = min(max(centre_y, area_top), area_top + area_height - 1)
        distance = (centre_x - clamped_x) ** 2 + (centre_y - clamped_y) ** 2
        if nearest_distance is None or distance < nearest_distance:
            nearest, nearest_distance = area, distance
    return nearest if nearest is not None else fallback


def pointer_probe_rect(root) -> tuple[int, int, int, int] | None:
    """Where the pointer is, as a one-pixel rect, or None if unknowable."""
    reader = getattr(root, "winfo_pointerxy", None)
    if not callable(reader):
        return None
    try:
        pointer_x, pointer_y = reader()
        return (int(pointer_x), int(pointer_y), 1, 1)
    except (tk.TclError, AttributeError, TypeError, ValueError):
        return None


def preview_is_reachable(
    display_bounds: tuple[tuple[int, int, int, int], ...],
    target_rect: tuple[int, int, int, int],
    preview_rect: tuple[int, int, int, int],
    margin: int = PREVIEW_RESCUE_MARGIN,
) -> bool:
    """Whether enough of the preview is on screen to be grabbed."""

    def covers(area) -> bool:
        area_left, area_top, area_width, area_height = area
        left, top, width, height = preview_rect
        overlap_width = min(left + width, area_left + area_width) - max(left, area_left)
        overlap_height = min(top + height, area_top + area_height) - max(top, area_top)
        return overlap_width >= margin and overlap_height >= margin

    # The target first, so a preview sitting where its own anchor put it
    # is reachable without ever consulting the machine's real monitors.
    if covers(target_rect):
        return True
    return any(covers(area) for area in display_bounds)


def rescued_position(
    display_bounds: tuple[tuple[int, int, int, int], ...],
    target_rect: tuple[int, int, int, int],
    overlay_size: tuple[int, int],
    position: tuple[int, int],
) -> tuple[int, int]:
    """Pull an unreachable preview back onto its target, and nothing else.

    A member whose saved fraction was measured against the wrong display
    can have captions parked off the desktop. Their file is not touched
    and their live overlay does not move, but the preview has to be
    somewhere they can grab it. Anything already reachable is returned
    byte-identical, so hanging captions off a screen edge stays legal.
    """
    left, top, width, height = target_rect
    overlay_width, overlay_height = overlay_size
    preview_rect = (position[0], position[1], overlay_width, overlay_height)
    if preview_is_reachable(display_bounds, target_rect, preview_rect):
        return position
    x = min(max(position[0], left), max(left, left + width - overlay_width))
    y = min(max(position[1], top), max(top, top + height - overlay_height))
    return int(x), int(y)


def panel_rigid_position(
    bounds: tuple[int, int, int, int],
    preview_position: tuple[int, int],
    panel_size: tuple[int, int],
    offset: tuple[int, int],
) -> tuple[int, int, bool]:
    """Translate the panel with the captions, by the offset it settled at.

    The other half of the desync. The shipped rule re-derives the panel's
    place from the preview's on every motion frame, and that rule has
    discontinuities: on a 1080p screen the panel flips from below the
    captions to above them the instant they cross one line, jumping 354
    pixels for a one-pixel move, and with a tall caption stack it sits
    frozen for a 264-pixel band and then leaps most of the screen. Held
    rigid, the panel moves exactly as far as the captions did.

    Clamped per axis so the panel cannot be dragged off the display. A
    panel LARGER than the work area pins to the near edge rather than
    inverting.
    """
    left, top, width, height = bounds
    panel_width, panel_height = panel_size
    offset_x, offset_y = offset
    wanted_x = preview_position[0] + offset_x
    wanted_y = preview_position[1] + offset_y
    x = min(max(wanted_x, left), max(left, left + width - panel_width))
    y = min(max(wanted_y, top), max(top, top + height - panel_height))
    return int(x), int(y), (x, y) != (wanted_x, wanted_y)


def make_non_activating(window: tk.Misc) -> bool:
    """Keep the grab layer from ever taking focus.

    It sits over the captions to catch presses, so if it activated, the
    preview root would stop being the active window and the arrow-key
    nudges bound there would go dead after the first drag. Deliberately
    NOT applied to the controls panel: own_control_panel exists to keep
    that panel's buttons activatable.
    """
    if sys.platform != "win32":
        return False
    try:
        window.update_idletasks()
        handle = top_level_handle(window)
        if not handle:
            return False
        user32 = ctypes.windll.user32
        get_style = user32.GetWindowLongPtrW
        get_style.argtypes = [wintypes.HWND, ctypes.c_int]
        get_style.restype = ctypes.c_ssize_t
        set_style = user32.SetWindowLongPtrW
        set_style.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        set_style.restype = ctypes.c_ssize_t
        gwl_exstyle = -20
        ws_ex_noactivate = 0x08000000
        current = get_style(handle, gwl_exstyle)
        set_style(handle, gwl_exstyle, current | ws_ex_noactivate)
        return True
    except Exception:  # noqa: BLE001 - a focus nicety must never stop Move opening
        return False


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
        # A press is a click until it travels DRAG_THRESHOLD.
        self.drag_moved = False
        # The work area resolved once per press, so a full monitor
        # enumeration never runs inside a motion handler.
        self.drag_bounds: tuple[int, int, int, int] | None = None
        # The panel's place relative to the captions, latched when it
        # settles and translated rigidly while they move.
        self.panel_offset: tuple[int, int] | None = None
        self.panel_size: tuple[int, int] | None = None
        self.shield = None
        self.shield_ready = False
        self.shield_owned = False
        self.shield_non_activating = False
        self.chroma_transparent = False
        self.work_areas = monitor_work_areas()
        self.display_bounds = monitor_display_bounds()
        # Set only by a drag or a nudge. Save re-derives the anchor only
        # when it is True, so opening Move and pressing Save cannot move
        # captions the member never touched.
        self.position_dirty = False
        self.last_target_check = 0.0
        self.saved_label = DEFAULT_STATUS_TEXT
        self.settings_path = None if self.preview else default_settings_path()
        self.settings_mtime_ns = -1
        self.settings = (
            DEFAULT_SETTINGS
            if self.preview
            else load_settings(self.settings_path)
        )
        # No geometry() has been issued yet, so winfo_x/y are (0, 0) -
        # the primary's origin by definition, and useless as a probe.
        # The pointer is where the member is actually looking.
        _target = preview_target(
            root, self.settings, displays=self.display_bounds
        )
        self.target_rect = _target.rect
        self.target_resolved = _target.resolved
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
            # Recorded because the grab layer is only needed - and only
            # correct - where colour-key transparency actually applied.
            self.chroma_transparent = True
        except tk.TclError:
            self.chroma_transparent = False

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
        # After the footer, so the panel starts above the grab layer.
        self.build_shield()

        overlay_x, y = position_from_settings(
            self.target_rect,
            (self.overlay_width, self.canvas_height),
            self.settings,
            bottom_reference_height=BOTTOM_REFERENCE_HEIGHT,
        )
        x, y = rescued_position(
            self.display_bounds,
            self.target_rect,
            (self.overlay_width, self.canvas_height),
            (overlay_x, y),
        )
        self.place_preview(x, y, resize=True, settle=True)
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

    def preview_rect(self) -> tuple[int, int, int, int]:
        return (
            int(self.root.winfo_x()),
            int(self.root.winfo_y()),
            int(self.overlay_width),
            int(self.canvas_height),
        )

    def adopt_target(self, target) -> None:
        self.target_rect = target.rect
        self.target_resolved = target.resolved

    def refresh_target(self) -> None:
        """Re-resolve the target on the cadence the live overlay uses.

        The live overlay re-fits to its game window every
        TARGET_REFRESH_SECONDS; the preview used to resolve once, when
        Move opened, and then claim to be showing what the overlay would
        draw. Launching the game with Move open, or dropping the preview
        on another display, now re-fits it the same way.

        Never while a pointer is held: resizing and re-placing the
        window under the member's hand at the exact moment they are
        putting it somewhere is worse than being briefly stale.
        """
        if getattr(self, "drag_origin", None) is not None:
            return
        now = time.monotonic()
        if now - getattr(self, "last_target_check", 0.0) < TARGET_REFRESH_SECONDS:
            return
        self.last_target_check = now
        try:
            probe = self.preview_rect()
        except tk.TclError:
            return
        self.display_bounds = monitor_display_bounds() or self.display_bounds
        target = preview_target(
            self.root,
            self.settings,
            probe_rect=probe,
            displays=self.display_bounds,
        )
        if (
            target.rect == self.target_rect
            and target.resolved == getattr(self, "target_resolved", False)
        ):
            return
        self.adopt_target(target)
        # An untouched preview re-places to where the overlay will
        # actually draw. One the member has dragged keeps their pixel.
        self.apply_settings(
            self.settings,
            reposition=not getattr(self, "position_dirty", False),
        )

    def update_control_bounds(self) -> None:
        """Re-measure and re-anchor controls after size or DPI changes."""
        self.position_controls()

    def position_controls(
        self,
        preview_position: tuple[int, int] | None = None,
        *,
        settle: bool = False,
    ) -> None:
        """The only writer of the panel's geometry.

        Two modes. SETTLING re-measures the panel, solves its place from
        scratch and latches the offset; that happens when Move opens, when
        the preview is resized, when it is re-shown, and once at the end
        of a drag. TRACKING translates by the latched offset and touches
        nothing else - the panel keeps its place relative to the captions
        instead of being re-derived across a rule with cliffs in it.

        A settle is downgraded to a track whenever a drag is live, so no
        caller can re-solve under the pointer: not the 250 ms settings
        poll, not a live appearance change, not a resize.
        """
        controls = getattr(self, "controls", None)
        if controls is None:
            return
        settling = settle and getattr(self, "drag_origin", None) is None
        try:
            if preview_position is None:
                preview_position = (
                    int(self.root.winfo_x()),
                    int(self.root.winfo_y()),
                )
            preview = (
                int(preview_position[0]),
                int(preview_position[1]),
                int(self.overlay_width),
                int(self.canvas_height),
            )
            if (
                settling
                or self.panel_offset is None
                or self.panel_size is None
            ):
                controls.update_idletasks()
                self.panel_size = (
                    max(1, int(controls.winfo_reqwidth())),
                    max(1, int(controls.winfo_reqheight())),
                )
                bounds = work_area_for_rect(
                    self.work_areas, preview, self.target_rect
                )
                panel_x, panel_y = control_panel_position(
                    bounds, preview, self.panel_size
                )
                self.panel_offset = (
                    panel_x - preview[0],
                    panel_y - preview[1],
                )
                controls.geometry(
                    "%dx%d+%d+%d"
                    % (self.panel_size[0], self.panel_size[1], panel_x, panel_y)
                )
                controls.lift()
                return
            bounds = self.drag_bounds or work_area_for_rect(
                self.work_areas, preview, self.target_rect
            )
            # The offset is NOT refreshed from a clamped result: if it
            # were, repeated bumps against a screen edge would drift it
            # and the panel would never come back to its true place.
            panel_x, panel_y, _clamped = panel_rigid_position(
                bounds, (preview[0], preview[1]), self.panel_size,
                self.panel_offset,
            )
            # No update_idletasks and no lift here on purpose: this runs
            # inside a motion handler, and both pump the event loop.
            controls.geometry(
                "%dx%d+%d+%d"
                % (self.panel_size[0], self.panel_size[1], panel_x, panel_y)
            )
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
                shield = getattr(self, "shield", None)
                if shield is not None:
                    try:
                        self.sync_shield()
                        shield.deiconify()
                        shield.lift()
                    except tk.TclError:
                        pass
                self.position_controls(settle=True)
                controls.deiconify()
                # Last, so the panel is above the grab layer wherever the
                # two overlap and Save still takes a click.
                controls.lift()
            else:
                shield = getattr(self, "shield", None)
                if shield is not None:
                    try:
                        shield.withdraw()
                    except tk.TclError:
                        pass
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
        self.heading_label = tk.Label(
            top,
            text="Move captions",
            bg=SLATE,
            fg=TEXT,
            font=self.fonts["control"],
            anchor="w",
        )
        self.heading_label.pack(side="left")
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
        self.font_label = tk.Label(
            appearance,
            text="Font",
            bg=SLATE,
            fg=MUTED,
            font=self.fonts["control"],
        )
        self.font_label.pack(side="left", padx=(0, 5))
        self.font_families = caption_font_choices(
            tkfont.families(self.root), self.settings.caption_font_family
        )
        self.font_family_var = tk.StringVar(
            value=self.settings.caption_font_family
        )
        self.font_family_menu = tk.OptionMenu(
            appearance,
            self.font_family_var,
            *self.font_families,
            command=self.set_font_family,
        )
        self.style_option_menu(self.font_family_menu, width=19)
        self.break_font_menu_into_columns()
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
        self.bind_panel_drag()

    def bind_panel_drag(self) -> None:
        """Give the inert chrome of the panel the drag, and nothing else.

        Shino, 2026-09-07: "allow text to be moved by dragging the Font
        box". The box he means is this one, and it had no pointer binding
        at all. Binding the TOPLEVEL would have been one line and wrong:
        a press on any descendant reaches it, because Button, Menubutton
        and Spinbox class scripts do not return break, so Save, Done, the
        colour swatches and the size spinner would each start a drag.
        Binding the inert widgets instead means a control press never
        reaches a drag handler in the first place.
        """
        controls = getattr(self, "controls", None)
        if controls is None:
            return
        pending = list(controls.winfo_children())
        while pending:
            widget = pending.pop()
            try:
                pending.extend(widget.winfo_children())
                if not panel_drag_surface(widget.winfo_class()):
                    continue
                widget.bind("<ButtonPress-1>", self.panel_press)
                widget.bind("<B1-Motion>", self.panel_drag)
                widget.bind("<ButtonRelease-1>", self.panel_release)
                widget.configure(cursor="fleur")
            except tk.TclError:
                continue

    def panel_press(self, event: tk.Event) -> str | None:
        # Checked again at dispatch, so a widget bound later by someone
        # else cannot quietly become a drag handle.
        try:
            widget_class = event.widget.winfo_class()
        except (AttributeError, tk.TclError):
            return None
        if not panel_drag_surface(widget_class):
            return None
        # Deliberately no canvas.focus_set: a press on the panel must not
        # pull focus out of a half-typed size, whose FocusOut commits it.
        self.begin_drag(event)
        return "break"

    def panel_drag(self, event: tk.Event) -> str | None:
        if self.drag_origin is None:
            return None
        self.drag(event)
        return "break"

    def panel_release(self, event: tk.Event) -> str | None:
        if self.drag_origin is None:
            return None
        self.release(event)
        return "break"

    def build_shield(self) -> None:
        """A hit area over the captions that no colour key can punch through.

        The card backdrop is drawn with a stipple, and the gaps in that
        stipple ARE the chroma key, which Windows treats as absent: at
        the default 50 percent backdrop about half the card's pixels take
        a press straight through to the game. No drawn item can fix that
        without making the Backdrop control lie, so the grab surface has
        to be a separate window that is invisible by ALPHA instead.

        Only built where colour-key transparency actually applied, and if
        anything about it fails it is destroyed rather than left over the
        raid as a near-opaque slab.
        """
        self.shield = None
        self.shield_ready = False
        if not self.chroma_transparent:
            return
        shield = None
        try:
            shield = tk.Toplevel(self.root)
            shield.withdraw()
            shield.title("Move captions grab layer")
            shield.overrideredirect(True)
            # Never CHROMA_KEY, and never -transparentcolor: either one
            # silently restores the whole defect this exists to fix.
            shield.configure(bg=VOID, cursor="fleur")
            shield.attributes("-alpha", SHIELD_ALPHA)
        except tk.TclError:
            if shield is not None:
                try:
                    shield.destroy()
                except tk.TclError:
                    pass
            self.shield = None
            self.shield_ready = False
            return
        self.shield = shield
        self.shield_owned = own_control_panel(shield, self.root)
        # After own_control_panel, which recreates the wrapper window and
        # would discard an ex-style set before it.
        self.shield_non_activating = make_non_activating(shield)
        shield.bind("<ButtonPress-1>", self.press)
        shield.bind("<B1-Motion>", self.drag)
        shield.bind("<ButtonRelease-1>", self.release)
        shield.bind("<Escape>", lambda _event: self.root.destroy())
        shield.protocol("WM_DELETE_WINDOW", self.root.destroy)
        self.shield_ready = True

    def sync_shield(self, x: int | None = None, y: int | None = None) -> None:
        shield = getattr(self, "shield", None)
        if shield is None:
            return
        try:
            if x is None or y is None:
                x, y = int(self.root.winfo_x()), int(self.root.winfo_y())
            shield.geometry(
                "%dx%d+%d+%d"
                % (self.window_width, self.window_height, int(x), int(y))
            )
            # Not while a drag is live: lifting pumps the event loop from
            # inside a motion handler.
            if self.drag_origin is None:
                shield.lift()
        except tk.TclError:
            return

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

    def break_font_menu_into_columns(self) -> None:
        """Wrap the family list so the whole of it is reachable.

        The row height is measured from the control font rather than
        assumed, and the space available is the height of the display
        the panel is on - not the primary, and not the full monitor,
        so the list never runs under the taskbar either.
        """
        menu = getattr(self, "font_family_menu", None)
        if menu is None:
            return
        try:
            entries = len(self.font_families)
            row_height = self.fonts["control"].metrics("linespace") + 6
            bounds = work_area_for_rect(
                getattr(self, "work_areas", ()) or monitor_work_areas(),
                (
                    int(self.target_rect[0]),
                    int(self.target_rect[1]),
                    int(self.target_rect[2]),
                    int(self.target_rect[3]),
                ),
                self.target_rect,
            )
            per_column = menu_column_size(entries, row_height, bounds[3])
            inner = menu["menu"]
            for index in range(per_column, entries, per_column):
                inner.entryconfigure(index, columnbreak=1)
        except (tk.TclError, KeyError, AttributeError, IndexError):
            return

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
        if not self.preview:
            # Record what we just wrote. Without this the 250 ms poll
            # sees its own change as somebody else's and re-enters
            # apply_settings within a quarter second of every appearance
            # change - which, before the drag rebase, also killed a live
            # drag. Written inline rather than through a helper: the
            # test subject is a namespace with neither.
            try:
                self.settings_mtime_ns = self.settings_path.stat().st_mtime_ns
            except OSError:
                pass
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
        if rewidth:
            self.canvas.configure(width=self.window_width)
        if reposition:
            overlay_x, y = position_from_settings(
                self.target_rect,
                (self.overlay_width, self.canvas_height),
                updated,
                bottom_reference_height=BOTTOM_REFERENCE_HEIGHT,
            )
            x, y = rescued_position(
                getattr(self, "display_bounds", ()),
                self.target_rect,
                (self.overlay_width, self.canvas_height),
                (overlay_x, y),
            )
        else:
            if rewidth or overlay_rewidth:
                # Keep the caption surface centred on the member's drag.
                x += (old_overlay_width - desired_overlay_width) // 2
            if resized and self.settings.growth != "down":
                # Preserve the dragged caption baseline while the stack grows.
                y += old_height - desired_height
        if reposition or resized or rewidth or overlay_rewidth:
            self.place_preview(x, y, resize=True, settle=True)
        else:
            self.sync_shield(x, y)
            self.position_controls((x, y), settle=True)
        # After the geometry write, so the pointer and the recorded
        # origin belong to the same instant. update_control_bounds above
        # runs before it and reads winfo_x/y one frame stale; this call
        # corrects that inside the same function.
        self.rebase_drag(x, y)
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
                self.adopt_target(
                    preview_target(
                        self.root,
                        updated,
                        probe_rect=self.preview_rect(),
                        displays=self.display_bounds,
                    )
                )
            self.apply_settings(updated, reposition=target_changed)
            sync_controls = getattr(self, "sync_appearance_controls", None)
            if callable(sync_controls):
                sync_controls()
        self.refresh_target()
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

    def place_preview(
        self,
        x: int,
        y: int,
        *,
        resize: bool = False,
        settle: bool = False,
    ) -> None:
        """The only writer of the preview root's geometry.

        The captions, the grab layer over them and the controls panel are
        one assembly. Moving them separately is what let them come apart,
        so nothing moves one alone.
        """
        x, y = int(x), int(y)
        if resize:
            self.root.geometry(
                "%dx%d+%d+%d" % (self.window_width, self.window_height, x, y)
            )
        else:
            self.root.geometry("+%d+%d" % (x, y))
        self.sync_shield(x, y)
        self.position_controls((x, y), settle=settle)

    def begin_drag(self, event: tk.Event) -> None:
        """Start a gesture, whichever surface it was started from."""
        self.drag_origin = (
            event.x_root,
            event.y_root,
            self.root.winfo_x(),
            self.root.winfo_y(),
        )
        self.drag_moved = False
        # Enumerated once per press, never inside a motion handler.
        self.work_areas = monitor_work_areas() or getattr(
            self, "work_areas", ()
        )
        self.drag_bounds = work_area_for_rect(
            self.work_areas,
            (
                int(self.root.winfo_x()),
                int(self.root.winfo_y()),
                int(self.overlay_width),
                int(self.canvas_height),
            ),
            self.target_rect,
        )

    def press(self, event: tk.Event) -> None:
        focus = getattr(self.canvas, "focus_set", None)
        if callable(focus):
            focus()
        self.begin_drag(event)

    def drag(self, event: tk.Event) -> None:
        if self.drag_origin is None:
            return
        start_x, start_y, window_x, window_y = self.drag_origin
        delta_x = event.x_root - start_x
        delta_y = event.y_root - start_y
        if (
            not getattr(self, "drag_moved", False)
            and max(abs(delta_x), abs(delta_y)) < DRAG_THRESHOLD
        ):
            return
        # The FULL delta once it trips, never delta minus the threshold,
        # or the captions trail the pointer by three pixels forever.
        self.drag_moved = True
        self.position_dirty = True
        self.place_preview(window_x + delta_x, window_y + delta_y)

    def release(self, _event: tk.Event) -> None:
        self.drag_origin = None
        self.drag_bounds = None
        self.drag_moved = False
        # The one settle point, at a moment when nothing is held.
        self.position_controls(settle=True)
        # A drop on another display re-fits now rather than up to a
        # rate-limit later. Guarded because the test harness drives
        # release() on a stand-in that has no target tracking.
        refresh = getattr(self, "refresh_target", None)
        if callable(refresh):
            self.last_target_check = -TARGET_REFRESH_SECONDS
            refresh()

    def rebase_drag(self, x: int, y: int) -> None:
        """Keep a live drag alive across a resize instead of dropping it.

        A live appearance change used to clear drag_origin, so the
        captions silently stopped following the pointer - one of the
        shapes a member experiences as the box coming unstuck. BOTH
        halves are replaced: keeping the press-time pointer and moving
        only the window origin would replay the whole accumulated delta
        on the next motion and throw the captions across the screen.
        """
        if self.drag_origin is None:
            return
        try:
            pointer_x, pointer_y = self.root.winfo_pointerxy()
        except (tk.TclError, AttributeError):
            self.drag_origin = None
            self.drag_moved = False
            return
        self.drag_origin = (int(pointer_x), int(pointer_y), int(x), int(y))

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
        self.position_dirty = True
        self.place_preview(x, y)
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
        if getattr(self, "position_dirty", False):
            overlay_width = getattr(self, "overlay_width", WINDOW_WIDTH)
            settings = settings_from_position(
                self.target_rect,
                (overlay_width, self.canvas_height),
                (self.root.winfo_x(), self.root.winfo_y()),
                current=current,
                bottom_reference_height=BOTTOM_REFERENCE_HEIGHT,
            )
        else:
            # The member did not move the captions this session, so the
            # anchor they saved before is still what they meant. Today a
            # Save with no drag re-derives it from pixels and flips
            # growth - measured, growth "down" with anchor_y 0.10 came
            # back as "up" with 0.918056, moving the live overlay 47 px
            # on a 1080p. Opening Move must not be able to move
            # anybody's captions.
            settings = current
        if getattr(self, "preview", False) or save_settings(settings):
            self.settings = settings
            self.position_dirty = False
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
        self.position_dirty = False
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
        self.saved_label = DEFAULT_STATUS_TEXT
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
