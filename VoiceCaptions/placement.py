"""Interactive placement mode for the otherwise click-through overlay."""

from __future__ import annotations

import sys
import tkinter as tk
from dataclasses import replace
from pathlib import Path
from tkinter import font as tkfont

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from VoiceCaptions.layout import (  # type: ignore
        BASE_FONT_SIZE,
        DEFAULT_METRICS,
        clipped_text,
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
        canvas_height_on_screen,
        resolve_anchor_rect,
        rounded_rectangle,
    )
    from VoiceCaptions.settings import (  # type: ignore
        DEFAULT_SETTINGS,
        default_settings_path,
        load_settings,
        position_from_settings,
        save_settings,
        settings_from_position,
    )
else:
    from .layout import BASE_FONT_SIZE, DEFAULT_METRICS, clipped_text
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
        canvas_height_on_screen,
        resolve_anchor_rect,
        rounded_rectangle,
    )
    from .settings import (
        DEFAULT_SETTINGS,
        default_settings_path,
        load_settings,
        position_from_settings,
        save_settings,
        settings_from_position,
    )

WINDOW_WIDTH = DEFAULT_METRICS.canvas_width
FOOTER_GAP = 6
FOOTER_HEIGHT = 90
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
        self.canvas_height = canvas_height_on_screen(
            root,
            self.settings.max_visible_speakers,
            self.settings.caption_font_size,
        )
        self.footer_top = self.canvas_height + FOOTER_GAP
        self.window_height = self.footer_top + FOOTER_HEIGHT
        self.target_rect = target_rect_for_settings(root, self.settings)

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
            width=WINDOW_WIDTH,
            height=self.canvas_height,
            bg=CHROMA_KEY,
            highlightthickness=0,
            borderwidth=0,
            cursor="fleur",
        )
        self.canvas.pack(fill="x")
        self.fonts = self.preview_fonts(self.settings.caption_font_size)
        self.build_footer()

        x, y = position_from_settings(
            self.target_rect,
            (WINDOW_WIDTH, self.canvas_height),
            self.settings,
            bottom_reference_height=BOTTOM_REFERENCE_HEIGHT,
        )
        root.geometry(f"{WINDOW_WIDTH}x{self.window_height}+{x}+{y}")
        self.draw()

        self.canvas.bind("<ButtonPress-1>", self.press)
        self.canvas.bind("<B1-Motion>", self.drag)
        self.canvas.bind("<ButtonRelease-1>", self.release)
        root.bind("<Escape>", lambda _event: root.destroy())
        root.bind("<Left>", lambda _event: self.nudge(-1, 0))
        root.bind("<Right>", lambda _event: self.nudge(1, 0))
        root.bind("<Up>", lambda _event: self.nudge(0, -1))
        root.bind("<Down>", lambda _event: self.nudge(0, 1))
        root.bind("<Shift-Left>", lambda _event: self.nudge(-10, 0))
        root.bind("<Shift-Right>", lambda _event: self.nudge(10, 0))
        root.bind("<Shift-Up>", lambda _event: self.nudge(0, -10))
        root.bind("<Shift-Down>", lambda _event: self.nudge(0, 10))
        root.focus_force()
        if not self.preview:
            root.after(250, self.refresh_settings)

    @staticmethod
    def preview_fonts(size: int) -> dict:
        """Preview text scaled like the overlay's, so sizes read true.

        The control font is deliberately NOT scaled. Placement controls are
        real Tk buttons, so they retain readable labels and keyboard focus
        without growing alongside the combat captions.
        """
        factor = size / BASE_FONT_SIZE

        def px(base: int) -> int:
            return max(1, round(base * factor))

        return {
            "speaker": tkfont.Font(family="Segoe UI Variable", size=px(9), weight="bold"),
            "caption": tkfont.Font(family="Segoe UI Variable", size=px(10)),
            "previous": tkfont.Font(family="Segoe UI Variable", size=px(9)),
            "status": tkfont.Font(family="Segoe UI Variable", size=px(8), weight="bold"),
            "control": tkfont.Font(family="Segoe UI Variable", size=9, weight="bold"),
        }

    def update_control_bounds(self) -> None:
        """Compatibility hook retained for layout extensions.

        Controls are native widgets now, so their hit regions are owned by Tk
        and automatically follow DPI scaling.
        """

    def build_footer(self) -> None:
        shell = tk.Frame(
            self.root,
            bg="#2a3c52",
            height=FOOTER_HEIGHT,
        )
        shell.pack(fill="x", pady=(FOOTER_GAP, 0))
        shell.pack_propagate(False)
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
        """Resize once for a deliberate row or font change, keeping baseline."""
        old_height = self.canvas_height
        desired_height = canvas_height_on_screen(
            self.root,
            updated.max_visible_speakers,
            updated.caption_font_size,
        )
        x = self.root.winfo_x()
        y = self.root.winfo_y()
        resized = desired_height != old_height
        refonted = updated.caption_font_size != self.settings.caption_font_size
        self.settings = updated
        if refonted:
            self.fonts = self.preview_fonts(updated.caption_font_size)
        if resized:
            self.canvas_height = desired_height
            self.footer_top = self.canvas_height + FOOTER_GAP
            self.window_height = self.footer_top + FOOTER_HEIGHT
            self.update_control_bounds()
            self.canvas.configure(height=self.canvas_height)
            self.drag_origin = None
        if reposition:
            x, y = position_from_settings(
                self.target_rect,
                (WINDOW_WIDTH, self.canvas_height),
                updated,
                bottom_reference_height=BOTTOM_REFERENCE_HEIGHT,
            )
        elif resized and self.settings.growth != "down":
            # A Save that beats the polling timer still honors the new row
            # count. Grow upward from the preview's current caption
            # baseline - unless the feed hangs, in which case the top edge
            # is the stable line and y stays exactly where it is.
            y += old_height - desired_height
        if reposition or resized:
            self.root.geometry(
                f"{WINDOW_WIDTH}x{self.window_height}+{x}+{y}"
            )
        if resized or refonted:
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
        # cards is the mistake this window exists to prevent. The window's
        # WIDTH stays fixed: its footer buttons are laid out against it.
        metrics = DEFAULT_METRICS.for_font_size(
            self.settings.caption_font_size
        ).fitted_to_width(WINDOW_WIDTH)
        examples = (
            (330, "Tari", "Adds are spawning.", "#a330c9"),
            (370, "Lena", "Move out, then back in.", "#fff468"),
            (360, "Drex", "Boss turning left.", "#4b8fe8"),
            (430, "Mira", "Spread for circles, then stack on me.", "#4fd182"),
        )
        # More rows than there are examples: repeat them so the preview
        # shows the real count rather than silently capping at four.
        wanted = max(1, self.settings.max_visible_speakers)
        shown = [examples[index % len(examples)] for index in range(wanted)]
        heights = (
            [metrics.final_height] * max(len(shown) - 1, 0)
            + [metrics.live_height]
        )
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
        for index, (width, speaker, text, color) in enumerate(shown):
            live = index == len(shown) - 1
            height = heights[index]
            self.draw_caption(
                y1=cursor_y,
                height=height,
                width=min(width, metrics.max_card_width),
                speaker=speaker,
                text=text,
                color=color,
                live=live,
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
    ) -> None:
        x1 = (WINDOW_WIDTH - width) // 2
        x2, y2 = x1 + width, y1 + height
        rounded_rectangle(
            self.canvas,
            x1,
            y1,
            x2,
            y2,
            8,
            fill=SLATE_LIVE if live else VOID,
            outline=FROST if live else SLATE,
        )
        self.canvas.create_rectangle(
            x1 + 1, y1 + 6, x1 + 4, y2 - 6, fill=color, outline=""
        )
        # Proportional to the card rather than fixed, so the initial does
        # not sit as a dot in the corner of a large-font row.
        name_x = x1 + 14
        text_y = y1 + height // 2
        self.canvas.create_text(
            name_x,
            text_y,
            text=speaker,
            fill=TEXT if live else MUTED,
            font=self.fonts["speaker"],
            anchor="w",
        )
        name_column = min(
            round(96 * self.settings.caption_font_size / BASE_FONT_SIZE),
            width // 3,
        )
        caption_x = name_x + name_column + 10
        tally_width = self.recording_tally_size()[0] if live else 0
        reserve = tally_width + 12 if live else 9
        available = max(x2 - caption_x - reserve, 0)
        caption_font = self.fonts["caption"] if live else self.fonts["previous"]
        self.canvas.create_text(
            caption_x,
            text_y,
            text=clipped_text(text, available, caption_font.measure),
            fill=TEXT if live else MUTED,
            font=caption_font,
            anchor="w",
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
        )
        self.canvas.create_text(
            dot_left + dot_size + 4,
            top + height // 2,
            text="REC",
            fill=DANGER,
            font=self.fonts["status"],
            anchor="w",
        )

    def press(self, event: tk.Event) -> None:
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

    def release(self, _event: tk.Event) -> None:
        self.drag_origin = None

    def nudge(self, dx: int, dy: int) -> str:
        x = self.root.winfo_x() + int(dx)
        y = self.root.winfo_y() + int(dy)
        self.root.geometry(f"+{x}+{y}")
        return "break"

    def save(self) -> None:
        # Re-read rather than reusing the copy loaded at startup: this
        # window can sit open while the companion's focus toggle changes
        # the file, and saving a position must not undo that.
        current = self.settings if getattr(self, "preview", False) else load_settings()
        desired_height = canvas_height_on_screen(
            self.root,
            current.max_visible_speakers,
            current.caption_font_size,
        )
        if desired_height != self.canvas_height:
            self.apply_settings(current, reposition=False)
            # apply_settings just issued a geometry() call, and winfo_x/y
            # report the OLD position until the window manager processes
            # it. Reading them stale here saved an anchor up to a few
            # hundred pixels below where the member actually put the
            # window - a Save that quietly moved their captions.
            self.root.update_idletasks()
        else:
            self.settings = current
        settings = settings_from_position(
            self.target_rect,
            (WINDOW_WIDTH, self.canvas_height),
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
