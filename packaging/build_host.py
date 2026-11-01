"""Build a portable source package for a Windows/NVIDIA Vemal's Ears host.

Only the fixed source inventory below is read. Operator state, downloaded
runtimes and local credentials are not inputs, even when beside the source.
The same script can rebuild an extracted package without Git or the client.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import stat
import zipfile


ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_NAME = "VemalsEars-Host.zip"
ARCHIVE_ROOT = "VemalsEars-Host/"
MANIFEST_NAME = "HOST-MANIFEST.json"

# Keep individual names explicit: a recursive copy of a runtime directory
# could silently include a new credentials file, transcript or local backup.
HOST_FILES = (
    "LICENSE",
    "Hosting/__init__.py",
    "Hosting/setup.py",
    "Hosting/control.py",
    "Hosting/wizard.py",
    "Hosting/assets.py",
    "Hosting/service.py",
    "Hosting/Install-Host.ps1",
    "Hosting/Close-Host.ps1",
    "Hosting/README.txt",
    "Hosting/QUICKSTART.txt",
    "Hosting/ARCHITECTURE.txt",
    "Hosting/requirements.txt",
    "Hosting/RELAY-IMPORT.json",
    "Hosting/Installer/VemalsEarsHost.iss",
    "Hosting/Installer/assets.lock.json",
    "Installer/runtime-lock.json",
    "Installer/runtime-sources.lock.json",
    "Windows/VemalsEars.ico",
    "GuildHub/__init__.py",
    "GuildHub/host.py",
    "GuildHub/protocol.py",
    "GuildHub/publisher.py",
    "GuildHub/quick_chat_worker.py",
    "GuildHub/relay_env.py",
    "GuildHub/release_pointer.py",
    "GuildHub/server.py",
    "GuildHub/store.py",
    "GuildHub/worker_health.py",
    "GuildHub/version.py",
    "GuildHub/third-party/CPP-HTTPLIB-LICENSE.txt",
    "GuildHub/third-party/SILERO-VAD-LICENSE.txt",
    "GuildHub/third-party/SILERO-VAD-MODEL-CARD.txt",
    "GuildHub/third-party/SOURCES.json",
    "GuildHub/third-party/WHISPER-MODEL-CARD.txt",
    "GuildHub/third-party/WHISPER-MODEL-LICENSE.txt",
    "VoiceCaptions/__init__.py",
    "VoiceCaptions/coordinator.py",
    "VoiceCaptions/credentials.py",
    "VoiceCaptions/events.py",
    "VoiceCaptions/member_overlay.py",
    "VoiceCaptions/prosody.py",
    "VoiceCaptions/runtime_installer.py",
    "VoiceCaptions/settings.py",
    "VoiceCaptions/voice_captions.py",
    "VoiceCaptions/stt/__init__.py",
    "VoiceCaptions/stt/backend.py",
    "VoiceCaptions/stt/contracts.py",
    "VoiceCaptions/stt/fake.py",
    "VoiceCaptions/stt/service.py",
    "VoiceCaptions/stt/vad.py",
    "VoiceCaptions/stt/vocabulary.py",
    "VoiceCaptions/stt/whisper_cpp.py",
    "VoiceCaptions/discord_adapter/package.json",
    "VoiceCaptions/discord_adapter/package-lock.json",
    "VoiceCaptions/discord_adapter/src/audio_sink.js",
    "VoiceCaptions/discord_adapter/src/command_controller.js",
    "VoiceCaptions/discord_adapter/src/command_schema.js",
    "VoiceCaptions/discord_adapter/src/config.js",
    "VoiceCaptions/discord_adapter/src/consent_ledger.js",
    "VoiceCaptions/discord_adapter/src/discord_runtime.js",
    "VoiceCaptions/discord_adapter/src/index.js",
    "VoiceCaptions/discord_adapter/src/mapping_ledger.js",
    "VoiceCaptions/discord_adapter/src/protocol.js",
    "VoiceCaptions/discord_adapter/src/quick_chat_input.js",
    "VoiceCaptions/discord_adapter/src/session_ledger.js",
    "VoiceCaptions/discord_adapter/src/session_manager.js",
    "VoiceCaptions/discord_adapter/src/speaker_tracker.js",
    "packaging/build_host.py",
    "packaging/build_host_installer.py",
    "packaging/build_runtime.py",
    "packaging/runtime_licenses.py",
    "packaging/runtime_sources.py",
    "packaging/worker_sources.py",
    "packaging/worker-sources.lock.json",
)


class HostArchiveError(ValueError):
    """The host source is incomplete or the archive is not the curated slice."""


def source_path(root: Path, relative: str) -> Path:
    logical = PurePosixPath(relative)
    if (logical.is_absolute() or ".." in logical.parts or not logical.parts
            or "\\" in relative or ":" in relative or relative not in HOST_FILES):
        raise HostArchiveError("Unlisted host source path: " + relative)
    path = root.joinpath(*logical.parts)
    current = path
    while current != root:
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            raise HostArchiveError("Linked host source is refused: " + relative)
        current = current.parent
    if not path.is_file() or not path.resolve().is_relative_to(root):
        raise HostArchiveError("Missing or escaping host source: " + relative)
    return path


def snapshot(root: Path) -> dict[str, bytes]:
    root = root.resolve(strict=True)
    return {name: source_path(root, name).read_bytes() for name in sorted(HOST_FILES)}


def manifest(files: dict[str, bytes]) -> dict:
    return {"schema": 1, "files": [
        {"path": name, "sha256": hashlib.sha256(data).hexdigest()}
        for name, data in sorted(files.items())
    ]}


def archive_bytes(files: dict[str, bytes]) -> bytes:
    if set(files) != set(HOST_FILES):
        raise HostArchiveError("Host source inventory is incomplete or contains unlisted files.")
    records = dict(files)
    records[MANIFEST_NAME] = (json.dumps(manifest(files), indent=2) + "\n").encode("ascii")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(records.items()):
            info = zipfile.ZipInfo(ARCHIVE_ROOT + name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, data)
    return buffer.getvalue()


def verify_archive(data: bytes) -> dict[str, bytes]:
    """Validate names and recorded bytes without extracting untrusted paths."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            expected = {ARCHIVE_ROOT + name for name in (*HOST_FILES, MANIFEST_NAME)}
            if len(names) != len(set(names)) or set(names) != expected:
                raise HostArchiveError("Host archive has missing, duplicate or unlisted files.")
            # This source slice is small; refuse resource-exhausting ZIP input.
            if any(item.file_size > 16 * 1024 * 1024 for item in archive.infolist()):
                raise HostArchiveError("Host archive source exceeds the size limit.")
            if sum(item.file_size for item in archive.infolist()) > 64 * 1024 * 1024:
                raise HostArchiveError("Host archive exceeds the size limit.")
            files = {name: archive.read(ARCHIVE_ROOT + name) for name in HOST_FILES}
            recorded = json.loads(archive.read(ARCHIVE_ROOT + MANIFEST_NAME))
            if recorded != manifest(files):
                raise HostArchiveError("Host archive source does not match its manifest.")
            return files
    except (zipfile.BadZipFile, KeyError, UnicodeError, json.JSONDecodeError) as error:
        raise HostArchiveError("Invalid host archive.") from error


def build(root: Path, target: Path) -> Path:
    payload = archive_bytes(snapshot(root))
    verify_archive(payload)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    options = parser.add_mutually_exclusive_group()
    options.add_argument("--output", type=Path, default=ROOT / "dist" / ARCHIVE_NAME)
    options.add_argument("--verify", type=Path, help="Validate an existing host ZIP without extracting it")
    arguments = parser.parse_args(argv)
    try:
        if arguments.verify:
            files = verify_archive(arguments.verify.read_bytes())
            print("Verified host archive: " + str(len(files)) + " source files.")
        else:
            print(build(ROOT, arguments.output))
    except (OSError, HostArchiveError) as error:
        parser.exit(1, "Host package failed: " + str(error) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
