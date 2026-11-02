"""The shareable host never inherits another operator's configuration."""

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from Hosting import service


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "Hosting" / "Install-Host.ps1"
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


def configuration(state):
    return {
        "VEMALS_EARS_GUILD_ID": "12345678901234567",
        "VEMALS_EARS_GUILD_NAME": "Test Guild",
        "VEMALS_EARS_BIND": "127.0.0.1",
        "VEMALS_EARS_PORT": "18770",
        "VEMALS_EARS_PUBLIC_SERVER_URL": "wss://captions.example.org",
        "VEMALS_EARS_PUBLIC_HTTP_URL": "https://captions.example.org",
        "VEMALS_EARS_CLIENT_DOWNLOAD_URL": "https://captions.example.org/download",
        "VEMALS_EARS_DATABASE": str(state / "relay" / "guild-hub.sqlite3"),
        "VEMALS_EARS_PEPPER_FILE": str(state / "secrets" / "pepper"),
        "VEMALS_EARS_PUBLISHER_TOKEN_FILE": str(state / "secrets" / "publisher-token"),
        "VEMALS_EARS_DISCORD_APPLICATION_ID_FILE": str(state / "secrets" / "application-id"),
        "VEMALS_EARS_DISCORD_BOT_TOKEN_FILE": str(state / "secrets" / "bot-token"),
    }


def write_configuration(state, values=None):
    state.mkdir(parents=True, exist_ok=True)
    values = configuration(state) if values is None else values
    (state / "relay.env").write_text(
        "\n".join(key + "=" + value for key, value in values.items()) + "\n",
        encoding="ascii",
    )


