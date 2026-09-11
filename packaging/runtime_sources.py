# SPDX-License-Identifier: GPL-3.0-only
"""Preserve verified upstream source inputs beside the bundled runtime.

The helper never executes upstream code. Release assets stay intact; Gitiles
snapshots are safely repacked to remove request-time metadata. Locks are
editable build inputs, including for modified redistributions.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
import tempfile
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_LOCK = ROOT / "Installer/runtime-sources.lock.json"
DEFAULT_RUNTIME_LOCK = ROOT / "Installer/runtime-lock.json"
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 100_000
MAX_EXPANDED_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_STREAM_BYTES = 512 * 1024 * 1024
# A day inside ZIP's range even after UTC is converted to local time.
SOURCE_MTIME = 315619200
MANIFEST_NAME = "MANIFEST.json"
README_NAME = "README.txt"
GITILES_CANONICALIZATION = "gitiles-ustar-v1"


class LimitedReader:
    def __init__(self, stream, limit: int):
        self.stream = stream
        self.limit = limit
        self.total = 0

    def read(self, size: int = -1) -> bytes:
        remaining = self.limit - self.total
        if size < 0 or size > remaining + 1:
            size = remaining + 1
        data = self.stream.read(size)
        self.total += len(data)
        if self.total > self.limit:
            raise ValueError("archive expands beyond the stream limit")
        return data


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


def is_gitiles_archive_url(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    return (parsed.scheme == "https"
            and (parsed.hostname or "").endswith(".googlesource.com")
            and "/+archive/" in parsed.path)


def gitiles_revision(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    matched = re.search(r"/\+archive/([a-f0-9]{40})\.tar\.gz$", parsed.path)
    if matched is None or parsed.query or parsed.fragment:
        return ""
    return matched.group(1)


def _git_object_id(kind: bytes, payload: bytes) -> bytes:
    header = kind + b" " + str(len(payload)).encode("ascii") + b"\0"
    return hashlib.sha1(header + payload).digest()


def _git_tree_id(files: dict[str, tuple[str, bytes]]) -> str:
    root: dict[str, object] = {}
    for path, value in files.items():
        node = root
        parts = path.split("/")
        for part in parts[:-1]:
            child = node.setdefault(part, {})
            if not isinstance(child, dict):
                raise ValueError("archive path collides with a file: " + path)
            node = child
        if parts[-1] in node:
            raise ValueError("duplicate archive path: " + path)
        node[parts[-1]] = value

    def hash_tree(node: dict[str, object]) -> bytes:
        rows = []
        for name, value in node.items():
            encoded = name.encode("ascii")
            if isinstance(value, dict):
                mode = b"40000"
                object_id = hash_tree(value)
                order = encoded + b"/"
            else:
                mode_text, payload = value
                mode = mode_text.encode("ascii")
                object_id = _git_object_id(b"blob", payload)
                order = encoded
            rows.append((order, mode + b" " + encoded + b"\0" + object_id))
        payload = b"".join(row for _, row in sorted(rows))
        return _git_object_id(b"tree", payload)

    return hash_tree(root).hex()


def canonicalize_gitiles_archive(source: Path, target: Path) -> str:
    """Repack a Gitiles snapshot without its request-time tar metadata."""
    files: dict[str, tuple[str, bytes]] = {}
    explicit_directories = set()
    case_names: dict[str, str] = {}
    seen_names = set()
    total = 0
    try:
        with gzip.open(source, "rb") as expanded, tarfile.open(
            fileobj=LimitedReader(expanded, MAX_ARCHIVE_STREAM_BYTES), mode="r|",
        ) as incoming:
            count = 0
            for member in incoming:
                count += 1
                if count > MAX_ARCHIVE_MEMBERS:
                    raise ValueError("archive has too many members")
                if set(member.pax_headers) - {"mtime"}:
                    raise ValueError("archive has unsupported PAX metadata")
                if getattr(member, "sparse", None):
                    raise ValueError("archive has a sparse member")
                name = member.name.rstrip("/")
                path = PurePosixPath(name)
                if (not name or "\\" in name or path.is_absolute()
                        or any(part in {"", ".", ".."} for part in path.parts)):
                    raise ValueError("archive has an unsafe path")
                name = "/".join(path.parts)
                name.encode("ascii")
                for index in range(1, len(path.parts) + 1):
                    prefix = "/".join(path.parts[:index])
                    folded = prefix.casefold()
                    previous = case_names.setdefault(folded, prefix)
                    if previous != prefix:
                        raise ValueError("archive has case-colliding paths")
                if name in seen_names:
                    raise ValueError("archive has a duplicate path")
                seen_names.add(name)
                if member.isdir():
                    if member.size != 0:
                        raise ValueError("archive directory has a payload")
                    explicit_directories.add(name)
                    continue
                if member.isfile():
                    if member.size > MAX_EXPANDED_BYTES - total:
                        raise ValueError("archive expands beyond the size limit")
                    stream = incoming.extractfile(member)
                    if stream is None:
                        raise ValueError("archive file payload is missing")
                    payload = stream.read()
                    if len(payload) != member.size:
                        raise ValueError("archive file payload is truncated")
                    total += len(payload)
                    mode = "100755" if member.mode & 0o111 else "100644"
                else:
                    raise ValueError("archive has an unsupported member type")
                files[name] = (mode, payload)

        directories = {
            "/".join(parts[:index])
            for name in files
            for parts in (name.split("/"),)
            for index in range(1, len(parts))
        }
        if explicit_directories - directories:
            raise ValueError("archive contains an empty or untracked directory")
        tree = _git_tree_id(files)

        with tarfile.open(target, "w", format=tarfile.USTAR_FORMAT) as output:
            for name in sorted(directories | set(files)):
                info = tarfile.TarInfo(name)
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                info.mtime = SOURCE_MTIME
                if name in directories:
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    output.addfile(info)
                    continue
                mode, payload = files[name]
                info.mode = 0o755 if mode == "100755" else 0o644
                info.size = len(payload)
                output.addfile(info, io.BytesIO(payload))
    except (EOFError, OSError, tarfile.TarError, UnicodeError, ValueError) as error:
        target.unlink(missing_ok=True)
        raise SystemExit("invalid Gitiles source archive: " + source.name + ": " + str(error)) from error
    return tree


def validate_declared_license_files(item: dict, source: Path) -> None:
    """Require audit pointers in the lock to exist in the verified archive."""
    required = item.get("license_files", [])
    if not required:
        return
    try:
        filename = item.get("filename", "")
        if filename.endswith(".tar.gz"):
            expanded = gzip.open(source, "rb")
        elif filename.endswith(".tar"):
            expanded = source.open("rb")
        else:
            raise ValueError("declared license files require a tar archive")
        names = set()
        with expanded, tarfile.open(
            fileobj=LimitedReader(expanded, MAX_ARCHIVE_STREAM_BYTES), mode="r|",
        ) as archive:
            for count, member in enumerate(archive, start=1):
                if count > MAX_ARCHIVE_MEMBERS:
                    raise ValueError("archive has too many members")
                if member.isfile():
                    names.add(member.name.rstrip("/"))
    except (EOFError, OSError, tarfile.TarError, ValueError) as error:
        raise SystemExit(
            "cannot inspect declared source notices in " + source.name + ": " + str(error)
        ) from error
    missing = set(required) - names
    if missing:
        raise SystemExit(
            "source archive is missing declared license files: "
            + source.name + ": " + ", ".join(sorted(missing))
        )


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
        license_files = item.get("license_files", [])
        if (not isinstance(license_files, list)
                or any(not isinstance(path, str) for path in license_files)
                or len(license_files) != len(set(license_files))):
            raise SystemExit("invalid declared source license files: " + filename)
        for name in license_files:
            path = PurePosixPath(name)
            try:
                name.encode("ascii")
            except UnicodeError as error:
                raise SystemExit("unsafe declared source license file: " + filename) from error
            if (not name or "\\" in name or path.is_absolute()
                    or any(part in {"", ".", ".."} for part in path.parts)):
                raise SystemExit("unsafe declared source license file: " + filename)
        canonicalization = item.get("canonicalization")
        if is_gitiles_archive_url(item["url"]):
            if canonicalization != GITILES_CANONICALIZATION:
                raise SystemExit(
                    "Gitiles source archives require deterministic canonicalization: " + filename)
            if not re.fullmatch(r"[a-f0-9]{40}", gitiles_revision(item["url"])):
                raise SystemExit("Gitiles source archive must pin a full commit: " + filename)
            if not filename.endswith(".tar"):
                raise SystemExit("canonical Gitiles source filename must end in .tar: " + filename)
            if not re.fullmatch(r"[a-f0-9]{40}", item.get("git_tree_sha1", "")):
                raise SystemExit("canonical Gitiles source needs its Git tree SHA1: " + filename)
        elif canonicalization is not None:
            raise SystemExit("unsupported source canonicalization: " + filename)
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
        "These contain upstream source bytes and their original license notices.",
        "Most are intact upstream archives. Generated Gitiles snapshots are repacked",
        "as deterministic tar files without changing paths, modes or file contents.",
        "MANIFEST.json records versions, provenance, Git trees and SHA256 checksums.",
        "The source and runtime locks are editable inputs for modified builds.",
        "Refresh their versions and checksums together when changing dependencies.",
        "No Git checkout or repository account is needed to use these archives.",
        "A slim installed runtime keeps only this guide and MANIFEST.json as its",
        "source receipt. Get its exact source-complete member ZIP from the URL in",
        "app/docs/SOURCE.txt; the full member ZIP keeps every archive listed here.",
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
        canonical = None
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
            candidate = temporary
            if item.get("canonicalization") == GITILES_CANONICALIZATION:
                with tempfile.NamedTemporaryFile(
                    dir=cache, prefix="source-canonical-", suffix=".part", delete=False,
                ) as output:
                    canonical = Path(output.name)
                tree = canonicalize_gitiles_archive(temporary, canonical)
                if tree != item["git_tree_sha1"]:
                    raise SystemExit("source Git tree mismatch: " + target.name)
                candidate = canonical
            if file_digest(candidate) != item["sha256"]:
                raise SystemExit("source SHA256 mismatch: " + target.name)
            validate_declared_license_files(item, candidate)
            candidate.replace(target)
        except OSError as error:
            raise SystemExit("source download failed for " + target.name + ": " + str(error)) from error
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
            if canonical is not None and canonical.exists():
                canonical.unlink()
    if file_digest(target) != item["sha256"]:
        raise SystemExit("source SHA256 mismatch: " + target.name)
    validate_declared_license_files(item, target)
    return target


def source_directory(runtime: Path) -> Path:
    target = runtime / "Source"
    if target.is_symlink():
        raise SystemExit("runtime Source directory must not be a symlink")
    return target


def validate_runtime_sources(
    runtime: Path, *, source_lock: Path = DEFAULT_SOURCE_LOCK,
    runtime_lock: Path = DEFAULT_RUNTIME_LOCK, allow_receipt_only: bool = False,
) -> dict:
    lock = load_source_lock(source_lock, runtime_lock)
    source = source_directory(runtime)
    manifest = expected_manifest(lock, source_lock)
    expected = {item["filename"] for item in lock["artifacts"]} | {MANIFEST_NAME, README_NAME}
    actual = {path.name for path in source.iterdir()} if source.is_dir() else set()
    receipts = {MANIFEST_NAME, README_NAME}
    if allow_receipt_only and actual == receipts:
        for name, wanted in ((MANIFEST_NAME, json_bytes(manifest)), (README_NAME, readme_bytes(lock))):
            path = source / name
            if path.is_symlink() or not path.is_file() or path.read_bytes() != wanted:
                raise SystemExit("runtime source receipt differs: " + name)
        return manifest
    if actual != expected:
        raise SystemExit("runtime source inventory mismatch; missing=" + ", ".join(sorted(expected - actual))
                         + "; unexpected=" + ", ".join(sorted(actual - expected)))
    for item in lock["artifacts"]:
        path = source / item["filename"]
        if path.is_symlink() or not path.is_file() or file_digest(path) != item["sha256"]:
            raise SystemExit("runtime source SHA256 mismatch: " + item["filename"])
        validate_declared_license_files(item, path)
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
