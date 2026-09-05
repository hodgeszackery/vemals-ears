"""Small, privacy-safe settings file for overlay placement."""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path

SCHEMA = 1
STATE_FOLDER = "WoWVoiceCaptions"
SETTINGS_NAME = "overlay-settings.json"
# Where the adapter remembers who has consented. It lives beside the overlay
# settings rather than in the repository, because it names real people.
CONSENT_STATE_NAME = "consent-state.json"
# And who plays what: /vem map records, same handling for the same
# reason - a mapping names a real person's characters.
MAPPING_STATE_NAME = "mapping-state.json"

# Now typed in freely rather than stepped through a fixed list, so the
# bounds are the only guard. Wide but not unbounded: a hold of a minute is
# already a standing transcript, and a font smaller than 6pt or larger
# than 48pt stops being a caption.
MIN_CAPTION_HOLD_SECONDS = 0.5
MAX_CAPTION_HOLD_SECONDS = 60.0
MIN_VISIBLE_SPEAKERS = 1
# Must equal events.MAX_OVERLAY_ROWS; a test pins them together. Kept as a
# literal here to avoid settings importing the caption model.
MAX_VISIBLE_SPEAKERS = 20
# Where the caption overlay anchors itself. "auto" scans the known-game
# table; "window" pins to a remembered executable the member chose once;
# "monitor" pins to a display for anything unsupported. These three keys
# are the contract between the companion's chooser UI (which writes
# them) and the overlay's resolver (which reads them) - the two ship
# independently and either alone degrades to today's WoW-only behaviour.
ANCHOR_MODES = ("auto", "window", "monitor")
MAX_ANCHOR_MONITOR = 15
MAX_ANCHOR_EXECUTABLE_CHARS = 64

MIN_CAPTION_FONT_SIZE = 6
MAX_CAPTION_FONT_SIZE = 48
DEFAULT_CAPTION_FONT_SIZE = 10
# Caption placement is intentionally allowed beyond the selected game window.
# This is intentionally not a display boundary. The generous guard prevents a
# corrupted file from handing the window manager an overflowing coordinate.
MAX_SAFE_POSITION_RATIO = 1000.0
MAX_MUTED_SPEAKERS = 128
SPEAKER_TOKEN_PATTERN = re.compile(r"^[0-9a-f]{24}$")


@dataclass(frozen=True)
class OverlaySettings:
    # Center point as a proportion of the current WoW client rectangle. It
    # may fall outside 0..1 when the member drags captions beyond that window.
    anchor_x: float = 0.5
    anchor_y: float = 0.79
    # Focus mode: show only tank and healer captions. STRICT by request -
    # an unmapped speaker (role UNKNOWN) is dropped too, so a group that
    # never runs /vem map hears nobody while this is on. The companion's
    # toggle label says so, because the setting itself cannot.
    focus_mode: bool = False
    # Finished captions remain long enough to read without building a
    # transcript. Live partials still update immediately while someone talks.
    caption_hold_seconds: float = 3.6
    # One card represents one speaker. Keeping this bounded prevents the
    # overlay from expanding across raid mechanics during cross-talk.
    max_visible_speakers: int = 2
    # The caption text size. Every other font and the row heights scale
    # from it, so a bigger number is a bigger overlay, not clipped text.
    caption_font_size: int = DEFAULT_CAPTION_FONT_SIZE
    # The anchor chain's first two decisions. Mode "window" reads
    # anchor_executable; mode "monitor" reads anchor_monitor; "auto"
    # reads neither. A mode that cannot resolve falls through to
    # auto-detect rather than pinning captions to nothing.
    anchor_mode: str = "auto"
    anchor_executable: str = ""
    anchor_monitor: int = 0
    # Mute: hide caption text without stopping capture or the
    # connection. For running the companion to sim while the caption
    # window would otherwise sit on screen - unlike Stop, unmuting is
    # instant and drops nothing. The privacy REC tally remains visible
    # during an active or unknown session because hiding text does not stop
    # capture and a lost connection does not prove remote capture stopped.
    muted: bool = False
    # Per-speaker mute is a local display preference. IDs are opaque HMAC
    # tokens, never Discord account IDs, and names live in a separate bounded
    # local directory so this settings file still contains no transcript.
    muted_speaker_ids: tuple[str, ...] = ()
    # Which way the caption stack grows from its anchored edge. "up" is
    # the classic above-the-action-bars placement: cards stand on the
    # baseline and stack upward. "down" hangs the feed from its top edge
    # instead - chosen automatically when the member drops it in the top
    # half of its surface, because the canvas is sized for the WORST-case
    # stack and bottom-anchored cards in it can never reach the top of
    # the screen: even dragged fully up they sat a whole empty canvas
    # below the edge, and letting the window go higher would have clipped
    # captions mid-raid instead.
    growth: str = "up"


