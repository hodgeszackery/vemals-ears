"""Click-through, raid-safe caption overlay for World of Warcraft.

The window never injects into or reads memory from the game. It only finds
the visible WoW window rectangle through normal Win32 window APIs so the
captions stay attached to the game when it moves between monitors.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import queue
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import PurePath
from tkinter import font as tkfont

if __package__ in {None, ""}:
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from VoiceCaptions.events import (  # type: ignore
        MAX_OVERLAY_ROWS,
        CaptionBoard,
        CaptionEvent,
        CaptionProtocolError,
        ClearEvent,
        ProtocolEvent,
        SessionEvent,
        parse_event_line,
    )
    from VoiceCaptions.layout import (  # type: ignore
        BASE_FONT_SIZE,
        DEFAULT_METRICS,
        background_stipple,
        canvas_height_for_target,
        caption_font_specs,
        clipped_text,
        metrics_for_target,
        wrap_text,
    )
    from VoiceCaptions.settings import (  # type: ignore
        MAX_ANCHOR_MONITOR,
        default_settings_path,
        load_settings,
        position_from_settings,
    )
    from VoiceCaptions.speaker_directory import (  # type: ignore
        load_known_speakers,
        remember_speaker,
    )
else:
    from .events import (
        MAX_OVERLAY_ROWS,
        CaptionBoard,
        CaptionEvent,
        CaptionProtocolError,
        ClearEvent,
        ProtocolEvent,
        SessionEvent,
        parse_event_line,
    )
    from .layout import (
        BASE_FONT_SIZE,
        DEFAULT_METRICS,
        background_stipple,
        canvas_height_for_target,
        caption_font_specs,
        clipped_text,
        metrics_for_target,
        wrap_text,
    )
    from .settings import (
        MAX_ANCHOR_MONITOR,
        default_settings_path,
        load_settings,
        position_from_settings,
    )
    from .speaker_directory import load_known_speakers, remember_speaker


# Compact token set shared with the existing companion. The chroma key is
# deliberately outside the palette and becomes fully transparent on Windows.
CHROMA_KEY = "#010203"
VOID = "#090b11"
SLATE = "#11161d"
SLATE_LIVE = "#151c25"
FROST = "#42b8f2"
# High-contrast chrome and the default caption text colour. Caption copy
# reads the persisted appearance setting; privacy/status UI stays fixed.
TEXT = "#ffffff"
# The accent placement mode draws its saved-state marker in.
CLOCKFIRE = "#ffab3d"
MUTED = "#879cb3"
DANGER = "#e85c61"

GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
WS_EX_APPWINDOW = 0x00040000
LWA_COLORKEY = 0x00000001
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
HWND_TOPMOST = -1
MONITORINFOF_PRIMARY = 0x00000001
DISPLAY_DEVICE_MIRRORING_DRIVER = 0x00000008

DEFAULT_MAX_ROWS = 2
MAX_ROWS = MAX_OVERLAY_ROWS
MAX_PICKER_WINDOW_TITLE_LENGTH = 512


# Sized once for the tallest stack the board can produce, not resized per
# frame. It may resize when the member deliberately changes their visible
# speaker limit or font size, but never because a caption wrapped during a
# pull. The height scales with the caption font size, so a bigger font is a
# taller overlay rather than clipped rows.
def canvas_height_for_rows(
    max_rows: int, font_size: int = BASE_FONT_SIZE
) -> int:
    if max_rows < 1 or max_rows > MAX_ROWS:
        raise ValueError(
            "max_rows must be between 1 and %d" % MAX_ROWS
        )
    return DEFAULT_METRICS.for_font_size(font_size).tallest_stack(max_rows)


def canvas_height_on_screen(root, max_rows: int, font_size: int = BASE_FONT_SIZE) -> int:
    """The wanted height, capped at the display it has to fit on.

    A large font and many rows can want more height than any monitor has.
    Rather than run off the top, the stack is capped at the screen and
    drawn bottom-anchored, so the newest speakers - nearest the action
    bars - always show and only the oldest scrollback is lost. That is the
    honest failure for a setting somebody pushed past what fits.

    Shared by the overlay and the Move preview so the preview shows the
    footprint the member will actually get.
    """
    canvas_height_for_rows(max_rows, font_size)
    try:
        available = int(root.winfo_screenheight())
    except tk.TclError:
        # The window is going away; an uncapped height is the old
        # behaviour and harmless for a frame nobody will see.
        available = None
    return canvas_height_for_target(
        max_rows,
        font_size,
        None,
        fallback_height=available,
    )


CANVAS_HEIGHT = canvas_height_for_rows(DEFAULT_MAX_ROWS)
TICK_MS = 40
TARGET_REFRESH_SECONDS = 0.75
_EOF = object()

# A File Explorer window opened on the install folder is titled exactly
# "World of Warcraft", and maximized on a wide monitor it is larger than a
# windowed client, so the title alone cannot pick the anchor window. The owning
# process settles it. Matching on window class instead would be fragile: retail
# 12.1.0 reports "waApplication Window", not the GxWindowClass the game used
# historically, and a class this code does not know about must never cost the
# player their captions.
WOW_PROCESS_NAMES = frozenset(
    {"wow.exe", "wowclassic.exe", "wowt.exe", "wowb.exe"}
)

# The auto-detect table: (title fragment, executable basenames), scanned
# in order. One row today, and that is deliberate - a row is added when
# somebody has actually verified the game's window title and image names,
# not speculatively. The chosen-window mode covers every game this table
# does not, and a member's choice teaches the operator which rows are
# worth adding.
GAME_PROFILES = (
    ("world of warcraft", WOW_PROCESS_NAMES),
)

# Enough to read another process's image name without asking for the rights a
# debugger would need, which the game's anti-cheat would rightly object to.
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _MONITORINFOEXW(ctypes.Structure):
    """The monitor record that includes a stable Windows display device."""

    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", ctypes.c_wchar * 32),
    ]


class _DISPLAY_DEVICEW(ctypes.Structure):
    """EnumDisplayDevicesW record used to exclude mirror pseudo-displays."""

    _fields_ = [
        ("cb", wintypes.DWORD),
        ("DeviceName", ctypes.c_wchar * 32),
        ("DeviceString", ctypes.c_wchar * 128),
        ("StateFlags", wintypes.DWORD),
        ("DeviceID", ctypes.c_wchar * 128),
        ("DeviceKey", ctypes.c_wchar * 128),
    ]


@dataclass(frozen=True)
class CaptionTargetChoice:
    """One short-lived picker choice mapped to the anchor settings contract."""

    mode: str = "auto"
    executable: str = ""
    monitor_index: int = 0


@dataclass(frozen=True)
class CaptionWindowCandidate:
    """A currently-visible application choice for the caption display.

    ``title`` is presentation-only.  The durable target deliberately keeps
    only executable basename, so it remains valid after the application
    recreates its window and never writes a document or character name into
    settings.
    """

    executable: str
    title: str

    @property
    def target(self) -> CaptionTargetChoice:
        return CaptionTargetChoice("window", self.executable)

    @property
    def label(self) -> str:
        title = _display_title(self.title)
        if title:
            return f"{title} ({self.executable})"
        return self.executable

    def __repr__(self) -> str:
        return (
            "CaptionWindowCandidate("
            f"executable={self.executable!r}, title=<local>)"
        )


@dataclass(frozen=True)
class CaptionMonitorCandidate:
    """A monitor choice using the resolver's stable-in-layout index."""

    index: int
    device: str
    left: int
    top: int
    width: int
    height: int
    primary: bool = False

    @property
    def target(self) -> CaptionTargetChoice:
        return CaptionTargetChoice("monitor", monitor_index=self.index)

    @property
    def label(self) -> str:
        display = self.device.rsplit("\\", 1)[-1]
        primary = " primary" if self.primary else ""
        return f"{display} - {self.width} x {self.height}{primary}"


