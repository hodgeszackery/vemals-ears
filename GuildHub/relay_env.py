"""The relay's configuration file, shared by the service and the console.

One KEY=VALUE file under LOCALAPPDATA rather than environment blocks baked
into a service registration, because the operator console edits it live and
a service restart picks it up - reconfiguring must never mean reinstalling.
Secrets stay as *_FILE paths; no secret value ever lives in this file.
"""

from __future__ import annotations

import os
from pathlib import Path

STATE_DIR_NAME = "VemalsEars"
ENV_FILE_NAME = "relay.env"

# The keys the console may edit, in display order. Everything else in the
# file survives round-trips untouched.
EDITABLE_KEYS = (
    "VEMALS_EARS_GUILD_ID",
    "VEMALS_EARS_GUILD_NAME",
    "VEMALS_EARS_PORT",
    "VEMALS_EARS_PUBLIC_SERVER_URL",
    "VEMALS_EARS_PUBLIC_HTTP_URL",
    "VEMALS_EARS_CLIENT_DOWNLOAD_URL",
    # The version the published download IS. Members are offered an update
    # by comparing this against what they report, so it has to be settable
    # from the console - otherwise publishing a new build means editing an
    # elevated file by hand, and the update notice quietly never fires.
    "VEMALS_EARS_CLIENT_VERSION",
)


def state_dir() -> Path:
    # Services run as LocalSystem, whose LOCALAPPDATA is the systemprofile -
    # a profile that holds none of our state. The installer therefore pins
    # the real location through this variable; interactive runs fall back to
    # the user profile as before.
    explicit = os.environ.get("VEMALS_EARS_STATE_DIR", "").strip()
    if explicit:
        return Path(explicit)
    root = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    return root / STATE_DIR_NAME


def env_path() -> Path:
    return state_dir() / ENV_FILE_NAME


def default_environment() -> dict[str, str]:
    secrets = state_dir() / "secrets"
    return {
        "VEMALS_EARS_PEPPER_FILE": str(secrets / "pepper"),
        "VEMALS_EARS_PUBLISHER_TOKEN_FILE": str(secrets / "publisher-token"),
        "VEMALS_EARS_DATABASE": str(state_dir() / "relay" / "guild-hub.sqlite3"),
        "VEMALS_EARS_BIND": "127.0.0.1",
        "VEMALS_EARS_PORT": "8770",
        "VEMALS_EARS_GUILD_ID": "REPLACE_WITH_DISCORD_GUILD_ID",
        "VEMALS_EARS_GUILD_NAME": "Vemal's Ears",
        "VEMALS_EARS_PUBLIC_SERVER_URL": "wss://ears.xenon.solutions",
        "VEMALS_EARS_PUBLIC_HTTP_URL": "https://ears.xenon.solutions",
        # The relay serves the member download itself: the repository is
        # private, so a GitHub release URL 404s for exactly the people it
        # exists for.
        "VEMALS_EARS_CLIENT_DOWNLOAD_URL": "https://ears.xenon.solutions/download",
        # Empty means "advertise nothing", which is the safe default: an
        # update offer nobody can act on is worse than no offer.
        "VEMALS_EARS_CLIENT_VERSION": "",
        # The installer, not the source archive. Members are older and
        # were handed this link by someone they know: "run the installer"
        # is a step they have taken before, while "extract the zip, find
        # the .cmd inside, double-click that one" is not. The installer
        # asks for no administrator password - it installs for the
        # current user - and it registers the pairing link handler, which
        # the archive can only do once the app has been run by hand.
        "VEMALS_EARS_DOWNLOAD_FILE": str(
            state_dir() / "downloads" / "Vemals-Ears-Setup.exe"
        ),
        # The worker's Discord credentials, as secret FILES the console
        # writes - a LocalSystem service cannot read a user's credential
        # vault, and no secret value belongs in this file.
        "VEMALS_EARS_DISCORD_APPLICATION_ID_FILE": str(
            secrets / "discord-application-id"
        ),
        "VEMALS_EARS_DISCORD_BOT_TOKEN_FILE": str(secrets / "discord-bot-token"),
    }


def load(path: Path | None = None) -> dict[str, str]:
    """Read the env file; a missing file reads as the defaults."""
    target = path or env_path()
    values = dict(default_environment())
    try:
        raw = target.read_text(encoding="ascii")
    except OSError:
        return values
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key:
            values[key] = value
    return values


def save(values: dict[str, str], path: Path | None = None) -> None:
    """Atomically write the file, plain ASCII, keys sorted stably."""
    target = path or env_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Vemal's Ears relay configuration. Edited by the console;"]
    lines.append("# the relay service reads it at startup. Secrets are file")
    lines.append("# PATHS only - never values.")
    for key in sorted(values):
        value = str(values[key])
        if any(ord(ch) < 32 or ord(ch) > 126 for ch in key + value):
            raise ValueError("relay.env accepts printable ASCII only: " + key)
        lines.append(key + "=" + value)
    body = "\n".join(lines) + "\n"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(body, encoding="ascii")
    os.replace(temporary, target)


def apply(values: dict[str, str] | None = None) -> dict[str, str]:
    """Load the file into os.environ for this process; returns what applied."""
    resolved = values if values is not None else load()
    for key, value in resolved.items():
        os.environ[key] = value
    return resolved
