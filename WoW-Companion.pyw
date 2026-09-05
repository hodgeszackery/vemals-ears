"""VE-owned UI snapshot, initially extracted from the Downtime implementation.

Downtime owns gear and talent tools. The Vemals-Ears.pyw entrypoint owns
captions and guild messages, with separate settings and Windows identity.
The legacy filename supports installed native launchers. See ADR-001.txt for
the source boundary and the intentionally retained common UI implementation.

The .pyw extension and CREATE_NO_WINDOW keep background helpers quiet. Custom
window chrome follows each app's theme, while every shell integration
falls back to native Windows behavior when the custom path cannot be proven.
"""

from __future__ import annotations

import asyncio
import csv
import ctypes
import json
import math
import ntpath
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import tkinter as tk
import unicodedata
import webbrowser
import winreg
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass, field, replace
from pathlib import Path
from tkinter import font as tkfont
from tkinter import ttk
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlsplit

from downtime_ui import (
    CardShell,
    ChatButton,
    CompactButton,
    ContextMenu,
    EditionBadge,
    HeaderDivider,
    IconButton,
    JobBadge,
    PrimaryButton,
    ProgressBar,
    RecordingIndicator,
    SectionHeader,
    SignalBar,
    StandardButton,
    VelvetButton,
    StyledEntry,
    get_theme_for_edition,
)

ROOT = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("LOCALAPPDATA", ROOT)) / "GearHelper"
LOCK = STATE_DIR / "companion-window.lock"
SETTINGS_FILE = STATE_DIR / "companion-settings.json"
ICON_FILE = STATE_DIR / "companion.ico"
MEMBER_ICON_FILE = STATE_DIR / "vemals-ears.ico"
HOSTED_ICON_FILE = STATE_DIR / "downtime-lite.ico"
PAIR_SIGNAL = STATE_DIR / "vemals-ears-paired.signal"
# Written by the uninstaller to ask for a clean exit before it falls
# back to killing the process. Deleting it is the acknowledgement: a
# build that predates it simply never consumes it, and the uninstaller
# reads that silence as "escalate".
QUIT_SIGNAL = STATE_DIR / "companion-quit.request"
MEMBER_EDITION_MARKER = ROOT / "vemals-ears-member.json"
MEMBER_EDITION_DOCUMENT = {"edition": "member", "schema": 1}
HOSTED_EDITION_MARKER = ROOT / "downtime-hosted.json"
HOSTED_EDITION_DOCUMENT = {"edition": "downtime-hosted", "schema": 1}
FULL_EDITION = "full-local"
MEMBER_EDITION = "member"
HOSTED_EDITION = "downtime-hosted"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
INSTANCE_MUTEX_NAME = r"Local\Downtime.Companion"
ACTIVE_EDITION: str | None = None
# Historical identifier retained for compatibility; another product must
# never invoke this uninstaller.
VE_UNINSTALL_KEY = (
    r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    r"\{6A72924D-172F-4A52-A0EC-D26A302C043A}_is1"
)
INSTANCE_MUTEX_HANDLE: int | None = None

RUN_KEY = "Software\\Microsoft\\Windows\\CurrentVersion\\Run"
RUN_VALUE = "Downtime"
# The two editions run side by side on one machine, so each owns its own
# Run value and must never delete the other's.
FULL_PRODUCT_NAME = "Downtime"
MEMBER_PRODUCT_NAME = "Vemal's Ears"
LEGACY_RUN_VALUE = "WoW Companion"
# Loopback only, and fixed: never derived from pairing data, so a guild
# cannot point the operator at an address of its choosing.
OPERATOR_CONSOLE_URL = "http://127.0.0.1:8771/"
# These are public, fixed destinations chosen by the product owner. Patreon
# owns the custom monthly amount form; the companion only opens that form.
PATREON_CUSTOM_PLEDGE_URL = "https://www.patreon.com/join/downtimeaddons"
PAYPAL_DONATE_URL = (
    "https://www.paypal.com/donate/?business=NG5E2DXV3V4ZW&no_recurring=0"
    "&currency_code=USD"
)
SUPPORT_URLS = frozenset((PATREON_CUSTOM_PLEDGE_URL, PAYPAL_DONATE_URL))
# The autostart value was named "WoW Companion" before the desktop
# app was renamed. A machine that enabled autostart under the old name
# keeps that entry until it is removed, or the app starts twice - so the
# disable and enable paths both clear it.
LEGACY_RUN_VALUES = ("WoW Companion",)

# Nightwatch: a restrained raid-console palette drawn from the existing
# Downtime clock artwork. Cool light belongs to system state; the one warm
# accent belongs to a person speaking to their raid.
BG = "#080b10"
PANEL = "#101823"
PANEL_ALT = "#182433"
PANEL_HOVER = "#203044"
BORDER = "#2a3c52"
BORDER_STRONG = "#3a5674"
TEXT = "#eaf3fa"
MUTED = "#91a4b7"
SUBTLE = "#7f93a7"
ACCENT = "#35c6f4"
ACCENT_HOVER = "#70d8f7"
LITE_ACCENT = "#78a7ff"
CLOCKFIRE = "#ffab3d"
CLOCKFIRE_HOVER = "#ffc36e"
SUCCESS = "#55d58a"
WARNING = "#f0b45d"
DANGER = "#ff6b72"
PATREON_CORAL = "#ff6c66"
PAYPAL_BLUE = "#5fa8ff"

QUICK_CHAT_TEXT_LIMIT = 320
QUICK_CHAT_NAME_LIMIT = 32
# Below this length, finding the member's line inside a server message is
# coincidence rather than an echo. See transmit_quick_chat.
QUICK_CHAT_ECHO_MIN_CHARS = 12
QUICK_CHAT_BIDI_CONTROLS = frozenset({
    "FSI",
    "LRE",
    "LRI",
    "LRO",
    "PDF",
    "PDI",
    "RLE",
    "RLI",
    "RLO",
})
DISCORD_WORKER_CONSOLE_URL = "http://127.0.0.1:8771/api/discord-worker"
CONSOLE_CONTROL_HEADER = "X-Vemals-Ears-Console"
EXTERNAL_SERVICE_POLL_SECONDS = 2.0
DISCORD_WORKER_STATES = frozenset({
    "continue_pending",
    "not installed",
    "pause_pending",
    "paused",
    "running",
    "start_pending",
    "stop_pending",
    "stopped",
    "unknown",
})

# Segoe MDL2 Assets is the font Windows itself draws chrome with. The
# fallbacks are plain ASCII so the buttons still say something if a future
# Windows drops it.
GLYPHS = {
    "settings": ("\ue713", "*"),
    "minimize": ("\ue921", "-"),
    "maximize": ("\ue922", "[ ]"),
    "restore": ("\ue923", "[+]"),
    "close": ("\ue8bb", "X"),
}

# A line like "[####....] 3/9 Nymrissa" is per-item progress; it belongs on
# the status line rather than as another log entry.
# Tk class names of widgets that own their own keystrokes. Checked by
# name because ttk widgets are not subclasses of their tk namesakes.
TEXT_ENTRY_WIDGET_CLASSES = frozenset({
    "Entry", "TEntry", "Text", "Spinbox", "TSpinbox", "TCombobox",
})

PROGRESS = re.compile(r"^\[[#.\s-]*\]\s+(\d+)\s*/\s*(\d+)")

# An automatic restart backs off instead of hammering. A child that
# cannot start - no pairing, no network, a file the bundle does not
# ship - used to be respawned every tick for the life of the session.
RESTART_BASE_SECONDS = 3.0
RESTART_MAX_SECONDS = 300.0
# Give up after this many consecutive failures and say so, rather than
# retrying invisibly forever. Managed editions hide the Stop button, so
# for a member this status is the only way to learn anything is wrong.
RESTART_GIVE_UP_AFTER = 8
# A child that stayed up this long counts as healthy, so the next death
# starts the backoff over. Without it, a child that dies after 20
# minutes every time would eventually be given up on.
RESTART_HEALTHY_SECONDS = 60.0
# The Activity log is a session-long sink for several chatty children,
# and a Tk Text keeps every line plus two tag ranges per line in memory
# until the process exits.
MAX_ACTIVITY_LINES = 2000
DISCORD_GUILD_ID = re.compile(r"^\d{17,20}$")
DISCORD_DESKTOP_GUILD_PREFIX = "discord://-/channels/"
DISCORD_WEB_GUILD_PREFIX = "https://discord.com/channels/"


JOB_BADGE_STYLES = {
    "ready": ("Ready", ACCENT),
    "queued": ("Queued", LITE_ACCENT),
    "checking": ("Checking", WARNING),
    "done": ("Done", SUCCESS),
    "stopped": ("Stopped", MUTED),
    "attention": ("Needs attention", DANGER),
}

LIFECYCLE_BADGE_STYLES = {
    "checking": ("Checking", WARNING),
    "running": ("Running", SUCCESS),
    "online": ("Online", SUCCESS),
    "starting": ("Starting", WARNING),
    "stopping": ("Stopping", WARNING),
    "stopped": ("Stopped", MUTED),
    "setup": ("Setup needed", WARNING),
    "attention": ("Needs attention", DANGER),
    "unavailable": ("Unavailable", DANGER),
}


# The Activity summary's last resort. The marker tables above are the
# explicit channel; this catches a failure nobody anticipated. It reads a
# child's own words, so it has to distinguish a line that REPORTS a
# failure from one that merely contains a word that can measure one.
#
# Gear Helper is a simulation tool. "0.1% error" is its accuracy target,
# printed on three separate lines during a completely normal run, and a
# bare "error" substring painted every one of them as a problem while the
# badges read Running and Checking.
FAILURE_PHRASES = (
    "error",
    "failed",
    "could not",
    "unexpectedly",
)

# Gear Helper is a simulation tool and error is its ACCURACY TARGET, so
# these read as measurements wherever they appear. Three of its lines say
# so during a completely normal run - "at 0.6% error before the precise
# pass", "refining them at 0.1% error", "at 0.1% error: 18 profilesets" -
# and a bare "error" substring painted every one of them as a problem
# while the badges correctly read Running and Checking.
#
# Do not narrow this to punctuation. The first attempt keyed on "error:"
# assuming a measurement never carries a colon; the third line above has
# one, introducing the profileset count.
MEASUREMENT_PHRASES = (
    "% error",
    "target error",
    "screen error",
    "error=",
)

# Lines that are already handling themselves. Housekeeping that names its
# own retry is describing routine work, and a producer that labelled the
# line a note meant it - those sentences interpolate an exception, so the
# DETAIL can say "could not" while the sentence says an optional step was
# skipped and will be swept next run.
SELF_HANDLED_PHRASES = (
    "will be retried",
    "next run will",
    "retrying",
    "note:",
    "skipping optional",
)


# The structured channel. The watcher prints one of these beside the
# human sentence for every request transition, so the badge can be told
# what happened instead of guessing it from prose. Everything below this
# block - the marker tables, the measurement-phrase allowlist,
# reads_as_a_failure itself - remains as the FALLBACK for lines that do
# not carry it: a child that predates the prefix, or output nobody
# anticipated. The two prior misclassification bugs in this window were
# both the fallback being asked to carry the whole load.
GHSTATUS_PREFIX = "@ghstatus "

# phase -> badge state, in the badge vocabulary the window already has.
# An unknown phase is NOT mapped: a newer child must degrade to being
# read as prose, never render a badge this build has no meaning for.
GHSTATUS_PHASES = {
    "queued": "queued",
    "running": "checking",
    "done": "done",
    "failed": "attention",
}

# A running payload may carry the watcher's progress: which pass of the
# sim, how many profilesets are done of how many, simc's own estimate of
# the time left. The window draws a filling bar from it - Shino,
# 2026-09-04: "instead of Preparing candidates I want to see the status
# bar filling up". The stage key is the watcher's; the wording is ours.
PROGRESS_STAGE_LABELS = {
    "screen": "Screening pass",
    "precise": "Precise pass",
    "vault": "Great Vault pass",
    "bis": "Best in slot pass",
}
# The rows that run a sim and can therefore report one. Built hidden on
# every edition: the local watcher fills it from @ghstatus lines, the
# hosted jobs client from its "[####....] 3/9" prose count.
PROGRESS_SERVICE_KEYS = frozenset({"gear", "talents"})


def describe_time_left(seconds: float) -> str:
    """simc's estimate, rounded the way a person would say it."""
    if seconds < 60:
        return "under a minute left"
    minutes = int(math.ceil(seconds / 60.0))
    return f"about {minutes} min left"


def _finite_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def describe_progress(payload: dict) -> tuple[str, float, str | None] | None:
    """(status text, fill, bar caption) for a running payload, or None.

    None means "this line carries no progress" - a plain running status,
    or one from a newer watcher whose fields this build cannot read - and
    the bar is left exactly as it was. Stage, done and total are the
    contract; an unknown stage still fills, labelled "Simulating". Every
    field is checked before it is used: this runs inside the window's
    tick, and a NaN in a payload must never be what stops the clock.
    The caption is None for "let the bar say its percent" - the count is
    already on the status line - and empty for the baseline, which has
    no honest percent.
    """
    if payload.get("phase") != "running":
        return None
    done = payload.get("done")
    total = payload.get("total")
    fraction = payload.get("fraction")
    if not (_finite_number(done) and _finite_number(total) and _finite_number(fraction)):
        return None
    fraction = max(0.0, min(1.0, float(fraction)))
    stage = payload.get("stage")
    label = PROGRESS_STAGE_LABELS.get(stage, "Simulating") if isinstance(stage, str) else "Simulating"
    if total <= 0:
        # The baseline outfit runs before the profilesets; simc gives it
        # no honest count, so neither do we.
        return label + " - simulating the baseline", 0.0, ""
    total = int(total)
    done = max(0, min(int(done), total))
    text = f"{label} {done}/{total}"
    left = payload.get("left")
    if _finite_number(left) and left >= 0:
        text += " - " + describe_time_left(float(left))
    return text, fraction, None


def parse_status_line(stripped: str) -> dict | None:
    """The payload of an @ghstatus line, or None if it is not one of ours.

    None means "treat it as an ordinary line". Malformed JSON, a wrong
    schema, or an unknown phase all land there deliberately - a truncated
    or future status becomes visible prose instead of vanishing.
    """
    try:
        payload = json.loads(stripped[len(GHSTATUS_PREFIX):])
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schema") != 1:
        return None
    if payload.get("phase") not in GHSTATUS_PHASES:
        return None
    return payload


STATUS_LINE_LIMIT = 70
FAILED_PREFIX = "Failed - "


def shortened_detail(detail: str, budget: int = STATUS_LINE_LIMIT - len(FAILED_PREFIX)) -> str:
    """As much of a failure detail as fits, ending where a reader expects.

    The old cut was a flat slice, and a slice does not know what a word is.
    A healer's refusal came out as "Failed - Gear Helper ranks gear by
    damage output, which does not measu" - a fragment with no verb, no
    subject it was about, and nothing to do next. The reader could not even
    tell it had been cut on purpose.

    So: keep whole sentences while whole sentences fit, and fall back to
    whole words with an ellipsis when the very first sentence is too long
    to show. Either way the line ends somewhere a person reads as an
    ending. The unabridged text is one glance away in Activity.
    """
    detail = detail.strip()
    if len(detail) <= budget:
        return detail

    kept = ""
    # Sentence ends only. A semicolon is not one: stopping the line at
    # "Healing specs are not ranked yet;" would read as a dropped
    # connection rather than as a finished thought.
    for piece in re.split(r"(?<=[.!?])\s+", detail):
        candidate = (kept + " " + piece).strip() if kept else piece
        if len(candidate) > budget:
            break
        kept = candidate
    if kept:
        return kept

    # Not even one sentence fits. Cut on a space so no word is halved, and
    # say so, rather than stopping mid-syllable as if the text just ended.
    room = budget - 3
    clipped = detail[:room]
    spaced = clipped.rsplit(" ", 1)[0] if " " in clipped else clipped
    return spaced.rstrip(" ,;:.") + "..."


def describe_status(payload: dict) -> tuple[str, str, str]:
    """Status-bar text, rail color, and badge state for a payload."""
    phase = payload["phase"]
    job_state = GHSTATUS_PHASES[phase]
    if phase == "queued":
        remaining = None
        until = payload.get("cooldownUntil")
        if isinstance(until, (int, float)) and not isinstance(until, bool):
            remaining = max(0, int(until - time.time()))
        if remaining:
            minutes = max(1, remaining // 60)
            return (
                f"Queued - next sim slot in about {minutes:.0f} min",
                LITE_ACCENT,
                job_state,
            )
        return "Queued for the next sim", LITE_ACCENT, job_state
    if phase == "running":
        progress = describe_progress(payload)
        if progress is not None:
            return progress[0], WARNING, job_state
        # A coarse step - "Screening the full gear candidate pool." -
        # is the sentence the watcher printed to Activity, on the badge
        # line as well. A running line with neither is the old shape.
        detail = payload.get("detail")
        if isinstance(payload.get("step"), int) and isinstance(detail, str) and detail.strip():
            return detail.strip()[:70], WARNING, job_state
        return "Simulating - a few minutes", WARNING, job_state
    if phase == "done":
        return "Results ready - reload WoW", SUCCESS, job_state
    detail = payload.get("detail")
    if isinstance(detail, str) and detail.strip():
        # The reason, on the badge line itself, instead of a generic
        # pointer at the Activity drawer. Shortened: the full text is in
        # Activity via the human line printed beside this payload.
        return "Failed - " + shortened_detail(detail.strip()), DANGER, job_state
    return "Needs attention - open Activity", DANGER, job_state


def reads_as_a_failure(line: str) -> bool:
    """True when a child's line asserts a failure rather than measuring one."""
    lowered = line.casefold()
    if any(phrase in lowered for phrase in MEASUREMENT_PHRASES):
        return False
    if any(phrase in lowered for phrase in SELF_HANDLED_PHRASES):
        return False
    return any(phrase in lowered for phrase in FAILURE_PHRASES)


# Both status boxes carry the same kind of thing - a short state word - so
# they should be the same box. They were not: the job badge was 15
# characters and the lifecycle badge 12, and because a Tk width is a
# MINIMUM the narrower one grew whenever it had something long to say.
# "Needs attention" is 15 characters and both of them can show it, so the
# lifecycle box changed size as the state changed.
#
# Derived rather than typed, so adding a longer label widens both boxes
# instead of quietly reintroducing the mismatch.
STATUS_BADGE_WIDTH = max(
    len(label)
    for label, _color in list(JOB_BADGE_STYLES.values())
    + list(LIFECYCLE_BADGE_STYLES.values())
)


def job_status_badge(state: str) -> tuple[str, str]:
    """Return the visible label and color for an explicit watcher state."""
    try:
        return JOB_BADGE_STYLES[state]
    except KeyError:
        raise ValueError("unknown job status") from None


@dataclass
class ServiceSpec:
    key: str
    label: str
    script: Path
    color: str
    # (substring, status text, colour, job state) matched against child output.
    markers: tuple[tuple[str, str, str, str | None], ...]
    note: str = ""
    action_label: str = ""
    action_script: Path | None = None
    second_action_label: str = ""
    second_action_script: Path | None = None
    third_action_label: str = ""
    third_action_script: Path | None = None
    arguments: tuple[str, ...] = ("--watch",)
    auto_restart: bool = False
    show_lifecycle_controls: bool = True
    separate_lifecycle_controls: bool = False
    external_control: bool = False
    start_on_launch: bool = True


SERVICES = (
    ServiceSpec(
        key="gear",
        label="Gear Helper",
        script=ROOT / "GearHelper" / "Tools" / "gearhelper_auto.py",
        color=ACCENT,
        markers=(
            # First on purpose. The "Gear Helper:" prefix is used for
            # exactly three things in the watcher and all of them are
            # errors, so a line carrying it must never be classified by a
            # later success marker because of what else it happens to say.
            (
                "Gear Helper:",
                "Needs attention - open Activity",
                DANGER,
                "attention",
            ),
            (
                "Watching Gear Helper SavedVariables",
                "Watching for a sim request",
                ACCENT,
                "ready",
            ),
            (
                "Gear Helper request queued:",
                "Queued for the next sim",
                LITE_ACCENT,
                "queued",
            ),
            (
                "Gear Helper queue cleared.",
                "Watching for a sim request",
                ACCENT,
                "ready",
            ),
            (
                "Starting Gear Helper request",
                "Starting the gear check",
                WARNING,
                "checking",
            ),
            ("Comparing", "Preparing candidates", ACCENT, "checking"),
            ("Holding the", "Preparing candidates", ACCENT, "checking"),
            (
                "Running the local raid",
                "Simulating - a few minutes",
                WARNING,
                "checking",
            ),
            (
                "Reload fallback:",
                "Results ready - reload WoW",
                SUCCESS,
                "done",
            ),
            (
                "Local comparison completed",
                "Results ready - choose Load Results",
                SUCCESS,
                "done",
            ),
            (
                "Great Vault comparison completed",
                "Results ready - choose Load Results",
                SUCCESS,
                "done",
            ),
            (
                # The trailing space matters. Without it this also matches
                # "Generated.lua", which the watcher's own error text names
                # when a write fails - and first-substring-wins meant a
                # thrown-away result reported itself as ready.
                "Generated ",
                "Results ready - choose Load Results",
                SUCCESS,
                "done",
            ),
            (
                "Gear Helper result loaded by WoW.",
                "Results ready - reload WoW",
                SUCCESS,
                "done",
            ),
            (
                "already current",
                "Watching for a sim request",
                ACCENT,
                "ready",
            ),
        ),
    ),
    ServiceSpec(
        key="talents",
        label="Boss Talents",
        script=ROOT / "BossTalents" / "Tools" / "boss_talents_auto.py",
        color=SUCCESS,
        markers=(
            (
                "Watching for BossTalents requests",
                "Watching for a build request",
                ACCENT,
                "ready",
            ),
            ("Fetching ", "Checking encounter logs", WARNING, "checking"),
            ("Reload fallback:", "Results ready - reload WoW", SUCCESS, "done"),
            ("Ready:", "Results ready - choose Load Results", SUCCESS, "done"),
            (
                "BossTalents result loaded by WoW.",
                "Results ready - reload WoW",
                SUCCESS,
                "done",
            ),
            (
                "Waiting:",
                "Waiting for attention - open Activity",
                WARNING,
                "attention",
            ),
            (
                "Request failed:",
                "Needs attention - open Activity",
                DANGER,
                "attention",
            ),
            (
                "Unexpected error:",
                "Needs attention - open Activity",
                DANGER,
                "attention",
            ),
            ("Error:", "Needs attention - open Activity", DANGER, "attention"),
            ("Stopped.", "Stopped", MUTED, "stopped"),
        ),
        note="Needs Warcraft Logs credentials: use the Boss Talents setup tool once.",
    ),
    ServiceSpec(
        key="voice",
        label="Vemal's Ears",
        script=ROOT / "GuildHub" / "client.py",
        color=ACCENT,
        markers=(
            (
                "waiting for a pairing link",
                "Open your guild pairing link",
                WARNING,
                None,
            ),
            ("needs a new pairing link", "Pair this computer again", WARNING, None),
            (
                "waiting for you to join the active Discord voice channel",
                "Join Discord voice to see captions",
                WARNING,
                None,
            ),
            (
                "opening the caption window",
                "Connecting to guild captions",
                ACCENT,
                None,
            ),
            (
                "connected to guild captions",
                "Connected - captions ready",
                SUCCESS,
                None,
            ),
            (
                "cannot reach the guild server",
                "Guild server unavailable - retrying",
                WARNING,
                None,
            ),
            ("Error:", "Needs attention - open Activity", DANGER, None),
            ("Stopped.", "Stopped", MUTED, None),
        ),
        note=(
            "Text-only guild captions. When the Vemal's Ears Bot is present, "
            "it should be assumed someone is reading speech ONLY if the speaker has consented."
        ),
        action_label="Pair",
        action_script=ROOT / "GuildHub" / "setup.py",
        second_action_label="Move",
        second_action_script=ROOT / "VoiceCaptions" / "placement.py",
        third_action_label="Voice setup",
        third_action_script=ROOT / "VoiceCaptions" / "setup.py",
        separate_lifecycle_controls=True,
    ),
    ServiceSpec(
        key="voice_discord",
        label="Vemal's Ears Discord",
        script=ROOT / "GuildHub" / "worker_launcher.py",
        color=LITE_ACCENT,
        markers=(),
        note=(
            "The guild Discord listener. It only transcribes speakers who "
            "have turned consent on."
        ),
        separate_lifecycle_controls=True,
        external_control=True,
        start_on_launch=False,
    ),
)


VOICE_SERVICE = next(spec for spec in SERVICES if spec.key == "voice")

# The caption services. An edition with a Vemal's Ears page draws their
# rows THERE and nowhere else - Shino, 2026-09-04: "remove Vemal's ears
# from the main screen and only leave the vemal's ears tab".
CAPTION_PAGE_SERVICE_KEYS = frozenset({"voice", "voice_discord"})

MEMBER_SERVICES = (
    replace(
        VOICE_SERVICE,
        third_action_label="",
        third_action_script=None,
        auto_restart=True,
        show_lifecycle_controls=False,
    ),
)

HOSTED_JOB_MARKERS = (
    (
        "could not read the saved Downtime pairing.",
        "Pairing needs attention - open Activity",
        DANGER,
        "attention",
    ),
    (
        "jobs are waiting for Downtime pairing.",
        "Pair this computer",
        WARNING,
        "attention",
    ),
    (
        "could not read the saved Vemal's Ears pairing.",
        "Pairing needs attention - open Activity",
        DANGER,
        "attention",
    ),
    (
        "jobs are waiting for Vemal's Ears pairing.",
        "Pair this computer",
        WARNING,
        "attention",
    ),
    (
        "jobs are not enabled for this paired device.",
        "Hosted jobs are not enabled",
        DANGER,
        "attention",
    ),
    (
        "no longer authorized. Pair it again.",
        "Pair this computer again",
        DANGER,
        "attention",
    ),
    (
        "jobs are waiting for World of Warcraft.",
        "Waiting for WoW",
        WARNING,
        "attention",
    ),
    (
        "jobs are waiting for the ",
        "Waiting for the WoW addon",
        WARNING,
        "attention",
    ),
    (" job queued:", "Queued on the server", ACCENT, "queued"),
    (" job running:", "Running on the server", WARNING, "checking"),
    (
        " result ready to load in WoW.",
        "Results ready - choose Load Results",
        SUCCESS,
        "done",
    ),
    (
        " result written. Restart WoW, then choose Load Results.",
        "Restart WoW, then Load Results",
        WARNING,
        "done",
    ),
    (
        " result ready through the reload fallback.",
        "Results ready - reload WoW",
        SUCCESS,
        "done",
    ),
    (
        # Older hosted workers emitted this line after observing a later
        # SavedVariables write. That does not prove the player actually
        # loaded the result, so retain the terminal ready state instead of
        # presenting a fictional import as a fresh idle state.
        " result loaded by WoW.",
        "Results ready - reload WoW",
        SUCCESS,
        "done",
    ),
    (" job failed:", "Needs attention - open Activity", DANGER, "attention"),
    (" job cancelled:", "Request canceled - open Activity", DANGER, "attention"),
    (" job expired:", "Request expired - open Activity", DANGER, "attention"),
    (
        "server is busy; retrying shortly.",
        "Server busy - retrying",
        WARNING,
        "checking",
    ),
    (
        "server is temporarily unreachable; retrying.",
        "Server unavailable - retrying",
        WARNING,
        "checking",
    ),
    (
        "Hosted addon job problem:",
        "Needs attention - open Activity",
        DANGER,
        "attention",
    ),
    ("Error:", "Needs attention - open Activity", DANGER, "attention"),
)

HOSTED_SERVICES = (
    ServiceSpec(
        key="gear",
        label="Gear Helper",
        script=ROOT / "GuildHub" / "jobs_client.py",
        color=ACCENT,
        markers=(
            (
                "Watching for hosted Gear Helper jobs.",
                "Waiting for a request",
                SUCCESS,
                "ready",
            ),
            *HOSTED_JOB_MARKERS,
        ),
        note="Gear comparisons run on the paired server, not on this computer.",
        arguments=("--watch", "--kind", "gear"),
        auto_restart=True,
        show_lifecycle_controls=False,
    ),
    ServiceSpec(
        key="talents",
        label="Boss Talents",
        script=ROOT / "GuildHub" / "jobs_client.py",
        color=SUCCESS,
        markers=(
            (
                "Watching for hosted BossTalents jobs.",
                "Waiting for a request",
                SUCCESS,
                "ready",
            ),
            *HOSTED_JOB_MARKERS,
        ),
        note="Talent jobs use the paired server's Warcraft Logs connection.",
        arguments=("--watch", "--kind", "talents"),
        auto_restart=True,
        show_lifecycle_controls=False,
    ),
    replace(
        VOICE_SERVICE,
        third_action_label="",
        third_action_script=None,
        auto_restart=True,
        show_lifecycle_controls=False,
    ),
)


def normalize_edition(edition: str | bool) -> str:
    """Accept the old bool API while all new callers use an explicit edition."""
    if isinstance(edition, bool):
        return MEMBER_EDITION if edition else FULL_EDITION
    if edition not in {FULL_EDITION, MEMBER_EDITION, HOSTED_EDITION}:
        raise ValueError("unknown companion edition")
    return edition


def operator_console_available(
    member_edition: bool,
    *,
    state_root: Path | None = None,
) -> bool:
    """Expose the loopback administration shortcut only on a guild host."""
    if member_edition:
        return False
    if state_root is None:
        program_data = os.environ.get("PROGRAMDATA", "").strip()
        if not program_data:
            return False
        state_root = Path(program_data) / "VemalsEars"
    try:
        return (state_root / "relay.env").is_file()
    except OSError:
        return False


def launch_operator_console(
    *,
    member_edition: bool,
    state_root: Path | None = None,
    opener: Callable[[str], bool] | None = None,
) -> bool:
    """Open only the fixed loopback console, never a paired or remote URL."""
    if not operator_console_available(member_edition, state_root=state_root):
        return False
    open_url = opener or webbrowser.open_new_tab
    try:
        return bool(open_url(OPERATOR_CONSOLE_URL))
    except (OSError, webbrowser.Error):
        return False


def open_support_url(
    url: str,
    *,
    opener: Callable[[str], bool] | None = None,
) -> bool:
    """Open one owner-approved support destination in the default browser."""
    if url not in SUPPORT_URLS:
        return False
    open_url = opener or webbrowser.open_new_tab
    try:
        return bool(open_url(url))
    except (OSError, webbrowser.Error):
        return False


def service_specs_for_edition(edition: str | bool) -> tuple[ServiceSpec, ...]:
    selected = normalize_edition(edition)
    if selected == MEMBER_EDITION:
        return MEMBER_SERVICES
    if selected == HOSTED_EDITION:
        return tuple(spec for spec in HOSTED_SERVICES if spec.key in {"gear", "talents"})
    return tuple(spec for spec in SERVICES if spec.key in {"gear", "talents"})


def edition_product_name(edition: str | bool) -> str:
    selected = normalize_edition(edition)
    if selected == MEMBER_EDITION:
        return "Vemal's Ears"
    if selected == HOSTED_EDITION:
        return "Downtime Lite"
    return "Downtime"


def provision_downtime_results_carrier(
    edition: str | bool,
    *,
    wow_root: Path | None = None,
) -> bool:
    """Preinstall the static one-use carrier for Downtime editions only.

    The member Vemal's Ears bundle has no gear or talent result path and must
    never create a WoW addon folder. The return value is true only when the
    carrier was already installed before this process started, which is the
    safe condition for presenting Load Results in an already-running WoW
    session. A false result retains the reload fallback for this session.
    """

    if normalize_edition(edition) == MEMBER_EDITION:
        return False
    try:
        from GuildHub import job_codec
    except ImportError:
        return False
    try:
        retail_root = job_codec.locate_retail_root(wow_root)
        return bool(job_codec.provision_result_carrier(retail_root))
    except (job_codec.JobCodecError, OSError):
        return False


# --------------------------------------------------------------------------
# Job object
# --------------------------------------------------------------------------

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_EXTENDED_LIMIT_INFORMATION = 9


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def create_watcher_job() -> int | None:
    """A job the watchers are assigned to, killed when this process dies.

    Stopping cleanly already kills the children, but Task Manager's End
    task, an unhandled crash and Windows logoff never run that path, and a
    watcher orphaned by one of those is invisible: no console, no window,
    still holding the SavedVariables it polls. KILL_ON_JOB_CLOSE makes
    Windows tear them down whenever this process goes, however it goes.
    """
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
        ]
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return None
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            handle, JOB_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info), ctypes.sizeof(info),
        ):
            return None
        return int(handle)
    except (OSError, AttributeError, ValueError):
        return None


def assign_to_watcher_job(process: subprocess.Popen) -> bool:
    if WATCHER_JOB is None:
        return False
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        return bool(kernel32.AssignProcessToJobObject(
            wintypes.HANDLE(WATCHER_JOB), wintypes.HANDLE(int(process._handle)),
        ))
    except (OSError, AttributeError, ValueError):
        return False


# Held for the life of the process; closing the handle kills the watchers.
WATCHER_JOB = create_watcher_job()


def child_python() -> str:
    """A console-free interpreter for the children."""
    executable = Path(sys.executable)
    windowless = executable.with_name("pythonw.exe")
    return str(windowless if windowless.is_file() else executable)


def full_launcher(root: Path | None = None) -> Path | None:
    """Return the optional native launcher shipped with the full bundle."""
    app_root = ROOT if root is None else root
    candidate = app_root / ("DowntimeLite.exe" if
        (app_root / HOSTED_EDITION_MARKER.name).exists() else "Downtime.exe")
    script = app_root / "WoW-Companion.pyw"
    if candidate.is_file() and script.is_file():
        return candidate
    return None


def installed_launcher() -> Path | None:
    """Return the signed installer launcher only in the fixed installed layout."""
    if ROOT.name.casefold() != "app":
        return None
    candidate = ROOT.parent / "VemalsEars.exe"
    private_python = ROOT.parent / "runtime" / "pythonw.exe"
    if candidate.is_file() and private_python.is_file():
        return candidate
    return None


def resolve_edition(
    *,
    root: Path | None = None,
    installed: bool | None = None,
) -> str:
    """Select behavior from a distribution marker, never from pairing state.

    Pairing is deliberately not the mode bit: an operator or source checkout
    may pair with a guild too. A hosted marker that exists but is damaged still
    fails toward the hosted shell, and a damaged installed member layout still
    fails toward the member shell. Neither may launch heavyweight local helpers
    by accident.
    """
    app_root = ROOT if root is None else root
    if root is None and installed is None and ACTIVE_EDITION is not None:
        return ACTIVE_EDITION
    hosted_marker = app_root / HOSTED_EDITION_MARKER.name
    try:
        hosted_payload = json.loads(hosted_marker.read_text(encoding="ascii"))
    except (OSError, ValueError):
        hosted_payload = None
    if hosted_payload == HOSTED_EDITION_DOCUMENT:
        return HOSTED_EDITION
    if hosted_marker.exists():
        return HOSTED_EDITION

    member_marker = app_root / MEMBER_EDITION_MARKER.name
    try:
        member_payload = json.loads(member_marker.read_text(encoding="ascii"))
    except (OSError, ValueError):
        member_payload = None
    if member_payload == MEMBER_EDITION_DOCUMENT:
        return MEMBER_EDITION
    if installed is not None:
        return MEMBER_EDITION if installed else FULL_EDITION
    return MEMBER_EDITION if installed_launcher() is not None else FULL_EDITION


def product_name_for_edition(member_edition: bool) -> str:
    return MEMBER_PRODUCT_NAME if member_edition else FULL_PRODUCT_NAME


def configure_application(edition: str) -> None:
    """Select process-local identity before reading settings or IPC files."""
    global ACTIVE_EDITION, STATE_DIR, LOCK, SETTINGS_FILE, ICON_FILE
    global MEMBER_ICON_FILE, HOSTED_ICON_FILE, PAIR_SIGNAL, QUIT_SIGNAL
    global INSTANCE_MUTEX_NAME, TRAY_WINDOW_CLASS
    ACTIVE_EDITION = normalize_edition(edition)
    member = ACTIVE_EDITION == MEMBER_EDITION
    set_windows_app_identity(member)
    STATE_DIR = Path(os.environ.get("LOCALAPPDATA", ROOT)) / (
        "VemalsEars" if member else "GearHelper")
    LOCK = STATE_DIR / "companion-window.lock"
    SETTINGS_FILE = STATE_DIR / "companion-settings.json"
    ICON_FILE = STATE_DIR / "companion.ico"
    MEMBER_ICON_FILE = STATE_DIR / "vemals-ears.ico"
    HOSTED_ICON_FILE = STATE_DIR / "downtime-lite.ico"
    PAIR_SIGNAL = STATE_DIR / ("vemals-ears-paired.signal" if member else
                               "downtime-paired.signal")
    QUIT_SIGNAL = STATE_DIR / "companion-quit.request"
    INSTANCE_MUTEX_NAME = (r"Local\VemalsEars.Companion" if member else
                           r"Local\Downtime.Companion")
    TRAY_WINDOW_CLASS = "VemalsEarsCompanionTray" if member else "DowntimeCompanionTray"
    # Preserve existing preferences on the first separate VE launch. Never
    # move/delete the source: Downtime still owns its original settings.
    legacy = Path(os.environ.get("LOCALAPPDATA", ROOT)) / "GearHelper" / SETTINGS_FILE.name
    if member and not SETTINGS_FILE.exists() and legacy.is_file():
        try:
            preferences = json.loads(legacy.read_text(encoding="ascii"))
            if isinstance(preferences, dict):
                save_settings({key: preferences[key] for key in
                    ("minimize_to_taskbar", "speak_hotkey") if key in preferences})
        except (OSError, ValueError):
            pass


def application_script(member: bool | None = None) -> Path:
    selected = is_member_edition() if member is None else member
    return ROOT / ("Vemals-Ears.pyw" if selected else "WoW-Companion.pyw")


def guild_credential_store():
    """Use independent guild connections for voice and addon jobs."""
    from GuildHub.credentials import (
        WindowsDeviceCredentialStore, WindowsDowntimeCredentialStore)
    return (WindowsDeviceCredentialStore() if is_member_edition() else
            WindowsDowntimeCredentialStore())


def set_windows_app_identity(member_edition: bool) -> bool:
    """Give the taskbar a stable identity even though Tk runs under Python."""
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        setter = shell32.SetCurrentProcessExplicitAppUserModelID
        setter.argtypes = [wintypes.LPCWSTR]
        setter.restype = ctypes.c_long
        identity = "VemalsEars.Companion" if member_edition else "Downtime.Companion"
        return int(setter(identity)) >= 0
    except (OSError, AttributeError, ValueError):
        return False


def is_member_edition(
    *,
    root: Path | None = None,
    installed: bool | None = None,
) -> bool:
    """Compatibility wrapper for callers that only distinguish VE member mode."""
    return resolve_edition(root=root, installed=installed) == MEMBER_EDITION


def claim_instance_mutex() -> bool:
    """Hold the per-session app mutex and report whether another copy owns it."""
    global INSTANCE_MUTEX_HANDLE
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.CreateMutexW(None, False, INSTANCE_MUTEX_NAME)
        if not handle:
            return False
        already_exists = ctypes.get_last_error() == 183
        if already_exists:
            kernel32.CloseHandle(handle)
            return True
        INSTANCE_MUTEX_HANDLE = int(handle)
        return False
    except (OSError, AttributeError, ValueError):
        return False


