"""Create a new host's private configuration and prepare its GPU speech runtime."""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
from urllib.parse import urlsplit

from GuildHub import relay_env
from VoiceCaptions.credentials import (
    CredentialValidationError, build_bot_install_url, validate_credentials,
)
from VoiceCaptions import runtime_installer

ROOT = Path(__file__).resolve().parents[1]
_SNOWFLAKE = re.compile(r"[0-9]{17,20}")
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


class SetupError(ValueError):
    """A host setup step could not be completed."""


def state_path(value: Path) -> Path:
    if not value.is_absolute():
        raise SetupError("Choose an absolute state directory outside the extracted package.")
    result = value.resolve()
    if result == Path(result.anchor) or result.is_relative_to(ROOT):
        raise SetupError("Choose a dedicated state directory outside the extracted package.")
    if any(ord(char) < 32 or ord(char) > 126 for char in result.as_posix()):
        raise SetupError("The state directory must use printable ASCII characters.")
    return result


def hostname(value: str) -> str:
    value = value.strip().lower()
    labels = value.split(".")
    if (len(value) > 253 or len(labels) < 2
            or not all(_HOST_LABEL.fullmatch(label) for label in labels)):
        raise SetupError("Enter a public DNS hostname, such as ears.example.org, without a URL path.")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    raise SetupError("Use a public DNS hostname rather than an IP address.")


def download_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.fragment or parsed.port not in (None, 443)
                or any(ord(char) < 33 or ord(char) > 126 for char in value)):
            raise ValueError
    except ValueError:
        raise SetupError("The client download URL must be a public HTTPS URL.") from None
    return value


def protect_directory(path: Path) -> None:
    """Remove inherited access before any credential is written."""
    if os.name != "nt":
        path.chmod(0o700)
        return
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    identity = subprocess.run(
        ["whoami.exe", "/user", "/fo", "csv", "/nh"],
        capture_output=True, text=True, check=True, creationflags=flags,
    )
    match = re.search(r"S-1-[0-9-]+", identity.stdout)
    if match is None:
        raise SetupError("Could not determine the Windows account for the state directory.")
    result = subprocess.run(
        ["icacls.exe", str(path), "/inheritance:r", "/grant:r",
         "*" + match.group() + ":(OI)(CI)F", "*S-1-5-18:(OI)(CI)F",
         "*S-1-5-32-544:(OI)(CI)F"],
        capture_output=True, text=True, creationflags=flags,
    )
    if result.returncode:
        raise SetupError("Could not restrict the state directory's Windows permissions.")


def create_configuration(
    state: Path, *, public_hostname: str, guild_id: str, application_id: str,
    bot_token: str, guild_name: str = "Vemal's Ears", port: int = 8770,
    client_download_url: str | None = None,
) -> dict[str, str]:
    state = state_path(state)
    domain = hostname(public_hostname)
    if not _SNOWFLAKE.fullmatch(guild_id):
        raise SetupError("The Discord server ID must contain 17 to 20 digits.")
    if (not guild_name.strip() or len(guild_name) > 80
            or any(ord(char) < 32 or ord(char) > 126 for char in guild_name)):
        raise SetupError("The guild name must contain 1 to 80 printable ASCII characters.")
    if not 1 <= port <= 65535:
        raise SetupError("The relay port must be between 1 and 65535.")
    try:
        credentials = validate_credentials(application_id, bot_token)
    except CredentialValidationError as error:
        raise SetupError(str(error)) from None
    if any(ord(char) < 33 or ord(char) > 126 for char in credentials.bot_token):
        raise SetupError("The Discord bot token must contain printable ASCII without spaces.")
    client_url = download_url(client_download_url or "https://" + domain + "/download")
    # A new directory avoids accidentally replacing an existing host's database,
    # secrets or inherited permissions. Reruns use prepare, never configure.
    if state.exists():
        raise SetupError("State directory already exists. Choose a new directory; existing hosts are never overwritten.")
    private = {
        "pepper": secrets.token_urlsafe(48),
        "publisher-token": secrets.token_urlsafe(48),
        "discord-application-id": credentials.application_id,
        "discord-bot-token": credentials.bot_token,
    }
    values = {
        "VEMALS_EARS_BIND": "127.0.0.1",
        "VEMALS_EARS_PORT": str(port),
        "VEMALS_EARS_GUILD_ID": guild_id,
        "VEMALS_EARS_GUILD_NAME": guild_name.strip(),
        "VEMALS_EARS_PUBLIC_SERVER_URL": "wss://" + domain,
        "VEMALS_EARS_PUBLIC_HTTP_URL": "https://" + domain,
        "VEMALS_EARS_CLIENT_DOWNLOAD_URL": client_url,
        "VEMALS_EARS_CLIENT_VERSION": "",
        "VEMALS_EARS_DOWNLOAD_FILE": (state / "downloads/Vemals-Ears-Setup.exe").as_posix(),
        "VEMALS_EARS_DATABASE": (state / "relay/guild-hub.sqlite3").as_posix(),
        "VEMALS_EARS_PEPPER_FILE": (state / "secrets/pepper").as_posix(),
        "VEMALS_EARS_PUBLISHER_TOKEN_FILE": (state / "secrets/publisher-token").as_posix(),
        "VEMALS_EARS_DISCORD_APPLICATION_ID_FILE": (state / "secrets/discord-application-id").as_posix(),
        "VEMALS_EARS_DISCORD_BOT_TOKEN_FILE": (state / "secrets/discord-bot-token").as_posix(),
        "VEMALS_EARS_CONNECTION_LIMIT": "256",
        "VEMALS_EARS_REVALIDATION_SECONDS": "15",
        "VEMALS_EARS_HOUSEKEEPING_SECONDS": "3600",
        "VEMALS_EARS_UNUSED_DEVICE_SECONDS": "172800",
        "VEMALS_EARS_IDLE_DEVICE_SECONDS": "2592000",
        "VEMALS_EARS_REVOKED_RETENTION_SECONDS": "2592000",
    }
    state.parent.mkdir(parents=True, exist_ok=True)
    # Build private state beside its final location, then publish it with one
    # rename. A failed write/ACL check never strands a half-configured host at
    # the fixed path, and a retry never needs to remove existing credentials.
    with tempfile.TemporaryDirectory(prefix=".VemalsEarsHost-", dir=state.parent) as folder:
        staging = Path(folder)
        protect_directory(staging)
        for relative in ("secrets", "relay", "downloads", "logs", "consent", "profile/WoWVoiceCaptions"):
            (staging / relative).mkdir(parents=True, exist_ok=True)
        for name, value in private.items():
            target = staging / "secrets" / name
            with target.open("x", encoding="ascii", newline="\n") as stream:
                stream.write(value + "\n")
            if os.name != "nt":
                target.chmod(0o600)
        relay_env.save(values, staging / "relay.env")
        (staging / "Caddyfile").write_text(
            "# Run on this Windows host. Point public DNS here and allow TCP 80/443.\n"
            + domain + " {\n    reverse_proxy 127.0.0.1:" + str(port) + "\n}\n",
            encoding="ascii",
        )
        if state.exists():
            raise SetupError("Another setup created this host state. Reopen the Host app.")
        staging.rename(state)
    return values