@dataclass(frozen=True)
class _WindowSnapshot:
    candidate: CaptionWindowCandidate
    rect: tuple[int, int, int, int]


def top_level_handle(root: tk.Misc) -> int:
    """Return Tk's outer HWND rather than its inner drawing frame."""
    try:
        user32 = ctypes.windll.user32
        user32.GetParent.restype = wintypes.HWND
        user32.GetParent.argtypes = [wintypes.HWND]
        inner = root.winfo_id()
        return int(user32.GetParent(inner) or inner)
    except Exception:
        return 0


def make_click_through(hwnd: int) -> bool:
    """Make a topmost overlay that cannot steal focus or mouse input."""
    if not hwnd or sys.platform != "win32":
        return False
    try:
        user32 = ctypes.windll.user32
        get_style = user32.GetWindowLongPtrW
        set_style = user32.SetWindowLongPtrW
        get_style.restype = ctypes.c_longlong
        get_style.argtypes = [wintypes.HWND, ctypes.c_int]
        set_style.restype = ctypes.c_longlong
        set_style.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_longlong]
        style = get_style(hwnd, GWL_EXSTYLE)
        style = (
            (style | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_LAYERED | WS_EX_NOACTIVATE)
            & ~WS_EX_APPWINDOW
        )
        if not set_style(hwnd, GWL_EXSTYLE, style) and ctypes.get_last_error():
            return False
        user32.SetLayeredWindowAttributes.argtypes = [
            wintypes.HWND,
            wintypes.COLORREF,
            wintypes.BYTE,
            wintypes.DWORD,
        ]
        user32.SetLayeredWindowAttributes.restype = wintypes.BOOL
        # COLORREF is 0x00bbggrr; CHROMA_KEY is RGB 01,02,03.
        if not user32.SetLayeredWindowAttributes(hwnd, 0x00030201, 255, LWA_COLORKEY):
            return False
        user32.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        user32.SetWindowPos.restype = wintypes.BOOL
        user32.SetWindowPos(
            hwnd,
            HWND_TOPMOST,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
        )
        return True
    except Exception:
        return False


def process_image_name(pid: int) -> str | None:
    """The executable basename for a pid, or None if it cannot be read."""
    if sys.platform != "win32" or pid <= 0:
        return None
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(260)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(size)
        ):
            return None
        return PurePath(buffer.value).name.lower()
    finally:
        kernel32.CloseHandle(handle)


def _display_title(value: str) -> str:
    """Keep an OS-provided title safe and compact for a local picker."""
    clean = "".join(
        character if character.isprintable() else " " for character in value
    )
    clean = " ".join(clean.split())
    if len(clean) > 72:
        return clean[:69].rstrip() + "..."
    return clean


def _window_client_rect(user32, hwnd) -> tuple[int, int, int, int] | None:
    rect = _RECT()
    origin = _POINT(0, 0)
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    if not user32.ClientToScreen(hwnd, ctypes.byref(origin)):
        return None
    width = max(rect.right - rect.left, 0)
    height = max(rect.bottom - rect.top, 0)
    if width < 1 or height < 1:
        return None
    return origin.x, origin.y, width, height