def release_instance_mutex() -> bool:
    """Drop the app mutex so an installer stops counting this copy as running.

    The installer decides whether a companion is running by asking for
    this mutex, and Windows holds it until the owning process exits. That
    is far too late for an update, which has to launch the installer from
    inside the very process being replaced, so the handle is closed
    explicitly first. Reports whether a handle was actually released -
    the update path checks, because silently failing here is what a
    stalled installer looks like from the member's side.
    """
    global INSTANCE_MUTEX_HANDLE
    handle = INSTANCE_MUTEX_HANDLE
    if not handle:
        return False
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle(handle)
    except (OSError, AttributeError, ValueError):
        return False
    finally:
        INSTANCE_MUTEX_HANDLE = None
    return True


def instance_conflict_message(edition: str) -> str:
    selected = normalize_edition(edition)
    # Reached only after raise_running_companion observably failed, so
    # this reports what happened rather than giving navigation advice
    # for a window that did not come up ("look for its taskbar button"
    # was often a lie - a withdrawn window has no button).
    return (
        edition_product_name(selected) + " is already running, but it did not respond when "
        "this launch asked it to come forward.\n\n"
        "Look for its icon by the clock. If it stays unresponsive, end "
        "it from Task Manager and start it again."
    )


@dataclass(frozen=True)
class PairingNotice:
    success: bool
    message: str


@dataclass(frozen=True)
class DiscordWorkerSnapshot:
    state: str
    healthy: bool
    configured: bool
    detail: str = ""
    listening: bool | None = False


def discord_worker_request(action: str | None = None) -> DiscordWorkerSnapshot:
    """Read or change the hosted Discord worker through its local console."""
    if action not in {None, "start", "stop"}:
        raise ValueError("Discord worker action must be start or stop")

    body = None
    method = "GET"
    headers = {
        "Accept": "application/json",
        CONSOLE_CONTROL_HEADER: "1",
    }
    if action is not None:
        body = json.dumps({"action": action}, separators=(",", ":")).encode("ascii")
        method = "POST"
        headers["Content-Type"] = "application/json"
    request = urllib_request.Request(
        DISCORD_WORKER_CONSOLE_URL,
        data=body,
        headers=headers,
        method=method,
    )

    try:
        with urllib_request.urlopen(request, timeout=5.0) as response:
            raw = response.read(16_384)
    except urllib_error.HTTPError as error:
        raw_error = error.read(4_096)
        message = ""
        try:
            error_payload = json.loads(raw_error.decode("utf-8"))
            candidate = error_payload.get("error", "")
            if isinstance(candidate, str):
                message = candidate.strip()
        except (UnicodeDecodeError, ValueError, AttributeError):
            pass
        if error.code == 409 and message:
            raise RuntimeError(message) from None
        operation = "check" if action is None else action
        raise RuntimeError(
            "Vemal's Ears could not " + operation + " the Discord worker."
        ) from None
    except (urllib_error.URLError, TimeoutError, OSError):
        raise RuntimeError("Vemal's Ears console is unavailable.") from None

    try:
        payload = json.loads(raw.decode("utf-8"))
        state = payload["state"]
        healthy = payload["healthy"]
        configured = payload["configured"]
        listening = payload.get("listening")
        detail = payload.get("detail", "")
    except (UnicodeDecodeError, ValueError, KeyError, TypeError, AttributeError):
        raise RuntimeError(
            "Vemal's Ears console returned an unreadable Discord status."
        ) from None
    if (
        state not in DISCORD_WORKER_STATES
        or type(healthy) is not bool
        or type(configured) is not bool
        or (listening is not None and type(listening) is not bool)
        or not isinstance(detail, str)
        or (
            listening is True
            and state not in {
                "continue_pending",
                "pause_pending",
                "paused",
                "running",
                "start_pending",
                "stop_pending",
            }
        )
        or (listening is True and state == "running" and not healthy)
    ):
        raise RuntimeError(
            "Vemal's Ears console returned an unreadable Discord status."
        )
    return DiscordWorkerSnapshot(
        state=state,
        healthy=healthy,
        configured=configured,
        detail=" ".join(detail.split())[:160],
        listening=(
            False
            if listening is None and state in {"not installed", "stopped"}
            else listening
        ),
    )


@dataclass(frozen=True)
class ConsentLaunchResult:
    paired: bool
    guild_name: str
    discord_opened: bool
    browser_opened: bool = False


@dataclass(frozen=True)
class QuickChatSendResult:
    request_id: str
    success: bool
    message: str
    cursor: int | None = None


@dataclass(frozen=True)
class QueuedQuickChatResult:
    attempt_id: int
    result: QuickChatSendResult


@dataclass(frozen=True, repr=False)
class QuickChatProfileResult:
    sequence: int
    operation: str
    success: bool
    name: str | None = None
    message: str = ""

    def __repr__(self) -> str:
        return "QuickChatProfileResult(<redacted>)"


class _QuickChatCredentialSnapshot:
    """One request's pairing, fixed before a later re-pair can replace it."""

    def __init__(self, credentials: object) -> None:
        self._credentials = credentials

    def load(self):
        return self._credentials

    def __repr__(self) -> str:
        return "_QuickChatCredentialSnapshot(<redacted>)"


@dataclass
class HotkeyUpdateRequest:
    """One tray-thread registration request and its truthful result."""

    hotkey: tuple[int, int] | None
    done: threading.Event = field(default_factory=threading.Event)
    success: bool = False
    cancelled: bool = False
    lock: object = field(default_factory=threading.Lock, repr=False)


@dataclass
class HotkeySuppressionRequest:
    """A tray-thread barrier for pausing or resuming shortcut dispatch."""

    suppressed: bool
    done: threading.Event = field(default_factory=threading.Event)
    success: bool = False
    cancelled: bool = False
    lock: object = field(default_factory=threading.Lock, repr=False)


def new_quick_chat_request_id() -> str:
    """Return an opaque ASCII id that is safe for the relay protocol."""
    return "qch_" + secrets.token_urlsafe(18)


def canonicalize_quick_chat_name(value: object) -> str | None:
    """Return one safe message identity, or None when it cannot be used.

    Names are chosen by the paired member and displayed to the guild, so
    invisible controls must never be able to make one name read as another.
    Natural left-to-right and right-to-left names remain valid; only control
    characters and the explicit direction-changing controls are rejected.
    """
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFKC", value)
    if any(
        unicodedata.category(character).startswith("C")
        or unicodedata.bidirectional(character) in QUICK_CHAT_BIDI_CONTROLS
        for character in normalized
    ):
        return None
    clean = " ".join(normalized.split())
    if not clean or len(clean) > QUICK_CHAT_NAME_LIMIT:
        return None
    return clean


def safe_quick_chat_profile_error(error: Exception, operation: str) -> str:
    """Keep trusted client guidance, but never echo an arbitrary exception."""
    fallback = (
        "Your message name could not be loaded. Check your guild connection "
        "and try again."
        if operation == "load"
        else "Your message name could not be saved. Check your guild connection "
        "and try again."
    )
    try:
        from GuildHub.client import GuildHubClientError
    except ImportError:
        return fallback
    if not isinstance(error, GuildHubClientError):
        return fallback
    message = " ".join(str(error).split())
    if (
        not message
        or len(message) > 160
        or any(
            unicodedata.category(character).startswith("C")
            or unicodedata.bidirectional(character) in QUICK_CHAT_BIDI_CONTROLS
            for character in message
        )
    ):
        return fallback
    return message


def transmit_quick_chat(text: str, request_id: str) -> QuickChatSendResult:
    """Use the paired device credential without exposing message contents."""
    try:
        from GuildHub.client import GuildHubClientError, send_quick_chat
        from GuildHub.credentials import (
            DeviceCredentialStoreError,
            WindowsDeviceCredentialStore,
        )
    except ImportError:
        return QuickChatSendResult(
            request_id,
            False,
            "Quick chat needs the Vemal's Ears network component.",
        )
    try:
        acknowledgement = asyncio.run(
            send_quick_chat(
                WindowsDeviceCredentialStore(),
                text,
                request_id=request_id,
            )
        )
        acknowledgement_request_id = getattr(
            acknowledgement, "request_id", None
        )
        cursor = getattr(acknowledgement, "cursor", None)
        if (
            acknowledgement_request_id != request_id
            or not isinstance(cursor, int)
            or isinstance(cursor, bool)
            or cursor < 0
        ):
            raise ValueError("invalid quick-chat acknowledgement")
        return QuickChatSendResult(
            request_id,
            True,
            "Posted to Discord and in-game captions.",
            cursor,
        )
    except GuildHubClientError as error:
        # GuildHubClientError text is already bounded and protocol-validated;
        # preserving it tells the member whether the raid session is absent,
        # the host still needs to assign their name, or the device was revoked.
        safe = str(error).strip()
        # The echo check only applies to a line long enough that finding it
        # inside the relay's message cannot be coincidence. A plain
        # substring test threw the real reason away whenever a short line
        # happened to occur in it: typing "on" matches the "on" in a
        # fixed error, so a member who had not set a typed-chat name was
        # told to check their connection instead of opening Preferences.
        # "the", "art" and "me" collide the same way.
        # The relay's quick-chat messages are fixed literals that never
        # contain member text, so this guard could only ever misfire.
        echoed = len(text) >= QUICK_CHAT_ECHO_MIN_CHARS and text in safe
        if not safe or len(safe) > 160 or echoed:
            safe = "Quick chat could not post. Check the connection and try again."
        return QuickChatSendResult(request_id, False, safe)
    except DeviceCredentialStoreError:
        return QuickChatSendResult(
            request_id,
            False,
            (
                "Quick chat could not post. Check the raid session, guild "
                "setup, or connection, then try again."
            ),
        )
    except Exception:
        # Network libraries can include request bodies in diagnostics. Keep
        # the member's line out of both the UI error and every log path.
        return QuickChatSendResult(
            request_id,
            False,
            "Quick chat could not send. Try again.",
        )


def dev_coexist_requested() -> bool:
    """The coexistence switch, from the environment or its durable store.

    setx writes HKCU Environment; a process started before the setx keeps
    its stale environment and passes it to children, so the process
    environment alone said "no" on machines that had opted in. The
    registry read costs nothing and cannot be stale.
    """
    if os.environ.get("DOWNTIME_DEV_COEXIST") == "1":
        return True
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return winreg.QueryValueEx(key, "DOWNTIME_DEV_COEXIST")[0] == "1"
    except OSError:
        return False


def retire_standalone_vemals_ears() -> bool:
    """Compatibility no-op: the products are independent installations."""
    return False


def paired_privacy_url(*, credential_store=None) -> str:
    """The paired guild's own Guild & Privacy page, or "".

    Derived from the pairing at CALL time, never hardcoded: every guild
    runs its own relay, and a URL captured at build time would go stale
    the moment the player re-pairs elsewhere. Only https results are
    returned - a ws:// scheme is the transport's business, and a
    loopback pairing has no public page worth opening.
    """
    if credential_store is None:
        try:
            from GuildHub.credentials import WindowsDeviceCredentialStore
            credential_store = guild_credential_store()
        except (ImportError, OSError, RuntimeError):
            return ""
    try:
        credentials = credential_store.load()
    except (OSError, RuntimeError, ValueError):
        return ""
    if credentials is None:
        return ""
    parts = urlsplit(str(getattr(credentials, "server_url", "")))
    if parts.scheme != "wss" or not parts.netloc:
        return ""
    return "https://" + parts.netloc + "/privacy"


def paired_guild_name(*, credential_store=None) -> str:
    """Return only the safe display name for the currently paired guild."""
    if credential_store is None:
        try:
            from GuildHub.credentials import WindowsDeviceCredentialStore
            credential_store = guild_credential_store()
        except (ImportError, OSError, RuntimeError):
            return ""
    try:
        credentials = credential_store.load()
    except (OSError, RuntimeError, ValueError):
        return ""
    if credentials is None:
        return ""
    name = str(getattr(credentials, "guild_name", "")).strip()
    return name[:80]


def prepare_paired_discord_guild(
    *,
    credential_store=None,
    opener: Callable[[str], bool] | None = None,
    browser_opener: Callable[[str], bool] | None = None,
) -> ConsentLaunchResult:
    """Open the paired guild in Discord Desktop, then fall back to the web.

    The companion owns only a validated guild ID, not the changing raid
    control channel or a Discord interaction. The desktop link gets the
    member into the right guild without claiming it can operate consent or
    class controls for them.
    """
    if credential_store is None:
        try:
            from GuildHub.credentials import WindowsDeviceCredentialStore
            credential_store = WindowsDeviceCredentialStore()
        except (ImportError, OSError, RuntimeError):
            credential_store = None

    credentials = None
    if credential_store is not None:
        try:
            credentials = credential_store.load()
        except (OSError, RuntimeError, ValueError):
            credentials = None
    if credentials is None:
        return ConsentLaunchResult(False, "", False)

    guild_id = getattr(credentials, "guild_id", "")
    guild_name = str(getattr(credentials, "guild_name", ""))
    if not isinstance(guild_id, str) or not DISCORD_GUILD_ID.fullmatch(guild_id):
        return ConsentLaunchResult(True, guild_name, False)

    def open_discord_desktop(url: str) -> bool:
        """Ask Windows to invoke the registered Discord protocol handler."""
        try:
            os.startfile(url)  # noqa: S606 - fixed Discord URI plus validated guild ID
        except (AttributeError, OSError):
            return False
        return True

    open_desktop = opener or open_discord_desktop
    discord_opened = False
    try:
        discord_opened = bool(open_desktop(DISCORD_DESKTOP_GUILD_PREFIX + guild_id))
    except (OSError, webbrowser.Error):
        pass
    browser_opened = False
    if not discord_opened:
        open_browser = browser_opener or webbrowser.open_new_tab
        try:
            browser_opened = bool(open_browser(DISCORD_WEB_GUILD_PREFIX + guild_id))
        except (OSError, webbrowser.Error):
            pass
    return ConsentLaunchResult(
        True,
        guild_name,
        discord_opened,
        browser_opened,
    )


def prepare_discord_consent(
    *,
    credential_store=None,
    opener: Callable[[str], bool] | None = None,
    browser_opener: Callable[[str], bool] | None = None,
) -> ConsentLaunchResult:
    """Navigate to consent without pretending that navigation grants it.

    Only a Discord interaction can prove which speaker changed their toggle.
    """
    return prepare_paired_discord_guild(
        credential_store=credential_store,
        opener=opener,
        browser_opener=browser_opener,
    )


def prepare_discord_class_picker(
    *,
    credential_store=None,
    opener: Callable[[str], bool] | None = None,
    browser_opener: Callable[[str], bool] | None = None,
) -> ConsentLaunchResult:
    """Navigate to the paired guild where Discord can identify the player."""
    return prepare_paired_discord_guild(
        credential_store=credential_store,
        opener=opener,
        browser_opener=browser_opener,
    )


def consume_pairing_argument(
    arguments: list[str],
    *,
    confirm_pairing: Callable[[str, str, bool], bool] | None = None,
) -> PairingNotice | None:
    """Exchange one deep link without ever rendering or logging the link."""
    flags = {"--vemals-ears-pair", "--downtime-pair"}
    if not any(flag in arguments for flag in flags):
        return None
    if len(arguments) != 2 or arguments[0] not in flags:
        return PairingNotice(False, "The guild connection request is invalid.")
    try:
        from GuildHub.pairing import GuildHubClientError, pair_device
        from GuildHub.credentials import (
            DeviceCredentialStoreError,
            DeviceCredentialValidationError,
            WindowsDeviceCredentialStore,
        )
    except ImportError:
        return PairingNotice(
            False,
            "The app needs its network component installed before it can connect.",
        )
    try:
        def confirm(credentials, existing_credentials) -> bool:
            if confirm_pairing is None:
                return False
            server_host = urlsplit(credentials.server_url).netloc
            return bool(
                confirm_pairing(
                    credentials.guild_name,
                    server_host,
                    existing_credentials is not None,
                )
            )

        credentials = asyncio.run(
            pair_device(
                arguments[1],
                guild_credential_store(),
                confirm=confirm,
            )
        )
        return PairingNotice(
            True,
            "This computer is now paired with " + credentials.guild_name + ".",
        )
    except (
        GuildHubClientError,
        DeviceCredentialStoreError,
        DeviceCredentialValidationError,
    ) as error:
        # These boundaries guarantee that pairing codes and device tokens are
        # never echoed in their messages.
        return PairingNotice(False, str(error))
    except Exception:
        # Unknown dependencies may include their request URL in diagnostics.
        return PairingNotice(
            False,
            "Could not connect to your guild. Ask your guild admin for a new link.",
        )


def notify_running_companion() -> None:
    """Wake the existing companion after another process completes pairing."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        PAIR_SIGNAL.write_text(str(time.time()), encoding="ascii")
    except OSError:
        pass


class PairingFailed(RuntimeError):
    """A connection attempt that did not end in a paired guild.

    The message is written for the member and never contains the
    invitation, a code or a token - the pairing code's own errors keep
    that promise, and the catch-all below says nothing specific at all.
    """


def looks_like_invitation(text: object) -> bool:
    """Would the pairing code accept this text as an invitation?

    A local check only - no request is made - used to prefill from the
    clipboard and to refuse a stray paste before it costs a network
    round-trip. The real parsers decide, so this can never accept a
    shape they would reject; without the network component the shape
    is judged by eye.
    """
    if not isinstance(text, str):
        return False
    raw = text.strip()
    if not raw or len(raw) > 2048 or "\n" in raw or "\r" in raw:
        return False
    try:
        from GuildHub.pairing import parse_join_link, parse_pair_link
    except ImportError:
        lowered = raw.lower()
        return lowered.startswith(("vemalsears:", "downtime:")) or (
            lowered.startswith("https://") and "/join#" in lowered
        )
    try:
        if raw.lower().startswith(("vemalsears:", "downtime:")):
            parse_pair_link(raw)
        else:
            parse_join_link(raw)
    except Exception:  # noqa: BLE001 - any refusal means "not an invitation"
        return False
    return True


CONNECTION_FAILED_GENERIC = (
    "Could not complete the connection. Ask your guild for a "
    "new invitation."
)
INVITATION_DECLINED = (
    "Not connected to that guild. The invitation was used, so ask for a new "
    "one if you change your mind."
)
NOT_AN_INVITATION = (
    "That does not look like a guild invitation. It starts with "
    "downtime:// or vemalsears://, or is your guild's https://.../join link."
)
# How a member actually gets an invitation: the bot answers /vem setup
# privately with one; /vem invite is the officer's way to hand one out
# (VoiceCaptions/coordinator.py). Nothing is posted publicly for them to
# find, so the panel says the command, not "look for a link".
HOW_TO_GET_AN_INVITATION = (
    "In your guild's Discord, type /vem setup - the Vemal's Ears bot "
    "replies to you privately with your invitation. A guild officer can "
    "also send you one. Each invitation works once."
)
HOW_TO_SWITCH_GUILDS = (
    "To switch to another guild, paste its invitation below. Type "
    "/vem setup in that guild's Discord to get one, or ask an officer "
    "there. Each invitation works once."
)

# The pairing code's own sentences, in the member's words. Matched by
# substring on the library's message rather than by exception class,
# because one class carries a dozen different situations and the member
# needs to know which one they are in - "already used" is a new
# invitation, "could not reach" is a network check, "not a Vemal's Ears
# server" is a wrong link. Anything unrecognised gets the generic line.
CONNECTION_ERROR_WORDS: tuple[tuple[str, str], ...] = (
    ("was cancelled", INVITATION_DECLINED),
    (
        "already been used",
        "That invitation has already been used, or your guild's server "
        "refused it. Ask your guild for a new one.",
    ),
    (
        "could not securely reach",
        "Could not reach your guild's server. Check your "
        "internet connection and try again in a moment.",
    ),
    (
        "Could not reach the guild server",
        "Could not reach your guild's server. Check your "
        "internet connection and try again in a moment.",
    ),
    (
        "does not point at a Vemal's Ears server",
        "That link does not lead to a supported guild server. Check it "
        "with your guild.",
    ),
    (
        "invalid pairing response",
        "Your guild's server answered in a way the app could not use. "
        "Ask your guild to check its server setup.",
    ),
    (
        "did not select the Vemal's Ears protocol",
        "That link does not lead to a supported guild server. Check it "
        "with your guild.",
    ),
    (
        "websockets package",
        "The app is missing a part it needs to connect. Reinstall it, "
        "then try again.",
    ),
    ("pairing link", NOT_AN_INVITATION),
    ("invitation link", NOT_AN_INVITATION),
    ("pairing link is required", NOT_AN_INVITATION),
    ("must contain one server", NOT_AN_INVITATION),
)


def member_words_for(error: BaseException, *, kind: str = "client") -> str:
    """The sentence a member sees for a pairing failure.

    kind is "client" for the protocol's own errors, "store" for the
    Windows credential vault refusing to save, "validation" for a server
    answer that did not validate. The library text never reaches the
    member verbatim: it says pairing, device, credential and token, the
    words this panel exists to keep off the screen.
    """
    if kind == "store":
        return (
            "Windows could not save your guild connection on this computer. "
            "Try again in a moment."
        )
    if kind == "validation":
        return (
            "Your guild's server sent details the app could not use. "
            "Ask your guild to check its server setup."
        )
    text = str(error).casefold()
    for needle, words in CONNECTION_ERROR_WORDS:
        if needle.casefold() in text:
            return words
    return CONNECTION_FAILED_GENERIC


def pair_invitation(invitation: str, confirm: Callable[..., bool]) -> tuple[str, str]:
    """Exchange one invitation for a guild connection.

    Returns the guild's display name and its server host. confirm is
    called with (credentials, existing_credentials) before anything is
    saved, exactly as the deep-link path calls it. Raises PairingFailed
    with a sentence written for the member - never the library's.
    """
    try:
        from GuildHub.pairing import GuildHubClientError, pair_device
        from GuildHub.credentials import (
            DeviceCredentialStoreError,
            DeviceCredentialValidationError,
            WindowsDeviceCredentialStore,
        )
    except ImportError:
        raise PairingFailed(
            "The app is missing a part it needs to connect. Reinstall "
            "it, then try again."
        ) from None
    try:
        credentials = asyncio.run(
            pair_device(invitation, guild_credential_store(), confirm=confirm)
        )
    except GuildHubClientError as error:
        raise PairingFailed(member_words_for(error)) from None
    except DeviceCredentialStoreError as error:
        raise PairingFailed(member_words_for(error, kind="store")) from None
    except DeviceCredentialValidationError as error:
        raise PairingFailed(member_words_for(error, kind="validation")) from None
    except Exception:
        raise PairingFailed(CONNECTION_FAILED_GENERIC) from None
    return credentials.guild_name, urlsplit(credentials.server_url).netloc


def register_pairing_links(icon: Path | None) -> bool:
    """Refresh the URL handler; pairing can still be pasted if this fails."""
    try:
        from GuildHub.windows_integration import register_current_user_protocol

        member = is_member_edition()
        launcher = installed_launcher() if member else full_launcher()
        frozen = bool(getattr(sys, "frozen", False))
        application = launcher or (Path(sys.executable) if frozen else application_script(member))
        register_current_user_protocol(
            application,
            pythonw_path=None if launcher or frozen else child_python(),
            icon_path=None if launcher else icon,
            app="vemalsears" if member else "downtime",
        )
        return True
    except Exception:
        return False


def running_instance_pid() -> int | None:
    """The other companion's pid from the lock, verified still alive."""
    try:
        pid = int(LOCK.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None
    if pid == os.getpid():
        return None
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # Windows recycles process ids. A lock left behind by a companion that
    # was killed rather than closed will eventually name a live but entirely
    # unrelated process, and matching on the number alone would then refuse
    # every future launch for good. Match the exact image too; in a packaged
    # release that image is VemalsEars.exe rather than Python.
    # Both interpreter faces count as "the companion". The protocol
    # handler is pinned to pythonw.exe while a terminal launch runs under
    # python.exe, so an exact match on THIS process's basename declared
    # "no instance" across that split, clobbered the lock, and started a
    # second full companion - two watchers writing one Generated.lua and
    # two caption clients evicting each other. A frozen VemalsEars.exe
    # still matches itself through expected_image.
    expected_images = {
        Path(sys.executable).name.casefold(),
        "python.exe",
        "pythonw.exe",
    }
    expected_images.update(
        {"vemalsears.exe"} if is_member_edition()
        else {"downtime.exe", "downtimelite.exe"}
    )
    try:
        rows = csv.reader((result.stdout or "").splitlines())
        alive = any(
            len(row) >= 2
            and row[0].strip().casefold() in expected_images
            and row[1].strip() == str(pid)
            for row in rows
        )
    except csv.Error:
        return None
    return pid if alive else None


def another_instance_running() -> bool:
    """Kept as a wrapper: the recycled-pid tests pin this name, and that
    guard is the most defect-prone code in the area."""
    return running_instance_pid() is not None


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

def hotkey_support():
    """The shared shortcut helpers, or no-ops if they are not shipped.

    Imported lazily like every other VoiceCaptions use in this file: the
    window must still open on a copy that is missing them, and losing the
    hotkey is not a reason to lose the companion.
    """
    try:
        from VoiceCaptions.settings import (
            DEFAULT_SPEAK_HOTKEY,
            canonicalize_hotkey,
            parse_hotkey,
        )

        return DEFAULT_SPEAK_HOTKEY, parse_hotkey, canonicalize_hotkey
    except ImportError:
        return "", lambda _spec: None, lambda _spec: None


HOTKEY_MODIFIER_KEYSYMS = {
    "Control_L": "Ctrl",
    "Control_R": "Ctrl",
    "Alt_L": "Alt",
    "Alt_R": "Alt",
    "Meta_L": "Alt",
    "Meta_R": "Alt",
    "Shift_L": "Shift",
    "Shift_R": "Shift",
    "Win_L": "Win",
    "Win_R": "Win",
    "Super_L": "Win",
    "Super_R": "Win",
}
HOTKEY_MODIFIER_ORDER = ("Ctrl", "Alt", "Shift", "Win")


def hotkey_key_name(keysym: str, keycode: int | None = None) -> str | None:
    """Return the capture name for a supported non-modifier key."""
    # Tk reports shifted number keys as punctuation names (for example,
    # Shift+9 becomes "parenleft"). On Windows the keycode remains the
    # physical virtual-key value, which preserves the shortcut the player
    # actually pressed.
    if isinstance(keycode, int):
        if 0x30 <= keycode <= 0x39:
            return chr(keycode)
        if 0x41 <= keycode <= 0x5A:
            return chr(keycode)
        if 0x70 <= keycode <= 0x87:
            return "F" + str(keycode - 0x6F)
    text = str(keysym or "")
    if len(text) == 1 and (text.isascii() and text.isalnum()):
        return text.upper()
    match = re.fullmatch(r"[fF](\d{1,2})", text)
    if match and 1 <= int(match.group(1)) <= 24:
        return "F" + match.group(1)
    return None


def captured_hotkey_spec(
    modifiers,
    keysym: str,
    keycode: int | None = None,
) -> str | None:
    """Canonicalize one keyboard capture through the shared policy."""
    key = hotkey_key_name(keysym, keycode)
    if key is None:
        return None
    ordered = [name for name in HOTKEY_MODIFIER_ORDER if name in modifiers]
    candidate = "+".join((*ordered, key))
    return hotkey_support()[2](candidate)


def default_minimize_to_taskbar(member_edition: bool | None = None) -> bool:
    """Whether closing the window should leave the app running.

    Off for Downtime and Downtime Lite: closing a window is a request to
    close it, and an app that only pretends to close is a small daily
    annoyance for the person who has it open beside a game.

    ON for Vemal's Ears, and that is not an inconsistency. The member
    edition runs the caption connection as a CHILD of this process - see
    MEMBER_SERVICES - so quitting stops captions. A member who closes the
    window mid-pull would lose the thing they installed the app for, with
    nothing on screen saying why. The tray note already promises the
    opposite ("Closing hides Vemal's Ears by the clock and keeps captions
    connected"), and that promise is worth keeping.

    Either way this is only the DEFAULT. The switch in Preferences still
    decides, and an existing choice on disk still wins.
    """
    member = is_member_edition() if member_edition is None else bool(member_edition)
    return member


def load_settings(member_edition: bool | None = None) -> dict:
    default_hotkey, parse_hotkey, canonicalize_hotkey = hotkey_support()
    defaults = {
        "minimize_to_taskbar": default_minimize_to_taskbar(member_edition),
        # The key that opens the box for speaking back. Lives here rather
        # than in the overlay's settings because the companion is what
        # registers it; the overlay has no use for it.
        "speak_hotkey": default_hotkey,
    }
    try:
        stored = json.loads(SETTINGS_FILE.read_text(encoding="ascii"))
    except (OSError, ValueError):
        return defaults
    if isinstance(stored, dict):
        for key, fallback in defaults.items():
            value = stored.get(key)
            if isinstance(fallback, bool):
                if isinstance(value, bool):
                    defaults[key] = value
            elif key == "speak_hotkey" and (
                isinstance(value, str) and parse_hotkey(value) is not None
            ):
                # An unusable hotkey falls back to the default rather than
                # leaving the member with no way to speak and nothing
                # saying why.
                defaults[key] = canonicalize_hotkey(value) or default_hotkey
    return defaults


def save_settings(settings: dict) -> bool:
    """Reports whether the settings actually reached the disk."""
    temporary = SETTINGS_FILE.with_suffix(SETTINGS_FILE.suffix + ".tmp")
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(settings, indent=2) + "\n",
            encoding="ascii",
        )
        os.replace(temporary, SETTINGS_FILE)
        return True
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def autostart_value_name(member_edition: bool | None = None) -> str:
    member = is_member_edition() if member_edition is None else bool(member_edition)
    return product_name_for_edition(member)


def sibling_autostart_value_name(member_edition: bool) -> str:
    return product_name_for_edition(not member_edition)


def autostart_command(member_edition: bool | None = None) -> str:
    member = is_member_edition() if member_edition is None else bool(member_edition)
    launcher = installed_launcher() if member else full_launcher()
    if launcher is not None:
        return f'"{launcher}"'
    return f'"{child_python()}" -E -s -B "{application_script(member)}"'


def legacy_source_autostart_commands() -> tuple[str, ...]:
    pythonw = child_python()
    script = Path(__file__).resolve()
    return (
        f'"{pythonw}" "{script}"',
        f'"{pythonw}" -E -s "{script}"',
    )


_SOURCE_AUTOSTART = re.compile(
    r'^"([^"\r\n]+[\\/]pythonw\.exe)"(?: -E -s(?: -B)?)? '
    r'"([^"\r\n]+[\\/](?:WoW-Companion|Vemals-Ears)\.pyw)"$',
    re.IGNORECASE,
)


def source_autostart_paths(value: object) -> tuple[str, str] | None:
    match = _SOURCE_AUTOSTART.fullmatch(str(value).strip())
    if match is None:
        return None
    pythonw, script = match.groups()
    if not (
        ntpath.isabs(pythonw)
        and ntpath.isabs(script)
        and ntpath.basename(pythonw).casefold() == "pythonw.exe"
        and ntpath.basename(script).casefold() in {"wow-companion.pyw", "vemals-ears.pyw"}
    ):
        return None
    return pythonw, script


def source_autostart_command(value: object) -> bool:
    """Recognize the old source shape for the legacy Run value only."""
    return source_autostart_paths(value) is not None


def same_windows_path(left: object, right: object) -> bool:
    return ntpath.normcase(ntpath.normpath(str(left))) == ntpath.normcase(
        ntpath.normpath(str(right))
    )


def current_source_autostart_command(value: object) -> bool:
    paths = source_autostart_paths(value)
    if paths is None:
        return False
    pythonw, script = paths
    return same_windows_path(pythonw, child_python()) and same_windows_path(
        script, Path(__file__).resolve()
    )


def source_autostart_edition(value: object) -> bool | None:
    """Read an edition only from the referenced bundle itself.

    Missing markers identify full source bundles. A damaged marker is unknown,
    so it is never safe to remove that Run value as ours.
    """
    paths = source_autostart_paths(value)
    if paths is None:
        return None
    script = Path(paths[1])
    if not script.is_file():
        return None
    if script.name.casefold() == "vemals-ears.pyw":
        return True
    marker = script.parent / MEMBER_EDITION_MARKER.name
    if not marker.exists():
        return False
    try:
        payload = json.loads(marker.read_text(encoding="ascii"))
    except (OSError, ValueError):
        return None
    return True if payload == MEMBER_EDITION_DOCUMENT else None


def installed_member_autostart_command() -> str | None:
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        return None
    launcher = ntpath.join(
        local_app_data,
        "Programs",
        "VemalsEars",
        "VemalsEars.exe",
    )
    return f'"{launcher}"'


def sibling_autostart_value_is_owned(value: object, member_edition: bool) -> bool:
    """No product owns the other product's Windows startup entry."""
    return False


def read_autostart_value(value_name: str) -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, value_name)
    except OSError:
        return None
    return str(value).strip()


def write_autostart_value(value_name: str, value: str) -> bool:
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            RUN_KEY,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, value)
        return True
    except OSError:
        return False


def autostart_value_is_owned(
    value: object,
    member_edition: bool | None = None,
    *,
    allow_legacy_source: bool = False,
) -> bool:
    normalized = str(value).strip().casefold()
    if normalized == autostart_command(member_edition).strip().casefold():
        return True
    member = (
        is_member_edition() if member_edition is None else bool(member_edition)
    )
    source_member = source_autostart_edition(value)
    if source_member is not None and source_member != member:
        return False
    if current_source_autostart_command(value) and source_member == member:
        return True
    return allow_legacy_source and source_member == member


def delete_owned_autostart_value(
    value_name: str,
    member_edition: bool | None = None,
    *,
    sibling: bool = False,
) -> bool:
    """Delete one Run value only when its command belongs to this app."""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            RUN_KEY,
            0,
            winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE,
        ) as key:
            try:
                current, _kind = winreg.QueryValueEx(key, value_name)
            except FileNotFoundError:
                return True
            member = is_member_edition() if member_edition is None else bool(member_edition)
            owned = (
                sibling_autostart_value_is_owned(current, member)
                if sibling
                else autostart_value_is_owned(
                    current,
                    member,
                    allow_legacy_source=value_name == LEGACY_RUN_VALUE,
                )
            )
            if not owned:
                return False
            winreg.DeleteValue(key, value_name)
            return True
    except OSError:
        return False


def migrate_autostart(member_edition: bool | None = None) -> bool:
    """Move an owned Run entry to the edition's current Windows name.

    The new value is written and read back before the legacy value is removed.
    A foreign value under the new name is never overwritten.
    """
    member = is_member_edition() if member_edition is None else bool(member_edition)
    target_name = autostart_value_name(member)
    expected = autostart_command(member)
    current = read_autostart_value(target_name)
    legacy = read_autostart_value(LEGACY_RUN_VALUE)
    target_was_missing = current is None
    current_owned = current is not None and autostart_value_is_owned(current, member)
    legacy_owned = legacy is not None and autostart_value_is_owned(
        legacy, member, allow_legacy_source=True
    )

    if current is not None and not current_owned:
        return False
    if not (current_owned or legacy_owned):
        return current is not None and current.casefold() == expected.casefold()

    if current is None or current.casefold() != expected.casefold():
        if not write_autostart_value(target_name, expected):
            return False
        written = read_autostart_value(target_name)
        if written is None or written.casefold() != expected.casefold():
            return False

    if legacy_owned:
        if not delete_owned_autostart_value(LEGACY_RUN_VALUE, member):
            if target_was_missing:
                delete_owned_autostart_value(target_name, member)
            return False
    return True


def autostart_enabled(member_edition: bool | None = None) -> bool:
    """The registry is the state - never a cached copy of it.

    An entry that names some other copy of the companion is not this one
    starting with Windows, so the switch must not claim it is.
    """
    member = is_member_edition() if member_edition is None else bool(member_edition)
    value = read_autostart_value(autostart_value_name(member))
    if value is not None and value.casefold() == autostart_command(member).strip().casefold():
        return True
    legacy = read_autostart_value(LEGACY_RUN_VALUE)
    return legacy is not None and autostart_value_is_owned(
        legacy, member, allow_legacy_source=True
    )


def set_autostart(enabled: bool, member_edition: bool | None = None) -> bool:
    """Returns the state the registry actually holds afterwards."""
    member = is_member_edition() if member_edition is None else bool(member_edition)
    target_name = autostart_value_name(member)
    if enabled:
        migrate_autostart(member)
        current = read_autostart_value(target_name)
        legacy = read_autostart_value(LEGACY_RUN_VALUE)
        if (
            current is None
            and legacy is not None
            and autostart_value_is_owned(legacy, member, allow_legacy_source=True)
        ):
            return autostart_enabled(member)
        if current is None:
            # The products have separate values. Never migrate, replace,
            # or disable the other app's Start with Windows preference.
            write_autostart_value(target_name, autostart_command(member))
            current = read_autostart_value(target_name)
        if current is not None and current.casefold() == autostart_command(member).casefold():
            delete_owned_autostart_value(LEGACY_RUN_VALUE, member)
    else:
        delete_owned_autostart_value(target_name, member)
        delete_owned_autostart_value(LEGACY_RUN_VALUE, member)
    return autostart_enabled(member)



# --------------------------------------------------------------------------
# Icon
# --------------------------------------------------------------------------

# The suite artwork, shipped inside every addon's Media folder. The companion
# is not an addon and has no Media folder of its own, so it looks in the
# places the artwork actually is: beside this file in a source checkout, and
# in the same relative spot inside the released companion bundle.
ARTWORK_NAME = "Media/DowntimeIcon.png"
ARTWORK_ADDONS = ("GearHelper", "BossTalents", "ReadyRoom", "TokenWatch")


def shipped_version() -> str:
    """The version this copy of the app was built from.

    Read lazily and defensively: it is a label, and a label must never be
    the reason a preferences panel refuses to open. GuildHub/version.py
    has no imports of its own, so this costs nothing.
    """
    try:
        from GuildHub.version import CLIENT_VERSION
    except Exception:
        return "unknown"
    return CLIENT_VERSION


def find_artwork(edition: str = FULL_EDITION) -> Path | None:
    """Each product keeps its own mark, including in a standalone bundle."""
    if normalize_edition(edition) == MEMBER_EDITION:
        candidate = ROOT / "Windows" / "VemalsEars.png"
        return candidate if candidate.is_file() else None
    for addon in ARTWORK_ADDONS:
        candidate = ROOT / addon / ARTWORK_NAME
        if candidate.is_file():
            return candidate
    # Bundled on its own, without the addon folders around it.
    loose = ROOT / ARTWORK_NAME
    return loose if loose.is_file() else None


def draw_fallback_icon(image_module, draw_module):
    """Only reached when the artwork is missing - a plain mark beats no icon,
    and beats a window that refuses to start over a missing png."""
    image = image_module.new("RGBA", (256, 256), (0, 0, 0, 0))
    draw = draw_module.Draw(image)
    draw.rounded_rectangle([6, 6, 250, 250], radius=54, fill=PANEL, outline=BORDER, width=8)
    draw.line([(70, 172), (128, 110), (186, 172)], fill=ACCENT, width=34, joint="curve")
    draw.line([(70, 208), (128, 146), (186, 208)], fill=BORDER, width=22, joint="curve")
    return image


