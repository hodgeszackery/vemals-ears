"""Graphical host setup and narrowly scoped Windows service operations."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import urllib.request
from urllib.parse import urlsplit

from GuildHub import relay_env
from GuildHub.worker_health import WorkerHealthMarker
from Hosting import service, setup
from VoiceCaptions import runtime_installer
from VoiceCaptions.credentials import build_bot_install_url, validate_credentials

ROOT = Path(__file__).resolve().parents[1]
RELAY = "VemalsEarsHostRelay"
WORKER = "VemalsEarsHostWorker"
TUNNEL = "VemalsEarsHostTunnel"
SERVICES = (RELAY, WORKER, TUNNEL)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class HostError(RuntimeError):
    """A user-readable host setup or operation failure."""


def default_state_directory() -> Path:
    return Path(os.environ.get("ProgramData", "C:/ProgramData")) / "VemalsEarsHost"


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=True) + "\n", encoding="ascii")
    os.replace(temporary, path)


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="ascii"))
        if isinstance(value, dict):
            return value
    except (OSError, ValueError, UnicodeError):
        pass
    raise HostError("The saved host settings could not be read. Restore the state folder from your backup.")


def _same_path(left: str | Path, right: str | Path) -> bool:
    return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(str(Path(right).resolve()))


class HostController:
    def __init__(self, package_root: Path | None = None, state_directory: Path | None = None):
        self.package_root = Path(package_root or ROOT).resolve()
        self.install_root = self.package_root.parent
        self.state = setup.state_path(Path(state_directory or default_state_directory()))
        self.tools = self.install_root / "tools"
        self.node = self.tools / "node/node.exe"
        self.npm = self.tools / "node/node_modules/npm/bin/npm-cli.js"
        self.nssm = self.tools / "nssm/nssm.exe"
        self.cloudflared = self.tools / "cloudflared/cloudflared.exe"
        self.python = self.install_root / "runtime/python.exe"

    def _run(self, arguments: list[str], *, timeout: float = 30, check: bool = True):
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("VEMALS_EARS_", "VOICE_CAPTIONS_", "CAPTIONS_"))}
        environment["PATH"] = str(self.node.parent) + os.pathsep + environment.get("PATH", "")
        try:
            result = subprocess.run(
                [str(item) for item in arguments], cwd=self.package_root, env=environment,
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                errors="replace", timeout=timeout, creationflags=NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError):
            raise HostError("The Windows service tool could not finish. Check administrator access and retry.") from None
        if check and result.returncode:
            # Child stderr can include third-party diagnostics. Never echo it
            # into the wizard or persist it as a setup log.
            raise HostError("Windows could not complete the service operation. Check host status and retry.")
        return result

    def _nssm(self, *arguments: str, check: bool = True):
        return self._run([str(self.nssm), *arguments], check=check)

    def _service_status(self, name: str) -> str:
        result = self._run(["sc.exe", "query", name], check=False)
        if result.returncode == 1060:
            return "Not installed"
        if result.returncode:
            raise HostError("Windows service status could not be read. Open the host app as administrator.")
        match = re.search(r"\b(STOPPED|START_PENDING|STOP_PENDING|RUNNING|PAUSED|PAUSE_PENDING|CONTINUE_PENDING)\b", result.stdout)
        if not match:
            raise HostError("Windows returned an unknown host service status.")
        return match.group().replace("_", " ").title()

    def _get(self, name: str, setting: str) -> str:
        return self._nssm("get", name, setting).stdout.strip()

    def _owned_services(self) -> dict[str, str]:
        statuses = {name: self._service_status(name) for name in SERVICES}
        for name, status in statuses.items():
            if status == "Not installed":
                continue
            expected_executable = self.cloudflared if name == TUNNEL else self.python
            if (not _same_path(self._get(name, "AppDirectory"), self.package_root)
                    or not _same_path(self._get(name, "Application"), expected_executable)):
                raise HostError("A host service belongs to another installation. No services were changed.")
            parameters = self._get(name, "AppParameters")
            if name == TUNNEL:
                expected = subprocess.list2cmdline([
                    "tunnel", "--no-autoupdate", "run", "--token-file",
                    str(self.state / "secrets/cloudflare-tunnel-token"),
                ])
            else:
                role = "relay" if name == RELAY else "worker"
                expected = '-B -m Hosting.service --state-directory "' + str(self.state) + '" ' + role
            if parameters != expected:
                raise HostError("A host service uses another state folder. No services were changed.")
        return statuses

    def read_settings(self) -> dict:
        if not (self.state / "relay.env").is_file():
            return {"configured": False, "hostname": "", "guild_id": "", "application_id": "",
                    "guild_name": "Vemal's Ears", "port": 8770, "client_installer": "",
                    "client_download_url": "", "tunnel_enabled": True,
                    "bot_install_url": "", "join_url": "", "identity_locked": False}
        values = service.load_configuration(self.state)
        metadata = _read_json(self.state / "host-settings.json")
        try:
            application_id = Path(values["VEMALS_EARS_DISCORD_APPLICATION_ID_FILE"]).read_text(encoding="ascii").strip()
            install_url = build_bot_install_url(application_id)
        except (OSError, ValueError, UnicodeError):
            raise HostError("The Discord application ID could not be read. Restore your private state backup.") from None
        return {
            "configured": True,
            "identity_locked": self._identity_locked(values),
            "hostname": urlsplit(values["VEMALS_EARS_PUBLIC_HTTP_URL"]).hostname,
            "guild_id": values["VEMALS_EARS_GUILD_ID"], "application_id": application_id,
            "guild_name": values["VEMALS_EARS_GUILD_NAME"], "port": int(values["VEMALS_EARS_PORT"]),
            "client_installer": metadata.get("client_installer", ""),
            "client_download_url": values["VEMALS_EARS_CLIENT_DOWNLOAD_URL"],
            "tunnel_enabled": metadata.get("tunnel_enabled", False) is True,
            "bot_install_url": install_url, "join_url": values["VEMALS_EARS_PUBLIC_HTTP_URL"] + "/join",
        }

    def _identity_locked(self, values: dict) -> bool:
        # A human typo may be corrected before this host has any audience.
        # Pairing, consent or session state makes the guild identity durable.
        consent = self.state / "consent"
        if consent.is_dir() and any(path.is_file() for path in consent.rglob("*")):
            return True
        database = Path(values["VEMALS_EARS_DATABASE"])
        if not database.exists():
            return False
        try:
            connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
            try:
                return any(connection.execute("SELECT 1 FROM " + table + " LIMIT 1").fetchone()
                           for table in ("devices", "enrollments"))
            finally:
                connection.close()
        except sqlite3.Error:
            return True

    def configure(self, settings: dict) -> None:
        current = self.read_settings()
        domain = setup.hostname(str(settings.get("hostname", "")))
        guild_id = str(settings.get("guild_id", "")).strip()
        application_id = str(settings.get("application_id", "")).strip()
        name = str(settings.get("guild_name", "Vemal's Ears")).strip()
        try:
            port = int(settings.get("port", 8770))
        except (TypeError, ValueError):
            raise HostError("Enter a relay port between 1 and 65535.") from None
        if not 1 <= port <= 65535:
            raise HostError("Enter a relay port between 1 and 65535.")
        if not re.fullmatch(r"[0-9]{17,20}", guild_id):
            raise HostError("Enter your Discord server ID (17 to 20 digits).")
        if not name or len(name) > 80 or any(ord(char) < 32 or ord(char) > 126 for char in name):
            raise HostError("Use a guild name with 1 to 80 printable ASCII characters.")
        identity_changed = current["configured"] and (guild_id != current["guild_id"] or application_id != current["application_id"])
        if identity_changed and current["identity_locked"]:
            raise HostError("An existing host with member state keeps its Discord server and application identity. Use a separate host for another guild.")
        token = str(settings.get("bot_token", "")).strip()
        if identity_changed and not token:
            raise HostError("Enter the bot token again when correcting the Discord IDs.")
        if not token and current["configured"]:
            token = (self.state / "secrets/discord-bot-token").read_text(encoding="ascii").strip()
        validate_credentials(application_id, token)
        if any(ord(char) < 33 or ord(char) > 126 for char in token):
            raise HostError("The Discord token contains unsupported characters.")
        installer_text = str(settings.get("client_installer", "")).strip()
        installer = Path(installer_text).resolve() if installer_text else None
        if installer and (installer.suffix.lower() != ".exe" or not installer.is_file()):
            raise HostError("Choose an existing Vemal's Ears member installer (.exe).")
        local_download = self.state / "downloads/Vemals-Ears-Setup.exe"
        url = str(settings.get("client_download_url", "")).strip()
        if installer:
            url = "https://" + domain + "/download"
        elif not url or (url == "https://" + domain + "/download" and not local_download.is_file()):
            raise HostError("Choose the member installer file or provide its HTTPS download link.")
        url = setup.download_url(url)
        tunnel_enabled = settings.get("tunnel_enabled", False) is True
        tunnel_token = str(settings.get("tunnel_token", "")).strip()
        tunnel_file = self.state / "secrets/cloudflare-tunnel-token"
        if tunnel_enabled and not tunnel_token and not tunnel_file.is_file():
            raise HostError("Paste the Cloudflare tunnel token, or select existing HTTPS forwarding.")
        if tunnel_token and (not 32 <= len(tunnel_token) <= 16384
                             or not re.fullmatch(r"[A-Za-z0-9_+=/.-]+", tunnel_token)):
            raise HostError("Paste only the Cloudflare tunnel token, without the installation command.")

        if not current["configured"]:
            setup.create_configuration(
                self.state, public_hostname=domain, guild_id=guild_id,
                application_id=application_id, bot_token=token, guild_name=name,
                port=port, client_download_url=url,
            )
        else:
            values = relay_env.load(self.state / "relay.env")
            if identity_changed:
                self.stop_services()
                if self._identity_locked(values):
                    raise HostError("Members have used this host. Its Discord IDs can no longer be changed.")
                self._write_secret(self.state / "secrets/discord-application-id", application_id)
            values.update({"VEMALS_EARS_GUILD_ID": guild_id,
                           "VEMALS_EARS_GUILD_NAME": name, "VEMALS_EARS_PORT": str(port),
                           "VEMALS_EARS_PUBLIC_HTTP_URL": "https://" + domain,
                           "VEMALS_EARS_PUBLIC_SERVER_URL": "wss://" + domain,
                           "VEMALS_EARS_CLIENT_DOWNLOAD_URL": url})
            relay_env.save(values, self.state / "relay.env")
            if settings.get("bot_token"):
                self._write_secret(self.state / "secrets/discord-bot-token", token)
        if tunnel_token:
            self._write_secret(tunnel_file, tunnel_token)
        _write_json(self.state / "host-settings.json", {
            "schema": 1, "tunnel_enabled": tunnel_enabled,
            "client_installer": str(installer) if installer else "",
        })

    @staticmethod
    def _write_secret(path: Path, value: str) -> None:
        temporary = path.with_suffix(".tmp")
        temporary.write_text(value + "\n", encoding="ascii")
        if os.name != "nt":
            temporary.chmod(0o600)
        os.replace(temporary, path)

    def prepare(self, progress=lambda message: None) -> None:
        settings = self.read_settings()
        if not settings["configured"]:
            raise HostError("Complete the host settings first.")
        for path in (self.node, self.npm, self.nssm, self.cloudflared, self.python):
            if not path.is_file():
                raise HostError("The host installation is incomplete. Run the Windows installer again.")
        if not runtime_installer.cuda_12_4_available():
            raise HostError("Install a current NVIDIA driver for your GPU, then retry setup.")
        if settings["client_installer"]:
            progress("Preparing the member download...")
            destination = self.state / "downloads/Vemals-Ears-Setup.exe"
            source = Path(settings["client_installer"])
            if not _same_path(source, destination):
                temporary = destination.with_suffix(".tmp")
                shutil.copyfile(source, temporary)
                os.replace(temporary, destination)
            metadata = _read_json(self.state / "host-settings.json")
            metadata["client_installer"] = str(destination)
            _write_json(self.state / "host-settings.json", metadata)
        progress("Preparing the NVIDIA speech engine and Discord helper...")
        outcome = runtime_installer.install_voice_dependencies(
            self.state / "profile/WoWVoiceCaptions/runtime",
            adapter_dir=self.package_root / "VoiceCaptions/discord_adapter",
            node_path=self.node, npm_cli_path=self.npm,
            progress=lambda update: progress(update.message),
        )
        if outcome.runtime.acceleration != "cuda":
            raise HostError("NVIDIA speech preparation did not finish. Check your driver and internet connection, then retry.")
        if not outcome.adapter.ready:
            raise HostError("The Discord helper could not be prepared. Check your internet connection and retry.")
        progress("Speech engine and Discord helper are ready.")

    def install_services(self) -> None:
        self._owned_services()  # Validate every existing registration first.
        result = self._run([
            "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(self.package_root / "Hosting/Install-Host.ps1"),
            "-StateDirectory", str(self.state), "-PythonPath", str(self.python),
            "-NssmPath", str(self.nssm),
        ], timeout=180, check=False)
        if result.returncode:
            raise HostError("Host services could not start. Check NVIDIA readiness, administrator access, and whether the relay port is already used.")
        self._install_tunnel()

    def _install_tunnel(self) -> None:
        statuses = self._owned_services()
        if not self.read_settings()["tunnel_enabled"]:
            if statuses[TUNNEL] != "Not installed":
                if statuses[TUNNEL] != "Stopped":
                    self._nssm("stop", TUNNEL)
                self._nssm("remove", TUNNEL, "confirm")
            return
        if statuses[TUNNEL] == "Not installed":
            self._nssm("install", TUNNEL, str(self.cloudflared))
        elif statuses[TUNNEL] != "Stopped":
            self._nssm("stop", TUNNEL)
        parameters = subprocess.list2cmdline([
            "tunnel", "--no-autoupdate", "run", "--token-file",
            str(self.state / "secrets/cloudflare-tunnel-token"),
        ])
        settings = {
            "Application": str(self.cloudflared), "AppDirectory": str(self.package_root),
            "AppParameters": parameters, "DisplayName": "Vemal's Ears Host Tunnel",
            "AppStdout": str(self.state / "logs/tunnel.log"),
            "AppStderr": str(self.state / "logs/tunnel.log"),
            "AppRotateFiles": "1", "AppRotateBytes": "1048576",
            "AppRestartDelay": "30000", "Start": "SERVICE_DELAYED_AUTO_START",
        }
        try:
            for key, value in settings.items():
                self._nssm("set", TUNNEL, key, value)
            self._nssm("start", TUNNEL)
        except HostError:
            if statuses[TUNNEL] == "Not installed":
                # Only roll back the registration this invocation created.
                # A preexisting service is never deleted as error recovery.
                self._nssm("stop", TUNNEL, check=False)
                self._nssm("remove", TUNNEL, "confirm")
            raise

    def start_services(self) -> None:
        statuses = self._owned_services()
        selected = (RELAY, WORKER, TUNNEL) if self.read_settings()["tunnel_enabled"] else (RELAY, WORKER)
        if any(statuses[name] == "Not installed" for name in selected):
            raise HostError("Finish setup before starting the host.")
        for name in selected:
            if statuses[name] != "Running":
                self._nssm("start", name)

    def stop_services(self) -> None:
        statuses = self._owned_services()
        for name in (WORKER, TUNNEL, RELAY):
            if statuses[name] not in {"Not installed", "Stopped"}:
                self._nssm("stop", name)

    def remove_services(self) -> None:
        statuses = self._owned_services()
        self.stop_services()
        for name in (WORKER, TUNNEL, RELAY):
            if statuses[name] != "Not installed":
                self._nssm("remove", name, "confirm")

    def status(self) -> dict:
        settings = self.read_settings()
        if not settings["configured"]:
            return {"configured": False, "relay": "Not installed", "worker": "Not installed",
                    "tunnel": "Not installed", "health": False, "detail": "Complete setup to start your host."}
        statuses = self._owned_services()
        healthy = False
        if statuses[RELAY] == "Running":
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                with opener.open("http://127.0.0.1:" + str(settings["port"]) + "/healthz", timeout=2) as response:
                    healthy = json.loads(response.read(4096)) == {"status": "ok", "service": "vemals-ears"}
            except (OSError, ValueError):
                pass
        worker_ready = statuses[WORKER] == "Running" and WorkerHealthMarker(self.state / "worker.state").is_healthy()
        if healthy and worker_ready:
            detail = "Relay and caption worker are ready. Verify your public address and try /vem start in Discord."
        elif healthy:
            detail = "Relay is ready. The caption worker is starting or needs attention; check Discord credentials and NVIDIA setup."
        else:
            detail = "The relay is not ready. Start the host or retry setup."
        return {"configured": True, "relay": statuses[RELAY], "worker": statuses[WORKER],
                "tunnel": statuses[TUNNEL] if settings["tunnel_enabled"] else "External HTTPS",
                "health": healthy and worker_ready, "detail": detail}


def installer_lifecycle(install_root: Path, state_directory: Path, *, remove: bool = False, resume: bool = False) -> None:
    install_root = install_root.resolve()
    marker = _read_json(install_root / "host-install.json")
    if marker.get("product") != "vemals-ears-host" or marker.get("schema") != 1:
        raise HostError("This directory is not a recognized Vemal's Ears Host installation.")
    if marker.get("test_build") is True:
        return
    controller = HostController(install_root / "app", state_directory)
    receipt_path = install_root / "host-resume.json"
    if resume:
        receipt = _read_json(receipt_path)
        if not receipt:
            return
        running = receipt.get("running")
        if receipt.get("schema") != 1 or not isinstance(running, list) or any(name not in SERVICES for name in running):
            raise HostError("The host upgrade service receipt is invalid.")
        statuses = controller._owned_services()
        for name in (RELAY, WORKER, TUNNEL):
            if name in running and statuses[name] != "Running":
                if statuses[name] == "Not installed":
                    raise HostError("An installed host service disappeared during the upgrade.")
                controller._nssm("start", name)
        receipt_path.unlink()
        return
    statuses = controller._owned_services()
    controller._run([
        "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", str(controller.package_root / "Hosting/Close-Host.ps1"),
        "-PythonwPath", str(install_root / "runtime/pythonw.exe"),
    ], timeout=120)
    if remove:
        controller.remove_services()
    else:
        previous = _read_json(receipt_path)
        running = {name for name in previous.get("running", []) if name in SERVICES}
        running.update(name for name, status in statuses.items() if status == "Running")
        _write_json(receipt_path, {"schema": 1, "running": sorted(running)})
        controller.stop_services()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-directory", required=True, type=Path)
    parser.add_argument("action", choices=("installer-stop", "installer-remove", "installer-resume"))
    parser.add_argument("--install-root", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        installer_lifecycle(args.install_root, args.state_directory,
                            remove=args.action == "installer-remove", resume=args.action == "installer-resume")
        return 0
    except HostError as error:
        print("Host setup: " + str(error), file=sys.stderr)
        return 2
    except (OSError, ValueError) as error:
        print("Host installation operation failed (" + type(error).__name__ + "). Check the installation and private state folder.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
