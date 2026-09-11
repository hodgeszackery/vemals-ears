import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import build_release
import source_distribution

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "Installer"
sys.path.insert(0, str(ROOT / "packaging"))
import runtime_sources
# Same path build_installer.ps1 defaults to. Overridable for a machine
# that keeps it elsewhere; the compile test skips when it is absent, so
# CI without Inno stays green rather than failing on a missing tool.
ISCC = Path(
    os.environ.get("ISCC_PATH", "C:/Program Files/Inno Setup 7/ISCC.exe")
)


def load_stage_module():
    spec = importlib.util.spec_from_file_location(
        "vemals_ears_stage_installer",
        INSTALLER / "stage_installer.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_uninstall_module():
    spec = importlib.util.spec_from_file_location(
        "vemals_ears_uninstall_cleanup",
        INSTALLER / "uninstall_cleanup.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class InstallerAllowlistTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stage = load_stage_module()

    def test_member_installer_allowlist_excludes_host_and_private_state(self):
        files = set(build_release.WINDOWS_INSTALLER_APP_FILES)
        required = {
            "Vemals-Ears.pyw",
            "WoW-Companion.pyw",
            "GuildHub/client.py",
            "GuildHub/credentials.py",
            "GuildHub/updater.py",
            "GuildHub/verify_update.ps1",
            "GuildHub/windows_integration.py",
            "VoiceCaptions/member_overlay.py",
            "VoiceCaptions/overlay.py",
            "Installer/uninstall_cleanup.py",
        }
        self.assertTrue(required.issubset(files))
        self.assertNotIn("GearHelper/Tools/gearhelper_auto.py", files)
        self.assertNotIn("GearHelper/Tools/raidbots_import.py", files)
        self.assertNotIn("BossTalents/Tools/boss_talents_auto.py", files)
        self.assertNotIn("GuildHub/console.py", files)
        self.assertFalse(any(name in files for name in (
            'GuildHub/job_client.py', 'GuildHub/jobs_client.py',
            'GuildHub/job_codec.py', 'GuildHub/job_protocol.py')))

        forbidden_parts = {
            "server.py",
            "store.py",
            "host.py",
            "publisher.py",
            "Dockerfile",
            "docker-compose",
            "discord_adapter",
            "/stt/",
            "runtime_installer.py",
            ".env",
            ".sqlite",
            "node_modules",
            "Generated.lua",
            "/tests/",
        }
        for relative in files:
            normalized = "/" + relative.replace("\\", "/")
            for forbidden in forbidden_parts:
                self.assertNotIn(forbidden.casefold(), normalized.casefold(), relative)

    def test_staging_version_comes_from_ve_without_an_addon_checkout(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "GuildHub").mkdir()
            (root / "GuildHub/version.py").write_text('CLIENT_VERSION = "7.8.9"\n', encoding="ascii")
            self.assertEqual("7.8.9", self.stage._version_from_toc(root))
            with self.assertRaisesRegex(self.stage.StageError, "does not match Vemals Ears"):
                self.stage.stage_payload(root, root / "stage", "0.5.8")
            self.assertFalse((root / "stage").exists())

    def test_stage_copies_only_the_explicit_allowlist(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source"
            stage = Path(folder) / "stage"
            (source / "GuildHub").mkdir(parents=True)
            (source / "GuildHub" / "version.py").write_text(
                'CLIENT_VERSION = "1.2.3"\n',
                encoding="ascii",
            )
            (source / "allowed").mkdir()
            (source / "allowed" / "member.py").write_text("safe = True\n", encoding="ascii")
            (source / "GuildHub").mkdir(exist_ok=True)
            (source / "GuildHub" / ".env").write_text(
                "TOKEN=canary-private-secret\n",
                encoding="ascii",
            )
            (source / "GuildHub" / "host.py").write_text(
                "CANARY = 'host-secret'\n",
                encoding="ascii",
            )
            # Source packaging is independently curated, including build inputs.
            for relative in source_distribution.BUILD_FILES:
                target = source / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, target)

            with mock.patch.object(
                self.stage,
                "_load_allowlist",
                return_value=("allowed/member.py",),
            ), mock.patch.object(self.stage, "_write_icon") as icon, mock.patch.object(
                self.stage,
                "_git_output",
                return_value="a" * 40,
            ):
                manifest = self.stage.stage_payload(source, stage, "1.2.3")
                trusted_stage = Path(folder) / "trusted-stage"
                trusted_manifest = self.stage.stage_payload(
                    source,
                    trusted_stage,
                    "1.2.3",
                    trusted_publishers=("CN=Zackery Hodges", "CN=Future Publisher"),
                )

            self.assertEqual(2, icon.call_count)
            self.assertEqual(
                ["allowed/member.py", "Source/vemals-ears-source.zip", "vemals-ears-member.json"],
                [row["path"] for row in manifest["app_files"]],
            )
            self.assertEqual("member", manifest["audience"])
            self.assertNotIn("update_trust", manifest)
            self.assertEqual(
                {
                    "schema": 1,
                    "authenticode_publishers": [
                        "CN=Zackery Hodges",
                        "CN=Future Publisher",
                    ],
                },
                trusted_manifest["update_trust"],
            )
            self.assertEqual(
                trusted_manifest,
                json.loads(
                    (trusted_stage / "payload-manifest.json").read_text(encoding="ascii")
                ),
            )
            self.assertEqual(
                {"edition": "member", "schema": 1},
                json.loads(
                    (stage / "app" / "vemals-ears-member.json").read_text(
                        encoding="ascii"
                    )
                ),
            )
            staged_bytes = b"".join(
                path.read_bytes() for path in (stage / "app").rglob("*") if path.is_file()
            )
            self.assertNotIn(b"canary-private-secret", staged_bytes)
            self.assertNotIn(b"host-secret", staged_bytes)

    def test_stage_rejects_parent_traversal(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with self.assertRaises(self.stage.StageError):
                self.stage._safe_source_file(root, "../private.txt")

    def test_update_trust_rejects_ambiguous_publisher_subjects(self):
        for publishers in (
            (),
            ("",),
            (" CN=Publisher",),
            ("CN=Publisher\nOU=Injected",),
            ("CN=" + "x" * 321,),
            ("CN=Publisher", "CN=Publisher"),
            tuple("CN=Publisher " + str(index) for index in range(5)),
        ):
            with self.subTest(publishers=publishers), self.assertRaises(self.stage.StageError):
                self.stage._update_trust(publishers)

    def test_icon_staging_accepts_canonicalized_root_alias_but_rejects_resolved_escape(self):
        with tempfile.TemporaryDirectory() as folder:
            parent = Path(folder).resolve()
            root = parent / 'Long Installer Source'
            icon = root / 'Windows/VemalsEars.ico'
            icon.parent.mkdir(parents=True)
            icon.write_bytes((ROOT / 'Windows/VemalsEars.ico').read_bytes())
            alias = parent / 'LONGIN~1'
            outside = parent / 'outside.ico'
            outside.write_bytes(b'not application artwork')
            original_resolve = Path.resolve
            for escapes in (False, True):
                with self.subTest(resolved_escape=escapes):
                    def resolve(path, *args, **kwargs):
                        if path == alias:
                            return root
                        if path in (alias / 'Windows/VemalsEars.ico', icon):
                            return outside if escapes else icon
                        return original_resolve(path, *args, **kwargs)
                    target = parent / ('escaped.ico' if escapes else 'staged.ico')
                    with mock.patch.object(Path, 'resolve', resolve):
                        if escapes:
                            with self.assertRaisesRegex(self.stage.StageError, 'escapes the source tree'):
                                self.stage._write_icon(alias, target)
                            self.assertFalse(target.exists())
                        else:
                            self.stage._write_icon(alias, target)
                            self.assertEqual(icon.read_bytes(), target.read_bytes())

    def test_staging_reads_the_selected_source_manifest_despite_an_imported_checkout(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "build_release.py").write_text(
                'WINDOWS_INSTALLER_APP_FILES = ("modified/member.py",)\n', encoding="ascii")
            self.assertNotIn("modified/member.py", build_release.WINDOWS_INSTALLER_APP_FILES)
            self.assertEqual(("modified/member.py",), self.stage._load_allowlist(root))

    def test_final_installer_gate_refuses_missing_runtime_source_or_changed_app_bytes(self):
        from tests.test_runtime_sources import write_source_fixture
        import runtime_licenses
        import runtime_sources
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source'
            source_distribution.write_source_tree(
                source_distribution.snapshot(ROOT, build_release.WINDOWS_INSTALLER_APP_FILES), source)
            lock_path = source / 'Installer/runtime-lock.json'
            fixture = write_source_fixture(root / 'upstream-fixture', runtime_lock=lock_path)
            source_lock = source / 'Installer/runtime-sources.lock.json'
            source_lock.write_bytes(fixture['source_lock'].read_bytes())
            stage = root / 'stage'
            self.stage.stage_payload(source, stage, self.stage._version_from_toc(source))
            runtime = stage / 'runtime'
            for relative in runtime_licenses.required_license_paths(json.loads(lock_path.read_text())):
                target = runtime / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b'upstream license fixture\r\n')
            runtime_sources.stage_runtime_sources(runtime, fixture['cache'], source_lock=source_lock,
                                                  runtime_lock=lock_path, offline=True)
            command = [sys.executable, '-E', '-s', '-B', str(stage / 'source/source_distribution.py'),
                       '--verify-stage', str(stage)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            notice = runtime / 'Source/README.txt'
            original = notice.read_bytes()
            notice.unlink()
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(0, result.returncode)
            self.assertIn('source inventory mismatch', result.stderr)
            notice.write_bytes(original)
            (stage / 'app/Vemals-Ears.pyw').write_bytes(b'# mismatched staged application\n')
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(0, result.returncode)
            self.assertIn('payload differs', result.stderr)

    def test_installer_stages_the_distinct_ve_icon_without_reencoding(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "VemalsEars.ico"
            self.stage._write_icon(ROOT, target)
            self.assertEqual((ROOT / "Windows" / "VemalsEars.ico").read_bytes(),
                             target.read_bytes())
            self.assertEqual(b"\x00\x00\x01\x00", target.read_bytes()[:4])
            self.assertGreater(int.from_bytes(target.read_bytes()[4:6], "little"), 1)

    def test_signed_stage_requires_allowlisted_files_to_be_tracked(self):
        with mock.patch.object(
            self.stage,
            "_git_output",
            return_value="allowed/member.py",
        ) as git_output:
            self.stage.require_tracked_file(Path("C:/source"), "allowed/member.py")
        git_output.assert_called_once_with(
            Path("C:/source"),
            "ls-files",
            "--error-unmatch",
            "--",
            "allowed/member.py",
        )

        with mock.patch.object(
            self.stage,
            "_git_output",
            return_value="",
        ), self.assertRaises(self.stage.StageError):
            self.stage.require_tracked_file(Path("C:/source"), "ignored/private.py")

    def test_runtime_inputs_are_https_and_sha256_locked(self):
        lock = json.loads((INSTALLER / "runtime-lock.json").read_text(encoding="ascii"))
        downloads = [lock["python"], lock["inno_setup"], lock["artifact_signing_tools"]]
        downloads.extend(lock["wheels"])
        for item in downloads:
            self.assertTrue(item["url"].startswith("https://"), item["url"])
            self.assertRegex(item["sha256"], r"^[0-9a-f]{64}$")


class InstallerPolicyTests(unittest.TestCase):
    def test_member_installer_documentation_keeps_hosted_client_zip_only(self):
        documentation = (INSTALLER / "README.md").read_text(encoding="ascii")
        self.assertIn("`downtime-lite.zip`", documentation)
        self.assertIn(
            "it is not staged or installed by this signed EXE",
            documentation,
        )

    def test_uninstall_attempts_local_cleanup_when_credential_delete_fails(self):
        cleanup = load_uninstall_module()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pair_signal = root / "VemalsEars" / "vemals-ears-paired.signal"
            overlay = root / "WoWVoiceCaptions" / "overlay-settings.json"
            pair_signal.parent.mkdir()
            overlay.parent.mkdir()
            pair_signal.write_text("paired\n", encoding="ascii")
            overlay.write_text("{}\n", encoding="ascii")
            store = mock.Mock()
            store.delete.side_effect = OSError("credential failure")
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(root)}), mock.patch(
                "GuildHub.credentials.WindowsDeviceCredentialStore",
                return_value=store,
            ):
                self.assertEqual(1, cleanup.main())
            self.assertFalse(pair_signal.exists())
            self.assertFalse(overlay.exists())

    def test_installer_powershell_scripts_parse(self):
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if powershell is None:
            self.skipTest("PowerShell is unavailable")
        parser = (
            "$errors=$null; "
            "[System.Management.Automation.Language.Parser]::ParseFile("
            "$env:INSTALLER_PARSE_FILE,[ref]$null,[ref]$errors) | Out-Null; "
            "if ($errors.Count -gt 0) { $errors | ForEach-Object { "
            "[Console]::Error.WriteLine($_.ToString()) }; exit 1 }"
        )
        for script in sorted(INSTALLER.glob("*.ps1")):
            with self.subTest(script=script.name):
                environment = os.environ.copy()
                environment["INSTALLER_PARSE_FILE"] = str(script)
                completed = subprocess.run(
                    [
                        powershell,
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        parser,
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    env=environment,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)

    def test_inno_is_per_user_and_protocol_targets_signed_launcher(self):
        source = (INSTALLER / "VemalsEars.iss").read_text(encoding="utf-8")
        self.assertIn("PrivilegesRequired=lowest", source)
        # One define names the fixed VE installation folder.
        self.assertIn(
            '#define FixedAppFolder "{localappdata}\\Programs\\VemalsEars"',
            source,
        )
        self.assertIn("DefaultDirName={#FixedAppFolder}", source)
        self.assertIn("Root: HKCU", source)
        self.assertNotIn("Root: HKLM", source)
        self.assertNotIn('Name: "{group}\\Set up Boss Talents"', source)
        self.assertNotIn('Name: "{group}\\Set up Boss Talents.lnk"', source)
        command_line = next(
            line for line in source.splitlines() if "vemalsears\\shell\\open\\command" in line
        )
        self.assertIn('#define AppExeName "VemalsEars.exe"', source)
        self.assertIn("RunValueName = 'Vemal''s Ears'", source)
        self.assertNotIn("LegacyRunValueName", source)
        self.assertIn("{#AppExeName}", command_line)
        self.assertNotIn("python", command_line.casefold())
        self.assertNotIn(".pyw", command_line.casefold())

    def test_slim_installer_omits_runtime_archives_but_keeps_exact_receipts(self):
        source = (INSTALLER / "VemalsEars.iss").read_text(encoding="ascii")
        runtime_line = next(
            line for line in source.splitlines()
            if line.startswith('Source: "{#StageRoot}\\runtime\\*";')
        )
        self.assertIn('Excludes: "Source\\*"', runtime_line)
        self.assertIn(
            'Source: "{#StageRoot}\\runtime\\Source\\MANIFEST.json"; '
            'DestDir: "{app}\\runtime\\Source";',
            source,
        )
        self.assertIn(
            'Source: "{#StageRoot}\\runtime\\Source\\README.txt"; '
            'DestDir: "{app}\\runtime\\Source";',
            source,
        )
        self.assertIn("app/docs/SOURCE.txt", runtime_sources.readme_bytes(
            runtime_sources.load_source_lock(
                runtime_sources.DEFAULT_SOURCE_LOCK,
                runtime_sources.DEFAULT_RUNTIME_LOCK,
            )
        ).decode("ascii"))

    def test_source_complete_member_uses_the_frozen_installer_snapshot(self):
        source = (INSTALLER / "build_installer.ps1").read_text(encoding="ascii")
        self.assertIn(
            '(Join-Path $stageRoot "source\\build_release.py")', source
        )
        self.assertNotIn(
            '(Join-Path $repoRoot "build_release.py")', source
        )

    def test_app_shortcut_uses_the_native_taskbar_identity_and_ear_icon(self):
        source = (INSTALLER / "VemalsEars.iss").read_text(encoding="ascii")
        shortcut = next(line for line in source.splitlines()
                        if line.startswith('Name: "{group}\\Vemal\'s Ears";'))
        self.assertIn('Filename: "{app}\\{#AppExeName}"', shortcut)
        self.assertIn('WorkingDir: "{app}"', shortcut)
        self.assertIn('IconFilename: "{app}\\{#AppExeName}"', shortcut)
        self.assertIn('AppUserModelID: "VemalsEars.Companion"', shortcut)
        self.assertNotIn("python", shortcut.casefold())
        self.assertNotIn("VemalsEars.Member", shortcut)

    @unittest.skipUnless(ISCC.is_file(), "Inno Setup 7 is not installed here")
    def test_the_installer_pascal_actually_compiles(self):
        """Every other installer test reads the .iss as text.

        Text assertions cannot tell valid Pascal from a build-breaking
        typo, and nothing else notices until someone cuts a release -
        the worst moment to find out. This one hands the real file to the
        real compiler.

        It earned its place immediately: a wrapped string beginning with
        #13#10 compiles as an unknown PREPROCESSOR directive, because
        ISPP claims any line whose first non-blank character is '#'. That
        would have failed the release build and nothing else here would
        have said a word.

        Only the sections that point at staged payload files are emptied;
        [Setup] and [Code] go to the compiler verbatim.
        """
        source = (INSTALLER / "VemalsEars.iss").read_text(encoding="utf-8")
        for name in (
            "Files", "InstallDelete", "Icons", "Registry", "UninstallRun"
        ):
            source = re.sub(
                r"^\[" + name + r"\]\n(?:(?!^\[).*\n)*",
                "[" + name + "]\n",
                source,
                flags=re.MULTILINE,
            )
        for missing in ("AppVersion", "StageRoot", "OutputDir"):
            source = source.replace(
                "#error " + missing + " must be supplied by build_installer.ps1", ""
            )

        with tempfile.TemporaryDirectory() as folder:
            stage = Path(folder)
            shutil.copy2(ROOT / "Windows" / "VemalsEars.ico", stage / "VemalsEars.ico")
            script = stage / "compile-check.iss"
            script.write_text(source, encoding="utf-8")
            result = subprocess.run(
                [
                    str(ISCC),
                    "/DAppVersion=0.0.0",
                    "/DStageRoot=" + str(stage),
                    "/DOutputDir=" + str(stage / "Output"),
                    str(script),
                ],
                capture_output=True,
                text=True,
                timeout=300,
            )

        if result.returncode != 0:
            output = (result.stdout or "") + (result.stderr or "")
            lines = output.splitlines()
            errors = [
                line for index, line in enumerate(lines)
                if "Error" in line or "aborted" in line
                or (index > 0 and "Error" in lines[index - 1])
            ]
            self.fail(
                "VemalsEars.iss does not compile:\n  " + "\n  ".join(errors)
            )

    def test_nothing_reachable_from_startup_touches_the_app_constant(self):
        """InitializeSetup runs before the wizard initializes app paths."""
        source = (INSTALLER / "VemalsEars.iss").read_text(encoding="utf-8")
        start = source.index("function InitializeSetup")
        end = source.index("procedure RemoveOwnedRegistryValues")
        startup_region = source[start:end]
        self.assertNotIn(
            "'{app}", startup_region,
            "the app constant is back in code reachable from "
            "InitializeSetup, where expanding it is a startup crash",
        )

    def test_installer_always_allows_downtime_alongside_vemals_ears(self):
        source = (INSTALLER / "VemalsEars.iss").read_text(encoding="utf-8")
        self.assertNotIn("HigherTierBlocksThisInstall", source)
        self.assertNotIn("DOWNTIME_DEV_COEXIST", source)
        self.assertNotIn("RegisteredCompanionFolder", source)
        # Setup may read the old shared lock to identify an installed VE
        # process, but must never signal the shared Downtime quit file.
        self.assertNotIn("{localappdata}\\GearHelper\\companion-quit.request", source)
        self.assertIn("{localappdata}\\VemalsEars\\companion-window.lock", source)
        self.assertIn("{localappdata}\\VemalsEars\\companion-quit.request", source)

    def test_legacy_upgrade_close_is_targeted_and_setup_only(self):
        source = (INSTALLER / "VemalsEars.iss").read_text(encoding="ascii")
        start = source.index("function SetupLegacyProcessIsOurs")
        stop = source.index("procedure SetupCloseRunningCompanion();", start)
        fallback = source[start:stop]
        self.assertIn("{app}\\VemalsEars.exe", fallback)
        self.assertIn("{app}\\runtime\\pythonw.exe", fallback)
        self.assertIn("SameText(Buffer,", fallback)
        self.assertNotIn("CompareText(Copy", fallback)
        self.assertIn("SetupPostMessage(Window, $0111, 1024, 0)", fallback)
        self.assertNotIn("SaveStringToFile", fallback)
        self.assertNotIn("DeleteFile", fallback)
        self.assertNotIn("taskkill", fallback)
        self.assertEqual(fallback.count("SetupLegacyProcessIsOurs(ProcessHandle)"), 2)
        self.assertEqual(fallback.count("LoadStringFromFile(LockPath, LockText)"), 2)
        self.assertLess(fallback.index("SetupOpenProcess"), fallback.index("SetupPostMessage"))
        self.assertGreater(fallback.index("SetupCloseHandle"), fallback.index("SetupPostMessage"))
        uninstall = source[source.index("procedure CloseRunningCompanion();"):]
        self.assertNotIn("SetupCloseLegacyCompanion", uninstall)
        self.assertNotIn("{localappdata}\\GearHelper", uninstall)
        current = source[stop:source.index("function PrepareToInstall", stop)]
        self.assertLess(current.index("DeleteFile(QuitRequest)"),
                        current.index("SetupCloseLegacyCompanion();"))

    @unittest.skipUnless(ISCC.is_file(), "Inno Setup 7 is not installed here")
    def test_compiled_legacy_close_refuses_ambiguous_owners(self):
        """Execute the real Pascal helpers with isolated OS/file test doubles.

        This is an initialization-only compiled probe: no payload, registry,
        live process APIs, services, app state, or credential access. It exits
        before installation and writes its receipt only in a temporary folder.
        """
        source = (INSTALLER / "VemalsEars.iss").read_text(encoding="ascii")
        start = source.index("function SetupLegacyProcessIsOurs")
        stop = source.index("procedure SetupCloseRunningCompanion();", start)
        helpers = source[start:stop]
        for name in ("ExpandConstant", "LoadStringFromFile", "CheckForMutexes", "Sleep"):
            helpers = helpers.replace(name + "(", "Probe" + name + "(")
        # These functions are our only execution surface. No native external
        # declarations or shared-path writes can leak into this harness.
        self.assertNotIn("external ", helpers)
        self.assertNotIn("SaveStringToFile", helpers)
        doubles = textwrap.dedent(r"""
            var
              Scenario, LockReads, ImageReads, PidReads, Finds: Integer;
              Opens, Closes, Posts, Sleeps: Integer;
              Failures: String;

            procedure Verify(Condition: Boolean; Detail: String);
            begin
              if not Condition then
                Failures := Failures + IntToStr(Scenario) + ': ' + Detail + #13#10;
            end;

            function ProbeExpandConstant(Value: String): String;
            begin
              StringChangeEx(Value, '{app}', 'C:\isolated\VemalsEars', True);
              StringChangeEx(Value, '{localappdata}', 'C:\isolated\state', True);
              Result := Value;
            end;

            function ProbeLoadStringFromFile(Path: String; var Value: AnsiString): Boolean;
            begin
              LockReads := LockReads + 1;
              Verify(Path = 'C:\isolated\state\GearHelper\companion-window.lock', 'unexpected read');
              Value := '4242';
              if Scenario = 4 then Value := 'not-a-pid';
              if Scenario = 19 then Value := '-1';
              if Scenario = 21 then Value := '';
              if (Scenario = 11) and (LockReads > 1) then Value := '9000';
              Result := (Scenario <> 3) and not ((Scenario = 20) and (LockReads > 1));
            end;

            function ProbeCheckForMutexes(Name: String): Boolean;
            begin
              Result := (Posts = 0) or (Scenario = 15);
            end;

            procedure ProbeSleep(Delay: Integer);
            begin
              Verify(Delay = 250, 'unexpected wait');
              Sleeps := Sleeps + 1;
            end;

            function SetupOpenProcess(Access: DWORD; Inherit: BOOL; Pid: DWORD): THandle;
            begin
              Verify((Access = $1000) and not Boolean(Inherit) and (Pid = 4242), 'unexpected process open');
              Opens := Opens + 1;
              Result := 123;
              if Scenario = 2 then Result := 0;
            end;

            function SetupCloseHandle(Handle: THandle): BOOL;
            begin
              Verify(Handle = 123, 'unexpected handle close');
              Closes := Closes + 1;
              Result := True;
            end;

            function SetupGetExitCode(Handle: THandle; var Code: DWORD): BOOL;
            begin
              Verify(Handle = 123, 'unexpected exit-code handle');
              Code := 259;
              if (Scenario = 10) or ((Scenario = 24) and (ImageReads > 0)) then Code := 0;
              Result := Scenario <> 17;
            end;

            function SetupQueryImageName(Handle: THandle; Flags: DWORD;
              var Value: String; var Size: DWORD): BOOL;
            begin
              Verify((Handle = 123) and (Flags = 0), 'unexpected image query');
              ImageReads := ImageReads + 1;
              Value := 'C:\isolated\VemalsEars\runtime\pythonw.exe';
              if Scenario = 1 then Value := 'C:\isolated\VemalsEars\VemalsEars.exe';
              if (Scenario = 5) or ((Scenario = 13) and (ImageReads > 1)) then
                Value := 'C:\isolated\Downtime\runtime\pythonw.exe';
              if Scenario = 6 then Value := 'C:\isolated\VemalsEars-other\runtime\pythonw.exe';
              if Scenario = 7 then Value := 'C:\isolated\VemalsEars\app\helper.exe';
              if Scenario = 23 then Value := 'C:\isolated\VemalsEars\runtime\python.exe';
              Size := Length(Value);
              Result := Scenario <> 18;
            end;

            function SetupFindWindowAfter(Parent: THandle; After: THandle;
              ClassName: String; WindowName: Integer): THandle;
            begin
              Verify((Parent = 0) and (ClassName = 'DowntimeCompanionTray') and
                (WindowName = 0), 'unexpected tray search');
              Finds := Finds + 1;
              Result := 0;
              if Scenario = 8 then Exit;
              if Scenario = 22 then begin Result := Finds; Exit; end;
              if (Scenario = 16) and (After = 0) then begin Result := 100; Exit; end;
              if After <> 200 then Result := 200;
            end;

            function SetupWindowProcessId(Window: THandle; var Pid: DWORD): DWORD;
            begin
              PidReads := PidReads + 1;
              Pid := 4242;
              if (Window = 100) or (Scenario = 9) or (Scenario = 22) or
                ((Scenario = 12) and (PidReads > 1)) then Pid := 9000;
              Result := 1;
            end;

            function SetupPostMessage(Window: THandle; Msg: Cardinal;
              WParam: Integer; LParam: Integer): BOOL;
            begin
              Verify((Window = 200) and (Msg = $0111) and (WParam = 1024) and
                (LParam = 0), 'unexpected message target');
              Verify((Opens = 1) and (Closes = 0) and (LockReads = 2) and
                (ImageReads = 2), 'owner was not held and rechecked');
              Posts := Posts + 1;
              Result := Scenario <> 14;
            end;
        """)
        checks = textwrap.dedent(r"""
            function InitializeSetup(): Boolean;
            var ExpectedPosts: Integer;
            begin
              Failures := '';
              for Scenario := 0 to 24 do
              begin
                LockReads := 0; ImageReads := 0; PidReads := 0; Finds := 0;
                Opens := 0; Closes := 0; Posts := 0; Sleeps := 0;
                SetupCloseLegacyCompanion();
                ExpectedPosts := 0;
                if (Scenario = 0) or (Scenario = 1) or (Scenario = 14) or
                  (Scenario = 15) or (Scenario = 16) then ExpectedPosts := 1;
                Verify(Posts = ExpectedPosts, 'wrong message count');
                if Scenario = 2 then Verify(Closes = 0, 'closed failed handle')
                else Verify(Closes = Opens, 'handle leak');
                Verify(Finds <= 64, 'unbounded enumeration');
                if Scenario = 15 then Verify(Sleeps = 48, 'timeout was not bounded')
                else Verify(Sleeps = 0, 'unexpected wait');
              end;
              if Failures = '' then Failures := '25 scenarios passed';
              SaveStringToFile('__RECEIPT__', AnsiString(Failures), False);
              Result := False;
            end;
        """)
        with tempfile.TemporaryDirectory(prefix="ve-legacy-close-probe-") as folder:
            stage = Path(folder)
            receipt = stage / "receipt.txt"
            probe = stage / "legacy-close-probe.iss"
            setup = (
                '#define AppMutexName "Local\\VE.LegacyClose.Test"\n'
                "[Setup]\nAppName=VE legacy close test\nAppVersion=0.0.0\n"
                "DefaultDirName=" + str(stage / "unused") + "\n"
                "CreateAppDir=no\nUninstallable=no\nCreateUninstallRegKey=no\n"
                "PrivilegesRequired=lowest\nOutputBaseFilename=legacy-close-probe\n"
                "OutputDir=" + str(stage) + "\nCompression=none\n[Code]\n"
            )
            probe.write_text(
                setup + doubles + helpers + checks.replace("__RECEIPT__", str(receipt).replace("'", "''")),
                encoding="ascii",
            )
            compiled = subprocess.run([str(ISCC), str(probe)], capture_output=True,
                                      text=True, timeout=60)
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            executed = subprocess.run(
                [str(stage / "legacy-close-probe.exe"), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
                capture_output=True, text=True, timeout=30,
            )
            self.assertTrue(receipt.is_file(), "compiled probe produced no receipt: " + str(executed.returncode))
            self.assertEqual(receipt.read_text(encoding="ascii"), "25 scenarios passed")

    def test_uninstall_aborts_before_removing_lock_when_voice_still_runs(self):
        source = (INSTALLER / "VemalsEars.iss").read_text(encoding="ascii")
        start = source.index("procedure CloseRunningCompanion();")
        stop = source.index("procedure CurUninstallStepChanged", start)
        close = source[start:stop]
        recheck = close.rindex("if CheckForMutexes('{#AppMutexName}') then")
        abort = close.index("Abort;", recheck)
        delete_lock = close.index("DeleteFile(LockPath)")
        self.assertLess(recheck, abort)
        self.assertLess(abort, delete_lock)
        self.assertIn("any older copy", close)
        self.assertIn("SuppressibleMsgBox", close)
        self.assertNotIn("{localappdata}\\GearHelper", close)
        event = source[stop:]
        self.assertIn("CurUninstallStep = usAppMutexCheck", event)
        self.assertIn("CloseRunningCompanion();", event)

    def test_member_launchers_select_the_explicit_voice_entrypoint(self):
        batch = build_release.member_launcher()
        self.assertIn('"%~dp0Vemals-Ears.pyw"', batch)
        self.assertNotIn('"%~dp0WoW-Companion.pyw"', batch)
        native = (INSTALLER / "launcher.c").read_text(encoding="ascii")
        self.assertIn("#define VEMALS_EARS", native)
        self.assertIn('#include "../Windows/DowntimeLauncher.c"', native)
        host = (ROOT / "Windows" / "DowntimeLauncher.c").read_text(encoding="ascii")
        self.assertIn('app\\\\Vemals-Ears.pyw', host)
        self.assertNotIn('app\\\\WoW-Companion.pyw', host)

    def test_uninstall_preserves_downtime_state(self):
        cleanup = load_uninstall_module()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            downtime = root / "GearHelper"
            ve = root / "VemalsEars"
            downtime.mkdir()
            ve.mkdir()
            filenames = (
                "vemals-ears-paired.signal", "companion-window.lock",
                "companion-quit.request", "companion-settings.json",
            )
            for name in filenames:
                (downtime / name).write_text("retain", encoding="ascii")
            for name in filenames[:3]:
                (ve / name).write_text("remove", encoding="ascii")
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(root)}), mock.patch(
                "GuildHub.credentials.WindowsDeviceCredentialStore"
            ) as store:
                self.assertEqual(0, cleanup.main())
            store.return_value.delete.assert_called_once_with()
            for name in filenames:
                self.assertEqual("retain", (downtime / name).read_text(encoding="ascii"))
            for name in filenames[:3]:
                self.assertFalse((ve / name).exists())


    def test_setup_closes_the_companion_instead_of_prompting_for_it(self):
        """Why AppMutex must stay out of [Setup].

        Inno checks AppMutex at startup - before the wizard, and long
        before PrepareToInstall, where SetupCloseRunningCompanion lives.
        Declaring it meant the member was told to "close all instances"
        by the very program about to close the companion for them, and
        the update stopped there. Re-adding the directive would restore
        the bug in full while every other test still passed, so its
        absence is pinned here.

        Measured against Inno 7 rather than reasoned about:
        usAppMutexCheck still fires for the uninstaller with no AppMutex
        declared, so CloseRunningCompanion stays wired.
        """
        source = (INSTALLER / "VemalsEars.iss").read_text(encoding="utf-8")

        for line in source.splitlines():
            self.assertFalse(
                line.strip().startswith("AppMutex="),
                "AppMutex in [Setup] makes Setup prompt at startup, before "
                "PrepareToInstall can close the companion: " + line,
            )
        # The name itself must survive - both close routines check it.
        self.assertIn('#define AppMutexName "Local\\VemalsEars.Companion"', source)

        prepare = source[source.index("function PrepareToInstall"):]
        prepare = prepare[: prepare.index("\nend;")]
        close_at = prepare.index("SetupCloseRunningCompanion();")
        recheck_at = prepare.index("CheckForMutexes('{#AppMutexName}')")
        # Close first, then refuse only if the close actually failed.
        # Refusing first would be the old prompt wearing a new message.
        self.assertLess(close_at, recheck_at)
        self.assertIn("still running and could not be closed", prepare)
        self.assertIn("run this installer again", prepare)
        # PrepareToInstall runs before the first file is written, so
        # refusing here cannot leave a half-installed copy.
        self.assertIn("PathIsSafeForPayloadCleanup(WizardDirValue)", prepare)

    def test_silent_auto_update_is_the_only_unattended_relaunch_path(self):
        source = (INSTALLER / "VemalsEars.iss").read_text(encoding="ascii")
        run = source[source.index("[Run]"):source.index("[UninstallRun]")]
        launch_lines = [line for line in run.splitlines() if line.startswith("Filename:")]
        self.assertEqual(2, len(launch_lines))
        interactive, automatic = launch_lines
        self.assertIn("postinstall skipifsilent", interactive)
        self.assertNotIn("Check:", interactive)
        self.assertIn("Check: IsAutomaticUpdateInvocation", automatic)
        self.assertNotIn("postinstall", automatic)
        self.assertEqual(1, source.count("'/VEAUTOUPDATE'"))
        check = source[
            source.index("function IsAutomaticUpdateInvocation"):
            source.index("function PathIsSafeForPayloadCleanup")
        ]
        self.assertIn("if not WizardSilent then", check)
        self.assertIn("ParamStr(Index)", check)
        self.assertNotIn("SetupCloseRunningCompanion", check)

        build = (INSTALLER / "build_installer.ps1").read_text(encoding="ascii")
        self.assertIn('if ($Signed -and -not $AllowUntrustedSigner)', build)
        self.assertIn('$stageArguments += @("--trusted-publisher", $ExpectedPublisher)', build)
        self.assertIn('$stageArguments += @("--trusted-publisher", $publisher)', build)
        self.assertIn("The official signed payload is missing its update publisher trust", build)
        self.assertIn("An unsigned or proof payload must not authorize", build)

    @unittest.skipUnless(ISCC.is_file(), "Inno Setup 7 is not installed here")
    def test_compiled_auto_update_switch_gates_the_silent_relaunch(self):
        source = (INSTALLER / "VemalsEars.iss").read_text(encoding="ascii")
        check = source[
            source.index("function IsAutomaticUpdateInvocation"):
            source.index("function PathIsSafeForPayloadCleanup")
        ]
        with tempfile.TemporaryDirectory(prefix="ve-update-switch-") as folder:
            stage = Path(folder)
            receipt = stage / "launches.txt"
            probe = stage / "update-switch-probe.iss"
            probe.write_text(
                textwrap.dedent(
                    r'''
                    [Setup]
                    AppName=VE update switch probe
                    AppVersion=0.0.0
                    DefaultDirName={tmp}\VE-update-switch-probe
                    CreateAppDir=no
                    Uninstallable=no
                    CreateUninstallRegKey=no
                    PrivilegesRequired=lowest
                    OutputBaseFilename=update-switch-probe
                    OutputDir=__OUTPUT__
                    Compression=none

                    [Run]
                    Filename: "{cmd}"; Parameters: "/D /C echo launched>>""__RECEIPT__"""; Flags: waituntilterminated; Check: IsAutomaticUpdateInvocation

                    [Code]
                    '''
                ).replace("__OUTPUT__", str(stage)).replace("__RECEIPT__", str(receipt))
                + check,
                encoding="ascii",
            )
            compiled = subprocess.run(
                [str(ISCC), str(probe)], capture_output=True, text=True, timeout=60
            )
            self.assertEqual(0, compiled.returncode, compiled.stdout + compiled.stderr)
            installer = stage / "update-switch-probe.exe"
            quiet = [str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"]
            ordinary = subprocess.run(quiet, capture_output=True, text=True, timeout=30)
            self.assertEqual(0, ordinary.returncode, ordinary.stdout + ordinary.stderr)
            self.assertFalse(receipt.exists())
            automatic = subprocess.run(
                quiet + ["/VEAUTOUPDATE"], capture_output=True, text=True, timeout=30
            )
            self.assertEqual(0, automatic.returncode, automatic.stdout + automatic.stderr)
            self.assertEqual(["launched"], receipt.read_text(encoding="ascii").splitlines())

    def test_release_signing_is_fail_closed_and_hash_is_post_sign(self):
        inno = (INSTALLER / "VemalsEars.iss").read_text(encoding="utf-8")
        build = (INSTALLER / "build_installer.ps1").read_text(encoding="utf-8")
        verify = (INSTALLER / "verify_signature.ps1").read_text(encoding="utf-8")
        self.assertIn("SignTool=artifact", inno)
        self.assertIn("SignedUninstaller=yes", inno)
        self.assertIn("signonce", inno)
        self.assertIn("TimeStamperCertificate", verify)
        self.assertIn("-cne $ExpectedPublisher", verify)
        verify_position = build.index("verify_signature.ps1")
        checksum_position = build.index("Get-Sha256", verify_position)
        self.assertGreater(checksum_position, verify_position)
        self.assertIn("PROOF BUILD signed by an untrusted certificate", build)
        self.assertIn("not an official release", build)
        self.assertIn("--no-compile", build)
        self.assertIn('Filter "direct_url.json"', build)
        self.assertIn('Filter "*.pyc"', build)
        self.assertIn('Filter "__pycache__"', build)

    def test_downgrades_and_foreign_registry_owners_are_rejected(self):
        source = (INSTALLER / "VemalsEars.iss").read_text(encoding="utf-8")
        self.assertIn("ComparePackedVersion(CurrentVersion, NewVersion) > 0", source)
        self.assertIn("if not StrToVersion(CurrentText, CurrentVersion) then", source)
        self.assertIn("SameText(CurrentValue, ExpectedValue)", source)
        self.assertIn("CurUninstallStep = usPostUninstall", source)
        self.assertNotIn("RegDeleteKeyIncludingSubkeys(HKCU, ProtocolKey)", source)
        self.assertIn('Name: "{app}\\app"; Check: InstallTargetIsSafe', source)
        self.assertIn('Name: "{app}\\runtime"; Check: InstallTargetIsSafe', source)
        self.assertIn("PathIsSafeForPayloadCleanup(WizardDirValue)", source)
        self.assertIn("{localappdata}\\Programs\\VemalsEars", source)
        self.assertNotIn("uninsdeletekey", source.casefold())

    def test_launcher_accepts_only_normal_start_or_one_pairing_uri(self):
        source = (ROOT / "Windows" / "DowntimeLauncher.c").read_text(encoding="utf-8")
        self.assertIn("argc == 1", source)
        self.assertIn("argc != 3", source)
        self.assertIn('wcscmp(argv[1], L"--vemals-ears-pair")', source)
        self.assertIn('#define APP_PAIRING_PREFIX L"vemalsears://pair?"', source)
        self.assertIn("ARRAY_LENGTH(PAIRING_URI_PREFIX) - 1", source)
        self.assertNotIn('L"vemalsears://pair?", 19', source)
        self.assertIn("MAX_PAIRING_URI", source)
        self.assertNotIn("CreateProcessW", source)
        self.assertIn("exit_code = python_main(python_argc, python_argv)", source)
        self.assertIn('ignore_environment[] = L"-E"', source)
        self.assertIn('no_user_site[] = L"-s"', source)
        self.assertIn('no_bytecode[] = L"-B"', source)
        self.assertNotIn("system(", source)
        self.assertNotIn("ShellExecute", source)


class SigningProviderTests(unittest.TestCase):
    """Signing must not be wired to the one provider that cannot be used.

    Checked 2026-09-03: Azure Artifact Signing needs a three-year-old
    organisation, and an individual's certificate arrives as an entry in
    the Windows certificate store backed by a token or cloud HSM. The
    pipeline has to serve that path with no .pfx and no password, and
    has to be provable end to end with a self-signed certificate before
    any real one is bought.
    """

    def powershell(self):
        found = shutil.which("pwsh") or shutil.which("powershell")
        if found is None:
            self.skipTest("PowerShell is unavailable")
        return found

    def run_script(self, script, *arguments):
        completed = subprocess.run(
            [
                self.powershell(), "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass",
                "-File", str(INSTALLER / script), *arguments,
            ],
            capture_output=True, text=True, timeout=120,
        )
        return completed.returncode, completed.stdout + completed.stderr

    # ---------------------------------------------------------- invariants

    def test_no_pfx_path_and_no_password_exist_anywhere(self):
        # A password on a command line is a password in a build log, and
        # since June 2023 there is no loose .pfx to point at anyway.
        for name in ("sign_file.ps1", "build_installer.ps1", "verify_signature.ps1",
                     "prove_signing.ps1"):
            source = (INSTALLER / name).read_text(encoding="utf-8")
            with self.subTest(script=name):
                self.assertNotIn("PfxPath", source)
                self.assertNotIn("Password", source)
                self.assertNotRegex(source, r'"/f"')
                self.assertNotRegex(source, r'"/p"')

    def test_the_store_provider_signs_by_thumbprint_from_the_windows_store(self):
        sign = (INSTALLER / "sign_file.ps1").read_text(encoding="utf-8")
        self.assertIn('[ValidateSet("artifact", "store")]', sign)
        self.assertIn('"/sha1", $Thumbprint', sign)
        # Both providers still timestamp by default; only an explicit
        # "none" skips it, and only a proof should ever say that.
        self.assertIn('if ($TimestampUrl -ne "none")', sign)

    def test_untrusted_root_is_never_accepted_without_a_pinned_thumbprint(self):
        verify = (INSTALLER / "verify_signature.ps1").read_text(encoding="utf-8")
        self.assertIn("-AllowUntrustedRoot requires -ExpectedThumbprint", verify)
        build = (INSTALLER / "build_installer.ps1").read_text(encoding="utf-8")
        self.assertIn("PROOF-UNTRUSTED", build)
        self.assertIn("/DProofUntrusted=1", build)

    def test_flag_validation_runs_before_any_tool_is_probed(self):
        # So a bad flag combination fails in milliseconds, and so these
        # tests can run on a machine with no Inno Setup at all.
        build = (INSTALLER / "build_installer.ps1").read_text(encoding="utf-8")
        self.assertLess(
            build.index("-AllowUntrustedSigner only means something"),
            build.index("Inno Setup 7.1.0 is required"),
        )

    def test_installer_hashing_is_independent_of_powershell_utility_cmdlets(self):
        build = (INSTALLER / "build_installer.ps1").read_text(encoding="utf-8")
        self.assertNotIn("Get-FileHash", build)
        function = re.search(r"(?ms)^function Get-Sha256 \{.*?^\}\n", build)
        self.assertIsNotNone(function)
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            payload = folder / "payload.bin"
            payload.write_bytes(b"host independent sha256 fixture\n")
            probe = folder / "hash-probe.ps1"
            probe.write_text(
                function.group(0) + "Get-Sha256 -LiteralPath $args[0]\n",
                encoding="ascii",
            )
            expected = hashlib.sha256(payload.read_bytes()).hexdigest()
            hosts = dict.fromkeys(
                found for name in ("powershell", "pwsh")
                if (found := shutil.which(name)) is not None
            )
            if not hosts:
                self.skipTest("PowerShell is unavailable")
            for host in hosts:
                with self.subTest(host=host):
                    completed = subprocess.run(
                        [host, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                         "-File", str(probe), str(payload)],
                        capture_output=True, text=True, timeout=30,
                    )
                    self.assertEqual(0, completed.returncode, completed.stderr)
                    self.assertEqual(expected, completed.stdout.strip())

    # -------------------------------------------------------- behaviour

    def test_build_refuses_a_malformed_thumbprint(self):
        with tempfile.TemporaryDirectory() as out:
            code, output = self.run_script(
                "build_installer.ps1", "-Signed", "-SigningProvider", "store",
                "-SigningThumbprint", "not-a-thumbprint",
                "-ExpectedPublisher", "CN=Anyone", "-OutputDirectory", out,
            )
        self.assertNotEqual(0, code)
        self.assertIn("40-hex-digit", output)

    def test_build_refuses_a_proof_with_the_azure_provider(self):
        with tempfile.TemporaryDirectory() as out:
            code, output = self.run_script(
                "build_installer.ps1", "-Signed", "-AllowUntrustedSigner",
                "-SigningProvider", "artifact", "-SigningMetadataPath", "x.json",
                "-ExpectedPublisher", "CN=Anyone", "-OutputDirectory", out,
            )
        self.assertNotEqual(0, code)
        self.assertIn("requires -SigningProvider store", output)

    def test_build_refuses_an_untrusted_signer_on_an_unsigned_build(self):
        with tempfile.TemporaryDirectory() as out:
            code, output = self.run_script(
                "build_installer.ps1", "-AllowUntrustedSigner", "-OutputDirectory", out,
            )
        self.assertNotEqual(0, code)
        self.assertIn("only means something for a -Signed", output)

    def test_unsigned_build_cannot_grant_a_future_publisher_update_trust(self):
        with tempfile.TemporaryDirectory() as out:
            code, output = self.run_script(
                "build_installer.ps1", "-AdditionalUpdatePublisher", "CN=Future",
                "-OutputDirectory", out,
            )
        self.assertNotEqual(0, code)
        self.assertIn("only in an official signed build", output)

    def test_the_azure_path_still_demands_its_metadata(self):
        with tempfile.TemporaryDirectory() as out:
            code, output = self.run_script(
                "build_installer.ps1", "-Signed", "-ExpectedPublisher", "CN=Anyone",
                "-OutputDirectory", out,
            )
        self.assertNotEqual(0, code)
        self.assertIn("Artifact Signing metadata file is required", output)

    def test_signed_build_rejects_a_publisher_the_updater_cannot_parse(self):
        with tempfile.TemporaryDirectory() as out:
            code, output = self.run_script(
                "build_installer.ps1", "-Signed", "-ExpectedPublisher", " CN=Anyone",
                "-OutputDirectory", out,
            )
        self.assertNotEqual(0, code)
        self.assertIn("exact certificate subject accepted by the updater", output)

    def test_verify_refuses_untrusted_mode_without_a_thumbprint(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "unsigned.bin"
            target.write_bytes(b"MZ" + b"\0" * 64)
            code, output = self.run_script(
                "verify_signature.ps1", "-FilePath", str(target),
                "-ExpectedPublisher", "CN=Anyone", "-AllowUntrustedRoot",
            )
        self.assertNotEqual(0, code)
        self.assertIn("requires -ExpectedThumbprint", output)

    def test_verify_rejects_an_unsigned_file_in_strict_mode(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "unsigned.bin"
            target.write_bytes(b"MZ" + b"\0" * 64)
            code, output = self.run_script(
                "verify_signature.ps1", "-FilePath", str(target),
                "-ExpectedPublisher", "CN=Anyone",
            )
        self.assertNotEqual(0, code)
        self.assertIn("Authenticode is not valid", output)

    def test_verify_rejects_an_unsigned_file_even_in_untrusted_mode(self):
        # Untrusted mode relaxes the CHAIN, never the presence of a
        # signature by the pinned certificate.
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "unsigned.bin"
            target.write_bytes(b"MZ" + b"\0" * 64)
            code, output = self.run_script(
                "verify_signature.ps1", "-FilePath", str(target),
                "-ExpectedPublisher", "CN=Anyone", "-AllowUntrustedRoot",
                "-ExpectedThumbprint", "0" * 40,
            )
        self.assertNotEqual(0, code)
        self.assertIn("No Authenticode signature is present", output)

    def test_the_proof_removes_its_certificate_on_every_path(self):
        proof = (INSTALLER / "prove_signing.ps1").read_text(encoding="utf-8")
        self.assertIn("-KeyExportPolicy NonExportable", proof)
        self.assertIn("finally {", proof)
        self.assertIn("Remove-Item -LiteralPath $storePath -Force", proof)
        # Verification caches the signer in another store; a My-only sweep
        # left a copy behind on 2026-09-03.
        self.assertIn("Get-ChildItem Cert:\\CurrentUser -Recurse", proof)
        # ...and the sweep must skip store containers, which have no
        # Thumbprint and abort the whole finally under strict mode.
        self.assertIn("$_ -is $certificateType -and $_.Thumbprint", proof)
        self.assertIn("is STILL in Cert:", proof)

if __name__ == "__main__":
    unittest.main(verbosity=2)
