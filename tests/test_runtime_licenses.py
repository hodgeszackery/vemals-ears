"""Runtime size pruning must retain complete upstream license artifacts."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging"))
import build_runtime
import runtime_licenses


class RuntimeLicenseTests(unittest.TestCase):
    def setUp(self):
        self.lock = json.loads((ROOT / "Installer/runtime-lock.json").read_text(encoding="utf-8"))

    def stage_notices(self, runtime):
        for relative in runtime_licenses.required_license_paths(self.lock):
            path = runtime / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"upstream license fixture\r\n")

    def test_real_prune_preserves_exact_incorporated_notice_and_wheel_licenses(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder)
            self.stage_notices(runtime)
            source = runtime / runtime_licenses.INCORPORATED_SOURCE
            source.parent.mkdir(parents=True)
            original = b"Copyright \xc2\xa9 upstream\r\n\r\nOriginal terms.\n"
            source.write_bytes(original)
            (runtime / "Doc/unused.html").write_bytes(b"large documentation")
            runtime_licenses.preserve_incorporated_notice(runtime)
            build_runtime.prune(runtime)
            self.assertFalse((runtime / "Doc").exists())
            self.assertEqual(original, (runtime / runtime_licenses.INCORPORATED_NOTICE).read_bytes())
            runtime_licenses.validate_runtime_licenses(runtime, self.lock)

    def test_missing_or_empty_component_license_stops_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder)
            self.stage_notices(runtime)
            for relative in runtime_licenses.required_license_paths(self.lock):
                with self.subTest(relative=relative):
                    path = runtime / relative
                    original = path.read_bytes()
                    path.unlink()
                    with self.assertRaisesRegex(SystemExit, "missing or empty"):
                        runtime_licenses.validate_runtime_licenses(runtime, self.lock)
                    path.write_bytes(b" \r\n")
                    with self.assertRaisesRegex(SystemExit, "missing or empty"):
                        runtime_licenses.validate_runtime_licenses(runtime, self.lock)
                    path.write_bytes(original)

    def test_missing_original_notice_cannot_reuse_previous_preserved_file(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder)
            self.stage_notices(runtime)
            with self.assertRaisesRegex(SystemExit, "missing or empty"):
                runtime_licenses.preserve_incorporated_notice(runtime)

    def test_cli_fails_when_upstream_notice_missing(self):
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run(
                [sys.executable, "-E", "-s", "-B", str(ROOT / "packaging/runtime_licenses.py"),
                 folder, "--preserve"], capture_output=True, text=True, timeout=15,
            )
            self.assertNotEqual(0, result.returncode)
            self.assertIn(runtime_licenses.INCORPORATED_SOURCE, result.stderr.replace("\\", "/"))

    def test_new_wheel_requires_explicit_license_inventory(self):
        with self.assertRaisesRegex(SystemExit, "audit is missing"):
            runtime_licenses.required_license_paths({"wheels": [{"name": "unreviewed", "version": "1"}]})


if __name__ == "__main__":
    unittest.main()
