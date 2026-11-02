"""Wizard setup keeps credentials private and touches only its own services."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from Hosting import control, setup


class HostControlTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = self.folder / "Host Program"
        (self.root / "app").mkdir(parents=True)
        self.controller = control.HostController(self.root / "app", self.folder / "host state")
        self.settings = {
            "hostname": "ears.example.org", "guild_id": "123456789012345678",
            "application_id": "234567890123456789", "guild_name": "Example Guild",
            "bot_token": "XENO_SYNTHETIC_BOT_TOKEN_ONLY", "port": 28770,
            "client_download_url": "https://downloads.example.org/Setup.exe",
            "tunnel_enabled": False,
        }

    def configure(self, **changes):
        with patch.object(setup, "protect_directory"):
            self.controller.configure(self.settings | changes)

    def test_configuration_and_reopen_never_return_saved_tokens(self):
        self.configure(tunnel_enabled=True, tunnel_token="XENO_SYNTHETIC_TUNNEL_TOKEN_123456789")
        settings = self.controller.read_settings()
        self.assertTrue(settings["configured"])
        self.assertTrue(settings["tunnel_enabled"])
        self.assertEqual("234567890123456789", settings["application_id"])
        self.assertNotIn("XENO_SYNTHETIC", json.dumps(settings))
        self.assertNotIn("XENO_SYNTHETIC", (self.controller.state / "host-settings.json").read_text())
        self.assertNotIn("XENO_SYNTHETIC", (self.controller.state / "relay.env").read_text())

    def test_edit_keeps_identity_keys_and_tokens_when_blank(self):
        self.configure()
        secret = self.controller.state / "secrets"
        original = {p.name: p.read_bytes() for p in secret.iterdir()}
        self.controller.configure(self.settings | {"bot_token": "", "hostname": "new.example.org"})
        self.assertEqual(original, {p.name: p.read_bytes() for p in secret.iterdir()})
        self.assertEqual("new.example.org", self.controller.read_settings()["hostname"])
        (self.controller.state / "consent/consent.json").write_text("{}", encoding="ascii")
        with self.assertRaisesRegex(control.HostError, "Discord server and application identity"):
            self.controller.configure(self.settings | {"guild_id": "345678901234567890"})

    def test_failed_validation_does_not_create_partial_state(self):
        cases = (
            {"tunnel_enabled": True},
            {"client_download_url": ""},
            {"client_installer": str(self.folder / "missing.exe")},
            {"tunnel_enabled": True, "tunnel_token": "cloudflared service install secret"},
            {"guild_id": "123"},
        )
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises((control.HostError, ValueError)):
                self.configure(**changes)
            self.assertFalse(self.controller.state.exists())

    def test_private_tools_and_profile_are_used_during_preparation(self):
        self.configure()
        for path in (self.controller.node, self.controller.npm, self.controller.nssm,
                     self.controller.cloudflared, self.controller.python):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"test tool")
        outcome = SimpleNamespace(runtime=SimpleNamespace(acceleration="cuda"),
                                  adapter=SimpleNamespace(ready=True))
        with patch.object(control.runtime_installer, "cuda_12_4_available", return_value=True), \
             patch.object(control.runtime_installer, "install_voice_dependencies", return_value=outcome) as install:
            self.controller.prepare()
        self.assertEqual(self.controller.state / "profile/WoWVoiceCaptions/runtime", install.call_args.args[0])
        self.assertEqual(self.controller.node, install.call_args.kwargs["node_path"])
        self.assertEqual(self.controller.npm, install.call_args.kwargs["npm_cli_path"])

    def test_no_cpu_fallback_is_reported_as_a_completed_gpu_setup(self):
        self.configure()
        with patch.object(Path, "is_file", return_value=True), \
             patch.object(control.runtime_installer, "cuda_12_4_available", return_value=True), \
             patch.object(control.runtime_installer, "install_voice_dependencies", return_value=SimpleNamespace(
                 runtime=SimpleNamespace(acceleration="cpu"), adapter=SimpleNamespace(ready=True))):
            with self.assertRaisesRegex(control.HostError, "GPU speech preparation"):
                self.controller.prepare()

    def test_vulkan_runtime_is_a_completed_gpu_setup(self):
        self.configure()
        progress = []
        with patch.object(Path, "is_file", return_value=True), \
             patch.object(control.runtime_installer, "cuda_12_4_available", return_value=False), \
             patch.object(control.runtime_installer, "vulkan_runtime_pinned", return_value=True), \
             patch.object(control.runtime_installer, "vulkan_gpu_available", return_value=True), \
             patch.object(control.runtime_installer, "install_voice_dependencies", return_value=SimpleNamespace(
                 runtime=SimpleNamespace(acceleration="vulkan"), adapter=SimpleNamespace(ready=True))) as install:
            self.controller.prepare(progress.append)
        install.assert_called_once()
        self.assertIn("with AMD/Vulkan acceleration", progress[-1])

    def test_no_usable_gpu_stops_before_downloading(self):
        self.configure()
        for pinned, expected in ((False, "not available in this build"), (True, "NVIDIA or AMD GPU is required")):
            with self.subTest(pinned=pinned), \
                    patch.object(Path, "is_file", return_value=True), \
                    patch.object(control.runtime_installer, "cuda_12_4_available", return_value=False), \
                    patch.object(control.runtime_installer, "vulkan_runtime_pinned", return_value=pinned), \
                    patch.object(control.runtime_installer, "vulkan_gpu_available", return_value=False), \
                    patch.object(control.runtime_installer, "install_voice_dependencies") as install:
                with self.assertRaisesRegex(control.HostError, expected):
                    self.controller.prepare()
                install.assert_not_called()

    def test_ownership_check_happens_for_all_services_before_stopping(self):
        def get(name, setting):
            if setting == "AppDirectory":
                return str(self.root / "app") if name != control.WORKER else str(self.folder / "another-host")
            if setting == "Application":
                return str(self.controller.python)
            return '-B -m Hosting.service --state-directory "' + str(self.controller.state) + '" relay'
        with patch.object(self.controller, "_service_status", return_value="Running"), \
             patch.object(self.controller, "_get", side_effect=get), \
             patch.object(self.controller, "_nssm") as nssm:
            with self.assertRaisesRegex(control.HostError, "another installation"):
                self.controller.stop_services()
            nssm.assert_not_called()

    def test_stop_order_and_service_names_never_include_production_host(self):
        statuses = {name: "Running" for name in control.SERVICES}
        with patch.object(self.controller, "_owned_services", return_value=statuses), \
             patch.object(self.controller, "_nssm") as nssm:
            self.controller.stop_services()
        self.assertEqual([("stop", control.WORKER), ("stop", control.TUNNEL), ("stop", control.RELAY)],
                         [call.args for call in nssm.call_args_list])

    def test_tunnel_uses_token_file_without_secret_on_command_line(self):
        token = "XENO_SYNTHETIC_TUNNEL_TOKEN_123456789"
        self.configure(tunnel_enabled=True, tunnel_token=token)
        with patch.object(self.controller, "_owned_services", return_value={
            control.RELAY: "Running", control.WORKER: "Running", control.TUNNEL: "Not installed",
        }), patch.object(self.controller, "_nssm") as nssm:
            self.controller._install_tunnel()
        commands = [call.args for call in nssm.call_args_list]
        joined = "\n".join(str(args) for args in commands)
        self.assertNotIn(token, joined)
        self.assertIn("--token-file", joined)
        self.assertIn("cloudflare-tunnel-token", joined)
        self.assertEqual(("start", control.TUNNEL), commands[-1])

    def test_unknown_installation_marker_cannot_stop_or_remove_services(self):
        with patch.object(control.HostController, "stop_services") as stop, \
             patch.object(control.HostController, "remove_services") as remove:
            with self.assertRaises(control.HostError):
                control.installer_lifecycle(self.root, self.controller.state, remove=True)
        stop.assert_not_called()
        remove.assert_not_called()

    def test_failed_tunnel_configuration_rolls_back_only_a_new_registration(self):
        self.configure(tunnel_enabled=True, tunnel_token="XENO_SYNTHETIC_TUNNEL_TOKEN_123456789")
        for prior_status in ("Not installed", "Stopped"):
            with self.subTest(prior_status=prior_status):
                calls = []
                def nssm(*args, **kwargs):
                    calls.append(args)
                    if args[:3] == ("set", control.TUNNEL, "AppParameters"):
                        raise control.HostError("Synthetic set failure")
                with patch.object(self.controller, "_owned_services", return_value={
                    control.RELAY: "Running", control.WORKER: "Running", control.TUNNEL: prior_status,
                }), patch.object(self.controller, "_nssm", side_effect=nssm):
                    with self.assertRaisesRegex(control.HostError, "Synthetic set failure"):
                        self.controller._install_tunnel()
                self.assertEqual(prior_status == "Not installed",
                                 ("remove", control.TUNNEL, "confirm") in calls)

    def test_unused_initial_ids_can_be_corrected_with_a_new_token(self):
        self.configure()
        self.assertFalse(self.controller.read_settings()["identity_locked"])
        changes = {"guild_id": "345678901234567890", "application_id": "456789012345678901"}
        with self.assertRaisesRegex(control.HostError, "token again"):
            self.controller.configure(self.settings | changes | {"bot_token": ""})
        with patch.object(self.controller, "stop_services") as stop:
            self.controller.configure(self.settings | changes)
        stop.assert_called_once()
        self.assertEqual(changes["guild_id"], self.controller.read_settings()["guild_id"])
        self.assertEqual(changes["application_id"], self.controller.read_settings()["application_id"])

    def test_existing_pairing_locks_identity_even_without_consent_file(self):
        import sqlite3
        self.configure()
        database = self.controller.state / "relay/guild-hub.sqlite3"
        connection = sqlite3.connect(database)
        try:
            connection.execute("CREATE TABLE devices (id TEXT)")
            connection.execute("CREATE TABLE enrollments (id TEXT)")
            connection.execute("INSERT INTO enrollments VALUES ('XENO_ENROLLMENT')")
            connection.commit()
        finally:
            connection.close()
        self.assertTrue(self.controller.read_settings()["identity_locked"])

    def test_upgrade_restores_only_previously_running_owned_services(self):
        (self.root / "host-install.json").write_text(json.dumps({
            "schema": 1, "product": "vemals-ears-host", "test_build": False,
        }), encoding="ascii")
        original = {control.RELAY: "Running", control.WORKER: "Stopped", control.TUNNEL: "Running"}
        with patch.object(control.HostController, "_owned_services", return_value=original), \
             patch.object(control.HostController, "_run"), \
             patch.object(control.HostController, "stop_services"):
            control.installer_lifecycle(self.root, self.controller.state)
        stopped = {name: "Stopped" for name in control.SERVICES}
        with patch.object(control.HostController, "_owned_services", return_value=stopped), \
             patch.object(control.HostController, "_nssm") as nssm:
            control.installer_lifecycle(self.root, self.controller.state, resume=True)
        self.assertEqual([("start", control.RELAY), ("start", control.TUNNEL)],
                         [call.args for call in nssm.call_args_list])
        self.assertFalse((self.root / "host-resume.json").exists())

    def test_test_installer_lifecycle_never_touches_services(self):
        (self.root / "host-install.json").write_text(json.dumps({
            "schema": 1, "product": "vemals-ears-host", "test_build": True,
        }), encoding="ascii")
        with patch.object(control.HostController, "stop_services") as stop, \
             patch.object(control.HostController, "remove_services") as remove:
            control.installer_lifecycle(self.root, self.controller.state)
            control.installer_lifecycle(self.root, self.controller.state, remove=True)
        stop.assert_not_called()
        remove.assert_not_called()

    def test_subprocess_failure_does_not_echo_third_party_output(self):
        result = subprocess.CompletedProcess([], 1, "XENO_PRIVATE_stdout", "XENO_PRIVATE_stderr")
        with patch.object(subprocess, "run", return_value=result) as run:
            with self.assertRaises(control.HostError) as failure:
                self.controller._run([str(self.controller.nssm), "status", control.RELAY])
        self.assertNotIn("XENO_PRIVATE", str(failure.exception))
        self.assertEqual(subprocess.DEVNULL, run.call_args.kwargs["stdin"])
        self.assertIn(str(self.controller.node.parent), run.call_args.kwargs["env"]["PATH"])


if __name__ == "__main__":
    unittest.main()
