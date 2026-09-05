# SPDX-License-Identifier: GPL-3.0-only
"""Runtime source releases must match their binaries and survive offline use."""

import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
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