def _visible_window_snapshots() -> tuple[_WindowSnapshot, ...]:
    """Read visible user windows with normal Win32 APIs only.

    A picker lists only windows whose executable name Windows can report.
    The overlay resolver itself retains the member-friendly fail-open behavior
    established for the anchor chain.
    """
    if sys.platform != "win32":
        return ()
    try:
        user32 = ctypes.windll.user32
        user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [
            wintypes.HWND,
            wintypes.LPWSTR,
            ctypes.c_int,
        ]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
        user32.GetClientRect.restype = wintypes.BOOL
        user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(_POINT)]
        user32.ClientToScreen.restype = wintypes.BOOL
        try:
            get_style = user32.GetWindowLongPtrW
            get_style.argtypes = [wintypes.HWND, ctypes.c_int]
            get_style.restype = ctypes.c_ssize_t
        except AttributeError:
            get_style = None

        snapshots: list[_WindowSnapshot] = []
        own_pid = os.getpid()
        callback_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )

        def visit(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                return True
            if (
                get_style is not None
                and int(get_style(hwnd, GWL_EXSTYLE)) & WS_EX_TOOLWINDOW
            ):
                return True
            title_length = user32.GetWindowTextLengthW(hwnd)
            # Borderless/fullscreen games often expose no useful title.  A
            # title is only a picker label, never part of the saved identity,
            # so retain those choices and fall back to the executable label.
            title = ""
            if 0 < title_length <= MAX_PICKER_WINDOW_TITLE_LENGTH:
                title_buffer = ctypes.create_unicode_buffer(title_length + 1)
                user32.GetWindowTextW(hwnd, title_buffer, title_length + 1)
                title = _display_title(title_buffer.value)
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value or pid.value == own_pid:
                return True
            executable = process_image_name(pid.value)
            # If Windows cannot tell us the app identity, we cannot offer a
            # durable executable choice for it.
            if executable is None:
                return True
            rect = _window_client_rect(user32, hwnd)
            if rect is None or rect[2] < 640 or rect[3] < 360:
                return True
            snapshots.append(
                _WindowSnapshot(
                    CaptionWindowCandidate(
                        executable=executable,
                        title=title,
                    ),
                    rect,
                )
            )
            return True

        callback = callback_type(visit)
        user32.EnumWindows(callback, 0)
        return tuple(snapshots)
    except Exception:
        return ()


def enumerate_caption_windows() -> tuple[CaptionWindowCandidate, ...]:
    """Return currently visible, usable app choices without live handles.

    Multiple windows with the same durable identity collapse into one picker
    entry: choosing either would resolve to the same reopened application.
    """
    choices: dict[str, tuple[CaptionWindowCandidate, int]] = {}
    for snapshot in _visible_window_snapshots():
        candidate = snapshot.candidate
        key = candidate.executable
        area = snapshot.rect[2] * snapshot.rect[3]
        existing = choices.get(key)
        if existing is None or area > existing[1]:
            choices[key] = (candidate, area)
    return tuple(
        candidate
        for candidate, _area in sorted(
            choices.values(), key=lambda item: item[0].label.casefold()
        )
    )


def _is_mirroring_display_device(user32, device: str) -> bool:
    """Whether Windows calls this monitor a mirror/pseudo display."""
    try:
        enum_devices = user32.EnumDisplayDevicesW
        enum_devices.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(_DISPLAY_DEVICEW),
            wintypes.DWORD,
        ]
        enum_devices.restype = wintypes.BOOL
        index = 0
        while True:
            record = _DISPLAY_DEVICEW()
            record.cb = ctypes.sizeof(_DISPLAY_DEVICEW)
            if not enum_devices(None, index, ctypes.byref(record), 0):
                return False
            if record.DeviceName.casefold() == device.casefold():
                return bool(
                    int(record.StateFlags) & DISPLAY_DEVICE_MIRRORING_DRIVER
                )
            index += 1
    except Exception:
        # A picker that cannot identify a pseudo device still has the normal
        # monitor record as a safe anchor.  Do not make every display vanish
        # because an older Windows build lacks this optional query.
        return False


def _monitor_candidate_from_info(
    info, index: int
) -> CaptionMonitorCandidate | None:
    """Make a picker entry keyed to the anchor resolver's monitor index."""
    device = str(info.szDevice).strip()
    if not device:
        return None
    # The resolver pins to the work area so caption cards avoid the taskbar;
    # show that same usable surface in the picker.
    rect = info.rcWork
    width = max(rect.right - rect.left, 0)
    height = max(rect.bottom - rect.top, 0)
    if width < 1 or height < 1:
        return None
    return CaptionMonitorCandidate(
        index=index,
        device=device,
        left=rect.left,
        top=rect.top,
        width=width,
        height=height,
        primary=bool(int(info.dwFlags) & MONITORINFOF_PRIMARY),
    )


def _monitor_records() -> tuple[CaptionMonitorCandidate, ...]:
    """Read display work areas in the exact order the anchor resolver uses."""
    if sys.platform != "win32":
        return ()
    try:
        user32 = ctypes.windll.user32
        user32.EnumDisplayMonitors.argtypes = [
            wintypes.HDC,
            ctypes.POINTER(_RECT),
            ctypes.c_void_p,
            wintypes.LPARAM,
        ]
        user32.EnumDisplayMonitors.restype = wintypes.BOOL
        user32.GetMonitorInfoW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_MONITORINFOEXW),
        ]
        user32.GetMonitorInfoW.restype = wintypes.BOOL
        monitors: list[CaptionMonitorCandidate] = []
        callback_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL,
            wintypes.HANDLE,
            wintypes.HDC,
            ctypes.POINTER(_RECT),
            wintypes.LPARAM,
        )

        monitor_index = 0

        def visit(handle, _hdc, _clip, _lparam):
            nonlocal monitor_index
            info = _MONITORINFOEXW()
            info.cbSize = ctypes.sizeof(_MONITORINFOEXW)
            if not user32.GetMonitorInfoW(handle, ctypes.byref(info)):
                return True
            # This is the same successful-record order monitor_work_area()
            # uses. Keep the index even when the picker hides a pseudo
            # display, otherwise choosing the next real display could point
            # at a different monitor in the resolver.
            index = monitor_index
            monitor_index += 1
            candidate = _monitor_candidate_from_info(info, index)
            if candidate is not None:
                monitors.append(candidate)
            return True

        callback = callback_type(visit)
        user32.EnumDisplayMonitors(None, None, callback, 0)
        return tuple(monitors)
    except Exception:
        return ()


def enumerate_caption_monitors() -> tuple[CaptionMonitorCandidate, ...]:
    """Return selectable physical monitors keyed to anchor_monitor indexes."""
    if sys.platform != "win32":
        return ()
    try:
        user32 = ctypes.windll.user32
        candidates = (
            monitor
            for monitor in _monitor_records()
            if monitor.index <= MAX_ANCHOR_MONITOR
            and not _is_mirroring_display_device(user32, monitor.device)
        )
        return tuple(
            sorted(
                candidates,
                key=lambda monitor: (
                    not monitor.primary,
                    monitor.top,
                    monitor.left,
                    monitor.device,
                ),
            )
        )
    except Exception:
        return ()
def window_belongs_to(hwnd, user32, executables) -> bool:
    """Whether a window is really owned by one of these executables.

    Fails open. A window whose owner cannot be identified is accepted, because
    losing the anchor costs the player every caption, while accepting a stray
    window only puts the overlay in the wrong place. For an accessibility
    product, wrong-place beats gone - preserve this asymmetry in every
    extension of the anchor code.
    """
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    image = process_image_name(pid.value)
    if image is None:
        return True
    return image in executables


