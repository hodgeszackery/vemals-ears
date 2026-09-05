"""Build the exact generic application payload for the Windows installer."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
MEMBER_EDITION_PATH = "vemals-ears-member.json"
MEMBER_EDITION_DOCUMENT = {"edition": "member", "schema": 1}


class StageError(RuntimeError):
    """The installer payload could not be assembled safely."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _safe_source_file(source_root: Path, relative: str) -> Path:
    logical = PurePosixPath(relative)
    if logical.is_absolute() or ".." in logical.parts or not logical.parts:
        raise StageError("unsafe installer allowlist path: " + relative)
    candidate = source_root.joinpath(*logical.parts)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise StageError("missing installer file: " + relative) from error
    if not _is_relative_to(resolved, source_root) or not resolved.is_file():
        raise StageError("installer file escapes the source tree: " + relative)
    current = candidate
    while current != source_root:
        if current.is_symlink():
            raise StageError("installer file uses a link: " + relative)
        current = current.parent
    return resolved


def _git_output(source_root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=source_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        check=False,
    )
    if result.returncode != 0:
        raise StageError("git could not verify the installer source checkout")
    return result.stdout.strip()


def require_clean_checkout(source_root: Path) -> None:
    if _git_output(source_root, "status", "--porcelain", "--untracked-files=all"):
        raise StageError("signed installers must be built from a clean checkout")


def require_tracked_file(source_root: Path, relative: str) -> None:
    tracked = _git_output(
        source_root,
        "ls-files",
        "--error-unmatch",
        "--",
        relative,
    ).replace("\\", "/")
    if tracked != relative:
        raise StageError("signed installer file is not tracked: " + relative)


def _version_from_toc(source_root: Path) -> str:
    """Read the product version without importing application code."""
    source = source_root / 'GuildHub' / 'version.py'
    for line in source.read_text(encoding='ascii').splitlines():
        if line.startswith('CLIENT_VERSION = '):
            import ast
            version = ast.literal_eval(line.partition('=')[2].strip())
            if isinstance(version, str):
                return version
    raise StageError('GuildHub/version.py does not declare an installer version')


def _load_allowlist(source_root: Path) -> tuple[str, ...]:
    sys.path.insert(0, str(source_root))
    try:
        from build_release import WINDOWS_INSTALLER_APP_FILES
    except (ImportError, AttributeError) as error:
        raise StageError("the Windows installer allowlist is unavailable") from error
    values = tuple(WINDOWS_INSTALLER_APP_FILES)
    if len(values) != len(set(values)):
        raise StageError("the Windows installer allowlist contains duplicates")
    return values


def _write_icon(source_root: Path, target: Path) -> None:
    # The installer and native launcher share the reviewed, multisize VE
    # artwork. Never regenerate this from Downtime's clock: that makes two
    # independent applications indistinguishable in Explorer and the taskbar.
    artwork = _safe_source_file(
        source_root,
        "Windows/VemalsEars.ico",
    )
    shutil.copyfile(artwork, target)


def stage_payload(
    source_root: Path,
    stage_root: Path,
    version: str,
    *,
    require_clean: bool = False,
) -> dict[str, object]:
    source_root = source_root.resolve(strict=True)
    if require_clean:
        require_clean_checkout(source_root)
    declared_version = _version_from_toc(source_root)
    if version != declared_version:
        raise StageError(
            "installer version " + version + " does not match Vemals Ears " + declared_version
        )

    app_root = stage_root / "app"
    if app_root.exists():
        raise StageError("the installer app staging directory already exists")
    app_root.mkdir(parents=True)

    allowlist = _load_allowlist(source_root)
    if require_clean:
        for relative in allowlist:
            require_tracked_file(source_root, relative)

    records: list[dict[str, str]] = []
    for relative in allowlist:
        source = _safe_source_file(source_root, relative)
        target = app_root.joinpath(*PurePosixPath(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        records.append({"path": relative, "sha256": _sha256(target)})

    member_marker = app_root / MEMBER_EDITION_PATH
    member_marker.write_text(
        json.dumps(MEMBER_EDITION_DOCUMENT, separators=(",", ":")) + "\n",
        encoding="ascii",
    )
    records.append({"path": MEMBER_EDITION_PATH, "sha256": _sha256(member_marker)})

    _write_icon(source_root, stage_root / "VemalsEars.ico")
    manifest: dict[str, object] = {
        "schema": 1,
        "product": "Vemal's Ears",
        "version": version,
        "source_commit": _git_output(source_root, "rev-parse", "HEAD"),
        "generic": True,
        "audience": "member",
        "guild_configuration_embedded": False,
        "app_files": records,
    }
    (stage_root / "payload-manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=True) + "\n",
        encoding="ascii",
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT)
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--require-clean", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        manifest = stage_payload(
            arguments.source,
            arguments.stage,
            arguments.version,
            require_clean=arguments.require_clean,
        )
    except (OSError, StageError, ValueError) as error:
        print("Installer staging failed: " + str(error), file=sys.stderr)
        return 1
    print(
        "Staged {0} generic application files from {1}.".format(
            len(manifest["app_files"]),
            manifest["source_commit"],
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
