# SPDX-License-Identifier: GPL-3.0-only
"""Stage hash-pinned private Windows host tools without executing installers.

All downloads happen at build time. Original upstream source archives and
notices travel with the tools; operators need no global Node/npm/NSSM install.
"""

from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tarfile
import tempfile
import time
from urllib.parse import urlsplit
import urllib.error
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCK = ROOT / "Hosting/Installer/assets.lock.json"
MAX_ASSET_BYTES = 256 * 1024 * 1024
MAX_EXPANDED_BYTES = 512 * 1024 * 1024
MAX_SOURCE_BYTES = 2 * 1024 * 1024 * 1024
MAX_MEMBERS = 100_000
MAX_NOTICE_BYTES = 4 * 1024 * 1024
MANIFEST = "MANIFEST.json"
TOOL_PATHS = {
    "node": "node/node.exe",
    "npm": "node/node_modules/npm/bin/npm-cli.js",
    "nssm": "nssm/nssm.exe",
    "cloudflared": "cloudflared/cloudflared.exe",
}


class AssetError(RuntimeError):
    """The tool inputs or staged payload are incomplete or unsafe."""


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")


def _safe_relative(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or "\\" in name or ":" in name
            or any(part in {"", ".", ".."} or part.endswith((" ", "."))
                   for part in name.rstrip("/").split("/"))
            or any(ord(char) < 32 for char in name)):
        raise AssetError("Unsafe tool archive path")
    return path


def _load_lock(path: Path) -> dict:
    try:
        lock = json.loads(path.read_text(encoding="ascii"))
        if lock["schema"] != 1 or lock["architecture"] != "windows-amd64":
            raise ValueError("unsupported tool architecture")
        for key in ("node", "nssm", "cloudflared"):
            if not lock[key]["version"]:
                raise ValueError("missing version")
        for item in [lock[key] for key in ("node", "nssm", "cloudflared")] + lock["sources"]:
            name = _safe_relative(item["filename"])
            url = urlsplit(item["url"])
            if (len(name.parts) != 1 or url.scheme != "https" or not url.hostname
                    or url.username or url.password or url.fragment
                    or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])
                    or type(item["bytes"]) is not int or not 0 < item["bytes"] <= MAX_ASSET_BYTES):
                raise ValueError("invalid pinned asset")
        return lock
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise AssetError("Invalid host tool lock") from error


def _verify_download(path: Path, item: dict) -> None:
    if (path.is_symlink() or not path.is_file() or path.stat().st_size != item["bytes"]
            or _digest(path) != item["sha256"]):
        raise AssetError("Host tool checksum mismatch: " + item["filename"])


def _fetch_once(item: dict, cache: Path) -> Path:
    target = cache / item["filename"]
    if target.exists() or target.is_symlink():
        _verify_download(target, item)
        return target
    cache.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix="asset-", suffix=".partial", dir=cache)
    temporary = Path(temporary_name)
    try:
        request = urllib.request.Request(item["url"], headers={"User-Agent": "VemalsEars-Host-Build/1"})
        with os.fdopen(descriptor, "wb") as output, urllib.request.urlopen(request, timeout=45) as response:
            if urlsplit(response.geturl()).scheme != "https":
                raise AssetError("Host tool download redirected away from HTTPS")
            count = 0
            while chunk := response.read(1024 * 1024):
                count += len(chunk)
                if count > item["bytes"]:
                    raise AssetError("Host tool download exceeds its pinned size")
                output.write(chunk)
        _verify_download(temporary, item)
        os.replace(temporary, target)
        return target
    finally:
        temporary.unlink(missing_ok=True)


def _fetch(item: dict, cache: Path) -> Path:
    for attempt in range(3):
        try:
            return _fetch_once(item, cache)
        except urllib.error.HTTPError as error:
            error.close()
            if error.code not in {408, 429, 500, 502, 503, 504} or attempt == 2:
                raise AssetError("Download failed for " + item["filename"] + ": HTTP " + str(error.code)) from error
        except (urllib.error.URLError, TimeoutError) as error:
            if attempt == 2:
                raise AssetError("Download failed for " + item["filename"]) from error
        time.sleep(attempt + 1)
    raise AssetError("Host tool download retries exhausted")


def _zip_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members = archive.infolist()
    if len(members) > MAX_MEMBERS or sum(item.file_size for item in members) > MAX_EXPANDED_BYTES:
        raise AssetError("Host tool archive exceeds extraction limits")
    seen = set()
    spellings = {}
    for item in members:
        path = _safe_relative(item.filename)
        name = path.as_posix().casefold()
        if name in seen:
            raise AssetError("Duplicate host tool archive path")
        seen.add(name)
        for index in range(1, len(path.parts) + 1):
            prefix = "/".join(path.parts[:index])
            if spellings.setdefault(prefix.casefold(), prefix) != prefix:
                raise AssetError("Case-colliding host tool archive paths")
        mode = item.external_attr >> 16
        if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) and not (stat.S_ISDIR(mode) or stat.S_ISREG(mode))):
            raise AssetError("Linked host tool archive member")
    return members


