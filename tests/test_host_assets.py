"""Pinned build tools must stage offline and reject corrupt or escaping input."""

from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import zipfile

from Hosting import assets


class HostAssetTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.stage = self.root / "stage"
        self.lock_path = self.root / "assets.lock.json"
        self.node_files = {
            "node.exe": b"MZ private node",
            "node_modules/npm/bin/npm-cli.js": b"// npm fixture",
            "LICENSE": b"Node original notice",
        }
        node = self.make_zip("node.zip", {"node-test/" + name: body for name, body in self.node_files.items()})
        nssm = self.make_zip("nssm.zip", {
            "nssm-test/win64/nssm.exe": b"MZ private nssm",
            "nssm-test/src/nssm.cpp": b"// editable source",
            "nssm-test/README.txt": b"NSSM is public domain.",
        })
        cloud = self.cache / "cloudflared.exe"
        cloud.write_bytes(b"MZ private tunnel")
        source = self.make_zip("module.zip", {
            "module.test/example@v1.0.0/LICENSE": b"Original module notice",
            "module.test/example@v1.0.0/main.go": b"package example",
        })
        self.lock = {
            "schema": 1,
            "architecture": "windows-amd64",
            "node": {**self.record(node), "version": "22.23.4", "npm_version": "10.9.9",
                     "archive_root": "node-test", "payload_tree_sha256": self.node_tree_hash()},
            "nssm": {**self.record(nssm), "version": "2.24-101-g897c7ad", "archive_root": "nssm-test",
                     "executable_sha256": hashlib.sha256(b"MZ private nssm").hexdigest()},
            "cloudflared": {**self.record(cloud), "version": "2026.10.7"},
            "sources": [
                {**self.record(nssm), "notice_members": ["nssm-test/README.txt"],
                 "source_member": "nssm-test/src/nssm.cpp"},
                {**self.record(source), "notice_members": ["module.test/example@v1.0.0/LICENSE"],
                 "go_sum": self.go_hash(source)},
            ],
        }
        self.save_lock()

    def make_zip(self, name, files):
        path = self.cache / name
        with zipfile.ZipFile(path, "w") as archive:
            for member, payload in files.items():
                archive.writestr(member, payload)
        return path

    @staticmethod
    def record(path):
        return {"filename": path.name, "url": "https://assets.example.test/" + path.name,
                "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def node_tree_hash(self):
        body = "\n".join(name + " " + hashlib.sha256(payload).hexdigest()
                         for name, payload in sorted(self.node_files.items())) + "\n"
        return hashlib.sha256(body.encode("ascii")).hexdigest()

    @staticmethod
    def go_hash(path):
        digest = hashlib.sha256()
        with zipfile.ZipFile(path) as archive:
            for name in sorted(archive.namelist()):
                digest.update((hashlib.sha256(archive.read(name)).hexdigest() + "  " + name + "\n").encode())
        return "h1:" + base64.b64encode(digest.digest()).decode()

    def save_lock(self):
        self.lock_path.write_bytes(assets._json_bytes(self.lock))

    def stage_tools(self):
        with patch.object(assets.urllib.request, "urlopen", side_effect=AssertionError("unexpected network")):
            return assets.stage_tools(self.stage, self.cache, lock_path=self.lock_path)

    def test_offline_stage_has_exact_private_paths_and_original_notices_and_source(self):
        paths = self.stage_tools()
        self.assertEqual(set(assets.TOOL_PATHS), set(paths))
        self.assertEqual(b"MZ private node", paths["node"].read_bytes())
        self.assertEqual(self.stage / "tools/node/node_modules/npm/bin/npm-cli.js", paths["npm"])
        self.assertEqual(b"MZ private nssm", paths["nssm"].read_bytes())
        self.assertEqual(b"MZ private tunnel", paths["cloudflared"].read_bytes())
        self.assertEqual((self.cache / "module.zip").read_bytes(), (self.stage / "tools/Source/module.zip").read_bytes())
        notice = self.stage / "tools/notices/module.zip/0-LICENSE"
        self.assertEqual(b"Original module notice", notice.read_bytes())
        manifest = (self.stage / "tools/MANIFEST.json").read_bytes()
        self.assertEqual(paths, self.stage_tools())
        self.assertEqual(manifest, (self.stage / "tools/MANIFEST.json").read_bytes())

    def test_cached_same_size_corruption_is_rejected_without_publishing_tools(self):
        path = self.cache / "cloudflared.exe"
        path.write_bytes(b"NZ private tunnel")
        with self.assertRaisesRegex(assets.AssetError, "checksum mismatch"):
            self.stage_tools()
        self.assertFalse((self.stage / "tools").exists())

    def test_zip_traversal_or_link_is_rejected_before_any_tool_is_published(self):
        for member in ("node-test/../../escape.exe", "node-test/link"):
            with self.subTest(member=member):
                path = self.make_zip("node.zip", {"node-test/" + name: body for name, body in self.node_files.items()})
                with zipfile.ZipFile(path, "a") as archive:
                    if member.endswith("link"):
                        info = zipfile.ZipInfo(member)
                        info.create_system = 3
                        info.external_attr = (stat.S_IFLNK | 0o777) << 16
                        archive.writestr(info, "../../escape")
                    else:
                        archive.writestr(member, b"bad")
                self.lock["node"].update(self.record(path))
                self.save_lock()
                with self.assertRaises(assets.AssetError):
                    self.stage_tools()
                self.assertFalse((self.stage / "tools").exists())
                self.assertFalse((self.root / "escape.exe").exists())

    def test_missing_declared_source_notice_aborts_stage(self):
        self.lock["sources"][1]["notice_members"] = ["missing/LICENSE"]
        self.save_lock()
        with self.assertRaises(assets.AssetError):
            self.stage_tools()
        self.assertFalse((self.stage / "tools").exists())

    def test_module_source_must_match_embedded_go_build_sum(self):
        self.lock["sources"][1]["go_sum"] = "h1:" + "A" * 44
        self.save_lock()
        with self.assertRaisesRegex(assets.AssetError, "embedded build information"):
            self.stage_tools()

    def test_staged_executable_stays_pinned_even_if_manifest_is_rewritten(self):
        paths = self.stage_tools()
        paths["node"].write_bytes(b"MZ replaced node")
        manifest_path = self.stage / "tools/MANIFEST.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["files"]["node/node.exe"] = {
            "bytes": paths["node"].stat().st_size,
            "sha256": hashlib.sha256(paths["node"].read_bytes()).hexdigest(),
        }
        manifest_path.write_bytes(assets._json_bytes(manifest))
        with self.assertRaisesRegex(assets.AssetError, "pinned tool"):
            self.stage_tools()

    def test_extra_or_missing_file_invalidates_the_stage(self):
        paths = self.stage_tools()
        extra = self.stage / "tools/node/MANIFEST.json"
        extra.write_text("{}", encoding="ascii")
        with self.assertRaises(assets.AssetError):
            self.stage_tools()
        extra.unlink()
        paths["npm"].unlink()
        with self.assertRaises(assets.AssetError):
            self.stage_tools()

    def test_lock_refuses_plain_http_and_escaping_cache_filename(self):
        for key, value in (("url", "http://example.test/tool.zip"), ("filename", "../escape.zip")):
            original = self.lock["node"][key]
            self.lock["node"][key] = value
            self.save_lock()
            with self.assertRaises(assets.AssetError):
                self.stage_tools()
            self.lock["node"][key] = original

    def test_oversized_download_does_not_poison_cache(self):
        item = self.record(self.cache / "cloudflared.exe")
        (self.cache / "cloudflared.exe").unlink()
        response = io.BytesIO(b"x" * (item["bytes"] + 1))
        response.geturl = lambda: item["url"]
        with patch.object(assets.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(assets.AssetError, "pinned size"):
                assets._fetch(item, self.cache)
        self.assertFalse((self.cache / item["filename"]).exists())
        self.assertEqual([], list(self.cache.glob("*.partial")))

    def test_transient_503_retries_but_retains_checksum_verification(self):
        path = self.cache / "cloudflared.exe"
        item = self.record(path)
        payload = path.read_bytes()
        path.unlink()
        response = io.BytesIO(payload)
        response.geturl = lambda: item["url"]
        unavailable = urllib.error.HTTPError(item["url"], 503, "Unavailable", {}, None)
        with patch.object(assets.urllib.request, "urlopen", side_effect=[unavailable, response]) as opened:
            with patch.object(assets.time, "sleep"):
                self.assertEqual(path, assets._fetch(item, self.cache))
        self.assertEqual(2, opened.call_count)
        self.assertEqual(payload, path.read_bytes())


if __name__ == "__main__":
    unittest.main()
