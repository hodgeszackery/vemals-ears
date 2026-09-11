# SPDX-License-Identifier: GPL-3.0-only
"""Runtime source releases must match their binaries and survive offline use."""

import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging"))
import runtime_sources


def write_source_fixture(folder: Path, runtime_lock: Path | None = None) -> dict:
    """Tiny editable source inputs, also usable by installer integration tests.

    Supply an existing runtime lock to preserve its actual component versions.
    No network, real interpreters, credentials or upstream archives are needed.
    """
    folder.mkdir(parents=True, exist_ok=True)
    if runtime_lock is None:
        runtime_lock = folder / "runtime-lock.json"
        runtime_lock.write_bytes(runtime_sources.json_bytes({
            "schema": 1, "python": {"version": "3.14.7"},
            "wheels": [{"name": "pillow", "version": "12.3.0"}],
        }))
    runtime = json.loads(runtime_lock.read_text(encoding="utf-8"))
    components = {"cpython": runtime["python"]["version"]}
    components.update({item["name"]: item["version"] for item in runtime.get("wheels", [])})
    cache = folder / "cache"
    cache.mkdir(exist_ok=True)
    artifacts = []
    payloads = {}
    for name, version in components.items():
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(zipfile.ZipInfo("LICENSE"), "fixture upstream notice\r\n")
            archive.writestr(zipfile.ZipInfo("source.c"), "/* editable source fixture */\n")
        payload = buffer.getvalue()
        filename = name + "-" + version + ".zip"
        (cache / filename).write_bytes(payload)
        url = "https://example.test/" + filename
        payloads[url] = payload
        artifacts.append({
            "name": name, "version": version, "runtime_component": name,
            "filename": filename, "url": url,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "provenance": "offline source fixture",
        })
    lock = {
        "schema": 1, "runtime_lock_hash_mode": "sha256-lf",
        "runtime_lock_sha256": runtime_sources.normalized_digest(runtime_lock),
        "runtime_components": components, "artifacts": artifacts,
        "build_notes": ["Modify source.c and build with your normal compiler."],
        "coverage_notes": ["Small offline fixture, not production sources."],
    }
    source_lock = folder / "runtime-sources.lock.json"
    source_lock.write_bytes(runtime_sources.json_bytes(lock))
    return {
        "runtime": folder / "runtime", "cache": cache,
        "source_lock": source_lock, "runtime_lock": runtime_lock,
        "lock": lock, "payloads": payloads,
    }


def gitiles_archive(mtime: float, payload: bytes, *, reverse: bool = False) -> bytes:
    entries = [("src/run.sh", payload, 0o755), ("README.txt", b"source\n", 0o644)]
    if reverse:
        entries.reverse()
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
        directory = tarfile.TarInfo("src")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        directory.mtime = mtime
        archive.addfile(directory)
        for name, data, mode in entries:
            item = tarfile.TarInfo(name)
            item.mode = mode
            item.mtime = mtime
            item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
    return output.getvalue()


def archive_with_members(members, *, archive_format=tarfile.PAX_FORMAT) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz", format=archive_format) as archive:
        for name, kind, data, pax_headers in members:
            item = tarfile.TarInfo(name)
            item.mode = 0o644
            item.mtime = 1000
            item.pax_headers = pax_headers
            if kind == "file":
                item.size = len(data)
                archive.addfile(item, io.BytesIO(data))
            elif kind == "symlink":
                item.type = tarfile.SYMTYPE
                item.linkname = data.decode("ascii")
                archive.addfile(item)
            elif kind == "fifo":
                item.type = tarfile.FIFOTYPE
                archive.addfile(item)
            else:
                raise AssertionError("unknown test archive member")
    return output.getvalue()


class RuntimeSourcesTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.fixture = write_source_fixture(self.folder)

    def stage(self, **kwargs):
        fixture = self.fixture
        return runtime_sources.stage_runtime_sources(
            fixture["runtime"], fixture["cache"],
            source_lock=fixture["source_lock"], runtime_lock=fixture["runtime_lock"], **kwargs,
        )

    def check(self):
        fixture = self.fixture
        return runtime_sources.validate_runtime_sources(
            fixture["runtime"], source_lock=fixture["source_lock"],
            runtime_lock=fixture["runtime_lock"],
        )

    def write_lock(self, lock):
        self.fixture["source_lock"].write_bytes(runtime_sources.json_bytes(lock))

    def test_aom_source_uses_the_immutable_official_release_object(self):
        lock = json.loads(runtime_sources.DEFAULT_SOURCE_LOCK.read_text(encoding="ascii"))
        aom = next(item for item in lock["artifacts"] if item["name"] == "pillow-aom")
        self.assertEqual("libaom-3.14.1.tar.gz", aom["filename"])
        self.assertEqual(
            "https://storage.googleapis.com/aom-releases/libaom-3.14.1.tar.gz"
            "?generation=1779477091458380",
            aom["url"],
        )
        self.assertNotIn("+archive", aom["url"])
        self.assertEqual(
            "44bf90dbd23e734d50e70a8c41c285193922938bd0d3bc2ee56764d181d55ef5",
            aom["sha256"],
        )
        self.assertEqual(
            ["libaom-3.14.1/LICENSE", "libaom-3.14.1/PATENTS"],
            aom["license_files"],
        )
        self.assertIn("03087864cf4bea6abb0d28f95cf7843511413d8f", aom["provenance"])
        self.assertIn("libaom-3.14.1.tar.gz.asc?generation=1779477091374696", aom["provenance"])
        self.assertIn("B002F08B74A148DAA01F7123A48E86DB0B830498", aom["provenance"])

    def test_libyuv_source_is_commit_and_semantic_tree_pinned(self):
        lock = json.loads(runtime_sources.DEFAULT_SOURCE_LOCK.read_text(encoding="ascii"))
        libyuv = next(item for item in lock["artifacts"] if item["name"] == "pillow-libyuv")
        self.assertEqual("libyuv-644251f.tar", libyuv["filename"])
        self.assertEqual(runtime_sources.GITILES_CANONICALIZATION, libyuv["canonicalization"])
        self.assertEqual("ba22b636166dc16532fcc9d5a9524117432d1782", libyuv["git_tree_sha1"])
        self.assertEqual(
            "3938b5524e30ff82b953ee69b910726a97c4a0468084defe4107399ef5908ae8",
            libyuv["sha256"],
        )
        self.assertEqual(["LICENSE", "PATENTS"], libyuv["license_files"])

    def test_declared_license_files_must_exist_in_the_verified_archive(self):
        archive_path = self.folder / "source.tar"
        with tarfile.open(archive_path, "w", format=tarfile.USTAR_FORMAT) as archive:
            for name in ("LICENSE", "PATENTS"):
                payload = (name + " fixture\n").encode("ascii")
                member = tarfile.TarInfo(name)
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
        item = {"filename": "source.tar", "license_files": ["LICENSE", "PATENTS"]}
        runtime_sources.validate_declared_license_files(item, archive_path)
        item["license_files"].append("MISSING")
        with self.assertRaisesRegex(SystemExit, "missing declared license files"):
            runtime_sources.validate_declared_license_files(item, archive_path)

    def test_fresh_downloads_verify_notices_before_promoting_temporary_files(self):
        members = [
            ("LICENSE", "file", b"license\n", {}),
            ("PATENTS", "file", b"patents\n", {}),
        ]
        release = archive_with_members(members)
        release_item = {
            "filename": "release.tar.gz",
            "url": "https://example.test/release.tar.gz",
            "sha256": hashlib.sha256(release).hexdigest(),
            "license_files": ["LICENSE", "PATENTS"],
        }
        release_cache = self.folder / "release-cache"
        with mock.patch.object(
            runtime_sources.urllib.request, "urlopen", return_value=io.BytesIO(release),
        ):
            downloaded = runtime_sources.verified_download(release_item, release_cache)
        self.assertEqual(release, downloaded.read_bytes())

        raw_gitiles = archive_with_members(members)
        source = self.folder / "raw.tar.gz"
        canonical = self.folder / "canonical.tar"
        source.write_bytes(raw_gitiles)
        tree = runtime_sources.canonicalize_gitiles_archive(source, canonical)
        gitiles_item = {
            "filename": "snapshot.tar",
            "url": "https://example.googlesource.com/code/+archive/" + "a" * 40 + ".tar.gz",
            "sha256": hashlib.sha256(canonical.read_bytes()).hexdigest(),
            "git_tree_sha1": tree,
            "canonicalization": runtime_sources.GITILES_CANONICALIZATION,
            "license_files": ["LICENSE", "PATENTS"],
        }
        gitiles_cache = self.folder / "gitiles-cache"
        with mock.patch.object(
            runtime_sources.urllib.request, "urlopen", return_value=io.BytesIO(raw_gitiles),
        ):
            downloaded = runtime_sources.verified_download(gitiles_item, gitiles_cache)
        self.assertEqual(canonical.read_bytes(), downloaded.read_bytes())

    def test_gitiles_snapshots_are_commit_pinned_and_never_raw_hash_pinned(self):
        original = self.fixture["lock"]
        lock = json.loads(json.dumps(original))
        item = lock["artifacts"][0]
        item["url"] = "https://example.googlesource.com/code/+archive/refs/tags/v1.tar.gz"
        self.write_lock(lock)
        with self.assertRaisesRegex(SystemExit, "require deterministic canonicalization"):
            self.stage(offline=True)

        item["canonicalization"] = runtime_sources.GITILES_CANONICALIZATION
        item["filename"] = "snapshot.tar"
        item["git_tree_sha1"] = "a" * 40
        self.write_lock(lock)
        with self.assertRaisesRegex(SystemExit, "must pin a full commit"):
            self.stage(offline=True)

        item["url"] = "https://example.googlesource.com/code/+archive/" + "a" * 40
        self.write_lock(lock)
        with self.assertRaisesRegex(SystemExit, "must pin a full commit"):
            self.stage(offline=True)

    def test_gitiles_request_metadata_is_removed_and_content_changes_fail_closed(self):
        first = gitiles_archive(1000.25, b"echo safe\n")
        second = gitiles_archive(2000.75, b"echo safe\n", reverse=True)
        self.assertNotEqual(hashlib.sha256(first).digest(), hashlib.sha256(second).digest())
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            source = folder / "source.tar.gz"
            canonical = folder / "canonical.tar"
            source.write_bytes(first)
            tree = runtime_sources.canonicalize_gitiles_archive(source, canonical)
            expected = canonical.read_bytes()
            item = {
                "filename": "snapshot.tar",
                "url": "https://example.googlesource.com/code/+archive/" + "a" * 40 + ".tar.gz",
                "sha256": hashlib.sha256(expected).hexdigest(),
                "git_tree_sha1": tree,
                "canonicalization": runtime_sources.GITILES_CANONICALIZATION,
            }
            cache = folder / "cache"
            with mock.patch.object(runtime_sources.urllib.request, "urlopen", return_value=io.BytesIO(second)):
                result = runtime_sources.verified_download(item, cache)
            self.assertEqual(expected, result.read_bytes())

            result.unlink()
            changed = gitiles_archive(3000.5, b"echo changed\n")
            with mock.patch.object(runtime_sources.urllib.request, "urlopen", return_value=io.BytesIO(changed)):
                with self.assertRaisesRegex(SystemExit, "Git tree mismatch"):
                    runtime_sources.verified_download(item, cache)
            self.assertFalse(result.exists())

    def test_gitiles_canonicalization_rejects_unsafe_or_ambiguous_archives(self):
        cases = {
            "parent traversal": [("../escape", "file", b"x", {})],
            "absolute path": [("/escape", "file", b"x", {})],
            "backslash path": [(r"dir\escape", "file", b"x", {})],
            "escaping symlink": [("link", "symlink", b"../../outside", {})],
            "special member": [("pipe", "fifo", b"", {})],
            "duplicate path": [
                ("same", "file", b"one", {}),
                ("same", "file", b"two", {}),
            ],
            "case collision": [
                ("Folder/one", "file", b"one", {}),
                ("folder/two", "file", b"two", {}),
            ],
            "file and directory collision": [
                ("node", "file", b"one", {}),
                ("node/child", "file", b"two", {}),
            ],
            "unexpected PAX metadata": [("file", "file", b"x", {"comment": "no"})],
        }
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            for label, members in cases.items():
                with self.subTest(case=label):
                    source = folder / "source.tar.gz"
                    target = folder / "target.tar"
                    source.write_bytes(archive_with_members(members))
                    with self.assertRaisesRegex(SystemExit, "invalid Gitiles source archive"):
                        runtime_sources.canonicalize_gitiles_archive(source, target)
                    self.assertFalse(target.exists())

            source = folder / "source.tar.gz"
            target = folder / "target.tar"
            source.write_bytes(archive_with_members([
                ("payload", "file", b"four", {}),
            ]))
            with mock.patch.object(runtime_sources, "MAX_EXPANDED_BYTES", 3):
                with self.assertRaisesRegex(SystemExit, "size limit"):
                    runtime_sources.canonicalize_gitiles_archive(source, target)
            self.assertFalse(target.exists())

            source.write_bytes(archive_with_members([
                ("x" * 101, "file", b"x", {}),
            ], archive_format=tarfile.GNU_FORMAT))
            with self.assertRaisesRegex(SystemExit, "invalid Gitiles source archive"):
                runtime_sources.canonicalize_gitiles_archive(source, target)
            self.assertFalse(target.exists())

    def test_gitiles_archive_bombs_are_rejected_before_payload_consumption(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            source = folder / "source.tar.gz"
            target = folder / "target.tar"
            claimed = tarfile.TarInfo("claimed-huge-file")
            claimed.mode = 0o644
            claimed.size = runtime_sources.MAX_EXPANDED_BYTES + 1
            source.write_bytes(gzip.compress(
                claimed.tobuf(format=tarfile.USTAR_FORMAT) + b"\0" * 1024,
                mtime=0,
            ))
            with self.assertRaisesRegex(SystemExit, "size limit"):
                runtime_sources.canonicalize_gitiles_archive(source, target)
            self.assertFalse(target.exists())

            source.write_bytes(archive_with_members([
                ("payload", "file", b"small", {}),
            ]))
            with mock.patch.object(runtime_sources, "MAX_ARCHIVE_STREAM_BYTES", 511):
                with self.assertRaisesRegex(SystemExit, "stream limit"):
                    runtime_sources.canonicalize_gitiles_archive(source, target)
            self.assertFalse(target.exists())

    def test_downloaded_archives_are_preserved_and_repeated_offline_stage_is_identical(self):
        fixture = self.fixture
        for path in fixture["cache"].iterdir():
            path.unlink()
        (fixture["cache"] / "operator-token.txt").write_bytes(b"unrelated local state")
        with mock.patch.object(runtime_sources.urllib.request, "urlopen", side_effect=(
            lambda url, **kwargs: io.BytesIO(fixture["payloads"][url])
        )) as downloaded:
            manifest = self.stage()
        self.assertEqual(len(fixture["payloads"]), downloaded.call_count)
        source = fixture["runtime"] / "Source"
        before = {path.name: path.read_bytes() for path in source.iterdir()}
        for path in source.iterdir():
            self.assertGreaterEqual(time.localtime(path.stat().st_mtime).tm_year, 1980)
        for item in manifest["artifacts"]:
            self.assertEqual(fixture["payloads"][item["url"]], before[item["filename"]])
        self.assertNotIn("operator-token.txt", before)
        with mock.patch.object(runtime_sources.urllib.request, "urlopen") as downloaded:
            self.stage(offline=True)
        downloaded.assert_not_called()
        self.assertEqual(before, {path.name: path.read_bytes() for path in source.iterdir()})
        self.check()

    def test_missing_offline_archive_fails_before_declaring_source_complete(self):
        next(self.fixture["cache"].iterdir()).unlink()
        with self.assertRaisesRegex(SystemExit, "missing from offline cache"):
            self.stage(offline=True)
        self.assertFalse((self.fixture["runtime"] / "Source/MANIFEST.json").exists())

    def test_binary_lock_change_fails_closed_but_line_ending_conversion_is_harmless(self):
        path = self.fixture["runtime_lock"]
        original = path.read_bytes()
        path.write_bytes(original.replace(b"\n", b"\r\n"))
        self.stage(offline=True)
        path.write_bytes(original.replace(b"3.14.7", b"3.14.8"))
        with mock.patch.object(runtime_sources.urllib.request, "urlopen") as downloaded:
            with self.assertRaisesRegex(SystemExit, "runtime-lock.json changed"):
                self.stage()
        downloaded.assert_not_called()

    def test_sources_require_matching_versions_nonempty_inventory_and_all_components(self):
        original = self.fixture["lock"]
        for mutation, message in (
            (lambda lock: lock["runtime_components"].update(cpython="3.14.8"), "version mismatch"),
            (lambda lock: lock.update(artifacts=[]), "inventory is empty"),
            (lambda lock: lock["artifacts"].pop(), "missing runtime component source"),
            (lambda lock: lock["artifacts"][0].update(version="3.14.8"), "artifact version mismatch"),
            (lambda lock: lock["artifacts"][0].update(filename="../outside.zip"), "unsafe"),
        ):
            with self.subTest(message=message):
                lock = json.loads(json.dumps(original))
                mutation(lock)
                self.write_lock(lock)
                with self.assertRaisesRegex(SystemExit, message):
                    self.stage(offline=True)

    def test_corruption_is_rejected_in_download_cache_and_staged_source(self):
        item = self.fixture["lock"]["artifacts"][0]
        cached = self.fixture["cache"] / item["filename"]
        cached.unlink()
        with mock.patch.object(runtime_sources.urllib.request, "urlopen", return_value=io.BytesIO(b"wrong")):
            with self.assertRaisesRegex(SystemExit, "SHA256 mismatch"):
                self.stage()
        self.assertFalse(cached.exists())
        self.assertEqual([], list(self.fixture["cache"].glob("*.part")))
        cached.write_bytes(b"corrupt cached source")
        with self.assertRaisesRegex(SystemExit, "SHA256 mismatch"):
            self.stage(offline=True)
        cached.write_bytes(self.fixture["payloads"][item["url"]])
        self.stage(offline=True)
        (self.fixture["runtime"] / "Source" / item["filename"]).write_bytes(b"corrupt staged source")
        with self.assertRaisesRegex(SystemExit, "SHA256 mismatch"):
            self.check()

    def test_missing_archives_unknown_state_and_modified_manifest_cannot_pass_validation(self):
        self.stage(offline=True)
        source = self.fixture["runtime"] / "Source"
        for name in (self.fixture["lock"]["artifacts"][0]["filename"], "MANIFEST.json", "README.txt"):
            with self.subTest(missing=name):
                path = source / name
                original = path.read_bytes()
                path.unlink()
                with self.assertRaisesRegex(SystemExit, "inventory mismatch"):
                    self.check()
                path.write_bytes(original)
        unknown = source / "operator-token.txt"
        unknown.write_bytes(b"must not be packaged")
        with self.assertRaisesRegex(SystemExit, "untracked files"):
            self.stage(offline=True)
        unknown.unlink()
        (source / "MANIFEST.json").write_bytes(b"{}")
        with self.assertRaisesRegex(SystemExit, "manifest or build notes differ"):
            self.check()

    def test_slim_install_accepts_only_the_exact_two_file_source_receipt(self):
        self.stage(offline=True)
        source = self.fixture["runtime"] / "Source"
        for item in self.fixture["lock"]["artifacts"]:
            (source / item["filename"]).unlink()

        receipt = runtime_sources.validate_runtime_sources(
            self.fixture["runtime"],
            source_lock=self.fixture["source_lock"],
            runtime_lock=self.fixture["runtime_lock"],
            allow_receipt_only=True,
        )
        self.assertEqual(self.fixture["lock"]["runtime_components"], receipt["runtime_components"])
        with self.assertRaisesRegex(SystemExit, "inventory mismatch"):
            self.check()

        readme = source / "README.txt"
        original = readme.read_bytes()
        readme.write_bytes(original + b"changed\n")
        with self.assertRaisesRegex(SystemExit, "receipt differs"):
            runtime_sources.validate_runtime_sources(
                self.fixture["runtime"],
                source_lock=self.fixture["source_lock"],
                runtime_lock=self.fixture["runtime_lock"],
                allow_receipt_only=True,
            )
        readme.write_bytes(original)
        (source / "partial.zip").write_bytes(b"partial")
        with self.assertRaisesRegex(SystemExit, "inventory mismatch"):
            runtime_sources.validate_runtime_sources(
                self.fixture["runtime"],
                source_lock=self.fixture["source_lock"],
                runtime_lock=self.fixture["runtime_lock"],
                allow_receipt_only=True,
            )

    def test_cli_works_from_extracted_editable_locks_without_git_and_reports_missing_source(self):
        fixture = self.fixture
        command = [
            sys.executable, "-E", "-s", "-B", str(ROOT / "packaging/runtime_sources.py"),
            "--runtime", str(fixture["runtime"]), "--cache", str(fixture["cache"]),
            "--lock", str(fixture["source_lock"]), "--runtime-lock", str(fixture["runtime_lock"]),
            "--offline",
        ]
        result = subprocess.run(command, cwd=self.folder, capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("source inputs verified", result.stdout)
        (fixture["runtime"] / "Source/README.txt").unlink()
        result = subprocess.run(command + ["--check"], cwd=self.folder, capture_output=True, text=True, timeout=15)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("inventory mismatch", result.stderr)


if __name__ == "__main__":
    unittest.main()
