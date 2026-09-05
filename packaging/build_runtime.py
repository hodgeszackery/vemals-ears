"""Stage the self-contained Python runtime both bundles embed.

Members should unzip and double-click, not install Python. A venv cannot
deliver that - it hard-codes the creating machine's interpreter path - so
this stages an official python.org build instead.

Everything is pinned by sha256 in Installer/runtime-lock.json, which is the
same file the signed installer stages from (Installer/build_installer.ps1).
One lock, one set of versions: the ZIP a member downloads today and the
installer they get once it is signed cannot drift apart, and a version bump
is one edit in one file.

The pinned artifact is the install-free distribution, which already carries
tkinter with Tcl/Tk embedded. That matters more than it sounds: the earlier
version of this script harvested tkinter out of the builder's own Python,
which forced the build machine - and CI - to have that exact version
installed with tcl/tk present. Nothing here now depends on the machine it
runs on beyond being Windows.

The stage ends by running the staged interpreter, because a runtime that was
assembled but never executed is not a verified runtime.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

from runtime_licenses import preserve_incorporated_notice, validate_runtime_licenses
from runtime_sources import stage_runtime_sources, validate_runtime_sources

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
LOCK = ROOT / "Installer" / "runtime-lock.json"

# Weight a member downloads once and never opens. The interpreter, its
# standard library, tkinter and the pinned wheels all stay.
#
# This runs AFTER the wheels are vendored and BEFORE the smoke test, so the
# smoke test is what proves the pruning did not cut something the companion
# imports. Do not reorder those three.
PRUNE_DIRECTORIES = (
    "Doc",            # the full HTML documentation set
    "include",        # C headers, for building extensions
    "libs",           # import libraries, for linking extensions
    "Lib/test",       # CPython's own test suite
    "Lib/idlelib",    # the IDLE editor
    "Lib/tkinter/test",
    "Lib/lib2to3",
    "Tools",
    # Nothing in a member bundle installs a package: the wheels are already
    # vendored and the runtime is read-only from the member's point of view.
    "Lib/ensurepip",
    "Lib/site-packages/pip",
    # pywin32 is here for one thing, the tray icon (win32gui/win32con/
    # win32api). Its COM automation stack and its bundled IDE are not that.
    "Lib/site-packages/pythonwin",
    "Lib/site-packages/win32com",
    "Lib/site-packages/win32comext",
)


def load_lock() -> dict:
    try:
        lock = json.loads(LOCK.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SystemExit("cannot read " + str(LOCK) + ": " + str(error))
    if lock.get("schema") != 1:
        raise SystemExit("runtime-lock.json schema is not 1 - this stager "
                         "and the installer read the same file, so check "
                         "Installer/build_installer.ps1 before changing it")
    if lock.get("architecture") != "x64":
        raise SystemExit("runtime-lock.json is not x64")
    return lock


def fetch(url: str, destination: Path, sha256: str) -> Path:
    """Download once, verify always - a cached file is re-verified too."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.is_file():
        print("downloading " + url)
        with urllib.request.urlopen(url, timeout=600) as response:
            payload = response.read()
        destination.write_bytes(payload)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    if digest != sha256.lower():
        destination.unlink()
        raise SystemExit(
            "sha256 mismatch for " + destination.name + ": expected "
            + sha256 + ", got " + digest + " - the cached file was removed; "
            "if a re-run repeats this, the lock and the source disagree and "
            "the download must not be trusted")
    return destination


def stage_interpreter(lock: dict, runtime: Path, cache: Path) -> None:
    pinned = lock["python"]
    archive = fetch(pinned["url"], cache / Path(pinned["url"]).name,
                    pinned["sha256"])
    if runtime.exists():
        shutil.rmtree(runtime)
    runtime.mkdir(parents=True)
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(runtime)
    for name in ("python.exe", "pythonw.exe"):
        if not (runtime / name).is_file():
            raise SystemExit(
                name + " is missing from " + pinned["url"] + " - the pinned "
                "distribution's layout changed and this stager needs "
                "updating")


def prune(runtime: Path) -> int:
    removed = 0
    for relative in PRUNE_DIRECTORIES:
        target = runtime / relative
        if target.is_dir():
            removed += sum(1 for path in target.rglob("*") if path.is_file())
            shutil.rmtree(target)
    return removed