def window_belongs_to_wow(hwnd, user32) -> bool:
    """Kept for its tests and callers; the WoW row of the general rule."""
    return window_belongs_to(hwnd, user32, WOW_PROCESS_NAMES)


def find_game_client_rect(
    executables,
    title_fragment: str | None = None,
) -> tuple[int, int, int, int] | None:
    """The largest visible, non-minimized window owned by these executables.

    The title fragment is an optimisation for the auto-detect table, not
    a requirement: a CHOSEN executable matches on ownership alone,
    because the member already told us which program they mean and its
    window title is whatever the game likes.
    """
    if sys.platform != "win32":
        return None
    try:
        user32 = ctypes.windll.user32
        user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
        user32.GetClientRect.restype = wintypes.BOOL
        user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(_POINT)]
        user32.ClientToScreen.restype = wintypes.BOOL

        matches: list[tuple[int, int, int, int]] = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def visit(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length < 0 or length > MAX_PICKER_WINDOW_TITLE_LENGTH:
                return True
            title = ""
            if length:
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                title = buffer.value.lower()
            if title_fragment is not None and title_fragment not in title:
                return True
            if not window_belongs_to(hwnd, user32, executables):
                return True
            rect = _RECT()
            origin = _POINT(0, 0)
            if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
                return True
            if not user32.ClientToScreen(hwnd, ctypes.byref(origin)):
                return True
            width = max(rect.right - rect.left, 0)
            height = max(rect.bottom - rect.top, 0)
            if width >= 640 and height >= 360:
                matches.append((origin.x, origin.y, width, height))
            return True

        callback = callback_type(visit)
        user32.EnumWindows(callback, 0)
        return max(matches, key=lambda rect: rect[2] * rect[3]) if matches else None
    except Exception:
        return None


def find_wow_client_rect() -> tuple[int, int, int, int] | None:
    """Kept for its callers and tests; the WoW row of the general rule."""
    return find_game_client_rect(WOW_PROCESS_NAMES, "world of warcraft")


def _select_monitor(monitors, index):
    """Out of range is None, not a clamp.

    A pin to a monitor that was unplugged should fall through the chain,
    not silently mean a different screen the member deliberately did not
    choose. Pure so the rule is testable without a display.
    """
    if 0 <= index < len(monitors):
        return monitors[index]
    return None


def monitor_work_area(index: int) -> tuple[int, int, int, int] | None:
    """The work area of the index-th display, or None.

    Enumeration order is Windows's own, stable for a fixed monitor
    arrangement - the same order a settings UI would list them in. Index
    out of range is None, not a clamp: a pin to a monitor that was
    unplugged should fall through the chain, not silently mean a
    different screen.
    """
    monitor = _select_monitor(_monitor_records(), index)
    if monitor is None:
        return None
    return monitor.left, monitor.top, monitor.width, monitor.height


def resolve_anchor_rect(settings) -> tuple[int, int, int, int] | None:
    """Where the captions belong, by the precedence the member set.

    remembered window -> known-game auto-detect -> pinned monitor is the
    order only in the sense that each MODE tries its own step first: a
    mode that cannot resolve falls through to the auto-detect scan
    rather than pinning captions to nothing. None means the caller's
    work-area fallback - the chain never strands the overlay.
    """
    mode = getattr(settings, "anchor_mode", "auto")
    if mode == "window":
        executable = getattr(settings, "anchor_executable", "")
        if executable:
            rect = find_game_client_rect(frozenset({executable}))
            if rect is not None:
                return rect
    elif mode == "monitor":
        rect = monitor_work_area(getattr(settings, "anchor_monitor", 0))
        if rect is not None:
            return rect
    for title_fragment, executables in GAME_PROFILES:
        rect = find_game_client_rect(executables, title_fragment)
        if rect is not None:
            return rect
    return None


def rounded_rectangle(
    canvas: tk.Canvas,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    radius: int,
    *,
    fill: str,
    outline: str,
    stipple: str = "",
    tags=(),
) -> int:
    """A small smooth polygon avoids the square native Tk frame look.

    stipple="gray50" dithers the FILL so alternating pixels show the
    chroma key - the closest thing Tk has to a translucent background.
    Composited over the game it reads as a card at half opacity while
    the text on top stays fully solid, which is the whole point: the
    words must never fade with their backdrop.
    """
    radius = max(2, min(radius, (y2 - y1) // 2, (x2 - x1) // 2))
    points = (
        x1 + radius,
        y1,
        x2 - radius,
        y1,
        x2,
        y1,
        x2,
        y1 + radius,
        x2,
        y2 - radius,
        x2,
        y2,
        x2 - radius,
        y2,
        x1 + radius,
        y2,
        x1,
        y2,
        x1,
        y2 - radius,
        x1,
        y1 + radius,
        x1,
        y1,
    )
    options = {}
    if stipple:
        options["stipple"] = stipple
    if tags:
        options["tags"] = tags
    return canvas.create_polygon(
        points,
        smooth=True,
        **options,
        splinesteps=18,
        fill=fill,
        outline=outline,
        width=1,
    )


def _effective_recording_state(view: object) -> str:
    """Return the privacy tally state without inventing cold uncertainty.

    Drawing tests and lightweight preview subjects predate the independent
    latch, so they fall back to their exact session state. Real overlays set
    recording_state explicitly.
    """
    latched = getattr(view, "recording_state", None)
    if latched is not None:
        return latched
    session = getattr(view, "session_state", "")
    return session if session in {"active", "unknown"} else ""


class CaptionOverlay:
    def __init__(
        self,
        root: tk.Tk,
        events: queue.Queue,
        *,
        desktop_fallback: bool = False,
    ) -> None:
        self.root = root
        self.events = events
        self.desktop_fallback = desktop_fallback
        self.settings_path = default_settings_path()
        self.settings = load_settings(self.settings_path)
        self.known_speaker_labels = {
            speaker.speaker_id: speaker.display_name
            for speaker in load_known_speakers()
        }
        self.font_size = self.settings.caption_font_size
        self._font_family = self.settings.caption_font_family
        self.metrics = self._metrics_for(self.font_size)
        self.board = CaptionBoard(
            max_rows=self.settings.max_visible_speakers,
            final_hold_seconds=self.settings.caption_hold_seconds,
        )
        self.canvas_height = self._canvas_height_for(
            self.settings.max_visible_speakers, self.font_size
        )
        self.session_state = "ready"
        # "ready" is the quiet local default, not a relay-confirmed state.
        # This distinction lets a legacy cold-start caption prove activity
        # without letting delayed transcription override an explicit stop.
        self.session_state_confirmed = False
        self.recording_state = ""
        self.session_message = ""
        self.last_target_check = 0.0
        self.target_rect: tuple[int, int, int, int] | None = None
        self.settings_mtime_ns = -1
        self.visible = False
        self.last_signature: tuple | None = None

        root.title("Vemal's Ears")
        root.configure(bg=CHROMA_KEY)
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.geometry(
            f"{self.metrics.canvas_width}x{self.canvas_height}+0+0"
        )
        try:
            root.wm_attributes("-transparentcolor", CHROMA_KEY)
        except tk.TclError:
            pass

        self.canvas = tk.Canvas(
            root,
            width=self.metrics.canvas_width,
            height=self.canvas_height,
            bg=CHROMA_KEY,
            highlightthickness=0,
            borderwidth=0,
        )
        self.canvas.pack(fill="both", expand=True)
        self.fonts = self._make_fonts(self.font_size)

        root.update_idletasks()
        make_click_through(top_level_handle(root))
        root.withdraw()
        root.after(TICK_MS, self.tick)

    def _metrics_for(self, size: int):
        """Metrics scaled to the font, then fitted to the active WoW client.

        The single place metrics are built, so the width cap can never be
        applied on one path and forgotten on the other.

        The game window, not the desktop: on an ultrawide those differ by
        more than a factor of two, and laying captions out for the monitor
        puts them off the side of the window the player is looking at.
        """
        target = getattr(self, "target_rect", None)
        try:
            available = self.root.winfo_screenwidth()
        except tk.TclError:
            available = None
        return metrics_for_target(
            size,
            target,
            fallback_width=available,
        )

    def _make_fonts(self, size: int) -> dict:
        """Every caption font scaled from the chosen caption size.

        The five faces keep their relative proportions (the caption is the
        base; the speaker name and status are a touch smaller), so raising
        the caption size raises them all together.
        """
        family = getattr(
            self,
            "_font_family",
            getattr(self.settings, "caption_font_family", "Segoe UI"),
        )
        return {
            name: tkfont.Font(**spec)
            for name, spec in caption_font_specs(size, family).items()
        }

    def _canvas_height_for(self, rows: int, size: int) -> int:
        target = getattr(self, "target_rect", None)
        try:
            available = int(self.root.winfo_screenheight())
        except tk.TclError:
            available = None
        return canvas_height_for_target(
            rows,
            size,
            target,
            fallback_height=available,
        )

    def fallback_rect(self) -> tuple[int, int, int, int]:
        return (0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight())

    def refresh_target(self, now: float) -> None:
        if now - self.last_target_check < TARGET_REFRESH_SECONDS:
            return
        self.last_target_check = now
        previous = self.target_rect
        self.target_rect = resolve_anchor_rect(self.settings)
        if self.target_rect is None and self.desktop_fallback:
            self.target_rect = self.fallback_rect()
        # Re-fit when the game window moves or resizes. Without this the
        # captions keep the geometry of whatever the window was when the
        # overlay started.
        if self.target_rect != previous and hasattr(self, "canvas"):
            previous_width = self.metrics.canvas_width
            self.metrics = self._metrics_for(self.font_size)
            height = self._canvas_height_for(
                self.settings.max_visible_speakers,
                self.font_size,
            )
            if height != self.canvas_height:
                self.canvas_height = height
                self.canvas.configure(height=height)
            if self.metrics.canvas_width != previous_width:
                self.canvas.configure(width=self.metrics.canvas_width)

    def refresh_settings(self) -> None:
        try:
            modified = self.settings_path.stat().st_mtime_ns
        except OSError:
            modified = 0
        if modified == self.settings_mtime_ns:
            return
        self.settings_mtime_ns = modified
        updated = load_settings(self.settings_path)
        newly_muted = set(updated.muted_speaker_ids) - set(
            self.settings.muted_speaker_ids
        )
        if newly_muted:
            self.board.remove_speakers(newly_muted)
        anchor_changed = (
            updated.anchor_mode != self.settings.anchor_mode
            or updated.anchor_executable != self.settings.anchor_executable
            or updated.anchor_monitor != self.settings.anchor_monitor
        )
        self.board.configure_display(
            max_rows=updated.max_visible_speakers,
            final_hold_seconds=updated.caption_hold_seconds,
        )
        # A font-size change rescales every metric and rebuilds the fonts;
        # a row change (or the resulting taller stack) resizes the canvas.
        font_changed = (
            updated.caption_font_size != self.font_size
            or updated.caption_font_family
            != getattr(
                self,
                "_font_family",
                self.settings.caption_font_family,
            )
        )
        if font_changed:
            self.font_size = updated.caption_font_size
            self._font_family = updated.caption_font_family
            self.metrics = self._metrics_for(self.font_size)
            self.fonts = self._make_fonts(self.font_size)
        new_height = self._canvas_height_for(
            updated.max_visible_speakers, self.font_size
        )
        if new_height != self.canvas_height:
            self.canvas_height = new_height
            self.canvas.configure(height=new_height)
        self.settings = updated
        if anchor_changed:
            # The next tick has already read the changed settings. Do not make
            # a member wait for the regular target polling interval before the
            # new window or monitor takes effect.
            self.last_target_check = -TARGET_REFRESH_SECONDS
        self.last_signature = None

    def drain_events(self, now: float) -> bool:
        changed = False
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event is _EOF:
                self.root.quit()
                return True
            if isinstance(event, CaptionEvent):
                if not event.speaker_id.startswith("quick-chat-"):
                    known_labels = getattr(self, "known_speaker_labels", None)
                    previous_name = (
                        known_labels.get(event.speaker_id)
                        if isinstance(known_labels, dict)
                        else event.display_name
                    )
                    if (
                        isinstance(known_labels, dict)
                        and previous_name != event.display_name
                        and remember_speaker(event.speaker_id, event.display_name)
                    ):
                        known_labels[event.speaker_id] = event.display_name
                muted_speakers = getattr(
                    getattr(self, "settings", None),
                    "muted_speaker_ids",
                    (),
                )
                if event.speaker_id in muted_speakers:
                    changed = self.board.discard(event) or changed
                else:
                    changed = self.board.ingest(event, now) or changed
                # A spoken caption can repair only a missing legacy snapshot.
                # It cannot override an explicit ready, paused, stopped, or
                # error event: transcription may finish after capture stops.
                # Typed quick chat never proves that audio capture is active.
                if (
                    not event.speaker_id.startswith("quick-chat-")
                    and self.session_state == "unknown"
                    and (
                        _effective_recording_state(self) == "unknown"
                        or not getattr(self, "session_state_confirmed", False)
                    )
                ):
                    self.session_state = "active"
                    self.session_state_confirmed = True
                    self.recording_state = "active"
                    self.session_message = ""
                    changed = True
            elif isinstance(event, SessionEvent):
                previous_recording = _effective_recording_state(self)
                self.session_state = event.state
                if event.state != "unknown":
                    self.session_state_confirmed = True
                if event.state == "active":
                    self.recording_state = "active"
                elif (
                    event.state == "unknown"
                    and previous_recording in {"active", "unknown"}
                ):
                    self.recording_state = "unknown"
                else:
                    self.recording_state = ""
                self.session_message = event.message
                if event.state in {"stopped", "ready"}:
                    self.board.clear()
                changed = True
            elif isinstance(event, ClearEvent):
                self.board.clear()
                changed = True
        return changed

    def tick(self) -> None:
        now = time.monotonic()
        # Read display and target changes before ingesting the next caption, so
        # a newly muted speaker cannot flash for one frame before the local
        # preference takes effect.
        self.refresh_settings()
        self.drain_events(now)
        self.refresh_target(now)
        self.render(now)
        self.root.after(TICK_MS, self.tick)

    def render(self, now: float) -> None:
        # Muted: hide caption text, but keep draining and ageing the board
        # so unmuting shows what is live right now rather than a stale
        # backlog. Capture and the connection are untouched, so the tiny
        # privacy tally remains while an active session may receive audio.
        if self.settings.muted:
            # Age the board even while hidden, so unmuting shows what is
            # live now, not everything that arrived during the mute.
            self.board.visible(now)
            recording_state = _effective_recording_state(self)
            muted_signature = ("muted", recording_state, self.target_rect)
            if muted_signature != self.last_signature:
                self.last_signature = muted_signature
                self.canvas.delete("all")
                if self.target_rect is None or not recording_state:
                    self.hide()
                else:
                    x, y = position_from_settings(
                        self.target_rect,
                        (self.metrics.canvas_width, self.canvas_height),
                        self.settings,
                        bottom_reference_height=CANVAS_HEIGHT,
                    )
                    self.root.geometry(
                        f"{self.metrics.canvas_width}x{self.canvas_height}+{x}+{y}"
                    )
                    self.draw_recording_tally()
                    self.show()
            return
        lines = self.board.visible(now)
        show_status = not lines and self.session_state in {"paused", "error"}
        recording_state = _effective_recording_state(self)
        show_recording = bool(recording_state)
        signature = (
            tuple(
                (
                    line.event.speaker_id,
                    line.event.sequence,
                    line.event.text,
                    line.event.final,
                )
                for line in lines
            ),
            self.session_state if show_status else "",
            self.session_message if show_status else "",
            recording_state,
            self.target_rect,
            self.settings,
        )
        if signature == self.last_signature:
            return
        self.last_signature = signature
        self.canvas.delete("all")

        if self.target_rect is None or (
            not lines and not show_status and not show_recording
        ):
            self.hide()
            return

        x, y = position_from_settings(
            self.target_rect,
            (self.metrics.canvas_width, self.canvas_height),
            self.settings,
            bottom_reference_height=CANVAS_HEIGHT,
        )
        self.root.geometry(
            f"{self.metrics.canvas_width}x{self.canvas_height}+{x}+{y}"
        )
        if show_status:
            self.draw_status()
        elif not lines:
            self.draw_recording_tally()
        else:
            self.draw_lines(lines)
        self.show()

    def show(self) -> None:
        if self.visible:
            return
        self.root.deiconify()
        self.root.lift()
        make_click_through(top_level_handle(self.root))
        self.visible = True

    def hide(self) -> None:
        if not self.visible:
            return
        self.root.withdraw()
        self.visible = False

    def draw_status(self) -> None:
        font = self.fonts["status"]
        label = self.session_message or (
            "Captions paused - waiting for consent"
            if self.session_state == "paused"
            else "Captions unavailable"
        )
        try:
            line_height = max(1, int(font.metrics("linespace")))
        except (AttributeError, TypeError, ValueError, tk.TclError):
            line_height = max(10, round(12 * self.font_size / BASE_FONT_SIZE))
        available_height = self.canvas_height - 2 * self.metrics.vertical_padding
        if available_height < line_height + 1:
            return
        padding = min(max(4, round(line_height / 2)),
                      (available_height - line_height - 1) // 2)
        dot_size = max(3, round(line_height * 0.45))
        gap = max(3, round(line_height * 0.35))
        chrome = 2 * padding + dot_size + gap + 1
        max_width = min(
            self.metrics.canvas_width,
            self.metrics.max_card_width,
            round(350 * self.font_size / BASE_FONT_SIZE),
        )
        text_width = max_width - chrome
        if text_width <= 0:
            return
        # Use the existing transparent canvas for the whole instruction,
        # rather than dropping its actionable ending after one fixed line.
        # Only a message taller than the available canvas needs clipping.
        max_lines = (available_height - 2 * padding - 1) // line_height
        chunks = wrap_text(label, text_width, font.measure, max_lines)
        width = chrome + max(font.measure(chunk) for chunk in chunks)
        height = 2 * padding + len(chunks) * line_height + 1
        x1 = (self.metrics.canvas_width - width) // 2
        y1 = (
            self.metrics.vertical_padding if self.settings.growth == "down"
            else self.canvas_height - height - self.metrics.vertical_padding
        )
        color = DANGER if self.session_state == "error" else FROST
        rounded_rectangle(
            self.canvas,
            x1,
            y1,
            x1 + width,
            y1 + height,
            max(4, round(line_height * 0.6)),
            fill=SLATE,
            outline=color,
        )
        dot_left = x1 + padding
        dot_top = y1 + padding + (line_height - dot_size) // 2
        self.canvas.create_oval(
            dot_left,
            dot_top,
            dot_left + dot_size,
            dot_top + dot_size,
            fill=color,
            outline="",
        )
        text_x = dot_left + dot_size + gap
        for index, chunk in enumerate(chunks):
            text_y = y1 + padding + index * line_height
            text_id = self.canvas.create_text(
                text_x, text_y, text=chunk, fill=TEXT, font=font, anchor="nw",
            )
            shadow_id = self.canvas.create_text(
                text_x + 1, text_y + 1, text=chunk, fill="#000000",
                font=font, anchor="nw",
            )
            if hasattr(self.canvas, "tag_lower"):
                self.canvas.tag_lower(shadow_id, text_id)

    def recording_tally_size(self) -> tuple[int, int]:
        font = self.fonts["status"]
        label = (
            "REC?" if _effective_recording_state(self) == "unknown" else "REC"
        )
        dot_size = max(5, round(6 * self.font_size / BASE_FONT_SIZE))
        try:
            line_height = int(font.metrics("linespace"))
        except (AttributeError, TypeError, ValueError, tk.TclError):
            line_height = max(10, round(12 * self.font_size / BASE_FONT_SIZE))
        width = 7 + dot_size + 4 + font.measure(label) + 7
        return max(38, width), max(18, line_height + 6, dot_size + 8)

    def draw_recording_tally(
        self,
        *,
        right: int | None = None,
        center_y: int | None = None,
    ) -> None:
        """Draw a compact group-level privacy tally beside the captions."""
        width, height = self.recording_tally_size()
        label = (
            "REC?" if _effective_recording_state(self) == "unknown" else "REC"
        )
        if right is None:
            left = (self.metrics.canvas_width - width) // 2
            # The tally keeps to the caption edge: beside the bottom cards
            # of a standing feed, beside the top cards of a hanging one.
            if self.settings.growth == "down":
                top = self.metrics.vertical_padding
            else:
                top = self.canvas_height - height - self.metrics.vertical_padding
        else:
            left = right - width
            top = int(center_y or 0) - height // 2
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
        dot_size = max(5, round(6 * self.font_size / BASE_FONT_SIZE))
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
            text=label,
            fill=DANGER,
            font=self.fonts["status"],
            anchor="w",
        )

    def measure_card(self, line, *, recording: bool = False) -> dict:
        """Everything about one card that needs the fonts.

        Width, wrapped caption lines and height are decided together
        because they depend on each other: the caption gets the width the
        speaker name leaves behind, and the height depends on how many
        lines the caption then needs.
        """
        event = line.event
        live = line.live
        caption_font = self.fonts["caption"] if live else self.fonts["previous"]
        # A name allowed to run the length of the card silently deletes
        # the words it belongs to. Bound it before measuring anything.
        speaker = clipped_text(
            event.display_name.upper(),
            self.metrics.max_card_width // 3,
            self.fonts["speaker"].measure,
        )
        speaker_width = self.fonts["speaker"].measure(speaker)
        tally_width = self.recording_tally_size()[0] if recording else 0
        trailing = tally_width + 12 if recording else (18 if live else 10)
        chrome = 58 + speaker_width + trailing
        desired = chrome + caption_font.measure(event.text)
        if desired <= self.metrics.max_card_width:
            # Short enough for one line: keep the card only as wide as it
            # needs to be, exactly as before.
            width = max(self.metrics.min_card_width, desired)
            wrapped = [event.text]
        else:
            # Too long for one line, so take the full width and wrap into
            # it rather than deleting the tail of the sentence.
            width = self.metrics.max_card_width
            wrapped = wrap_text(
                event.text,
                max(width - chrome, 0),
                caption_font.measure,
            ) or [""]
        height = self.metrics.row_height(line, len(wrapped))
        if recording:
            height = max(height, self.recording_tally_size()[1] + 4)
        return {
            "speaker": speaker,
            "speaker_width": speaker_width,
            "caption_font": caption_font,
            "wrapped": wrapped,
            "width": width,
            "height": height,
            "recording": recording,
            "tally_width": tally_width,
        }

    def draw_lines(self, lines) -> None:
        recording_index = (
            len(lines) - 1
            if _effective_recording_state(self) in {"active", "unknown"}
            else -1
        )
        cards = [
            self.measure_card(line, recording=index == recording_index)
            for index, line in enumerate(lines)
        ]
        stack_height = self.metrics.stacked([card["height"] for card in cards])
        if self.settings.growth == "down":
            # A hanging feed: the first card sits at the very top of the
            # canvas and cross-talk extends downward. This is what lets a
            # member place captions AT the top of the screen - the canvas
            # is sized for the worst-case stack, so bottom-anchored cards
            # could never get closer to the top than a whole empty canvas.
            cursor_y = self.metrics.vertical_padding
        else:
            cursor_y = (
                self.canvas_height
                - stack_height
                + self.metrics.vertical_padding
            )
        for line, card in zip(lines, cards):
            self.draw_line(line, cursor_y, card["height"], card)
            cursor_y += card["height"] + self.metrics.row_gap

    def draw_line(self, line, y1: int, height: int, card: dict | None = None) -> None:
        event = line.event
        live = line.live
        card = card or self.measure_card(line)
        caption_font = card["caption_font"]
        speaker = card["speaker"]
        speaker_width = card["speaker_width"]
        width = card["width"]
        x1 = (self.metrics.canvas_width - width) // 2
        x2 = x1 + width
        y2 = y1 + height
        fill = self.settings.caption_background_color
        outline = event.color if live else SLATE
        # Card opacity is a dithered backdrop while caption text stays solid.
        # The REC privacy tally and the status card stay opaque on purpose -
        # a privacy indicator at half strength reads as half true.
        rounded_rectangle(
            self.canvas,
            x1,
            y1,
            x2,
            y2,
            8,
            stipple=background_stipple(
                self.settings.caption_background_opacity
            ),
            fill=fill,
            outline=outline,
        )
        self.canvas.create_rectangle(
            x1 + 1,
            y1 + 7,
            x1 + 4,
            y2 - 7,
            fill=event.color,
            outline="",
        )

        wrapped = card["wrapped"]
        # The first text line, which the avatar and the name sit on. A
        # single-line card centres exactly as it always did; a wrapped one
        # keeps its name at the top so the eye starts in the same place
        # whatever the length of the sentence.
        base_height = self.metrics.row_height(line, 1)
        first_text_y = y1 + base_height // 2

        avatar = 24 if live else 18
        avatar_x = x1 + 13
        avatar_y = first_text_y - avatar // 2
        self.canvas.create_oval(
            avatar_x,
            avatar_y,
            avatar_x + avatar,
            avatar_y + avatar,
            fill=event.color if live else SLATE,
            outline=event.color,
            width=1,
        )
        initial_id = self.canvas.create_text(
            avatar_x + avatar // 2,
            avatar_y + avatar // 2,
            text=speaker[:1] or "?",
            fill=VOID if live else MUTED,
            font=self.fonts["initial"],
        )
        shadow_id = self.canvas.create_text(
            avatar_x + avatar // 2 + 1,
            avatar_y + avatar // 2 + 1,
            text=speaker[:1] or "?",
            fill="#000000",
            font=self.fonts["initial"],
        )
        if hasattr(self.canvas, "tag_lower"):
            self.canvas.tag_lower(shadow_id, initial_id)

        text_y = first_text_y
        name_x = avatar_x + avatar + 9
        name_id = self.canvas.create_text(
            name_x,
            text_y,
            text=speaker,
            # The speaker's class colour, which a WoW player reads without
            # having to read the name. The card's accent stays the role
            # colour: that is what focus mode filters on, and conflating
            # the two would lose the distinction the reader is using. A
            # mapped name keeps its class colour in scrollback; unmapped
            # finished names retain the existing muted treatment.
            fill=(event.name_color if live or event.wow_class else MUTED),
            font=self.fonts["speaker"],
            anchor="w",
        )
        shadow_id = self.canvas.create_text(
            name_x + 1,
            text_y + 1,
            text=speaker,
            fill="#000000",
            font=self.fonts["speaker"],
            anchor="w",
        )
        if hasattr(self.canvas, "tag_lower"):
            self.canvas.tag_lower(shadow_id, name_id)
        caption_x = name_x + speaker_width + 9
        recording = card.get("recording", False)
        reserve = card.get("tally_width", 0) + 12 if recording else (18 if live else 9)
        available = max(x2 - caption_x - reserve, 0)
        # Continuation lines start under the name rather than under the
        # caption, so the second line does not read as a new speaker.
        for index, chunk in enumerate(wrapped):
            chunk_text = (chunk if index == 0 else clipped_text(chunk, max(x2 - name_x - reserve, 0), caption_font.measure))
            text_x = caption_x if index == 0 else name_x
            text_y_offset = text_y + index * self.metrics.text_line_height
            text_id = self.canvas.create_text(
                text_x,
                text_y_offset,
                text=chunk_text,
                fill=self.settings.caption_text_color,
                font=caption_font,
                anchor="w",
            )
            shadow_id = self.canvas.create_text(
                text_x + 1,
                text_y_offset + 1,
                text=chunk_text,
                fill="#000000",
                font=caption_font,
                anchor="w",
            )
            if hasattr(self.canvas, "tag_lower"):
                self.canvas.tag_lower(shadow_id, text_id)
        if recording:
            self.draw_recording_tally(right=x2 - 6, center_y=text_y)
        elif live:
            self.canvas.create_oval(
                x2 - 11,
                text_y - 3,
                x2 - 5,
                text_y + 3,
                fill=FROST,
                outline="",
            )


def read_json_lines(target: queue.Queue, exit_on_eof: bool) -> None:
    """Read untrusted component output without ever echoing transcript text."""
    for raw in sys.stdin:
        try:
            target.put(parse_event_line(raw))
        except CaptionProtocolError:
            # Malformed input fails closed. Do not print the offending line:
            # it may contain transcript text or a credential by mistake.
            continue
    if exit_on_eof:
        target.put(_EOF)


def demo_events() -> list[tuple[int, ProtocolEvent]]:
    return [
        (100, SessionEvent("active")),
        (
            450,
            CaptionEvent(
                "user-mira",
                "Mira",
                "Mira",
                "HEALER",
                "Spread for circles, then stack on me.",
                False,
                1,
                0.45,
            ),
        ),
        (
            1350,
            CaptionEvent(
                "user-drex",
                "Drex",
                "Drex",
                "TANK",
                "Boss turning left.",
                False,
                1,
                1.35,
            ),
        ),
        (
            2350,
            CaptionEvent(
                "user-drex",
                "Drex",
                "Drex",
                "TANK",
                "Boss turning left.",
                True,
                2,
                2.35,
            ),
        ),
        (
            2900,
            CaptionEvent(
                "user-mira",
                "Mira",
                "Mira",
                "HEALER",
                "Spread for circles, then stack on me.",
                True,
                2,
                2.9,
            ),
        ),
        (
            3250,
            CaptionEvent(
                "user-vale",
                "Vale",
                "Vale",
                "DAMAGE",
                "I have the next interrupt.",
                False,
                1,
                3.25,
            ),
        ),
        (
            4350,
            CaptionEvent(
                "user-vale",
                "Vale",
                "Vale",
                "DAMAGE",
                "I have the next interrupt.",
                True,
                2,
                4.35,
            ),
        ),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="Show a short local preview.")
    parser.add_argument(
        "--desktop-fallback",
        action="store_true",
        help="Show without WoW; intended only for visual preview.",
    )
    parser.add_argument(
        "--exit-on-eof",
        action="store_true",
        help="Close when the parent closes the JSON input stream.",
    )
    args = parser.parse_args(argv)

    event_queue: queue.Queue = queue.Queue()
    try:
        root = tk.Tk()
    except tk.TclError:
        return 2
    overlay = CaptionOverlay(
        root,
        event_queue,
        desktop_fallback=args.desktop_fallback or args.demo,
    )

    if args.demo:
        for delay, event in demo_events():
            root.after(delay, lambda value=event: event_queue.put(value))
        root.after(8_000, root.quit)
    else:
        threading.Thread(
            target=read_json_lines,
            args=(event_queue, args.exit_on_eof),
            daemon=True,
        ).start()
    try:
        root.mainloop()
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