# The taskbar and the tray want the clock, not the dark plate it is painted
# on. The square artwork is opaque right out to its corners, which Windows
# draws as a black tile behind the icon in both places - so it gets the same
# treatment the minimap art gets: crop in to the blue ring, then mask to a
# circle. The numbers match packaging/make_round_icon.py, which explains where
# they came from; they are repeated rather than imported because the companion
# bundle ships this file and the png and nothing else.
ICON_CROP = (0.086, 0.914)
ICON_SUPERSAMPLE = 8
ICON_SIZE = 256


def round_off(image_module, image):
    """Crop the dark plate away and round the art off to a circle."""
    from PIL import ImageChops, ImageDraw

    width, height = image.size
    low, high = ICON_CROP
    image = image.crop((int(width * low), int(height * low),
                        int(width * high), int(height * high)))
    image = image.resize((ICON_SIZE, ICON_SIZE), image_module.LANCZOS)

    # Supersampled so the edge is smooth rather than stair-stepped at the 16
    # and 24 pixel sizes the taskbar and tray actually draw.
    big = ICON_SIZE * ICON_SUPERSAMPLE
    mask = image_module.new("L", (big, big), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, big - 1, big - 1], fill=255)
    mask = mask.resize((ICON_SIZE, ICON_SIZE), image_module.LANCZOS)

    # Multiplied into whatever alpha the art already carries, not replacing it.
    image.putalpha(ImageChops.multiply(image.getchannel("A"), mask))
    return image


def photo_scale_factors(source_width: int, size: int) -> tuple[int, int]:
    """Whole-number zoom and subsample that take source_width to size.

    Tk only scales a PhotoImage by integers, so the ratio is reduced to a
    fraction and applied as both: 256 to 20 is 5/64, which zoom(5) then
    subsample(64) hits exactly. 256 to 64 needs no zoom at all.
    """
    if source_width <= 0 or size <= 0:
        return (1, 1)
    divisor = math.gcd(source_width, size)
    return (size // divisor, source_width // divisor)


def fit_photo_image(image, size: int):
    """Scale a Tk PhotoImage to size, the only way Tk can without Pillow.

    Nearest-neighbour, so the result is harder-edged than Pillow's LANCZOS.
    That is the whole cost of not having Pillow, and it is a far smaller
    cost than the alternative this replaced, which was to draw the image at
    its full size inside a small canvas and show only the middle of it.
    """
    try:
        width = image.width()
    except tk.TclError:
        return image
    if width <= 0 or size <= 0 or width == size:
        return image
    try:
        # Large or coprime artwork can need an enormous rational zoom:
        # 1254 -> 64 would first become 40128px on both axes. Reduce near
        # the target before computing that ratio. Tk rounds subsampling
        # up, so use the resulting width rather than integer division.
        reduce_by = width // size
        if reduce_by > 1:
            image = image.subsample(reduce_by)
            width = image.width()
        zoom, shrink = photo_scale_factors(width, size)
        # Scale one axis at a time so both dimensions are never enlarged
        # together. For square artwork, each intermediate stays within
        # 4 * size**3 pixels, independent of the source dimensions.
        if zoom > 1:
            image = image.zoom(zoom, 1)
        if shrink > 1:
            image = image.subsample(shrink, 1)
        if zoom > 1:
            image = image.zoom(1, zoom)
        if shrink > 1:
            image = image.subsample(1, shrink)
    except tk.TclError:
        return image
    return image


def ensure_icon(edition: str = FULL_EDITION) -> Path | None:
    """Convert the suite artwork into the .ico Windows wants.

    Windows takes an .ico for both the taskbar button and the tray, so the
    png is converted once and cached. The cache is rebuilt whenever the
    artwork is newer than it, otherwise updated art would never appear - and
    whenever THIS FILE is newer, because the recipe above lives here, so a
    change to how the icon is built has to invalidate the cache too. Without
    that second check a new shape would never reach anyone who had already
    run the companion once.
    """
    selected = normalize_edition(edition)
    if selected == MEMBER_EDITION:
        # Ship the same multi-resolution icon used by the native EXE and
        # installer. Do not reuse an older cached Downtime clock, or depend
        # on Pillow being available before Windows can identify this app.
        native_icon = ROOT / "Windows" / "VemalsEars.ico"
        if native_icon.is_file():
            return native_icon
    icon_file = (
        MEMBER_ICON_FILE
        if selected == MEMBER_EDITION
        else HOSTED_ICON_FILE
        if selected == HOSTED_EDITION
        else ICON_FILE
    )
    source = find_artwork(selected)
    if selected == MEMBER_EDITION and source is None:
        return None
    if icon_file.is_file():
        try:
            cached = icon_file.stat().st_mtime
            newest = Path(__file__).stat().st_mtime
            if source is not None:
                newest = max(newest, source.stat().st_mtime)
            if cached >= newest:
                return icon_file
        except OSError:
            return icon_file

    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return icon_file if icon_file.is_file() else None

    try:
        if source is not None:
            image = Image.open(source).convert("RGBA")
            if selected != MEMBER_EDITION:
                image = round_off(Image, image)
        else:
            image = draw_fallback_icon(Image, ImageDraw)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        image.save(icon_file, format="ICO",
                   sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])
        return icon_file
    except (OSError, ValueError):
        return icon_file if icon_file.is_file() else None


# --------------------------------------------------------------------------
# Notification area icon
# --------------------------------------------------------------------------

# The tray message window's class, registered in TrayIcon._run. It is the
# rendezvous point for the second-launch handoff, so the literal lives in
# one constant both sides use - and it must be defined BEFORE TrayIcon,
# whose class body reads TRAY_COMMAND_SHOW at import time.
TRAY_WINDOW_CLASS = "DowntimeCompanionTray"
TRAY_COMMAND_SHOW = 1023


class TrayIcon:
    """A Shell_NotifyIcon icon driven from its own thread.

    Nothing here touches Tk. Clicks are posted to a queue that the window
    drains on its normal tick, because Tk is not thread safe.
    """

    SHOW = TRAY_COMMAND_SHOW
    EXIT = 1024
    # Any small positive value; it only has to be unique within this
    # thread. Two ids make a live shortcut change transactional: Windows
    # must accept the candidate before the working shortcut is released.
    SPEAK_HOTKEY_ID = 1
    SPEAK_HOTKEY_ALT_ID = 2

    def __init__(self, tooltip: str, icon: Path | None, hotkey=None) -> None:
        self.tooltip = tooltip
        self.icon = icon
        # (modifiers, virtual key) or None. Registered on the tray thread
        # because RegisterHotKey delivers WM_HOTKEY to the window's own
        # message queue, and this window belongs to that thread.
        self.hotkey = hotkey
        self.hotkey_registered = False
        self.hotkey_suppressed = False
        self.hotkey_id = self.SPEAK_HOTKEY_ID
        self.commands: queue.Queue = queue.Queue()
        self.hotkey_updates: dict[
            int,
            HotkeyUpdateRequest | HotkeySuppressionRequest,
        ] = {}
        self.hotkey_update_lock = threading.Lock()
        self.hotkey_update_sequence = 0
        self.hwnd = 0
        self.ready = threading.Event()
        self.available = False
        self._class_atom = None
        self._hinst = None
        self._hicon = 0
        self._message = 0
        self._hotkey_message = 0
        self._taskbar_created = 0

    def start(self) -> bool:
        try:
            import win32gui  # noqa: F401
        except ImportError:
            return False
        threading.Thread(target=self._run, daemon=True).start()
        # The thread either registers the icon or gives up; either way it
        # sets the event, so this never hangs the window's startup.
        self.ready.wait(timeout=5)
        return self.available

    def _run(self) -> None:
        try:
            import win32api
            import win32con
            import win32gui
        except ImportError:
            self.ready.set()
            return
        try:
            self._message = win32con.WM_USER + 20
            self._hotkey_message = win32con.WM_USER + 21
            # Explorer broadcasts this when the notification area is rebuilt.
            # It is the only way to learn the icon has to be added again.
            self._taskbar_created = win32gui.RegisterWindowMessage("TaskbarCreated")
            self._hinst = win32api.GetModuleHandle(None)
            window_class = win32gui.WNDCLASS()
            window_class.hInstance = self._hinst
            window_class.lpszClassName = TRAY_WINDOW_CLASS
            window_class.lpfnWndProc = {
                self._message: self._on_notify,
                self._hotkey_message: self._on_hotkey_update,
                self._taskbar_created: self._on_taskbar_created,
                win32con.WM_COMMAND: self._on_command,
                win32con.WM_HOTKEY: self._on_hotkey,
                win32con.WM_CLOSE: self._on_close,
                win32con.WM_DESTROY: self._on_destroy,
            }
            self._class_atom = win32gui.RegisterClass(window_class)
            self.hwnd = win32gui.CreateWindow(
                self._class_atom, self.tooltip, win32con.WS_OVERLAPPED,
                0, 0, 0, 0, 0, 0, self._hinst, None,
            )
            handle = 0
            if self.icon is not None:
                try:
                    handle = win32gui.LoadImage(
                        0, str(self.icon), win32con.IMAGE_ICON, 0, 0,
                        win32con.LR_LOADFROMFILE | win32con.LR_DEFAULTSIZE,
                    )
                except Exception:
                    handle = 0
            if not handle:
                handle = win32gui.LoadIcon(0, win32con.IDI_APPLICATION)
            self._hicon = handle
            if self.hotkey is not None:
                modifiers, key = self.hotkey
                try:
                    # Raises when another program already owns the
                    # combination. That is a real answer for the member,
                    # not a crash: the companion says the key is taken
                    # rather than pretending it works.
                    win32gui.RegisterHotKey(
                        self.hwnd, self.hotkey_id, modifiers, key
                    )
                    self.hotkey_registered = True
                except Exception:
                    self.hotkey_registered = False
            # Started with Windows, this can run before the shell is ready to
            # take an icon, and a single attempt would simply lose it.
            for attempt in range(6):
                if self._add_icon():
                    self.available = True
                    break
                time.sleep(0.5)
        except Exception:
            self.available = False
            self.ready.set()
            return
        self.ready.set()
        try:
            win32gui.PumpMessages()
        except Exception:
            pass

    def _add_icon(self) -> bool:
        try:
            import win32gui
            flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
            win32gui.Shell_NotifyIcon(
                win32gui.NIM_ADD,
                (self.hwnd, 0, flags, self._message, self._hicon, self.tooltip),
            )
            return True
        except Exception:
            return False

    def _on_taskbar_created(self, _hwnd, _msg, _wparam, _lparam) -> int:
        """Explorer restarted and took the icon with it. Put it back, or the
        window can be hidden with nothing left to bring it up again."""
        self.available = self._add_icon()
        return 0

    def _on_notify(self, hwnd, _msg, _wparam, lparam) -> int:
        import win32con
        import win32gui
        if lparam in (win32con.WM_LBUTTONUP, win32con.WM_LBUTTONDBLCLK):
            self.commands.put("show")
        elif lparam == win32con.WM_RBUTTONUP:
            menu = win32gui.CreatePopupMenu()
            win32gui.AppendMenu(
                menu, win32con.MF_STRING, self.SHOW, "Open " + self.tooltip,
            )
            win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
            win32gui.AppendMenu(menu, win32con.MF_STRING, self.EXIT, "Exit")
            x, y = win32gui.GetCursorPos()
            # Both calls are the documented dance that lets the menu close
            # when the click lands somewhere else.
            win32gui.SetForegroundWindow(hwnd)
            win32gui.TrackPopupMenu(menu, win32con.TPM_LEFTALIGN, x, y, 0, hwnd, None)
            win32gui.PostMessage(hwnd, win32con.WM_NULL, 0, 0)
            # The menu is rebuilt on every right click, so it has to be
            # released on every right click too.
            win32gui.DestroyMenu(menu)
        return 0

    def _on_command(self, _hwnd, _msg, wparam, _lparam) -> int:
        import win32api
        chosen = win32api.LOWORD(wparam)
        if chosen == self.SHOW:
            self.commands.put("show")
        elif chosen == self.EXIT:
            self.commands.put("exit")
        return 0

    def _on_hotkey(self, _hwnd, _msg, wparam, _lparam) -> int:
        if (
            wparam == self.hotkey_id
            and self.hotkey_registered
            and not self.hotkey_suppressed
        ):
            # Tk is not thread safe, so this takes the same route every
            # other tray event does: a queue the window drains on its tick.
            self.commands.put("speak")
        return 0

    def update_hotkey(
        self,
        hotkey: tuple[int, int] | None,
        *,
        timeout: float = 1.5,
    ) -> bool:
        """Replace the global shortcut on the tray window's own thread."""
        if hotkey == self.hotkey and (
            (hotkey is None and not self.hotkey_registered)
            or self.hotkey_registered
        ):
            return True
        if not self.hwnd or not self._hotkey_message:
            return False
        request = HotkeyUpdateRequest(hotkey)
        request_id = self._store_hotkey_request(request)
        try:
            import win32gui

            win32gui.PostMessage(
                self.hwnd,
                self._hotkey_message,
                request_id,
                0,
            )
        except Exception:
            self._discard_hotkey_request(request_id)
            with request.lock:
                request.cancelled = True
            return False
        if not request.done.wait(timeout=max(float(timeout), 0.0)):
            # A delayed message must not surprise the member by applying a
            # shortcut after the UI already reported that it failed.
            with request.lock:
                if request.done.is_set():
                    return request.success
                request.cancelled = True
                self._discard_hotkey_request(request_id)
                return False
        return request.success

    def _store_hotkey_request(
        self,
        request: HotkeyUpdateRequest | HotkeySuppressionRequest,
    ) -> int:
        with self.hotkey_update_lock:
            self.hotkey_update_sequence = (self.hotkey_update_sequence + 1) & 0x7FFFFFFF
            if not self.hotkey_update_sequence:
                self.hotkey_update_sequence = 1
            request_id = self.hotkey_update_sequence
            self.hotkey_updates[request_id] = request
            return request_id

    def _discard_hotkey_request(self, request_id: int) -> None:
        with self.hotkey_update_lock:
            self.hotkey_updates.pop(request_id, None)

    def set_hotkey_suppressed(
        self,
        suppressed: bool,
        *,
        timeout: float = 0.5,
    ) -> bool:
        """Change dispatch state behind the tray thread's message barrier.

        Resuming through the window queue matters when the captured keys are
        also the active shortcut: any WM_HOTKEY already waiting ahead of this
        control message is discarded while suppression is still active.
        """
        value = bool(suppressed)
        if not self.hwnd or not self._hotkey_message:
            self.hotkey_suppressed = value
            return True
        if value:
            # Pausing must be immediate; resuming is the direction that needs
            # the ordered tray-thread barrier.
            self.hotkey_suppressed = True
        request = HotkeySuppressionRequest(value)
        request_id = self._store_hotkey_request(request)
        try:
            import win32gui

            win32gui.PostMessage(
                self.hwnd,
                self._hotkey_message,
                request_id,
                0,
            )
        except Exception:
            self._discard_hotkey_request(request_id)
            with request.lock:
                request.cancelled = True
            return False
        if not request.done.wait(timeout=max(float(timeout), 0.0)):
            with request.lock:
                if request.done.is_set():
                    return request.success
            # Leave the exact resume barrier live. If the tray thread is only
            # delayed, it must discard any earlier WM_HOTKEY before turning
            # dispatch back on. A dead tray cannot dispatch a shortcut anyway.
            return False
        return request.success

    def _on_hotkey_update(self, _hwnd, _msg, wparam, _lparam) -> int:
        with self.hotkey_update_lock:
            request = self.hotkey_updates.pop(int(wparam or 0), None)
        if request is None:
            return 0
        with request.lock:
            if not request.cancelled:
                if isinstance(request, HotkeySuppressionRequest):
                    self.hotkey_suppressed = request.suppressed
                    request.success = True
                else:
                    request.success = self._replace_hotkey_on_thread(
                        request.hotkey
                    )
            request.done.set()
        return 0

    def _replace_hotkey_on_thread(
        self,
        hotkey: tuple[int, int] | None,
    ) -> bool:
        """Try a candidate before releasing the current registration."""
        try:
            import win32gui
        except ImportError:
            return False
        if hotkey == self.hotkey and (
            (hotkey is None and not self.hotkey_registered)
            or self.hotkey_registered
        ):
            return True
        if hotkey is None:
            if self.hotkey_registered:
                try:
                    win32gui.UnregisterHotKey(self.hwnd, self.hotkey_id)
                except Exception:
                    return False
            self.hotkey = None
            self.hotkey_registered = False
            return True

        modifiers, key = hotkey
        candidate_id = (
            self.SPEAK_HOTKEY_ALT_ID
            if self.hotkey_id == self.SPEAK_HOTKEY_ID
            else self.SPEAK_HOTKEY_ID
        )
        if not self.hotkey_registered:
            candidate_id = self.hotkey_id
        try:
            win32gui.RegisterHotKey(
                self.hwnd,
                candidate_id,
                modifiers,
                key,
            )
        except Exception:
            return False

        if self.hotkey_registered:
            try:
                win32gui.UnregisterHotKey(self.hwnd, self.hotkey_id)
            except Exception:
                # The candidate is ours, but keeping both would make one key
                # open two composers. Drop it and leave the old truth intact.
                try:
                    win32gui.UnregisterHotKey(self.hwnd, candidate_id)
                except Exception:
                    pass
                return False
        self.hotkey_id = candidate_id
        self.hotkey = hotkey
        self.hotkey_registered = True
        return True

    def _on_close(self, hwnd, _msg, _wparam, _lparam) -> int:
        import win32gui
        win32gui.DestroyWindow(hwnd)
        return 0

    def _on_destroy(self, _hwnd, _msg, _wparam, _lparam) -> int:
        import win32gui
        if self.hotkey_registered:
            try:
                win32gui.UnregisterHotKey(self.hwnd, self.hotkey_id)
            except Exception:
                pass
            self.hotkey_registered = False
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (self.hwnd, 0))
        except Exception:
            pass
        win32gui.PostQuitMessage(0)
        return 0

    def notify(self, title: str, text: str) -> bool:
        """A balloon, purely so a first hide does not look like a crash."""
        if not self.available:
            return False
        try:
            import win32gui
            win32gui.Shell_NotifyIcon(
                win32gui.NIM_MODIFY,
                (self.hwnd, 0, win32gui.NIF_INFO, 0, 0, "", text, 5000, title,
                 getattr(win32gui, "NIIF_INFO", 1)),
            )
            return True
        except Exception:
            return False

    def stop(self) -> None:
        # The icon can fail while its hidden window and hotkey still exist.
        # Closing is keyed to the window, not to Explorer accepting the icon.
        if not self.hwnd:
            return
        try:
            import win32con
            import win32gui
            win32gui.PostMessage(self.hwnd, win32con.WM_CLOSE, 0, 0)
        except Exception:
            pass
        self.available = False


# --------------------------------------------------------------------------
# Win32 window chrome
# --------------------------------------------------------------------------

GWL_EXSTYLE = -20
WS_EX_APPWINDOW = 0x00040000
WS_EX_TOOLWINDOW = 0x00000080
SW_MINIMIZE = 6
SW_SHOW = 5
SW_RESTORE = 9
GW_OWNER = 4
WM_COMMAND = 0x0111
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
# Without ASYNCWINDOWPOS, SetWindowPos on a FOREIGN window sends
# WM_WINDOWPOSCHANGING synchronously and blocks against a wedged target -
# the exact app this path exists to recover.
SWP_ASYNCWINDOWPOS = 0x4000
MONITOR_DEFAULTTONEAREST = 2


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", _RECT),
                ("rcWork", _RECT), ("dwFlags", wintypes.DWORD)]


def companion_tray_window() -> int:
    """The running companion's tray message window, or 0.

    Found by class rather than title: the tray helper is created with the
    app's own tooltip as its title, so title matching picks whichever of
    the two windows enumerates first - the wrong one, half the time.
    """
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.FindWindowW.restype = wintypes.HWND
        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        return int(user32.FindWindowW(TRAY_WINDOW_CLASS, None) or 0)
    except (OSError, AttributeError, ValueError):
        return 0


def window_process_id(hwnd: int) -> int:
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(
            wintypes.HWND(hwnd), ctypes.byref(pid)
        )
        return int(pid.value)
    except (OSError, AttributeError, ValueError):
        return 0


def main_window_for_pid(pid: int) -> int:
    """The companion's top-level Tk window belonging to pid, or 0.

    Three filters, each load-bearing:
      - the pid, obviously;
      - class TkTopLevel, because the tray helper window carries the SAME
        TITLE as the app (it is created with the tooltip as its title);
      - no GW_OWNER, so an owned dialog is never mistaken for the app.
    Deliberately NOT filtered on visibility - the withdrawn, tray-hidden
    window is invisible and is exactly what this exists to find - and not
    on WS_EX_TOOLWINDOW either, because the borderless window IS a tool
    window until claim_taskbar_button flips it.
    """
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        enum_proc = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )
        found = []

        def visit(hwnd, _lparam):
            if window_process_id(int(hwnd)) != pid:
                return True
            buffer = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, buffer, 64)
            if buffer.value != "TkTopLevel":
                return True
            user32.GetWindow.restype = wintypes.HWND
            if user32.GetWindow(hwnd, GW_OWNER):
                return True
            found.append(int(hwnd))
            return False

        user32.EnumWindows(enum_proc(visit), 0)
        return found[0] if found else 0
    except (OSError, AttributeError, ValueError):
        return 0


def raise_running_companion(pid: int | None) -> bool:
    """Bring the already-running companion forward. True only if OBSERVED.

    The new process holds the foreground rights (the user just launched
    it); the running one has the window. So: grant the rights across,
    ask the running app to show itself through the tray channel it
    already has, and then watch the actual window state rather than
    trusting that any of it worked. A raise that silently fails falls
    back to the caller's messagebox, which at least says the app is
    running somewhere.
    """
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        tray = companion_tray_window()
        if tray:
            # Prefer the tray window's owner over the lock file: the
            # class name is unique to this product, while the lock can
            # be stale and its image whitelist accepts any python.exe.
            owner = window_process_id(tray)
            if owner:
                pid = owner
        if not pid:
            return False
        hwnd = main_window_for_pid(pid)
        if not hwnd:
            return False

        # Grant our foreground rights to the running process BEFORE
        # asking it to show itself, so its own SetForegroundWindow works.
        user32.AllowSetForegroundWindow(wintypes.DWORD(pid))

        if tray:
            # The handler already exists in every shipped build: the tray
            # menu's "Open" posts the same command. tick() drains it and
            # calls show().
            user32.PostMessageW(
                wintypes.HWND(tray), WM_COMMAND, TRAY_COMMAND_SHOW, 0
            )
        else:
            # No pywin32 means the window can never be withdrawn - the
            # close button minimizes instead - and a minimized window is
            # restorable from outside with no cooperation.
            if user32.IsIconic(wintypes.HWND(hwnd)):
                user32.ShowWindow(wintypes.HWND(hwnd), SW_RESTORE)
            else:
                user32.ShowWindow(wintypes.HWND(hwnd), SW_SHOW)

        # Observe, do not assume: poll the real window state. Success is
        # visible-and-not-iconic; a wedged app never gets there and the
        # caller falls back to the messagebox.
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            visible = bool(user32.IsWindowVisible(wintypes.HWND(hwnd)))
            iconic = bool(user32.IsIconic(wintypes.HWND(hwnd)))
            if visible and not iconic:
                break
            time.sleep(0.06)
        else:
            return False

        # The window is up. Try for foreground; if Windows refuses, a
        # brief topmost toggle plus a taskbar flash is the honest best -
        # in try/finally so a failure cannot strand the window topmost.
        user32.SetForegroundWindow(wintypes.HWND(hwnd))
        user32.GetForegroundWindow.restype = wintypes.HWND
        if int(user32.GetForegroundWindow() or 0) != hwnd:
            flags = (
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_ASYNCWINDOWPOS
            )
            try:
                user32.SetWindowPos(
                    wintypes.HWND(hwnd), wintypes.HWND(HWND_TOPMOST),
                    0, 0, 0, 0, flags,
                )
            finally:
                user32.SetWindowPos(
                    wintypes.HWND(hwnd), wintypes.HWND(HWND_NOTOPMOST),
                    0, 0, 0, 0, flags,
                )
            user32.FlashWindow(wintypes.HWND(hwnd), True)
        return True
    except (OSError, AttributeError, ValueError):
        return False


def window_handle(root: tk.Misc) -> int:
    """The real top level handle - winfo_id() is Tk's inner frame.

    Always resolved fresh. Tk rebuilds its top level window when the
    decorations change, so a handle cached across that is a handle to
    something that no longer exists.
    """
    try:
        user32 = ctypes.windll.user32
        user32.GetParent.restype = wintypes.HWND
        user32.GetParent.argtypes = [wintypes.HWND]
        inner = root.winfo_id()
        parent = user32.GetParent(inner)
        return int(parent or inner)
    except Exception:
        return 0


def own_popup_window(popup: tk.Misc, owner: tk.Misc) -> bool:
    """Keep a borderless popup above only its companion window on Windows."""
    if sys.platform != "win32":
        try:
            popup.transient(owner)
            popup.lift(owner)
            return True
        except tk.TclError:
            return False
    try:
        popup.update_idletasks()
        popup_hwnd = window_handle(popup)
        owner_hwnd = window_handle(owner)
        if not popup_hwnd or not owner_hwnd:
            return False
        user32 = ctypes.windll.user32
        set_parent = user32.SetWindowLongPtrW
        set_parent.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        set_parent.restype = ctypes.c_ssize_t
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
        # GWLP_HWNDPARENT turns a top-level window into an owned popup. Owned
        # windows stay above their owner but do not become system-wide topmost.
        set_parent(popup_hwnd, -8, owner_hwnd)
        owned = int(get_window(popup_hwnd, 4) or 0) == owner_hwnd  # GW_OWNER
        if not owned:
            return False
        # HWND_TOP; SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_SHOWWINDOW.
        set_position(popup_hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010 | 0x0040)
        return True
    except Exception:
        return False


def claim_taskbar_button(hwnd: int) -> bool:
    """A borderless window is a tool window to Windows, and tool windows get
    no taskbar button. Say otherwise, then bounce the window so the shell
    notices."""
    if not hwnd:
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
        style = (style & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW
        set_style(hwnd, GWL_EXSTYLE, style)
        return True
    except Exception:
        return False


def has_taskbar_button(hwnd: int) -> bool:
    if not hwnd:
        return False
    try:
        get_style = ctypes.windll.user32.GetWindowLongPtrW
        get_style.restype = ctypes.c_longlong
        get_style.argtypes = [wintypes.HWND, ctypes.c_int]
        style = get_style(hwnd, GWL_EXSTYLE)
        return bool(style & WS_EX_APPWINDOW) and not bool(style & WS_EX_TOOLWINDOW)
    except Exception:
        return False


def work_area(hwnd: int, root: tk.Tk) -> tuple[int, int, int, int]:
    """The current monitor's usable area, so maximise does not swallow the
    taskbar or land on the wrong screen of a multi monitor desk.

    The restypes matter: a monitor handle is pointer wide, and ctypes
    defaults every return to a 32 bit int, which quietly truncates it into
    a handle GetMonitorInfoW then rejects.
    """
    try:
        user32 = ctypes.windll.user32
        user32.MonitorFromWindow.restype = wintypes.HANDLE
        user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        user32.GetMonitorInfoW.restype = wintypes.BOOL
        user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MONITORINFO)]
        monitor = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        if monitor:
            info = _MONITORINFO()
            info.cbSize = ctypes.sizeof(_MONITORINFO)
            if user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                rect = info.rcWork
                return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
    except Exception:
        pass
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


# --------------------------------------------------------------------------
# Services
# --------------------------------------------------------------------------

