"""Build the curated corresponding source for the VE member application.

The archive contains current source bytes, including intentional local edits.
An extracted archive can be changed and rebuilt without Git or signing keys.
Operator configuration, worker code and unlisted checkout files are never read.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parent
ARCHIVE_NAME = "vemals-ears-source.zip"
EMBEDDED_PATH = "Source/" + ARCHIVE_NAME
ARCHIVE_ROOT = "VemalsEars-source/"
MANIFEST_NAME = "SOURCE-MANIFEST.json"

# Fixed build inputs, never a recursive checkout copy. Application files are
# added from the product manifest so newly shipped modules get their source.
BUILD_FILES = (
    "LICENSE", "SOURCE-IMPORT.json", "docs/SOURCE.txt",
    "build_release.py", "source_distribution.py",
    "Installer/README.md", "Installer/THIRD-PARTY-NOTICES.txt",
    "Installer/VemalsEars.iss", "Installer/build_installer.ps1",
    "Installer/launcher.c", "Installer/launcher.rc",
    "Installer/runtime-lock.json", "Installer/runtime-sources.lock.json", "Installer/stage_installer.py",
    "Installer/uninstall_cleanup.py", "Installer/sign_file.ps1",
    "Installer/verify_signature.ps1", "Installer/prove_signing.ps1",
    "Installer/smoke_test_installer.ps1",
    "Windows/DowntimeLauncher.c", "Windows/VemalsEars-ARTWORK.txt",
    "Windows/tests/NativePairingContract.c", "Windows/tests/NativeProductSmoke.pyw",
    "Windows/verify_native_products.ps1",
    "packaging/build_runtime.py", "packaging/verify_bundle.py", "packaging/runtime_licenses.py",
    "packaging/runtime_sources.py",
)


class SourceError(RuntimeError):
    """Corresponding source is missing, unsafe or differs from the payload."""


def source_path(root: Path, relative: str) -> Path:
    logical = PurePosixPath(relative)
    forbidden_parts = {".git", "dist", "node_modules", "__pycache__", "secrets", "cache", "build"}
    if (logical.is_absolute() or ".." in logical.parts or not logical.parts or "\\" in relative
            or ":" in relative or forbidden_parts.intersection(part.casefold() for part in logical.parts)
            or any(part.casefold().startswith(".env") or ".sqlite" in part.casefold() for part in logical.parts)):
        raise SourceError("unsafe corresponding-source path: " + relative)
    path = root.joinpath(*logical.parts)
    if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise SourceError("missing or escaping corresponding source: " + relative)
    current = path
    while current != root:
        if current.is_symlink():
            raise SourceError("linked corresponding source is refused: " + relative)
        current = current.parent
    return path


def git_commit(root: Path) -> str | None:
    """Git is optional; hashes identify the actual source regardless of HEAD."""
    if not (root / ".git").exists():
        return None
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                                capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def snapshot(root: Path, application_files: tuple[str, ...]) -> dict[str, bytes]:
    root = root.resolve(strict=True)
    names = sorted(set(BUILD_FILES) | set(application_files))
    return {relative: source_path(root, relative).read_bytes() for relative in names}


def archive_bytes(files: dict[str, bytes]) -> bytes:
    """Stable order, timestamps and metadata make identical source repeatable."""
    manifest = {"schema": 1, "files": [
        {"path": relative, "sha256": hashlib.sha256(data).hexdigest()}
        for relative, data in sorted(files.items())
    ]}
    records = dict(files)
    records[MANIFEST_NAME] = (json.dumps(manifest, indent=2, ensure_ascii=True) + "\n").encode("ascii")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative, data in sorted(records.items()):
            info = zipfile.ZipInfo(ARCHIVE_ROOT + relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    return output.getvalue()


def read_archive(data: bytes, allowed_files: set[str]) -> dict[str, bytes]:
    """Read a prior source snapshot without extracting or trusting extra paths."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            allowed = {ARCHIVE_ROOT + relative for relative in allowed_files | {MANIFEST_NAME}}
            if len(names) != len(set(names)) or not set(names).issubset(allowed):
                raise SourceError("the prior source archive has duplicate or unlisted files")
            files = {relative: archive.read(ARCHIVE_ROOT + relative) for relative in allowed_files
                     if ARCHIVE_ROOT + relative in names}
            manifest = json.loads(archive.read(ARCHIVE_ROOT + MANIFEST_NAME))
            records = [{"path": relative, "sha256": hashlib.sha256(payload).hexdigest()}
                       for relative, payload in sorted(files.items())]
            if manifest != {"schema": 1, "files": records}:
                raise SourceError("the prior source archive does not match its recorded source hashes")
            return files
    except (zipfile.BadZipFile, KeyError, ValueError, TypeError) as error:
        raise SourceError("the prior corresponding-source archive is invalid") from error


def write_source_tree(files: dict[str, bytes], target: Path) -> None:
    """The native compiler consumes these same bytes, outside the app payload."""
    for relative, data in files.items():
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def verify_payload(files: dict[str, bytes], read_payload, application_files: tuple[str, ...]) -> None:
    for relative in application_files:
        if relative not in files or read_payload(relative) != files[relative]:
            raise SourceError("payload differs from corresponding source: " + relative)
    if read_payload(EMBEDDED_PATH) != archive_bytes(files):
        raise SourceError("payload corresponding-source archive differs from its source snapshot")


def validate_runtime(
    runtime: Path, source_root: Path = ROOT, *, allow_source_receipt: bool = False,
) -> None:
    """Every distribution gate verifies notices and matching upstream sources."""
    sys.path.insert(0, str(ROOT / "packaging"))
    from runtime_licenses import validate_runtime_licenses
    from runtime_sources import validate_runtime_sources
    lock_path = source_root / "Installer/runtime-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    validate_runtime_licenses(runtime, lock)
    validate_runtime_sources(
        runtime,
        source_lock=source_root / "Installer/runtime-sources.lock.json",
        runtime_lock=lock_path,
        allow_receipt_only=allow_source_receipt,
    )


def build(root: Path, application_files: tuple[str, ...], target: Path) -> Path:
    files = snapshot(root, application_files)
    data = archive_bytes(files)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def main(argv: list[str] | None = None) -> int:
    from build_release import WINDOWS_INSTALLER_APP_FILES
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / ARCHIVE_NAME)
    parser.add_argument("--verify-stage", type=Path,
                        help="Verify an installer app/source/runtime stage before compiling its installer")
    arguments = parser.parse_args(argv)
    if arguments.verify_stage is not None:
        stage = arguments.verify_stage
        validate_runtime(stage / "runtime", ROOT)
        files = snapshot(ROOT, WINDOWS_INSTALLER_APP_FILES)
        verify_payload(files, lambda relative: (stage / "app" / relative).read_bytes(), WINDOWS_INSTALLER_APP_FILES)
        print("Installer source, app payload and runtime distribution verified.")
        return 0
    print(build(ROOT, WINDOWS_INSTALLER_APP_FILES, arguments.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