def prepare_runtime(state: Path) -> None:
    state = state_path(state)
    if not (state / "relay.env").is_file():
        raise SetupError("Configure this host before preparing its runtime.")
    # cuda first, then vulkan (only once its engine is pinned); cpu means no
    # usable GPU, which is not a host.
    if runtime_installer.select_acceleration() not in runtime_installer.GPU_ACCELERATIONS:
        raise SetupError(runtime_installer.gpu_requirement())
    outcome = runtime_installer.install_voice_dependencies(
        state / "profile/WoWVoiceCaptions/runtime",
        adapter_dir=ROOT / "VoiceCaptions/discord_adapter",
        progress=lambda update: print(update.message, flush=True),
    )
    if outcome.runtime.acceleration not in runtime_installer.GPU_ACCELERATIONS:
        raise SetupError("GPU runtime preparation failed. Compatibility mode is not a completed GPU host setup; retry prepare.")
    if not outcome.adapter.ready:
        raise SetupError(outcome.adapter.detail)
    print("Speech runtime is ready with "
          + runtime_installer.acceleration_label(outcome.runtime.acceleration)
          + " acceleration; Discord dependencies are ready.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-directory", required=True, type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    configure = sub.add_parser("configure", help="Create a new host; prompt privately for its bot token")
    configure.add_argument("--hostname", required=True)
    configure.add_argument("--guild-id", required=True)
    configure.add_argument("--application-id", required=True)
    configure.add_argument("--guild-name", default="Vemal's Ears")
    configure.add_argument("--port", type=int, default=8770)
    configure.add_argument("--client-download-url")
    sub.add_parser("prepare", help="Download and verify the pinned GPU speech runtime and Discord dependencies")
    args = parser.parse_args(argv)
    try:
        state = state_path(args.state_directory)
        if args.command == "prepare":
            prepare_runtime(state)
        else:
            if state.exists():
                raise SetupError("State directory already exists. Choose a new directory.")
            # Refuse terminal-less input; getpass otherwise falls back to echoing.
            import sys
            if not sys.stdin.isatty():
                raise SetupError("Run configure in an interactive terminal to enter the bot token privately.")
            token = getpass.getpass("Discord bot token (hidden): ")
            create_configuration(
                state, public_hostname=args.hostname, guild_id=args.guild_id,
                application_id=args.application_id, bot_token=token,
                guild_name=args.guild_name, port=args.port,
                client_download_url=args.client_download_url,
            )
            print("Host configuration created: " + str(state / "relay.env"))
            print("Install your bot in your Discord server: " + build_bot_install_url(args.application_id))
        return 0
    except (SetupError, OSError, subprocess.SubprocessError,
            runtime_installer.RuntimeInstallError) as error:
        parser.exit(2, "Host setup: " + str(error) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
