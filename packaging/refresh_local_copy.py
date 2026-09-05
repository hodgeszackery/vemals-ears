"""Update an extracted Downtime folder in place from this checkout.

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

    python packaging/refresh_local_copy.py "C:/path/to/Downtime"
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

    updated, added, same = [], [], 0
    for relative in required:
        source = ROOT / relative
        destination = target / relative
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            added.append(relative)
        elif digest(source) != digest(destination):
            shutil.copy2(source, destination)
            updated.append(relative)
        else:
            same += 1

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