@dataclass
class Service:
    spec: ServiceSpec
    window: "CompanionWindow"
    process: subprocess.Popen[str] | None = None
    action_process: subprocess.Popen | None = None
    dot: tk.Canvas | None = None
    dot_id: int = 0
    rail: tk.Frame | None = None
    status: tk.Label | None = None
    result_badge: tk.Label | None = None
    result_badge_rail: tk.Frame | None = None
    button: tk.Button | None = None
    start_button: tk.Button | None = None
    stop_button: tk.Button | None = None
    lifecycle_badge: tk.Label | None = None
    lifecycle_badge_rail: tk.Frame | None = None
    listening_indicator: tk.Frame | None = None
    listening_icon: tk.Canvas | None = None
    listening_label: tk.Label | None = None
    listening_indicator_before: tk.Widget | None = None
    listening_visible: bool = False
    listening_state: bool | None = False
    # The sim progress bar under the status line: a row holding the bar
    # and its caption, packed only while a sim is reporting and gone the
    # moment the job reaches any settled state. Absent on rows that
    # never sim.
    progress_row: tk.Frame | None = None
    progress_bar: tk.Canvas | None = None
    progress_caption: tk.Label | None = None
    # The row is packed straight after this widget - the card's top
    # block - so showing it never changes that block's height and the
    # controls on its right never move.
    progress_anchor: tk.Widget | None = None
    progress_visible: bool = False
    job_state: str = "checking"
    restart_at: float = 0.0
    # Consecutive failed or short-lived runs, driving the backoff and
    # the give-up. Reset by a healthy run or a deliberate Start, never
    # by an automatic one - that would flatten the backoff to nothing.
    restart_failures: int = 0
    started_at: float = 0.0
    # The player turned their own captions off. Managed editions
    # reconnects a dropped receiver on its own after three seconds, which
    # is right for a network blip and wrong for a deliberate stop - it
    # would undo the button before they let go of the mouse. Deliberately
    # NOT persisted: a fresh launch starts from the EDITION's default,
    # because a silent off-state surviving a restart is indistinguishable
    # from the product being broken. That default differs on purpose:
    # the member edition starts with captions on (they are its entire
    # purpose), while the Downtime editions start with the feed paused -
    # visibly, card reading "Captions off" and the button offering
    # "Resume caption feed" - because they are raid tools first and an
    # overlay appearing unasked is noise a hearing player then has to
    # hunt down.
    paused_by_member: bool = False
    lines: queue.Queue = field(default_factory=queue.Queue)
    # A finished action reports here, drained on the Tk thread. Without
    # it an action that dies immediately is indistinguishable from one
    # that worked, which is how a failing Pair button stayed invisible.
    action_results: queue.Queue = field(default_factory=queue.Queue)
    local_stop_results: queue.Queue = field(default_factory=queue.Queue)
    local_stop_in_flight: bool = False
    local_stop_thread: threading.Thread | None = field(default=None, repr=False)
    external_results: queue.Queue = field(default_factory=queue.Queue)
    external_in_flight: bool = False
    external_sequence: int = 0
    external_applied_sequence: int = 0
    external_next_poll: float = 0.0
    external_snapshot: DiscordWorkerSnapshot | None = None

    def set_status(
        self,
        text: str,
        color: str,
        job_state: str | None = None,
    ) -> None:
        if self.status is not None:
            self.status.config(text=text)
        if self.result_badge is not None and job_state is not None:
            # Hosted completion is followed by a final 1/1 progress line.
            # Keep the terminal result visible until a real new job marker
            # arrives instead of flashing Done for one stdout line.
            # "done" means a result is sitting on disk ready for an explicit
            # action in WoW. A SavedVariables timestamp cannot prove the
            # player chose Load Results, so only a real new request or a
            # failure changes that state. Work merely STARTING does not - and
            # with two requests outstanding it used to: the gear sim finished,
            # then five seconds later the still-queued vault request printed
            # its cooldown line, the badge flipped from "Done" to "Queued",
            # and the finished result was hidden.
            preserve_done = (
                self.job_state == "done"
                and job_state in ("checking", "queued", "ready")
            )
            if not preserve_done:
                self.job_state = job_state
            label, badge_color = job_status_badge(self.job_state)
            self.result_badge.config(
                text=label,
                fg=badge_color,
            )
            if self.result_badge_rail is not None:
                self.result_badge_rail.config(bg=badge_color)
        if self.dot is not None:
            if hasattr(self.dot, "show"):
                self.dot.show(color)
            else:
                self.dot.itemconfig(self.dot_id, fill=color)
        if self.rail is not None:
            self.rail.config(bg=color)
        update_signal = getattr(self.window, "update_service_signal", None)
        if update_signal is not None:
            update_signal(self.spec.key, color)
        # Any settled job state retires the bar. "checking" is the only
        # state a sim reports progress under, and a bar left standing
        # beside "Results ready" would say the sim was still running.
        if job_state is not None and job_state != "checking":
            self.clear_progress()

    def set_progress(self, fraction: float, caption: str | None = None) -> None:
        """Fill the bar and show it if it was hidden.

        caption None means the percent; an explicit string, empty
        included, is shown as given.
        """
        if self.progress_bar is None or self.progress_row is None:
            return
        try:
            fraction = float(fraction)
        except (TypeError, ValueError):
            return
        if not math.isfinite(fraction):
            return
        fraction = max(0.0, min(1.0, fraction))
        self.progress_bar.set_progress(fraction)
        if self.progress_caption is not None:
            self.progress_caption.config(
                text=f"{int(round(fraction * 100))}%" if caption is None else caption
            )
        if not self.progress_visible:
            options: dict[str, object] = {"fill": "x", "pady": (8, 0)}
            if self.progress_anchor is not None:
                options["after"] = self.progress_anchor
            self.progress_row.pack(**options)
            self.progress_visible = True

    def clear_progress(self) -> None:
        if self.progress_row is None or not self.progress_visible:
            return
        self.progress_row.pack_forget()
        self.progress_visible = False
        if self.progress_bar is not None:
            self.progress_bar.set_progress(0.0)

    def set_lifecycle_state(
        self,
        state: str,
        *,
        can_start: bool | None = None,
        can_stop: bool | None = None,
    ) -> None:
        """Update the visible process state and its two explicit controls."""
        try:
            label, color = LIFECYCLE_BADGE_STYLES[state]
        except KeyError:
            raise ValueError("unknown lifecycle status") from None
        if self.lifecycle_badge is not None:
            self.lifecycle_badge.config(text=label, fg=color)
        if self.lifecycle_badge_rail is not None:
            self.lifecycle_badge_rail.config(bg=color)
        if can_start is None:
            can_start = state in {"stopped", "attention"}
        if can_stop is None:
            can_stop = state in {"running", "online"}
        if self.start_button is not None:
            self.start_button.config(state="normal" if can_start else "disabled")
        if self.stop_button is not None:
            self.stop_button.config(state="normal" if can_stop else "disabled")

    def set_listening_indicator(self, listening: bool | None) -> None:
        """Show REC for active capture and REC? only after active uncertainty."""
        if listening is None and self.listening_state is False:
            listening = False
        self.listening_state = listening
        self.listening_visible = listening is not False
        indicator = self.listening_indicator
        if indicator is None:
            return
        if self.listening_label is not None:
            self.listening_label.config(text="REC" if listening is True else "REC?")
        if self.listening_visible:
            if not indicator.winfo_manager():
                options = {"side": "left", "padx": (0, 7)}
                if self.listening_indicator_before is not None:
                    options["before"] = self.listening_indicator_before
                indicator.pack(**options)
        elif indicator.winfo_manager():
            indicator.pack_forget()

    def start(self, *, automatic: bool = False) -> None:
        if self.spec.external_control:
            self.request_external_status(force=True)
            return
        # local_stop_in_flight as well as process: a stop the member asked
        # for is still settling, and starting into it would race it.
        if self.process is not None or self.local_stop_in_flight:
            return
        # Clear the schedule on entry so a stale timestamp cannot make
        # tick respawn on every 120ms pass. Every early return below
        # then has to RE-ARM it if this was an automatic attempt -
        # those paths never spawn a child, so no death event is coming
        # to reschedule from, and leaving it clear makes a single
        # transient failure terminal for the session.
        self.restart_at = 0.0
        if not automatic:
            # A deliberate Start is the member intervening; give them a
            # full set of attempts again.
            self.restart_failures = 0
        # Starting is itself the member changing their mind.
        self.paused_by_member = False
        if getattr(self.window, "preview", False) is True:
            self.set_status("Ready", SUCCESS, "ready")
            self.set_lifecycle_state("running")
            return
        if not self.spec.script.is_file():
            self.set_status("Not installed", MUTED, "attention")
            self.set_lifecycle_state("unavailable")
            self.reschedule_after_failed_start(automatic)
            return
        try:
            self.process = subprocess.Popen(
                # -u: a piped child is block-buffered otherwise, which would
                # leave this window blank through a whole run.
                [
                    child_python(), "-E", "-s", "-B", "-u",
                    str(self.spec.script), *self.spec.arguments,
                ],
                cwd=str(self.spec.script.parents[1]),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                errors="replace", bufsize=1, creationflags=NO_WINDOW,
            )
        except OSError as error:
            self.window.append(self.spec, "Could not start: " + str(error))
            self.set_status(
                "Could not start - open Activity",
                DANGER,
                "attention",
            )
            self.set_lifecycle_state("attention")
            self.reschedule_after_failed_start(automatic)
            return
        assign_to_watcher_job(self.process)
        # started_at, not a restart_at clear: the clear now happens at the
        # top of start(), and this is what the healthy-run check measures.
        self.started_at = time.monotonic()
        self.set_status("Starting", ACCENT, "checking")
        self.set_lifecycle_state("running")
        if self.button is not None:
            self.button.config(text="Stop")
        threading.Thread(target=self.pump, daemon=True).start()

    def pump(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            self.lines.put((process, line))
        self.lines.put((process, None))

    @staticmethod
    def terminate_process_tree(process: subprocess.Popen[str]) -> bool:
        """Stop a child tree without touching Tk state from the worker thread."""
        if process.poll() is not None:
            return True
        taskkill_failed = False
        # Kill the tree: terminating only the parent orphans a child such
        # as SimulationCraft, which keeps every core it was given busy.
        try:
            result = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                timeout=20,
                creationflags=NO_WINDOW,
            )
            taskkill_failed = result.returncode != 0
        except (OSError, subprocess.SubprocessError):
            taskkill_failed = True
        if taskkill_failed and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=2)
            except (OSError, subprocess.SubprocessError):
                pass
        return process.poll() is not None

    def stop(self) -> None:
        if self.spec.external_control:
            # The Windows worker belongs to the machine, not this window.
            # Closing Downtime must never stop it implicitly.
            return
        stop_thread = self.local_stop_thread
        if stop_thread is not None and stop_thread.is_alive():
            stop_thread.join(timeout=32)
        process = self.process
        if process and process.poll() is None:
            self.terminate_process_tree(process)
        stopped = process is None or process.poll() is not None
        if stopped:
            self.process = None
        self.local_stop_in_flight = False
        self.local_stop_thread = None
        if stopped:
            if self.button is not None:
                self.button.config(text="Start", state="normal")
            self.set_lifecycle_state("stopped")
        else:
            if self.button is not None:
                self.button.config(state="normal")
            self.set_status("Could not stop - open Activity", DANGER, "attention")
            self.set_lifecycle_state(
                "attention", can_start=False, can_stop=True
            )

    def request_start(self) -> None:
        if self.spec.external_control:
            self._begin_external_request("start")
            return
        self.start()

    def request_stop(self) -> None:
        if self.spec.external_control:
            self._begin_external_request("stop")
            return
        if self.local_stop_in_flight:
            return
        process = self.process
        if process is None:
            self.set_status("Stopped", MUTED, "stopped")
            self.set_lifecycle_state("stopped")
            return
        self.local_stop_in_flight = True
        self.set_status("Stopping", WARNING)
        self.set_lifecycle_state("stopping")
        if self.button is not None:
            self.button.config(state="disabled")

        def stop_worker() -> None:
            stopped = self.terminate_process_tree(process)
            self.local_stop_results.put_nowait((process, stopped))

        stop_thread = threading.Thread(target=stop_worker, daemon=True)
        self.local_stop_thread = stop_thread
        try:
            stop_thread.start()
        except RuntimeError:
            self.local_stop_in_flight = False
            self.local_stop_thread = None
            self.paused_by_member = False
            if self.button is not None:
                self.button.config(state="normal")
            self.set_status("Could not stop - open Activity", DANGER, "attention")
            self.set_lifecycle_state(
                "attention", can_start=False, can_stop=True
            )

    def drain_local_stop_results(self) -> None:
        while True:
            try:
                process, stopped = self.local_stop_results.get_nowait()
            except queue.Empty:
                break
            if process is not self.process:
                continue
            self.local_stop_in_flight = False
            self.local_stop_thread = None
            if stopped:
                self.process = None
                if self.button is not None:
                    self.button.config(text="Start", state="normal")
                if self.paused_by_member:
                    self.restart_at = 0.0
                    self.set_status("Captions off", MUTED)
                else:
                    self.set_status("Stopped", MUTED, "stopped")
                self.set_lifecycle_state("stopped")
            else:
                self.paused_by_member = False
                if self.button is not None:
                    self.button.config(state="normal")
                self.set_status("Could not stop - open Activity", DANGER, "attention")
                self.set_lifecycle_state(
                    "attention", can_start=False, can_stop=True
                )
            refresh_member = getattr(
                self.window, "refresh_member_caption_button", None
            )
            if refresh_member is not None:
                refresh_member()
            member_button = getattr(self.window, "member_caption_button", None)
            if member_button is not None:
                member_button.config(state="normal")

    def request_external_status(self, force: bool = False) -> None:
        if not self.spec.external_control or self.external_in_flight:
            return
        if getattr(self.window, "preview", False) is True:
            self.apply_external_snapshot(
                DiscordWorkerSnapshot("running", True, True, "preview")
            )
            return
        if not force and time.monotonic() < self.external_next_poll:
            return
        self._begin_external_request(None)

    def _begin_external_request(self, action: str | None) -> None:
        if not self.spec.external_control or self.external_in_flight:
            return
        self.external_in_flight = True
        self.external_sequence += 1
        sequence = self.external_sequence
        if action == "start":
            self.set_status("Starting the Discord worker", WARNING)
            self.set_lifecycle_state("starting")
        elif action == "stop":
            self.set_status("Stopping the Discord worker", WARNING)
            self.set_lifecycle_state("stopping")
        elif self.external_snapshot is None:
            self.set_status("Checking the Discord worker", WARNING)
            self.set_lifecycle_state("checking")

        def request_worker() -> None:
            try:
                snapshot = discord_worker_request(action)
                error_message = ""
            except RuntimeError as error:
                snapshot = None
                error_message = str(error).strip()
            except Exception:  # A thread boundary must always report back.
                snapshot = None
                error_message = "Discord worker status is unavailable."
            self.external_results.put_nowait(
                (sequence, action, snapshot, error_message)
            )

        worker_thread = threading.Thread(target=request_worker, daemon=True)
        try:
            worker_thread.start()
        except RuntimeError:
            self.external_in_flight = False
            self.external_next_poll = (
                time.monotonic() + EXTERNAL_SERVICE_POLL_SECONDS
            )
            self.set_status("Discord worker status is unavailable.", DANGER)
            self.set_lifecycle_state(
                "unavailable", can_start=False, can_stop=False
            )

    def apply_external_snapshot(self, snapshot: DiscordWorkerSnapshot) -> None:
        self.external_snapshot = snapshot
        self.set_listening_indicator(snapshot.listening)
        if snapshot.state == "running" and snapshot.healthy:
            self.set_status(
                "Live captions - consenting speakers only"
                if snapshot.listening is True
                else "Connected to Discord",
                SUCCESS,
            )
            self.set_lifecycle_state("online", can_start=False, can_stop=True)
        elif snapshot.state == "running":
            self.set_status("Running - Discord is connecting", WARNING)
            self.set_lifecycle_state("starting", can_start=False, can_stop=True)
        elif snapshot.state in {"start_pending", "continue_pending"}:
            self.set_status("Starting the Discord worker", WARNING)
            self.set_lifecycle_state("starting", can_start=False, can_stop=False)
        elif snapshot.state in {"stop_pending", "pause_pending"}:
            self.set_status("Stopping the Discord worker", WARNING)
            self.set_lifecycle_state("stopping", can_start=False, can_stop=False)
        elif snapshot.state == "paused":
            self.set_status(
                "Paused - capture shutdown is not confirmed"
                if snapshot.listening is True
                else "Paused - Discord is not listening",
                WARNING,
            )
            self.set_lifecycle_state("attention", can_start=False, can_stop=True)
        elif snapshot.state == "stopped" and snapshot.configured:
            self.set_status("Stopped", MUTED)
            self.set_lifecycle_state("stopped", can_start=True, can_stop=False)
        elif snapshot.state == "stopped":
            self.set_status("Connect Discord in the console first", WARNING)
            self.set_lifecycle_state("setup", can_start=False, can_stop=False)
        elif snapshot.state == "not installed":
            self.set_status("Discord worker service is not installed", DANGER)
            self.set_lifecycle_state("unavailable", can_start=False, can_stop=False)
        else:
            self.set_status("Discord worker service is unavailable", DANGER)
            self.set_lifecycle_state("unavailable", can_start=False, can_stop=False)

    def drain_external_results(self) -> None:
        while True:
            try:
                sequence, _action, snapshot, error_message = (
                    self.external_results.get_nowait()
                )
            except queue.Empty:
                break
            if sequence < self.external_applied_sequence:
                continue
            self.external_applied_sequence = sequence
            self.external_in_flight = False
            self.external_next_poll = (
                time.monotonic() + EXTERNAL_SERVICE_POLL_SECONDS
            )
            if snapshot is None:
                # Uncertainty is a transition from possible capture, not a
                # reason to invent a recording warning after capture was
                # explicitly confirmed inactive.
                self.set_listening_indicator(None)
                self.set_status(error_message, DANGER)
                self.set_lifecycle_state(
                    "unavailable", can_start=False, can_stop=False
                )
                continue
            self.apply_external_snapshot(snapshot)

    def reschedule_after_failed_start(self, automatic: bool) -> None:
        """Re-arm the backoff after a start that never spawned.

        note_death_and_schedule_restart is reached only from the
        pump EOF path, so it covers a child that RAN and then died.
        A launch that fails produces no child and no EOF, so without
        this the service sits with restart_at at 0 and tick never
        looks at it again - one antivirus lock on pythonw.exe and a
        member's captions are gone until they restart the app, with
        no Stop/Start button on a managed edition to force it.

        Only automatic attempts re-arm. A member who pressed Start
        and was told "Not installed" should be left alone rather
        than put on a silent retry they did not ask for.
        """
        if not automatic or not self.spec.auto_restart:
            return
        self.note_death_and_schedule_restart()

    def note_death_and_schedule_restart(self) -> None:
        """Schedule the next automatic restart, slowing down each time.

        A flat three-second retry is fine for a network blip and wrong
        for anything permanent: a missing file or a bad saved pairing
        fails in well under a second, so the member gets a child
        spawned every three seconds until they close the app - and the
        managed editions that carry auto_restart are exactly the ones
        with no Stop button to end it.
        """
        ran_for = (
            time.monotonic() - self.started_at if self.started_at else 0.0
        )
        if ran_for >= RESTART_HEALTHY_SECONDS:
            # It worked for a while, so this is a fresh problem.
            self.restart_failures = 0
        self.restart_failures += 1
        if self.restart_failures > RESTART_GIVE_UP_AFTER:
            self.restart_at = 0.0
            self.set_status(
                self.spec.label + " keeps stopping - open Activity",
                DANGER,
            )
            return
        delay = min(
            RESTART_BASE_SECONDS * (2 ** (self.restart_failures - 1)),
            RESTART_MAX_SECONDS,
        )
        self.restart_at = time.monotonic() + delay
        self.set_status("Reconnecting automatically", WARNING)

    def toggle(self) -> None:
        if self.process is None:
            self.request_start()
        else:
            self.request_stop()

    def toggle_member_captions(self) -> None:
        """The member's own captions, and only their own.

        Nothing here reaches the guild: it stops this computer receiving,
        which is why the label says "my". Somebody who thought this
        button silenced the whole raid's captions would be wrong in the
        worst possible direction.
        """
        if self.process is None:
            self.start()
        else:
            self.paused_by_member = True
            self.request_stop()
            member_button = getattr(self.window, "member_caption_button", None)
            if member_button is not None:
                member_button.config(
                    state="disabled" if self.local_stop_in_flight else "normal"
                )
        self.window.refresh_member_caption_button()

    def run_action(self, script: Path | None = None) -> None:
        if getattr(self.window, "preview", False) is True:
            self.set_status("Preview - no action started", MUTED)
            return
        script = script or self.spec.action_script
        if script is None:
            return
        action_name = next(
            (
                label
                for label, candidate in (
                    (self.spec.action_label, self.spec.action_script),
                    (self.spec.second_action_label, self.spec.second_action_script),
                    (self.spec.third_action_label, self.spec.third_action_script),
                )
                if candidate == script and label
            ),
            "Action",
        )
        if self.action_process is not None and self.action_process.poll() is None:
            return
        if not script.is_file():
            self.set_status(action_name + " tool is not installed", DANGER)
            return
        try:
            self.action_process = subprocess.Popen(
                [child_python(), "-E", "-s", "-B", str(script)],
                cwd=str(script.parents[1]),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=NO_WINDOW,
            )
        except OSError:
            self.set_status("Could not open " + action_name, DANGER)
            return
        assign_to_watcher_job(self.action_process)

        def watch_action(process, name):
            try:
                output = process.stdout.read() if process.stdout else ""
            except (OSError, ValueError):
                output = ""
            code = process.wait()
            self.action_results.put_nowait((name, code, output))

        threading.Thread(
            target=watch_action,
            args=(self.action_process, action_name),
            daemon=True,
        ).start()

    def drain(self) -> None:
        self.drain_local_stop_results()
        self.drain_external_results()
        if self.spec.external_control:
            return
        # An action that failed must say so. The last non-empty line of
        # its output is the useful part: the guarded import raises a
        # message written for a member rather than a traceback.
        while True:
            try:
                name, code, output = self.action_results.get_nowait()
            except queue.Empty:
                break
            if not code:
                continue
            tail = ""
            for line in reversed((output or "").splitlines()):
                if line.strip():
                    tail = line.strip()
                    break
            if tail:
                self.window.append(self.spec, name + " failed: " + tail)
            self.set_status(
                name + " did not open - check Activity",
                DANGER,
            )
        try:
            while True:
                process, line = self.lines.get_nowait()
                # A quick Stop/Start can leave the old reader's EOF behind.
                # Never let that stale sentinel mark the new process stopped.
                if process is not self.process:
                    continue
                if self.local_stop_in_flight:
                    continue
                if line is None:
                    code = self.process.poll() if self.process else None
                    self.process = None
                    # A child that is gone reports nothing; a bar it
                    # left half full would be a lie about a dead sim.
                    self.clear_progress()
                    if self.button is not None:
                        self.button.config(text="Start")
                    if self.paused_by_member:
                        self.restart_at = 0.0
                        self.restart_failures = 0
                        self.set_status("Captions off", MUTED)
                    elif self.spec.auto_restart:
                        self.note_death_and_schedule_restart()
                    elif code:
                        self.window.append(
                            self.spec,
                            "Stopped unexpectedly with exit code " + str(code),
                        )
                        self.set_status(
                            "Stopped unexpectedly - open Activity",
                            DANGER,
                            "attention",
                        )
                        self.set_lifecycle_state("attention")
                    else:
                        self.set_status("Stopped", MUTED, "stopped")
                        self.set_lifecycle_state("stopped")
                    continue
                stripped = line.strip()
                if stripped.startswith(GHSTATUS_PREFIX):
                    status = parse_status_line(stripped)
                    if status is not None:
                        # The child said what happened, in a form built for
                        # this window. No substring guessing, no marker
                        # ordering, no measurement-phrase allowlist - and
                        # the line itself stays out of Activity, because
                        # the human sentence for the same event is printed
                        # beside it.
                        text, color, job_state = describe_status(status)
                        self.set_status(text, color, job_state)
                        progress = describe_progress(status)
                        if progress is not None:
                            self.set_progress(progress[1], progress[2])
                        continue
                    # Not valid JSON after the prefix: fall through and let
                    # it be an ordinary line. A truncated status must not
                    # vanish silently.
                counted = PROGRESS.match(stripped)
                if counted:
                    self.set_status(stripped[:70], WARNING, "checking")
                    # The same per-item count fills the bar: "[##..] 3/9".
                    # A complete count is the loop's last word and clears
                    # it: the hosted client prints "[####] 1/1 Result
                    # ready" AFTER the result marker, and a full bar left
                    # under Done would say the job was still running.
                    done, total = int(counted.group(1)), int(counted.group(2))
                    if total > 0 and done < total:
                        self.set_progress(max(0, done) / total)
                    else:
                        self.clear_progress()
                    continue
                self.window.append(self.spec, line)
                for marker, text, color, job_state in self.spec.markers:
                    if marker in line:
                        if (
                            not getattr(self.window, "hosted_edition", False)
                            and not getattr(
                                self.window, "result_carrier_available", True
                            )
                            and text == "Results ready - choose Load Results"
                        ):
                            text = "Results ready - reload WoW"
                        self.set_status(text, color, job_state)
                        break
                else:
                    # The Activity summary used to carry this last resort,
                    # and it was the only thing that noticed a failure no
                    # marker recognises. That summary is gone, so the net
                    # moves onto the card rather than being lost with it.
                    if reads_as_a_failure(stripped):
                        self.set_status(
                            "Needs attention - open Activity",
                            DANGER,
                            "attention",
                        )
        except queue.Empty:
            pass


# --------------------------------------------------------------------------
# Settings menu
# --------------------------------------------------------------------------

def anchored_popup_position(
    anchor_rect: tuple[int, int, int, int],
    popup_size: tuple[int, int],
    work_rect: tuple[int, int, int, int],
    gap: int = 4,
) -> tuple[int, int]:
    """Place a popup beside its anchor without stranding rows off-screen."""
    anchor_x, anchor_y, anchor_width, anchor_height = anchor_rect
    popup_width, popup_height = popup_size
    left, top, width, height = work_rect
    right = left + width
    bottom = top + height
    x = anchor_x + anchor_width - popup_width
    below = anchor_y + anchor_height + gap
    above = anchor_y - popup_height - gap
    y = above if below + popup_height > bottom and above >= top else below
    x = max(left, min(x, right - popup_width))
    y = max(top, min(y, bottom - popup_height))
    return x, y


def foreground_window_handle() -> int:
    """Return the current foreground window without making it foreground."""
    if sys.platform != "win32":
        return 0
    try:
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = wintypes.HWND
        return int(user32.GetForegroundWindow() or 0)
    except Exception:
        return 0


def quick_chat_target_rect(
    root: tk.Misc,
    fallback_hwnd: int,
) -> tuple[int, int, int, int]:
    """Prefer the WoW client, then the monitor the player is using now."""
    try:
        from VoiceCaptions.overlay import find_wow_client_rect

        wow_rect = find_wow_client_rect()
        if wow_rect is not None and wow_rect[2] > 0 and wow_rect[3] > 0:
            return wow_rect
    except (ImportError, OSError, RuntimeError, ValueError):
        pass
    hwnd = foreground_window_handle() or fallback_hwnd
    return work_area(hwnd, root)


# How far above the bottom of the usable screen the toast sits. Shino,
# 2026-09-04: "can we make our notification a little higher? It's
# blocking other elements". Windows places a tray balloon itself and an
# application cannot move it, so the toast below is our own window and
# this is the number that answers him. It clears the row of pinned
# things people keep by the clock rather than sitting on top of them.
TOAST_BOTTOM_MARGIN = 96
TOAST_SIDE_MARGIN = 18


def toast_position(
    work_rect: tuple[int, int, int, int],
    toast_size: tuple[int, int],
    *,
    bottom_margin: int = TOAST_BOTTOM_MARGIN,
    side_margin: int = TOAST_SIDE_MARGIN,
) -> tuple[int, int]:
    """Bottom right of the usable area, lifted clear of the taskbar.

    Clamped to the work area on both axes: a margin taller than the
    screen must not push the toast off the top, which is what a laptop
    with a big display scale would otherwise do.
    """
    left, top, width, height = work_rect
    toast_width, toast_height = toast_size
    x = left + width - toast_width - max(0, side_margin)
    y = top + height - toast_height - max(0, bottom_margin)
    x = max(left, min(x, left + max(0, width - toast_width)))
    y = max(top, min(y, top + max(0, height - toast_height)))
    return x, y


def quick_chat_popup_position(
    target_rect: tuple[int, int, int, int],
    popup_size: tuple[int, int],
) -> tuple[int, int]:
    """Center a compact composer just above the usual caption area."""
    left, top, width, height = target_rect
    popup_width, popup_height = popup_size
    right = left + width
    bottom = top + height
    max_x = max(left, right - popup_width)
    max_y = max(top, bottom - popup_height)
    x = left + (width - popup_width) // 2
    y = top + int(height * 0.62) - popup_height
    return max(left, min(x, max_x)), max(top, min(y, max_y))


def move_toplevel(window: tk.Misc, x: int, y: int) -> None:
    """Place a popup at an absolute virtual-desktop coordinate."""
    window.geometry(f"+{max(x, 0)}+{max(y, 0)}")
    if sys.platform != "win32":
        return
    try:
        hwnd = window_handle(window)
        if not hwnd:
            return
        user32 = ctypes.windll.user32
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
        user32.SetWindowPos(hwnd, 0, x, y, 0, 0, 0x0001 | 0x0004)
    except Exception:
        pass


def clamp_display_value(text, low, high, cast):
    """Read a free-text overlay setting, clamped into its allowed range.

    A text box lets a member type anything. A number past the range is
    pulled to the nearest end - type 500 for the font and you get the
    largest it goes, type 30 lines and you get 20 - because the reach was
    unmistakable even when the number was not. Text that is not a number
    at all returns None, so the caller restores what was showing rather
    than guessing at a value the member never chose. The clamp matters
    more here than most places: whatever this returns is applied live to
    the only channel a deaf member has.
    """
    try:
        value = cast(str(text).strip())
    except (TypeError, ValueError, OverflowError):
        return None
    if value != value:  # NaN never compares equal to itself.
        return None
    return min(max(value, low), high)


def whole_number(text):
    """Read a count or a point size, accepting a typed decimal.

    Rounding 12.5 to 12 hands the member something they can see and
    correct; refusing it and snapping the box back to their old value
    tells them nothing about what was wrong.
    """
    return round(float(text))


def animations_enabled() -> bool:
    """Honor Windows' client-animation accessibility preference."""
    if sys.platform != "win32":
        return True
    try:
        enabled = wintypes.BOOL()
        # SPI_GETCLIENTAREAANIMATION
        ok = ctypes.windll.user32.SystemParametersInfoW(
            0x1042,
            0,
            ctypes.byref(enabled),
            0,
        )
        return bool(ok and enabled.value)
    except Exception:
        return True


def fade_in(window: tk.Misc, *, steps: int = 5, interval: int = 22) -> None:
    """One restrained entrance transition, skipped for reduced motion."""
    if not animations_enabled():
        return
    try:
        window.attributes("-alpha", 0.90)
    except tk.TclError:
        return

    def frame(index: int) -> None:
        try:
            window.attributes("-alpha", 0.90 + (0.10 * index / steps))
            if index < steps:
                window.after(interval, lambda: frame(index + 1))
        except tk.TclError:
            return

    window.after(interval, lambda: frame(1))


def hotkey_keycaps(spec: str) -> str:
    """Turn a canonical shortcut into calm, readable keycaps."""
    parts = [part.strip() for part in str(spec).split("+") if part.strip()]
    return "  ".join("[" + part + "]" for part in parts)


class StatusOrb(tk.Canvas):
    """A small text-adjacent status signal with one finite transition."""

    SIZE = 18

    def __init__(
        self,
        parent: tk.Misc,
        *,
        background: str,
        animate: bool = True,
    ) -> None:
        super().__init__(
            parent,
            width=self.SIZE,
            height=self.SIZE,
            bg=background,
            highlightthickness=0,
        )
        self.background = background
        self.animate = bool(animate)
        self.halo = self.create_oval(1, 1, 17, 17, outline=background, width=1)
        self.core = self.create_oval(6, 6, 12, 12, fill=MUTED, outline="")
        self._animation = None

    def show(self, color: str) -> None:
        self.itemconfig(self.core, fill=color)
        self.itemconfig(self.halo, outline=color)
        if self._animation is not None:
            try:
                self.after_cancel(self._animation)
            except tk.TclError:
                pass
        if not self.animate or not animations_enabled():
            self.itemconfig(self.halo, outline=self.background)
            return
        frames = (2, 1, 0)

        def pulse(index: int) -> None:
            if index >= len(frames):
                self.itemconfig(self.halo, outline=self.background)
                self._animation = None
                return
            inset = frames[index]
            self.coords(
                self.halo,
                1 + inset,
                1 + inset,
                17 - inset,
                17 - inset,
            )
            self._animation = self.after(45, lambda: pulse(index + 1))

        pulse(0)


class Toggle(tk.Checkbutton):
    """A themed native checkbox with an announced name and checked state."""

    def __init__(
        self,
        parent: tk.Misc,
        value: bool,
        command,
        *,
        text: str,
        font,
        background: str = PANEL_ALT,
    ) -> None:
        self.value = tk.BooleanVar(master=parent, value=bool(value))
        self.callback = command
        super().__init__(
            parent,
            text=text,
            variable=self.value,
            command=self._clicked,
            onvalue=True,
            offvalue=False,
            indicatoron=True,
            bg=background,
            fg=TEXT,
            activebackground=background,
            activeforeground=TEXT,
            selectcolor=PANEL,
            disabledforeground=SUBTLE,
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=background,
            highlightcolor=ACCENT,
            cursor="hand2",
            takefocus=True,
            anchor="w",
            padx=0,
            pady=0,
            font=font,
        )

    def _clicked(self) -> None:
        # The command reports what the world actually ended up as - a failed
        # write must not leave the checkbox claiming success.
        self.value.set(bool(self.callback(bool(self.value.get()))))


def nightwatch_scrollbar(parent: tk.Misc, *, command=None) -> ttk.Scrollbar:
    """Return a genuinely dark scrollbar instead of Windows' white classic one."""
    style = ttk.Style(parent)
    if "clam" in style.theme_names() and style.theme_use() != "clam":
        # This companion otherwise uses classic Tk widgets, so choosing the
        # colourable ttk theme affects only these scrollbars.
        style.theme_use("clam")
    name = "Nightwatch.Vertical.TScrollbar"
    style.configure(
        name,
        gripcount=0,
        background=BORDER_STRONG,
        darkcolor=BG,
        lightcolor=BORDER_STRONG,
        troughcolor=BG,
        bordercolor=BG,
        arrowcolor=MUTED,
        relief="flat",
        arrowsize=10,
    )
    style.map(
        name,
        background=[("pressed", ACCENT), ("active", BORDER_STRONG)],
        arrowcolor=[("active", TEXT)],
    )
    return ttk.Scrollbar(
        parent,
        orient="vertical",
        command=command,
        style=name,
        takefocus=True,
    )


