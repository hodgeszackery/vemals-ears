"""Build the standalone Vemal's Ears member bundle."""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

import source_distribution


ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"


DOWNTIME_UI_FILES = (
    "downtime_ui/__init__.py",
    "downtime_ui/theme.py",
    "downtime_ui/effects.py",
    "downtime_ui/backgrounds.py",
    "downtime_ui/buttons.py",
    "downtime_ui/indicators.py",
    "downtime_ui/cards.py",
    "downtime_ui/headers.py",
    "downtime_ui/tables.py",
    "downtime_ui/menus.py",
    "downtime_ui/inputs.py",
    "downtime_ui/tabs.py",
)


VEMALS_EARS_CLIENT_FILES = (
    "GuildHub/CLIENT-README.txt",
    "GuildHub/__init__.py",
    "GuildHub/client.py",
    "GuildHub/credentials.py",
    "GuildHub/pairing.py",
    "GuildHub/protocol.py",
    "GuildHub/requirements-client.txt",
    "GuildHub/setup.py",
    # The version the Preferences panel shows and the member client imports.
    "GuildHub/version.py",
    "GuildHub/windows_integration.py",
    "GuildHub/windows_taskbar.py",
)


VEMALS_EARS_MEMBER_APP_FILES = (
    "LICENSE",
    "docs/SOURCE.txt",
    "Installer/THIRD-PARTY-NOTICES.txt",
    "Installer/INNO-SETUP-LICENSE.txt",
    "Vemals-Ears.pyw",
    "WoW-Companion.pyw",
    "Windows/VemalsEars.png",
    "Windows/VemalsEars.ico",
    "VoiceCaptions/__init__.py",
    "VoiceCaptions/events.py",
    "VoiceCaptions/layout.py",
    "VoiceCaptions/member_overlay.py",
    "VoiceCaptions/overlay.py",
    "VoiceCaptions/placement.py",
    "VoiceCaptions/prosody.py",
    "VoiceCaptions/settings.py",
    "VoiceCaptions/speaker_directory.py",
    *VEMALS_EARS_CLIENT_FILES,
    *DOWNTIME_UI_FILES,
)


WINDOWS_INSTALLER_APP_FILES = (
    *VEMALS_EARS_MEMBER_APP_FILES,
    "Installer/uninstall_cleanup.py",
)


MEMBER_PREFIX = "VemalsEars/"


def launcher_cmd(
    product: str, archive: str, launcher: str, entrypoint: str = "WoW-Companion.pyw"
) -> str:
    """Build a runtime launcher for the chosen application entrypoint."""
    return "\r\n".join((
        "@echo off",
        "rem Starts " + product + " with the Python runtime bundled in the",
        "rem runtime folder beside this file. Nothing needs installing.",
        "setlocal",
        'set "TCL_LIBRARY="',
        'set "TK_LIBRARY="',
        'set "PYTHONHOME="',
        'set "PYTHONPATH="',
        'if not exist "%~dp0runtime\\pythonw.exe" (',
        "    echo " + product + " is not fully extracted, so it cannot start.",
        "    echo.",
        "    echo Right-click the downloaded " + archive + ", choose Extract",
        "    echo All, then run " + launcher + " from the extracted",
        "    echo folder - not from inside the zip preview window.",
        "    echo.",
        "    pause",
        "    exit /b 1",
        ")",
        # -E -s is what the signed installer uses too: ignore the member's
        # PYTHON* variables and their user site-packages, so a stray
        # package on their machine cannot shadow one of ours. The script's
        # own folder is still on sys.path, which is how GuildHub and
        # VoiceCaptions resolve.
        'start "" "%~dp0runtime\\pythonw.exe" -E -s "%~dp0' + entrypoint + '"',
    )) + "\r\n"


def member_launcher() -> str:
    return launcher_cmd("Vemal's Ears", "vemals-ears-member.zip",
                        "Start-Vemals-Ears.cmd", "Vemals-Ears.pyw")


