"""Move only a legacy caption worker; the shared relay remains its authority."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

from Hosting import setup


LEGACY_WORKER = "VemalsEarsWorker"
MAX_STATE_BYTES = 1_048_576
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class TakeoverError(ValueError):
    """An actionable takeover failure without private values."""


def _regular(path: Path, limit: int) -> bytes:
    try:
        for component in (path, *path.parents):
            if component.is_symlink() or getattr(component, "is_junction", lambda: False)():
                raise OSError
        if not path.is_file() or path.stat().st_size > limit:
            raise OSError
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise OSError
        return data
    except OSError:
        raise TakeoverError("A required private file is missing, linked, unreadable or too large.") from None


def _json_file(path: Path, limit=MAX_STATE_BYTES):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate key")
            result[key] = value
        return result
    try:
        return json.loads(_regular(path, limit).decode("utf-8"), object_pairs_hook=unique)
    except (ValueError, UnicodeError):
        raise TakeoverError("A private state file is damaged. Restore it before exporting.") from None


def _atomic_json(path: Path, value: dict):
    temporary = path.parent / (".takeover-" + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="ascii", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _explicit_configuration(path: Path) -> dict:
    try:
        raw = _regular(path, 65536).decode("ascii")
    except UnicodeError:
        raise TakeoverError("The existing relay settings are not readable ASCII.") from None
    result = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or key in result or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise TakeoverError("The existing relay settings contain an invalid or duplicate entry.")
        result[key] = value
    return result


class WindowsLegacyWorker:
    """The sole service this exporter can control is the legacy VE worker."""

    def registration(self) -> dict:
        if os.name != "nt":
            raise TakeoverError("Existing-host export requires Windows.")
        import winreg

        root = "SYSTEM\\CurrentControlSet\\Services\\" + LEGACY_WORKER
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root) as service_key:
                start = winreg.QueryValueEx(service_key, "Start")[0]
                image = winreg.QueryValueEx(service_key, "ImagePath")[0]
                account = winreg.QueryValueEx(service_key, "ObjectName")[0]
                try:
                    delayed = bool(winreg.QueryValueEx(service_key, "DelayedAutoStart")[0])
                except FileNotFoundError:
                    delayed = False
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root + "\\Parameters") as key:
                values = {name: winreg.QueryValueEx(key, name)[0] for name in
                          ("Application", "AppDirectory", "AppParameters", "AppEnvironmentExtra")}
        except OSError:
            raise TakeoverError("The existing Vemal's Ears worker is not installed or cannot be inspected.") from None
        environment = {}
        for line in values["AppEnvironmentExtra"]:
            name, separator, value = line.partition("=")
            if not separator or name in environment:
                raise TakeoverError("The existing worker has an unsupported environment.")
            environment[name] = value
        if (set(environment) != {"VEMALS_EARS_STATE_DIR", "LOCALAPPDATA"}
                or values["AppParameters"].strip() != "-m GuildHub.worker_launcher"
                or account != "LocalSystem" or start not in (2, 3, 4)):
            raise TakeoverError("This service is not the supported legacy Vemal's Ears worker. No services were changed.")
        paths = {
            "wrapper": Path(str(image).strip('"')),
            "python": Path(values["Application"]),
            "source": Path(values["AppDirectory"]),
            "state": Path(environment["VEMALS_EARS_STATE_DIR"]),
            "profile": Path(environment["LOCALAPPDATA"]),
        }
        if (not all(path.is_absolute() for path in paths.values())
                or paths["wrapper"].name.lower() != "nssm.exe"
                or paths["python"].name.lower() != "python.exe"
                or not paths["wrapper"].is_file() or not paths["python"].is_file()
                or not (paths["source"] / "GuildHub/worker_launcher.py").is_file()):
            raise TakeoverError("The existing worker's executable or source ownership could not be verified.")
        record = {name: str(path.resolve()) for name, path in paths.items()}
        record["parameters"] = values["AppParameters"].strip()
        record["start"] = "delayed-auto" if start == 2 and delayed else {2: "auto", 3: "demand", 4: "disabled"}[start]
        return record

    def _run(self, *arguments, check=True):
        try:
            result = subprocess.run(["sc.exe", *arguments], capture_output=True,
                                    text=True, errors="replace", timeout=35,
                                    creationflags=NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            raise TakeoverError("Windows could not control the existing caption worker. Reopen as administrator.") from None
        if check and result.returncode:
            raise TakeoverError("Windows could not control the existing caption worker. Reopen as administrator.")
        return result

    def status(self):
        result = self._run("query", LEGACY_WORKER)
        match = re.search(r"\b(STOPPED|RUNNING|STOP_PENDING|START_PENDING|PAUSED)\b", result.stdout)
        if not match:
            raise TakeoverError("The existing worker's state could not be determined.")
        return match.group()

    def set_start(self, start):
        if start not in {"auto", "delayed-auto", "demand", "disabled"}:
            raise TakeoverError("Unsupported worker startup mode.")
        self._run("config", LEGACY_WORKER, "start=", start)

    def stop(self):
        if self.status() != "STOPPED":
            self._run("stop", LEGACY_WORKER)
        self._wait("STOPPED")

    def start(self):
        if self.status() != "RUNNING":
            self._run("start", LEGACY_WORKER)
        self._wait("RUNNING")

    def _wait(self, expected):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.status() == expected:
                return
            time.sleep(0.2)
        raise TakeoverError("The caption worker did not finish changing state. Keep the replacement stopped and check this PC.")


def _fingerprint(record: dict) -> str:
    identity = {key: value for key, value in record.items() if key != "start"}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()


class LegacyTakeover:
    def __init__(self, worker=None, receipt_directory=None):
        self.worker = worker or WindowsLegacyWorker()
        self.directory = Path(receipt_directory or
                              (Path(os.environ.get("ProgramData", "C:/ProgramData")) / "VemalsEarsTakeover"))
        self.receipt = self.directory / "handoff.json"

    @contextmanager
    def _locked(self):
        if self.directory.is_symlink() or getattr(self.directory, "is_junction", lambda: False)():
            raise TakeoverError("The takeover recovery directory must not be linked.")
        self.directory.mkdir(parents=True, exist_ok=True)
        setup.protect_directory(self.directory)
        with (self.directory / "operation.lock").open("a+b") as stream:
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    raise TakeoverError("Another takeover operation is running. Wait for it to finish.") from None
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)

    def _receipt(self):
        return _json_file(self.receipt, 16384) if self.receipt.exists() else {}

    def inspect(self) -> dict:
        try:
            record = self.worker.registration()
            values = _explicit_configuration(Path(record["state"]) / "relay.env")
            receipt = self._receipt()
            return {"available": True, "worker": self.worker.status(),
                    "guild_name": values.get("VEMALS_EARS_GUILD_NAME", "Vemal's Ears"),
                    "public_http_url": values.get("VEMALS_EARS_PUBLIC_HTTP_URL", ""),
                    "handoff_pending": receipt.get("status") in {"stopping", "exported"},
                    "bundle": receipt.get("bundle", "")}
        except (TakeoverError, OSError):
            return {"available": False, "detail": "No supported existing caption worker was found on this PC."}

    def _guard(self, identity):
        record = self.worker.registration()
        if _fingerprint(record) != identity:
            raise TakeoverError("The existing worker registration changed. No further service changes were made.")
        return record

    def _payload(self, record):
        from Hosting import handoff

        state = Path(record["state"])
        values = _explicit_configuration(state / "relay.env")
        required = ("VEMALS_EARS_GUILD_ID", "VEMALS_EARS_GUILD_NAME",
                    "VEMALS_EARS_PUBLIC_SERVER_URL", "VEMALS_EARS_PUBLIC_HTTP_URL",
                    "VEMALS_EARS_CLIENT_DOWNLOAD_URL")
        if not all(values.get(name) for name in required):
            raise TakeoverError("The existing host must explicitly declare its guild and public addresses.")

        def secret(setting, filename):
            path = Path(values.get(setting, str(state / "secrets" / filename)))
            if not path.is_absolute() or path.resolve() != (state / "secrets" / filename).resolve():
                raise TakeoverError("A worker credential is outside its expected private location.")
            try:
                return _regular(path, 16384).decode("ascii").strip()
            except UnicodeError:
                raise TakeoverError("A worker credential file is not readable ASCII.") from None

        profile = Path(record["profile"]) / "WoWVoiceCaptions"
        consent_path = Path(values.get("CAPTIONS_CONSENT_STATE_PATH", str(profile / "consent-state.json")))
        mapping_path = Path(values.get("CAPTIONS_MAPPING_STATE_PATH", str(profile / "mapping-state.json")))
        if not consent_path.is_absolute() or not mapping_path.is_absolute():
            raise TakeoverError("Existing consent and mapping paths must be absolute.")
        marker = Path(str(consent_path) + ".dirty")
        if marker.exists():
            value = _json_file(marker, 4096)
            if not isinstance(value, dict) or value.get("version") != 1 or value.get("synced") is not True:
                raise TakeoverError("The consent ledger has an unfinished write. Resume the original host and resolve its consent state before exporting.")
        empty = {"version": 1, "updatedAt": datetime.now(timezone.utc).isoformat(), "entries": {}}
        def guild_ledger(path, *, mapping=False):
            ledger = _json_file(path) if path.exists() else dict(empty)
            if (not isinstance(ledger, dict) or set(ledger) != {"version", "updatedAt", "entries"}
                    or not isinstance(ledger["entries"], dict)):
                raise TakeoverError("A worker ledger has an unsupported structure. Restore it before exporting.")
            if not mapping and (len(ledger["entries"]) > 10_000 or any(
                    not re.fullmatch(r"[0-9]{1,20}:[0-9]{1,20}", key)
                    or not isinstance(value, str) or value not in {"granted", "declined"}
                    for key, value in ledger["entries"].items())):
                # Consent's loader distrusts the entire ledger on any bad row.
                # Projection must not turn an untrusted grant into a trusted one.
                raise TakeoverError("The consent ledger contains an untrusted entry. Restore it before exporting.")
            # Older bot profiles can retain records from other guilds. This
            # worker serves only its configured guild; leave every other row
            # on the original PC and validate the selected rows on import.
            prefix = values["VEMALS_EARS_GUILD_ID"] + ":"
            return {**ledger, "entries": {key: value for key, value in ledger["entries"].items()
                                         if key.startswith(prefix)}}
        payload = {
            "schema": 1, "kind": "vemals-ears-worker-handoff",
            "guild_id": values["VEMALS_EARS_GUILD_ID"],
            "guild_name": values["VEMALS_EARS_GUILD_NAME"],
            "public_server_url": values["VEMALS_EARS_PUBLIC_SERVER_URL"],
            "public_http_url": values["VEMALS_EARS_PUBLIC_HTTP_URL"],
            "client_download_url": values["VEMALS_EARS_CLIENT_DOWNLOAD_URL"],
            "application_id": secret("VEMALS_EARS_DISCORD_APPLICATION_ID_FILE", "discord-application-id"),
            "bot_token": secret("VEMALS_EARS_DISCORD_BOT_TOKEN_FILE", "discord-bot-token"),
            "publisher_token": secret("VEMALS_EARS_PUBLISHER_TOKEN_FILE", "publisher-token"),
            "consent": guild_ledger(consent_path),
            "mapping": guild_ledger(mapping_path, mapping=True),
        }
        return handoff.validate_payload(payload)

    def export(self, destination, password, progress=lambda text: None):
        from Hosting import handoff

        destination = Path(destination)
        if not destination.is_absolute() or destination.exists() or len(password) < 12:
            raise TakeoverError("Choose a new handoff file and a passphrase of at least 12 characters.")
        handoff.validate_password(password)
        with self._locked():
            if self._receipt().get("status") in {"stopping", "exported"}:
                raise TakeoverError("A handoff is already pending. Use it, or cancel it before the replacement has ever started.")
            record = self.worker.registration()
            if destination.resolve().is_relative_to(Path(record["state"]).resolve()):
                raise TakeoverError("Save the encrypted handoff outside the existing host's state and public downloads.")
            status = self.worker.status()
            if status not in {"RUNNING", "STOPPED"}:
                raise TakeoverError("Wait for the original caption worker to finish starting or stopping.")
            identity = _fingerprint(record)
            receipt = {"schema": 1, "status": "stopping", "identity": identity,
                       "start": record["start"], "was_running": status == "RUNNING",
                       "bundle": str(destination), "created_utc": datetime.now(timezone.utc).isoformat()}
            _atomic_json(self.receipt, receipt)
            published = False
            try:
                self._guard(identity)
                progress("Stopping only the original Vemal's Ears caption worker...")
                self.worker.set_start("disabled")
                self.worker.stop()
                if self._guard(identity)["start"] != "disabled" or self.worker.status() != "STOPPED":
                    raise TakeoverError("The original caption worker did not stay disabled and stopped.")
                payload = self._payload(record)
                progress("Encrypting the worker credentials, consent and mappings...")
                handoff.write_bundle(destination, payload, password)
                published = True
                receipt["status"] = "exported"
                _atomic_json(self.receipt, receipt)
                return {"bundle": str(destination), "public_http_url": payload["public_http_url"],
                        "guild_name": payload["guild_name"]}
            except Exception:
                # Publication may succeed before a writer's private staging
                # cleanup fails. Never restart a source once a handoff exists.
                if published or destination.exists():
                    raise TakeoverError("The encrypted file was created and the original worker remains stopped. Recovery notes could not be finalized; keep the file private.") from None
                try:
                    self._guard(identity)
                    self.worker.set_start(record["start"])
                    if status == "RUNNING":
                        self.worker.start()
                    receipt["status"] = "restored"
                    _atomic_json(self.receipt, receipt)
                except Exception:
                    raise TakeoverError("Export failed and the original caption worker could not be restored. Keep the replacement stopped; reopen this app to recover the unused handoff.") from None
                raise

    def cancel_unused(self, *, replacement_never_started: bool):
        if replacement_never_started is not True:
            raise TakeoverError("Only an unused handoff can be cancelled. Once the replacement has run, current consent must be transferred back before returning.")
        with self._locked():
            receipt = self._receipt()
            if receipt.get("status") not in {"stopping", "exported"}:
                raise TakeoverError("There is no pending handoff to cancel.")
            self._guard(receipt["identity"])
            current_status = self.worker.status()
            before_stop_crash = (current_status == "RUNNING" and receipt["was_running"]
                                 and not Path(receipt["bundle"]).exists())
            if current_status != "STOPPED" and not before_stop_crash:
                raise TakeoverError("The original worker is no longer stopped. Check which host is active before continuing.")
            self.worker.set_start(receipt["start"])
            if receipt["was_running"] and current_status != "RUNNING":
                self.worker.start()
            receipt["status"] = "cancelled"
            _atomic_json(self.receipt, receipt)
            return self.inspect()


class TakeoverController:
    """GUI adapter; preview decrypts in memory and never logs bundle contents."""

    def __init__(self, host_controller, legacy=None):
        self.host = host_controller
        self.legacy = legacy or LegacyTakeover()
        self.pending = None

    def preview(self, path, password):
        from Hosting import handoff

        if self.host.read_settings().get("configured"):
            raise TakeoverError("This PC already has host state. Use a separate replacement installation.")
        self.pending = None
        payload = handoff.read_bundle(Path(path), password)
        self.pending = payload
        return {key: payload[key] for key in ("guild_name", "guild_id", "application_id", "public_http_url")}

    def import_pending(self, *, original_stopped: bool):
        from Hosting import handoff

        if original_stopped is not True:
            raise TakeoverError("Confirm that the original caption worker is stopped before importing.")
        if self.pending is None:
            raise TakeoverError("Open and verify the encrypted handoff first.")
        handoff.import_worker(self.pending, self.host.state)
        self.pending = None
        self.confirm_start(original_stopped=True)
        return self.host.read_settings()

    def confirm_start(self, *, original_stopped: bool):
        if original_stopped is not True:
            raise TakeoverError("Confirm that the original caption worker is stopped before starting.")
        settings = self.host.read_settings()
        if settings.get("mode") != "remote-worker":
            raise TakeoverError("This installation is not a replacement caption worker.")
        path = self.host.state / "host-settings.json"
        metadata = _json_file(path, 16384)
        if metadata.get("schema") != 1 or metadata.get("mode") != "remote-worker":
            raise TakeoverError("The imported host metadata is invalid.")
        metadata["takeover_confirmed"] = True
        _atomic_json(path, metadata)