class ScrollSurface(tk.Frame):
    """A dark responsive surface that only exposes its scrollbar when needed."""

    def __init__(self, parent: tk.Misc, *, background: str) -> None:
        super().__init__(parent, bg=background, highlightthickness=0)
        self.canvas = tk.Canvas(
            self,
            bg=background,
            borderwidth=0,
            highlightthickness=0,
            yscrollincrement=24,
        )
        self.scrollbar = nightwatch_scrollbar(
            self,
            command=self.canvas.yview,
        )
        self.canvas.configure(yscrollcommand=self._on_yview)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.content = tk.Frame(self.canvas, bg=background)
        self.content_window = self.canvas.create_window(
            (0, 0),
            window=self.content,
            anchor="nw",
        )
        self.content.bind("<Configure>", self._sync_region, add="+")
        self.canvas.bind("<Configure>", self._sync_width, add="+")
        toplevel = self.winfo_toplevel()
        toplevel.bind("<MouseWheel>", self._on_mousewheel, add="+")
        toplevel.bind("<FocusIn>", self._on_focus_in, add="+")
        toplevel.bind("<Prior>", self._on_scroll_key, add="+")
        toplevel.bind("<Next>", self._on_scroll_key, add="+")
        toplevel.bind("<Home>", self._on_scroll_key, add="+")
        toplevel.bind("<End>", self._on_scroll_key, add="+")

    def _on_yview(self, first: str, last: str) -> None:
        self.scrollbar.set(first, last)
        needs_scrollbar = float(first) > 0.0 or float(last) < 1.0
        if needs_scrollbar and not self.scrollbar.winfo_manager():
            self.scrollbar.pack(side="right", fill="y")
        elif not needs_scrollbar and self.scrollbar.winfo_manager():
            self.scrollbar.pack_forget()

    def _sync_region(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _sync_width(self, event) -> None:
        self.canvas.itemconfigure(self.content_window, width=max(int(event.width), 1))
        self._sync_region()

    def _on_mousewheel(self, event) -> str | None:
        try:
            event_widget = getattr(event, "widget", None)
            if isinstance(event_widget, (tk.Text, tk.Listbox)) or (
                isinstance(event_widget, ttk.Scrollbar)
                and event_widget is not self.scrollbar
            ):
                # Nested logs own their wheel while the pointer is over them.
                return None
            left = self.canvas.winfo_rootx()
            top = self.canvas.winfo_rooty()
            right = left + self.canvas.winfo_width()
            bottom = top + self.canvas.winfo_height()
            if not (left <= event.x_root < right and top <= event.y_root < bottom):
                return None
            first, last = self.canvas.yview()
            if first <= 0.0 and event.delta > 0:
                return None
            if last >= 1.0 and event.delta < 0:
                return None
            steps = -1 if event.delta > 0 else 1
            self.canvas.yview_scroll(steps * 3, "units")
            return "break"
        except (AttributeError, tk.TclError):
            return None

    def _contains(self, widget: tk.Widget | None) -> bool:
        while widget is not None:
            if widget is self.content:
                return True
            widget = getattr(widget, "master", None)
        return False

    def _on_focus_in(self, event) -> None:
        widget = getattr(event, "widget", None)
        if self._contains(widget):
            self.after_idle(lambda target=widget: self.reveal(target))

    def reveal(self, widget: tk.Widget) -> None:
        try:
            if not self._contains(widget):
                return
            total = max(self.content.winfo_reqheight(), 1)
            top = widget.winfo_rooty() - self.content.winfo_rooty()
            bottom = top + widget.winfo_height()
            view_top = self.canvas.canvasy(0)
            view_height = self.canvas.winfo_height()
            if top < view_top:
                self.canvas.yview_moveto(max(top - 12, 0) / total)
            elif bottom > view_top + view_height:
                destination = max(bottom - view_height + 12, 0)
                self.canvas.yview_moveto(destination / total)
        except (AttributeError, tk.TclError):
            return

    def _on_scroll_key(self, event) -> str | None:
        widget = getattr(event, "widget", None)
        if not self._contains(widget):
            return None
        key = str(getattr(event, "keysym", ""))
        if isinstance(widget, (tk.Text, tk.Listbox)) or (
            isinstance(widget, ttk.Scrollbar) and widget is not self.scrollbar
        ):
            return None
        if key in ("Home", "End") and isinstance(widget, tk.Entry):
            return None
        first, last = self.canvas.yview()
        if first <= 0.0 and key in ("Prior", "Home"):
            return None
        if last >= 1.0 and key in ("Next", "End"):
            return None
        if key == "Prior":
            self.canvas.yview_scroll(-1, "pages")
        elif key == "Next":
            self.canvas.yview_scroll(1, "pages")
        elif key == "Home":
            self.canvas.yview_moveto(0.0)
        elif key == "End":
            self.canvas.yview_moveto(1.0)
        else:
            return None
        return "break"

    def show_bottom(self) -> None:
        self.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self.canvas.yview_moveto(1.0)


def caption_target_key(target) -> tuple[str, str, int]:
    """Return the anchor-contract identity for one picker target.

    A window handle and process ID both die when a game closes. They are useful
    while enumerating a live window, but they must never decide whether the
    saved setting is selected. The shared overlay contract intentionally keeps
    only its mode, executable basename, and monitor index.
    """
    mode = getattr(target, "mode", "auto")
    if mode not in {"auto", "window", "monitor"}:
        mode = "auto"
    executable = str(getattr(target, "executable", "") or "").strip().lower()
    monitor_index = getattr(target, "monitor_index", 0)
    if isinstance(monitor_index, bool) or not isinstance(monitor_index, int):
        monitor_index = 0
    return mode, executable, monitor_index


def caption_target_summary(target) -> str:
    """Make a saved caption target understandable without re-enumerating."""
    mode, executable, monitor_index = caption_target_key(target)
    if mode == "window":
        name = Path(executable).stem if executable else "selected window"
        return "Window - " + name
    if mode == "monitor":
        return "Monitor " + str(monitor_index + 1)
    return "Auto - World of Warcraft"


class CaptionTargetMenu(tk.Toplevel):
    """A small local picker for the window or display captions follow."""

    def __init__(self, window: "CompanionWindow", anchor: tk.Widget) -> None:
        super().__init__(window.root, bg=window.theme.border)
        self.window = window
        self.anchor = anchor
        self.current_target = window.caption_target_choice()
        self.window_candidates: tuple = ()
        self.monitor_candidates: tuple = ()
        self.window_listing_failed = False
        self.monitor_listing_failed = False
        self.overrideredirect(True)
        self.transient(window.root)

        body = tk.Frame(self, bg=window.theme.panel, width=510)
        body.pack(fill="both", expand=True, padx=1, pady=1)

        header = tk.Frame(body, bg=window.theme.panel)
        header.pack(fill="x", padx=22, pady=(18, 12))
        heading = tk.Frame(header, bg=window.theme.panel)
        heading.pack(side="left", fill="x", expand=True)
        tk.Label(
            heading,
            text="Caption target",
            bg=window.theme.panel,
            fg=window.theme.text,
            anchor="w",
            font=window.fonts["panel_title"],
        ).pack(fill="x")
        tk.Label(
            heading,
            text="Choose where the caption overlay should appear.",
            bg=window.theme.panel,
            fg=window.theme.muted,
            anchor="w",
            font=window.fonts["meta"],
        ).pack(fill="x", pady=(2, 0))
        self.done_button = window.make_compact_button(header, "Done", self.dismiss)
        self.done_button.pack(side="right", padx=(8, 0))
        self.refresh_button = window.make_compact_button(
            header,
            "Refresh",
            self.refresh_choices,
        )
        self.refresh_button.pack(side="right")

        self.scroll = ScrollSurface(body, background=window.theme.panel)
        self.scroll.pack(fill="both", expand=True)
        self.content = self.scroll.content
        self.status = tk.Label(
            self.content,
            text="",
            bg=window.theme.panel,
            fg=window.theme.muted,
            anchor="w",
            justify="left",
            wraplength=455,
            font=window.fonts["meta"],
        )
        self.status.pack(fill="x", padx=22, pady=(0, 9))
        self.options = tk.Frame(self.content, bg=window.theme.panel)
        self.options.pack(fill="x", padx=22, pady=(0, 14))

        self.refresh_choices()
        self.update_idletasks()
        area = work_area(window.handle(), window.root)
        desired_height = (
            header.winfo_reqheight()
            + self.content.winfo_reqheight()
            + 4
        )
        popup_size = (
            max(self.content.winfo_reqwidth() + 2, 510),
            min(desired_height, max(area[3] - 24, 360)),
        )
        x, y = anchored_popup_position(
            (
                anchor.winfo_rootx(),
                anchor.winfo_rooty(),
                anchor.winfo_width(),
                anchor.winfo_height(),
            ),
            popup_size,
            area,
            gap=8,
        )
        self.geometry(f"{popup_size[0]}x{popup_size[1]}+{x}+{y}")
        self.bind("<Escape>", self.on_escape)
        if not own_popup_window(self, window.root):
            self.attributes("-topmost", True)
            self.after_idle(lambda: self.attributes("-topmost", False))
        self.lift()
        self.focus_force()
        if not window.preview:
            fade_in(self)

    def section_label(self, text: str) -> None:
        tk.Label(
            self.options,
            text=text,
            bg=self.window.theme.panel,
            fg=self.window.theme.muted,
            anchor="w",
            font=self.window.fonts["section"],
        ).pack(fill="x", pady=(9, 6))

    def option_row(self, title: str, hint: str, target) -> None:
        selected = caption_target_key(target) == caption_target_key(
            self.current_target
        )
        shell = tk.Frame(self.options, bg=ACCENT if selected else self.window.theme.border)
        shell.pack(fill="x", pady=(0, 6))
        row = tk.Frame(shell, bg=self.window.theme.panel_alt)
        row.pack(fill="x", padx=1, pady=1)
        copy = tk.Frame(row, bg=self.window.theme.panel_alt)
        copy.pack(side="left", fill="x", expand=True, padx=(12, 8), pady=9)
        tk.Label(
            copy,
            text=title,
            bg=self.window.theme.panel_alt,
            fg=self.window.theme.text,
            anchor="w",
            font=self.window.fonts["body_strong"],
        ).pack(fill="x")
        tk.Label(
            copy,
            text=hint,
            bg=self.window.theme.panel_alt,
            fg=self.window.theme.muted,
            anchor="w",
            justify="left",
            wraplength=290,
            font=self.window.fonts["meta"],
        ).pack(fill="x", pady=(2, 0))
        action = self.window.make_compact_button(
            row,
            "Selected" if selected else "Use this",
            lambda choice=target, label=title: self.select_target(choice, label),
            accent=selected,
        )
        action.pack(side="right", padx=(8, 12), pady=9)
        if selected:
            action.config(state="disabled")

    def empty_row(self, text: str, *, error: bool = False) -> None:
        tk.Label(
            self.options,
            text=text,
            bg=self.window.theme.panel_alt,
            fg=DANGER if error else self.window.theme.subtle,
            anchor="w",
            justify="left",
            wraplength=430,
            padx=12,
            pady=10,
            font=self.window.fonts["meta"],
        ).pack(fill="x", pady=(0, 6))

    def current_target_is_listed(self) -> bool:
        current = caption_target_key(self.current_target)
        if current[0] == "auto":
            return True
        candidates = (
            self.window_candidates
            if current[0] == "window"
            else self.monitor_candidates
        )
        return any(
            caption_target_key(getattr(candidate, "target", None)) == current
            for candidate in candidates
        )

    def rebuild_options(self) -> None:
        for child in self.options.winfo_children():
            child.destroy()

        self.section_label("Automatic")
        try:
            from VoiceCaptions.overlay import CaptionTargetChoice

            auto_target = CaptionTargetChoice()
        except ImportError:
            auto_target = self.current_target
        self.option_row(
            "Auto - World of Warcraft",
            "Follows the WoW game window whenever it is available.",
            auto_target,
        )

        self.section_label("Detected windows")
        if self.window_listing_failed:
            self.empty_row(
                "Could not list open windows. Refresh to try again.",
                error=True,
            )
        elif not self.window_candidates:
            self.empty_row(
                "No usable game or application windows found. Open one, then refresh.",
            )
        else:
            for candidate in self.window_candidates:
                target = getattr(candidate, "target", None)
                if target is None:
                    continue
                label = str(getattr(candidate, "label", "Open window"))
                self.option_row(
                    label,
                    "Caption cards follow this application window.",
                    target,
                )

        self.section_label("Monitors")
        if self.monitor_listing_failed:
            self.empty_row(
                "Could not list displays. Auto remains available.",
                error=True,
            )
        elif not self.monitor_candidates:
            self.empty_row("No monitors were reported by Windows.")
        else:
            for candidate in self.monitor_candidates:
                target = getattr(candidate, "target", None)
                if target is None:
                    continue
                label = str(getattr(candidate, "label", "Monitor"))
                self.option_row(
                    label,
                    "Pins caption cards to this display.",
                    target,
                )

    def refresh_choices(self) -> None:
        """Enumerate only the local desktop, never a game process or server."""
        self.refresh_button.config(state="disabled")
        self.status.config(text="Refreshing local windows and displays...", fg=self.window.theme.muted)
        self.update_idletasks()
        self.window_listing_failed = False
        self.monitor_listing_failed = False
        self.current_target = self.window.caption_target_choice()
        if self.window.preview:
            self.window_candidates = ()
            self.monitor_candidates = ()
            self.status.config(
                text="Preview only. Your installed app lists local windows and displays.",
                fg=self.window.theme.subtle,
            )
        else:
            try:
                from VoiceCaptions.overlay import enumerate_caption_windows

                self.window_candidates = tuple(enumerate_caption_windows())
            except (ImportError, OSError, RuntimeError, ValueError):
                self.window_candidates = ()
                self.window_listing_failed = True
            except Exception:
                # A window title is untrusted outside-process data. Keep a
                # broken enumeration local and give no raw process details
                # back to the UI.
                self.window_candidates = ()
                self.window_listing_failed = True
            try:
                from VoiceCaptions.overlay import enumerate_caption_monitors

                self.monitor_candidates = tuple(enumerate_caption_monitors())
            except (ImportError, OSError, RuntimeError, ValueError):
                self.monitor_candidates = ()
                self.monitor_listing_failed = True
            except Exception:
                self.monitor_candidates = ()
                self.monitor_listing_failed = True
            if self.window_listing_failed and self.monitor_listing_failed:
                self.status.config(
                    text="Could not refresh local targets. Auto still follows World of Warcraft.",
                    fg=DANGER,
                )
            elif not self.current_target_is_listed():
                self.status.config(
                    text=(
                        "Your selected target is not available right now. Captions "
                        "will use automatic game detection until it returns."
                    ),
                    fg=WARNING,
                )
            else:
                self.status.config(
                    text="Choose where the caption overlay should appear.",
                    fg=self.window.theme.muted,
                )
        self.rebuild_options()
        self.refresh_button.config(state="normal")

    def select_target(self, target, label: str) -> None:
        """Save one durable target identity and keep the previous one on error."""
        if not self.window.set_caption_target(target):
            self.status.config(
                text="Could not save this target. Your previous target is still active.",
                fg=DANGER,
            )
            return
        self.current_target = target
        self.window.refresh_caption_target_card()
        self.status.config(text=label + " is now your caption target.", fg=SUCCESS)
        self.rebuild_options()

    def on_escape(self, _event=None) -> str:
        self.dismiss()
        return "break"

    def dismiss(self) -> None:
        if getattr(self.window, "caption_target_menu", None) is self:
            self.window.caption_target_menu = None
        self.destroy()


class SpeakerMuteMenu(tk.Toplevel):
    """Choose which known voice speakers this device should hide."""

    def __init__(self, window: "CompanionWindow", anchor: tk.Widget) -> None:
        super().__init__(window.root, bg=window.theme.border)
        self.window = window
        self.anchor = anchor
        self.overrideredirect(True)
        self.transient(window.root)

        body = tk.Frame(self, bg=window.theme.panel, width=510)
        body.pack(fill="both", expand=True, padx=1, pady=1)
        header = tk.Frame(body, bg=window.theme.panel)
        header.pack(fill="x", padx=22, pady=(18, 12))
        heading = tk.Frame(header, bg=window.theme.panel)
        heading.pack(side="left", fill="x", expand=True)
        tk.Label(
            heading,
            text="Muted voices",
            bg=window.theme.panel,
            fg=window.theme.text,
            anchor="w",
            font=window.fonts["panel_title"],
        ).pack(fill="x")
        tk.Label(
            heading,
            text="Hide a speaker only on this computer.",
            bg=window.theme.panel,
            fg=window.theme.muted,
            anchor="w",
            font=window.fonts["meta"],
        ).pack(fill="x", pady=(2, 0))
        window.make_compact_button(header, "Done", self.dismiss).pack(
            side="right",
            padx=(12, 0),
        )

        self.scroll = ScrollSurface(body, background=window.theme.panel)
        self.scroll.pack(fill="both", expand=True)
        self.content = self.scroll.content
        self.status = tk.Label(
            self.content,
            text="",
            bg=window.theme.panel,
            fg=window.theme.muted,
            anchor="w",
            justify="left",
            wraplength=455,
            font=window.fonts["meta"],
        )
        self.status.pack(fill="x", padx=22, pady=(0, 9))
        self.options = tk.Frame(self.content, bg=window.theme.panel)
        self.options.pack(fill="x", padx=22, pady=(0, 14))
        self.rebuild()

        self.update_idletasks()
        area = work_area(window.handle(), window.root)
        desired_height = header.winfo_reqheight() + self.content.winfo_reqheight() + 4
        popup_size = (
            max(self.content.winfo_reqwidth() + 2, 510),
            min(desired_height, max(area[3] - 24, 360)),
        )
        x, y = anchored_popup_position(
            (
                anchor.winfo_rootx(),
                anchor.winfo_rooty(),
                anchor.winfo_width(),
                anchor.winfo_height(),
            ),
            popup_size,
            area,
            gap=8,
        )
        self.geometry(f"{popup_size[0]}x{popup_size[1]}+{x}+{y}")
        self.bind("<Escape>", self.on_escape)
        if not own_popup_window(self, window.root):
            self.attributes("-topmost", True)
            self.after_idle(lambda: self.attributes("-topmost", False))
        self.lift()
        self.focus_force()
        if not window.preview:
            fade_in(self)

    def rebuild(self) -> None:
        from VoiceCaptions.settings import DEFAULT_SETTINGS, load_settings
        from VoiceCaptions.speaker_directory import load_known_speakers

        for child in self.options.winfo_children():
            child.destroy()
        settings = DEFAULT_SETTINGS if self.window.preview else load_settings()
        muted = set(settings.muted_speaker_ids)
        speakers = () if self.window.preview else tuple(reversed(load_known_speakers()))
        self.status.config(
            text=(
                f"{len(muted)} voice{'s' if len(muted) != 1 else ''} muted. "
                "This does not change their consent or anyone else's captions."
                if muted
                else "No voices are muted on this computer."
            ),
            fg=WARNING if muted else self.window.theme.muted,
        )
        if not speakers:
            tk.Label(
                self.options,
                text=(
                    "Speakers appear here after their first caption. No audio or "
                    "caption text is stored for this list."
                ),
                bg=self.window.theme.panel_alt,
                fg=self.window.theme.subtle,
                anchor="w",
                justify="left",
                wraplength=430,
                padx=12,
                pady=11,
                font=self.window.fonts["meta"],
            ).pack(fill="x")
            return
        for speaker in speakers:
            selected = speaker.speaker_id in muted
            shell = tk.Frame(self.options, bg=WARNING if selected else self.window.theme.border)
            shell.pack(fill="x", pady=(0, 6))
            row = tk.Frame(shell, bg=self.window.theme.panel_alt)
            row.pack(fill="x", padx=1, pady=1)
            copy = tk.Frame(row, bg=self.window.theme.panel_alt)
            copy.pack(side="left", fill="x", expand=True, padx=(12, 8), pady=9)
            tk.Label(
                copy,
                text=speaker.display_name,
                bg=self.window.theme.panel_alt,
                fg=self.window.theme.text,
                anchor="w",
                font=self.window.fonts["body_strong"],
            ).pack(fill="x")
            tk.Label(
                copy,
                text="Hidden on this computer" if selected else "Voice captions visible",
                bg=self.window.theme.panel_alt,
                fg=WARNING if selected else self.window.theme.muted,
                anchor="w",
                font=self.window.fonts["meta"],
            ).pack(fill="x", pady=(2, 0))
            action = self.window.make_compact_button(
                row,
                "Unmute" if selected else "Mute",
                lambda speaker_id=speaker.speaker_id: self.toggle(speaker_id),
                accent=not selected,
            )
            action.pack(side="right", padx=(8, 12), pady=9)

    def toggle(self, speaker_id: str) -> None:
        if self.window.preview:
            return
        from VoiceCaptions.settings import load_settings, save_settings

        current = load_settings()
        muted = set(current.muted_speaker_ids)
        if speaker_id in muted:
            muted.remove(speaker_id)
        else:
            muted.add(speaker_id)
        updated = replace(current, muted_speaker_ids=tuple(sorted(muted)))
        if not save_settings(updated):
            self.status.config(
                text="Could not save that choice. Your previous mute list is unchanged.",
                fg=DANGER,
            )
            return
        self.window.refresh_speaker_mute_card()
        self.rebuild()

    def on_escape(self, _event=None) -> str:
        self.dismiss()
        return "break"

    def dismiss(self) -> None:
        if getattr(self.window, "speaker_mute_menu", None) is self:
            self.window.speaker_mute_menu = None
        self.destroy()


class ConnectGuildHelper(tk.Toplevel):
    """Everything about the guild connection, in one place, in the
    member's words.

    Shino, 2026-09-04: "bring out the Connect guild button and make it an
    all in one connect guild helper". Before this, Connect guild launched
    a separate pairing window with its own look, and the card it sat on
    explained pairing from the developer's side. This panel walks the
    member through it: where the invitation comes from, paste it (or
    take it off the clipboard when one is already there), confirm the
    guild by name, and what to do next once connected. It is also where
    a connected member changes guilds.

    The exchange runs on a worker thread so the window never freezes on
    the network; the one question that needs the member - "connect to
    THIS guild?" - is posted back to the Tk thread and answered inline.
    The invitation itself is cleared from the field the moment Connect
    is chosen and is never shown, logged or kept.
    """

    POLL_MS = 100
    CONFIRM_TIMEOUT_SECONDS = 300.0

    def __init__(
        self,
        window: "CompanionWindow",
        *,
        pair: Callable[[str, Callable[..., bool]], tuple[str, str]] | None = None,
        clipboard: Callable[[], str] | None = None,
    ) -> None:
        super().__init__(window.root, bg=window.theme.border)
        self.window = window
        self.pair = pair or pair_invitation
        self.clipboard = clipboard
        self.events: queue.Queue = queue.Queue()
        self.answer = False
        self.answer_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.busy = False
        self.closed = False
        self.declined = False
        self.poll_id: str | None = None
        self.connected_name = ""
        self.overrideredirect(True)
        self.transient(window.root)

        body = tk.Frame(self, bg=window.theme.panel, width=540)
        body.pack(fill="both", expand=True, padx=1, pady=1)

        header = tk.Frame(body, bg=window.theme.panel)
        header.pack(fill="x", padx=22, pady=(18, 10))
        heading = tk.Frame(header, bg=window.theme.panel)
        heading.pack(side="left", fill="x", expand=True)
        self.title_label = tk.Label(
            heading,
            text="Connect to your guild",
            bg=window.theme.panel,
            fg=window.theme.text,
            anchor="w",
            font=window.fonts["panel_title"],
        )
        self.title_label.pack(fill="x")
        self.status_label = tk.Label(
            heading,
            text="",
            bg=window.theme.panel,
            fg=window.theme.muted,
            anchor="w",
            justify="left",
            wraplength=440,
            font=window.fonts["meta"],
        )
        self.status_label.pack(fill="x", pady=(2, 0))
        self.close_button = window.make_compact_button(header, "Close", self.dismiss)
        self.close_button.pack(side="right", padx=(14, 0))

        self.section(body, "1. Get your invitation")
        self.invitation_copy = tk.Label(
            body,
            text=HOW_TO_GET_AN_INVITATION,
            bg=window.theme.panel,
            fg=window.theme.muted,
            anchor="w",
            justify="left",
            wraplength=480,
            font=window.fonts["meta"],
        )
        self.invitation_copy.pack(fill="x", padx=22, pady=(4, 0))

        self.section(body, "2. Paste it here")
        field_row = tk.Frame(body, bg=window.theme.panel)
        field_row.pack(fill="x", padx=22, pady=(4, 0))
        self.invitation = StyledEntry(
            field_row,
            placeholder="Your invitation link",
            font_scale=window.fonts,
        )
        self.invitation.pack(side="left", fill="x", expand=True)
        self.paste_button = window.make_compact_button(
            field_row, "Paste", self.paste_from_clipboard,
        )
        self.paste_button.pack(side="left", padx=(8, 0))
        tk.Label(
            body,
            text="Your invitation is used once and is never saved or shown anywhere.",
            bg=window.theme.panel,
            fg=window.theme.muted,
            anchor="w",
            justify="left",
            wraplength=480,
            font=window.fonts["meta"],
        ).pack(fill="x", padx=22, pady=(6, 0))

        self.section(body, "3. Connect")
        self.message = tk.Label(
            body,
            text="",
            bg=window.theme.panel,
            fg=window.theme.muted,
            anchor="w",
            justify="left",
            wraplength=480,
            font=window.fonts["meta"],
        )
        self.message.pack(fill="x", padx=22, pady=(4, 0))
        self.actions = tk.Frame(body, bg=window.theme.panel)
        self.actions.pack(fill="x", padx=22, pady=(8, 0))
        self.connect_button = window.make_compact_button(
            self.actions, "Connect", self.connect, accent=True,
        )
        self.connect_button.pack(side="left")

        # The one question that needs the member, asked where they are
        # looking rather than in a system dialog behind the panel.
        self.confirm_frame = tk.Frame(body, bg=window.theme.panel_alt)
        self.confirm_text = tk.Label(
            self.confirm_frame,
            text="",
            bg=window.theme.panel_alt,
            fg=window.theme.text,
            anchor="w",
            justify="left",
            wraplength=460,
            font=window.fonts["body_strong"],
        )
        self.confirm_text.pack(fill="x", padx=12, pady=(10, 6))
        confirm_actions = tk.Frame(self.confirm_frame, bg=window.theme.panel_alt)
        confirm_actions.pack(fill="x", padx=12, pady=(0, 10))
        self.confirm_yes = window.make_compact_button(
            confirm_actions, "Yes, connect", lambda: self.answer_confirm(True), accent=True,
        )
        self.confirm_yes.pack(side="left")
        self.confirm_no = window.make_compact_button(
            confirm_actions, "No, not this one", lambda: self.answer_confirm(False),
        )
        self.confirm_no.pack(side="left", padx=(8, 0))

        self.next_frame = tk.Frame(body, bg=window.theme.panel)
        tk.Label(
            self.next_frame,
            text=(
                "Next: turn your voice consent on in Discord, so your own "
                "words reach the captions too."
                if window.caption_suite
                else "Next: request a gear or talent check from your WoW addon. "
                "Downtime delivers the results when your guild server finishes."
            ),
            bg=window.theme.panel,
            fg=window.theme.muted,
            anchor="w",
            justify="left",
            wraplength=480,
            font=window.fonts["meta"],
        ).pack(fill="x")
        self.consent_button = None
        self.privacy_link = None
        if window.caption_suite:
            next_actions = tk.Frame(self.next_frame, bg=window.theme.panel)
            next_actions.pack(fill="x", pady=(8, 0))
            self.consent_button = window.make_compact_button(
                next_actions, "My voice consent", window.open_discord_consent,
            )
            self.consent_button.pack(side="left")
            self.privacy_link = tk.Label(
                self.next_frame,
                text="What your guild keeps - and what it never keeps",
                bg=window.theme.panel,
                fg=window.accent,
                anchor="w",
                cursor="hand2",
                takefocus=1,
                font=window.fonts["meta"],
            )
            self.privacy_link.pack(fill="x", pady=(8, 0))
            self.privacy_link.bind("<Button-1>", lambda _e: window.open_guild_privacy())
            self.privacy_link.bind("<Return>", self.open_privacy_by_key)

        tk.Frame(body, bg=window.theme.panel, height=18).pack(fill="x")

        self.bind("<Escape>", lambda _e: self.dismiss())
        # Return on the FIELD only. A Toplevel-wide binding sits in every
        # child's bindtags, and a focused button has no Return binding of
        # its own, so Return on Paste, Close or Yes would have started a
        # connection instead - with a prefilled invitation, one the member
        # never chose to send. This is an accessibility app; the keyboard
        # path has to be right.
        self.invitation.entry.bind("<Return>", lambda _e: self.connect())
        self.bind("<Destroy>", self.on_destroy)
        self.refresh_state()
        self.prefill_from_clipboard()
        self.place_over_window()
        self.invitation.focus_set()
        self.poll_id = self.after(self.POLL_MS, self.poll)

    # -- building --------------------------------------------------------

    def open_privacy_by_key(self, _event=None) -> str:
        self.window.open_guild_privacy()
        return "break"

    def on_destroy(self, event) -> None:
        # Fires for every child too; only the panel itself owns the timer.
        if getattr(event, "widget", None) is not self:
            return
        self.closed = True
        self.answer_event.set()
        self.cancel_poll()

    def cancel_poll(self) -> None:
        if self.poll_id is None:
            return
        try:
            self.after_cancel(self.poll_id)
        except tk.TclError:
            pass
        self.poll_id = None

    def section(self, parent: tk.Frame, text: str) -> None:
        tk.Label(
            parent,
            text=text,
            bg=self.window.theme.panel,
            fg=self.window.theme.text,
            anchor="w",
            font=self.window.fonts["body_strong"],
        ).pack(fill="x", padx=22, pady=(14, 0))

    def place_over_window(self) -> None:
        root = self.window.root
        try:
            self.update_idletasks()
            width = max(self.winfo_reqwidth(), 1)
            height = max(self.winfo_reqheight(), 1)
            x = root.winfo_rootx() + max(0, (root.winfo_width() - width) // 2)
            y = root.winfo_rooty() + max(0, (root.winfo_height() - height) // 3)
            self.geometry(f"+{x}+{y}")
        except tk.TclError:
            pass

    # -- state -----------------------------------------------------------

    def paired_name(self) -> str:
        if getattr(self.window, "preview", False):
            return self.connected_name
        return paired_guild_name()

    def refresh_state(self) -> None:
        """Say where the member stands, and show only what applies."""
        name = self.paired_name()
        if name:
            self.title_label.config(text="Your guild")
            self.status_label.config(text="Connected to " + name + ".", fg=SUCCESS)
            self.invitation_copy.config(text=HOW_TO_SWITCH_GUILDS)
            if not self.next_frame.winfo_manager():
                self.next_frame.pack(fill="x", padx=22, pady=(14, 0), after=self.actions)
        else:
            self.title_label.config(text="Connect to your guild")
            self.status_label.config(
                text="Not connected yet. Captions and Message Group start "
                "once you are.",
                fg=WARNING,
            )
            self.invitation_copy.config(text=HOW_TO_GET_AN_INVITATION)
            if self.next_frame.winfo_manager():
                self.next_frame.pack_forget()

    def say(self, text: str, color: str = MUTED) -> None:
        self.message.config(text=text, fg=color)

    # -- clipboard -------------------------------------------------------

    def read_clipboard(self) -> str:
        if self.clipboard is not None:
            try:
                return str(self.clipboard() or "")
            except Exception:  # noqa: BLE001
                return ""
        try:
            return str(self.window.root.clipboard_get() or "")
        except tk.TclError:
            return ""

    def prefill_from_clipboard(self) -> None:
        text = self.read_clipboard()
        if looks_like_invitation(text):
            self.invitation.set_text(text.strip())
            self.say(
                "We found an invitation on your clipboard. Choose Connect to use it.",
                self.window.accent,
            )

    def paste_from_clipboard(self) -> None:
        text = self.read_clipboard()
        if looks_like_invitation(text):
            self.invitation.set_text(text.strip())
            self.say("Invitation pasted. Choose Connect.", self.window.accent)
        else:
            self.say(
                "Copy your invitation link first, then choose Paste.",
                DANGER,
            )

    # -- connecting ------------------------------------------------------

    def connect(self) -> None:
        if self.busy:
            return
        invitation = self.invitation.get().strip()
        # Off the screen before anything else happens: an invitation is
        # one-use and private, and there is no reason to keep looking at
        # it once it has been handed over.
        self.invitation.set_text("")
        if not invitation:
            self.say("Paste your invitation first.", DANGER)
            return
        if not looks_like_invitation(invitation):
            self.say(NOT_AN_INVITATION, DANGER)
            return
        if getattr(self.window, "preview", False) and self.pair is pair_invitation:
            self.say("Preview only - nothing was sent to a guild server.", self.window.theme.muted)
            return
        self.busy = True
        self.declined = False
        self.connect_button.config(state="disabled", text="Connecting...")
        self.say("Checking your invitation with your guild's server...", self.window.theme.muted)
        self.worker = threading.Thread(
            target=self.run_pairing, args=(invitation,), daemon=True,
        )
        self.worker.start()

    def run_pairing(self, invitation: str) -> None:
        """Worker thread: the exchange, with the confirm posted back to Tk."""

        def confirm(credentials, existing_credentials) -> bool:
            self.answer = False
            self.answer_event.clear()
            # Checked AFTER the clear: a close that landed while the
            # exchange was still on the wire set the event before this
            # ran, and without this the wait below would sit out the whole
            # timeout asking a panel that no longer exists.
            if self.closed:
                return False
            self.events.put((
                "confirm",
                str(getattr(credentials, "guild_name", "")),
                urlsplit(str(getattr(credentials, "server_url", ""))).netloc,
                existing_credentials is not None,
            ))
            self.answer_event.wait(timeout=self.CONFIRM_TIMEOUT_SECONDS)
            return self.answer

        try:
            guild_name, _host = self.pair(invitation, confirm)
        except PairingFailed as error:
            self.events.put(("failed", str(error)))
        except Exception:  # noqa: BLE001 - never leak a library's own words
            self.events.put((
                "failed",
                CONNECTION_FAILED_GENERIC,
            ))
        else:
            # The main window refreshes on this signal even if the panel
            # was closed while the exchange was still running.
            notify_running_companion()
            self.events.put(("connected", str(guild_name)))
        finally:
            invitation = ""  # noqa: F841 - drop the reference on purpose

    def poll(self) -> None:
        self.poll_id = None
        if self.closed:
            return
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "confirm":
                    self.show_confirm(event[1], event[2], event[3])
                elif kind == "failed":
                    self.finish_failed(event[1])
                elif kind == "connected":
                    self.finish_connected(event[1])
        except queue.Empty:
            pass
        try:
            self.poll_id = self.after(self.POLL_MS, self.poll)
        except tk.TclError:
            self.poll_id = None

    def show_confirm(self, guild_name: str, host: str, replacing: bool) -> None:
        lines = ["Connect this computer to " + (guild_name or "this guild") + "?"]
        if host:
            lines.append("Guild server: " + host)
        if replacing:
            lines.append("This replaces the guild you are connected to now.")
        self.confirm_text.config(text="\n".join(lines))
        self.say("Your guild's server accepted the invitation.", self.window.theme.muted)
        if not self.confirm_frame.winfo_manager():
            self.confirm_frame.pack(fill="x", padx=22, pady=(10, 0), after=self.actions)

    def answer_confirm(self, yes: bool) -> None:
        self.answer = bool(yes)
        self.declined = not yes
        if self.confirm_frame.winfo_manager():
            self.confirm_frame.pack_forget()
        self.say(
            "Connecting..." if yes else "Not connecting to that guild.",
            self.window.theme.muted,
        )
        self.answer_event.set()

    def finish_failed(self, text: str) -> None:
        self.busy = False
        self.connect_button.config(state="normal", text="Connect")
        if self.confirm_frame.winfo_manager():
            self.confirm_frame.pack_forget()
        if self.declined:
            # The member said no. That is a choice, not a failure, and the
            # only thing they need to know is that the invitation is spent.
            self.declined = False
            self.say(INVITATION_DECLINED, WARNING)
            return
        self.say(text, DANGER)

    def finish_connected(self, guild_name: str) -> None:
        self.busy = False
        self.connected_name = guild_name
        self.connect_button.config(state="normal", text="Connect")
        if self.confirm_frame.winfo_manager():
            self.confirm_frame.pack_forget()
        self.refresh_state()
        self.say("Connected to " + guild_name + ".", SUCCESS)
        for name in ("refresh_pairing_identity", "reset_quick_chat_profile_for_pairing"):
            method = getattr(self.window, name, None)
            if method is not None:
                try:
                    method()
                except tk.TclError:
                    pass

    def dismiss(self) -> None:
        if self.busy:
            # Closing now would throw away an exchange the guild's server
            # may already have accepted - the invitation is one-use, and
            # a panel that vanished mid-way could never say so. The wait
            # is bounded by the network timeouts; the answer arrives here.
            self.say("Still checking with your guild's server - one moment.", WARNING)
            return
        self.closed = True
        # A worker still waiting on the confirm gets its answer: no.
        self.answer = False
        self.answer_event.set()
        self.cancel_poll()
        if getattr(self.window, "connect_guild_helper", None) is self:
            self.window.connect_guild_helper = None
        self.destroy()


class Toast(tk.Toplevel):
    """A small notice this app places itself.

    It replaces the Windows tray balloon for the messages the companion
    raises, because Windows owns a balloon's position and Shino needs
    this one higher - it was covering things by the clock. Everything
    else about it is deliberately modest: it appears bottom right above
    the taskbar, says one thing, closes itself, and never takes focus
    away from the game.
    """

    SECONDS = 6

    def __init__(self, window: "CompanionWindow", title: str, text: str) -> None:
        super().__init__(window.root, bg=window.theme.border)
        self.window = window
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        body = tk.Frame(self, bg=window.theme.panel)
        body.pack(fill="both", expand=True, padx=1, pady=1)
        rail = tk.Frame(body, bg=window.accent, width=3)
        rail.pack(side="left", fill="y")
        copy = tk.Frame(body, bg=window.theme.panel)
        copy.pack(side="left", fill="both", expand=True, padx=14, pady=12)
        tk.Label(
            copy,
            text=title,
            bg=window.theme.panel,
            fg=window.theme.text,
            anchor="w",
            justify="left",
            font=window.fonts["body_strong"],
        ).pack(fill="x")
        tk.Label(
            copy,
            text=text,
            bg=window.theme.panel,
            fg=window.theme.muted,
            anchor="w",
            justify="left",
            wraplength=300,
            font=window.fonts["meta"],
        ).pack(fill="x", pady=(3, 0))
        for widget in (self, body, copy, *copy.winfo_children()):
            widget.bind("<Button-1>", lambda _event: self.dismiss())
        self.place_over_tray()
        self.after(int(self.SECONDS * 1000), self.dismiss)

    def place_over_tray(self) -> None:
        try:
            self.update_idletasks()
            size = (max(self.winfo_reqwidth(), 1), max(self.winfo_reqheight(), 1))
            hwnd = int(getattr(self.window, "hwnd", 0) or 0)
            x, y = toast_position(work_area(hwnd, self.window.root), size)
            self.geometry(f"+{x}+{y}")
        except tk.TclError:
            pass

    def dismiss(self) -> None:
        if getattr(self.window, "toast", None) is self:
            self.window.toast = None
        try:
            self.destroy()
        except tk.TclError:
            pass


class SettingsMenu(tk.Toplevel):
    """A stable preferences panel, grouped by what the player controls."""

    def __init__(self, window: "CompanionWindow", anchor: tk.Widget) -> None:
        super().__init__(window.root, bg=window.theme.border)
        self.window = window
        # Every typed field's commit, so closing the panel can flush
        # them. They commit on <Return> and <FocusOut>, and clicking
        # Done is neither.
        self.entry_commits: list = []
        self.capture_active = False
        self.capture_modifiers: set[str] = set()
        self.capture_previous = ""
        self.message_name_field: tk.StringVar | None = None
        self.message_name_entry: tk.Entry | None = None
        self.message_name_status: tk.Label | None = None
        self.message_name_save: tk.Button | None = None
        self.message_name_baseline: str | None = None
        self.overrideredirect(True)
        self.transient(window.root)

        body = tk.Frame(self, bg=window.theme.panel, width=510)
        body.pack(fill="both", expand=True, padx=1, pady=1)

        header = tk.Frame(body, bg=window.theme.panel)
        header.pack(fill="x", padx=22, pady=(18, 12))
        heading = tk.Frame(header, bg=window.theme.panel)
        heading.pack(side="left", fill="x", expand=True)
        tk.Label(
            heading,
            text="Preferences",
            bg=window.theme.panel,
            fg=window.theme.text,
            anchor="w",
            font=window.fonts["panel_title"],
        ).pack(fill="x")
        tk.Label(
            heading,
            text="Changes save automatically.",
            bg=window.theme.panel,
            fg=window.theme.muted,
            anchor="w",
            font=window.fonts["meta"],
        ).pack(fill="x", pady=(2, 0))
        # Which build this is, in the one panel somebody opens when they
        # are trying to work out why the app is behaving as it is. Three
        # installs live on a normal machine - full, member, and an older
        # member copy - and nothing on screen said which you had open or
        # how old it was. Answering that took reading CLIENT_VERSION out
        # of three different client.py files.
        tk.Label(
            heading,
            text=window.build_line(),
            bg=window.theme.panel,
            fg=window.theme.muted,
            anchor="w",
            font=window.fonts["meta"],
        ).pack(fill="x", pady=(2, 0))
        window.make_compact_button(header, "Done", self.dismiss).pack(
            side="right",
            padx=(14, 0),
        )

        self.preferences_scroll = ScrollSurface(body, background=window.theme.panel)
        self.preferences_scroll.pack(fill="both", expand=True)
        content = self.preferences_scroll.content

        self.section_label(content, "App")
        app_card = self.card(content)
        self.row(
            app_card,
            "Open at sign-in",
            "Start the companion after you sign in to Windows.",
            False if window.preview else autostart_enabled(),
            self.on_autostart,
        )
        self.divider(app_card)
        self.row(
            app_card,
            "Keep running when closed",
            window.tray_note(),
            window.settings["minimize_to_taskbar"],
            self.on_minimize,
        )

        if window.caption_suite:
            from VoiceCaptions.settings import (
                DEFAULT_SETTINGS as DEFAULT_CAPTION_SETTINGS,
                MIN_CAPTION_FONT_SIZE,
                MAX_CAPTION_FONT_SIZE,
                MIN_CAPTION_HOLD_SECONDS,
                MAX_CAPTION_HOLD_SECONDS,
                MIN_VISIBLE_SPEAKERS,
                MAX_VISIBLE_SPEAKERS,
                load_settings as load_caption_settings,
            )

            caption_settings = (
                DEFAULT_CAPTION_SETTINGS
                if window.preview
                else load_caption_settings()
            )
            self.section_label(content, "Captions")
            caption_card = self.card(content)
            self.entry_row(
                caption_card,
                "Caption duration",
                "How long finished captions remain on screen.",
                caption_settings.caption_hold_seconds,
                MIN_CAPTION_HOLD_SECONDS,
                MAX_CAPTION_HOLD_SECONDS,
                float,
                lambda value: f"{value:g}",
                "sec",
                window.set_caption_hold_seconds,
            )
            self.divider(caption_card)
            self.entry_row(
                caption_card,
                "Speakers on screen",
                "Maximum caption cards visible at once.",
                caption_settings.max_visible_speakers,
                MIN_VISIBLE_SPEAKERS,
                MAX_VISIBLE_SPEAKERS,
                whole_number,
                lambda value: str(value),
                "max",
                window.set_max_visible_speakers,
            )
            self.divider(caption_card)
            self.entry_row(
                caption_card,
                "Caption text size",
                "Text size inside each caption card.",
                caption_settings.caption_font_size,
                MIN_CAPTION_FONT_SIZE,
                MAX_CAPTION_FONT_SIZE,
                whole_number,
                lambda value: str(value),
                "pt",
                window.set_caption_font_size,
            )

            if window.caption_suite:
                self.section_label(content, "Message Group")
                chat_card = self.card(content)
                self.message_name_row(chat_card)
                self.divider(chat_card)
                self.hotkey_row(chat_card)
                self.after_idle(self.begin_message_name_load)

            tk.Label(
                content,
                text="Tip: test your shortcut in game before the next pull.",
                bg=window.theme.panel,
                fg=window.theme.subtle,
                anchor="w",
                font=window.fonts["meta"],
            ).pack(fill="x", padx=22, pady=(12, 18))

        self.update_idletasks()
        area = work_area(window.handle(), window.root)
        desired_height = (
            header.winfo_reqheight()
            + content.winfo_reqheight()
            + 4
        )
        popup_size = (
            max(content.winfo_reqwidth() + 2, 510),
            min(desired_height, max(area[3] - 24, 360)),
        )
        x, y = anchored_popup_position(
            (
                anchor.winfo_rootx(),
                anchor.winfo_rooty(),
                anchor.winfo_width(),
                anchor.winfo_height(),
            ),
            popup_size,
            area,
            gap=8,
        )
        self.geometry(f"{popup_size[0]}x{popup_size[1]}+{x}+{y}")
        self.bind("<Escape>", self.on_escape)
        self.bind("<KeyPress>", self.on_hotkey_keypress, add="+")
        self.bind("<KeyRelease>", self.on_hotkey_keyrelease, add="+")
        self.bind("<FocusOut>", self.on_focus_out, add="+")
        if not own_popup_window(self, window.root):
            # Compatibility fallback for a platform where explicit ownership
            # is unavailable. The pulse is cleared immediately so Preferences
            # cannot remain above another application after Alt-Tab.
            self.attributes("-topmost", True)
            self.after_idle(lambda: self.attributes("-topmost", False))
        self.lift()
        self.focus_force()
        if not window.preview:
            fade_in(self)

    @staticmethod
    def contains_widget(container: tk.Widget, widget: tk.Widget | None) -> bool:
        while widget is not None:
            if widget is container:
                return True
            widget = getattr(widget, "master", None)
        return False

    def section_label(self, parent: tk.Widget, text: str) -> None:
        tk.Label(
            parent,
            text=text,
            bg=self.window.theme.panel,
            fg=self.window.theme.muted,
            anchor="w",
            font=self.window.fonts["section"],
        ).pack(fill="x", padx=22, pady=(9, 6))

    def card(self, parent: tk.Widget) -> tk.Frame:
        shell = tk.Frame(parent, bg=self.window.theme.border)
        shell.pack(fill="x", padx=22)
        card = tk.Frame(shell, bg=self.window.theme.panel_alt)
        card.pack(fill="x", padx=1, pady=1)
        return card

    def divider(self, parent: tk.Widget) -> None:
        tk.Frame(parent, bg=self.window.theme.border, height=1).pack(fill="x", padx=16)

    def row(
        self,
        parent: tk.Widget,
        title: str,
        hint: str,
        value: bool,
        command,
    ) -> None:
        row = tk.Frame(parent, bg=self.window.theme.panel_alt)
        row.pack(fill="x", padx=16, pady=11)
        copy = tk.Frame(row, bg=self.window.theme.panel_alt)
        copy.pack(side="left", fill="x", expand=True)
        Toggle(
            copy,
            value,
            command,
            text=title,
            background=self.window.theme.panel_alt,
            font=self.window.fonts["body_strong"],
        ).pack(fill="x")
        tk.Label(
            copy,
            text=hint,
            bg=self.window.theme.panel_alt,
            fg=self.window.theme.muted,
            anchor="w",
            justify="left",
            wraplength=340,
            font=self.window.fonts["meta"],
        ).pack(fill="x", pady=(2, 0))

    def entry_row(
        self,
        parent: tk.Widget,
        title: str,
        hint: str,
        value,
        low,
        high,
        cast,
        formatter,
        suffix: str,
        command,
    ) -> None:
        current = value
        row = tk.Frame(parent, bg=self.window.theme.panel_alt)
        row.pack(fill="x", padx=16, pady=11)
        copy = tk.Frame(row, bg=self.window.theme.panel_alt)
        copy.pack(side="left", fill="x", expand=True)
        tk.Label(
            copy,
            text=title,
            bg=self.window.theme.panel_alt,
            fg=self.window.theme.text,
            anchor="w",
            font=self.window.fonts["body_strong"],
        ).pack(fill="x")
        tk.Label(
            copy,
            text=hint,
            bg=self.window.theme.panel_alt,
            fg=self.window.theme.muted,
            anchor="w",
            justify="left",
            wraplength=320,
            font=self.window.fonts["meta"],
        ).pack(fill="x", pady=(2, 0))

        controls = tk.Frame(row, bg=self.window.theme.panel_alt)
        controls.pack(side="right", padx=(16, 0))
        field = tk.StringVar(value=formatter(current))
        entry = tk.Entry(
            controls,
            textvariable=field,
            width=5,
            justify="center",
            bg=self.window.theme.panel,
            fg=self.window.theme.text,
            insertbackground=self.window.theme.text,
            relief="flat",
            highlightthickness=1,
            highlightbackground=self.window.theme.border,
            highlightcolor=ACCENT,
            font=self.window.fonts["keycap"],
        )
        entry.pack(side="left", ipady=5)
        tk.Label(
            controls,
            text=suffix,
            bg=self.window.theme.panel_alt,
            fg=self.window.theme.subtle,
            font=self.window.fonts["meta"],
        ).pack(side="left", padx=(6, 0))

        def commit(_event=None) -> None:
            nonlocal current
            parsed = clamp_display_value(field.get(), low, high, cast)
            if parsed is None:
                field.set(formatter(current))
                return
            current = command(parsed)
            field.set(formatter(current))

        entry.bind("<Return>", commit)
        entry.bind("<FocusOut>", commit)
        self.entry_commits.append(commit)

    def message_name_row(self, parent: tk.Widget) -> None:
        row = tk.Frame(parent, bg=self.window.theme.panel_alt)
        row.pack(fill="x", padx=16, pady=12)
        copy = tk.Frame(row, bg=self.window.theme.panel_alt)
        copy.pack(side="left", fill="x", expand=True)
        tk.Label(
            copy,
            text="Your message name",
            bg=self.window.theme.panel_alt,
            fg=self.window.theme.text,
            anchor="w",
            font=self.window.fonts["body_strong"],
        ).pack(fill="x")
        tk.Label(
            copy,
            text=(
                "This affects typed guild messages only, not voice captions "
                "or consent."
            ),
            bg=self.window.theme.panel_alt,
            fg=self.window.theme.muted,
            anchor="w",
            justify="left",
            wraplength=190,
            font=self.window.fonts["meta"],
        ).pack(fill="x", pady=(2, 0))
        self.message_name_status = tk.Label(
            copy,
            text="Loading from your guild...",
            bg=self.window.theme.panel_alt,
            fg=self.window.theme.subtle,
            anchor="w",
            justify="left",
            wraplength=190,
            font=self.window.fonts["meta"],
        )
        self.message_name_status.pack(fill="x", pady=(3, 0))

        controls = tk.Frame(row, bg=self.window.theme.panel_alt)
        controls.pack(side="right", padx=(14, 0))
        current = (
            getattr(self.window, "quick_chat_profile_pending_name", None)
            if getattr(
                self.window,
                "quick_chat_profile_pending_operation",
                "",
            ) == "save"
            else getattr(self.window, "quick_chat_profile_name", None)
            if getattr(self.window, "quick_chat_profile_loaded", False)
            else None
        )
        self.message_name_baseline = current
        self.message_name_field = tk.StringVar(value=current or "")
        self.message_name_entry = tk.Entry(
            controls,
            textvariable=self.message_name_field,
            width=14,
            bg=self.window.theme.panel,
            fg=self.window.theme.text,
            insertbackground=self.window.theme.text,
            relief="flat",
            highlightthickness=1,
            highlightbackground=self.window.theme.border,
            highlightcolor=self.window.theme.accent,
            font=self.window.fonts["body"],
        )
        self.message_name_entry.pack(side="left", ipady=5)
        self.message_name_save = self.window.make_compact_button(
            controls,
            "Save name",
            lambda: self.commit_message_name(force=True),
        )
        self.message_name_save.pack(side="left", padx=(8, 0))
        self.message_name_entry.bind(
            "<Return>",
            lambda event: self.commit_message_name(event, force=True),
        )
        self.message_name_entry.bind("<FocusOut>", self.commit_message_name)
        self.entry_commits.append(self.commit_message_name)
        self.set_message_name_busy(True)

    def begin_message_name_load(self) -> None:
        if self.message_name_entry is None:
            return
        self.window.request_quick_chat_profile(self)

    def set_message_name_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        if self.message_name_entry is not None:
            self.message_name_entry.config(state=state)
        if self.message_name_save is not None:
            self.message_name_save.config(state=state)

    def show_message_name_loading(self) -> None:
        self.set_message_name_busy(True)
        if self.message_name_status is not None:
            self.message_name_status.config(
                text="Loading from your guild...",
                fg=self.window.theme.subtle,
            )

    def show_message_name_saving(self) -> None:
        self.set_message_name_busy(True)
        if self.message_name_status is not None:
            self.message_name_status.config(
                text="Saving to your guild...",
                fg=self.window.theme.accent,
            )

    def show_message_name_ready(
        self,
        name: str | None,
        *,
        saved: bool = False,
        preview: bool = False,
    ) -> None:
        if self.message_name_field is not None:
            self.message_name_field.set(name or "")
        self.message_name_baseline = name
        self.set_message_name_busy(False)
        if self.message_name_status is None:
            return
        if preview:
            text = "Preview only - no name was loaded or saved."
            color = self.window.theme.subtle
        elif saved:
            text = "Saved to your guild."
            color = SUCCESS
        elif name:
            text = "Ready. This name is stored by your guild."
            color = SUCCESS
        else:
            text = "No message name is set yet."
            color = WARNING
        self.message_name_status.config(text=text, fg=color)

    def show_message_name_failure(self, message: str) -> None:
        self.set_message_name_busy(False)
        if self.message_name_status is not None:
            self.message_name_status.config(text=message, fg=DANGER)

    def commit_message_name(self, _event=None, *, force: bool = False) -> bool:
        field = self.message_name_field
        if field is None:
            return True
        raw = field.get()
        pending = getattr(
            self.window,
            "quick_chat_profile_pending_operation",
            "",
        )
        if pending == "save":
            self.show_message_name_saving()
            return True
        if (
            pending == "load"
            and not raw.strip()
            and not getattr(self.window, "quick_chat_profile_loaded", False)
        ):
            return True
        if not raw.strip() and not self.message_name_baseline:
            return True

        clean = canonicalize_quick_chat_name(raw)
        if clean is None:
            self.show_message_name_failure(
                "Use 1-32 characters without control or direction-changing "
                "characters."
            )
            return False
        if not force and clean == self.message_name_baseline:
            return True
        if getattr(self.window, "preview", False):
            self.show_message_name_ready(clean, preview=True)
            return True
        if (
            not force
            and getattr(self.window, "quick_chat_profile_loaded", False)
            and clean == getattr(self.window, "quick_chat_profile_name", None)
        ):
            self.show_message_name_ready(clean)
            return True

        started, message = self.window.save_quick_chat_profile_name(clean, self)
        if not started:
            self.show_message_name_failure(message)
            return False
        return True

    def hotkey_row(self, parent: tk.Widget) -> None:
        row = tk.Frame(parent, bg=self.window.theme.panel_alt)
        row.pack(fill="x", padx=16, pady=12)
        copy = tk.Frame(row, bg=self.window.theme.panel_alt)
        copy.pack(side="left", fill="x", expand=True)
        tk.Label(
            copy,
            text="Message Group shortcut",
            bg=self.window.theme.panel_alt,
            fg=self.window.theme.text,
            anchor="w",
            font=self.window.fonts["body_strong"],
        ).pack(fill="x")
        self.hotkey_help = tk.Label(
            copy,
            text="Opens the compact message box from anywhere.",
            bg=self.window.theme.panel_alt,
            fg=self.window.theme.muted,
            anchor="w",
            justify="left",
            wraplength=250 if self.window.member_edition else 300,
            font=self.window.fonts["meta"],
        )
        self.hotkey_help.pack(fill="x", pady=(2, 0))

        controls = tk.Frame(row, bg=self.window.theme.panel_alt)
        controls.pack(side="right", padx=(14, 0))
        self.hotkey_value = tk.Label(
            controls,
            text=hotkey_keycaps(self.window.settings.get("speak_hotkey", "")),
            bg=self.window.theme.panel,
            fg=self.window.theme.text,
            padx=9,
            pady=5,
            font=self.window.fonts["keycap"],
        )
        self.hotkey_value.pack(side="left")
        self.hotkey_change = self.window.make_compact_button(
            controls,
            "Change",
            self.begin_hotkey_capture,
        )
        self.hotkey_change.pack(side="left", padx=(8, 0))
        # Reserve the actual action/keycap width before giving the remaining
        # space to help text. Filled/rounded buttons include their focus ring.
        controls.pack_configure(before=copy)

    @staticmethod
    def event_modifiers(state: int) -> set[str]:
        modifiers = set()
        if state & 0x0004:
            modifiers.add("Ctrl")
        if state & 0x0008:
            modifiers.add("Alt")
        if state & 0x0001:
            modifiers.add("Shift")
        return modifiers

    def begin_hotkey_capture(self) -> None:
        if self.capture_active:
            self.cancel_hotkey_capture()
            return
        self.capture_active = True
        self.capture_modifiers.clear()
        self.capture_previous = str(
            self.window.settings.get("speak_hotkey", "")
        )
        self.window.tray.set_hotkey_suppressed(True)
        self.hotkey_change.config(text="Cancel")
        self.hotkey_value.config(text="Press shortcut", fg=self.window.theme.accent)
        self.hotkey_help.config(
            text=(
                "Hold Ctrl, Alt, or Win with a letter, number, or F-key. "
                "Escape cancels; Backspace restores the default."
            ),
            fg=self.window.theme.muted,
        )
        self.focus_force()

    def focus_is_text_field(self) -> bool:
        """True when this keystroke belongs to a field, not to capture.

        The capture handlers are bound on the Toplevel, so they see keys
        typed into the caption number boxes as well. Two real
        consequences before this existed: Ctrl+A to select a box's
        contents registered Ctrl+A as a system-wide hotkey, and Backspace
        to delete a digit reset the shortcut to its default.

        Focus leaving the window is already handled elsewhere; this is
        the case where focus is still inside the panel but belongs to
        something that legitimately wants the key.
        """
        try:
            focused = self.focus_get()
        except tk.TclError:
            return False
        if focused is None:
            return False
        try:
            widget_class = focused.winfo_class()
        except (tk.TclError, AttributeError):
            return False
        # By Tk class name rather than isinstance: a ttk.Entry is not a
        # tk.Entry subclass, so isinstance would wave ttk widgets through
        # and reintroduce the bug the day one is used here.
        return widget_class in TEXT_ENTRY_WIDGET_CLASSES

    def on_hotkey_keypress(self, event) -> str | None:
        if not self.capture_active:
            return None
        if self.focus_is_text_field():
            # Let the box have its own keystroke.
            return None
        keysym = str(getattr(event, "keysym", ""))
        if keysym == "Escape":
            self.cancel_hotkey_capture()
            return "break"
        if keysym == "BackSpace":
            self.apply_hotkey(hotkey_support()[0])
            return "break"
        modifier = HOTKEY_MODIFIER_KEYSYMS.get(keysym)
        if modifier is not None:
            self.capture_modifiers.add(modifier)
            self.hotkey_value.config(
                text=hotkey_keycaps("+".join(
                    name
                    for name in HOTKEY_MODIFIER_ORDER
                    if name in self.capture_modifiers
                )) or "Press shortcut"
            )
            return "break"

        modifiers = self.capture_modifiers | self.event_modifiers(
            int(getattr(event, "state", 0) or 0)
        )
        candidate = captured_hotkey_spec(
            modifiers,
            keysym,
            getattr(event, "keycode", None),
        )
        if candidate is None:
            self.hotkey_help.config(
                text=(
                    "Use Ctrl, Alt, or Win with a letter, number, or F-key. "
                    "Shift by itself is reserved for normal typing."
                ),
                fg=WARNING,
            )
            return "break"
        self.apply_hotkey(candidate)
        return "break"

    def on_hotkey_keyrelease(self, event) -> str | None:
        if not self.capture_active:
            return None
        if self.focus_is_text_field():
            return None
        modifier = HOTKEY_MODIFIER_KEYSYMS.get(
            str(getattr(event, "keysym", ""))
        )
        if modifier is not None:
            self.capture_modifiers.discard(modifier)
            return "break"
        return None

    def apply_hotkey(self, candidate: str) -> None:
        success, message = self.window.set_speak_hotkey(candidate)
        if not success:
            self.hotkey_help.config(text=message, fg=DANGER)
            self.hotkey_value.config(text="Try another", fg=self.window.theme.accent)
            self.focus_force()
            return
        self.capture_active = False
        self.capture_modifiers.clear()
        self.window.tray.set_hotkey_suppressed(False)
        self.hotkey_change.config(text="Change")
        self.hotkey_value.config(
            text=hotkey_keycaps(candidate),
            fg=self.window.theme.text,
        )
        self.hotkey_help.config(
            text=message,
            fg=WARNING if "could not be saved" in message else SUCCESS,
        )

    def cancel_hotkey_capture(self) -> None:
        if not self.capture_active:
            return
        self.capture_active = False
        self.capture_modifiers.clear()
        self.window.tray.set_hotkey_suppressed(False)
        self.hotkey_change.config(text="Change")
        self.hotkey_value.config(
            text=hotkey_keycaps(self.capture_previous),
            fg=self.window.theme.text,
        )
        self.hotkey_help.config(
            text="Shortcut unchanged.",
            fg=self.window.theme.muted,
        )

    def flush_typed_values(self) -> bool:
        """Commit anything still sitting in an entry box.

        The header promises "Changes save automatically." - so a
        number the member typed and can still see on screen has to
        be saved when the panel closes, not thrown away. Clicking
        Done fires neither <Return> nor, reliably, <FocusOut>.

        Each commit re-reads and re-clamps its own field, so running
        one that already committed is a no-op rather than a second
        write.
        """
        valid = True
        for commit in self.entry_commits:
            try:
                if commit() is False:
                    valid = False
            except tk.TclError:
                # The widget is already gone; nothing left to read.
                continue
        return valid

    def on_escape(self, _event=None) -> str:
        if self.capture_active:
            self.cancel_hotkey_capture()
        else:
            # Escape is the one explicit discard path. Done and the gear keep
            # the panel open on an invalid name so the promised auto-save
            # cannot silently throw an edit away.
            self.dismiss(commit=False)
        return "break"

    def on_focus_out(self, _event=None) -> None:
        if self.capture_active:
            self.after_idle(self.cancel_capture_if_focus_left)

    def cancel_capture_if_focus_left(self) -> None:
        if not self.capture_active:
            return
        try:
            focused = self.focus_get()
        except tk.TclError:
            focused = None
        if not self.contains_widget(self, focused):
            self.cancel_hotkey_capture()

    def on_autostart(self, value: bool) -> bool:
        if self.window.preview:
            return bool(value)
        return set_autostart(value)

    def on_minimize(self, value: bool) -> bool:
        return self.window.set_minimize_to_taskbar(value)

    def dismiss(self, *, commit: bool = True) -> None:
        if self.capture_active:
            self.cancel_hotkey_capture()
        if commit and not self.flush_typed_values():
            return
        self.window.menu = None
        self.window.menu_closed_at = time.monotonic()
        self.destroy()


# --------------------------------------------------------------------------
# Window
# --------------------------------------------------------------------------

class CompanionWindow:
    EDGE = 5
    CORNER = 14
    TITLEBAR = 40

    # Class-level so a window built without __init__ - the tests do that,
    # and so does the preview harness - still answers these truthfully.
    # Voice capabilities belong to the standalone Vemal's Ears app.
    theme = get_theme_for_edition(FULL_EDITION)
    caption_suite = False
    captions_page = False
    tab_bar = None
    connect_guild_helper = None
    hero_connect_button = None
    toast = None
    captions_surface = None
    captions_page_frame = None
    captions_status_label = None

    def __init__(
        self,
        root: tk.Tk,
        *,
        edition: str | None = None,
        member_edition: bool | None = None,
        preview: bool = False,
        result_carrier_available: bool = True,
    ) -> None:
        self.root = root
        self.preview = bool(preview)
        self.result_carrier_available = bool(result_carrier_available)
        if edition is not None and member_edition is not None:
            raise ValueError("choose edition or member_edition, not both")
        selected = (
            resolve_edition()
            if edition is None and member_edition is None
            else normalize_edition(
                member_edition if member_edition is not None else edition
            )
        )
        self.edition = selected
        self.theme = get_theme_for_edition(selected)
        self.accent = self.theme.accent
        self.accent_hover = self.theme.accent_hover
        # Kept as a compatibility attribute for code that customizes the
        # existing member shell. New behavior is selected by explicit edition
        # and service capabilities, not by stretching this bool further.
        self.member_edition = selected == MEMBER_EDITION
        self.hosted_edition = selected == HOSTED_EDITION
        self.managed_edition = self.member_edition or self.hosted_edition
        self.caption_suite = self.member_edition
        self.captions_page = False
        # The full local host is "Downtime" as well. The app was renamed
        # away from "WoW Companion" wholesale, so the edition split decides
        # which SERVICES run, not whether the old name comes back.
        # edition_product_name understands all three editions; the
        # member-or-not form it replaces could not name Downtime Lite.
        # (Mainline also assigned product_name twice in a row here, the
        # first immediately overwritten - that dead line goes with it.)
        self.product_name = edition_product_name(self.edition)
        self.has_operator_console = operator_console_available(self.member_edition)
        service_specs = service_specs_for_edition(self.edition)
        self.services = [Service(spec=spec, window=self) for spec in service_specs]
        self.settings = (
            {
                # The preview draws the switch, so it shows the same
                # default a real install of THIS edition would start with.
                "minimize_to_taskbar": default_minimize_to_taskbar(
                    self.member_edition
                ),
                "speak_hotkey": hotkey_support()[0],
            }
            if self.preview
            else load_settings(self.member_edition)
        )
        # Managed player editions build these; the full local host controls
        # the whole service instead, which is a different and louder action.
        self.member_caption_button: tk.Button | None = None
        self.member_caption_service: Service | None = None
        self.menu: SettingsMenu | None = None
        self.caption_target_menu: CaptionTargetMenu | None = None
        self.connect_guild_helper: ConnectGuildHelper | None = None
        self.toast: Toast | None = None
        self.hero_connect_button: tk.Button | None = None
        self.speaker_mute_menu: SpeakerMuteMenu | None = None
        self.caption_target_value: tk.Label | None = None
        self.caption_target_button: tk.Button | None = None
        self.hwnd = 0
        self.custom_chrome = False
        self.maximized = False
        self.restore_geometry = ""
        self.hidden = False
        self.announced_tray = False
        self.menu_closed_at = 0.0
        self.pair_signal_identity: tuple[int, int, int] | None = None
        self.drag_origin: tuple[int, int] | None = None
        self.resize_origin: tuple[str, int, int, int, int, int, int] | None = None
        self.member_guild_label: tk.Label | None = None
        self.hero_guild_label: tk.Label | None = None
        self.managed_cards: dict[str, dict[str, object]] = {}
        self.managed_card_order: list[str] = []
        self.quick_chat_results: queue.Queue[QueuedQuickChatResult] = queue.Queue()
        self.quick_chat_profile_results: queue.Queue[QuickChatProfileResult] = (
            queue.Queue()
        )
        self.quick_chat_profile_sequence = 0
        self.quick_chat_profile_active_sequence = 0
        self.quick_chat_profile_pending_operation = ""
        self.quick_chat_profile_pending_name: str | None = None
        self.quick_chat_profile_loaded = False
        self.quick_chat_profile_name: str | None = None
        self.quick_chat_draft_text = ""
        self.quick_chat_request_id = ""
        self.quick_chat_attempt_sequence = 0
        # Sends overlap now: the box closes on Enter and the member can be
        # typing the next line before the relay has answered the first, so
        # one in-flight slot is not enough. attempt_id -> (request_id, text).
        self.quick_chat_in_flight: dict[int, tuple[str, str]] = {}
        # Set when a send failed and the draft is waiting to be retried; it
        # is what makes the next hotkey press prefill the box.
        self.quick_chat_failure_message = ""

        family_names = tuple(tkfont.families(root))
        family_lookup = {name.casefold(): name for name in family_names}

        def choose_font(*names: str) -> str:
            for name in names:
                selected = family_lookup.get(name.casefold())
                if selected:
                    return selected
            return "Segoe UI"

        display_family = (
            choose_font("Segoe UI Variable Display", "Segoe UI", "Bahnschrift")
            if self.member_edition else
            choose_font("Bahnschrift", "Segoe UI Variable", "Segoe UI")
        )
        body_family = choose_font("Segoe UI Variable", "Segoe UI")
        mono_family = choose_font("Cascadia Mono", "Cascadia Code", "Consolas")
        self.fonts = {
            "title": tkfont.Font(family=display_family, size=11, weight="bold"),
            "hero": tkfont.Font(family=display_family, size=22, weight="bold"),
            "panel_title": tkfont.Font(
                family=display_family, size=16, weight="bold"
            ),
            "quick_title": tkfont.Font(
                family=display_family, size=14, weight="bold"
            ),
            "heading": tkfont.Font(
                family=display_family, size=11, weight="bold"
            ),
            "section": tkfont.Font(
                family=display_family, size=9, weight="bold"
            ),
            "body": tkfont.Font(family=body_family, size=10),
            "body_strong": tkfont.Font(
                family=body_family, size=10, weight="bold"
            ),
            "meta": tkfont.Font(family=body_family, size=9),
            "tiny": tkfont.Font(family=body_family, size=9),
            "button": tkfont.Font(
                family=body_family, size=9, weight="bold"
            ),
            "keycap": tkfont.Font(
                family=mono_family, size=9, weight="bold"
            ),
            "mono": tkfont.Font(family=mono_family, size=11, weight="bold"),
            "log": tkfont.Font(family=mono_family, size=9),
        }
        families = set(family_names)
        self.chrome_font = None
        if "Segoe MDL2 Assets" in families:
            self.chrome_font = tkfont.Font(family="Segoe MDL2 Assets", size=9)
        elif "Segoe Fluent Icons" in families:
            self.chrome_font = tkfont.Font(family="Segoe Fluent Icons", size=9)

        root.title(self.product_name)
        root.configure(bg=self.theme.bg)
        base_default = (
            (900, 760)
            if self.member_edition
            else (900, 780)
            if self.hosted_edition
            else (900, 760)
        )
        base_minimum = (
            (760, 640)
            if self.member_edition
            else (760, 640)
            if self.hosted_edition
            else (760, 640)
        )
        try:
            # Tk's baseline is 96 pixels / 72 points. Scaling the horizontal
            # shell with the font prevents side-packed raid actions from being
            # clipped by Windows 125-175% display scaling; vertical overflow is
            # handled by the accessible scroll surface below.
            display_scale = max(
                1.0,
                min(float(root.tk.call("tk", "scaling")) / (96.0 / 72.0), 1.75),
            )
        except (AttributeError, TypeError, ValueError, tk.TclError):
            display_scale = 1.0
        available_width = max(root.winfo_screenwidth() - 48, base_minimum[0])
        available_height = max(root.winfo_screenheight() - 80, 480)
        default_width = min(round(base_default[0] * display_scale), available_width)
        minimum_width = min(round(base_minimum[0] * display_scale), default_width)
        self.default_size = (
            default_width,
            min(base_default[1], available_height),
        )
        self.minimum_size = (
            minimum_width,
            min(base_minimum[1], self.default_size[1]),
        )
        root.geometry(f"{self.default_size[0]}x{self.default_size[1]}")
        root.minsize(*self.minimum_size)

        self.icon = None if self.preview else ensure_icon(self.edition)
        if self.icon is not None:
            try:
                root.iconbitmap(default=str(self.icon))
            except tk.TclError:
                pass

        self.tray = TrayIcon(
            self.product_name,
            self.icon,
            hotkey=(
                hotkey_support()[1](self.settings.get("speak_hotkey", ""))
                if self.caption_suite
                else None
            ),
        )
        # The result is deliberately not kept. Whether an icon exists is
        # asked fresh through tray_has_icon(), because Explorer can restart
        # and take it away long after this line has run.
        if getattr(self, "preview", False):
            self.tray.available = True
            self.tray.hotkey_registered = self.caption_suite
        else:
            self.tray.start()

        self.build_titlebar()
        self.titlebar_divider = HeaderDivider(self.root, color=self.theme.border, pady=0)
        self.tab_bar: TabBar | None = None
        self.captions_surface: ScrollSurface | None = None
        self.captions_page_frame: tk.Frame | None = None
        self.main_surface = ScrollSurface(root, background=self.theme.bg)
        self.main_surface.pack(fill="both", expand=True)
        self.main = self.main_surface.content
        self.build_audience_header()
        section_title = (
            "Raid communication"
            if self.member_edition
            else "Guild-powered tools"
            if self.hosted_edition
            else "Raid readiness"
        )
        section_subtitle = (
            "Caption controls and guild communication"
            if self.member_edition
            else "Sims and encounter builds from your guild server"
            if self.hosted_edition
            else "Your local raid tools and services"
        )
        self.service_section = SectionHeader(
            self.main,
            section_title,
            subtitle=section_subtitle,
            font_scale=self.fonts,
            theme=self.theme,
        )
        self.service_section.pack_configure(padx=24, pady=(18, 7))
        if self.member_edition:
            # The named cards already explain this screen. Keep this packing
            # anchor for update notices, without another repeated heading.
            for child in self.service_section.winfo_children():
                child.pack_forget()
            self.service_section.configure(height=1)
            self.service_section.pack_configure(pady=(12, 0))
        self.services_frame = tk.Frame(self.main, bg=self.theme.bg)
        self.services_frame.pack(fill="x")
        for service in self.services:
            self.build_row(service)
            if self.managed_edition and service.spec.key == "voice":
                self.managed_card_order.append("captions")
                self.build_managed_capability_cards(service)
        if not self.member_edition:
            self.build_tool_connection_card()
        self.build_activity_drawer()
        self.footer = tk.Frame(self.main, bg=self.theme.bg)
        self.footer.pack(fill="x", padx=24, pady=(12, 18))
        self.make_compact_button(
            self.footer,
            "Exit",
            self.quit_now,
        ).pack(side="right")
        self.footer_status = tk.Label(
            self.footer,
            text=self.quick_chat_hint(),
            bg=self.theme.bg,
            fg=self.theme.muted,
            font=self.fonts["meta"],
            anchor="w",
        )
        self.footer_status.pack(side="left", fill="x", expand=True)
        # Only shown if the custom title bar has to be abandoned, since the
        # gear lives on that bar and would go with it.
        self.native_settings = self.make_compact_button(
            self.footer,
            "Preferences",
            self.open_settings,
        )

        self.build_update_banner()
        if getattr(self, "preview", False):
            # Apply preview states after the selected service rows exist.
            preview_states = {
                "gear": ("Ready for your next check", SUCCESS, "ready"),
                "talents": ("Encounter builds ready", SUCCESS, "ready"),
                "voice": ("Connected - captions ready", SUCCESS, None),
                "voice_discord": (
                    "Live captions - consenting speakers only",
                    SUCCESS,
                    None,
                ),
            }
            for service in self.services:
                text, color, job_state = preview_states.get(
                    service.spec.key,
                    ("Ready", SUCCESS, None),
                )
                service.set_status(text, color, job_state)
                if service.spec.key == "voice":
                    service.set_lifecycle_state("running")
                elif service.spec.key == "voice_discord":
                    service.set_lifecycle_state("online")
                    service.set_listening_indicator(True)
        self.build_grips()
        root.protocol("WM_DELETE_WINDOW", self.on_close_button)
        if getattr(self, "preview", False):
            root.overrideredirect(True)
            self.custom_chrome = True
            self.center()
            root.deiconify()
        else:
            root.after(10, self.apply_chrome)
            root.after(120, self.tick)
            self.start_launch_services()

    def consume_quit_request(self) -> bool:
        """The uninstaller's polite close, answered.

        Consuming the file IS the acknowledgement - it distinguishes a
        live app from a build that predates the request, which the
        uninstaller escalates on. Freshness is the file's own mtime, not
        its content: the writer is the uninstaller's Pascal, and a
        contract only Python could conveniently satisfy would leave the
        polite path silently dead. The age gate stops a leftover file
        from a crashed uninstall killing the app at some random later
        launch.
        """
        if not QUIT_SIGNAL.is_file():
            return False
        fresh = False
        try:
            fresh = abs(time.time() - QUIT_SIGNAL.stat().st_mtime) < 10.0
        except OSError:
            pass
        try:
            QUIT_SIGNAL.unlink()
        except OSError:
            pass
        if fresh:
            self.quit_now()
            return True
        return False

    def start_launch_services(self) -> None:
        """Start this product's selected services only."""
        for service in self.services:
            if service.spec.key in CAPTION_PAGE_SERVICE_KEYS and not self.member_edition:
                continue
            if service.spec.start_on_launch:
                service.start()
            elif service.spec.external_control:
                service.request_external_status(force=True)

    def draw_listening_mark(
        self,
        canvas: tk.Canvas,
        size: int,
        *,
        accent: str = ACCENT,
    ) -> None:
        """Draw the Vemal's Ears signal mark without depending on artwork.

        Caption lines sit inside a quiet listening halo. The mark is static on
        purpose: motion in an accessibility tool should communicate a real
        state change, never exist only to decorate the screen.
        """
        scale = size / 68.0

        def point(value: float) -> int:
            return round(value * scale)

        stroke = max(1, point(2.0))
        canvas.create_oval(
            point(5), point(5), point(63), point(63),
            fill=self.theme.panel_alt,
            outline=self.theme.border_strong,
            width=stroke,
        )
        canvas.create_arc(
            point(10), point(10), point(58), point(58),
            start=214,
            extent=112,
            style="arc",
            outline=accent,
            width=stroke,
        )
        canvas.create_line(
            point(19), point(26), point(49), point(26),
            fill=self.theme.text,
            width=stroke,
            capstyle="round",
        )
        canvas.create_line(
            point(19), point(34), point(43), point(34),
            fill=accent,
            width=stroke,
            capstyle="round",
        )
        canvas.create_line(
            point(19), point(42), point(37), point(42),
            fill=self.theme.muted,
            width=stroke,
            capstyle="round",
        )
        canvas.create_line(
            point(37), point(42), point(32), point(48),
            fill=self.theme.muted,
            width=stroke,
            capstyle="round",
        )

    def draw_lite_relay_mark(
        self,
        canvas: tk.Canvas,
        size: int,
        *,
        accent: str = LITE_ACCENT,
    ) -> None:
        """Draw Downtime Lite's three-to-one guild relay mark."""
        scale = size / 68.0

        def point(value: float) -> int:
            return round(value * scale)

        stroke = max(1, point(2.0))
        canvas.create_oval(
            point(5), point(5), point(63), point(63),
            fill=self.theme.panel_alt,
            outline=self.theme.border_strong,
            width=stroke,
        )
        for y in (21, 34, 47):
            canvas.create_oval(
                point(15), point(y - 3), point(21), point(y + 3),
                fill=accent,
                outline="",
            )
            canvas.create_line(
                point(22), point(y), point(40), point(34),
                fill=accent if y == 34 else self.theme.muted,
                width=stroke,
                capstyle="round",
            )
        canvas.create_oval(
            point(39), point(27), point(53), point(41),
            fill=accent,
            outline=self.theme.text,
            width=max(1, point(1.0)),
        )
        canvas.create_oval(
            point(44), point(32), point(48), point(36),
            fill=self.theme.panel,
            outline="",
        )

    def build_audience_header(self) -> None:
        shell = CardShell(
            self.main,
            border_color=self.theme.border,
            background=self.theme.panel,
        )
        shell.pack(fill="x", padx=24, pady=(12, 0))
        hero = shell.content

        mark = tk.Canvas(
            hero,
            width=68,
            height=68,
            bg=self.theme.panel,
            highlightthickness=0,
        )
        mark.pack(side="left", padx=(18, 14), pady=14 if self.member_edition else 18)
        if self.member_edition:
            # Keep the image alive for the lifetime of the Tk window.
            self.hero_mark_image = self.load_mark_image(64)
            if self.hero_mark_image is not None:
                mark.create_image(34, 34, image=self.hero_mark_image)
            else:
                self.draw_listening_mark(mark, 68, accent=self.accent)
        elif self.hosted_edition:
            self.draw_lite_relay_mark(mark, 68, accent=self.accent)
        else:
            self.hero_mark_image = self.load_mark_image(64)
            if self.hero_mark_image is not None:
                mark.create_image(34, 34, image=self.hero_mark_image)
            else:
                mark.create_oval(6, 6, 62, 62, outline=self.accent, width=2)
                mark.create_line(34, 34, 51, 20, fill=self.theme.conversation, width=3)
                mark.create_oval(29, 29, 39, 39, fill=self.theme.conversation, outline="")

        copy = tk.Frame(hero, bg=self.theme.panel)
        copy.pack(side="left", fill="both", expand=True, pady=14 if self.member_edition else 18)
        identity = tk.Frame(copy, bg=self.theme.panel)
        identity.pack(fill="x")
        self.hero_title_label = tk.Label(
            identity,
            text=self.product_name,
            bg=self.theme.panel,
            fg=self.theme.text,
            anchor="w",
            font=self.fonts["hero"],
        )
        self.hero_title_label.pack(side="left")
        self.hero_edition_badge = EditionBadge(
            identity,
            self.theme.badge_label,
            accent=self.accent,
            background=self.theme.panel_alt,
            font=self.fonts["meta"],
        )
        if not self.member_edition:
            self.hero_edition_badge.pack(side="left", padx=(10, 0))
        self.brand_byline = None
        if self.member_edition:
            self.brand_byline = tk.Label(
                copy,
                text="(By Downtime)",
                bg=self.theme.panel,
                fg=self.theme.muted,
                anchor="w",
                font=self.fonts["tiny"],
            )
            self.brand_byline.pack(fill="x", pady=(1, 0))

        tagline = (
            "Your raid, in words."
            if self.member_edition
            else "Guild-powered tools, without the upkeep."
            if self.hosted_edition
            else "Raid tools, ready before the pull."
        )
        detail = (
            "Readable live captions, private consent, and one-key raid replies."
            if self.member_edition
            else "Your guild server handles sims and encounter data; Lite delivers the results to WoW."
            if self.hosted_edition
            else "Gear simulations and encounter builds, ready for your next raid."
        )
        tk.Label(
            copy,
            text=tagline,
            bg=self.theme.panel,
            fg=self.theme.text,
            anchor="w",
            font=self.fonts["heading"],
        ).pack(fill="x", pady=(7, 0))
        detail_label = tk.Label(
            copy,
            text=detail,
            bg=self.theme.panel,
            fg=self.theme.muted,
            anchor="w",
            justify="left",
            wraplength=430,
            font=self.fonts["meta"],
        )
        if not self.member_edition:
            detail_label.pack(fill="x", pady=(3, 0))

        actions = tk.Frame(hero, bg=self.theme.panel)
        actions.pack(side="right", fill="y", padx=(18, 20), pady=14 if self.member_edition else 18)
        if self.managed_edition:
            tk.Label(
                actions,
                text="Your guild",
                bg=self.theme.panel,
                fg=self.theme.muted,
                anchor="e",
                font=self.fonts["meta"],
            ).pack(fill="x", pady=(5, 0))
            self.hero_guild_label = tk.Label(
                actions,
                bg=self.theme.panel,
                fg=self.accent,
                anchor="e",
                font=self.fonts["body_strong"],
            )
            self.hero_guild_label.pack(fill="x", pady=(3, 0))
            # The first thing a new member has to do, where they look
            # first - not the emphasised button on a card two screens
            # down. Shino, 2026-09-04: "bring out the Connect guild
            # button". Once connected it becomes the way to switch.
            self.hero_connect_button = self.make_compact_button(
                actions, "Connect guild", self.open_connect_guild, accent=True,
            )
            self.hero_connect_button.pack(anchor="e", pady=(8, 0))
            self.refresh_pairing_identity()
        else:
            tk.Label(
                actions,
                text="Starts with the app",
                bg=self.theme.panel,
                fg=self.theme.muted,
                anchor="e",
                font=self.fonts["meta"],
            ).pack(fill="x", pady=(8, 0))

        signal_width = 174
        signal = SignalBar(
            actions,
            segments=max(len(self.services), 1),
            width=signal_width,
            height=7,
            background=self.theme.panel,
        )
        if not self.member_edition:
            signal.pack(side="bottom", anchor="e", pady=(12, 0))
        self.service_signal_items = {
            service.spec.key: signal.items[index]
            for index, service in enumerate(self.services)
        }
        self.service_signal = signal

    def update_service_signal(self, service_key: str, color: str) -> None:
        item = getattr(self, "service_signal_items", {}).get(service_key)
        signal = getattr(self, "service_signal", None)
        if item is not None and signal is not None:
            signal.itemconfig(item, fill=color)

    def build_activity_drawer(self) -> None:
        self.activity_expanded = False
        self.activity_count = 0
        shell = tk.Frame(self.main, bg=self.theme.border)
        self.activity_shell = shell
        shell.pack(fill="x", padx=24, pady=(4 if self.member_edition else 16, 0))
        header = tk.Frame(shell, bg=self.theme.panel)
        header.pack(fill="x", padx=1, pady=1)
        tk.Label(
            header,
            text="Recent jobs" if self.hosted_edition else "Connection details"
            if self.member_edition else "Activity",
            bg=self.theme.panel,
            fg=self.theme.text,
            font=self.fonts["body_strong"],
        ).pack(side="left", padx=(14, 10), pady=10)
        # No echo of the newest status sentence here. Every service card
        # already carries its own, so this line repeated a sentence sitting
        # two inches above it - Shino, 2026-09-05: "there are two text boxes
        # appearing for the same text". The card is the one that stays,
        # because it says WHICH service without having to name it.
        self.activity_summary = None
        # Holds the buttons at the right edge now that nothing between the
        # title and them expands.
        tk.Frame(header, bg=self.theme.panel).pack(
            side="left", fill="x", expand=True
        )
        self.activity_button = self.make_compact_button(
            header,
            "Show",
            self.toggle_activity,
        )
        self.activity_button.pack(side="right", padx=10, pady=7)
        # MAX_ACTIVITY_LINES bounds the log, but only by dropping the
        # oldest lines. After a failure the useful move is to empty it,
        # retry, and read only what the retry produced.
        self.activity_clear_button = self.make_compact_button(
            header,
            "Clear",
            self.clear_activity_log,
        )
        self.activity_clear_button.pack(side="right", padx=(0, 4), pady=7)

        self.activity_panel = tk.Frame(self.main, bg=self.theme.border)
        inner = tk.Frame(self.activity_panel, bg=self.theme.panel)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        scrollbar = nightwatch_scrollbar(inner)
        scrollbar.pack(side="right", fill="y")
        self.log = tk.Text(
            inner,
            bg=self.theme.panel,
            fg=self.theme.muted,
            insertbackground=self.theme.text,
            borderwidth=0,
            highlightthickness=0,
            wrap="word",
            state="disabled",
            font=self.fonts["log"],
            yscrollcommand=scrollbar.set,
            padx=12,
            pady=10,
            height=9,
        )
        self.log.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=self.log.yview)
        for spec in (*SERVICES, *HOSTED_SERVICES):
            self.log.tag_configure(spec.key, foreground=spec.color)
        self.log.tag_configure("body", foreground=self.theme.muted)

        # Right-click context menu for activity log
        self.activity_menu = ContextMenu(self.log)
        self.activity_menu.add_item("Copy Selection", self._copy_log_selection, accelerator="Ctrl+C")
        self.activity_menu.add_item("Clear Activity Log", self.clear_activity_log)
        self.activity_menu.add_divider()
        self.activity_menu.add_item("Select All", self._select_all_log, accelerator="Ctrl+A")
        self.activity_menu.attach_to(self.log)

    def _copy_log_selection(self) -> None:
        try:
            text = self.log.get("sel.first", "sel.last")
            if text:
                self.root.clipboard_clear()
                self.root.clipboard_append(text)
        except tk.TclError:
            pass

    def _select_all_log(self) -> None:
        try:
            self.log.tag_add("sel", "1.0", "end")
        except tk.TclError:
            pass

    def clear_activity_log(self) -> None:
        """Empty the Activity log at the member's request.

        Salvaged from the closed UI branch, which was the only place that
        had one. There is no summary line to reset any more; the service
        cards keep saying where each service stands, and clearing a log
        was never meant to claim a service had gone quiet.
        """
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")
        self.activity_count = 0

    def toggle_activity(self) -> None:
        self.activity_expanded = not self.activity_expanded
        if self.activity_expanded:
            self.activity_panel.pack(
                fill="x",
                padx=24,
                pady=(6, 0),
                before=self.footer,
            )
            self.activity_button.config(text="Hide")
            self.root.after_idle(self.main_surface.show_bottom)
            return
        self.activity_panel.pack_forget()
        self.activity_button.config(text="Show")

    def refresh_pairing_identity(self) -> None:
        if self.member_guild_label is None and self.hero_guild_label is None:
            return
        guild_name = (
            "Moonwell Raiders" if self.preview else paired_guild_name()
        )
        color = self.accent if guild_name else WARNING
        if self.hero_guild_label is not None:
            self.hero_guild_label.config(
                text=guild_name if guild_name else "Not connected",
                fg=color,
            )
        connect_button = getattr(self, "hero_connect_button", None)
        if connect_button is not None:
            connect_button.config(text="Change guild" if guild_name else "Connect guild")
        # An open helper says the same thing the header does. Pairing can
        # happen behind its back - the vemalsears:// link, the standalone
        # window - and a panel still offering "Connect" after that would
        # have the member spend a second invitation for nothing.
        helper = getattr(self, "connect_guild_helper", None)
        if helper is not None:
            try:
                if helper.winfo_exists():
                    helper.refresh_state()
            except tk.TclError:
                pass
        if self.member_guild_label is not None:
            self.member_guild_label.config(
                text=("Connected to " + guild_name)
                if guild_name
                else "Connect a guild to begin",
                fg=color,
            )
        guild_card = self.managed_cards.get("guild")
        if guild_card is not None:
            guild_card["rail"].config(bg=color)

    def consume_pairing_signal(self) -> bool:
        """Return true once per pairing signal, even if unlink is unavailable."""
        try:
            details = PAIR_SIGNAL.stat()
        except FileNotFoundError:
            self.pair_signal_identity = None
            return False
        except OSError:
            return False
        identity = (details.st_mtime_ns, details.st_ctime_ns, details.st_size)
        if identity == self.pair_signal_identity:
            return False
        try:
            PAIR_SIGNAL.unlink()
        except FileNotFoundError:
            self.pair_signal_identity = None
        except OSError:
            # The same unreadable signal must not start a new network request
            # on every UI tick. A rewritten signal has a new identity and is
            # still consumed normally.
            self.pair_signal_identity = identity
        else:
            self.pair_signal_identity = None
        return True

    def refresh_member_identity(self) -> None:
        """Compatibility name for extensions written for the member shell."""
        self.refresh_pairing_identity()

    # -- chrome ------------------------------------------------------------

    def handle(self) -> int:
        """Resolved fresh every time. Tk rebuilds its top level window when
        the decorations change, so a handle cached across that points at
        something that has already been destroyed."""
        self.hwnd = window_handle(self.root)
        if (self.hwnd and not getattr(self, "preview", False)
                and self.hwnd != getattr(self, "_taskbar_hwnd", None)):
            from GuildHub.windows_taskbar import apply_taskbar_identity
            member = self.member_edition
            if apply_taskbar_identity(
                self.hwnd, member=member,
                launcher=installed_launcher() if member else full_launcher(),
                product_name=self.product_name,
                icon_path=getattr(self, "icon", None),
            ):
                self._taskbar_hwnd = self.hwnd
        return self.hwnd

    def center(self) -> None:
        """Dropping the OS title bar also drops the placement it gave us,
        which lands the window in the top left corner."""
        width, height = self.default_size
        left, top, area_width, area_height = work_area(self.handle(), self.root)
        x = left + max((area_width - width) // 2, 0)
        y = top + max((area_height - height) // 3, 0)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def apply_chrome(self) -> None:
        """Drop the OS title bar, then buy back the taskbar button it came
        with. The shell only re-reads a window's style across a real hide
        and show, so this runs in stages rather than in one breath."""
        try:
            self.root.overrideredirect(True)
            if not self.handle():
                raise OSError("no window handle")
            # Claim it while the window is hidden, so the deiconify below is
            # the show the shell evaluates. Windows 11 also honours the claim
            # on an already visible window - verified on this machine, the
            # button appears either way - but doing it in the documented
            # order costs nothing and does not depend on that staying true.
            claim_taskbar_button(self.hwnd)
            self.center()
            self.root.withdraw()
            self.root.after(20, self.reshow_chrome)
        except Exception:
            self.use_native_chrome()

    def reshow_chrome(self) -> None:
        try:
            self.root.deiconify()
            self.root.after(20, self.confirm_chrome)
        except Exception:
            self.use_native_chrome()

    def confirm_chrome(self, attempt: int = 0) -> None:
        """Claim the taskbar button, then check on a LATER turn of the event
        loop.

        Be clear about what this proves and what it does not. Reading the
        style back catches exactly one failure - Tk overwriting the extended
        style while it finishes its own window setup - and it catches that
        only because the read happens a pass later than the write, never in
        the same breath. It is NOT evidence that the shell granted a taskbar
        button; nothing readable from in here is. That half was confirmed by
        looking at the real taskbar, which lists the window by name.
        """
        if self.custom_chrome:
            return
        if has_taskbar_button(self.handle()):
            self.custom_chrome = True
            return
        if attempt >= 6:
            self.use_native_chrome()
            return
        claim_taskbar_button(self.hwnd)
        self.root.after(80, lambda: self.confirm_chrome(attempt + 1))

    def use_native_chrome(self) -> None:
        """A window with no title bar and no taskbar button cannot be
        recovered by the person using it, so an ugly bar beats no bar."""
        self.custom_chrome = False
        try:
            self.root.overrideredirect(False)
            self.titlebar.pack_forget()
            for grip in self.grips:
                grip.place_forget()
            # The gear went with the title bar, so put Settings somewhere the
            # person can still reach it.
            self.native_settings.pack(side="right", padx=(0, 8))
            self.handle()
            self.root.deiconify()
        except tk.TclError:
            pass

    def load_mark_image(self, size: int):
        """The artwork scaled for the title bar, or None to draw instead.

        Tk reads png natively, but only whole-number subsampling, which turns
        a 256px source into a ragged 20px one. Pillow is already a dependency
        of the icon path, so it does the resampling when present.

        When it is not present the Tk path has to do the scaling itself.
        It used to hand the image back untouched, which is not a smaller
        mark at all: a 256px image drawn into a 68px canvas shows only its
        middle 68 pixels. Shino's clock lost its gear and its blue ring
        that way on 2026-09-05, on a bundle whose runtime shipped without
        Pillow. Ragged is a cosmetic complaint; cropped is the wrong
        picture.
        """
        source = find_artwork(self.edition)
        if source is None:
            return None
        try:
            from PIL import Image, ImageTk
            image = Image.open(source).convert("RGBA")
            if not self.member_edition:
                image = round_off(Image, image)
            image = image.resize((size, size), Image.LANCZOS)
            return ImageTk.PhotoImage(image, master=self.root)
        except Exception:
            pass
        try:
            return fit_photo_image(
                tk.PhotoImage(file=str(source), master=self.root), size
            )
        except tk.TclError:
            return None

    def build_titlebar(self) -> None:
        bar = tk.Frame(self.root, bg=self.theme.bg, height=self.TITLEBAR)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        self.titlebar = bar

        mark = tk.Canvas(bar, width=20, height=20, bg=self.theme.bg, highlightthickness=0)
        mark.pack(side="left", padx=(18, 0))
        # The real artwork when it is there, the drawn chevron when it is not.
        # Held on self because Tk drops an image the moment nothing references
        # it, and a garbage-collected PhotoImage renders as an empty square.
        self.mark_image = self.load_mark_image(20)
        if self.mark_image is not None:
            mark.create_image(10, 10, image=self.mark_image)
        elif self.member_edition:
            self.draw_listening_mark(mark, 20, accent=self.accent)
        else:
            mark.create_line(3, 13, 10, 6, 17, 13, fill=ACCENT, width=3,
                             capstyle="round", joinstyle="round")
        title = tk.Label(bar, text=self.product_name, bg=self.theme.bg, fg=self.theme.text,
                         font=self.fonts["title"])
        title.pack(side="left", padx=(9, 0))
        drag_widgets = [bar, mark, title]

        for name, command, danger in (
            ("close", self.on_close_button, True),
            ("maximize", self.toggle_maximize, False),
            ("minimize", self.minimize, False),
            ("settings", self.open_settings, False),
        ):
            button = self.chrome_button(bar, name, command, danger)
            button.pack(side="right", fill="y")
            if name == "settings":
                self.settings_button = button
            if name == "maximize":
                self.maximize_button = button

        # The support pair belongs in the persistent chrome, not the
        # scrollable footer. It remains visible without competing with a
        # person's raid actions or any product-tier controls.
        self.support_frame = tk.Frame(bar, bg=self.theme.bg)
        self.support_frame.pack(side="right", padx=(0, 7), pady=6)
        tk.Label(
            self.support_frame,
            text="Support",
            bg=self.theme.bg,
            fg=self.theme.subtle,
            font=self.fonts["tiny"],
        ).pack(side="left", padx=(0, 5))
        self.patreon_button = self.make_compact_button(
            self.support_frame,
            "Patreon",
            lambda: self.open_support_link(PATREON_CUSTOM_PLEDGE_URL, "Patreon"),
            text_color=PATREON_CORAL,
        )
        self.patreon_button.configure(padx=7, pady=2, font=self.fonts["tiny"])
        self.patreon_button.pack(side="left", padx=(0, 4))
        self.paypal_button = self.make_compact_button(
            self.support_frame,
            "PayPal",
            lambda: self.open_support_link(PAYPAL_DONATE_URL, "PayPal"),
            text_color=PAYPAL_BLUE,
        )
        self.paypal_button.configure(padx=7, pady=2, font=self.fonts["tiny"])
        self.paypal_button.pack(side="left")

        for widget in drag_widgets:
            widget.bind("<Button-1>", self.begin_drag)
            widget.bind("<B1-Motion>", self.do_drag)
            widget.bind("<Double-Button-1>", lambda _event: self.toggle_maximize())

    def chrome_button(
        self,
        parent: tk.Widget,
        name: str,
        command,
        danger: bool,
    ) -> tk.Button:
        button = IconButton(
            parent,
            name,
            command,
            danger=danger,
            chrome_font=self.chrome_font or self.fonts["body"],
        )
        hover = DANGER if danger else self.theme.panel_hover
        button.configure(bg=self.theme.bg, fg=self.theme.muted,
                         highlightbackground=self.theme.bg, highlightcolor=self.accent,
                         activebackground=hover)
        button.bind("<Enter>", lambda _e: button.configure(bg=hover, fg=self.theme.text))
        button.bind("<Leave>", lambda _e: button.configure(bg=self.theme.bg, fg=self.theme.muted))
        return button

    def set_chrome_glyph(self, button: tk.Widget, name: str) -> None:
        glyph, fallback = GLYPHS[name]
        button.config(text=glyph if self.chrome_font else fallback)

    def build_grips(self) -> None:
        """Resize edges, since a borderless window has none of its own."""
        edges = (
            ("n", "sb_v_double_arrow", dict(relx=0, rely=0, relwidth=1, height=self.EDGE, anchor="nw")),
            ("s", "sb_v_double_arrow", dict(relx=0, rely=1, relwidth=1, height=self.EDGE, anchor="sw")),
            ("w", "sb_h_double_arrow", dict(relx=0, rely=0, relheight=1, width=self.EDGE, anchor="nw")),
            ("e", "sb_h_double_arrow", dict(relx=1, rely=0, relheight=1, width=self.EDGE, anchor="ne")),
            ("nw", "size_nw_se", dict(relx=0, rely=0, width=self.CORNER, height=self.CORNER, anchor="nw")),
            ("ne", "size_ne_sw", dict(relx=1, rely=0, width=self.CORNER, height=self.CORNER, anchor="ne")),
            ("sw", "size_ne_sw", dict(relx=0, rely=1, width=self.CORNER, height=self.CORNER, anchor="sw")),
            ("se", "size_nw_se", dict(relx=1, rely=1, width=self.CORNER, height=self.CORNER, anchor="se")),
        )
        self.grips = []
        for edge, cursor, place in edges:
            grip = tk.Frame(self.root, bg=self.theme.bg, cursor=cursor)
            grip.place(**place)
            grip.lift()
            grip.bind("<Button-1>", lambda event, e=edge: self.begin_resize(e, event))
            grip.bind("<B1-Motion>", self.do_resize)
            self.grips.append(grip)

    # -- moving and sizing -------------------------------------------------

    def begin_drag(self, event: tk.Event) -> None:
        if self.maximized:
            return
        self.drag_origin = (event.x_root - self.root.winfo_x(),
                            event.y_root - self.root.winfo_y())

    def clamp_to_desktop(self, x: int, y: int, width: int) -> tuple[int, int]:
        """Keep enough of the title bar on a monitor to grab it again.

        The OS used to stop a window being dragged somewhere unreachable.
        Drawing our own title bar means enforcing that ourselves, or the
        window can be pushed off the edge with no way to get it back.
        """
        try:
            metric = ctypes.windll.user32.GetSystemMetrics
            left, top = metric(76), metric(77)
            span_x, span_y = metric(78), metric(79)
        except Exception:
            return x, y
        if span_x <= 0 or span_y <= 0:
            return x, y
        edge = 120
        x = max(left - width + edge, min(x, left + span_x - edge))
        y = max(top, min(y, top + span_y - self.TITLEBAR))
        return x, y

    def do_drag(self, event: tk.Event) -> None:
        if self.drag_origin is None or self.maximized:
            return
        dx, dy = self.drag_origin
        x, y = self.clamp_to_desktop(
            event.x_root - dx, event.y_root - dy, self.root.winfo_width())
        self.root.geometry(f"+{x}+{y}")

    def begin_resize(self, edge: str, event: tk.Event) -> None:
        if self.maximized:
            return
        self.resize_origin = (
            edge, event.x_root, event.y_root, self.root.winfo_x(),
            self.root.winfo_y(), self.root.winfo_width(), self.root.winfo_height(),
        )

    def do_resize(self, event: tk.Event) -> None:
        if self.resize_origin is None:
            return
        edge, sx, sy, x, y, width, height = self.resize_origin
        min_width, min_height = self.minimum_size
        dx, dy = event.x_root - sx, event.y_root - sy
        if "e" in edge:
            width = max(min_width, width + dx)
        if "s" in edge:
            height = max(min_height, height + dy)
        if "w" in edge:
            # Clamp the width first, then let the left edge follow it, or the
            # window slides sideways once it can shrink no further.
            new_width = max(min_width, width - dx)
            x += width - new_width
            width = new_width
        if "n" in edge:
            new_height = max(min_height, height - dy)
            y += height - new_height
            height = new_height
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def toggle_maximize(self) -> None:
        if not self.custom_chrome:
            self.root.state("normal" if self.maximized else "zoomed")
            self.maximized = not self.maximized
            return
        if self.maximized:
            if self.restore_geometry:
                self.root.geometry(self.restore_geometry)
            self.maximized = False
            self.set_chrome_glyph(self.maximize_button, "maximize")
            return
        self.restore_geometry = (
            f"{self.root.winfo_width()}x{self.root.winfo_height()}"
            f"+{self.root.winfo_x()}+{self.root.winfo_y()}"
        )
        left, top, width, height = work_area(self.handle(), self.root)
        self.root.geometry(f"{width}x{height}+{left}+{top}")
        self.maximized = True
        self.set_chrome_glyph(self.maximize_button, "restore")

    def minimize(self) -> None:
        self.close_menu()
        hwnd = self.handle()
        # Only send the window away if there is a button to bring it back.
        # Otherwise fall through and let Tk restore the native bar first.
        if self.custom_chrome and hwnd and has_taskbar_button(hwnd):
            # ShowWindow rather than iconify(): Tk refuses to iconify a
            # borderless window, and toggling overrideredirect to get around
            # that flashes the native title bar on every restore.
            try:
                user32 = ctypes.windll.user32
                user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
                user32.ShowWindow.restype = wintypes.BOOL
                user32.ShowWindow(hwnd, SW_MINIMIZE)
                return
            except Exception:
                pass
        try:
            self.root.overrideredirect(False)
            self.root.iconify()
        except tk.TclError:
            pass

    # -- settings ----------------------------------------------------------

    def tray_has_icon(self) -> bool:
        """Asked fresh every time. Whether an icon exists changes underneath
        us - Explorer can restart - and a stale yes is what turns hiding the
        window into losing it."""
        return bool(self.tray.available)

    def tray_note(self) -> str:
        if self.tray_has_icon():
            return (
                "Closing hides Vemal's Ears by the clock and keeps captions connected."
                if self.member_edition
                else "Closing hides Downtime Lite by the clock. Your guild connection stays active."
                if self.hosted_edition
                else "Closing hides Downtime by the clock and keeps raid tools ready."
            )
        return "No notification area available, so closing minimizes instead."

    def request_quick_chat_profile(self, menu: SettingsMenu) -> None:
        """Load the server-owned typed-message identity without blocking Tk."""
        if not self.caption_suite:
            menu.show_message_name_failure(
                "Message names are available in Vemal's Ears."
            )
            return
        if getattr(self, "preview", False):
            menu.show_message_name_ready(None, preview=True)
            return
        if self.quick_chat_profile_pending_operation == "save":
            menu.show_message_name_saving()
            return
        started, message = self._start_quick_chat_profile_request("load")
        if not started:
            menu.show_message_name_failure(message)
            return
        menu.show_message_name_loading()

    def reset_quick_chat_profile_for_pairing(self) -> None:
        """Drop an old guild's identity and make every old result stale."""
        self.quick_chat_profile_sequence += 1
        self.quick_chat_profile_active_sequence = self.quick_chat_profile_sequence
        self.quick_chat_profile_pending_operation = ""
        self.quick_chat_profile_pending_name = None
        self.quick_chat_profile_loaded = False
        self.quick_chat_profile_name = None
        menu = getattr(self, "menu", None)
        try:
            menu_open = menu is not None and bool(menu.winfo_exists())
        except (AttributeError, tk.TclError):
            menu_open = False
        if (
            menu_open
            and getattr(menu, "message_name_entry", None) is not None
        ):
            menu.message_name_baseline = None
            if menu.message_name_field is not None:
                menu.message_name_field.set("")
            self.request_quick_chat_profile(menu)

    def save_quick_chat_profile_name(
        self,
        name: str,
        menu: SettingsMenu,
    ) -> tuple[bool, str]:
        """Begin one authenticated save and report only that it began."""
        clean = canonicalize_quick_chat_name(name)
        if clean is None:
            return (
                False,
                "Use 1-32 characters without control or direction-changing "
                "characters.",
            )
        if not self.caption_suite:
            return (
                False,
                "Message names are available in Vemal's Ears.",
            )
        if getattr(self, "preview", False):
            menu.show_message_name_ready(clean, preview=True)
            return True, "Preview only - no name was saved."
        if self.quick_chat_profile_pending_operation == "save":
            return False, "A message name is already being saved."
        if menu.message_name_field is not None:
            menu.message_name_field.set(clean)
        started, message = self._start_quick_chat_profile_request("save", clean)
        if not started:
            return False, message
        menu.show_message_name_saving()
        return True, "Saving to your guild..."

    def _start_quick_chat_profile_request(
        self,
        operation: str,
        name: str | None = None,
    ) -> tuple[bool, str]:
        self.quick_chat_profile_sequence += 1
        sequence = self.quick_chat_profile_sequence
        self.quick_chat_profile_active_sequence = sequence
        self.quick_chat_profile_pending_operation = operation
        self.quick_chat_profile_pending_name = name if operation == "save" else None
        try:
            threading.Thread(
                target=self._perform_quick_chat_profile_request,
                args=(sequence, operation, name),
                name="VemalsEarsMessageName",
                daemon=True,
            ).start()
        except Exception as error:
            self.quick_chat_profile_pending_operation = ""
            self.quick_chat_profile_pending_name = None
            return False, safe_quick_chat_profile_error(error, operation)
        return True, ""

    def _perform_quick_chat_profile_request(
        self,
        sequence: int,
        operation: str,
        name: str | None,
    ) -> None:
        try:
            from GuildHub.client import (
                get_quick_chat_profile,
                set_quick_chat_profile,
            )
            if sequence != self.quick_chat_profile_active_sequence:
                return
            credential_store = guild_credential_store()
            credentials = credential_store.load()
            if sequence != self.quick_chat_profile_active_sequence:
                return
            credential_snapshot = _QuickChatCredentialSnapshot(credentials)
            if operation == "load":
                profile = asyncio.run(get_quick_chat_profile(credential_snapshot))
            elif operation == "save" and name is not None:
                profile = asyncio.run(
                    set_quick_chat_profile(credential_snapshot, name)
                )
            else:
                raise ValueError("invalid profile operation")
            server_name = getattr(profile, "quick_chat_name", object())
            if server_name is None:
                if operation != "load":
                    raise ValueError("saved profile has no name")
                clean_name = None
            else:
                clean_name = canonicalize_quick_chat_name(server_name)
                if clean_name is None:
                    raise ValueError("invalid profile response")
            result = QuickChatProfileResult(
                sequence,
                operation,
                True,
                clean_name,
            )
        except Exception as error:
            result = QuickChatProfileResult(
                sequence,
                operation,
                False,
                message=safe_quick_chat_profile_error(error, operation),
            )
        self.quick_chat_profile_results.put(result)

    def drain_quick_chat_profile_results(self) -> None:
        """Apply only the newest request; keep a closed panel's failure visible."""
        while True:
            try:
                result = self.quick_chat_profile_results.get_nowait()
            except queue.Empty:
                return
            if result.sequence != self.quick_chat_profile_active_sequence:
                continue
            self.quick_chat_profile_pending_operation = ""
            self.quick_chat_profile_pending_name = None
            if result.success:
                self.quick_chat_profile_loaded = True
                self.quick_chat_profile_name = result.name
            elif result.operation == "load":
                # The field may retain a last-known value as an editable
                # draft, but it is no longer truthful to treat that value as
                # confirmed by the currently paired guild.
                self.quick_chat_profile_loaded = False

            menu = getattr(self, "menu", None)
            try:
                menu_open = menu is not None and bool(menu.winfo_exists())
            except (AttributeError, tk.TclError):
                menu_open = False
            if menu_open and hasattr(menu, "show_message_name_ready"):
                if result.success:
                    menu.show_message_name_ready(
                        result.name,
                        saved=result.operation == "save",
                    )
                else:
                    menu.show_message_name_failure(result.message)
                continue
            if result.operation == "save" and not result.success:
                notified = self.notify(
                    "Message name could not save", result.message
                )
                if not notified:
                    footer = getattr(self, "footer_status", None)
                    if footer is not None:
                        footer.config(text=result.message, fg=DANGER)

    def quick_chat_hint(self) -> str:
        if not self.caption_suite:
            return (
                "Your guild connection keeps gear and talent checks up to date."
                if self.hosted_edition
                else "Raid tools keep watching while Downtime stays open."
            )
        hotkey = str(self.settings.get("speak_hotkey", "")).strip()
        prefix = (
            "Your guild connection restores automatically. "
            if self.hosted_edition
            else "Captions reconnect automatically. "
        )
        if hotkey and self.tray.hotkey_registered:
            return prefix + "Message Group: " + hotkey
        if hotkey:
            return prefix + "Message Group button ready - shortcut unavailable."
        return prefix + "Message Group button ready."

    def refresh_hotkey_surfaces(self) -> None:
        hotkey = str(self.settings.get("speak_hotkey", "")).strip()
        hero = getattr(self, "hero_hotkey", None)
        if hero is not None:
            hero.config(text=hotkey_keycaps(hotkey))
        footer = getattr(self, "footer_status", None)
        if footer is not None:
            footer.config(text=self.quick_chat_hint(), fg=self.theme.muted)

    def set_speak_hotkey(self, spec: str) -> tuple[bool, str]:
        """Register first, persist second, and roll back either failure."""
        if not self.caption_suite:
            return False, "Message Group shortcuts are available in Vemal's Ears."
        _default, parse_hotkey, canonicalize_hotkey = hotkey_support()
        canonical = canonicalize_hotkey(spec)
        parsed = parse_hotkey(canonical or "")
        if canonical is None or parsed is None:
            return False, "That shortcut is not supported. Try another."
        if getattr(self, "preview", False):
            self.settings["speak_hotkey"] = canonical
            self.refresh_hotkey_surfaces()
            return True, "Shortcut changed to " + canonical + "."
        previous = str(self.settings.get("speak_hotkey", ""))
        previous_canonical = canonicalize_hotkey(previous) or previous
        previous_parsed = parse_hotkey(previous_canonical)
        previous_worked = bool(self.tray.hotkey_registered)

        if not self.tray.update_hotkey(parsed):
            if previous_worked:
                return (
                    False,
                    "That shortcut is unavailable. Your previous shortcut is unchanged.",
                )
            return False, "That shortcut is unavailable right now. Try another."

        old_settings = dict(self.settings)
        self.settings["speak_hotkey"] = canonical
        if not save_settings(self.settings):
            self.settings = old_settings
            rolled_back = (
                previous_parsed is not None
                and self.tray.update_hotkey(previous_parsed)
            )
            self.refresh_hotkey_surfaces()
            if rolled_back:
                return False, "The shortcut could not be saved. Nothing changed."
            # Registration changed but the disk and UI did not. Say exactly
            # what survived rather than claiming a rollback that failed.
            self.settings["speak_hotkey"] = canonical
            self.refresh_hotkey_surfaces()
            return (
                True,
                "The shortcut works for this session but could not be saved.",
            )

        self.refresh_hotkey_surfaces()
        return True, "Shortcut changed to " + canonical + "."

    def set_minimize_to_taskbar(self, value: bool) -> bool:
        """A setting that could not be written is a setting that did not
        change - report what survived, not what was asked for."""
        previous = self.settings["minimize_to_taskbar"]
        self.settings["minimize_to_taskbar"] = bool(value)
        if getattr(self, "preview", False):
            return self.settings["minimize_to_taskbar"]
        if not save_settings(self.settings):
            self.settings["minimize_to_taskbar"] = previous
        return self.settings["minimize_to_taskbar"]

    def build_line(self) -> str:
        """Edition and version, for the Preferences header.

        The product and edition identify which capabilities this app carries;
        the version helps the player recognize an outdated installation.
        """
        # These are product names, so they are capitalised and they are
        # not derived from the internal marker values. The full build is
        # "Server Edition" - it is the one that runs the guild's services
        # - and calling it "full" on screen was this label's own coinage,
        # not a name anything else in the product uses.
        if self.member_edition:
            edition = "Vemal's Ears Member Edition"
        elif self.hosted_edition:
            edition = "Downtime Hosted Edition"
        else:
            edition = "Downtime Server Edition"
        return edition + " - version " + shipped_version()

    def open_settings(self) -> None:
        if self.menu is not None:
            self.close_menu()
            return
        # Clicking the gear while the popup is open takes focus off it, and
        # that dismissal lands first. Without this the same click would
        # immediately build a new one and the gear could never close it.
        if time.monotonic() - self.menu_closed_at < 0.25:
            return
        anchor = self.settings_button if self.custom_chrome else self.native_settings
        self.menu = SettingsMenu(self, anchor)

    def close_menu(self) -> None:
        if self.menu is not None:
            self.menu.dismiss()

    def open_connect_guild(self) -> None:
        """One panel for everything about the guild connection."""
        existing = self.connect_guild_helper
        try:
            exists = existing is not None and bool(existing.winfo_exists())
        except tk.TclError:
            exists = False
        if exists:
            # The connection may have changed under it - the deep link
            # pairs without this panel - so it re-reads before it shows.
            existing.refresh_state()
            existing.deiconify()
            existing.lift()
            existing.focus_force()
            return
        self.connect_guild_helper = ConnectGuildHelper(self)

    def open_caption_target_picker(self) -> None:
        """Open one target picker, or bring its existing picker back forward."""
        existing = self.caption_target_menu
        try:
            exists = existing is not None and bool(existing.winfo_exists())
        except tk.TclError:
            exists = False
        if exists:
            existing.deiconify()
            existing.lift()
            existing.focus_force()
            return
        anchor = self.caption_target_button
        if anchor is None:
            return
        self.caption_target_menu = CaptionTargetMenu(self, anchor)

    def open_speaker_mute_picker(self) -> None:
        existing = self.speaker_mute_menu
        if existing is not None and existing.winfo_exists():
            existing.lift()
            existing.focus_force()
            return
        anchor = getattr(self, "speaker_mute_button", None)
        if anchor is None:
            return
        self.speaker_mute_menu = SpeakerMuteMenu(self, anchor)

    # -- rows and log ------------------------------------------------------

    def build_standard_card(
        self,
        parent: tk.Frame,
        *,
        rail_color: str = SUBTLE,
    ) -> tuple[tk.Frame, tk.Frame, tk.Frame, tk.Frame]:
        """Create the shared Downtime card shell used by every main action."""
        shell = CardShell(
            parent,
            border_color=self.theme.border,
            background=self.theme.panel_alt,
            has_rail=True,
            rail_color=rail_color,
            rail_side="right",
            rail_width=2 if self.member_edition else 4,
            padx=16,
            pady=10 if self.member_edition else 13,
        )
        shell.pack(fill="x", padx=24, pady=(0, 8))
        card = shell.card
        content = shell.content
        rail = shell.rail
        if rail is None:
            raise RuntimeError("shared card shell did not create its status rail")
        return shell, card, content, rail

    def build_row(self, service: Service, parent: tk.Frame | None = None) -> None:
        _shell, card, content, service.rail = self.build_standard_card(
            parent if parent is not None else self.services_frame
        )
        top = tk.Frame(content, bg=self.theme.panel_alt)
        top.pack(fill="x")

        copy = tk.Frame(top, bg=self.theme.panel_alt)
        copy.pack(side="left", fill="x", expand=True)
        # Under the Vemal's Ears page header a row titled "Vemal's Ears"
        # would say nothing, so the caption rows there are named for what
        # they are, the way the managed editions already name the feed.
        on_captions_page = self.captions_page and service.spec.key in CAPTION_PAGE_SERVICE_KEYS
        label = (
            "Live captions"
            if service.spec.key == "voice" and (self.managed_edition or on_captions_page)
            else "Discord listener"
            if service.spec.key == "voice_discord" and on_captions_page
            else service.spec.label
        )
        tk.Label(
            copy,
            text=label,
            bg=self.theme.panel_alt,
            fg=self.theme.text,
            anchor="w",
            font=self.fonts["heading"],
        ).pack(fill="x")
        service.status = tk.Label(
            copy,
            text="Starting",
            bg=self.theme.panel_alt,
            fg=self.theme.muted,
            anchor="w",
            font=self.fonts["meta"],
        )
        service.status.pack(fill="x", pady=(2, 0))
        if service.spec.key in PROGRESS_SERVICE_KEYS:
            # Built now, packed later: Service.set_progress shows it on
            # the first progress line and clear_progress retires it, so
            # a row that never sims never grows. A child of the card's
            # content, packed after the top block, not inside it: inside,
            # it would raise that block and re-centre the buttons on the
            # right every time it came and went.
            service.progress_anchor = top
            service.progress_row = tk.Frame(content, bg=self.theme.panel_alt)
            service.progress_bar = ProgressBar(
                service.progress_row,
                width=260,
                height=8,
                accent=self.accent,
                background=self.theme.panel_alt,
            )
            service.progress_bar.pack(side="left")
            service.progress_caption = tk.Label(
                service.progress_row,
                text="",
                bg=self.theme.panel_alt,
                fg=self.theme.muted,
                anchor="w",
                font=self.fonts["meta"],
            )
            service.progress_caption.pack(side="left", padx=(8, 0))

        controls = tk.Frame(top, bg=self.theme.panel_alt)
        controls.pack(side="right", padx=(14, 0))
        action_controls = tk.Frame(controls, bg=self.theme.panel_alt)
        action_controls.pack(side="left")
        indicators = tk.Frame(controls, bg=self.theme.panel_alt)
        indicators.pack(side="left")
        if service.spec.key in {"gear", "talents"} and not self.member_edition:
            badge_text, badge_color = job_status_badge(service.job_state)
            badge_shell = tk.Frame(indicators, bg=self.theme.panel)
            badge_shell.pack(side="left", padx=(8, 0))
            service.result_badge_rail = tk.Frame(
                badge_shell,
                bg=badge_color,
                width=3,
            )
            service.result_badge_rail.pack(side="left", fill="y")
            service.result_badge = tk.Label(
                badge_shell,
                text=badge_text,
                width=STATUS_BADGE_WIDTH,
                bg=self.theme.panel,
                fg=badge_color,
                font=self.fonts["keycap"],
                padx=8,
                pady=5,
                relief="flat",
                bd=0,
                highlightthickness=0,
            )
            service.result_badge.pack(side="left")
        if (
            service.spec.show_lifecycle_controls
            and service.spec.separate_lifecycle_controls
        ):
            if service.spec.key == "voice_discord":
                # A miniature recorder tally: the red LED carries the privacy
                # warning, while the surrounding service state remains green.
                service.listening_indicator = tk.Frame(
                    action_controls,
                    bg=self.theme.panel,
                    highlightbackground=DANGER,
                    highlightcolor=DANGER,
                    highlightthickness=1,
                    bd=0,
                )
                service.listening_icon = tk.Canvas(
                    service.listening_indicator,
                    width=14,
                    height=14,
                    bg=self.theme.panel,
                    highlightthickness=0,
                    bd=0,
                )
                service.listening_icon.pack(side="left", padx=(6, 2), pady=4)
                service.listening_icon.create_oval(
                    3,
                    3,
                    11,
                    11,
                    fill=DANGER,
                    outline="",
                )
                service.listening_label = tk.Label(
                    service.listening_indicator,
                    text="REC",
                    bg=self.theme.panel,
                    fg=DANGER,
                    font=self.fonts["keycap"],
                    padx=0,
                    pady=3,
                )
                service.listening_label.pack(side="left", padx=(0, 7))
            lifecycle_shell = tk.Frame(indicators, bg=self.theme.panel)
            lifecycle_shell.pack(side="left", padx=(8, 0))
            service.lifecycle_badge_rail = tk.Frame(
                lifecycle_shell,
                bg=WARNING,
                width=3,
            )
            service.lifecycle_badge_rail.pack(side="left", fill="y")
            service.lifecycle_badge = tk.Label(
                lifecycle_shell,
                text="Checking",
                width=STATUS_BADGE_WIDTH,
                bg=self.theme.panel,
                fg=WARNING,
                font=self.fonts["keycap"],
                padx=8,
                pady=5,
                relief="flat",
                bd=0,
                highlightthickness=0,
            )
            service.lifecycle_badge.pack(side="left")
            service.start_button = self.make_button(
                action_controls,
                "Start",
                service.request_start,
            )
            service.start_button.pack(side="left")
            if service.listening_indicator is not None:
                # Keep the capture warning adjacent to the controls it
                # qualifies: it appears immediately before Start/Stop, not
                # alongside the right-edge connection statuses.
                service.listening_indicator_before = service.start_button
            service.stop_button = self.make_button(
                action_controls,
                "Stop",
                service.request_stop,
            )
            service.stop_button.pack(side="left", padx=(7, 0))
            service.set_lifecycle_state("checking")
        elif service.spec.show_lifecycle_controls:
            service.button = self.make_button(
                action_controls, "Stop", service.toggle
            )
            service.button.pack(side="left")
        elif service.spec.key == "voice":
            self.member_caption_service = service
            self.member_caption_button = self.make_button(
                action_controls,
                "Pause caption feed",
                service.toggle_member_captions,
            )
            self.member_caption_button.pack(side="left")

        # The compact health signal finishes a single right-edge instrument
        # cluster. Actions always precede it; badges and privacy state sit
        # between the actions and this final at-a-glance signal.
        service.dot = StatusOrb(
            indicators,
            background=self.theme.panel_alt,
            animate=not getattr(self, "preview", False),
        )
        service.dot.pack(side="left", padx=(9, 0))
        service.dot_id = service.dot.core
        if service.listening_indicator is None:
            service.listening_indicator_before = service.dot
        if service.spec.key == "voice_discord":
            service.set_listening_indicator(False)

        service_descriptions = {
            "gear": (
                "Your guild server handles the sim and sends results back to WoW."
                if self.hosted_edition
                else "Find, compare, and import your next meaningful upgrades."
            ),
            "talents": (
                "Encounter builds arrive through your guild's Warcraft Logs connection."
                if self.hosted_edition
                else "Keep encounter builds ready when the fight changes."
            ),
            "voice": (
                "Only consenting speakers are transcribed. Audio is never saved."
            ),
            "voice_discord": (
                "The guild Discord listener. Consenting speakers are processed "
                "live for captions; audio is never saved."
            ),
        }
        tk.Label(
            content,
            text=service_descriptions.get(service.spec.key, service.spec.note),
            bg=self.theme.panel_alt,
            fg=self.theme.muted,
            anchor="w",
            justify="left",
            wraplength=620,
            font=self.fonts["meta"],
        ).pack(fill="x", pady=(8, 0))

        actions = tk.Frame(content, bg=self.theme.panel_alt)
        has_action = False
        action_labels = {
            "Pair": "Connect guild",
            "Move": "Move captions",
            "Voice setup": "Install voice tools",
        }
        if service.spec.key == "voice" and self.captions_page:
            # The member app's pause, on the row that owns the receiver.
            # Start/Stop above are the process; this is the feed the
            # player reads. The launch default leaves it paused, so
            # "Resume caption feed" is the first thing the row offers.
            self.member_caption_service = service
            self.member_caption_button = self.make_compact_button(
                actions,
                "Pause caption feed",
                service.toggle_member_captions,
            )
            self.member_caption_button.pack(side="left", padx=(0, 7))
            has_action = True
        if not (service.spec.key == "voice" and self.managed_edition):
            for action_label, script in (
                (service.spec.action_label, service.spec.action_script),
                (service.spec.second_action_label, service.spec.second_action_script),
                (service.spec.third_action_label, service.spec.third_action_script),
            ):
                if script is None:
                    continue
                if (
                    self.captions_page
                    and service.spec.key == "voice"
                    and action_label in ("Pair", "Move")
                ):
                    # Already on this page: Connect guild on the Guild &
                    # Privacy card, Move captions on Caption display. A
                    # second copy here would be a dead duplicate.
                    continue
                has_action = True
                self.make_compact_button(
                    actions,
                    action_labels.get(action_label, action_label or "Open"),
                    lambda path=script: service.run_action(path),
                ).pack(side="left", padx=(0, 7))
        if has_action:
            actions.pack(fill="x", pady=(11, 0))
        if (
            service.spec.key == "voice"
            and not self.managed_edition
            and not self.captions_page
        ):
            # With a captions page the mute row lives there; both would
            # fight over the same single-instance widget attributes.
            self.build_speaker_mute_row(content)

    def show_page(self, index: int, *, remember: bool = True) -> None:
        """Compatibility for preview callers: both products now have one page."""
        return

    def build_managed_card(
        self,
        key: str,
        title: str,
        status_text: str,
        description: str,
        *,
        rail_color: str,
        parent: tk.Frame | None = None,
    ) -> tk.Frame:
        """Build one task-focused card for the caption suite."""
        shell, card, content, rail = self.build_standard_card(
            parent if parent is not None else self.services_frame,
            rail_color=rail_color,
        )
        top = tk.Frame(content, bg=self.theme.panel_alt)
        top.pack(fill="x")
        copy = tk.Frame(top, bg=self.theme.panel_alt)
        copy.pack(side="left", fill="x", expand=True)
        title_label = tk.Label(
            copy,
            text=title,
            bg=self.theme.panel_alt,
            fg=self.theme.text,
            anchor="w",
            font=self.fonts["heading"],
        )
        title_label.pack(fill="x")
        status = tk.Label(
            copy,
            text=status_text,
            bg=self.theme.panel_alt,
            fg=self.theme.muted,
            anchor="w",
            font=self.fonts["meta"],
        )
        status.pack(fill="x", pady=(2, 0))
        controls = tk.Frame(top, bg=self.theme.panel_alt)
        controls.pack(side="right", padx=(14, 0))
        if description:
            tk.Label(
                content,
                text=description,
                bg=self.theme.panel_alt,
                fg=self.theme.muted,
                anchor="w",
                justify="left",
                wraplength=520,
                font=self.fonts["meta"],
            ).pack(fill="x", pady=(8, 0))
        self.managed_cards[key] = {
            "shell": shell,
            "card": card,
            "content": content,
            "rail": rail,
            "title": title_label,
            "status": status,
            "controls": controls,
            "buttons": [],
        }
        self.managed_card_order.append(key)
        return controls

    def build_managed_capability_cards(
        self, service: Service, parent: tk.Frame | None = None
    ) -> None:
        """Split captions, conversation, and privacy into standard cards."""
        display_controls = self.build_managed_card(
            "display",
            "Caption display",
            "",
            "",
            rail_color=self.accent,
            parent=parent,
        )
        display_buttons = self.managed_cards["display"]["buttons"]
        for action_label, script in (
            (service.spec.action_label, service.spec.action_script),
            (service.spec.second_action_label, service.spec.second_action_script),
            (service.spec.third_action_label, service.spec.third_action_script),
        ):
            if script is None or action_label != "Move":
                continue
            button = self.make_compact_button(
                display_controls,
                "Move captions",
                lambda path=script: service.run_action(path),
            )
            button.pack(side="left", padx=(0, 7))
            display_buttons.append(button)
        mute_button = self.build_mute_toggle(display_controls, compact=True)
        focus_button = self.build_focus_toggle(display_controls, compact=True)
        display_buttons.extend(
            button for button in (mute_button, focus_button) if button is not None
        )
        self.managed_cards["display"]["status"].pack_forget()
        self.build_caption_options(self.managed_cards["display"]["content"])

        message_controls = self.build_managed_card(
            "message",
            "Message Group",
            "Reply in text without leaving the pull",
            "",
            rail_color=self.theme.conversation,
            parent=parent,
        )
        self.hero_hotkey = tk.Label(
            message_controls,
            text=hotkey_keycaps(self.settings.get("speak_hotkey", "")),
            bg=self.theme.panel_alt,
            fg=self.theme.muted,
            anchor="e",
            font=self.fonts["keycap"],
        )
        self.hero_hotkey.pack(side="left", padx=(0, 10))
        self.hero_chat_button = self.make_chat_button(
            message_controls,
            "Message Group",
            self.open_speak_box,
        )
        self.hero_chat_button.pack(side="left")
        self.managed_cards["message"]["buttons"].append(self.hero_chat_button)

        # No description: Shino, 2026-09-04, "Guild & Privacy is a strong
        # enough label without the extra". The status line under it says
        # which guild, and the buttons say what can be done.
        guild_controls = self.build_managed_card(
            "guild",
            "Guild & Privacy",
            "Checking guild connection",
            "",
            rail_color=self.theme.subtle,
            parent=parent,
        )
        self.member_guild_label = self.managed_cards["guild"]["status"]
        guild_buttons = self.managed_cards["guild"]["buttons"]
        if service.spec.action_script is not None and not self.managed_edition:
            # The managed editions carry Connect guild in the header,
            # where a new member looks first; only the full edition's
            # captions page, which has no such header, keeps it here.
            # Either way it opens the same in-app helper.
            connect_button = self.make_compact_button(
                guild_controls,
                "Connect guild",
                self.open_connect_guild,
                accent=True,
            )
            connect_button.pack(side="left", padx=(0, 7))
            guild_buttons.append(connect_button)
        consent_button = self.make_compact_button(
            guild_controls,
            "My voice consent",
            self.open_discord_consent,
        )
        consent_button.pack(side="left", padx=(0, 7))
        guild_buttons.append(consent_button)
        # A link on its own row rather than a fourth button in the action
        # row: at the managed minimum width the row fits exactly the
        # three actions it has, and a control that opens a web page reads
        # better as a link anyway. Keyboard reachable, because this is an
        # accessibility product.
        privacy_link = tk.Label(
            self.managed_cards["guild"]["content"],
            text="What your guild keeps - and what it never keeps",
            bg=self.theme.panel_alt,
            fg=self.accent,
            anchor="w",
            cursor="hand2",
            takefocus=1,
            font=self.fonts["meta"],
        )
        privacy_link.pack(fill="x", pady=(6, 0))
        privacy_link.bind("<Button-1>", lambda _e: self.open_guild_privacy())
        privacy_link.bind("<Return>", lambda _e: self.open_guild_privacy())
        privacy_link.bind("<space>", lambda _e: self.open_guild_privacy())
        if self.has_operator_console:
            console_button = self.make_compact_button(
                guild_controls,
                "Open console",
                self.open_operator_console,
            )
            console_button.pack(side="left")
            guild_buttons.append(console_button)
        self.refresh_pairing_identity()

    def build_tool_connection_card(self) -> None:
        """The raid tools keep their own connection without voice controls."""
        controls = self.build_managed_card(
            "guild",
            "Guild connection",
            "Checking guild connection",
            "Connect this app to the guild that provides your gear and talent checks."
            if self.hosted_edition
            else "Manage your guild server and its raid tools.",
            rail_color=self.accent,
        )
        self.member_guild_label = self.managed_cards["guild"]["status"]
        buttons = self.managed_cards["guild"]["buttons"]
        if not self.hosted_edition:
            connect = self.make_compact_button(
                controls, "Connect guild", self.open_connect_guild, accent=True,
            )
            connect.pack(side="left", padx=(0, 7))
            buttons.append(connect)
        if self.has_operator_console:
            console = self.make_compact_button(
                controls, "Open console", self.open_operator_console,
            )
            console.pack(side="left")
            buttons.append(console)
        self.refresh_pairing_identity()

    def build_caption_options(self, parent: tk.Frame) -> None:
        """Pair related controls at wide sizes, stack them when space is tight."""
        options = tk.Frame(parent, bg=self.theme.panel_alt)
        options.pack(fill="x", pady=(10, 0))
        self.caption_options = options
        target = self.build_caption_target_row(options)
        voices = self.build_speaker_mute_row(options)
        self.caption_option_rows = (target, voices)
        for row in self.caption_option_rows:
            row.pack_forget()
        orientation = [None]

        def arrange(event=None):
            width = event.width if event is not None else options.winfo_width()
            minimum = sum(row.winfo_reqwidth() for row in self.caption_option_rows) + 12
            wide = width >= minimum
            if orientation[0] == wide:
                return
            orientation[0] = wide
            options.grid_columnconfigure(0, weight=1, uniform="caption-options")
            options.grid_columnconfigure(1, weight=1 if wide else 0,
                                         uniform="caption-options" if wide else "")
            target.grid(row=0, column=0, sticky="nsew", padx=(0, 6 if wide else 0), pady=0)
            voices.grid(row=0 if wide else 1, column=1 if wide else 0,
                        sticky="nsew", padx=(6 if wide else 0, 0), pady=(0 if wide else 6, 0))

        options.bind("<Configure>", arrange)
        arrange()

    def build_caption_target_row(self, parent: tk.Frame) -> tk.Frame:
        """Add the one display-location control without crowding the toolbar."""
        row = tk.Frame(parent, bg=self.theme.panel)
        row.pack(fill="x", pady=(11, 0))
        copy = tk.Frame(row, bg=self.theme.panel)
        copy.pack(side="left", fill="x", expand=True, padx=(12, 8), pady=9)
        tk.Label(
            copy,
            text="Caption target",
            bg=self.theme.panel,
            fg=self.theme.text,
            anchor="w",
            font=self.fonts["body_strong"],
        ).pack(fill="x")
        self.caption_target_value = tk.Label(
            copy,
            text=caption_target_summary(self.caption_target_choice()),
            bg=self.theme.panel,
            fg=self.theme.muted,
            anchor="w",
            wraplength=165 if self.member_edition else 0,
            justify="left",
            font=self.fonts["meta"],
        )
        self.caption_target_value.pack(fill="x", pady=(2, 0))
        self.caption_target_button = self.make_compact_button(
            row,
            "Choose target",
            self.open_caption_target_picker,
            accent=False,
        )
        self.caption_target_button.pack(side="right", padx=(8, 12), pady=12)
        return row

    def build_speaker_mute_row(self, parent: tk.Frame) -> tk.Frame:
        row = tk.Frame(parent, bg=self.theme.panel)
        row.pack(fill="x", pady=(8, 0))
        copy = tk.Frame(row, bg=self.theme.panel)
        copy.pack(side="left", fill="x", expand=True, padx=(12, 8), pady=9)
        tk.Label(
            copy,
            text="Muted voices",
            bg=self.theme.panel,
            fg=self.theme.text,
            anchor="w",
            font=self.fonts["body_strong"],
        ).pack(fill="x")
        self.speaker_mute_value = tk.Label(
            copy,
            text=self.speaker_mute_summary(),
            bg=self.theme.panel,
            fg=self.theme.muted,
            anchor="w",
            wraplength=165 if self.member_edition else 0,
            justify="left",
            font=self.fonts["meta"],
        )
        self.speaker_mute_value.pack(fill="x", pady=(2, 0))
        self.speaker_mute_button = self.make_compact_button(
            row,
            "Choose voices",
            self.open_speaker_mute_picker,
            accent=False,
        )
        self.speaker_mute_button.pack(side="right", padx=(8, 12), pady=12)
        return row

    def speaker_mute_summary(self) -> str:
        if getattr(self, "preview", False):
            return "No voices muted"
        try:
            from VoiceCaptions.settings import load_settings

            count = len(load_settings().muted_speaker_ids)
        except (ImportError, OSError, ValueError):
            count = 0
        if count == 0:
            return "No voices muted"
        return f"{count} voice{'s' if count != 1 else ''} hidden on this computer"

    def refresh_speaker_mute_card(self) -> None:
        label = getattr(self, "speaker_mute_value", None)
        if label is not None:
            label.config(text=self.speaker_mute_summary())

    def open_guild_privacy(self) -> None:
        """Open the paired guild's Guild & Privacy page.

        Resolved at click time so a re-pair is always honoured, and the
        no-pairing case says so on the card instead of silently doing
        nothing - a button that does nothing reads as broken.
        """
        if getattr(self, "preview", False):
            return
        url = paired_privacy_url()
        if not url:
            label = getattr(self, "member_guild_label", None)
            if label is not None:
                label.config(
                    text="Pair with your guild first - its privacy page "
                    "lives on the guild server.",
                    fg=WARNING,
                )
            return
        try:
            webbrowser.open_new_tab(url)
        except (OSError, webbrowser.Error):
            pass

    def open_support_link(self, url: str, label: str) -> None:
        """Open an optional support link without interrupting raid controls."""
        if getattr(self, "preview", False):
            return
        if open_support_url(url):
            return
        footer = getattr(self, "footer_status", None)
        if footer is not None:
            footer.config(text="Could not open " + label + ". Try again.", fg=WARNING)

    def build_mute_toggle(
        self,
        row: tk.Frame,
        *,
        compact: bool = False,
    ) -> tk.Button | None:
        """Mute: hide caption text without stopping anything.

        For running the companion to sim while the caption window would
        otherwise sit on screen. Unlike Stop it keeps the connection and
        capture, so unmuting is instant. The tiny REC privacy tally stays
        visible while a session is active. The setting lives in the overlay
        settings file and the overlay polls its mtime, so this needs no
        restart. Its state is visible in this label so a persisted mute is
        never a mystery.
        """
        try:
            from VoiceCaptions.settings import DEFAULT_SETTINGS, load_settings
        except ImportError:
            return None

        preview = getattr(self, "preview", False)
        state = [DEFAULT_SETTINGS.muted if preview else load_settings().muted]

        def label_for(muted: bool) -> str:
            return "Show captions" if muted else "Hide captions"

        def flip() -> None:
            if preview:
                state[0] = not state[0]
                button.config(text=label_for(state[0]))
                return
            from dataclasses import replace
            from VoiceCaptions.settings import load_settings, save_settings
            current = load_settings()
            updated = replace(current, muted=not current.muted)
            if save_settings(updated):
                button.config(text=label_for(updated.muted))

        factory = self.make_compact_button if compact else self.make_button
        button = factory(row, label_for(state[0]), flip)
        button.pack(side="left", padx=(0, 7))
        return button

    def build_focus_toggle(
        self,
        row: tk.Frame,
        *,
        compact: bool = False,
    ) -> tk.Button | None:
        """Focus mode: the overlay shows tank and healer captions only.

        Deliberately strict - an unmapped speaker is dropped too, so the
        label names the roles that survive rather than the ones removed.
        The setting lives in the overlay settings file; the watch process
        polls its mtime, so flipping this needs no restart.
        """
        try:
            from VoiceCaptions.settings import DEFAULT_SETTINGS, load_settings
        except ImportError:
            return None

        preview = getattr(self, "preview", False)
        state = [
            DEFAULT_SETTINGS.focus_mode if preview else load_settings().focus_mode
        ]

        def label_for(enabled: bool) -> str:
            return "All speakers" if enabled else "Tanks + healers"

        def flip() -> None:
            if preview:
                state[0] = not state[0]
                button.config(text=label_for(state[0]))
                return
            from dataclasses import replace
            from VoiceCaptions.settings import load_settings, save_settings
            current = load_settings()
            updated = replace(current, focus_mode=not current.focus_mode)
            if save_settings(updated):
                button.config(text=label_for(updated.focus_mode))

        factory = self.make_compact_button if compact else self.make_button
        button = factory(row, label_for(state[0]), flip)
        button.pack(side="left", padx=(0, 7))
        return button

    @staticmethod
    def set_caption_display_value(field: str, value):
        """Persist one local overlay preference and return the saved value."""
        from dataclasses import replace
        from VoiceCaptions.settings import load_settings, save_settings

        current = load_settings()
        updated = replace(current, **{field: value})
        if not save_settings(updated):
            return getattr(current, field)
        return getattr(load_settings(), field)

    def caption_target_choice(self):
        """Read the overlay's shared anchor settings as a picker choice."""
        try:
            from VoiceCaptions.overlay import CaptionTargetChoice
            from VoiceCaptions.settings import load_settings
        except ImportError:
            return None
        if getattr(self, "preview", False):
            return CaptionTargetChoice()
        settings = load_settings()
        return CaptionTargetChoice(
            mode=getattr(settings, "anchor_mode", "auto"),
            executable=getattr(settings, "anchor_executable", ""),
            monitor_index=getattr(settings, "anchor_monitor", 0),
        )

    def set_caption_target(self, target) -> bool:
        """Persist a target through the overlay's anchor settings contract."""
        if target is None:
            return False
        mode, executable, monitor_index = caption_target_key(target)
        if mode == "window" and not executable:
            return False
        if mode != "window":
            executable = ""
        if mode != "monitor":
            monitor_index = 0
        if getattr(self, "preview", False):
            return True
        try:
            from dataclasses import replace
            from VoiceCaptions.overlay import CaptionTargetChoice
            from VoiceCaptions.settings import load_settings, save_settings

            current = load_settings()
            updated = replace(
                current,
                anchor_mode=mode,
                anchor_executable=executable,
                anchor_monitor=monitor_index,
            )
            if not save_settings(updated):
                return False
            saved_settings = load_settings()
            saved = CaptionTargetChoice(
                mode=getattr(saved_settings, "anchor_mode", "auto"),
                executable=getattr(saved_settings, "anchor_executable", ""),
                monitor_index=getattr(saved_settings, "anchor_monitor", 0),
            )
            return caption_target_key(saved) == (mode, executable, monitor_index)
        except (ImportError, OSError, RuntimeError, TypeError, ValueError):
            return False

    def refresh_caption_target_card(self) -> None:
        """Reflect a saved target in the Caption display card immediately."""
        label = getattr(self, "caption_target_value", None)
        if label is None:
            return
        try:
            label.config(text=caption_target_summary(self.caption_target_choice()))
        except tk.TclError:
            return

    def set_caption_hold_seconds(self, value: float) -> float:
        if getattr(self, "preview", False):
            return float(value)
        return float(
            self.set_caption_display_value("caption_hold_seconds", value)
        )

    def set_max_visible_speakers(self, value: int) -> int:
        if getattr(self, "preview", False):
            return int(value)
        return int(
            self.set_caption_display_value("max_visible_speakers", value)
        )

    def set_caption_font_size(self, value: int) -> int:
        if getattr(self, "preview", False):
            return int(value)
        return int(
            self.set_caption_display_value("caption_font_size", value)
        )

    # -- speaking back -----------------------------------------------------

    def open_speak_box(self) -> None:
        """A small box over the game: type, Enter, gone.

        Deliberately not the main window. Vemal is in a raid; summoning a
        whole companion over the fight to say four words would cost more
        than the words are worth. This takes focus for as long as it takes
        to type and hands it straight back.
        """
        if not self.caption_suite:
            return
        existing = getattr(self, "speak_box", None)
        if existing is not None and existing.winfo_exists():
            existing.deiconify()
            existing.lift()
            entry = getattr(self, "speak_entry", None)
            if entry is not None:
                entry.focus_force()
            else:
                existing.focus_force()
            return

        target_rect = (
            (
                self.root.winfo_rootx(),
                self.root.winfo_rooty(),
                self.root.winfo_width(),
                self.root.winfo_height(),
            )
            if getattr(self, "preview", False)
            else quick_chat_target_rect(self.root, self.handle())
        )
        box = tk.Toplevel(self.root)
        self.speak_box = box
        box.title("Message Group")
        box.configure(bg=self.theme.panel)
        box.attributes("-topmost", True)
        box.resizable(False, False)
        # No title bar: it is a prompt, not a window to manage.
        box.overrideredirect(True)

        frame = tk.Frame(box, bg=self.theme.panel, highlightbackground=self.theme.conversation,
                         highlightthickness=1)
        frame.pack(fill="both", expand=True)
        header = tk.Frame(frame, bg=self.theme.panel)
        header.pack(fill="x", padx=14, pady=(11, 7))
        signal = tk.Canvas(
            header,
            width=18,
            height=18,
            bg=self.theme.panel,
            highlightthickness=0,
        )
        signal.pack(side="left", padx=(0, 8))
        signal.create_oval(3, 3, 15, 15, outline=self.theme.conversation, width=2)
        signal.create_oval(7, 7, 11, 11, fill=self.theme.conversation, outline="")
        tk.Label(
            header,
            text="Message Group",
            bg=self.theme.panel,
            fg=self.theme.text,
            font=self.fonts["quick_title"],
            anchor="w",
        ).pack(side="left")
        self.make_compact_button(
            header,
            "Close",
            self.close_speak_box,
        ).pack(side="right")

        fields = tk.Frame(frame, bg=self.theme.panel)
        fields.pack(fill="x", padx=14)
        entry = tk.Entry(
            fields,
            bg=self.theme.panel_alt,
            fg=self.theme.text,
            insertbackground=self.theme.text,
            relief="flat",
            highlightthickness=1,
            highlightbackground=self.theme.border,
            highlightcolor=self.theme.accent,
            font=self.fonts["body"],
            width=52,
        )
        self.speak_entry = entry
        entry.pack(side="left", fill="x", expand=True, ipady=7)
        self.speak_send = self.make_chat_button(
            fields,
            "Send",
            self.send_spoken_text,
        )
        self.speak_send.pack(side="right", padx=(8, 0))
        # Only a failed line comes back. A line still in flight does not:
        # the box closed on Enter and the next press must be a clean start.
        if self.quick_chat_draft_text and self.quick_chat_failure_message:
            entry.insert(0, self.quick_chat_draft_text)

        meta = tk.Frame(frame, bg=self.theme.panel)
        meta.pack(fill="x", padx=14, pady=(6, 11))
        status = tk.Label(
            meta,
            text=(
                "Enter sends and closes. Captions get it at once; "
                "Discord keeps a copy."
            ),
            bg=self.theme.panel,
            fg=self.theme.muted,
            font=self.fonts["meta"],
            anchor="w",
            justify="left",
            wraplength=330,
            height=3,
        )
        self.speak_status = status
        status.pack(side="left", fill="x", expand=True)
        self.speak_counter = tk.Label(
            meta,
            text="0 / " + str(QUICK_CHAT_TEXT_LIMIT),
            bg=self.theme.panel,
            fg=self.theme.subtle,
            font=self.fonts["keycap"],
            anchor="e",
        )
        self.speak_counter.pack(side="right", padx=(12, 0))
        self.update_speak_counter()
        if self.quick_chat_draft_text and self.quick_chat_failure_message:
            status.config(text=self.quick_chat_failure_message, fg=DANGER)

        entry.bind("<Return>", lambda _event: self.send_spoken_text())
        entry.bind("<KeyRelease>", lambda _event: self.update_speak_counter())
        box.bind(
            "<Escape>",
            lambda _event: self.close_speak_box() or "break",
        )
        box.bind("<FocusOut>", self.on_speak_focus_out)

        box.update_idletasks()
        # Just above where the captions sit, so the member's eye does not
        # have to travel to a different part of the screen to type.
        width = box.winfo_width()
        height = box.winfo_height()
        x, y = quick_chat_popup_position(
            target_rect,
            (width, height),
        )
        move_toplevel(box, x, y)
        entry.focus_force()
        if not getattr(self, "preview", False):
            fade_in(box)

    def update_speak_counter(self) -> None:
        entry = getattr(self, "speak_entry", None)
        counter = getattr(self, "speak_counter", None)
        if entry is None or counter is None:
            return
        try:
            length = len(entry.get())
        except tk.TclError:
            return
        counter.config(
            text=str(length) + " / " + str(QUICK_CHAT_TEXT_LIMIT),
            fg=WARNING if length > QUICK_CHAT_TEXT_LIMIT else self.theme.subtle,
        )

    def on_speak_focus_out(self, _event) -> None:
        box = getattr(self, "speak_box", None)
        if box is not None and box.winfo_exists():
            box.after_idle(self.close_speak_box_if_focus_left)

    def close_speak_box_if_focus_left(self) -> None:
        box = getattr(self, "speak_box", None)
        if box is None or not box.winfo_exists():
            return
        try:
            focused = box.focus_get()
        except tk.TclError:
            return
        if not SettingsMenu.contains_widget(box, focused):
            self.hide_speak_box()

    def hide_speak_box(self) -> None:
        box = getattr(self, "speak_box", None)
        if box is not None and box.winfo_exists():
            box.withdraw()

    def close_speak_box(self) -> None:
        self.destroy_speak_box(clear_draft=True)

    def destroy_speak_box(self, *, clear_draft: bool) -> None:
        box = getattr(self, "speak_box", None)
        if box is not None and box.winfo_exists():
            box.destroy()
        self.speak_box = None
        self.speak_entry = None
        self.speak_status = None
        self.speak_send = None
        self.speak_counter = None
        if clear_draft:
            self.quick_chat_draft_text = ""
            self.quick_chat_request_id = ""
            self.quick_chat_failure_message = ""

    def send_spoken_text(self) -> None:
        entry = getattr(self, "speak_entry", None)
        status = getattr(self, "speak_status", None)
        if getattr(self, "preview", False):
            if status is not None:
                status.config(text="Preview only - nothing was sent.", fg=self.theme.muted)
            return
        text = entry.get().strip() if entry is not None else ""
        if not text:
            return
        if len(text) > QUICK_CHAT_TEXT_LIMIT:
            if status is not None:
                status.config(
                    text="Keep the message to 320 characters or fewer.",
                    fg=WARNING,
                )
            return

        # The same line keeps its request id, whether it is the failed draft
        # being retried or a line still in flight sent twice on a nervous
        # Enter - the relay dedups on the id, so a fresh one would post it
        # twice. Anything else is a new line and gets a new id.
        request_id = ""
        if text == self.quick_chat_draft_text:
            request_id = self.quick_chat_request_id
        if not request_id:
            for flight_id, flight_text in self.quick_chat_in_flight.values():
                if flight_text == text:
                    request_id = flight_id
                    break
        if not request_id:
            request_id = new_quick_chat_request_id()
        self.quick_chat_draft_text = text
        self.quick_chat_request_id = request_id
        self.quick_chat_failure_message = ""
        self.quick_chat_attempt_sequence += 1
        attempt_id = self.quick_chat_attempt_sequence
        self.quick_chat_in_flight[attempt_id] = (request_id, text)
        # Gone before the relay is even asked. The member is mid-fight and
        # the round trip includes a Discord post; his words: "there's a
        # sending delay but the game is still happening." The draft stays so
        # a failure can hand the line back.
        self.destroy_speak_box(clear_draft=False)
        self.deliver_spoken_text(text, request_id, attempt_id)

    def deliver_spoken_text(
        self,
        text: str,
        request_id: str,
        attempt_id: int,
    ) -> None:
        """Send off the Tk thread and return only a redacted result."""

        def send() -> None:
            self.quick_chat_results.put(
                QueuedQuickChatResult(
                    attempt_id,
                    transmit_quick_chat(text, request_id),
                )
            )

        threading.Thread(
            target=send,
            name="VemalsEarsQuickChat",
            daemon=True,
        ).start()

    def drain_quick_chat_results(self) -> None:
        while True:
            try:
                queued = self.quick_chat_results.get_nowait()
            except queue.Empty:
                return
            result = queued.result
            record = self.quick_chat_in_flight.pop(queued.attempt_id, None)
            if record is None or record[0] != result.request_id:
                continue
            request_id, text = record
            if result.success:
                # Silent on purpose: the caption landing on screen is the
                # confirmation, and a tray balloon over a raid is noise.
                if self.quick_chat_draft_text == text:
                    self.quick_chat_draft_text = ""
                    self.quick_chat_request_id = ""
                    self.quick_chat_failure_message = ""
                continue
            # The box is long gone, so the failed line becomes the draft and
            # the next hotkey press hands it back with the reason.
            self.quick_chat_draft_text = text
            self.quick_chat_request_id = request_id
            self.quick_chat_failure_message = result.message
            self.notify("Quick chat could not send", result.message)
            box = getattr(self, "speak_box", None)
            if box is None or not box.winfo_exists():
                continue
            # A box is already up (or hidden by a focus change). Fill it only
            # when that does not stamp on a line the member is typing.
            entry = getattr(self, "speak_entry", None)
            status = getattr(self, "speak_status", None)
            if entry is None:
                continue
            try:
                current = entry.get().strip()
            except tk.TclError:
                continue
            if current and current != text:
                continue
            if not current:
                entry.insert(0, text)
                self.update_speak_counter()
            if status is not None:
                status.config(text=result.message, fg=DANGER)

    def refresh_member_caption_button(self) -> None:
        """Keep the label describing what pressing it will do."""
        button = getattr(self, "member_caption_button", None)
        service = getattr(self, "member_caption_service", None)
        if button is None or service is None:
            return
        button.config(
            text="Resume caption feed" if service.paused_by_member
            else "Pause caption feed"
        )

    # -- updates -----------------------------------------------------------

    def build_update_banner(self) -> None:
        """A prominent update card that only exists when there is something to say."""
        self.update_tick = 0
        self.update_offer = None
        self.update_busy = False
        self.update_banner = tk.Frame(self.main, bg=self.accent)
        update_inner = tk.Frame(self.update_banner, bg=self.theme.panel_alt)
        update_inner.pack(fill="x", padx=1, pady=1)
        self.update_message = tk.Label(
            update_inner,
            text="",
            bg=self.theme.panel_alt,
            fg=self.theme.text,
            anchor="w",
            font=self.fonts["body"],
            justify="left",
            wraplength=520,
        )
        self.update_message.pack(side="left", fill="x", expand=True,
                                 padx=(14, 8), pady=11)
        self.update_button = self.make_primary_button(
            update_inner, "Update now", self.install_update
        )
        self.update_button.pack(side="right", padx=(0, 12), pady=8)

    def refresh_update_banner(self) -> None:
        # Caption update offers belong only to the standalone Vemal's Ears app.
        if not self.member_edition:
            self.update_offer = None
            self.update_banner.pack_forget()
            return
        if self.update_busy:
            return
        try:
            # Imported here like the other GuildHub entry points: a copy
            # without the caption client must still open its window.
            from GuildHub.client import read_update_notice

            offer = read_update_notice()
        except Exception:
            offer = None
        self.update_offer = offer
        if offer is None:
            self.update_banner.pack_forget()
            return
        self.update_message.configure(
            text=(
                f"Vemal's Ears {offer['version']} is available. "
                "Updating closes this window and reopens the installer."
            )
        )
        self.update_banner.pack(
            fill="x",
            padx=24,
            pady=(12, 0),
            before=self.service_section,
        )

    def install_update(self) -> None:
        """Download, verify, then hand the member the installer.

        Nothing is run until the bytes match the checksum the guild
        server gave over its authenticated connection, and the member
        still clicks through the installer themselves - which is what
        keeps an unsigned build honest.
        """
        offer = self.update_offer
        if offer is None or self.update_busy:
            return
        import tempfile
        from tkinter import messagebox

        from GuildHub.client import download_verified_update

        self.update_busy = True
        self.update_button.configure(state="disabled")
        self.update_message.configure(text="Downloading the update...")
        self.root.update_idletasks()
        try:
            target = Path(tempfile.gettempdir()) / "Vemals-Ears-Setup.exe"
            download_verified_update(offer, target)
        except Exception as error:
            self.update_message.configure(text="The update could not be installed.")
            self.update_button.configure(state="normal")
            self.update_busy = False
            messagebox.showerror("Vemal's Ears", str(error))
            return
        # The installer closes this copy. This copy does not close itself.
        #
        # Both of the obvious orderings were wrong. Launching and then
        # quitting raced our own shutdown - stopping the services alone
        # can take tens of seconds - against an installer that asked for
        # the app mutex within its first, so the member was told to close
        # all instances of a companion already on its way out, and the
        # update never applied. Standing down before the launch won that
        # race, but paid for it by going down before knowing the install
        # worked: a launch that then failed left the member with stopped
        # services and no captions.
        #
        # Xenon's agent updater settled the same tradeoff on real
        # machines, and the reasoning carries: whoever exits first commits
        # before knowing the outcome. So the installer owns the closing.
        # It does that at PrepareToInstall, and [Setup] no longer declares
        # AppMutex, so there is no startup check left to race. A cancelled
        # or failed install now leaves this copy exactly as it was - still
        # running, still captioning.
        try:
            os.startfile(str(target))  # noqa: S606 - verified above
        except OSError as error:
            self.update_message.configure(text="The update could not be started.")
            self.update_button.configure(state="normal")
            self.update_busy = False
            messagebox.showerror("Vemal's Ears", str(error))
            return
        # Stays busy: the installer is what ends this process, and
        # re-offering the button meanwhile would start a second one.
        self.update_message.configure(
            text="The installer is open. It closes this window when you continue."
        )

    def make_button(self, parent: tk.Frame, label: str, command) -> tk.Button:
        if self.member_edition:
            return VelvetButton(parent, label, command, theme=self.theme,
                                font=self.fonts["button"])
        button = StandardButton(
            parent,
            label,
            command,
            font=self.fonts["button"],
        )
        return self.theme_button(button)

    def make_compact_button(
        self,
        parent: tk.Frame,
        label: str,
        command,
        *,
        accent: bool = False,
        text_color: str | None = None,
    ) -> tk.Button:
        if self.member_edition:
            quiet = label in {"Exit", "Show", "Clear", "Done", "Close", "Patreon", "PayPal"}
            return VelvetButton(
                parent, label, command, theme=self.theme,
                variant="primary" if accent else "quiet" if quiet else "secondary",
                compact=True, font=self.fonts["button"], text_color=text_color,
            )
        button = CompactButton(
            parent,
            label,
            command,
            accent=accent,
            accent_color=self.accent,
            text_color=text_color,
            font=self.fonts["button"],
        )
        return self.theme_button(button, accent=accent, text_color=text_color)

    def theme_button(self, button: tk.Button, *, accent=False, text_color=None):
        """Keep resting, hover, and keyboard-focus colors in this app's theme."""
        if not self.member_edition:
            return button
        background = self.theme.panel_hover if accent else self.theme.panel
        foreground = text_color or (self.accent if accent else self.theme.text)
        button.configure(
            bg=background,
            fg=foreground,
            activebackground=self.theme.panel_hover,
            activeforeground=self.theme.text,
            highlightbackground=self.accent if accent else self.theme.border,
            highlightcolor=self.accent,
        )
        button.bind("<Enter>", lambda _e: button.configure(
            bg=self.theme.panel_hover, fg=self.theme.text,
        ))
        button.bind("<Leave>", lambda _e: button.configure(
            bg=background, fg=foreground,
        ))
        return button

    def make_primary_button(self, parent: tk.Frame, label: str, command) -> tk.Button:
        if self.member_edition:
            return VelvetButton(parent, label, command, theme=self.theme,
                                variant="primary", font=self.fonts["button"])
        return PrimaryButton(
            parent,
            label,
            command,
            accent=self.accent,
            accent_hover=self.accent_hover,
            font=self.fonts["button"],
        )

    def make_chat_button(self, parent: tk.Frame, label: str, command) -> tk.Button:
        """Warm emphasis is reserved for a person speaking to the raid."""
        if self.member_edition:
            return VelvetButton(parent, label, command, theme=self.theme,
                                variant="conversation", font=self.fonts["button"],
                                icon="chat")
        button = ChatButton(
            parent,
            label,
            command,
            bg=self.theme.conversation,
            font=self.fonts["button"],
        )
        button.configure(activebackground=self.theme.conversation_hover)
        button.bind("<Enter>", lambda _e: button.configure(bg=self.theme.conversation_hover))
        button.bind("<Leave>", lambda _e: button.configure(bg=self.theme.conversation))
        return button

    def open_discord_consent(self) -> None:
        from tkinter import messagebox

        if getattr(self, "preview", False):
            messagebox.showinfo(
                "Consent in Discord",
                "Preview only - Discord was not opened.",
                parent=self.root,
            )
            return

        result = prepare_discord_consent()
        if not result.paired:
            message = (
                "Pair this computer with your guild first. Then this button can "
                "open the correct Discord server."
            )
        elif result.discord_opened:
            message = (
                "Discord has been asked to open " + result.guild_name + ".\n\n"
                "Choose Manage my consent on the active Vemal's Ears raid "
                "message. New speakers start OFF.\n\n"
                "When the Vemal's Ears Bot is present, it should be assumed "
                "someone is reading speech ONLY if the speaker has consented."
            )
        elif result.browser_opened:
            message = (
                "Discord Desktop was unavailable, so " + result.guild_name + " opened "
                "in your browser.\n\n"
                "In Discord, choose Manage my consent on the active Vemal's Ears "
                "raid message. New speakers start OFF.\n\n"
                "When the Vemal's Ears Bot is present, it should be assumed "
                "someone is reading speech ONLY if the speaker has consented."
            )
        else:
            message = (
                "Open " + result.guild_name + " in Discord, then choose Manage my "
                "consent on the active Vemal's Ears raid message. Your "
                "personal toggle shows whether consent is ON or OFF.\n\n"
                "When the Vemal's Ears Bot is present, it should be assumed "
                "someone is reading speech ONLY if the speaker has consented."
            )
        message += (
            "\n\nIf the toggle is not visible, ask the raid lead to start a "
            "fresh Vemal's Ears session."
        )
        messagebox.showinfo("Consent in Discord", message, parent=self.root)

    def open_discord_class_picker(self) -> None:
        from tkinter import messagebox

        if getattr(self, "preview", False):
            messagebox.showinfo(
                "Choose your class",
                "Preview only - Discord was not opened.",
                parent=self.root,
            )
            return

        result = prepare_discord_class_picker()
        guild_name = result.guild_name or "your guild"
        if not result.paired:
            message = (
                "Pair this computer with your guild first. Then this button can "
                "open the correct Discord server."
            )
        elif result.discord_opened:
            message = "Discord has been asked to open " + guild_name + "."
        elif result.browser_opened:
            message = (
                "Discord Desktop was unavailable, so " + guild_name + " opened "
                "in your browser."
            )
        else:
            message = "Open " + guild_name + " in Discord."
        if result.paired:
            message += (
                "\n\nIn the active Vemal's Ears raid message, select "
                '"Choose your class".\n\n'
                "If you do not see Choose your class, run /vem map in Discord."
            )
        messagebox.showinfo("Choose your class", message, parent=self.root)

    def open_operator_console(self) -> None:
        from tkinter import messagebox

        if launch_operator_console(member_edition=self.member_edition):
            return
        messagebox.showerror(
            "Vemal's Ears",
            "The local VE console could not be opened. Check that the guild "
            "host services are installed, then try again.",
            parent=self.root,
        )

    def append(self, spec: ServiceSpec, text: str) -> None:
        clean = text.rstrip()
        self.log.config(state="normal")
        self.log.insert("end", f"{spec.label:<12} ", spec.key)
        self.log.insert("end", clean + "\n", "body")
        # Drop the oldest lines rather than keeping the whole session.
        # Nothing here ever deleted, so a long raid night grew this
        # widget until the app was closed.
        overflow = int(float(self.log.index("end-1c"))) - MAX_ACTIVITY_LINES
        if overflow > 0:
            self.log.delete("1.0", str(overflow + 1) + ".0")
        self.log.see("end")
        self.log.config(state="disabled")
        self.activity_count += 1
        # The friendly sentence this line would produce is the same one the
        # service card is already showing - Service.drain reads the very same
        # marker table. Deriving it twice and printing it twice is what put
        # the same words on screen in two places, so the summary is gone and
        # only the log itself is written here.

    def tick(self) -> None:
        self.drain_quick_chat_results()
        for service in self.services:
            service.drain()
            service.request_external_status()
            if (
                service.process is None
                and not service.paused_by_member
                and service.restart_at
                and time.monotonic() >= service.restart_at
            ):
                service.start(automatic=True)
        # Cheap: a stat, and only a read when the file has changed. The
        # caption client rewrites it on every connection, so it cannot
        # describe a version that is no longer being offered. Hosted mode
        # explicitly ignores the captions-only installer in the method.
        if self.update_tick % 25 == 0:
            self.refresh_update_banner()
        self.update_tick += 1
        if self.consume_pairing_signal():
            self.refresh_pairing_identity()
            self.reset_quick_chat_profile_for_pairing()
        self.drain_quick_chat_profile_results()
        if self.consume_quit_request():
            return
        try:
            while True:
                command = self.tray.commands.get_nowait()
                if command == "speak":
                    self.open_speak_box()
                elif command == "show":
                    self.show()
                elif command == "exit":
                    self.quit_now()
                    return
        except queue.Empty:
            pass
        self.root.after(120, self.tick)

    # -- showing and closing -----------------------------------------------

    def show(self) -> None:
        self.hidden = False
        try:
            # minimize() uses raw ShowWindow on the borderless window
            # (Tk refuses to iconify it), so Tk's own state can be out of
            # sync and deiconify() alone may not restore it.
            handle = self.handle()
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            if user32.IsIconic(wintypes.HWND(handle)):
                user32.ShowWindow(wintypes.HWND(handle), SW_RESTORE)
        except (OSError, AttributeError, ValueError, tk.TclError):
            pass
        self.root.deiconify()
        if self.custom_chrome:
            claim_taskbar_button(self.handle())
        self.root.lift()
        self.root.focus_force()
        try:
            # The in-process half of the handoff: the second launch
            # granted us its foreground rights with
            # AllowSetForegroundWindow, and this is what spends them.
            user32.SetForegroundWindow(wintypes.HWND(self.handle()))
        except (OSError, AttributeError, ValueError, tk.TclError):
            pass

    def notify(self, title: str, text: str) -> bool:
        """One short notice, in a window this app places itself.

        Windows decides where a tray balloon goes and an application
        cannot move it, which is why this exists: Shino needed the
        minimise notice higher, because it covered things by the clock.
        The balloon stays as the fallback for the case Tk cannot draw -
        no window at all is worse than one in the wrong place.
        """
        existing = getattr(self, "toast", None)
        try:
            if existing is not None and existing.winfo_exists():
                existing.dismiss()
        except tk.TclError:
            pass
        self.toast = None
        if getattr(self, "preview", False):
            return False
        try:
            self.toast = Toast(self, title, text)
            return True
        except (tk.TclError, AttributeError, RuntimeError):
            # No usable Tk - a window built without one, or a display
            # that has gone away. The balloon is worse placed but it is
            # still a notice, and silence here is how a member never
            # learns their message did not send.
            self.toast = None
            return self.tray.notify(title, text)

    def hide(self) -> None:
        self.close_menu()
        self.hidden = True
        self.root.withdraw()
        if not self.announced_tray:
            self.announced_tray = True
            self.notify(
                self.product_name,
                "Still running down here. Right click for Exit.",
            )

    def on_close_button(self) -> None:
        if getattr(self, "preview", False):
            self.root.destroy()
            return
        if not self.settings.get("minimize_to_taskbar"):
            self.quit_now()
        elif self.tray_has_icon():
            self.hide()
        else:
            # Hiding with no icon to click would leave the window with no way
            # back, so fall back to the taskbar rather than disappear.
            self.minimize()

    def release_for_shutdown(self) -> None:
        """Give up everything that makes this copy count as running.

        Quitting and updating both need the same thing to be true - no
        services, no tray, no lock file, no app mutex - so both go
        through here rather than keeping two lists that can drift. The
        mutex is last because it is the flag an installer actually reads,
        and it should not come down while the services it stands for are
        still up.
        """
        self.close_menu()
        for service in self.services:
            service.stop()
        self.tray.stop()
        try:
            if LOCK.is_file() and LOCK.read_text(encoding="ascii").strip() == str(os.getpid()):
                LOCK.unlink()
        except OSError:
            pass
        release_instance_mutex()

    def quit_now(self) -> None:
        if getattr(self, "preview", False):
            self.close_menu()
            self.root.destroy()
            return
        self.release_for_shutdown()
        from GuildHub.windows_taskbar import clear_taskbar_identity
        clear_taskbar_identity(window_handle(self.root))
        self.root.destroy()


def main(*, edition: str | None = None) -> int:
    edition = MEMBER_EDITION if edition is None else normalize_edition(edition)
    configure_application(edition)
    root = tk.Tk()
    root.withdraw()
    from tkinter import messagebox
    product_name = edition_product_name(edition)

    def confirm_pairing(guild_name: str, server_host: str, replacing: bool) -> bool:
        replacement = (
            "\n\nThis will replace the guild currently saved on this computer."
            if replacing
            else ""
        )
        return bool(
            messagebox.askyesno(
                "Pair " + product_name,
                "Pair this computer with:\n\n"
                + guild_name
                + "\n\nGuild server:\n"
                + server_host
                + replacement
                + "\n\nThe one-use invitation has already been accepted by the "
                "guild server and cannot be reused if you cancel.",
                parent=root,
            )
        )

    pairing = consume_pairing_argument(
        sys.argv[1:],
        confirm_pairing=confirm_pairing,
    )
    mutex_held = claim_instance_mutex()
    other_pid = running_instance_pid()
    if mutex_held or other_pid is not None:
        if pairing is not None:
            if pairing.success:
                notify_running_companion()
            messagebox.showinfo(product_name, pairing.message)
            return 0
        # The running app holds the window; this process, just launched
        # by the user, holds the foreground rights. Hand them over and
        # raise it. Only when that observably fails does the messagebox
        # appear - and for the hosted edition it always does, because
        # raising the OTHER product's window while silently not starting
        # the one that was clicked would be worse than a message.
        if raise_running_companion(other_pid):
            return 0
        messagebox.showinfo(
            product_name,
            instance_conflict_message(edition),
        )
        return 0
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        LOCK.write_text(str(os.getpid()), encoding="ascii")
    except OSError:
        pass
    # The full local tools need the one-use carrier before WoW launches.
    # Hosted workers own their first provisioning pass: doing it here would
    # make a carrier created after WoW opened look pre-existing to the worker.
    result_carrier_available = True
    if normalize_edition(edition) == FULL_EDITION:
        result_carrier_available = provision_downtime_results_carrier(edition)
    try:
        if PAIR_SIGNAL.is_file():
            PAIR_SIGNAL.unlink()
    except OSError:
        pass
    try:
        # A quit request left over from a crashed uninstall must not kill
        # this fresh launch moments after it starts.
        if QUIT_SIGNAL.is_file():
            QUIT_SIGNAL.unlink()
    except OSError:
        pass
    icon = ensure_icon(edition)
    pairing_links_ready = register_pairing_links(icon)
    migrate_autostart()
    CompanionWindow(
        root,
        edition=edition,
        result_carrier_available=result_carrier_available,
    )
    if pairing is not None:
        root.after(250, lambda: messagebox.showinfo(product_name, pairing.message))
    if not pairing_links_ready:
        root.after(
            350,
            lambda: messagebox.showwarning(
                product_name,
                "Windows could not register guild invitation links. Use Connect "
                "guild in " + product_name + " to paste the invitation instead.",
            ),
        )
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
