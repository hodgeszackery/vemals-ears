"""Stage checked upstream source and license bytes for the native worker.

The npm wrapper's MIT label does not describe every Rust dependency linked
into davey. Preserve its entire Cargo.lock source superset, including target
and optional packages, instead of guessing which optimized code survives.
This helper downloads public upstream artifacts only; it never runs them.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import tempfile
import tomllib
import urllib.request
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RECORD = "WORKER-SOURCES.json"
REQUIRED_LICENSES = {
    "davey-MIT.txt", "sapphire-async-queue-MIT.txt",
    "sapphire-snowflake-MIT.txt", "MPL-2.0.txt",
}
REQUIRED_PYTHON_PACKAGES = {"websockets==13.1"}
MAX_DOWNLOAD = 128 * 1024 * 1024


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def npm_lock_digest(data: bytes) -> str:
    """Match Git's LF source lock on both Windows and Linux checkouts."""
    return digest(data.replace(b"\r\n", b"\n"))


def download(url: str) -> bytes:
    if not url.startswith("https://"):
        raise ValueError("worker source downloads require HTTPS")
    with urllib.request.urlopen(url, timeout=60) as response:
        data = response.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD:
        raise ValueError("upstream source archive exceeds the size limit")
    return data


def checked_bytes(asset: dict, downloader) -> bytes:
    data = downloader(asset["url"])
    if not data or digest(data) != asset["sha256"]:
        raise ValueError("upstream source/license checksum mismatch: " + asset["name"])
    return data


def archive_member(archive: tarfile.TarFile, name: str) -> bytes:
    member = archive.getmember(name)
    if not member.isfile() or member.size > MAX_DOWNLOAD:
        raise ValueError("invalid upstream source member: " + name)
    return archive.extractfile(member).read()


def safe_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.+-]+", name):
        raise ValueError("invalid source artifact name")
    return name


def inspect_crate(data: bytes, name: str, version: str) -> dict:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        prefix = name + "-" + version + "/"
        metadata = tomllib.loads(archive_member(archive, prefix + "Cargo.toml").decode("utf-8"))
        package = metadata["package"]
        if package["name"] != name or package["version"] != version:
            raise ValueError("crate source identity differs from Cargo.lock")
        license_name = package.get("license", "")
        license_file = package.get("license-file")
        if license_file:
            if not archive_member(archive, prefix + license_file).strip():
                raise ValueError("crate license-file is empty")
        if not license_name and not license_file:
            raise ValueError("crate has no declared license: " + name)
        # A complete MPL license contains the text of Exhibit B as an example;
        # that is not an applied source-file designation. Check other files.
        if "MPL-2.0" in license_name:
            for member in archive.getmembers():
                basename = Path(member.name).name.lower()
                if not member.isfile() or basename.startswith(("license", "copying")):
                    continue
                content = archive_member(archive, member.name).decode("utf-8", errors="replace")
                if re.search(r"this source code form is\s+[\"\u201c]?incompatible with secondary licenses", content, re.I):
                    raise ValueError("MPL secondary-license designation requires review: " + name)
        return {"declared_license": license_name or None, "license_file": license_file}


