# SPDX-License-Identifier: GPL-3.0-only
"""Preserve verified upstream source inputs beside the bundled runtime.

Archives stay intact: this helper never executes or extracts upstream code.
Locks are editable build inputs, including for modified redistributions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_LOCK = ROOT / "Installer/runtime-sources.lock.json"
DEFAULT_RUNTIME_LOCK = ROOT / "Installer/runtime-lock.json"
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
# A day inside ZIP's range even after UTC is converted to local time.
SOURCE_MTIME = 315619200
MANIFEST_NAME = "MANIFEST.json"
README_NAME = "README.txt"


def normalized_digest(path: Path) -> str:
    """Hash exact text with checkout line endings normalized to LF."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")


def load_source_lock(source_lock: Path, runtime_lock: Path) -> dict:
    try:
        lock = json.loads(source_lock.read_text(encoding="utf-8"))
        runtime = json.loads(runtime_lock.read_text(encoding="utf-8"))
        runtime_hash = normalized_digest(runtime_lock)
    except (OSError, ValueError) as error:
        raise SystemExit("cannot read runtime source locks: " + str(error)) from error
    if lock.get("schema") != 1 or lock.get("runtime_lock_hash_mode") != "sha256-lf":
        raise SystemExit("unsupported runtime source lock schema or hash mode")
    if lock.get("runtime_lock_sha256") != runtime_hash:
        raise SystemExit("runtime-lock.json changed; refresh matching runtime sources before building")
    components = {"cpython": runtime.get("python", {}).get("version")}
    components.update({item["name"].lower(): item["version"] for item in runtime.get("wheels", [])})
    if lock.get("runtime_components") != components:
        raise SystemExit("runtime source component version mismatch")
    artifacts = lock.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise SystemExit("runtime source inventory is empty")
    names = set()
    covered = set()
    for item in artifacts:
        if not isinstance(item, dict):
            raise SystemExit("invalid runtime source artifact")
        filename = item.get("filename", "")
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", filename)
                or filename in names or filename in {MANIFEST_NAME, README_NAME}):
            raise SystemExit("unsafe or duplicate source filename: " + filename)
        names.add(filename)
        if not re.fullmatch(r"[a-f0-9]{64}", item.get("sha256", "")):
            raise SystemExit("missing source SHA256: " + filename)
        if not item.get("url", "").startswith("https://") or not item.get("provenance"):
            raise SystemExit("source artifact needs HTTPS URL and provenance: " + filename)
        if not item.get("name") or not item.get("version"):
            raise SystemExit("source artifact needs component and version: " + filename)
        if item.get("runtime_component"):
            component = item["runtime_component"]
            if components.get(component) != item["version"]:
                raise SystemExit("source artifact version mismatch: " + filename)
            covered.add(component)
    if covered != set(components):
        raise SystemExit("missing runtime component source: " + ", ".join(sorted(set(components) - covered)))
    if not isinstance(lock.get("build_notes"), list) or not lock["build_notes"]:
        raise SystemExit("runtime source build notes are missing")
    return lock


def expected_manifest(lock: dict, source_lock: Path) -> dict:
    return {
        "schema": 1,
        "runtime_lock_sha256": lock["runtime_lock_sha256"],
        "runtime_lock_hash_mode": lock["runtime_lock_hash_mode"],
        "source_lock_sha256": normalized_digest(source_lock),
        "runtime_components": lock["runtime_components"],
        "artifacts": sorted(lock["artifacts"], key=lambda item: item["filename"]),
        "build_notes": lock["build_notes"],
        "coverage_notes": lock.get("coverage_notes", []),
    }


def readme_bytes(lock: dict) -> bytes:
    lines = [
        "Matching upstream runtime source inputs", "=======================================", "",
        "These are intact upstream archives and their original license notices.",
        "MANIFEST.json records versions, provenance and SHA256 checksums.",
        "The source and runtime locks are editable inputs for modified builds.",
        "Refresh their versions and checksums together when changing dependencies.",
        "No Git checkout or repository account is needed to use these archives.",
        "", "BUILDING AND MODIFYING", "",
        *lock["build_notes"], "", "COVERAGE AND LIMITS", "",
        *lock.get("coverage_notes", []), "",
    ]
    return ("\n".join(lines)).encode("ascii")


