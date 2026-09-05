"""Offline source-availability checks for the worker's native dependencies."""

import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging"))
import worker_sources

NPM_LOCK = b'{"packages":{"node_modules/@snazzah/davey":{"version":"0.1.12","integrity":"native-fixture"},"node_modules/compiled":{"version":"1.0.0","integrity":"js-fixture"}}}\n'


def archive(files):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class WorkerSourceTests(unittest.TestCase):
    def test_repository_source_inventory_accepts_the_current_npm_lock(self):
        manifest = json.loads((ROOT / "packaging/worker-sources.lock.json").read_text())
        npm_lock = (ROOT / "VoiceCaptions/discord_adapter/package-lock.json").read_bytes()

        class DownloadReached(Exception):
            pass

        def stop_before_download(_url):
            # Exercise the real hash, license, and package coverage checks;
            # reaching this boundary proves preflight passed without network.
            raise DownloadReached()

        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "sources"
            with self.assertRaises(DownloadReached):
                worker_sources.stage_sources(manifest, npm_lock, output, stop_before_download)
            self.assertFalse(output.exists())

    def fixture(self, source=None):
        package = b'[package]\nname="hpke-rs"\nversion="0.6.1"\nlicense="MPL-2.0"\n'
        crate = archive({"hpke-rs-0.6.1/Cargo.toml": package,
                         "hpke-rs-0.6.1/src/lib.rs": source or b"// exact upstream source\n"})
        cargo = ('version = 4\n[[package]]\nname="hpke-rs"\nversion="0.6.1"\n'
                 'source="registry+https://github.com/rust-lang/crates.io-index"\n'
                 'checksum="' + worker_sources.digest(crate) + '"\n').encode()
        davey = archive({"davey/Cargo.lock": cargo, "davey/LICENSE": b"upstream MIT fixture"})
        data = {"https://fixture/davey": davey,
                "https://static.crates.io/crates/hpke-rs/hpke-rs-0.6.1.crate": crate}
        data["https://fixture/compiled"] = archive({"compiled/src/index.ts": b"export const source = true;\n"})
        data["https://fixture/python"] = archive({"websockets/speedups.c": b"/* original C source */\n"})
        licenses = []
        for name in sorted(worker_sources.REQUIRED_LICENSES):
            url = "https://fixture/" + name
            data[url] = ("exact upstream license fixture: " + name).encode()
            licenses.append({"name": name, "url": url, "sha256": worker_sources.digest(data[url])})
        manifest = {"schema": 1, "npm_lock_hash_mode": "sha256-lf", "npm_lock_sha256": worker_sources.npm_lock_digest(NPM_LOCK),
                    "licenses": licenses,
                    "python_sources": [{"name": "websockets.tar.gz", "url": "https://fixture/python",
                                        "sha256": worker_sources.digest(data["https://fixture/python"]),
                                        "package": "websockets==13.1", "source_member": "websockets/speedups.c",
                                        "metadata_url": "https://fixture/python-metadata"}],
                    "davey": {"name": "davey.tar.gz", "url": "https://fixture/davey",
                              "sha256": worker_sources.digest(davey), "cargo_lock_path": "davey/Cargo.lock",
                              "cargo_lock_sha256": worker_sources.digest(cargo), "packages": ["@snazzah/davey@0.1.12"]},
                    "npm_sources": [{"name": "compiled.tar.gz", "url": "https://fixture/compiled",
                                     "sha256": worker_sources.digest(data["https://fixture/compiled"]),
                                     "source_member": "compiled/src/index.ts", "packages": ["compiled@1.0.0"],
                                     "receipts": [{"package": "compiled@1.0.0", "npm_integrity": "js-fixture",
                                                   "basis": "npm gitHead", "metadata_url": "https://fixture/metadata"}]}]}
        return manifest, data

    def test_complete_record_preserves_mpl_source_and_license_bytes(self):
        manifest, data = self.fixture()
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "sources"
            result = worker_sources.stage_sources(manifest, NPM_LOCK, output, data.__getitem__)
            self.assertEqual("MPL-2.0", result["crates"][0]["declared_license"])
            self.assertEqual(data["https://fixture/MPL-2.0.txt"], (output / "licenses/MPL-2.0.txt").read_bytes())
            self.assertEqual(data["https://static.crates.io/crates/hpke-rs/hpke-rs-0.6.1.crate"],
                             (output / "sources/crates/hpke-rs-0.6.1.crate").read_bytes())
            self.assertTrue((output / worker_sources.RECORD).is_file())
            self.assertIn("MPL-2.0 covered source", (output / "SOURCE-AVAILABILITY.txt").read_text())
            receipt = next(item for item in result["npm_source_receipts"] if item["package"] == "compiled@1.0.0")
            self.assertEqual("js-fixture", receipt["npm_integrity"])
            self.assertEqual("https://fixture/metadata", receipt["metadata_url"])
            self.assertEqual(data["https://fixture/compiled"], (output / receipt["source_path"]).read_bytes())

    def test_missing_or_corrupt_mpl_source_or_license_produces_no_record(self):
        for url in ("https://fixture/MPL-2.0.txt",
                    "https://static.crates.io/crates/hpke-rs/hpke-rs-0.6.1.crate",
                    "https://fixture/python"):
            for corrupt in (False, True):
                with self.subTest(url=url, corrupt=corrupt), tempfile.TemporaryDirectory() as folder:
                    manifest, data = self.fixture()
                    if corrupt:
                        data[url] = b"corrupt"
                    else:
                        del data[url]
                    output = Path(folder) / "sources"
                    with self.assertRaises((KeyError, ValueError)):
                        worker_sources.stage_sources(manifest, NPM_LOCK, output, data.__getitem__)
                    self.assertFalse(output.exists())

    def test_omitted_mpl_license_and_changed_npm_lock_fail_before_download(self):
        manifest, data = self.fixture()
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "sources"
            with self.assertRaisesRegex(ValueError, "package-lock"):
                worker_sources.stage_sources(manifest, b"changed", output, data.__getitem__)
            manifest["licenses"] = [item for item in manifest["licenses"] if item["name"] != "MPL-2.0.txt"]
            with self.assertRaisesRegex(ValueError, "license artifact"):
                worker_sources.stage_sources(manifest, NPM_LOCK, output, data.__getitem__)
            self.assertFalse(output.exists())

    def test_applied_mpl_secondary_license_restriction_requires_review(self):
        manifest, data = self.fixture(b'// This Source Code Form is "Incompatible With Secondary Licenses".\n')
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "sources"
            with self.assertRaisesRegex(ValueError, "requires review"):
                worker_sources.stage_sources(manifest, NPM_LOCK, output, data.__getitem__)
            self.assertFalse(output.exists())

    def test_existing_output_is_not_overwritten(self):
        manifest, data = self.fixture()
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "already exists"):
                worker_sources.stage_sources(manifest, NPM_LOCK, Path(folder), data.__getitem__)

    def test_missing_preferred_npm_source_or_receipt_blocks_distribution(self):
        for missing in ("source", "receipt", "coverage"):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as folder:
                manifest, data = self.fixture()
                if missing == "source":
                    del data["https://fixture/compiled"]
                elif missing == "receipt":
                    manifest["npm_sources"][0]["receipts"] = []
                else:
                    manifest["npm_sources"] = []
                output = Path(folder) / "sources"
                with self.assertRaises((KeyError, ValueError)):
                    worker_sources.stage_sources(manifest, NPM_LOCK, output, data.__getitem__)
                self.assertFalse(output.exists())

    def test_windows_line_endings_match_the_git_source_lock(self):
        manifest, data = self.fixture()
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "sources"
            record = worker_sources.stage_sources(manifest, NPM_LOCK.replace(b"\n", b"\r\n"), output, data.__getitem__)
            self.assertEqual(worker_sources.digest(NPM_LOCK), record["npm_lock_sha256"])
            self.assertEqual("sha256-lf", record["npm_lock_hash_mode"])


if __name__ == "__main__":
    unittest.main()
