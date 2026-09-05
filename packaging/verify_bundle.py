"""Extract each built bundle and run it the way a member would.

Everything else about these bundles is checked against the SOURCE tree: the
packaging tests assert which names are in each archive, and build_runtime
smoke-tests the staged runtime in dist/. Neither proves the thing that
actually matters - that the interpreter inside the zip, unpacked into a
folder it has never seen, can import the modules the companion needs.

That gap is where the expensive failures live, and it is why the probe is
written into the extracted folder and run as a SCRIPT rather than with
-c: the bundle root reaches sys.path because it is the script's own
directory, exactly as it does when the launcher starts WoW-Companion.pyw.
A -c probe would import nothing and prove less than nothing.

The member bundle is the one the relay serves, so a failure there is a
failure in front of the people this product exists for. The hosted bundle is
equally strict: it must run the forwarding clients without recovering any
local simulation or server implementation by accident.

Run after build_release.py --member --require-runtime.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"

# Every launcher that opens a window must clear these: a stale TCL_LIBRARY
# from another Python points the bundled interpreter at a foreign Tcl and
# the caption window dies on import. This is a real failure that was caught
# here, not a theoretical one.
POISON_VARIABLES = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP",
                    "TCL_LIBRARY", "TK_LIBRARY")

# Pillow is what scales the artwork and builds the .ico. A bundle whose
# runtime is missing it still starts, so nothing here used to notice - and
# one shipped that way, showing the middle of the 256px clock as the logo,
# in the full Downtime bundle. It is pinned in runtime-lock.json, so its
# absence is a staging failure, and every bundle that draws a window needs
# it. That is all of them.
ARTWORK_IMPORTS = ("PIL.Image", "PIL.ImageTk")

# What each bundle has to be able to import before it can show anything.
MEMBER_IMPORTS = ARTWORK_IMPORTS + (
    "tkinter",
    "websockets",
    "GuildHub.client",
    "GuildHub.credentials",
    "GuildHub.pairing",
    "GuildHub.protocol",
    "GuildHub.windows_integration",
    "GuildHub.windows_taskbar",
    "VoiceCaptions.member_overlay",
    "VoiceCaptions.events",
    "VoiceCaptions.layout",
    "VoiceCaptions.settings",
    "VoiceCaptions.placement",
    "VoiceCaptions.speaker_directory",
)

BUNDLES = (
    {
        "archive": "vemals-ears-member.zip",
        "root": "VemalsEars",
        "launcher": "Start-Vemals-Ears.cmd",
        "imports": MEMBER_IMPORTS,
        "audience": "the member download the relay serves",
    },
)


def probe_source(imports) -> str:
    lines = ["import sys"]
    lines += ["import " + name for name in imports]
    lines += [
        "import tkinter",
        "interpreter = tkinter.Tcl()",
        "print('tcl', interpreter.eval('info patchlevel'))",
        "print('python', sys.version.split()[0])",
        "print('BUNDLE-OK')",
    ]
    return "\n".join(lines) + "\n"


def stripped_environment() -> dict:
    """The environment a member's launcher actually delivers.

    PATH keeps its shape minus every Python, because nothing clears PATH
    and the runtime must never find anything through it. The poisoning
    variables are absent because the launchers clear them, which is
    asserted separately - so this is not assuming the fix, it is checking
    the two halves in the two places they live.
    """
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() not in POISON_VARIABLES}
    parts = [part for part in environment.get("PATH", "").split(os.pathsep)
             if part and "python" not in part.lower()
             and "WindowsApps" not in part]
    environment["PATH"] = os.pathsep.join(parts)
    return environment


def check_launcher(root: Path, name: str) -> list[str]:
    problems = []
    launcher = root / name
    if not launcher.is_file():
        return [name + " is missing from the bundle"]
    raw = launcher.read_bytes()
    if b"\r\n" not in raw:
        problems.append(name + " has no CRLF - cmd.exe mis-parses bare-LF "
                        "batch files")
    text = raw.decode("ascii", errors="replace")
    if "if not exist" not in text:
        problems.append(
            name + " has no guard for a missing runtime, so running it from "
            "inside the zip preview fails with a console flash and no "
            "message - the hardest possible support report to answer")
    if "pause" not in text:
        problems.append(name + " does not pause on that guard, so the "
                        "message it prints is never read")
    for variable in ("TCL_LIBRARY", "TK_LIBRARY"):
        if variable not in text:
            problems.append(name + " does not clear " + variable)
    return problems


def check_runtime_carries_no_repo_code(root: Path) -> list[str]:
    """The runtime widens what a member can RUN, never what they are HANDED.

    Embedding an interpreter is the one place a broad directory copy
    enters a member bundle, so it gets its own check: none of this
    repository's tooling may ride in under runtime/. The member bundle's
    whole argument is that a sim runner, a Warcraft Logs credential tool
    and a Discord capture worker never reach a member's machine, and that
    argument is only as good as what is actually in the archive.
    """
    problems = []
    banned = ("gearhelper", "boss_talents", "raidbots", "whisper",
              "discord_adapter", "voice_captions", "coordinator",
              "publisher", "guildhub", "consent")
    for path in (root / "runtime").rglob("*"):
        if not path.is_file():
            continue
        name = path.name.lower()
        for word in banned:
            if word in name:
                problems.append("runtime carries " + word + ": "
                                + str(path.relative_to(root)))
    return problems


def check_no_builder_traces(root: Path) -> list[str]:
    """No bytecode, and nothing naming the machine that built it."""
    problems = []
    caches = [path for path in root.rglob("__pycache__") if path.is_dir()]
    if caches:
        problems.append(
            str(len(caches)) + " __pycache__ folder(s) shipped; a .pyc "
            "embeds the absolute source path of the build machine")
    builder = str(Path.home()).lower()
    for path in (root / "runtime").rglob("*"):
        if path.suffix.lower() in {".pth", ".cfg", ".txt"} and path.is_file():
            body = path.read_text(encoding="utf-8", errors="replace").lower()
            if builder in body:
                problems.append(path.name + " names the build machine's "
                                "home folder")
    return problems


def verify(bundle: dict) -> list[str]:
    archive_path = DIST / bundle["archive"]
    if not archive_path.is_file():
        return ["no bundle at " + str(archive_path) + " - build it first"]

    with tempfile.TemporaryDirectory() as folder:
        # A path the build never saw, with a space in it, because a real
        # member folder is likelier to look like this than like a repo.
        destination = Path(folder) / "Program Files Test"
        destination.mkdir()
        with zipfile.ZipFile(archive_path) as archive:
            archive.extractall(destination)
        root = destination / bundle["root"]
        interpreter = root / "runtime" / "python.exe"
        if not interpreter.is_file():
            return [bundle["archive"] + " carries no runtime - it would "
                    "require an installed Python"]

        problems = (check_launcher(root, bundle["launcher"])
                    + check_no_builder_traces(root)
                    + check_runtime_carries_no_repo_code(root))

        # Written INTO the bundle so the bundle root is the script's own
        # directory, which is the only reason its packages import.
        probe = root / "_verify_probe.py"
        probe.write_text(probe_source(bundle["imports"]), encoding="ascii")
        result = subprocess.run(
            [str(interpreter), "-E", "-s", "-B", str(probe)],
            capture_output=True, text=True, timeout=300,
            cwd=str(Path(folder)), env=stripped_environment())
        probe.unlink()

        if result.returncode != 0 or "BUNDLE-OK" not in result.stdout:
            problems.append(
                bundle["archive"] + ": the extracted runtime could not "
                "import what the companion needs (exit "
                + str(result.returncode) + "):\n"
                + result.stdout + result.stderr)
        else:
            print("  " + " ".join(result.stdout.split()))
    return problems


def main() -> int:
    problems = []
    for bundle in BUNDLES:
        print(bundle["archive"] + " - " + bundle["audience"])
        problems += verify(bundle)

    if problems:
        print("\nBUNDLE VERIFICATION FAILED")
        for problem in problems:
            print("  - " + problem)
        return 1
    print("\nall bundles run from a fresh extraction with no installed "
          "Python")
    return 0


if __name__ == "__main__":
    sys.exit(main())
