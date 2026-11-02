"""The host installer owns its payload and retains private state separately."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
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
    def test_host_version_is_independent_of_member_release(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "Hosting").mkdir()
            (root / "GuildHub").mkdir()
            (root / "GuildHub/version.py").write_text('CLIENT_VERSION = "9.9.9"\n', encoding="ascii")
            with self.assertRaises(installer.HostInstallerError):
                installer.source_version(root)
            (root / "Hosting/version.py").write_text('HOST_VERSION = "0.5.14"\n', encoding="ascii")
            self.assertEqual("0.5.14", installer.source_version(root))

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

    def test_installer_bundles_the_pinned_speech_engine_at_the_agreed_path(self):
        script = (ROOT / installer.INSTALLER_SOURCE).read_text(encoding="ascii")
        files = script.split("[Files]", 1)[1].split("\n[", 1)[0]
        self.assertIn(
            'Source: "{#StageRoot}\\speech-engine\\*"; DestDir: "{app}\\speech-engine"; '
            "Flags: ignoreversion nocompression", files)
        uninstall_deletes = script.split("[UninstallDelete]", 1)[1].split("[Code]", 1)[0]
        self.assertIn('Name: "{app}\\speech-engine"; Check: UninstallTargetValidated', uninstall_deletes)
        lock = json.loads((ROOT / "Hosting/Installer/assets.lock.json").read_text(encoding="ascii"))
        self.assertEqual(installer.SPEECH_ENGINE_PATH, lock["whisper_vulkan"]["install_path"])
        self.assertEqual(installer.SPEECH_ENGINE_SOURCE, lock["whisper_vulkan"]["local_path"])
        self.assertIn("--speech-engine", installer.main.__code__.co_consts)

    def test_speech_engine_staging_uses_the_staged_lock_and_refuses_bad_input_early(self):
        with tempfile.TemporaryDirectory() as folder:
            stage = Path(folder) / "stage"
            for relative in ("Hosting/assets.py", "Hosting/Installer/assets.lock.json"):
                target = stage / "app" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / relative).read_bytes())
            missing = Path(folder) / "absent.zip"
            with mock.patch("urllib.request.urlopen", side_effect=AssertionError("unexpected network")):
                with self.assertRaisesRegex(installer.HostInstallerError, "gh run download"):
                    installer.stage_speech_engine(stage, missing, Path(folder) / "cache")
                wrong = Path(folder) / "whisper-vulkan-bin-x64.zip"
                wrong.write_bytes(b"not the pinned speech engine")
                with self.assertRaisesRegex(installer.HostInstallerError, "pinned SHA-256"):
                    installer.stage_speech_engine(stage, wrong, Path(folder) / "cache")
            self.assertFalse((stage / "speech-engine").exists())

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

    def execute_preview_script(self, states):
        """Drive the actual generated launcher with a deterministic event loop."""
        witness = SimpleNamespace(now=0.0, events=[], callbacks=[], destroyed=False,
                                  visible=False, app=None)
        case = self

        class Root:
            def withdraw(self):
                witness.events.append("hidden")

            def after_idle(self, callback):
                witness.callbacks.append(callback)

            def after(self, delay, callback):
                case.assertGreater(delay, 0, "Readiness retries must yield to the event loop")
                witness.callbacks.append(callback)

            def deiconify(self):
                witness.visible = True
                witness.events.append(("visible", witness.app.busy, witness.app.page, witness.now))

            def destroy(self):
                witness.destroyed = True
                witness.events.append("destroyed")

            def mainloop(self):
                for now, busy, page in states:
                    case.assertTrue(witness.callbacks, "Readiness observation stopped too early")
                    witness.now = now
                    witness.app.busy, witness.app.page = busy, page
                    witness.callbacks.pop(0)()
                    if witness.visible or witness.destroyed:
                        return
                case.fail("Preview neither became ready nor reached its bounded timeout")

        root = Root()
        preview_controller = object()

        def create_wizard(actual_root, controller, *, open_url):
            self.assertIs(root, actual_root)
            self.assertIs(preview_controller, controller)
            self.assertEqual(["hidden"], witness.events, "Hide before constructing the asynchronous wizard")
            self.assertIsNone(open_url("https://example.org/preview-only"))
            witness.app = SimpleNamespace(busy=True, page="progress")
            return witness.app

        package = ModuleType("Hosting")
        package.wizard = SimpleNamespace(tk=SimpleNamespace(Tk=lambda: root),
            HostWizard=create_wizard, PreviewController=lambda: preview_controller)
        clock = ModuleType("time")
        clock.monotonic = lambda: witness.now
        with mock.patch.dict(sys.modules, {"Hosting": package, "time": clock}):
            try:
                exec(installer.smoke_preview_script(), {})
            except SystemExit as error:
                witness.exit_code = error.code
            else:
                witness.exit_code = 0
        return witness

    def test_smoke_preview_remains_hidden_until_initial_job_and_choice_page_are_ready(self):
        witness = self.execute_preview_script((
            (0.1, True, "progress"),
            (0.8, True, "choose-host"),
            (1.1, False, "progress"),
            (1.2, False, "choose-host"),
        ))
        self.assertEqual(["hidden", ("visible", False, "choose-host", 1.2)], witness.events)
        self.assertFalse(witness.destroyed)
        self.assertEqual(0, witness.exit_code)

    def test_smoke_preview_timeout_never_exposes_or_claims_a_ready_window(self):
        for busy, page in ((True, "choose-host"), (False, "progress")):
            with self.subTest(busy=busy, page=page):
                witness = self.execute_preview_script(((0.1, busy, page), (19.9, busy, page), (20.0, busy, page)))
                self.assertEqual(["hidden", "destroyed"], witness.events)
                self.assertFalse(witness.visible)
                self.assertTrue(witness.destroyed)
                self.assertEqual(3, witness.exit_code)

    def test_external_close_runs_only_after_the_exact_preview_reports_readiness(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder).resolve()
            helper = destination / "app/Hosting/Close-Host.ps1"
            helper.parent.mkdir(parents=True)
            helper.write_text("# synthetic close helper", encoding="ascii")
            for ready_code in (0, 3):
                with self.subTest(ready_code=ready_code):
                    process = mock.Mock(pid=4321)
                    process.wait.return_value = 0
                    process.poll.return_value = 0 if ready_code == 0 else None
                    events = []

                    def run(arguments, **options):
                        if "-Command" in arguments:
                            events.append("readiness")
                            self.assertEqual("4321", options["env"]["XENO_HOST_SMOKE_PID"])
                            return subprocess.CompletedProcess(arguments, ready_code, "", "")
                        self.assertEqual(["readiness"], events)
                        self.assertEqual(0, ready_code, "A failed readiness probe must not send a close request")
                        self.assertIn(str(helper), arguments)
                        self.assertIn(str(destination / "runtime/pythonw.exe"), arguments)
                        events.append("close")
                        return subprocess.CompletedProcess(arguments, 0, "", "")

                    with mock.patch.object(installer.subprocess, "Popen", return_value=process) as launch, \
                         mock.patch.object(installer.subprocess, "run", side_effect=run):
                        if ready_code:
                            with self.assertRaisesRegex(installer.HostInstallerError, "did not open"):
                                installer.smoke_wizard(destination)
                        else:
                            installer.smoke_wizard(destination)
                    command = launch.call_args.args[0]
                    self.assertEqual(str(destination / "runtime/pythonw.exe"), command[0])
                    self.assertEqual(installer.smoke_preview_script(), command[-1])
                    self.assertIn("-c", command)
                    self.assertEqual(["readiness", "close"] if ready_code == 0 else ["readiness"], events)
                    if ready_code:
                        process.terminate.assert_called_once_with()
                    else:
                        process.terminate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