DEFAULT_SETTINGS = OverlaySettings()


def default_settings_path() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    return root / STATE_FOLDER / SETTINGS_NAME


def default_consent_state_path() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    return root / STATE_FOLDER / CONSENT_STATE_NAME


def default_mapping_state_path() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    return root / STATE_FOLDER / MAPPING_STATE_NAME


# Win32 RegisterHotKey modifier bits, named here so the parser stays a
# pure function that the companion's Win32 code can consume without this
# module importing anything Windows-only.
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
# Without this a held key repeats the hotkey dozens of times a second,
# which would open the box, then reopen it under the member's typing.
MOD_NOREPEAT = 0x4000

_MODIFIER_NAMES = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
}

_CANONICAL_MODIFIERS = (
    ("Ctrl", MOD_CONTROL),
    ("Alt", MOD_ALT),
    ("Shift", MOD_SHIFT),
    ("Win", MOD_WIN),
)


DEFAULT_SPEAK_HOTKEY = "Ctrl+Alt+V"


def _hotkey_components(spec: str):
    """Return canonical text plus the modifiers and virtual key Windows wants."""
    if not isinstance(spec, str) or not spec.strip():
        return None
    parts = [part.strip().lower() for part in spec.split("+") if part.strip()]
    if len(parts) < 2:
        return None
    modifiers = 0
    key = None
    for part in parts:
        if part in _MODIFIER_NAMES:
            modifiers |= _MODIFIER_NAMES[part]
            continue
        if key is not None:
            return None  # two non-modifier keys is not a hotkey
        key = part
    if not modifiers or key is None:
        return None

    virtual_key = None
    canonical_key = ""
    if len(key) == 1 and ("a" <= key <= "z" or "0" <= key <= "9"):
        # Shift plus a printable key is ordinary typing, not a safe global
        # shortcut. Ctrl, Alt, or Win must also be present for these keys.
        if not modifiers & (MOD_CONTROL | MOD_ALT | MOD_WIN):
            return None
        canonical_key = key.upper()
        virtual_key = ord(canonical_key)
    elif len(key) in (2, 3) and key[0] == "f" and key[1:].isdigit():
        number = int(key[1:])
        # Windows reserves F12 for debuggers even when no debugger is active.
        if 1 <= number <= 24 and number != 12:
            canonical_key = f"F{number}"
            virtual_key = 0x70 + number - 1
    if virtual_key is None:
        return None

    canonical_parts = [
        label for label, flag in _CANONICAL_MODIFIERS if modifiers & flag
    ]
    canonical_parts.append(canonical_key)
    return "+".join(canonical_parts), modifiers | MOD_NOREPEAT, virtual_key


def canonicalize_hotkey(spec: str) -> str | None:
    """Return one stable display/storage form, or None for an unsafe shortcut."""
    parsed = _hotkey_components(spec)
    return parsed[0] if parsed is not None else None


def parse_hotkey(spec: str):
    """Turn "Ctrl+Alt+V" into the (modifiers, virtual key) Windows wants.

    None means the spec is unusable, and the caller falls back to the
    default. A hotkey with no modifier is refused on purpose: registering
    a bare letter steals it from every application on the desktop,
    including the game the member is playing.
    """
    parsed = _hotkey_components(spec)
    return parsed[1:] if parsed is not None else None