def _extract_node(source: Path, target: Path, prefix: str) -> None:
    with zipfile.ZipFile(source) as archive:
        for item in _zip_members(archive):
            path = PurePosixPath(item.filename)
            if path.parts[0] != prefix:
                raise AssetError("Unexpected Node archive root")
            if item.is_dir():
                continue
            if len(path.parts) < 2:
                raise AssetError("Unexpected Node archive member")
            destination = target.joinpath(*path.parts[1:])
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as incoming, destination.open("wb") as output:
                shutil.copyfileobj(incoming, output)


def _archive_notices(source: Path, item: dict, destination: Path) -> None:
    names = item.get("notice_members", [])
    if not names:
        raise AssetError("Source archive has no declared notices: " + source.name)
    wanted = set(names) | ({item["source_member"]} if item.get("source_member") else set())
    payloads = {}
    if source.suffix == ".zip":
        with zipfile.ZipFile(source) as archive:
            members = _zip_members(archive)
            if "go_sum" in item:
                digest = hashlib.sha256()
                for member in sorted(members, key=lambda entry: entry.filename):
                    data = archive.read(member)
                    digest.update((hashlib.sha256(data).hexdigest() + "  " + member.filename + "\n").encode("utf-8"))
                if "h1:" + base64.b64encode(digest.digest()).decode("ascii") != item["go_sum"]:
                    raise AssetError("Cloudflared module does not match embedded build information")
            for name in wanted:
                try:
                    member = archive.getinfo(name)
                except KeyError as error:
                    raise AssetError("Source archive is missing declared notices or source") from error
                if member.is_dir() or member.file_size > MAX_NOTICE_BYTES:
                    raise AssetError("Invalid source notice member")
                payloads[name] = archive.read(member)
    else:
        total = 0
        with tarfile.open(source, "r|*") as archive:
            for count, member in enumerate(archive, start=1):
                total += member.size
                if count > MAX_MEMBERS or total > MAX_SOURCE_BYTES:
                    raise AssetError("Source archive exceeds inspection limits")
                if member.name not in wanted:
                    continue
                if member.name in payloads or not member.isfile() or member.size > MAX_NOTICE_BYTES:
                    raise AssetError("Invalid source notice member")
                handle = archive.extractfile(member)
                if handle is None:
                    raise AssetError("Missing source notice member")
                payloads[member.name] = handle.read()
    if wanted - payloads.keys():
        raise AssetError("Source archive is missing declared notices or source")
    for index, name in enumerate(names):
        destination.mkdir(parents=True, exist_ok=True)
        # Preserve original notices byte-for-byte, including upstream encoding.
        (destination / (str(index) + "-" + PurePosixPath(name).name)).write_bytes(payloads[name])


def _paths(destination: Path) -> dict[str, Path]:
    return {key: destination / "tools" / relative for key, relative in TOOL_PATHS.items()}


def _tree_digest(root: Path) -> str:
    records = {path.relative_to(root).as_posix(): _digest(path)
               for path in root.rglob("*") if path.is_file()}
    body = "\n".join(name + " " + digest for name, digest in sorted(records.items())) + "\n"
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def validate_tools(destination: Path, *, lock_path: Path = DEFAULT_LOCK) -> dict[str, Path]:
    """Verify every staged file, source archive and tool entry point."""
    root = Path(destination) / "tools"
    lock = _load_lock(Path(lock_path))
    try:
        if root.is_symlink():
            raise AssetError("Staged tools directory is a link")
        manifest = json.loads((root / MANIFEST).read_text(encoding="ascii"))
        if manifest["lock_sha256"] != hashlib.sha256(_json_bytes(lock)).hexdigest():
            raise AssetError("Staged tools use a different asset lock")
        expected = manifest["files"]
        actual = {}
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise AssetError("Staged tools contain a link")
            if path.is_file() and path != root / MANIFEST:
                actual[path.relative_to(root).as_posix()] = {"sha256": _digest(path), "bytes": path.stat().st_size}
        if expected != actual:
            raise AssetError("Staged host tools changed or are incomplete")
        for path in _paths(Path(destination)).values():
            if not path.is_file():
                raise AssetError("A required host tool is missing")
        if (_tree_digest(root / "node") != lock["node"]["payload_tree_sha256"]
                or _digest(root / "nssm/nssm.exe") != lock["nssm"]["executable_sha256"]
                or _digest(root / "cloudflared/cloudflared.exe") != lock["cloudflared"]["sha256"]):
            raise AssetError("Staged executable differs from the pinned tool")
        for source in lock["sources"]:
            _verify_download(root / "Source" / source["filename"], source)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise AssetError("Invalid staged host tools") from error
    return _paths(Path(destination))