def vendor_wheels(lock: dict, runtime: Path, cache: Path) -> list[str]:
    """Unpack the pinned wheels where the interpreter already looks.

    Pure unpacking, not pip: nothing resolves a dependency at build time,
    so what ships is exactly what the lock names and nothing it pulled in.
    """
    site_packages = runtime / "Lib" / "site-packages"
    site_packages.mkdir(parents=True, exist_ok=True)
    staged = []
    for wheel in lock.get("wheels", ()):
        path = fetch(wheel["url"], cache / wheel["filename"], wheel["sha256"])
        with zipfile.ZipFile(path) as bundle:
            for entry in bundle.namelist():
                if entry.endswith("/"):
                    continue
                if ".data/" in entry:
                    scheme = entry.split(".data/", 1)[1].split("/", 1)[0]
                    # purelib and platlib mean "site-packages", which is
                    # where everything else is going anyway.
                    if scheme in ("purelib", "platlib"):
                        target = entry.split(".data/", 1)[1].split("/", 1)[1]
                        destination = site_packages / target
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        with bundle.open(entry) as source:
                            destination.write_bytes(source.read())
                        continue
                    # scripts and headers are developer tools - pywin32's
                    # postinstall and testall - that nothing in a member
                    # bundle runs. Anything else needs real install-scheme
                    # handling, so refuse rather than half-install it.
                    if scheme in ("scripts", "headers"):
                        continue
                    raise SystemExit(
                        wheel["filename"] + " carries a ." + scheme
                        + " payload that unpacking alone cannot place")
                bundle.extract(entry, site_packages)
        staged.append(wheel["name"] + " " + wheel["version"])
    return staged


def purge_bytecode(runtime: Path) -> int:
    """A .pyc records the absolute source path of the machine that wrote it.

    One written here would ship the builder's user folder to every member,
    so they are removed as well as suppressed with -B.
    """
    removed = 0
    for folder in sorted(runtime.rglob("__pycache__"), reverse=True):
        if folder.is_dir():
            shutil.rmtree(folder)
            removed += 1
    return removed


def smoke_test(runtime: Path, wheels: list[str]) -> None:
    """Run the staged interpreter; assembly without execution proves nothing.

    -E -s is how the launchers start it: ignore the member's PYTHON*
    variables and their user site-packages, so what imports here is what
    ships here.
    """
    checks = [
        "import sys, asyncio, ctypes, ssl, sqlite3, json, tkinter",
        "import websockets",
        "from websockets.asyncio.client import connect",
        "interpreter = tkinter.Tcl()",
        "print('python', sys.version.split()[0])",
        "print('tcl', interpreter.eval('info patchlevel'))",
        "print('websockets', websockets.__version__)",
    ]
    if any(name.startswith("pillow") for name in wheels):
        checks += ["from PIL import Image, ImageTk, ImageDraw, ImageChops",
                   "print('pillow ok')"]
    if any(name.startswith("pywin32") for name in wheels):
        # The tray icon. Guarded at every call site, so a failure here is
        # a missing feature rather than a dead companion - but it is
        # pinned in the lock, so it is meant to be present.
        checks += ["import win32gui, win32con, win32api",
                   "print('pywin32 ok')"]
    checks.append("print('RUNTIME-OK')")

    environment = {key: value for key, value in os.environ.items()
                   if key.upper() not in ("PYTHONPATH", "PYTHONHOME",
                                          "PYTHONSTARTUP", "TCL_LIBRARY",
                                          "TK_LIBRARY")}
    result = subprocess.run(
        [str(runtime / "python.exe"), "-E", "-s", "-B", "-c",
         "\n".join(checks)],
        capture_output=True, text=True, timeout=300,
        cwd=str(runtime.parent), env=environment)
    if result.returncode != 0 or "RUNTIME-OK" not in result.stdout:
        raise SystemExit(
            "staged runtime failed its own smoke test (exit "
            + str(result.returncode) + "):\n" + result.stdout + result.stderr)
    print(result.stdout.strip())


def main() -> int:
    if sys.platform != "win32":
        raise SystemExit("the runtime is staged on Windows only")
    lock = load_lock()
    runtime = DIST / "runtime"
    cache = DIST / "cache"

    stage_interpreter(lock, runtime, cache)
    wheels = vendor_wheels(lock, runtime, cache)
    preserve_incorporated_notice(runtime)
    validate_runtime_licenses(runtime, lock)
    dropped = prune(runtime)
    validate_runtime_licenses(runtime, lock)
    stage_runtime_sources(runtime, cache)
    validate_runtime_sources(runtime)
    smoke_test(runtime, wheels)
    purge_bytecode(runtime)

    total = sum(path.stat().st_size for path in runtime.rglob("*")
                if path.is_file())
    print("staged " + str(runtime))
    print("  python " + lock["python"]["version"]
          + ", wheels: " + (", ".join(wheels) or "none"))
    print("  pruned " + str(dropped) + " files not needed to run")
    print("  {0:.1f} MB on disk".format(total / (1024 * 1024)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
