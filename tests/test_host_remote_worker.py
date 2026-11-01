"""Imported workers keep the shared relay and unrelated Windows services intact."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from Hosting import control, service


def write_remote_state(state: Path, *, confirmed=False, changes=None):
    state.mkdir(parents=True, exist_ok=True)
    secrets = state / "secrets"
    secrets.mkdir(exist_ok=True)
    (secrets / "discord-application-id").write_text("234567890123456789\n", encoding="ascii")
    (secrets / "discord-bot-token").write_text("XENO_SYNTHETIC_BOT_TOKEN_ONLY\n", encoding="ascii")
    (secrets / "publisher-token").write_text("XENO_SYNTHETIC_PUBLISHER_TOKEN_ONLY_12345678\n", encoding="ascii")
    values = {
        "VEMALS_EARS_HOST_MODE": "remote-worker",
        "VEMALS_EARS_GUILD_ID": "123456789012345678",
        "VEMALS_EARS_GUILD_NAME": "Existing Example Guild",
        "VEMALS_EARS_PUBLIC_SERVER_URL": "wss://shared.example.org",
        "VEMALS_EARS_PUBLIC_HTTP_URL": "https://shared.example.org",
        "VEMALS_EARS_CLIENT_DOWNLOAD_URL": "https://members.example.org/download/1.2.3",
        "VEMALS_EARS_PUBLISHER_TOKEN_FILE": str(secrets / "publisher-token"),
        "VEMALS_EARS_DISCORD_APPLICATION_ID_FILE": str(secrets / "discord-application-id"),
        "VEMALS_EARS_DISCORD_BOT_TOKEN_FILE": str(secrets / "discord-bot-token"),
    }
    values.update(changes or {})
    (state / "relay.env").write_text("".join(key + "=" + value + "\n" for key, value in values.items()), encoding="ascii")
    (state / "host-settings.json").write_text(json.dumps({
        "schema": 1, "mode": "remote-worker", "takeover_confirmed": confirmed,
    }), encoding="ascii")
    return values


class RemoteWorkerTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.root = self.folder / "Host Program"
        (self.root / "app").mkdir(parents=True)
        self.state = self.folder / "imported state"
        self.controller = control.HostController(self.root / "app", self.state)
        self.values = write_remote_state(self.state)

    def snapshot(self):
        return {str(path.relative_to(self.state)): path.read_bytes()
                for path in self.state.rglob("*") if path.is_file()}

    def owned_value(self, name, setting):
        self.assertEqual(control.WORKER, name)
        return {
            "AppDirectory": str(self.controller.package_root),
            "Application": str(self.controller.python),
            "AppParameters": '-B -m Hosting.service --state-directory "' + str(self.state) + '" --host-mode remote-worker worker',
        }[setting]

    def test_configuration_preserves_remote_urls_and_creates_no_relay_state(self):
        before = self.snapshot()
        with patch.dict(os.environ, {"VEMALS_EARS_SERVER_URL": "ws://127.0.0.1:8770",
                                    "VEMALS_EARS_PEPPER": "XENO_OLD_SECRET"}):
            values = service.load_configuration(self.state)
            service.configure_environment(values)
            self.assertEqual("wss://shared.example.org", os.environ["VEMALS_EARS_SERVER_URL"])
            self.assertNotIn("VEMALS_EARS_PEPPER", os.environ)
            for key in service.LOCAL_RELAY_KEYS:
                self.assertNotIn(key, values)
                self.assertNotIn(key, os.environ)
        for key, value in self.values.items():
            self.assertEqual(value, values[key])
        self.assertEqual(str(self.state / "profile"), values["LOCALAPPDATA"])
        self.assertEqual(str(self.state / "consent/consent.json"), values["CAPTIONS_CONSENT_STATE_PATH"])
        self.assertEqual(before, self.snapshot())

    def test_remote_origins_and_local_configuration_mixing_fail_closed(self):
        invalid = (
            {"VEMALS_EARS_PUBLIC_SERVER_URL": "ws://shared.example.org"},
            {"VEMALS_EARS_PUBLIC_SERVER_URL": "wss://XENO_SECRET@shared.example.org"},
            {"VEMALS_EARS_PUBLIC_HTTP_URL": "https://shared.example.org/?token=XENO_SECRET"},
            {"VEMALS_EARS_PUBLIC_HTTP_URL": "https://shared.example.org?"},
            {"VEMALS_EARS_PUBLIC_HTTP_URL": "https://shared.example.org/#"},
            {"VEMALS_EARS_PUBLIC_SERVER_URL": "wss://@shared.example.org"},
            {"VEMALS_EARS_PUBLIC_HTTP_URL": "https://shared.example.org/path"},
            {"VEMALS_EARS_PUBLIC_HTTP_URL": "https://other.example.org"},
            {"VEMALS_EARS_SERVER_URL": "ws://127.0.0.1:8770"},
            {"VEMALS_EARS_DATABASE": str(self.state / "relay/database")},
            {"VEMALS_EARS_PORT": "8770"},
            {"VEMALS_EARS_HOST_MODE": "unknown"},
        )
        for changes in invalid:
            with self.subTest(changes=changes):
                write_remote_state(self.state, changes=changes)
                with self.assertRaises(service.ConfigurationError) as failure:
                    service.load_configuration(self.state)
                self.assertNotIn("XENO_SECRET", str(failure.exception))

    def test_explicit_default_wss_port_matches_the_same_https_origin(self):
        write_remote_state(self.state, changes={"VEMALS_EARS_PUBLIC_SERVER_URL": "wss://shared.example.org:443"})
        values = service.load_configuration(self.state)
        self.assertEqual("wss://shared.example.org:443", values["VEMALS_EARS_SERVER_URL"])
        self.assertEqual("https://shared.example.org", values["VEMALS_EARS_PUBLIC_HTTP_URL"])

    def test_worker_preflight_needs_no_activation_port_or_local_server(self):
        before = self.snapshot()
        environment = dict(os.environ)
        with patch.object(service, "validate_runtime") as runtime, \
                patch.object(service.socket, "socket") as socket, \
                patch("GuildHub.server.main") as relay, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, service.main([
                "--state-directory", str(self.state), "--host-mode", "remote-worker", "--check-only", "worker",
            ]))
        runtime.assert_called_once()
        socket.assert_not_called()
        relay.assert_not_called()
        self.assertEqual(environment, dict(os.environ))
        self.assertEqual(before, self.snapshot())

    def test_actual_start_requires_confirmation_and_exact_mode_then_uses_wss(self):
        args = ["--state-directory", str(self.state), "--host-mode", "remote-worker", "worker"]
        observed = []
        def run_worker(arguments):
            observed.append((arguments, os.environ["VEMALS_EARS_SERVER_URL"]))
            return 7
        with patch.dict(os.environ), patch.object(service, "validate_runtime"), \
                patch("GuildHub.host.main", side_effect=run_worker), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(2, service.main(args))
            self.assertEqual([], observed)
            write_remote_state(self.state, confirmed=True)
            self.assertEqual(2, service.main(["--state-directory", str(self.state), "worker"]))
            self.assertEqual(7, service.main(args))
            for role in ("relay", "admin"):
                self.assertEqual(2, service.main(args[:-1] + [role]))
        self.assertEqual([(["--watch"], "wss://shared.example.org")], observed)

    def test_dashboard_is_locked_and_fresh_host_edits_cannot_replace_import(self):
        before = self.snapshot()
        settings = self.controller.read_settings()
        self.assertEqual("remote-worker", settings["mode"])
        self.assertTrue(settings["identity_locked"])
        self.assertFalse(settings["takeover_confirmed"])
        self.assertFalse(settings["tunnel_enabled"])
        self.assertIsNone(settings["port"])
        self.assertEqual("https://shared.example.org/join", settings["join_url"])
        self.assertEqual(self.values["VEMALS_EARS_CLIENT_DOWNLOAD_URL"], settings["client_download_url"])
        self.assertNotIn("XENO_", json.dumps(settings))
        with self.assertRaisesRegex(control.HostError, "Imported worker settings"):
            self.controller.configure({"hostname": "new.example.org"})
        self.assertEqual(before, self.snapshot())

    def test_preparation_needs_no_cloudflared_or_member_download_and_never_changes_services(self):
        for path in (self.controller.python, self.controller.node, self.controller.npm, self.controller.nssm):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"synthetic private tool")
        outcome = SimpleNamespace(runtime=SimpleNamespace(acceleration="cuda"), adapter=SimpleNamespace(ready=True))
        with patch.object(control.runtime_installer, "cuda_12_4_available", return_value=True), \
                patch.object(control.runtime_installer, "install_voice_dependencies", return_value=outcome) as install, \
                patch.object(self.controller, "_run") as run, patch.object(control.shutil, "copyfile") as copy:
            self.controller.prepare()
        install.assert_called_once()
        self.assertEqual(self.state / "profile/WoWVoiceCaptions/runtime", install.call_args.args[0])
        run.assert_not_called()
        copy.assert_not_called()
        self.assertFalse((self.state / "relay").exists())
        self.assertFalse((self.state / "downloads").exists())

    def test_controller_activation_gate_precedes_any_service_access(self):
        with patch.object(self.controller, "_run") as run, \
                patch.object(self.controller, "_service_status") as status:
            for action in (self.controller.install_services, self.controller.start_services):
                with self.assertRaisesRegex(control.HostError, "previous caption worker is stopped"):
                    action()
        run.assert_not_called()
        status.assert_not_called()

    def test_remote_lifecycle_queries_and_mutates_only_its_exact_worker(self):
        write_remote_state(self.state, confirmed=True)
        with patch.object(self.controller, "_service_status", return_value="Stopped") as query, \
                patch.object(self.controller, "_get", side_effect=self.owned_value), \
                patch.object(self.controller, "_nssm") as nssm:
            self.controller.start_services()
            self.controller.remove_services()
        self.assertEqual({control.WORKER}, {call.args[0] for call in query.call_args_list})
        self.assertEqual([("start", control.WORKER), ("remove", control.WORKER, "confirm")],
                         [call.args for call in nssm.call_args_list])
        with patch.object(self.controller, "_service_status", return_value="Running") as query, \
                patch.object(self.controller, "_get", side_effect=self.owned_value), \
                patch.object(self.controller, "_nssm") as nssm:
            self.controller.stop_services()
        self.assertEqual([("stop", control.WORKER)], [call.args for call in nssm.call_args_list])
        self.assertEqual([control.WORKER], [call.args[0] for call in query.call_args_list])

    def test_remote_service_install_never_installs_or_removes_a_tunnel(self):
        write_remote_state(self.state, confirmed=True)
        with patch.object(self.controller, "_owned_services", return_value={control.WORKER: "Not installed"}), \
                patch.object(self.controller, "_run", return_value=subprocess.CompletedProcess([], 0)) as run, \
                patch.object(self.controller, "_install_tunnel") as tunnel:
            self.controller.install_services()
        self.assertIn(str(self.state), run.call_args.args[0])
        tunnel.assert_not_called()

    def test_prior_standalone_worker_registration_is_not_adopted_or_stopped(self):
        def standalone_parameters(name, setting):
            value = self.owned_value(name, setting)
            return value.replace("--host-mode remote-worker ", "") if setting == "AppParameters" else value
        write_remote_state(self.state, confirmed=True)
        with patch.object(self.controller, "_service_status", return_value="Running"), \
                patch.object(self.controller, "_get", side_effect=standalone_parameters), \
                patch.object(self.controller, "_nssm") as nssm:
            with self.assertRaisesRegex(control.HostError, "another state folder"):
                self.controller.stop_services()
        nssm.assert_not_called()

    def test_status_checks_shared_https_without_redirects_and_worker_readiness(self):
        write_remote_state(self.state, confirmed=True)
        response = io.BytesIO(b'{"status":"ok","service":"vemals-ears"}')
        opener = SimpleNamespace(open=lambda url, timeout: response)
        with patch.object(self.controller, "_service_status", return_value="Running") as query, \
                patch.object(self.controller, "_get", side_effect=self.owned_value), \
                patch.object(control.urllib.request, "build_opener", return_value=opener) as build, \
                patch.object(control.WorkerHealthMarker, "is_healthy", return_value=True), \
                patch.object(opener, "open", wraps=opener.open) as request:
            result = self.controller.status()
        self.assertTrue(result["health"])
        self.assertEqual("Shared relay ready", result["relay"])
        self.assertEqual("Shared host", result["tunnel"])
        request.assert_called_once_with("https://shared.example.org/healthz", timeout=2)
        self.assertTrue(any(isinstance(handler, control._NoHealthRedirect) for handler in build.call_args.args))
        self.assertIsNone(control._NoHealthRedirect().redirect_request(None, None, 302, "Found", {}, "http://elsewhere.invalid"))
        self.assertEqual({control.WORKER}, {call.args[0] for call in query.call_args_list})

    def test_upgrade_stop_resume_and_removal_are_worker_only(self):
        write_remote_state(self.state, confirmed=True)
        (self.root / "host-install.json").write_text(json.dumps({
            "schema": 1, "product": "vemals-ears-host", "test_build": False,
        }), encoding="ascii")
        with patch.object(control.HostController, "_owned_services", return_value={control.WORKER: "Running"}), \
                patch.object(control.HostController, "_run"), \
                patch.object(control.HostController, "_nssm") as nssm:
            control.installer_lifecycle(self.root, self.state)
        self.assertEqual([("stop", control.WORKER)], [call.args for call in nssm.call_args_list])
        receipt = json.loads((self.root / "host-resume.json").read_text())
        self.assertEqual([control.WORKER], receipt["running"])
        self.assertEqual("remote-worker", receipt["mode"])
        with patch.object(control.HostController, "_owned_services", return_value={control.WORKER: "Stopped"}), \
                patch.object(control.HostController, "_nssm") as nssm:
            control.installer_lifecycle(self.root, self.state, resume=True)
        self.assertEqual([("start", control.WORKER)], [call.args for call in nssm.call_args_list])
        with patch.object(control.HostController, "_owned_services", return_value={control.WORKER: "Stopped"}), \
                patch.object(control.HostController, "_run"), \
                patch.object(control.HostController, "_nssm") as nssm:
            control.installer_lifecycle(self.root, self.state, remove=True)
        self.assertEqual([("remove", control.WORKER, "confirm")], [call.args for call in nssm.call_args_list])
        (self.root / "host-resume.json").write_text(json.dumps({
            "schema": 1, "mode": "remote-worker", "running": [control.RELAY],
        }), encoding="ascii")
        with patch.object(control.HostController, "_nssm") as nssm:
            with self.assertRaisesRegex(control.HostError, "receipt is invalid"):
                control.installer_lifecycle(self.root, self.state, resume=True)
        nssm.assert_not_called()


if __name__ == "__main__":
    unittest.main()