def verified_download(item: dict, cache: Path, *, offline: bool = False) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / item["filename"]
    if target.is_symlink():
        raise SystemExit("source cache path must not be a symlink: " + str(target))
    if not target.is_file():
        if offline:
            raise SystemExit("source archive is missing from offline cache: " + target.name)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=cache, prefix="source-", suffix=".part", delete=False) as output:
                temporary = Path(output.name)
                with urllib.request.urlopen(item["url"], timeout=60) as response:
                    size = 0
                    while block := response.read(1024 * 1024):
                        size += len(block)
                        if size > MAX_DOWNLOAD_BYTES:
                            raise SystemExit("source download exceeds limit: " + target.name)
                        output.write(block)
            if file_digest(temporary) != item["sha256"]:
                raise SystemExit("source SHA256 mismatch: " + target.name)
            temporary.replace(target)
        except OSError as error:
            raise SystemExit("source download failed for " + target.name + ": " + str(error)) from error
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
    if file_digest(target) != item["sha256"]:
        raise SystemExit("source SHA256 mismatch: " + target.name)
    return target


def source_directory(runtime: Path) -> Path:
    target = runtime / "Source"
    if target.is_symlink():
        raise SystemExit("runtime Source directory must not be a symlink")
    return target


def validate_runtime_sources(
    runtime: Path, *, source_lock: Path = DEFAULT_SOURCE_LOCK,
    runtime_lock: Path = DEFAULT_RUNTIME_LOCK,
) -> dict:
    lock = load_source_lock(source_lock, runtime_lock)
    source = source_directory(runtime)
    manifest = expected_manifest(lock, source_lock)
    expected = {item["filename"] for item in lock["artifacts"]} | {MANIFEST_NAME, README_NAME}
    actual = {path.name for path in source.iterdir()} if source.is_dir() else set()
    if actual != expected:
        raise SystemExit("runtime source inventory mismatch; missing=" + ", ".join(sorted(expected - actual))
                         + "; unexpected=" + ", ".join(sorted(actual - expected)))
    for item in lock["artifacts"]:
        path = source / item["filename"]
        if path.is_symlink() or not path.is_file() or file_digest(path) != item["sha256"]:
            raise SystemExit("runtime source SHA256 mismatch: " + item["filename"])
    for name, wanted in ((MANIFEST_NAME, json_bytes(manifest)), (README_NAME, readme_bytes(lock))):
        path = source / name
        if path.is_symlink() or path.read_bytes() != wanted:
            raise SystemExit("runtime source manifest or build notes differ: " + name)
    return manifest


def stage_runtime_sources(
    runtime: Path, cache: Path, *, source_lock: Path = DEFAULT_SOURCE_LOCK,
    runtime_lock: Path = DEFAULT_RUNTIME_LOCK, offline: bool = False,
) -> dict:
    lock = load_source_lock(source_lock, runtime_lock)
    source = source_directory(runtime)
    expected = {item["filename"] for item in lock["artifacts"]} | {MANIFEST_NAME, README_NAME}
    if source.exists():
        unexpected = {path.name for path in source.iterdir()} - expected
        if unexpected:
            raise SystemExit("runtime Source contains untracked files: " + ", ".join(sorted(unexpected)))
        if any(path.is_symlink() for path in source.iterdir()):
            raise SystemExit("runtime Source entries must not be symlinks")
    # Verify all downloads before declaring this runtime's source complete.
    archives = [(item, verified_download(item, cache, offline=offline)) for item in lock["artifacts"]]
    source.mkdir(parents=True, exist_ok=True)
    for item, archive in archives:
        target = source / item["filename"]
        if archive.resolve() != target.resolve():
            shutil.copyfile(archive, target)
        os.utime(target, (SOURCE_MTIME, SOURCE_MTIME))
    (source / MANIFEST_NAME).write_bytes(json_bytes(expected_manifest(lock, source_lock)))
    (source / README_NAME).write_bytes(readme_bytes(lock))
    for name in (MANIFEST_NAME, README_NAME):
        os.utime(source / name, (SOURCE_MTIME, SOURCE_MTIME))
    return validate_runtime_sources(runtime, source_lock=source_lock, runtime_lock=runtime_lock)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--cache", type=Path, default=ROOT / "dist/cache/runtime-sources")
    parser.add_argument("--lock", type=Path, default=DEFAULT_SOURCE_LOCK)
    parser.add_argument("--runtime-lock", type=Path, default=DEFAULT_RUNTIME_LOCK)
    parser.add_argument("--check", action="store_true", help="validate staged files without downloads")
    parser.add_argument("--offline", action="store_true", help="use verified cached archives only")
    args = parser.parse_args(argv)
    options = {"source_lock": args.lock, "runtime_lock": args.runtime_lock}
    if args.check:
        result = validate_runtime_sources(args.runtime, **options)
    else:
        result = stage_runtime_sources(args.runtime, args.cache, offline=args.offline, **options)
    print("runtime source inputs verified: " + str(len(result["artifacts"])) + " artifacts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