def stage_sources(manifest: dict, npm_lock: bytes, output: Path, downloader=download) -> dict:
    if (manifest.get("schema") != 1 or manifest.get("npm_lock_hash_mode") != "sha256-lf"
            or npm_lock_digest(npm_lock) != manifest["npm_lock_sha256"]):
        raise ValueError("worker source inventory does not match package-lock.json")
    if {item["name"] for item in manifest["licenses"]} != REQUIRED_LICENSES:
        raise ValueError("required worker license artifact is missing from inventory")
    if {item["package"] for item in manifest["python_sources"]} != REQUIRED_PYTHON_PACKAGES:
        raise ValueError("required worker Python source is missing from inventory")
    npm_inventory = {}
    for location, package in json.loads(npm_lock)["packages"].items():
        if location:
            identity = location.split("node_modules/")[-1] + "@" + package["version"]
            npm_inventory[identity] = package["integrity"]
    covered = list(manifest["davey"]["packages"])
    for asset in manifest["npm_sources"]:
        covered.extend(asset["packages"])
    if len(covered) != len(set(covered)) or set(covered) != set(npm_inventory):
        raise ValueError("preferred npm source inventory does not cover every locked package exactly once")
    if output.exists():
        raise ValueError("worker source output already exists; choose a new output directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    records = []
    with tempfile.TemporaryDirectory(prefix=".worker-sources-", dir=output.parent) as folder:
        staging = Path(folder) / "payload"
        staging.mkdir()

        def preserve(asset: dict, directory: str) -> bytes:
            data = checked_bytes(asset, downloader)
            relative = directory + "/" + safe_name(asset["name"])
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            records.append({"path": relative, "url": asset["url"], "sha256": digest(data)})
            return data

        for asset in sorted(manifest["licenses"], key=lambda item: item["name"]):
            preserve(asset, "licenses")
        python_receipts = []
        for asset in sorted(manifest["python_sources"], key=lambda item: item["name"]):
            python_source = preserve(asset, "sources/python")
            with tarfile.open(fileobj=io.BytesIO(python_source), mode="r:gz") as archive:
                if not archive_member(archive, asset["source_member"]).strip():
                    raise ValueError("worker Python source is empty")
            python_receipts.append({"package": asset["package"], "metadata_url": asset["metadata_url"],
                                    "source_path": "sources/python/" + asset["name"]})
        davey = manifest["davey"]
        source = preserve(davey, "sources")
        npm_receipts = []
        for identity in sorted(davey["packages"]):
            package_name, version = identity.rsplit("@", 1)
            npm_receipts.append({"package": identity, "npm_integrity": npm_inventory[identity],
                                 "source_path": "sources/" + davey["name"],
                                 "basis": "npm gitHead; native Cargo sources included separately",
                                 "metadata_url": "https://registry.npmjs.org/" + package_name + "/" + version})
        for asset in sorted(manifest["npm_sources"], key=lambda item: item["name"]):
            npm_source = preserve(asset, "sources/npm")
            with tarfile.open(fileobj=io.BytesIO(npm_source), mode="r:gz") as archive:
                if not archive_member(archive, asset["source_member"]).strip():
                    raise ValueError("preferred npm source is empty: " + asset["name"])
                for member in asset.get("embedded_source_maps", ()):
                    source_map = json.loads(archive_member(archive, member))
                    contents = source_map.get("sourcesContent", [])
                    if (not contents or len(contents) != len(source_map["sources"])
                            or any(not isinstance(content, str) for content in contents)):
                        raise ValueError("npm publication is missing original source-map content")
            receipt_packages = {receipt["package"] for receipt in asset["receipts"]}
            if not set(asset["packages"]).issubset(receipt_packages):
                raise ValueError("npm source archive is missing its upstream receipt")
            for receipt in asset["receipts"]:
                identity = receipt["package"]
                if identity not in npm_inventory:
                    raise ValueError("source receipt names an unlocked npm package")
                if receipt.get("npm_integrity", npm_inventory[identity]) != npm_inventory[identity]:
                    raise ValueError("npm source receipt differs from locked package integrity")
                npm_receipts.append({**receipt, "npm_integrity": npm_inventory[identity],
                                     "source_path": "sources/npm/" + asset["name"],
                                     "embedded_source_maps": asset.get("embedded_source_maps", [])})
        with tarfile.open(fileobj=io.BytesIO(source), mode="r:gz") as archive:
            cargo_bytes = archive_member(archive, davey["cargo_lock_path"])
        if digest(cargo_bytes) != davey["cargo_lock_sha256"]:
            raise ValueError("davey Cargo.lock differs from audited source")
        cargo = tomllib.loads(cargo_bytes.decode("utf-8"))
        crates = []
        for package in sorted(cargo["package"], key=lambda item: (item["name"], item["version"])):
            if "source" not in package:
                continue  # Workspace source is in the complete davey archive.
            if package["source"] != "registry+https://github.com/rust-lang/crates.io-index":
                raise ValueError("unreviewed Cargo source: " + package["source"])
            name, version = safe_name(package["name"]), safe_name(package["version"])
            filename = name + "-" + version + ".crate"
            asset = {"name": filename, "sha256": package["checksum"],
                     "url": "https://static.crates.io/crates/" + name + "/" + filename}
            data = preserve(asset, "sources/crates")
            details = inspect_crate(data, name, version)
            crates.append({"name": name, "version": version, **details})
        (staging / "Cargo.lock").write_bytes(cargo_bytes)
        (staging / "package-lock.json").write_bytes(npm_lock)
        notice = (
            "Worker third-party source availability\n\n"
            "The sources/ directory contains the exact davey source archive and all registry\n"
            "crate archives in its Cargo.lock, including optional and target-specific code.\n"
            "sources/npm retains upstream preferred source for every other locked npm package,\n"
            "including TypeScript source/build files and opusscript's native Opus submodule.\n"
            "Exact npm publication archives are also retained: their JSON .map files preserve\n"
            "original TypeScript in sourcesContent, indexed by the matching sources array.\n"
            "This retains publication-time generated sources alongside upstream build trees.\n"
            "sources/python contains the pinned websockets source, including C speedups.\n"
            "The archives retain upstream source and notices without modification. Extract\n"
            "the .crate files as gzip-compressed tar archives to read or modify their source.\n"
            "Third-party files retain their own licenses; they are not relabeled as GPL or MIT.\n"
            "MPL-2.0 covered source, including the hpke-rs packages, is provided in these\n"
            "archives under MPL-2.0; its complete terms are in licenses/MPL-2.0.txt.\n"
            "WORKER-SOURCES.json records upstream URLs, hashes, and declared crate licenses.\n"
            "Keep this directory available to recipients together with worker binaries.\n"
        )
        (staging / "SOURCE-AVAILABILITY.txt").write_text(notice, encoding="ascii")
        record = {"schema": 1, "scope": "worker npm preferred sources, Cargo source superset, Python sources and notices",
                  "npm_lock_sha256": npm_lock_digest(npm_lock), "npm_lock_hash_mode": "sha256-lf",
                  "cargo_lock_sha256": digest(cargo_bytes),
                  "artifacts": sorted(records, key=lambda item: item["path"]), "crates": crates,
                  "python_source_receipts": python_receipts,
                  "npm_source_receipts": sorted(npm_receipts, key=lambda item: (item["package"], item["source_path"]))}
        (staging / RECORD).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="ascii")
        staging.rename(output)
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=Path(__file__).with_name("worker-sources.lock.json"))
    parser.add_argument("--npm-lock", type=Path, default=ROOT / "VoiceCaptions/discord_adapter/package-lock.json")
    args = parser.parse_args(argv)
    manifest = json.loads(args.manifest.read_text(encoding="ascii"))
    record = stage_sources(manifest, args.npm_lock.read_bytes(), args.output)
    print("Preserved worker source for " + str(len(record["crates"])) + " locked crates.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
