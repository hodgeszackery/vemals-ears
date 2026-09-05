"""Update an extracted Vemal's Ears folder from one source snapshot.

An extracted bundle is a separate copy with no link back to the
repository, so it silently falls behind every time client code changes.
That has now caused three false alarms in one evening: captions that
still truncated after the wrap was written, names that stayed grey after
class colours landed, and a member client stuck in a reconnect loop
because its protocol.py predated a fix its events.py depended on. Each
looked like a bug in the fix rather than a stale file.

Only the files THIS bundle ships, and only those whose contents differ.
The edition matters: a member extraction carries an edition marker, and
its manifest is an allowlist that deliberately excludes the Discord
adapter, the speech-recognition worker and the host coordinator. A
refresh must never widen that - a member's computer holding the guild's
bot code is the exact thing the allowlist exists to prevent.

The embedded runtime is left alone: it is tens of megabytes, it changes
only when runtime-lock.json does, and rewriting it under a running app is
the one way to break something that currently works.

Overwriting a .py under a running process is safe - Python already holds
the old code in memory - but the app must be restarted to pick it up,
which this says out loud rather than leaving somebody to wonder.

    python packaging/refresh_local_copy.py "C:/path/to/VemalsEars"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import build_release  # noqa: E402
import source_distribution  # noqa: E402


EDITION_MANIFESTS = {'vemals-ears-member.json': lambda: (build_release.VEMALS_EARS_MEMBER_APP_FILES,)}


def edition_marker(target: Path) -> str | None:
    return 'vemals-ears-member.json' if (target / 'vemals-ears-member.json').is_file() else None


def is_member_copy(target: Path) -> bool:
    return edition_marker(target) is not None


def shipped_files(target: Path) -> list[str]:
    if not is_member_copy(target) or (target / 'downtime-hosted.json').exists():
        raise SystemExit('This checkout refreshes only a Vemals Ears member installation.')
    return list(build_release.VEMALS_EARS_MEMBER_APP_FILES)



# Marker file -> the manifests that edition ships. A new edition MUST be
# added here or its extractions refuse to refresh - which is the point.
# Checking for one marker and letting everything else fall through to the
# full manifests is how a hosted extraction got the Discord adapter and
# the GearHelper Tools written into it on 2026-08-31, the exact widening
# the member branch below was built to prevent.








# The file that says which build the installer laid down, written by
# Installer/stage_installer.py and placed at the install root. Its
# presence is what makes a folder installer-managed rather than an
# extracted zip.
PAYLOAD_MANIFEST = "payload-manifest.json"

# The installed copy's identity. Refreshing it is what let a folder
# report a version no installer ever put there.
VERSION_FILE = "GuildHub/version.py"

# These inputs describe retained compiled components, rather than editable
# app Python or documentation. A changed native/runtime build needs its
# installer; a Python-only refresh can still add newly allowlisted modules.
RETAINED_BINARY_INPUTS = (
    "Installer/launcher.c", "Installer/launcher.rc", "Windows/DowntimeLauncher.c",
    "Windows/VemalsEars.ico", "Installer/build_installer.ps1", "Installer/VemalsEars.iss",
    "Installer/runtime-lock.json", "Installer/runtime-sources.lock.json",
)


def installer_manifest(target: Path) -> Path | None:
    """The payload manifest governing this folder, or None.

    Checked at the folder AND its parent because the refresh target is
    the install's app/ subdirectory, while the installer writes the
    manifest to the install root beside it.
    """
    for candidate in (target / PAYLOAD_MANIFEST, target.parent / PAYLOAD_MANIFEST):
        if candidate.is_file():
            return candidate
    return None


def recorded_version(manifest: Path) -> str | None:
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = payload.get("version")
    return version if isinstance(version, str) and version.strip() else None


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def refresh_snapshot(target: Path, required: list[str], manifest: Path | None) -> dict[str, bytes]:
    """Preflight the full source and retained binaries before changing a file.

    Older installed EXEs have no source capsule to verify. Install a source-
    complete installer once to enable this development refresh safely.
    """
    files = source_distribution.snapshot(ROOT, tuple(required))
    if manifest is not None:
        files[VERSION_FILE] = (target / VERSION_FILE).read_bytes()
    runtimes = [target / 'runtime']
    if manifest is not None:
        runtimes.append(target.parent / 'runtime')
    for runtime in runtimes:
        if runtime.exists():
            try:
                source_distribution.validate_runtime(runtime, ROOT)
            except (OSError, ValueError, SystemExit) as error:
                raise source_distribution.SourceError(
                    'the retained runtime does not match the current license/source inputs (' + str(error) +
                    '); rebuild the full ZIP or install a source-complete installer before refreshing') from error
    native = manifest is not None and (target.parent / "VemalsEars.exe").is_file()
    if native:
        capsule = target / source_distribution.EMBEDDED_PATH
        if not capsule.is_file():
            raise source_distribution.SourceError(
                "this installed EXE has no matching source archive; install a source-complete "
                "Vemal's Ears installer once before refreshing it")
        previous = source_distribution.read_archive(capsule.read_bytes(), set(files))
        changed = [relative for relative in RETAINED_BINARY_INPUTS
                   if previous.get(relative) != files.get(relative)]
        if changed:
            raise source_distribution.SourceError(
                "retained native/runtime build inputs changed (" + ", ".join(changed) +
                "); build and install the updated source-complete installer before refreshing")
        # The native installer also carries Python used only at uninstall.
        # Keep its real installed bytes in the source capsule during refresh.
        for relative in set(build_release.WINDOWS_INSTALLER_APP_FILES) - set(required):
            files[relative] = (target / relative).read_bytes()
    return files


def refresh(target: Path) -> int:
    if not target.is_dir():
        raise SystemExit("no extracted copy at " + str(target))

    manifest = installer_manifest(target)

    required = []
    for relative in shipped_files(target):
        if manifest is not None and relative.replace("\\", "/") == VERSION_FILE:
            # Left deliberately. See PAYLOAD_MANIFEST above: rewriting
            # this is what made the install claim a build its installer
            # never delivered, and permanently ineligible for the update
            # that would have fixed it.
            continue
        required.append(relative)

    # Validate the entire edition before copying its first file. A missing
    # protocol module must not leave earlier modules updated against an old
    # contract while the refresh claims success.
    absent = [relative for relative in required if not (ROOT / relative).is_file()]
    if absent:
        print("Refresh aborted: required files are missing from this checkout:", file=sys.stderr)
        for relative in absent:
            print("    " + relative, file=sys.stderr)
        print("The extracted copy was not changed.", file=sys.stderr)
        return 1

    try:
        source_files = refresh_snapshot(target, required, manifest)
        source_archive = source_distribution.archive_bytes(source_files)
    except (OSError, source_distribution.SourceError) as error:
        print("Refresh aborted: " + str(error) + ". The extracted copy was not changed.", file=sys.stderr)
        return 1

    updated, added, same = [], [], 0
    for relative in required:
        data = source_files[relative]
        destination = target / relative
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            added.append(relative)
        elif data != destination.read_bytes():
            destination.write_bytes(data)
            updated.append(relative)
        else:
            same += 1

    capsule = target / source_distribution.EMBEDDED_PATH
    capsule.parent.mkdir(parents=True, exist_ok=True)
    capsule.write_bytes(source_archive)
    try:
        verify_files = tuple(required) + ((VERSION_FILE,) if manifest is not None else ())
        source_distribution.verify_payload(source_files, lambda relative: (target / relative).read_bytes(), verify_files)
    except (OSError, source_distribution.SourceError) as error:
        print("Refresh verification failed: " + str(error), file=sys.stderr)
        return 1

    for label, names in (("updated", updated), ("added", added)):
        if names:
            print(label + ":")
            for name in names:
                print("   ", name)
    print("unchanged:", same)
    if manifest is not None:
        installed = recorded_version(manifest) or "unknown"
        print()
        print("Installer-managed copy: left " + VERSION_FILE + " alone.")
        print("  It keeps reporting " + installed + ", the build its installer")
        print("  delivered, so the update path still works on this copy.")
        print("  Its code is now current; only its version is not.")
    if updated or added:
        print()
        print("Restart the app: the running process still holds the old code.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "target",
        type=Path,
        help="the extracted VemalsEars folder or installed app directory to update",
    )
    arguments = parser.parse_args(argv)
    return refresh(arguments.target.expanduser().resolve())


if __name__ == "__main__":
    sys.exit(main())