def stage_tools(destination: Path, cache: Path, *, lock_path: Path = DEFAULT_LOCK) -> dict[str, Path]:
    """Stage tools under destination/tools; return node/npm/nssm/cloudflared paths.

    Existing complete stages are verified and reused. Partial or altered stages
    fail without overwriting them. Neither credentials nor installed tools are
    inspected, and no upstream executable is run by this function.
    """
    destination, cache = Path(destination), Path(cache)
    lock = _load_lock(Path(lock_path))
    if (destination / "tools").exists():
        return validate_tools(destination, lock_path=lock_path)
    destination.mkdir(parents=True, exist_ok=True)
    records = [lock[key] for key in ("node", "nssm", "cloudflared")] + lock["sources"]
    by_name = {}
    for record in records:
        previous = by_name.setdefault(record["filename"], record)
        if (previous["sha256"], previous["url"]) != (record["sha256"], record["url"]):
            raise AssetError("Asset cache names collide")
    with ThreadPoolExecutor(max_workers=4) as pool:
        downloaded = dict(zip(by_name, pool.map(lambda item: _fetch(item, cache), by_name.values())))
    with tempfile.TemporaryDirectory(prefix=".host-tools-", dir=destination) as temporary:
        stage = Path(temporary) / "tools"
        stage.mkdir()
        _extract_node(downloaded[lock["node"]["filename"]], stage / "node", lock["node"]["archive_root"])
        with zipfile.ZipFile(downloaded[lock["nssm"]["filename"]]) as archive:
            _zip_members(archive)
            (stage / "nssm").mkdir()
            (stage / "nssm/nssm.exe").write_bytes(archive.read(lock["nssm"]["archive_root"] + "/win64/nssm.exe"))
        (stage / "cloudflared").mkdir()
        shutil.copyfile(downloaded[lock["cloudflared"]["filename"]], stage / "cloudflared/cloudflared.exe")
        (stage / "Source").mkdir()
        for source in lock["sources"]:
            archive = downloaded[source["filename"]]
            _archive_notices(archive, source, stage / "notices" / source["filename"])
            shutil.copyfile(archive, stage / "Source" / source["filename"])
        (stage / "Source/assets.lock.json").write_bytes(_json_bytes(lock))
        (stage / "THIRD-PARTY-NOTICES.txt").write_text(
            "Vemal's Ears Host tools\n======================\n\n"
            "Node.js " + lock["node"]["version"] + " and npm " + lock["node"]["npm_version"] + ": MIT and bundled third-party terms.\n"
            "See node/LICENSE and original npm notices under node/node_modules.\n"
            "NSSM " + lock["nssm"]["version"] + ": public domain; its original ZIP includes the C++ source.\n"
            "cloudflared " + lock["cloudflared"]["version"] + ": Apache-2.0 and dependency terms.\n"
            "Original license/copyright notices are in notices/. Source/ contains\n"
            "the matching Node, NSSM, cloudflared and Go sources, plus every Go\n"
            "module identified by the distributed Windows cloudflared binary.\n"
            "Source/assets.lock.json records official URLs, sizes, SHA-256 hashes,\n"
            "module h1 sums and the original binary's source/build information.\n"
            "These are separate, unmodified tools. No operator secrets or state\n"
            "are included. Original upstream text encoding is preserved.\n",
            encoding="ascii",
        )
        for relative in (*TOOL_PATHS.values(), "node/LICENSE"):
            if not (stage / relative).is_file():
                raise AssetError("Tool archive lacks required file: " + relative)
        manifest = {
            "schema": 1,
            "lock_sha256": hashlib.sha256(_json_bytes(lock)).hexdigest(),
            "files": {path.relative_to(stage).as_posix(): {"sha256": _digest(path), "bytes": path.stat().st_size}
                      for path in sorted(stage.rglob("*")) if path.is_file()},
        }
        (stage / MANIFEST).write_bytes(_json_bytes(manifest))
        os.replace(stage, destination / "tools")
    return validate_tools(destination, lock_path=lock_path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path, default=ROOT / "Installer/cache/host-assets")
    arguments = parser.parse_args()
    stage_tools(arguments.output, arguments.cache)
    print("Verified Windows host tools and matching sources staged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