class HostServiceTests(unittest.TestCase):
    def test_no_legacy_defaults_or_inherited_host_state_are_used(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder).resolve()
            write_configuration(state)
            with patch.dict(os.environ, {
                "VEMALS_EARS_PEPPER": "XENO_inherited_secret",
                "VEMALS_EARS_SERVER_URL": "ws://another-host.invalid",
                "VOICE_CAPTIONS_WHISPER_SERVER": "another-runtime",
                "CAPTIONS_CONSENT_STATE_PATH": "another-state",
                "LOCALAPPDATA": "another-profile",
            }):
                values = service.load_configuration(state)
                self.assertNotIn("VEMALS_EARS_DOWNLOAD_FILE", values)
                self.assertNotIn("ears.xenon.solutions", repr(values))
                service.configure_environment(values)
                self.assertNotIn("VEMALS_EARS_PEPPER", os.environ)
                self.assertNotIn("VOICE_CAPTIONS_WHISPER_SERVER", os.environ)
                self.assertEqual("ws://127.0.0.1:18770", os.environ["VEMALS_EARS_SERVER_URL"])
                self.assertEqual(str(state / "profile"), os.environ["LOCALAPPDATA"])
                self.assertEqual(str(state / "consent" / "consent.json"),
                                 os.environ["CAPTIONS_CONSENT_STATE_PATH"])
                self.assertEqual(str(state / "consent" / "mapping.json"),
                                 os.environ["CAPTIONS_MAPPING_STATE_PATH"])

    def test_missing_explicit_keys_never_fall_back_to_legacy_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            for key in service.REQUIRED_KEYS:
                with self.subTest(key=key):
                    values = configuration(state)
                    del values[key]
                    write_configuration(state, values)
                    with self.assertRaisesRegex(service.ConfigurationError, key):
                        service.load_configuration(state)

    def test_invalid_configuration_fails_without_echoing_secret_values(self):
        cases = (
            ("VEMALS_EARS_BIND", "0.0.0.0"),
            ("VEMALS_EARS_PORT", "70000"),
            ("VEMALS_EARS_PORT", "invalid"),
            ("VEMALS_EARS_GUILD_ID", "guild"),
            ("VEMALS_EARS_PUBLIC_HTTP_URL", "http://captions.example.org"),
            ("VEMALS_EARS_PUBLIC_SERVER_URL", "wss://user:password@captions.example.org"),
            ("VEMALS_EARS_PEPPER_FILE", "relative-secret"),
            ("VEMALS_EARS_DISCORD_BOT_TOKEN", "XENO_secret_must_not_appear"),
        )
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            for key, value in cases:
                with self.subTest(key=key, invalid=value):
                    values = configuration(state)
                    values[key] = value
                    write_configuration(state, values)
                    output = io.StringIO()
                    with contextlib.redirect_stderr(output):
                        result = service.main(["--state-directory", str(state), "relay"])
                    self.assertEqual(2, result)
                    self.assertNotIn("XENO_secret_must_not_appear", output.getvalue())
                    self.assertNotIn("user:password", output.getvalue())

    def test_check_only_creates_nothing_and_does_not_change_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            write_configuration(state)
            original = dict(os.environ)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, service.main([
                    "--state-directory", str(state), "--check-only", "relay",
                ]))
            self.assertEqual(original, dict(os.environ))
            self.assertEqual(["relay.env"], [path.name for path in state.iterdir()])

    def test_missing_file_relative_path_and_duplicate_keys_fail_closed(self):
        with self.assertRaises(service.ConfigurationError):
            service.load_configuration("relative")
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            with self.assertRaises(service.ConfigurationError):
                service.load_configuration(state)
            write_configuration(state)
            with (state / "relay.env").open("a", encoding="ascii") as handle:
                handle.write("VEMALS_EARS_PORT=8770\n")
            with self.assertRaisesRegex(service.ConfigurationError, "duplicate"):
                service.load_configuration(state)

    def test_admin_and_service_commands_use_the_selected_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            write_configuration(state)
            calls = []
            fake_server = types.SimpleNamespace(main=lambda args: calls.append(args) or 17)
            fake_worker = types.SimpleNamespace(main=lambda args: calls.append(args) or 18)
            with patch.dict(os.environ), patch("GuildHub.server", fake_server, create=True), \
                    patch("GuildHub.host", fake_worker, create=True), \
                    patch.object(service, "validate_runtime") as validate:
                self.assertEqual(17, service.main([
                    "--state-directory", str(state), "admin", "list",
                ]))
                self.assertEqual(["list"], calls[-1])
                self.assertEqual(17, service.main([
                    "--state-directory", str(state), "relay",
                ]))
                self.assertEqual(["serve"], calls[-1])
                self.assertEqual(18, service.main([
                    "--state-directory", str(state), "worker",
                ]))
                self.assertEqual(["--watch"], calls[-1])
                validate.assert_called_once()

    def test_worker_preflight_requires_verified_cuda_and_a_current_driver(self):
        from VoiceCaptions import runtime_installer

        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder).resolve()
            write_configuration(state)
            args = ["--state-directory", str(state), "--check-only", "worker"]
            for acceleration, driver_ready, expected in (
                (None, True, 2), ("cpu", True, 2), ("cuda", False, 2), ("cuda", True, 0),
            ):
                with self.subTest(acceleration=acceleration, driver=driver_ready):
                    runtime = None if acceleration is None else types.SimpleNamespace(acceleration=acceleration)
                    before = dict(os.environ)
                    with patch.object(runtime_installer, "detect_installed_runtime", return_value=runtime) as detect, \
                            patch.object(runtime_installer, "cuda_12_4_available", return_value=driver_ready), \
                            contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(expected, service.main(args))
                    detect.assert_called_once_with(state / "profile" / "WoWVoiceCaptions" / "runtime")
                    self.assertEqual(before, dict(os.environ))
                    self.assertEqual(["relay.env"], [path.name for path in state.iterdir()])

    def test_worker_preflight_accepts_verified_vulkan_with_a_vulkan_capable_gpu(self):
        from VoiceCaptions import runtime_installer

        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder).resolve()
            write_configuration(state)
            args = ["--state-directory", str(state), "--check-only", "worker"]
            for vulkan_ready, expected in ((False, 2), (True, 0)):
                with self.subTest(vulkan_ready=vulkan_ready):
                    runtime = types.SimpleNamespace(acceleration="vulkan")
                    stderr = io.StringIO()
                    with patch.object(runtime_installer, "detect_installed_runtime", return_value=runtime), \
                            patch.object(runtime_installer, "cuda_12_4_available",
                                         side_effect=AssertionError("CUDA is irrelevant to a Vulkan runtime")), \
                            patch.object(runtime_installer, "vulkan_gpu_available", return_value=vulkan_ready), \
                            contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                        self.assertEqual(expected, service.main(args))
                    if not vulkan_ready:
                        self.assertIn("Vulkan-capable GPU", stderr.getvalue())
                    self.assertEqual(["relay.env"], [path.name for path in state.iterdir()])

    def test_missing_runtime_error_names_only_supported_gpus(self):
        from VoiceCaptions import runtime_installer

        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder).resolve()
            write_configuration(state)
            args = ["--state-directory", str(state), "--check-only", "worker"]
            for pinned, expected in ((False, "verified NVIDIA GPU"), (True, "verified NVIDIA or AMD GPU")):
                with self.subTest(pinned=pinned):
                    stderr = io.StringIO()
                    with patch.object(runtime_installer, "detect_installed_runtime", return_value=None), \
                            patch.object(runtime_installer, "vulkan_runtime_pinned", return_value=pinned), \
                            contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                        self.assertEqual(2, service.main(args))
                    self.assertIn(expected, stderr.getvalue())

    def test_existing_port_owner_blocks_relay_preflight(self):
        with tempfile.TemporaryDirectory() as folder, socket.socket() as owner:
            owner.bind(("127.0.0.1", 0))
            owner.listen(1)
            state = Path(folder)
            values = configuration(state)
            values["VEMALS_EARS_PORT"] = str(owner.getsockname()[1])
            write_configuration(state, values)
            output = io.StringIO()
            with contextlib.redirect_stderr(output):
                result = service.main([
                    "--state-directory", str(state), "--check-only", "--check-port", "relay",
                ])
            self.assertEqual(2, result)
            self.assertIn("port is unavailable", output.getvalue())
            self.assertEqual(["relay.env"], [path.name for path in state.iterdir()])


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Windows PowerShell required")
class HostInstallationTests(unittest.TestCase):
    def preflight(self, script, state):
        return subprocess.run(
            [POWERSHELL, "-NoProfile", "-File", str(script), "-CheckOnly",
             "-PythonPath", sys.executable, "-NssmPath", str(state / "no-nssm.exe"),
             "-StateDirectory", str(state)],
            cwd=script.parent, capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def test_missing_runtime_blocks_installation_without_requiring_nssm_or_modifying_state(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "host state"
            write_configuration(state)
            result = self.preflight(SCRIPT, state)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("GPU speech runtime preflight failed", result.stdout + result.stderr)
            self.assertEqual(["relay.env"], [path.name for path in state.iterdir()])

    def test_incomplete_package_fails_before_creating_state(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            script = root / "Hosting" / SCRIPT.name
            script.parent.mkdir()
            shutil.copyfile(SCRIPT, script)
            state = root / "state"
            result = self.preflight(script, state)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("Incomplete host package", result.stdout + result.stderr)
            self.assertFalse(state.exists())

    def test_windows_powershell_preserves_native_arguments_and_reinstallation_comparison(self):
        # Load only the native invocation functions. The harness never runs the
        # installer, starts a service, or calls the real NSSM executable.
        powershell = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        if not powershell.is_file():
            self.skipTest("Windows PowerShell 5.1 is required")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "native arguments with spaces"
            root.mkdir()
            fake = root / "fake service tool.py"
            record = root / "configuration.json"
            harness = root / "probe.ps1"
            fake.write_text("\n".join((
                "import json, pathlib, sys",
                "record = pathlib.Path(sys.argv[1])",
                "data = json.loads(record.read_text()) if record.exists() else {}",
                "action = sys.argv[2]",
                "if action == 'install':",
                "    assert len(sys.argv) == 4, sys.argv",
                "    data['installed_application'] = sys.argv[3]",
                "elif action == 'set':",
                "    assert len(sys.argv) == 5, sys.argv",
                "    data[sys.argv[3]] = sys.argv[4]",
                "elif action == 'get':",
                "    assert len(sys.argv) == 4, sys.argv",
                "    print(data[sys.argv[3]])",
                "else:",
                "    raise AssertionError(action)",
                "if action != 'get':",
                "    record.write_text(json.dumps(data), encoding='ascii')",
            )) + "\n", encoding="ascii")
            harness.write_text("\n".join((
                "param([string]$Source, [string]$Python, [string]$Fake, [string]$Record)",
                "$ErrorActionPreference = 'Stop'",
                "$tokens = $null; $parseErrors = $null",
                "$ast = [Management.Automation.Language.Parser]::ParseFile($Source, [ref]$tokens, [ref]$parseErrors)",
                "if ($parseErrors.Count) { throw 'Installer could not be parsed.' }",
                "$definitions = $ast.FindAll({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -in @('ConvertTo-NativeArgument', 'Invoke-NativeProcess') }, $true)",
                "foreach ($definition in $definitions) { Invoke-Expression $definition.Extent.Text }",
                "$application = 'C:\\Program Files\\Python\\python.exe'",
                "$parameters = '-B -m Hosting.service --state-directory \"C:\\Host State\" worker'",
                "$result = Invoke-NativeProcess -FilePath $Python -ArgumentList @($Fake, $Record, 'install', $application)",
                "if ($result.ExitCode -ne 0) { throw $result.StandardError }",
                "$settings = @{ Application = $application; AppParameters = $parameters; TrailingSlash = 'C:\\Host State\\'; Empty = ''; EmbeddedQuote = 'quoted \"value\"' }",
                "foreach ($key in $settings.Keys) {",
                "    $result = Invoke-NativeProcess -FilePath $Python -ArgumentList @($Fake, $Record, 'set', $key, $settings[$key])",
                "    if ($result.ExitCode -ne 0) { throw $result.StandardError }",
                "    $result = Invoke-NativeProcess -FilePath $Python -ArgumentList @($Fake, $Record, 'get', $key)",
                "    if ($result.ExitCode -ne 0 -or $result.StandardOutput.Trim() -cne $settings[$key]) { throw ('Argument roundtrip failed: ' + $key + ' ' + $result.StandardError) }",
                "}",
                "Write-Output 'NATIVE-ARGUMENT-ROUNDTRIP-OK'",
            )) + "\n", encoding="ascii")
            result = subprocess.run(
                [str(powershell), "-NoProfile", "-File", str(harness), str(SCRIPT),
                 sys.executable, str(fake), str(record)],
                cwd=root, capture_output=True, text=True, timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("NATIVE-ARGUMENT-ROUNDTRIP-OK", result.stdout)
            recorded = json.loads(record.read_text(encoding="ascii"))
            self.assertEqual('C:\\Program Files\\Python\\python.exe', recorded["Application"])
            self.assertEqual('-B -m Hosting.service --state-directory "C:\\Host State" worker',
                             recorded["AppParameters"])


if __name__ == "__main__":
    unittest.main()
