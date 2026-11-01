"""Run the standalone host with an explicitly selected configuration."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import socket
import sys
from urllib.parse import urlsplit

from GuildHub import relay_env


REQUIRED_KEYS = (
    "VEMALS_EARS_GUILD_ID",
    "VEMALS_EARS_GUILD_NAME",
    "VEMALS_EARS_BIND",
    "VEMALS_EARS_PORT",
    "VEMALS_EARS_PUBLIC_SERVER_URL",
    "VEMALS_EARS_PUBLIC_HTTP_URL",
    "VEMALS_EARS_CLIENT_DOWNLOAD_URL",
    "VEMALS_EARS_DATABASE",
    "VEMALS_EARS_PEPPER_FILE",
    "VEMALS_EARS_PUBLISHER_TOKEN_FILE",
    "VEMALS_EARS_DISCORD_APPLICATION_ID_FILE",
    "VEMALS_EARS_DISCORD_BOT_TOKEN_FILE",
)
STANDALONE = "standalone"
REMOTE_WORKER = "remote-worker"
LOCAL_RELAY_KEYS = (
    "VEMALS_EARS_BIND", "VEMALS_EARS_PORT", "VEMALS_EARS_DATABASE",
    "VEMALS_EARS_PEPPER_FILE", "VEMALS_EARS_DOWNLOAD_FILE",
)
REMOTE_REQUIRED_KEYS = tuple(key for key in REQUIRED_KEYS if key not in LOCAL_RELAY_KEYS)
MANAGED_PREFIXES = ("VEMALS_EARS_", "VOICE_CAPTIONS_", "CAPTIONS_")
SECRET_KEYS = (
    "VEMALS_EARS_PEPPER",
    "VEMALS_EARS_PUBLISHER_TOKEN",
    "VEMALS_EARS_DISCORD_APPLICATION_ID",
    "VEMALS_EARS_DISCORD_BOT_TOKEN",
)


class ConfigurationError(ValueError):
    """Host configuration is incomplete or unsafe to start."""


def load_configuration(state_directory: str | Path) -> dict[str, str]:
    state = Path(state_directory)
    if not state.is_absolute():
        raise ConfigurationError("State directory must be an absolute path.")
    state = state.resolve()
    config = state / "relay.env"
    try:
        raw = config.read_text(encoding="ascii")
    except (OSError, UnicodeError):
        raise ConfigurationError("Run host setup first; relay.env is unavailable.") from None

    # relay_env.load supplies legacy defaults. Retain only keys explicitly
    # present here, so another operator never inherits those public addresses.
    explicit = set()
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ConfigurationError("relay.env contains an invalid setting.")
        if key in explicit:
            raise ConfigurationError("relay.env contains a duplicate setting: " + key)
        if not key.startswith(MANAGED_PREFIXES):
            raise ConfigurationError("relay.env contains an unsupported setting: " + key)
        if any(ord(character) < 32 or ord(character) > 126 for character in value):
            raise ConfigurationError("relay.env must contain printable ASCII settings.")
        explicit.add(key)
    loaded = relay_env.load(config)
    values = {key: loaded[key] for key in explicit}
    mode = values.get("VEMALS_EARS_HOST_MODE", STANDALONE)
    if mode not in (STANDALONE, REMOTE_WORKER):
        raise ConfigurationError("VEMALS_EARS_HOST_MODE must be standalone or remote-worker.")
    values["VEMALS_EARS_HOST_MODE"] = mode
    required = REMOTE_REQUIRED_KEYS if mode == REMOTE_WORKER else REQUIRED_KEYS
    for key in required:
        if not values.get(key):
            raise ConfigurationError("relay.env must explicitly set " + key)
    for key in SECRET_KEYS:
        if key in values:
            raise ConfigurationError("Use a secret file instead of " + key)
    if not re.fullmatch(r"[0-9]{17,20}", values["VEMALS_EARS_GUILD_ID"]):
        raise ConfigurationError("VEMALS_EARS_GUILD_ID must be a Discord guild ID.")
    port = None
    if mode == REMOTE_WORKER:
        if any(key in values for key in LOCAL_RELAY_KEYS):
            raise ConfigurationError("Remote-worker state cannot contain local relay settings. Import into a fresh state folder.")
    else:
        if values["VEMALS_EARS_BIND"] != "127.0.0.1":
            raise ConfigurationError("This host must bind to 127.0.0.1 behind HTTPS.")
        try:
            port = int(values["VEMALS_EARS_PORT"])
        except ValueError:
            port = 0
        if not 1 <= port <= 65535:
            raise ConfigurationError("VEMALS_EARS_PORT must be between 1 and 65535.")
    for key, scheme in (
        ("VEMALS_EARS_PUBLIC_SERVER_URL", "wss"),
        ("VEMALS_EARS_PUBLIC_HTTP_URL", "https"),
        ("VEMALS_EARS_CLIENT_DOWNLOAD_URL", "https"),
    ):
        try:
            url = urlsplit(values[key])
            valid = (url.scheme == scheme and url.hostname and url.username is None
                     and url.password is None and not url.fragment and url.port != 0)
        except ValueError:
            valid = False
        if not valid:
            raise ConfigurationError(key + " must be a public " + scheme + " URL.")
    if mode == REMOTE_WORKER:
        websocket = urlsplit(values["VEMALS_EARS_PUBLIC_SERVER_URL"])
        public = urlsplit(values["VEMALS_EARS_PUBLIC_HTTP_URL"])
        if (any(url.path not in ("", "/") or url.query for url in (websocket, public))
                or any(marker in values[key] for marker in ("?", "#")
                       for key in ("VEMALS_EARS_PUBLIC_SERVER_URL", "VEMALS_EARS_PUBLIC_HTTP_URL"))
                or (websocket.hostname, websocket.port or 443) != (public.hostname, public.port or 443)):
            raise ConfigurationError("Remote-worker relay addresses must be matching WSS and HTTPS origins without paths or queries.")
        if (values.get("VEMALS_EARS_SERVER_URL")
                and values["VEMALS_EARS_SERVER_URL"] != values["VEMALS_EARS_PUBLIC_SERVER_URL"]):
            raise ConfigurationError("Remote-worker server URL must match the shared public relay.")
    for key, value in values.items():
        if key.endswith("_FILE") or key in {
            "VEMALS_EARS_DATABASE", "CAPTIONS_CONSENT_STATE_PATH",
            "CAPTIONS_MAPPING_STATE_PATH", "VEMALS_EARS_WORKER_HEALTH_FILE",
        }:
            if value and not Path(value).is_absolute():
                raise ConfigurationError(key + " must be an absolute path.")

    values.update({
        "VEMALS_EARS_STATE_DIR": str(state),
        "VEMALS_EARS_SERVER_URL": (values["VEMALS_EARS_PUBLIC_SERVER_URL"]
                                   if mode == REMOTE_WORKER else "ws://127.0.0.1:" + str(port)),
        "VEMALS_EARS_WORKER_HEALTH_FILE": str(state / "worker.state"),
        "CAPTIONS_CONSENT_STATE_PATH": str(state / "consent" / "consent.json"),
        "CAPTIONS_MAPPING_STATE_PATH": str(state / "consent" / "mapping.json"),
        "LOCALAPPDATA": str(state / "profile"),
    })
    return values


def validate_activation(values: dict[str, str]) -> None:
    """An imported worker needs explicit confirmation before it may run."""
    if values["VEMALS_EARS_HOST_MODE"] != REMOTE_WORKER:
        return
    try:
        metadata = json.loads((Path(values["VEMALS_EARS_STATE_DIR"]) / "host-settings.json").read_text(encoding="ascii"))
    except (OSError, ValueError, UnicodeError):
        metadata = {}
    if (not isinstance(metadata, dict) or metadata.get("schema") != 1
            or metadata.get("mode") != REMOTE_WORKER or metadata.get("takeover_confirmed") is not True):
        raise ConfigurationError("Confirm that the previous caption worker is stopped before starting the imported worker.")


def configure_environment(values: dict[str, str]) -> None:
    # The service and an interactive admin command must use the same explicit
    # configuration even if their parent process has another host configured.
    for key in tuple(os.environ):
        if key.startswith(MANAGED_PREFIXES):
            del os.environ[key]
    os.environ.update(values)


def validate_runtime(values: dict[str, str]) -> None:
    from VoiceCaptions import runtime_installer

    root = Path(values["LOCALAPPDATA"]) / "WoWVoiceCaptions" / "runtime"
    runtime = runtime_installer.detect_installed_runtime(root)
    if runtime is None or runtime.acceleration != "cuda":
        raise ConfigurationError("Run host setup prepare; a verified NVIDIA speech runtime is required.")
    if not runtime_installer.cuda_12_4_available():
        raise ConfigurationError("The host requires an NVIDIA GPU with a CUDA 12.4-capable driver.")


def check_port(values: dict[str, str]) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            listener.bind(("127.0.0.1", int(values["VEMALS_EARS_PORT"])))
        except OSError:
            raise ConfigurationError("The configured relay port is unavailable; stop its existing owner or choose another port.") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-directory", required=True)
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--check-port", action="store_true")
    parser.add_argument("--host-mode", choices=(STANDALONE, REMOTE_WORKER), default=STANDALONE)
    parser.add_argument("role", choices=("relay", "worker", "admin"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.role != "admin" and args.arguments:
        parser.error("Only admin accepts additional arguments.")
    if args.check_port and (args.role != "relay" or not args.check_only):
        parser.error("--check-port requires --check-only relay.")
    try:
        values = load_configuration(args.state_directory)
        if values["VEMALS_EARS_HOST_MODE"] != args.host_mode:
            raise ConfigurationError("The service host mode does not match its state configuration.")
        if args.host_mode == REMOTE_WORKER and args.role != "worker":
            raise ConfigurationError("Remote-worker mode cannot run a local relay or relay administration.")
        if args.role == "worker":
            validate_runtime(values)
        if not args.check_only:
            validate_activation(values)
        if args.check_port:
            check_port(values)
    except ConfigurationError as error:
        print(str(error), file=sys.stderr)
        return 2
    if args.check_only:
        print("Host configuration verified."
              + (" NVIDIA runtime verified." if args.role == "worker" else ""))
        return 0
    configure_environment(values)
    if args.role == "worker":
        from GuildHub import host

        return host.main(["--watch"])
    from GuildHub import server

    return server.main(["serve"] if args.role == "relay" else args.arguments)


if __name__ == "__main__":
    raise SystemExit(main())
