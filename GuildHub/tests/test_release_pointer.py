from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from GuildHub.release_pointer import (
    MAX_ARTIFACT_BYTES,
    POINTER_FILENAME,
    ReleasePointerError,
    canonical_version,
    immutable_artifact,
    load_release_pointer,
)


class ReleasePointerTests(unittest.TestCase):
    VERSION = "0.5.13"

    def setUp(self):
        self.temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.temporary)
        self.installer = self.root / "Vemals-Ears-Setup-0.5.13.exe"
        self.source = self.root / "vemals-ears-member-0.5.13.zip"
        self.installer.write_bytes(b"MZ installer")
        self.source.write_bytes(b"PK source")
        self.document = {
            "schema": 1,
            "version": self.VERSION,
            "installer": {
                "filename": self.installer.name,
                "bytes": self.installer.stat().st_size,
                "sha256": hashlib.sha256(self.installer.read_bytes()).hexdigest(),
            },
            "source": {
                "filename": self.source.name,
                "bytes": self.source.stat().st_size,
                "sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
            },
        }

    def write(self, document=None):
        (self.root / POINTER_FILENAME).write_text(
            json.dumps(self.document if document is None else document) + "\n",
            encoding="ascii",
        )

    def assert_invalid(self, document):
        self.write(document)
        with self.assertRaises(ReleasePointerError):
            load_release_pointer(self.root)

    def test_absent_is_the_only_non_error_without_a_snapshot(self):
        self.assertIsNone(load_release_pointer(self.root))
        self.write()
        snapshot = load_release_pointer(self.root)
        self.assertEqual(self.VERSION, snapshot.version)
        self.assertEqual(self.installer, snapshot.installer.path)
        self.assertEqual(self.source, snapshot.source.path)

    def test_release_version_is_exactly_three_canonical_numeric_parts(self):
        self.assertEqual("0.5.13", canonical_version("0.5.13"))
        for value in (
            None,
            1,
            "",
            "0.5",
            "0.5.13.0",
            "00.5.13",
            "0.05.13",
            "0.5.013",
            "0.5.65536",
            "0.5.-1",
            "0.5.1/source",
        ):
            with self.subTest(value=value):
                self.assertIsNone(canonical_version(value))

    def test_root_and_artifact_shapes_are_exact(self):
        for mutate in (
            lambda item: item.pop("source"),
            lambda item: item.update(extra=True),
            lambda item: item["installer"].pop("sha256"),
            lambda item: item["source"].update(extra=True),
        ):
            document = deepcopy(self.document)
            mutate(document)
            with self.subTest(document=document):
                self.assert_invalid(document)

    def test_schema_version_names_sizes_and_digests_are_strict(self):
        changes = (
            ("schema", True),
            ("schema", 2),
            ("version", "0.05.13"),
            ("version", "0.5.13.0"),
            ("installer.filename", "../Vemals-Ears-Setup-0.5.13.exe"),
            ("installer.filename", "Vemals-Ears-Setup.exe"),
            ("installer.bytes", True),
            ("installer.bytes", 0),
            ("installer.bytes", MAX_ARTIFACT_BYTES + 1),
            ("installer.sha256", "A" * 64),
            ("installer.sha256", "a" * 63),
            ("source.filename", "vemals-ears-member.zip"),
            ("source.bytes", -1),
            ("source.sha256", "g" * 64),
        )
        for dotted, value in changes:
            document = deepcopy(self.document)
            target = document
            parts = dotted.split(".")
            for part in parts[:-1]:
                target = target[part]
            target[parts[-1]] = value
            with self.subTest(field=dotted, value=value):
                self.assert_invalid(document)

    def test_missing_directory_or_wrong_size_artifact_is_invalid(self):
        self.source.unlink()
        self.source.mkdir()
        self.assert_invalid(self.document)
        self.source.rmdir()
        self.source.write_bytes(b"PK changed size")
        self.assert_invalid(self.document)

    def test_duplicate_keys_and_oversized_pointer_are_invalid(self):
        pointer = self.root / POINTER_FILENAME
        pointer.write_text(
            '{"schema":1,"schema":1,"version":"0.5.13",'
            '"installer":{},"source":{}}',
            encoding="ascii",
        )
        with self.assertRaisesRegex(ReleasePointerError, "duplicate"):
            load_release_pointer(self.root)
        pointer.write_bytes(b" " * (16 * 1024 + 1))
        with self.assertRaisesRegex(ReleasePointerError, "size"):
            load_release_pointer(self.root)

    def test_hostile_json_values_fail_as_pointer_errors(self):
        pointer = self.root / POINTER_FILENAME
        payloads = (
            b'{"schema":NaN}',
            b'{"schema":' + (b"9" * 1000) + b"}",
            (b"[" * 1000) + b"0" + (b"]" * 1000),
            b"\xff",
        )
        for payload in payloads:
            with self.subTest(payload=payload[:20]):
                pointer.write_bytes(payload)
                with self.assertRaises(ReleasePointerError):
                    load_release_pointer(self.root)

    def test_same_size_installer_corruption_is_rejected(self):
        self.installer.write_bytes(b"NZ installer")
        self.assert_invalid(self.document)

    def test_same_size_source_corruption_is_rejected(self):
        self.source.write_bytes(b"PZ source")
        self.assert_invalid(self.document)

    def test_cached_digest_rechecks_same_size_rewrite_with_restored_mtime(self):
        self.write()
        load_release_pointer(self.root)
        before = self.source.stat()
        self.source.write_bytes(b"PZ source")
        os.utime(
            self.source,
            ns=(before.st_atime_ns, before.st_mtime_ns),
        )
        with self.assertRaisesRegex(ReleasePointerError, "SHA-256"):
            load_release_pointer(self.root)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_artifact_symlinks_are_not_regular_siblings(self):
        target = self.root / "real.exe"
        target.write_bytes(self.installer.read_bytes())
        self.installer.unlink()
        try:
            self.installer.symlink_to(target)
        except OSError as error:
            self.skipTest("symlink creation unavailable: " + str(error))
        self.assert_invalid(self.document)

    def test_immutable_lookup_accepts_only_safe_canonical_regular_files(self):
        self.assertEqual(
            self.installer,
            immutable_artifact(self.root, self.VERSION, installer=True),
        )
        self.assertEqual(
            self.source,
            immutable_artifact(self.root, self.VERSION, installer=False),
        )
        for value in ("0.5.13.0", "00.5.13", "../0.5.13", "0.5.14"):
            with self.subTest(value=value):
                self.assertIsNone(
                    immutable_artifact(self.root, value, installer=True)
                )


if __name__ == "__main__":
    unittest.main()