def build_member_companion(require_runtime: bool = False) -> tuple[Path, list[str]]:
    """Build the generic Vemal's Ears member application for the relay.

    This is the artifact the relay serves, so it is the one that has to
    open on a machine with nothing installed: the same staged runtime the
    full companion embeds rides along, and Start-Vemals-Ears.cmd runs it.
    The file allowlist stays narrow - no sim runner, no log-analysis
    credential tool, no Discord capture, no speech model - so carrying a
    runtime widens what members can RUN, never what they are HANDED.
    """
    DIST.mkdir(exist_ok=True)
    target = DIST / "vemals-ears-member.zip"
    missing = []

    runtime = DIST / "runtime"
    bundled = (runtime / "pythonw.exe").is_file()
    if require_runtime and not bundled:
        raise SystemExit(
            "no staged runtime at " + str(runtime) + " - run "
            "packaging/build_runtime.py first (the relay serves this zip, "
            "so it must never silently need an installed Python)")
    if runtime.exists() and not bundled:
        raise SystemExit("the staged runtime is incomplete: runtime/pythonw.exe is missing")
    if bundled:
        source_distribution.validate_runtime(runtime, ROOT)

    source_files = source_distribution.snapshot(ROOT, WINDOWS_INSTALLER_APP_FILES)
    source_archive = source_distribution.archive_bytes(source_files)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, strict_timestamps=False) as archive:
        for relative in VEMALS_EARS_MEMBER_APP_FILES:
            archive.writestr(MEMBER_PREFIX + relative, source_files[relative])
        archive.writestr(MEMBER_PREFIX + source_distribution.EMBEDDED_PATH, source_archive)
        archive.writestr(
            MEMBER_PREFIX + "vemals-ears-member.json",
            json.dumps({"edition": "member", "schema": 1}, separators=(",", ":"))
            + "\n",
        )
        if bundled:
            for path in sorted(runtime.rglob("*")):
                if path.is_symlink():
                    missing.append(str(path.relative_to(ROOT)))
                    continue
                if path.is_file():
                    archive.write(
                        path,
                        MEMBER_PREFIX + "runtime/"
                        + path.relative_to(runtime).as_posix(),
                    )
            archive.writestr(MEMBER_PREFIX + "Start-Vemals-Ears.cmd",
                             member_launcher())
        archive.writestr(MEMBER_PREFIX + "HOW-TO-RUN.txt",
                         member_readme(bundled))

    with zipfile.ZipFile(target) as archive:
        source_distribution.verify_payload(source_files, lambda relative: archive.read(MEMBER_PREFIX + relative),
                                           VEMALS_EARS_MEMBER_APP_FILES)
    (DIST / source_distribution.ARCHIVE_NAME).write_bytes(source_archive)
    return target, missing


MEMBER_README_HEAD = """Vemal's Ears
============

This desktop program is separate from the World of Warcraft addons. It shows
your guild's consent-gated Discord voice captions in a small movable window
above the game.

It is the member edition: it carries the encrypted caption receiver and the
overlay, and nothing else. No SimulationCraft runner, no Raidbots importer, no
Warcraft Logs credential tool, no Discord capture worker, no speech model, no
server state. Those live on the guild host, not on your computer.

"""


MEMBER_RUN_BUNDLED = """START IT

Double-click Start-Vemals-Ears.cmd and leave it open while you play.

Nothing needs installing first. The runtime folder holds this program's own
Python (an official python.org build, signed by the Python Software
Foundation), used only by Vemal's Ears - it does not change or depend on any
Python already on the computer. If Windows asks once whether to run
Start-Vemals-Ears.cmd, choose Run.

"""


MEMBER_RUN_PLAIN = """START IT

This copy does not carry its own Python, so it needs Python 3 for Windows
installed, plus one networking package. In this folder, run:

    py -m pip install --require-hashes -r GuildHub\\requirements-client.txt

Then double-click Vemals-Ears.pyw and leave it open while you play.

"""


MEMBER_README_TAIL = """The member edition has no Start or Stop controls. It
reconnects the caption receiver on its own and never starts local simulation
services.

PAIR THIS COMPUTER

Open the one-use pairing link your guild administrator sent you and choose
Open Vemal's Ears when the browser asks. If the browser cannot open it, choose
Pair in the window and paste the complete invitation. If the link expired or
was already used, ask your administrator for a new one.

PRIVACY

Your computer receives caption text, never Discord voice audio. The guild's
bot token never comes to your computer. Your device credential is revocable
and stays in Windows Credential Manager. Caption text stays in memory and is
not written to an addon file or a log. Every Discord speaker must consent
before the guild host captures or transcribes them.

Keep the VoiceCaptions, GuildHub and runtime folder names as they are.

Vemal's Ears can run alongside Downtime or Downtime Lite. Each app has its own
startup setting and guild pairing. Updating or uninstalling Vemal's Ears does
not remove Downtime's settings or its job-server pairing.
"""


def member_readme(bundled: bool) -> str:
    run = MEMBER_RUN_BUNDLED if bundled else MEMBER_RUN_PLAIN
    return MEMBER_README_HEAD + run + MEMBER_README_TAIL


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--member', action='store_true', help='Build the member bundle (default).')
    parser.add_argument('--require-runtime', action='store_true', help='Require the staged private runtime.')
    args = parser.parse_args(argv)
    target, missing = build_member_companion(require_runtime=args.require_runtime)
    print(target)
    if missing:
        print('MISSING member files: ' + ', '.join(missing), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
