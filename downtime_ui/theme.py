"""Design system tokens, typography scales, and edition themes for Downtime UI."""

from __future__ import annotations

from dataclasses import dataclass
from tkinter import font as tkfont
import tkinter as tk

# Nightwatch Core Palette: A restrained, high-contrast dark raid-console palette.
BG = "#080b10"
PANEL = "#101823"
PANEL_ALT = "#182433"
PANEL_HOVER = "#203044"
BORDER = "#2a3c52"
BORDER_STRONG = "#3a5674"
TEXT = "#eaf3fa"
MUTED = "#91a4b7"
SUBTLE = "#7f93a7"

# Semantic Status Colors
SUCCESS = "#55d58a"
WARNING = "#f0b45d"
DANGER = "#ff6b72"

# Dark Blue Button Palette (high-visibility midnight navy)
BUTTON_DARK_BLUE = "#13233c"
BUTTON_DARK_BLUE_HOVER = "#1c355c"
BUTTON_DARK_BLUE_ACTIVE = "#26487e"
BUTTON_DARK_BLUE_BORDER = "#355b94"
BUTTON_SHINE = "#ffffff"

# Brand & Accent Colors
ACCENT_DOWNTIME = "#35c6f4"
ACCENT_DOWNTIME_HOVER = "#70d8f7"

ACCENT_VEMALS = "#e4bd6b"
ACCENT_VEMALS_HOVER = "#f3d798"

ACCENT_LITE = "#78a7ff"
ACCENT_LITE_HOVER = "#9ebbff"

CLOCKFIRE = "#ffab3d"
CLOCKFIRE_HOVER = "#ffc36e"

PATREON_CORAL = "#ff6c66"
PAYPAL_BLUE = "#5fa8ff"

# Default fallback accent
ACCENT = ACCENT_DOWNTIME
ACCENT_HOVER = ACCENT_DOWNTIME_HOVER


@dataclass(frozen=True)
class EditionTheme:
    """Design configuration customized per companion edition."""

    name: str
    edition_id: str
    badge_label: str
    accent: str
    accent_hover: str
    tagline: str
    detail: str
    bg: str = BG
    panel: str = PANEL
    panel_alt: str = PANEL_ALT
    border: str = BORDER
    border_strong: str = BORDER_STRONG
    text: str = TEXT
    muted: str = MUTED
    subtle: str = SUBTLE
    panel_hover: str = PANEL_HOVER
    conversation: str = CLOCKFIRE
    conversation_hover: str = CLOCKFIRE_HOVER
    conversation_text: str = BG


# Predefined themes for the three companion applications
DOWNTIME_THEME = EditionTheme(
    name="Downtime",
    edition_id="full-local",
    badge_label="LOCAL",
    accent=ACCENT_DOWNTIME,
    accent_hover=ACCENT_DOWNTIME_HOVER,
    tagline="Raid tools, ready before the pull.",
    detail="Gear simulations and encounter builds, ready for your next raid.",
)

VEMALS_EARS_THEME = EditionTheme(
    name="Vemal's Ears",
    edition_id="member",
    badge_label="Voice & text",
    accent=ACCENT_VEMALS,
    accent_hover=ACCENT_VEMALS_HOVER,
    tagline="Your raid, in words.",
    detail="Live captions and a fast way to message your team.",
    bg="#0b0b0d",
    panel="#141416",
    panel_alt="#202022",
    panel_hover="#30272a",
    border="#463b31",
    border_strong="#79613b",
    text="#f5eee2",
    muted="#c0b4a1",
    subtle="#afa392",
    conversation="#a91e2d",
    conversation_hover="#c73342",
    conversation_text="#fff1e5",
)

DOWNTIME_LITE_THEME = EditionTheme(
    name="Downtime Lite",
    edition_id="downtime-hosted",
    badge_label="HOSTED",
    accent=ACCENT_LITE,
    accent_hover=ACCENT_LITE_HOVER,
    tagline="Raid tools without the local upkeep.",
    detail="Gear and talent checks stay connected to your guild server.",
)

EDITION_THEMES = {
    "full-local": DOWNTIME_THEME,
    "member": VEMALS_EARS_THEME,
    "downtime-hosted": DOWNTIME_LITE_THEME,
}


def get_theme_for_edition(edition_id: str) -> EditionTheme:
    """Resolve an EditionTheme from an edition identifier."""
    return EDITION_THEMES.get(edition_id, DOWNTIME_THEME)


def create_font_scale(root: tk.Misc | None = None) -> dict[str, tkfont.Font]:
    """Create the typography hierarchy used across all components.

    Uses Segoe UI where available, falling back to clean system sans-serif.
    """
    family = "Segoe UI"
    if root is not None:
        try:
            available = tkfont.families(root)
            if family not in available:
                family = "Helvetica" if "Helvetica" in available else "TkDefaultFont"
        except Exception:
            pass

    return {
        "hero": tkfont.Font(root=root, family=family, size=20, weight="bold"),
        "heading": tkfont.Font(root=root, family=family, size=13, weight="bold"),
        "subheading": tkfont.Font(root=root, family=family, size=11, weight="bold"),
        "body": tkfont.Font(root=root, family=family, size=10),
        "button": tkfont.Font(root=root, family=family, size=10, weight="bold"),
        "meta": tkfont.Font(root=root, family=family, size=9),
        "status": tkfont.Font(root=root, family=family, size=9, weight="bold"),
        "keycap": tkfont.Font(root=root, family="Consolas", size=9, weight="bold"),
    }
