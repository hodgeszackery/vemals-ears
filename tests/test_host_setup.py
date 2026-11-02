"""Fresh host configuration stays portable, private and separate from production."""

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from Hosting import setup


class HostSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.state = Path(self.temporary).resolve() / "new-host"
        self.options = dict(
            public_hostname="ears.example.org", guild_id="123456789012345678",
            application_id="234567890123456789", bot_token="XENO_test_discord_token_fixture",
        )

    def configure(self, **overrides):
        return setup.create_configuration(self.state, **(self.options | overrides))

    def test_configuration_uses_only_new_host_identity_and_separate_secret_files(self):
        with mock.patch.object(setup, "protect_directory") as protect:
            values = self.configure()
        protect.assert_called_once()
        self.assertEqual(self.state.parent, protect.call_args.args[0].parent)
        self.assertTrue(protect.call_args.args[0].name.startswith(".VemalsEarsHost-"))
        config = (self.state / "relay.env").read_text(encoding="ascii")
        self.assertNotIn(self.options["bot_token"], config)
        self.assertNotIn("xenon.solutions", config)
        self.assertNotIn("REQUIRE_VOICE_MEMBERSHIP", config)
        self.assertEqual("127.0.0.1", values["VEMALS_EARS_BIND"])
        self.assertEqual("", values["VEMALS_EARS_CLIENT_VERSION"])
        for name, value in values.items():
            if name.endswith("_FILE") or name.endswith("_DATABASE"):
                self.assertTrue(Path(value).is_relative_to(self.state))
        self.assertEqual("wss://ears.example.org", values["VEMALS_EARS_PUBLIC_SERVER_URL"])
        self.assertEqual(self.options["bot_token"],
                         (self.state / "secrets/discord-bot-token").read_text().strip())
        self.assertIn("reverse_proxy 127.0.0.1:8770", (self.state / "Caddyfile").read_text())

    def test_each_host_gets_fresh_relay_secrets(self):
        with mock.patch.object(setup, "protect_directory"):
            self.configure()
            other = Path(self.temporary) / "other-host"
            setup.create_configuration(other, **self.options)
        for name in ("pepper", "publisher-token"):
            first = (self.state / "secrets" / name).read_text().strip()
            second = (other / "secrets" / name).read_text().strip()
            self.assertNotEqual(first, second)
            self.assertRegex(first, r"^[A-Za-z0-9_-]{64}$")

    def test_rerun_cannot_replace_secrets_or_state(self):
        with mock.patch.object(setup, "protect_directory"):
            self.configure()
        original = (self.state / "secrets/pepper").read_bytes()
        with self.assertRaisesRegex(setup.SetupError, "already exists"):
            self.configure()
        self.assertEqual(original, (self.state / "secrets/pepper").read_bytes())

    def test_bad_values_are_rejected_before_creating_state(self):
        cases = (
            {"public_hostname": "ears.example.org\nreverse_proxy evil:80"},
            {"public_hostname": "https://ears.example.org"},
            {"public_hostname": "127.0.0.1"},
            {"guild_id": "123"}, {"guild_name": "Guild\nEXTRA=value"},
            {"guild_name": "G" * 81},
            {"bot_token": "XENO_fixture_token_with_\u00e9"}, {"port": 0},
            {"client_download_url": "https://user:password@example.org/client.exe"},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(setup.SetupError):
                    self.configure(**overrides)
                self.assertFalse(self.state.exists())

    def test_permission_failure_writes_no_credentials(self):
        with mock.patch.object(setup, "protect_directory", side_effect=setup.SetupError("ACL failed")):
            with self.assertRaises(setup.SetupError):
                self.configure()
        self.assertFalse(self.state.exists())
        self.assertEqual([], list(self.state.parent.iterdir()))

    def test_interrupted_configuration_can_retry_without_manual_cleanup(self):
        with mock.patch.object(setup, "protect_directory"), \
             mock.patch.object(setup.relay_env, "save", side_effect=OSError("Synthetic disk failure")):
            with self.assertRaises(OSError):
                self.configure()
        self.assertFalse(self.state.exists())
        self.assertEqual([], list(self.state.parent.iterdir()))
        with mock.patch.object(setup, "protect_directory"):
            self.configure()
        self.assertTrue((self.state / "relay.env").is_file())

    def test_state_cannot_live_inside_shareable_package(self):
        with self.assertRaises(setup.SetupError):
            setup.state_path(setup.ROOT / "state")
        with self.assertRaises(setup.SetupError):
            setup.state_path(Path("state"))

    def test_prepare_requires_cuda_and_rejects_runtime_fallback(self):
        with mock.patch.object(setup, "protect_directory"):
            self.configure()
        installer = setup.runtime_installer
        with mock.patch.object(installer, "cuda_12_4_available", return_value=False), \
             mock.patch.object(installer, "install_voice_dependencies") as install:
            with self.assertRaisesRegex(setup.SetupError, "compatible NVIDIA"):
                setup.prepare_runtime(self.state)
            install.assert_not_called()
        outcome = SimpleNamespace(runtime=SimpleNamespace(acceleration="cpu"),
                                  adapter=SimpleNamespace(ready=True))
        with mock.patch.object(installer, "cuda_12_4_available", return_value=True), \
             mock.patch.object(installer, "install_voice_dependencies", return_value=outcome):
            with self.assertRaisesRegex(setup.SetupError, "not a completed GPU"):
                setup.prepare_runtime(self.state)

    def test_prepare_uses_service_profile_not_interactive_profile(self):
        with mock.patch.object(setup, "protect_directory"):
            self.configure()
        outcome = SimpleNamespace(runtime=SimpleNamespace(acceleration="cuda"),
                                  adapter=SimpleNamespace(ready=True))
        with mock.patch.object(setup.runtime_installer, "cuda_12_4_available", return_value=True), \
             mock.patch.object(setup.runtime_installer, "install_voice_dependencies", return_value=outcome) as install, \
             mock.patch("builtins.print"):
            setup.prepare_runtime(self.state)
        self.assertEqual(self.state / "profile/WoWVoiceCaptions/runtime", install.call_args.args[0])

    def test_prepare_requirement_names_amd_only_once_vulkan_is_pinned(self):
        with mock.patch.object(setup, "protect_directory"):
            self.configure()
        installer = setup.runtime_installer
        probe = mock.Mock(side_effect=AssertionError("unpinned Vulkan must not be probed"))
        with mock.patch.object(installer, "cuda_12_4_available", return_value=False), \
             mock.patch.object(installer, "vulkan_gpu_available", probe), \
             mock.patch.object(installer, "install_voice_dependencies") as install:
            with self.assertRaisesRegex(setup.SetupError, "AMD/Vulkan support is not available in this build"):
                setup.prepare_runtime(self.state)
            install.assert_not_called()
        probe.assert_not_called()
        with mock.patch.object(installer, "cuda_12_4_available", return_value=False), \
             mock.patch.object(installer, "vulkan_runtime_pinned", return_value=True), \
             mock.patch.object(installer, "vulkan_gpu_available", return_value=False), \
             mock.patch.object(installer, "install_voice_dependencies") as install:
            with self.assertRaisesRegex(setup.SetupError, "compatible NVIDIA or AMD GPU is required"):
                setup.prepare_runtime(self.state)
            install.assert_not_called()

    def test_prepare_accepts_vulkan_and_rejects_its_runtime_fallback(self):
        with mock.patch.object(setup, "protect_directory"):
            self.configure()
        installer = setup.runtime_installer
        vulkan = SimpleNamespace(runtime=SimpleNamespace(acceleration="vulkan"),
                                 adapter=SimpleNamespace(ready=True))
        with mock.patch.object(installer, "cuda_12_4_available", return_value=False), \
             mock.patch.object(installer, "vulkan_runtime_pinned", return_value=True), \
             mock.patch.object(installer, "vulkan_gpu_available", return_value=True), \
             mock.patch.object(installer, "install_voice_dependencies", return_value=vulkan) as install, \
             mock.patch("builtins.print") as printed:
            setup.prepare_runtime(self.state)
        install.assert_called_once()
        self.assertIn("with AMD/Vulkan acceleration", printed.call_args.args[0])
        cpu = SimpleNamespace(runtime=SimpleNamespace(acceleration="cpu"),
                              adapter=SimpleNamespace(ready=True))
        with mock.patch.object(installer, "cuda_12_4_available", return_value=False), \
             mock.patch.object(installer, "vulkan_runtime_pinned", return_value=True), \
             mock.patch.object(installer, "vulkan_gpu_available", return_value=True), \
             mock.patch.object(installer, "install_voice_dependencies", return_value=cpu):
            with self.assertRaisesRegex(setup.SetupError, "not a completed GPU"):
                setup.prepare_runtime(self.state)

    def test_prepare_reports_nvidia_acceleration(self):
        with mock.patch.object(setup, "protect_directory"):
            self.configure()
        outcome = SimpleNamespace(runtime=SimpleNamespace(acceleration="cuda"),
                                  adapter=SimpleNamespace(ready=True))
        with mock.patch.object(setup.runtime_installer, "cuda_12_4_available", return_value=True), \
             mock.patch.object(setup.runtime_installer, "install_voice_dependencies", return_value=outcome), \
             mock.patch("builtins.print") as printed:
            setup.prepare_runtime(self.state)
        self.assertIn("with NVIDIA acceleration", printed.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