def _valid_ratio(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not math.isfinite(value) or abs(value) > MAX_SAFE_POSITION_RATIO:
        return None
    return value


def _valid_hold_seconds(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if (
        not math.isfinite(value)
        or value < MIN_CAPTION_HOLD_SECONDS
        or value > MAX_CAPTION_HOLD_SECONDS
    ):
        return None
    return value


def _valid_speaker_count(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < MIN_VISIBLE_SPEAKERS or value > MAX_VISIBLE_SPEAKERS:
        return None
    return value


def _valid_font_size(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < MIN_CAPTION_FONT_SIZE or value > MAX_CAPTION_FONT_SIZE:
        return None
    return value


def _valid_anchor_mode(value: object) -> str:
    return value if value in ANCHOR_MODES else DEFAULT_SETTINGS.anchor_mode


def _valid_growth(value: object) -> str:
    return value if value in ("up", "down") else DEFAULT_SETTINGS.growth


def _valid_anchor_executable(value: object) -> str:
    """A bare, lowercased image name, or the default.

    Bounded and separator-free so the settings file can never smuggle a
    path into the window-matching code - the match is against image
    BASENAMES read back from the processes themselves.
    """
    if not isinstance(value, str):
        return DEFAULT_SETTINGS.anchor_executable
    name = value.strip().lower()
    if not name or len(name) > MAX_ANCHOR_EXECUTABLE_CHARS:
        return DEFAULT_SETTINGS.anchor_executable
    if any(sep in name for sep in ("/", chr(92), ":")):
        return DEFAULT_SETTINGS.anchor_executable
    if any(ord(ch) < 32 or ord(ch) > 126 for ch in name):
        return DEFAULT_SETTINGS.anchor_executable
    return name


def _valid_anchor_monitor(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return DEFAULT_SETTINGS.anchor_monitor
    if value < 0 or value > MAX_ANCHOR_MONITOR:
        return DEFAULT_SETTINGS.anchor_monitor
    return value


def _valid_muted_speaker_ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    selected: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not SPEAKER_TOKEN_PATTERN.fullmatch(item):
            continue
        if item in seen:
            continue
        selected.append(item)
        seen.add(item)
        if len(selected) >= MAX_MUTED_SPEAKERS:
            break
    return tuple(selected)


def load_settings(path: Path | None = None) -> OverlaySettings:
    path = path or default_settings_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return DEFAULT_SETTINGS
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        return DEFAULT_SETTINGS
    # Anything but a literal true reads as off: focus silently muting people
    # because of a mangled settings file would be the wrong failure.
    focus_mode = payload.get("focus_mode") is True
    # Anything but a literal true reads as unmuted, so a mangled file
    # cannot silently hide a deaf member's only channel.
    muted = payload.get("muted") is True
    muted_speaker_ids = _valid_muted_speaker_ids(
        payload.get("muted_speaker_ids")
    )
    anchor_mode = _valid_anchor_mode(payload.get("anchor_mode"))
    anchor_executable = _valid_anchor_executable(payload.get("anchor_executable"))
    anchor_monitor = _valid_anchor_monitor(payload.get("anchor_monitor"))
    caption_hold_seconds = _valid_hold_seconds(
        payload.get("caption_hold_seconds")
    )
    if caption_hold_seconds is None:
        caption_hold_seconds = DEFAULT_SETTINGS.caption_hold_seconds
    max_visible_speakers = _valid_speaker_count(
        payload.get("max_visible_speakers")
    )
    if max_visible_speakers is None:
        max_visible_speakers = DEFAULT_SETTINGS.max_visible_speakers
    caption_font_size = _valid_font_size(payload.get("caption_font_size"))
    if caption_font_size is None:
        caption_font_size = DEFAULT_SETTINGS.caption_font_size
    growth = _valid_growth(payload.get("growth"))
    anchor_x = _valid_ratio(payload.get("anchor_x"))
    anchor_y = _valid_ratio(payload.get("anchor_y"))
    if anchor_x is None or anchor_y is None:
        # Bad placement resets placement; it does not flip unrelated display
        # choices that were still valid.
        return OverlaySettings(
            focus_mode=focus_mode,
            caption_hold_seconds=caption_hold_seconds,
            max_visible_speakers=max_visible_speakers,
            caption_font_size=caption_font_size,
            muted=muted,
            muted_speaker_ids=muted_speaker_ids,
            anchor_mode=anchor_mode,
            anchor_executable=anchor_executable,
            anchor_monitor=anchor_monitor,
        )
    return OverlaySettings(
        anchor_x=anchor_x,
        anchor_y=anchor_y,
        growth=growth,
        focus_mode=focus_mode,
        caption_hold_seconds=caption_hold_seconds,
        max_visible_speakers=max_visible_speakers,
        caption_font_size=caption_font_size,
        muted=muted,
        muted_speaker_ids=muted_speaker_ids,
        anchor_mode=anchor_mode,
        anchor_executable=anchor_executable,
        anchor_monitor=anchor_monitor,
    )


def save_settings(settings: OverlaySettings, path: Path | None = None) -> bool:
    """Atomically write placement and display preferences, never session data."""
    path = path or default_settings_path()
    caption_hold_seconds = _valid_hold_seconds(settings.caption_hold_seconds)
    if caption_hold_seconds is None:
        caption_hold_seconds = DEFAULT_SETTINGS.caption_hold_seconds
    max_visible_speakers = _valid_speaker_count(settings.max_visible_speakers)
    if max_visible_speakers is None:
        max_visible_speakers = DEFAULT_SETTINGS.max_visible_speakers
    caption_font_size = _valid_font_size(settings.caption_font_size)
    if caption_font_size is None:
        caption_font_size = DEFAULT_SETTINGS.caption_font_size
    anchor_x = _valid_ratio(settings.anchor_x)
    if anchor_x is None:
        anchor_x = DEFAULT_SETTINGS.anchor_x
    anchor_y = _valid_ratio(settings.anchor_y)
    if anchor_y is None:
        anchor_y = DEFAULT_SETTINGS.anchor_y
    payload = {
        "schema": SCHEMA,
        "anchor_x": round(anchor_x, 6),
        "anchor_y": round(anchor_y, 6),
        "focus_mode": settings.focus_mode is True,
        "caption_hold_seconds": round(caption_hold_seconds, 3),
        "max_visible_speakers": max_visible_speakers,
        "caption_font_size": caption_font_size,
        "muted": settings.muted is True,
        "muted_speaker_ids": list(
            _valid_muted_speaker_ids(settings.muted_speaker_ids)
        ),
        "growth": _valid_growth(settings.growth),
        "anchor_mode": _valid_anchor_mode(settings.anchor_mode),
        "anchor_executable": _valid_anchor_executable(settings.anchor_executable),
        "anchor_monitor": _valid_anchor_monitor(settings.anchor_monitor),
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
        return True
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def position_from_settings(
    client_rect: tuple[int, int, int, int],
    overlay_size: tuple[int, int],
    settings: OverlaySettings,
    *,
    bottom_reference_height: int | None = None,
) -> tuple[int, int]:
    left, top, width, height = client_rect
    overlay_width, overlay_height = overlay_size
    reference_height = (
        overlay_height
        if bottom_reference_height is None
        else max(int(bottom_reference_height), 0)
    )
    center_x = left + int(width * settings.anchor_x)
    center_y = top + int(height * settings.anchor_y)
    x = center_x - overlay_width // 2
    # The persisted point can refer to a stable baseline height. Caption
    # stacks then grow upward as more speakers are enabled instead of moving
    # their lower edge toward action bars and encounter effects.
    reference_y = center_y - reference_height // 2
    if settings.growth == "down":
        # Hanging feed: the stable line is the TOP edge, so a bigger
        # stack or row count extends downward and the first caption
        # stays exactly where the member pinned it.
        y = reference_y
    else:
        y = reference_y + reference_height - overlay_height
    return x, y


def settings_from_position(
    client_rect: tuple[int, int, int, int],
    overlay_size: tuple[int, int],
    position: tuple[int, int],
    current: OverlaySettings | None = None,
    *,
    bottom_reference_height: int | None = None,
) -> OverlaySettings:
    """Move the overlay, keeping every other setting the caller had.

    This returns what gets written over the settings file, so anything it
    forgets is silently switched off. It used to build a fresh
    OverlaySettings from the anchors alone, which meant dragging the
    overlay one pixel and pressing Save turned focus mode off - while the
    companion's toggle went on saying it was on, because nothing told it
    otherwise. Take the current settings and replace only the position.
    """
    settings = current if current is not None else DEFAULT_SETTINGS
    left, top, width, height = client_rect
    overlay_width, overlay_height = overlay_size
    x, y = position
    if width <= 0 or height <= 0:
        # No usable window to measure against: keep the position the
        # caller already had rather than silently recentering it.
        return settings
    reference_height = (
        overlay_height
        if bottom_reference_height is None
        else max(int(bottom_reference_height), 0)
    )
    # Dropped in the top half, the feed hangs and grows downward; in the
    # bottom half it stands and grows upward. Decided ONCE, here, and
    # STORED - position_from_settings reads the stored mode rather than
    # re-deriving it, so any anchor round-trips exactly under its mode.
    #
    # The standing interpretation is tried first and kept whenever it
    # lands at or below the middle, so every placement that was legal
    # before this feature existed keeps its exact meaning; only a drop
    # whose standing anchor would land in the top half becomes a hanging
    # feed. Both interpretations are computed from the same reference-box
    # midpoint the format has always stored.
    anchor_x = (x + overlay_width / 2 - left) / width
    standing_reference_y = y + overlay_height - reference_height
    standing_anchor_y = (
        standing_reference_y + reference_height / 2 - top
    ) / height
    hanging_anchor_y = (y + reference_height / 2 - top) / height
    if standing_anchor_y >= 0.5 or hanging_anchor_y >= 0.5:
        grows_down = False
        anchor_y = standing_anchor_y
    else:
        grows_down = True
        anchor_y = hanging_anchor_y
    return replace(
        settings,
        anchor_x=anchor_x,
        anchor_y=anchor_y,
        growth="down" if grows_down else "up",
    )
