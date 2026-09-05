"""Downtime UI: A comprehensive, feature-complete UI framework for World of Warcraft companion applications.

Designed for Downtime (Server Edition), Vemal's Ears (Deaf/Hard-of-Hearing Member Edition),
and Downtime Lite (Hosted Edition).
"""

from __future__ import annotations

from downtime_ui.backgrounds import CardShell, HeaderDivider, PanelSurface, WindowSurface
from downtime_ui.buttons import (
    ChatButton,
    CompactButton,
    DangerButton,
    GlassButton,
    IconButton,
    PrimaryButton,
    StandardButton,
    SupportButton,
    ToggleSwitch,
    VelvetButton,
)
from downtime_ui.cards import AlertBannerCard, ServiceCard, StatMetricCard
from downtime_ui.effects import (
    blend_colors,
    draw_drop_shadow,
    draw_glow_ring,
    hex_to_rgb,
    rgb_to_hex,
    rounded_rectangle,
)
from downtime_ui.headers import (
    DrawerHeader,
    HeroAudienceHeader,
    SectionHeader,
    WindowTitleBar,
)
from downtime_ui.tables import DataTable, EmptyStateView, TableHeader, TableRow
from downtime_ui.tabs import TabBar
from downtime_ui.indicators import (
    EditionBadge,
    JobBadge,
    ProgressBar,
    RecordingIndicator,
    SignalBar,
    StatusOrb,
)
from downtime_ui.inputs import HotkeyInput, ScrollSurface, StyledEntry
from downtime_ui.menus import ContextMenu, MenuItem
from downtime_ui.theme import (
    ACCENT,
    ACCENT_DOWNTIME,
    ACCENT_HOVER,
    ACCENT_LITE,
    ACCENT_VEMALS,
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
    DOWNTIME_LITE_THEME,
    DOWNTIME_THEME,
    EDITION_THEMES,
    MUTED,
    PANEL,
    PANEL_ALT,
    PANEL_HOVER,
    PATREON_CORAL,
    PAYPAL_BLUE,
    SUBTLE,
    SUCCESS,
    TEXT,
    VEMALS_EARS_THEME,
    WARNING,
    EditionTheme,
    create_font_scale,
    get_theme_for_edition,
)

__version__ = "1.0.0"

__all__ = [
    # Theme & Tokens
    "BG",
    "PANEL",
    "PANEL_ALT",
    "PANEL_HOVER",
    "BORDER",
    "BORDER_STRONG",
    "TEXT",
    "MUTED",
    "SUBTLE",
    "SUCCESS",
    "WARNING",
    "DANGER",
    "BUTTON_DARK_BLUE",
    "BUTTON_DARK_BLUE_HOVER",
    "BUTTON_DARK_BLUE_ACTIVE",
    "BUTTON_DARK_BLUE_BORDER",
    "BUTTON_SHINE",
    "ACCENT",
    "ACCENT_HOVER",
    "ACCENT_DOWNTIME",
    "ACCENT_VEMALS",
    "ACCENT_LITE",
    "CLOCKFIRE",
    "CLOCKFIRE_HOVER",
    "PATREON_CORAL",
    "PAYPAL_BLUE",
    "EditionTheme",
    "DOWNTIME_THEME",
    "VEMALS_EARS_THEME",
    "DOWNTIME_LITE_THEME",
    "EDITION_THEMES",
    "get_theme_for_edition",
    "create_font_scale",
    # Effects & Geometry
    "hex_to_rgb",
    "rgb_to_hex",
    "blend_colors",
    "rounded_rectangle",
    "draw_drop_shadow",
    "draw_glow_ring",
    # Backgrounds & Surfaces
    "CardShell",
    "PanelSurface",
    "WindowSurface",
    "HeaderDivider",
    # Buttons & Switches
    "PrimaryButton",
    "StandardButton",
    "GlassButton",
    "CompactButton",
    "ChatButton",
    "DangerButton",
    "SupportButton",
    "IconButton",
    "ToggleSwitch",
    "VelvetButton",
    # Cards
    "ServiceCard",
    "AlertBannerCard",
    "StatMetricCard",
    # Headers
    "WindowTitleBar",
    "HeroAudienceHeader",
    "SectionHeader",
    "DrawerHeader",
    # Tables
    "DataTable",
    "TableHeader",
    "TableRow",
    "TabBar",
    "EmptyStateView",
    # Indicators
    "StatusOrb",
    "RecordingIndicator",
    "EditionBadge",
    "JobBadge",
    "ProgressBar",
    "SignalBar",
    # Menus
    "ContextMenu",
    "MenuItem",
    # Inputs
    "StyledEntry",
    "HotkeyInput",
    "ScrollSurface",
]
