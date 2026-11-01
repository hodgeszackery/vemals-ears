"""The host installer owns its payload and retains private state separately."""

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("build_host_installer", ROOT / "packaging/build_host_installer.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)

FIXTURE_FILES = (
    "LICENSE", "Hosting/wizard.py", "Hosting/control.py", "Hosting/assets.py",
    "Hosting/Installer/VemalsEarsHost.iss", "Windows/VemalsEars.ico",
)


class HostInstallerTests(unittest.TestCase):
    def source(self, root):
        for relative in FIXTURE_FILES:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("source fixture: " + relative + "\n", encoding="ascii")

    def test_stage_is_exact_source_complete_and_contains_no_private_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "source"
            self.source(root)
            (root / "Hosting/relay.env").write_text("XENO_PRIVATE_CONFIG", encoding="ascii")
            stage = Path(folder) / "stage"
            with mock.patch.object(installer.build_host, "HOST_FILES", FIXTURE_FILES):
                marker = installer.stage_source(root, stage, "1.2.3", test_build=True)
                archived = installer.build_host.verify_archive(
                    (stage / "app/Source/VemalsEars-Host.zip").read_bytes())
            self.assertEqual(set(FIXTURE_FILES), set(archived))
            self.assertEqual("vemals-ears-host", marker["product"])
            self.assertTrue(marker["test_build"])
            self.assertEqual("1.2.3", marker["version"])
            self.assertFalse((stage / "app/Hosting/relay.env").exists())
            for relative, data in archived.items():
                self.assertEqual(data, (stage / "app" / relative).read_bytes())

    def test_existing_stage_and_invalid_versions_fail_without_replacing_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "source"
            self.source(root)
            stage = Path(folder) / "stage"
            stage.mkdir()
            sentinel = stage / "keep.txt"
            sentinel.write_text("keep", encoding="ascii")
            with mock.patch.object(installer.build_host, "HOST_FILES", FIXTURE_FILES):
                with self.assertRaises(installer.HostInstallerError):
                    installer.stage_source(root, stage, "1.2.3", test_build=True)
            self.assertEqual("keep", sentinel.read_text())
            for version in ("1.2", "1.2.3/4", "65536.0.0", "1.2.3\n", "1.2.3.4.5"):
                with self.subTest(version=version), self.assertRaises(installer.HostInstallerError):
                    installer.checked_version(version)

    def test_modified_staged_source_fails_before_private_runtime_execution(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "source"
            self.source(root)
            stage = Path(folder) / "stage"
            with mock.patch.object(installer.build_host, "HOST_FILES", FIXTURE_FILES):
                installer.stage_source(root, stage, "1.2.3", test_build=False)
                (stage / "app/Hosting/control.py").write_text("changed", encoding="ascii")
                with mock.patch.object(installer.subprocess, "run") as run:
                    with self.assertRaisesRegex(installer.HostInstallerError, "differs"):
                        installer.verify_stage(stage)
                    run.assert_not_called()

    def test_compiler_receipt_identifies_test_artifact_and_uses_staged_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            stage = root / "stage"
            output = root / "out"
            stage.mkdir()
            (stage / "app/Installer").mkdir(parents=True)
            (stage / "app/Installer/runtime-lock.json").write_text(json.dumps({
                "schema": 1, "architecture": "x64", "inno_setup": {"version": "7.1.0"},
            }), encoding="ascii")
            (stage / "host-install.json").write_text(json.dumps({
                "schema": 1, "product": "vemals-ears-host", "version": "1.2.3", "test_build": True,
            }), encoding="ascii")
            commands = []

            def run(command, **kwargs):
                commands.append(command)
                if command[-1] == "--version":
                    return subprocess.CompletedProcess(command, 0, "7.1.0\n", "")
                (output / (installer.TEST_OUTPUT_NAME + ".exe")).write_bytes(b"test installer")
                return subprocess.CompletedProcess(command, 0, "compiled", "")

            with mock.patch.object(installer.subprocess, "run", side_effect=run):
                target = installer.compile_installer(stage, output, root / "ISCC.exe")
            self.assertEqual(stage / "app" / installer.INSTALLER_SOURCE, Path(commands[1][-1]))
            self.assertIn("/DTestBuild=1", commands[1])
            receipt = json.loads(target.with_suffix(".json").read_text())
            self.assertTrue(receipt["test_build"])
            self.assertFalse(receipt["signed"])
            self.assertEqual(len(b"test installer"), receipt["bytes"])

    def test_installer_has_distinct_identity_and_never_removes_programdata_state(self):
        script = (ROOT / installer.INSTALLER_SOURCE).read_text(encoding="ascii")
        member_script = (ROOT / "Installer/VemalsEars.iss").read_text(encoding="ascii")
        import re
        host_ids = set(re.findall(r"(?m)^AppId=(.+)$", script))
        member_ids = set(re.findall(r"(?m)^AppId=(.+)$", member_script))
        self.assertEqual(2, len(host_ids))
        self.assertFalse(host_ids & member_ids)
        uninstall_deletes = script.split("[UninstallDelete]", 1)[1].split("[Code]", 1)[0]
        self.assertNotIn("commonappdata", uninstall_deletes)
        self.assertIn("installer-stop", script)
        self.assertIn("installer-remove", script)
        self.assertIn("PrivilegesRequired=admin", script)
        self.assertIn("VemalsEarsHost-Installer-Test-", script)
        self.assertNotIn("Install-Host.ps1", script)

    def test_install_smoke_refuses_production_artifacts_before_execution(self):
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / (installer.OUTPUT_NAME + ".exe")
            executable.write_bytes(b"production artifact")
            executable.with_suffix(".json").write_text(json.dumps({
                "schema": 1, "product": "vemals-ears-host", "test_build": False,
            }), encoding="ascii")
            with mock.patch.object(installer.subprocess, "run") as run:
                with self.assertRaisesRegex(installer.HostInstallerError, "TestBuild"):
                    installer.smoke_installer(executable)
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
