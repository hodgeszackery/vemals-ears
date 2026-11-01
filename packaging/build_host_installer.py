"""Build the self-contained Windows host installer from pinned inputs."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_host
import build_runtime
from runtime_licenses import preserve_incorporated_notice, validate_runtime_licenses
from runtime_sources import stage_runtime_sources, validate_runtime_sources

PRODUCT = "vemals-ears-host"
OUTPUT_NAME = "Vemals-Ears-Host-Setup"
TEST_OUTPUT_NAME = OUTPUT_NAME + "-TEST-UNSIGNED"
INSTALLER_SOURCE = "Hosting/Installer/VemalsEarsHost.iss"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class HostInstallerError(RuntimeError):
    """The installer could not be built from the selected source and runtime."""


def checked_version(value: str) -> str:
    if not re.fullmatch(r"[0-9]{1,5}(?:\.[0-9]{1,5}){2,3}", value):
        raise HostInstallerError("Version must have three or four numeric components.")
    if any(int(part) > 65535 for part in value.split(".")):
        raise HostInstallerError("Version components must be at most 65535.")
    return value


def source_version(root: Path) -> str:
    path = root / "GuildHub/version.py"
    if path.is_file():
        match = re.search(r'^CLIENT_VERSION = "([0-9.]+)"$', path.read_text(encoding="ascii"), re.M)
        if match:
            return checked_version(match[1])
    return "0.5.13"


def runtime_lock(root: Path) -> dict:
    lock = json.loads((root / "Installer/runtime-lock.json").read_text(encoding="ascii"))
    if lock.get("schema") != 1 or lock.get("architecture") != "x64":
        raise HostInstallerError("The host requires the pinned x64 runtime lock.")
    return lock


def stage_source(root: Path, stage: Path, version: str, *, test_build: bool) -> dict:
    """Copy a fixed, hashed source snapshot into a NEW staging directory."""
    version = checked_version(version)
    root = root.resolve(strict=True)
    files = build_host.snapshot(root)
    for required in ("Hosting/wizard.py", "Hosting/control.py", "Hosting/assets.py",
                     INSTALLER_SOURCE, "Windows/VemalsEars.ico"):
        if required not in files:
            raise HostInstallerError("The host source inventory is missing " + required)
    if stage.exists():
        raise HostInstallerError("Use a new staging directory; existing contents are never merged.")
    stage.mkdir(parents=True)
    for relative, data in files.items():
        target = stage / "app" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    archive = stage / "app/Source/VemalsEars-Host.zip"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(build_host.archive_bytes(files))
    (stage / "VemalsEars.ico").write_bytes(files["Windows/VemalsEars.ico"])
    marker = {
        "schema": 1, "product": PRODUCT, "version": version,
        "test_build": test_build, "source_files": build_host.manifest(files)["files"],
    }
    (stage / "host-install.json").write_text(json.dumps(marker, indent=2) + "\n", encoding="ascii")
    return marker


def stage_runtime(root: Path, stage: Path, cache: Path, existing: Path | None = None) -> None:
    """Use the same checked private Python/Tk runtime as the member installer."""
    lock = runtime_lock(root)
    runtime = stage / "runtime"
    if runtime.exists():
        raise HostInstallerError("The new staging directory already contains a runtime.")
    if existing is not None:
        existing = existing.resolve(strict=True)
        if any(path.is_symlink() or getattr(path, "is_junction", lambda: False)()
               for path in existing.rglob("*")):
            raise HostInstallerError("Linked files are not accepted in a staged private runtime.")
        validate_runtime_licenses(existing, lock)
        validate_runtime_sources(existing, source_lock=root / "Installer/runtime-sources.lock.json",
                                 runtime_lock=root / "Installer/runtime-lock.json")
        shutil.copytree(existing, runtime)
        wheels = [wheel["name"] + " " + wheel["version"] for wheel in lock["wheels"]]
    else:
        build_runtime.stage_interpreter(lock, runtime, cache)
        wheels = build_runtime.vendor_wheels(lock, runtime, cache)
        preserve_incorporated_notice(runtime)
        validate_runtime_licenses(runtime, lock)
        build_runtime.prune(runtime)
        stage_runtime_sources(runtime, cache, source_lock=root / "Installer/runtime-sources.lock.json",
                              runtime_lock=root / "Installer/runtime-lock.json")
    validate_runtime_licenses(runtime, lock)
    validate_runtime_sources(runtime, source_lock=root / "Installer/runtime-sources.lock.json",
                             runtime_lock=root / "Installer/runtime-lock.json")
    build_runtime.smoke_test(runtime, wheels)
    build_runtime.purge_bytecode(runtime)


def stage_tools(stage: Path, cache: Path) -> None:
    # Load the exact source snapshot that will be installed, not a later edit
    # from the working tree. Its lock file is included in that same snapshot.
    path = stage / "app/Hosting/assets.py"
    name = "host_installer_assets_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        module.stage_tools(stage, cache)
    finally:
        sys.modules.pop(name, None)


def verify_stage(stage: Path) -> None:
    marker = json.loads((stage / "host-install.json").read_text(encoding="ascii"))
    if marker.get("schema") != 1 or marker.get("product") != PRODUCT:
        raise HostInstallerError("Host installer identity marker is invalid.")
    archived = build_host.verify_archive((stage / "app/Source/VemalsEars-Host.zip").read_bytes())
    if marker["source_files"] != build_host.manifest(archived)["files"]:
        raise HostInstallerError("Host installer source manifest differs from its source archive.")
    for relative, data in archived.items():
        if (stage / "app" / relative).read_bytes() != data:
            raise HostInstallerError("Staged application differs from its source: " + relative)
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() not in {"PYTHONHOME", "PYTHONPATH", "TCL_LIBRARY", "TK_LIBRARY"}}
    probe = (
        "import tkinter, websockets; from pathlib import Path; "
        "from Hosting import wizard, control, assets; from GuildHub import host, server; "
        "assets.validate_tools(Path.cwd().parent); tkinter.Tcl(); print('HOST-INSTALLER-IMPORT-OK')"
    )
    result = subprocess.run(
        [str(stage / "runtime/python.exe"), "-E", "-s", "-B", "-c", probe],
        cwd=stage / "app", env=environment, capture_output=True, text=True,
        timeout=60, creationflags=NO_WINDOW,
    )
    if result.returncode or "HOST-INSTALLER-IMPORT-OK" not in result.stdout:
        detail = ""
        tool_manifest = stage / "tools/MANIFEST.json"
        if tool_manifest.is_file():
            expected = json.loads(tool_manifest.read_text(encoding="ascii"))["files"]
            actual = {path.relative_to(stage / "tools").as_posix(): path
                      for path in (stage / "tools").rglob("*")
                      if path.is_file() and path != tool_manifest}
            missing = sorted(set(expected) - set(actual))
            extra = sorted(set(actual) - set(expected))
            changed = sorted(name for name in set(expected) & set(actual)
                             if actual[name].stat().st_size != expected[name]["bytes"]
                             or hashlib.sha256(actual[name].read_bytes()).hexdigest() != expected[name]["sha256"])
            if missing or extra or changed:
                detail = "\nTool differences: " + json.dumps({
                    "missing": missing[:20], "extra": extra[:20], "changed": changed[:20],
                })
        raise HostInstallerError("The private host runtime failed imports:\n" + result.stdout + result.stderr + detail)
    for relative in ("tools/node/node.exe", "tools/node/node_modules/npm/bin/npm-cli.js",
                     "tools/nssm/nssm.exe", "tools/cloudflared/cloudflared.exe"):
        if not (stage / relative).is_file():
            raise HostInstallerError("The private host tools are incomplete: " + relative)


def compile_installer(stage: Path, output: Path, iscc: Path) -> Path:
    marker = json.loads((stage / "host-install.json").read_text(encoding="ascii"))
    version = checked_version(marker["version"])
    lock = runtime_lock(stage / "app")
    result = subprocess.run([str(iscc), "--version"], capture_output=True, text=True,
                            timeout=30, creationflags=NO_WINDOW)
    version_lines = result.stdout.strip().splitlines()
    if result.returncode or not version_lines or version_lines[0] != lock["inno_setup"]["version"]:
        raise HostInstallerError("Use the Inno Setup version pinned in Installer/runtime-lock.json.")
    output.mkdir(parents=True, exist_ok=True)
    command = [str(iscc), "--quiet-progress", "/DAppVersion=" + version,
               "/DStageRoot=" + str(stage.resolve()), "/DOutputDir=" + str(output.resolve())]
    if marker["test_build"]:
        command.append("/DTestBuild=1")
    command.append(str(stage / "app" / INSTALLER_SOURCE))
    result = subprocess.run(command, capture_output=True, text=True, timeout=1800, creationflags=NO_WINDOW)
    if result.returncode:
        raise HostInstallerError("Inno Setup compilation failed:\n" + result.stdout + result.stderr)
    target = output / ((TEST_OUTPUT_NAME if marker["test_build"] else OUTPUT_NAME) + ".exe")
    if not target.is_file():
        raise HostInstallerError("Inno Setup did not create the expected host installer.")
    receipt = {"schema": 1, "product": PRODUCT, "version": version,
               "test_build": marker["test_build"], "signed": False,
               "bytes": target.stat().st_size,
               "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}
    target.with_suffix(".json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="ascii")
    return target


def smoke_installer(target: Path) -> None:
    """Install/uninstall only the isolated TestBuild; never start its services."""
    receipt = json.loads(target.with_suffix(".json").read_text(encoding="ascii"))
    if (target.name != TEST_OUTPUT_NAME + ".exe" or receipt.get("product") != PRODUCT
            or receipt.get("test_build") is not True
            or receipt.get("sha256") != hashlib.sha256(target.read_bytes()).hexdigest()):
        raise HostInstallerError("Install smoke tests require the verified, isolated TestBuild artifact.")
    temp_root = Path(os.environ["LOCALAPPDATA"]) / "Temp"
    destination = temp_root / ("VemalsEarsHost-Installer-Test-" + uuid.uuid4().hex)
    if destination.exists():
        raise HostInstallerError("The isolated test installation directory already exists.")
    with tempfile.TemporaryDirectory(prefix="vemals-host-installer-logs-") as folder:
        logs = Path(folder)
        try:
            result = subprocess.run(
                [str(target), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-",
                 "/DIR=" + str(destination), "/LOG=" + str(logs / "install.log")],
                capture_output=True, text=True, timeout=600, creationflags=NO_WINDOW,
            )
            if result.returncode:
                detail = (logs / "install.log").read_text(encoding="utf-8", errors="replace")
                raise HostInstallerError("Isolated host install failed:\n" + detail[-12000:])
            marker = json.loads((destination / "host-install.json").read_text(encoding="ascii"))
            if marker.get("product") != PRODUCT or marker.get("test_build") is not True:
                raise HostInstallerError("Installed host test identity did not match.")
            verify_stage(destination)
            smoke_wizard(destination)
        finally:
            uninstaller = destination / "unins000.exe"
            marker_path = destination / "host-install.json"
            if uninstaller.is_file() and marker_path.is_file():
                marker = json.loads(marker_path.read_text(encoding="ascii"))
                if marker.get("product") == PRODUCT and marker.get("test_build") is True:
                    result = subprocess.run(
                        [str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                         "/LOG=" + str(logs / "uninstall.log")],
                        capture_output=True, text=True, timeout=300, creationflags=NO_WINDOW,
                    )
                    if result.returncode:
                        raise HostInstallerError("Isolated uninstall failed; inspect " + str(destination))
                    deadline = time.monotonic() + 10
                    while destination.exists() and time.monotonic() < deadline:
                        time.sleep(0.1)
            if destination.exists():
                raise HostInstallerError("Isolated installer left files at " + str(destination))
    print("Isolated host install, private-runtime imports, wizard close and uninstall passed.", flush=True)


def smoke_wizard(destination: Path) -> None:
    """Open an in-memory preview and close only its exact private runtime."""
    pythonw = destination / "runtime/pythonw.exe"
    close_script = destination / "app/Hosting/Close-Host.ps1"
    if not close_script.is_file():
        raise HostInstallerError("The installed host is missing its graceful window-close helper.")
    process = subprocess.Popen(
        [str(pythonw), "-E", "-s", "-B", "-m", "Hosting.wizard", "--smoke-test"],
        cwd=destination / "app", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=NO_WINDOW,
    )
    try:
        # Observe only the newly created preview process. No arbitrary process
        # name is used as evidence that this particular window opened.
        ready_script = "\n".join((
            "$deadline = (Get-Date).AddSeconds(20)",
            "do {",
            "  $candidate = Get-Process -Id $env:XENO_HOST_SMOKE_PID -ErrorAction SilentlyContinue",
            "  if (-not $candidate) { exit 2 }",
            "  if ($candidate.MainWindowTitle -eq \"Vemal's Ears - Host\") { exit 0 }",
            "  Start-Sleep -Milliseconds 100",
            "} while ((Get-Date) -lt $deadline)",
            "exit 3",
        ))
        ready = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ready_script],
            env={**os.environ, "XENO_HOST_SMOKE_PID": str(process.pid)},
            capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW,
        )
        if ready.returncode:
            raise HostInstallerError("The installed host wizard did not open its preview window.")
        closed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(close_script),
             "-PythonwPath", str(pythonw)],
            capture_output=True, text=True, timeout=40, creationflags=NO_WINDOW,
        )
        if closed.returncode:
            raise HostInstallerError("The installed host window could not close gracefully:\n" + closed.stderr)
        if process.wait(timeout=10) != 0:
            raise HostInstallerError("The installed host wizard failed while closing.")
    finally:
        # Only our synthetic preview can reach this cleanup fallback.
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default=source_version(ROOT))
    parser.add_argument("--stage", type=Path)
    parser.add_argument("--runtime", type=Path, help="Reuse an already source-complete private runtime")
    parser.add_argument("--cache", type=Path, default=ROOT / "Installer/cache")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--iscc", type=Path, default=Path("C:/Program Files/Inno Setup 7/ISCC.exe"))
    parser.add_argument("--test-build", action="store_true")
    parser.add_argument("--compile-stage", type=Path, help="Verify and compile an existing staged host")
    parser.add_argument("--stage-only", action="store_true")
    parser.add_argument("--smoke-install", action="store_true", help="Install/uninstall only an isolated TestBuild")
    args = parser.parse_args(argv)
    try:
        checked_version(args.version)
        if args.compile_stage:
            stage = args.compile_stage.resolve(strict=True)
        else:
            stage = (args.stage or ROOT / "Installer/build" / ("host-" + uuid.uuid4().hex) / "stage").resolve()
            stage_source(ROOT, stage, args.version, test_build=args.test_build)
            print("Staged host source: " + str(stage), flush=True)
            stage_runtime(stage / "app", stage, args.cache.resolve(), args.runtime)
            stage_tools(stage, args.cache.resolve() / "host-assets")
        verify_stage(stage)
        print("Verified host stage: " + str(stage), flush=True)
        if not args.stage_only:
            if args.smoke_install and not json.loads((stage / "host-install.json").read_text())["test_build"]:
                raise HostInstallerError("--smoke-install requires an isolated TestBuild stage.")
            target = compile_installer(stage, args.output.resolve(), args.iscc.resolve())
            print(target, flush=True)
            if args.smoke_install:
                smoke_installer(target)
    except (HostInstallerError, build_host.HostArchiveError, OSError, ValueError,
            subprocess.SubprocessError) as error:
        parser.exit(1, "Host installer: " + str(error) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
