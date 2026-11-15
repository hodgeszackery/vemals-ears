"""No start route runs a host that is pending, being sent, released or moved.

Also the pieces the sender and receiver sequences are built from: control
frames, the typed-chat profile delta, the list of served downloads and the
HTTP edges. Synthetic values and temp directories only; no service is touched.
"""

import contextlib
import hashlib
import hmac
import http.server
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from GuildHub import relay_env
from GuildHub.store import CredentialStore
from Hosting import control, service, setup, transfer, transfer_flow, transfer_net, transfer_pake
from Hosting.transfer import TransferError

from tests.test_host_transfer import APPLICATION, GUILD, PEPPER, TRANSFER, make_state


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "Hosting" / "Install-Host.ps1"
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")
RELAY, WORKER, TUNNEL = control.RELAY, control.WORKER, control.TUNNEL
KEY = b"\x33" * 32
# (role, state) receipts, and whether a start is allowed.
BLOCKED = (("sender", "sending"), ("sender", "released"), ("sender", "moved"), ("receiver", "pending"))
SENDING_RELAY = ("sender", "sending", "relay")
ALLOWED = (("sender", "offered"), ("sender", "cancelled"), ("receiver", "active"), ("receiver", "discarded"))


class GuardCase(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(setup, "protect_directory"))
        self.enterContext(mock.patch.object(setup, "restrict_descendants"))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.state, self.device = make_state(self.root)
        self.records = transfer.TransferRecords(service.transfer_directory(self.state))

    def receipt(self, role, state):
        """Drive a real receipt into this state through the public record API."""
        if self.records.receipt.exists():
            self.records.receipt.unlink()
        with self.records.locked():
            if role == "sender":
                self.records.offer(TRANSFER)
                if state in ("sending", "released", "moved"):
                    self.records.advance("sending")
                if state in ("released", "moved"):
                    self.records.release()
                if state == "moved":
                    self.records.advance("moved")
                if state == "cancelled":
                    self.records.advance("cancelled")
            else:
                secret = b"\x5a" * 32
                self.records.receive(TRANSFER, transfer_pake.release_commitment(TRANSFER, secret))
                if state == "active":
                    self.records.activate(secret)
                if state == "discarded":
                    self.records.advance("discarded")
        self.assertEqual((role, state), (self.records.read()["role"], self.records.read()["state"]))

    def settings(self, **changes):
        path = self.state / "host-settings.json"
        value = json.loads(path.read_text(encoding="ascii"))
        value.update(changes)
        path.write_text(json.dumps(value), encoding="ascii")

    def values(self):
        return service.load_configuration(self.state)


class ServiceGuardTests(GuardCase):
    def test_transfer_paths_are_derived_for_a_standalone_host_only(self):
        values = self.values()
        self.assertEqual(str(self.state / "secrets" / "transfer-key"), values["VEMALS_EARS_TRANSFER_KEY_FILE"])
        self.assertEqual(str(self.state / "transfer-frozen"), values["VEMALS_EARS_TRANSFER_FREEZE_FILE"])
        self.assertEqual(self.state.with_name(self.state.name + "Transfer"), service.transfer_directory(self.state))
        self.assertFalse(service.transfer_directory(self.state).is_relative_to(self.state))
        # relay.env cannot point the relay at another key or flag.
        explicit = relay_env.load(self.state / "relay.env")
        explicit["VEMALS_EARS_TRANSFER_KEY_FILE"] = (self.root / "elsewhere-key").as_posix()
        explicit["VEMALS_EARS_TRANSFER_FREEZE_FILE"] = (self.root / "elsewhere-flag").as_posix()
        relay_env.save(explicit, self.state / "relay.env")
        self.assertEqual(values["VEMALS_EARS_TRANSFER_KEY_FILE"], self.values()["VEMALS_EARS_TRANSFER_KEY_FILE"])
        self.assertEqual(values["VEMALS_EARS_TRANSFER_FREEZE_FILE"], self.values()["VEMALS_EARS_TRANSFER_FREEZE_FILE"])

    def remote_state(self, extra=""):
        remote = self.root / "RemoteWorker"
        (remote / "secrets").mkdir(parents=True)
        lines = [
            "VEMALS_EARS_HOST_MODE=remote-worker", "VEMALS_EARS_GUILD_ID=" + GUILD,
            "VEMALS_EARS_GUILD_NAME=XENO Test Guild",
            "VEMALS_EARS_PUBLIC_SERVER_URL=wss://ears.example.org",
            "VEMALS_EARS_PUBLIC_HTTP_URL=https://ears.example.org",
            "VEMALS_EARS_CLIENT_DOWNLOAD_URL=https://ears.example.org/download",
            "VEMALS_EARS_PUBLISHER_TOKEN_FILE=" + (remote / "secrets/publisher-token").as_posix(),
            "VEMALS_EARS_DISCORD_APPLICATION_ID_FILE=" + (remote / "secrets/discord-application-id").as_posix(),
            "VEMALS_EARS_DISCORD_BOT_TOKEN_FILE=" + (remote / "secrets/discord-bot-token").as_posix(),
        ]
        (remote / "relay.env").write_text("\n".join(lines) + "\n" + extra, encoding="ascii")
        return remote

    def test_remote_worker_is_never_given_a_transfer_key_or_freeze_flag(self):
        values = service.load_configuration(self.remote_state())
        self.assertEqual(service.REMOTE_WORKER, values["VEMALS_EARS_HOST_MODE"])
        self.assertFalse([key for key in values if "TRANSFER" in key])
        remote = self.remote_state_with_explicit_paths()
        self.assertFalse([key for key in service.load_configuration(remote) if "TRANSFER" in key])

    def remote_state_with_explicit_paths(self):
        shutil.rmtree(self.root / "RemoteWorker")
        return self.remote_state(
            "VEMALS_EARS_TRANSFER_KEY_FILE=" + (self.root / "key").as_posix() + "\n"
            "VEMALS_EARS_TRANSFER_FREEZE_FILE=" + (self.root / "flag").as_posix() + "\n")

    def test_every_blocked_receipt_refuses_relay_worker_and_admin(self):
        for role, state in BLOCKED:
            self.receipt(role, state)
            for process in (None, "relay", "worker", "admin"):
                if (role, state, process) == SENDING_RELAY:
                    continue
                with self.subTest(role=role, state=state, process=process), \
                     self.assertRaises(service.ConfigurationError):
                    service.validate_activation(self.values(), process)
        # The one exception: the relay of a host that is being sent may come
        # back after a reboot. It starts frozen from the flag file, so typed
        # chat does not wait for the operator. Nothing else may start, and
        # once the host is released its relay is refused like the rest.
        self.receipt("sender", "sending")
        service.validate_activation(self.values(), "relay")
        for role, state in ALLOWED:
            self.receipt(role, state)
            for process in (None, "relay", "worker"):
                with self.subTest(role=role, state=state, process=process):
                    service.validate_activation(self.values(), process)

    def test_messages_say_what_to_do_and_never_what_is_secret(self):
        cases = ((("sender", "sending"), "being moved"), (("sender", "released"), "being moved"),
                 (("sender", "moved"), "locked archive"), (("receiver", "pending"), "waiting for that PC"))
        for (role, state), text in cases:
            self.receipt(role, state)
            with self.assertRaisesRegex(service.ConfigurationError, text) as refused:
                service.validate_activation(self.values())
            self.assertNotIn("XENO", str(refused.exception))

    def test_pending_mark_in_the_state_blocks_even_without_a_receipt(self):
        self.settings(activation="pending")
        with self.assertRaisesRegex(service.ConfigurationError, "waiting for that PC"):
            service.validate_activation(self.values(), "worker")
        self.settings(activation="active")
        service.validate_activation(self.values(), "worker")
        # Released on paper but the state still says pending: not yet.
        self.receipt("receiver", "active")
        self.settings(activation="pending")
        with self.assertRaises(service.ConfigurationError):
            service.validate_activation(self.values(), "relay")

    def test_unreadable_receipt_is_never_no_transfer(self):
        self.receipt("sender", "moved")
        for damaged in (b"", b"{", b"[]", b'{"role":"sender"}', b'{"role":"nobody","state":"moved"}',
                        b"\xff\xfe", b" " * 70_000):
            self.records.receipt.write_bytes(damaged)
            with self.subTest(damaged=damaged[:12]), \
                 self.assertRaisesRegex(service.ConfigurationError, "cannot be read"):
                service.validate_activation(self.values(), "worker")
            with self.assertRaisesRegex(control.HostError, "cannot be read"):
                control.HostController(self.root / "app", self.state).transfer_hold()

    def test_host_admin_cli_is_refused_while_a_receipt_is_active(self):
        for role, state in (("sender", "offered"), *BLOCKED):
            self.receipt(role, state)
            output = io.StringIO()
            with self.subTest(role=role, state=state), contextlib.redirect_stderr(output), \
                 mock.patch("GuildHub.server.main") as relay_main:
                self.assertEqual(2, service.main(["--state-directory", str(self.state), "admin", "list"]))
                relay_main.assert_not_called()
            self.assertNotIn("XENO", output.getvalue())
        self.receipt("sender", "offered")
        with self.assertRaisesRegex(service.ConfigurationError, "transfer is open"):
            service.validate_activation(self.values(), "admin")
        # An open offer does not stop the running host itself, and a finished one stops nothing.
        service.validate_activation(self.values(), "worker")
        self.receipt("sender", "cancelled")
        with mock.patch("GuildHub.server.main", return_value=0) as relay_main:
            self.assertEqual(0, service.main(["--state-directory", str(self.state), "admin", "list"]))
        relay_main.assert_called_once_with(["list"])

    def test_a_relay_restarted_while_its_host_is_being_sent_runs_and_is_frozen_by_the_flag_file(self):
        self.receipt("sender", "sending")
        (self.state / "transfer-frozen").write_bytes(b"1\n")
        with contextlib.redirect_stderr(io.StringIO()), \
             mock.patch.object(service, "validate_runtime"), \
             mock.patch("GuildHub.server.main", return_value=0) as relay_main, \
             mock.patch("GuildHub.host.main") as worker_main:
            self.assertEqual(0, service.main(["--state-directory", str(self.state), "relay"]))
            relay_main.assert_called_once_with(["serve"])
            # The relay it starts is told where the persisted freeze flag is.
            self.assertEqual(str(self.state / "transfer-frozen"), os.environ["VEMALS_EARS_TRANSFER_FREEZE_FILE"])
            self.assertEqual(2, service.main(["--state-directory", str(self.state), "worker"]))
            worker_main.assert_not_called()

    def test_service_process_refuses_to_run_a_held_host(self):
        for role, state in BLOCKED:
            self.receipt(role, state)
            for process in ("relay", "worker"):
                if (role, state, process) == SENDING_RELAY:
                    continue
                with self.subTest(role=role, state=state, process=process), \
                     contextlib.redirect_stderr(io.StringIO()), \
                     mock.patch.object(service, "validate_runtime"), \
                     mock.patch("GuildHub.server.main") as relay_main, \
                     mock.patch("GuildHub.host.main") as worker_main:
                    self.assertEqual(2, service.main(["--state-directory", str(self.state), process]))
                    relay_main.assert_not_called()
                    worker_main.assert_not_called()

    def test_staged_install_is_only_for_a_received_host_waiting_for_release(self):
        for role, state in (*BLOCKED, *ALLOWED):
            self.receipt(role, state)
            self.settings(activation="pending")
            with self.subTest(role=role, state=state):
                if (role, state) == ("receiver", "pending"):
                    service.validate_staged(self.values())
                else:
                    with self.assertRaises(service.ConfigurationError):
                        service.validate_staged(self.values())
        self.receipt("receiver", "pending")
        self.settings(activation="active")
        with self.assertRaises(service.ConfigurationError):
            service.validate_staged(self.values())
        with self.assertRaises(service.ConfigurationError):
            service.validate_staged(service.load_configuration(self.remote_state()))

    def test_worker_handoff_activation_is_unchanged(self):
        remote = self.remote_state()
        values = service.load_configuration(remote)
        with self.assertRaisesRegex(service.ConfigurationError, "previous caption worker is stopped"):
            service.validate_activation(values)
        (remote / "host-settings.json").write_text(json.dumps(
            {"schema": 1, "mode": "remote-worker", "takeover_confirmed": True}), encoding="ascii")
        service.validate_activation(values)
        service.validate_activation(values, "worker")


class ControlGuardTests(GuardCase):
    def setUp(self):
        super().setUp()
        (self.root / "Program/app").mkdir(parents=True)
        self.controller = control.HostController(self.root / "Program/app", self.state)
        self.statuses = {name: "Stopped" for name in control.SERVICES}

    def owned(self):
        return mock.patch.object(self.controller, "_owned_services", side_effect=lambda: dict(self.statuses))

    def test_no_control_start_path_touches_a_held_host(self):
        for role, state in BLOCKED:
            self.receipt(role, state)
            actions = (self.controller.start_services, self.controller.install_services,
                       self.controller.enable_services, self.controller.release_tunnel,
                       lambda: self.controller.restore_service(WORKER, control.AUTOMATIC, running=True))
            for action in actions:
                with self.subTest(role=role, state=state), self.owned(), \
                     mock.patch.object(self.controller, "_nssm") as nssm, \
                     mock.patch.object(self.controller, "_run") as run, \
                     self.assertRaises(control.HostError):
                    action()
                started = [call.args for call in nssm.call_args_list if call.args[0] == "start"]
                self.assertEqual([], started)
                run.assert_not_called()
            self.assertEqual("moved" if state == "moved" else "held", self.controller.transfer_hold())

    def test_a_withheld_tunnel_is_left_disabled_by_every_ordinary_path(self):
        # A received, active host whose old public address was never verified
        # quiet: nothing but release_tunnel() may enable or start its tunnel,
        # whatever the transfer receipt says by now (here: none at all).
        self.settings(tunnel_enabled=True, tunnel_withheld=True)
        self.assertTrue(self.controller.read_settings()["tunnel_withheld"])
        self.statuses = {RELAY: "Running", WORKER: "Running", TUNNEL: "Stopped"}
        calls = []

        def nssm(*arguments, check=True):
            calls.append(arguments)
            return mock.Mock(returncode=0, stdout="")

        with self.owned(), mock.patch.object(self.controller, "_nssm", side_effect=nssm), \
             mock.patch.object(self.controller, "_run", return_value=mock.Mock(returncode=0)):
            self.controller.install_services()          # "Retry setup"
            self.assertIn(("set", TUNNEL, "Start", control.DISABLED), calls)
            self.controller.start_services()            # "Start"
            self.controller.enable_services()
            self.assertEqual([], [call for call in calls if call[:2] == ("start", TUNNEL)])
            self.assertNotIn(("set", TUNNEL, "Start", control.AUTOMATIC), calls)
            self.assertTrue(self.controller.read_settings()["tunnel_withheld"])
            del calls[:]
            self.controller.release_tunnel()
            self.assertEqual([("set", TUNNEL, "Start", control.AUTOMATIC), ("start", TUNNEL)], calls)
            self.assertFalse(self.controller.read_settings()["tunnel_withheld"])
            # From here it is an ordinary tunnel again.
            del calls[:]
            self.controller.install_services()
            self.assertIn(("set", TUNNEL, "Start", control.AUTOMATIC), calls)
            self.assertIn(("start", TUNNEL), calls)
        # A host that was never received has no mark and is not affected.
        self.assertNotIn("tunnel_withheld", json.loads((self.state / "host-settings.json").read_text()))

    def test_staged_runtime_counts_only_as_a_verified_gpu_runtime(self):
        staging = self.root / "staging"
        self.assertFalse(self.controller.staged_runtime_ready(staging))
        with mock.patch.object(control.runtime_installer, "detect_installed_runtime") as detect:
            detect.return_value = mock.Mock(acceleration="cuda")
            self.assertTrue(self.controller.staged_runtime_ready(staging))
            detect.assert_called_once_with(staging / "profile/WoWVoiceCaptions/runtime")
            detect.return_value = mock.Mock(acceleration="vulkan")
            self.assertTrue(self.controller.staged_runtime_ready(staging))
            detect.return_value = mock.Mock(acceleration="cpu")
            self.assertFalse(self.controller.staged_runtime_ready(staging))
            detect.return_value = None
            self.assertFalse(self.controller.staged_runtime_ready(staging))

    def test_staged_install_registers_disabled_and_starts_nothing(self):
        self.receipt("receiver", "pending")
        self.settings(activation="pending")
        self.statuses = {name: "Not installed" for name in control.SERVICES}
        calls = []

        def nssm(*arguments, check=True):
            calls.append(arguments)
            if arguments[0] == "install":
                self.statuses[arguments[1]] = "Stopped"
            return mock.Mock(returncode=0, stdout="")

        with self.owned(), mock.patch.object(self.controller, "_nssm", side_effect=nssm), \
             mock.patch.object(self.controller, "_run", return_value=mock.Mock(returncode=0)) as run:
            self.controller.install_services(staged=True)
        command = [str(item) for item in run.call_args.args[0]]
        self.assertIn("-Staged", command)
        self.assertEqual(("install", TUNNEL, str(self.controller.cloudflared)), calls[0])
        self.assertIn(("set", TUNNEL, "Start", control.DISABLED), calls)
        self.assertEqual([], [call for call in calls if call[0] == "start"])
        # A tunnel token on the command line would be a secret in the service record.
        self.assertNotIn("XENO_synthetic_tunnel", json.dumps(calls))

    def test_staged_install_refuses_a_pc_that_already_has_services_or_an_ordinary_host(self):
        self.receipt("receiver", "pending")
        self.settings(activation="pending")
        self.statuses[RELAY] = "Stopped"
        with self.owned(), mock.patch.object(self.controller, "_run") as run, \
             self.assertRaisesRegex(control.HostError, "already has host services"):
            self.controller.install_services(staged=True)
        run.assert_not_called()
        self.records.receipt.unlink()
        self.settings(activation="")
        with self.owned(), mock.patch.object(self.controller, "_run") as run, \
             self.assertRaisesRegex(control.HostError, "only for a received host"):
            self.controller.install_services(staged=True)
        run.assert_not_called()

    def test_ordinary_install_does_not_pass_the_staged_switch(self):
        with self.owned(), mock.patch.object(self.controller, "_nssm"), \
             mock.patch.object(self.controller, "_run", return_value=mock.Mock(returncode=0)) as run:
            self.controller.install_services()
        self.assertNotIn("-Staged", [str(item) for item in run.call_args.args[0]])

    def test_disable_is_set_before_the_stop_and_both_are_verified(self):
        self.statuses[WORKER] = "Running"
        calls, start = [], {"mode": control.AUTOMATIC}

        def nssm(*arguments, check=True):
            calls.append(arguments[:2] + arguments[2:4])
            if arguments[0] == "set" and arguments[2] == "Start":
                start["mode"] = arguments[3]
            if arguments[0] == "stop":
                self.statuses[WORKER] = "Stopped"
            return mock.Mock(returncode=0, stdout=start["mode"] + "\n")

        with self.owned(), mock.patch.object(self.controller, "_nssm", side_effect=nssm), \
             mock.patch.object(self.controller, "_service_status", side_effect=lambda name: self.statuses[name]):
            self.controller.disable_service(WORKER)
            self.assertEqual([("set", WORKER, "Start", control.DISABLED), ("stop", WORKER)], calls[:2])
            # A service that restarts itself, or whose start type did not stick, is not "down".
            self.statuses[WORKER] = "Running"
            with mock.patch.object(control.time, "monotonic", side_effect=[0, 100]), \
                 mock.patch.object(control.time, "sleep"), \
                 mock.patch.object(self.controller, "_nssm", return_value=mock.Mock(stdout=control.DISABLED)), \
                 self.assertRaisesRegex(control.HostError, "did not finish changing state"):
                self.controller.disable_service(WORKER)
            self.statuses[WORKER] = "Stopped"
            with mock.patch.object(self.controller, "_nssm", return_value=mock.Mock(stdout=control.AUTOMATIC)), \
                 self.assertRaisesRegex(control.HostError, "did not stay disabled"):
                self.controller.disable_service(WORKER)
        self.statuses[TUNNEL] = "Not installed"
        with self.owned(), mock.patch.object(self.controller, "_nssm") as untouched, \
             self.assertRaisesRegex(control.HostError, "not installed"):
            self.controller.disable_service(TUNNEL)
        untouched.assert_not_called()
        with self.owned(), self.assertRaisesRegex(control.HostError, "not supported"):
            self.controller.restore_service(WORKER, "SERVICE_BOOT_START", running=True)

    def test_saving_settings_keeps_the_activation_mark_and_is_refused_mid_move(self):
        settings = {"hostname": "ears.example.org", "guild_id": GUILD, "application_id": APPLICATION,
                    "guild_name": "XENO Test Guild", "port": 8770, "tunnel_enabled": True,
                    "client_download_url": "https://downloads.example.org/Setup.exe"}
        self.settings(activation="active", transfer_id=TRANSFER)
        self.controller.configure(settings)
        saved = json.loads((self.state / "host-settings.json").read_text(encoding="ascii"))
        self.assertEqual(("active", TRANSFER), (saved["activation"], saved["transfer_id"]))
        self.assertEqual("active", self.controller.read_settings()["activation"])
        self.settings(activation="pending")
        with self.assertRaisesRegex(control.HostError, "part of a move"):
            self.controller.configure(settings)
        self.assertEqual("pending", json.loads((self.state / "host-settings.json").read_text())["activation"])
        with self.owned():
            self.assertIn("stays stopped until the old PC releases it", self.controller.status()["detail"])
        self.settings(activation="active")
        for role, state in (("sender", "sending"), ("sender", "released")):
            self.receipt(role, state)
            with self.assertRaisesRegex(control.HostError, "part of a move"):
                self.controller.configure(settings)

    def lifecycle(self, **arguments):
        install = self.root / "Program"
        (install / "host-install.json").write_text(json.dumps(
            {"schema": 1, "product": "vemals-ears-host", "test_build": False}), encoding="ascii")
        with mock.patch.object(control.HostController, "_owned_services",
                               return_value={name: "Running" for name in control.SERVICES}), \
             mock.patch.object(control.HostController, "_run") as run, \
             mock.patch.object(control.HostController, "stop_services") as stop, \
             mock.patch.object(control.HostController, "_nssm") as nssm:
            control.installer_lifecycle(install, self.state, **arguments)
        return install / "host-resume.json", run, stop, nssm

    def test_updating_a_host_mid_move_stops_it_and_never_starts_it(self):
        for role, state in (("sender", "sending"), ("sender", "released"), ("receiver", "pending")):
            self.receipt(role, state)
            with self.subTest(role=role, state=state):
                resume, _run, stop, nssm = self.lifecycle()
                stop.assert_called_once()
                self.assertEqual([], json.loads(resume.read_text(encoding="ascii"))["running"])
                # Even a receipt written before the move began starts nothing.
                resume.write_text(json.dumps({"schema": 1, "mode": "standalone",
                                              "running": [RELAY, WORKER, TUNNEL]}), encoding="ascii")
                resume, _run, _stop, nssm = self.lifecycle(resume=True)
                nssm.assert_not_called()
                self.assertFalse(resume.exists())

    def test_updating_the_old_pc_after_a_move_touches_nothing(self):
        self.receipt("sender", "moved")
        resume = self.root / "Program/host-resume.json"
        (self.root / "Program").mkdir(exist_ok=True)
        resume.write_text(json.dumps({"schema": 1, "mode": "standalone", "running": [RELAY, WORKER]}),
                          encoding="ascii")
        for arguments in ({}, {"resume": True}):
            resume_path, run, stop, nssm = self.lifecycle(**arguments)
            run.assert_not_called()
            stop.assert_not_called()
            nssm.assert_not_called()
        self.assertFalse(resume_path.exists())

    def test_an_ordinary_update_still_restores_what_was_running(self):
        resume, _run, stop, _nssm = self.lifecycle()
        stop.assert_called_once()
        self.assertEqual(sorted(control.SERVICES), json.loads(resume.read_text(encoding="ascii"))["running"])
        with mock.patch.object(control.HostController, "_owned_services",
                               return_value={name: "Stopped" for name in control.SERVICES}), \
             mock.patch.object(control.HostController, "_nssm") as nssm:
            control.installer_lifecycle(self.root / "Program", self.state, resume=True)
        self.assertEqual([("start", RELAY), ("start", WORKER), ("start", TUNNEL)],
                         [call.args for call in nssm.call_args_list])


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Windows PowerShell required")
class InstallScriptTests(GuardCase):
    def run_script(self, *switches):
        return subprocess.run(
            [POWERSHELL, "-NoProfile", "-File", str(SCRIPT), *switches, "-PythonPath", sys.executable,
             "-NssmPath", str(self.root / "no-nssm.exe"), "-StateDirectory", str(self.state)],
            cwd=SCRIPT.parent, capture_output=True, text=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def test_script_preflight_refuses_by_transfer_state_before_any_service_tool_is_needed(self):
        refusal = "transfer state preflight failed"
        later = "GPU speech runtime preflight failed"
        # (receipt, switches) -> the first failure. no-nssm.exe does not exist,
        # so reaching the service tool at all would fail differently.
        cases = (
            (("receiver", "pending"), (), refusal),
            (("sender", "sending"), (), refusal),
            (("sender", "released"), (), refusal),
            (("sender", "moved"), (), refusal),
            (("sender", "cancelled"), ("-Staged",), refusal),
            (("receiver", "pending"), ("-Staged",), later),
            (("sender", "cancelled"), (), later),
            (("sender", "moved"), ("-CheckOnly",), later),
        )
        for (role, state), switches, expected in cases:
            self.receipt(role, state)
            self.settings(activation="pending" if (role, state) == ("receiver", "pending") else "")
            with self.subTest(role=role, state=state, switches=switches):
                result = self.run_script(*switches)
                self.assertNotEqual(0, result.returncode)
                self.assertIn(expected, result.stdout + result.stderr)
                self.assertNotIn("XENO", result.stdout + result.stderr)

    def test_staged_registration_is_disabled_never_started_and_needs_an_empty_pc(self):
        # Only the registration function runs, against an in-memory service manager.
        harness = self.root / "staged.ps1"
        harness.write_text(r'''
param([string]$Source, [string]$StateDirectory)
$ErrorActionPreference = 'Stop'
$tokens = $null; $parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($Source, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw 'Installer parse failed.' }
$definition = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Install-RegisteredHostServices' }, $true)
Invoke-Expression $definition.Extent.Text
$script:registry = @{}
$script:events = [Collections.Generic.List[string]]::new()
function New-FakeService {
    param([string]$Name)
    $value = [pscustomobject]@{ Name=$Name; Status='Stopped'; Settings=@{} }
    Add-Member -InputObject $value -MemberType ScriptMethod -Name WaitForStatus -Value { param($Expected, $Timeout) }
    return $value
}
function Get-Service { [CmdletBinding()] param([string]$Name) return $script:registry[$Name] }
function Start-Service { [CmdletBinding()] param([string]$Name) $script:events.Add('start ' + $Name); $script:registry[$Name].Status = 'Running' }
function Stop-Service { [CmdletBinding()] param([string]$Name) $script:events.Add('stop ' + $Name); $script:registry[$Name].Status = 'Stopped' }
function Invoke-Nssm {
    param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
    $action, $name = $Arguments[0], $Arguments[1]
    if ($action -eq 'install') { $script:registry[$name] = New-FakeService $name }
    elseif ($action -eq 'set') { $script:registry[$name].Settings[$Arguments[2]] = $Arguments[3] }
    elseif ($action -eq 'reset') { }
    else { $script:events.Add($action + ' ' + $name) }
}
function Get-NssmValue { param([string]$Name, [string]$Setting) return $script:registry[$Name].Settings[$Setting] }
$services = @(
    @{ Name='VemalsEarsHostRelay'; Role='relay'; Title='Relay'; Parameters='relay parameters' },
    @{ Name='VemalsEarsHostWorker'; Role='worker'; Title='Worker'; Parameters='worker parameters' }
)
$arguments = @{ Services=$services; PythonPath='C:/Program Files/Python/python.exe'; RepoRoot=[IO.Path]::GetFullPath($StateDirectory); StateDirectory=$StateDirectory; NodePath='C:/Program Files/Node/node.exe' }
Install-RegisteredHostServices @arguments -Staged
$staged = [ordered]@{
    modes = @($services | ForEach-Object { $script:registry[$_.Name].Settings['Start'] })
    status = @($services | ForEach-Object { $script:registry[$_.Name].Status })
    events = @($script:events)
}
$refused = ''
try { Install-RegisteredHostServices @arguments -Staged } catch { $refused = $_.ToString() }
$afterRefusal = @($script:events)
$remote = ''
try { Install-RegisteredHostServices @arguments -Staged -HostMode 'remote-worker' } catch { $remote = $_.ToString() }
$script:registry = @{}
$script:events.Clear()
Install-RegisteredHostServices @arguments
@{ staged=$staged; refused=$refused; afterRefusal=$afterRefusal; remote=$remote
   normalModes=@($services | ForEach-Object { $script:registry[$_.Name].Settings['Start'] })
   normalEvents=@($script:events) } | ConvertTo-Json -Compress -Depth 4
'''.lstrip(), encoding="ascii")
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-File", str(harness), str(SCRIPT), str(self.root / "ps state")],
            cwd=self.root, capture_output=True, text=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        proof = json.loads(result.stdout)
        self.assertEqual(["SERVICE_DISABLED", "SERVICE_DISABLED"], proof["staged"]["modes"])
        self.assertEqual(["Stopped", "Stopped"], proof["staged"]["status"])
        self.assertEqual([], proof["staged"]["events"] or [])
        self.assertIn("already has host services", proof["refused"])
        self.assertEqual([], proof["afterRefusal"] or [])
        self.assertIn("standalone", proof["remote"])
        self.assertEqual(["SERVICE_DELAYED_AUTO_START"] * 2, proof["normalModes"])
        self.assertEqual(["start VemalsEarsHostRelay", "start VemalsEarsHostWorker"], proof["normalEvents"])


class ControlFrameTests(unittest.TestCase):
    def pair(self):
        return (transfer.ControlChannel(TRANSFER, KEY, transfer_pake.ROLE_OFFER),
                transfer.ControlChannel(TRANSFER, KEY, transfer_pake.ROLE_CLAIM))

    def test_messages_arrive_in_order_in_both_directions(self):
        offer, claim = self.pair()
        self.assertEqual({"type": "card", "card": {"port": 8770}},
                         claim.open(offer.seal("card", card={"port": 8770})))
        self.assertEqual({"type": "ready"}, offer.open(claim.seal("ready")))
        self.assertEqual({"type": "release", "secret": "ab"}, claim.open(offer.seal("release", secret="ab")))
        frame = offer.seal("delta", rows=[["device", "Name", "", None]], final=True)
        self.assertTrue(transfer.is_control_frame(frame))
        self.assertLessEqual(len(frame), transfer.FRAME_LIMIT)
        with self.assertRaises(TransferError):
            offer.seal("delta", rows=["x" * transfer.FRAME_LIMIT])
        with self.assertRaises(TransferError):
            offer.seal("Not A Kind")

    def test_reflected_replayed_reordered_foreign_and_tampered_frames_end_the_channel(self):
        def attempt(make):
            offer, claim = self.pair()
            frame = make(offer, claim)
            with self.assertRaisesRegex(TransferError, "invalid transfer message"):
                claim.open(frame)
            # One bad frame is the end: even a good one is refused afterwards.
            with self.assertRaises(TransferError):
                claim.open(offer.seal("ready"))

        attempt(lambda offer, claim: claim.seal("ready"))                      # reflected
        attempt(lambda offer, claim: (claim.open(first := offer.seal("card")), first)[1])   # replayed
        attempt(lambda offer, claim: (offer.seal("card"), offer.seal("delta"))[1])          # reordered
        attempt(lambda offer, claim: transfer.ControlChannel(
            "f" * 32, KEY, transfer_pake.ROLE_OFFER).seal("card"))                           # other transfer
        attempt(lambda offer, claim: transfer.ControlChannel(
            TRANSFER, b"\x44" * 32, transfer_pake.ROLE_OFFER).seal("card"))                  # other session
        attempt(lambda offer, claim: offer.seal("card")[:-1] + b"\x00")
        attempt(lambda offer, claim: b"")
        attempt(lambda offer, claim: "text")

    def test_control_and_bundle_frames_have_separate_mac_domains(self):
        offer, claim = self.pair()
        bundle_frames = transfer.split_frames(b"x" * 100, TRANSFER, KEY)
        with self.assertRaises(TransferError):
            claim.open(bundle_frames[0])
        with self.assertRaises(TransferError):
            transfer.FrameAssembler(TRANSFER, KEY).feed(offer.seal("card"))
        # The same key, the bundle-frame MAC over a control body: still refused.
        offer, claim = self.pair()
        body = offer.seal("release", secret="00")[:-32]
        forged = body + transfer._frame_mac(KEY, body)
        with self.assertRaises(TransferError):
            claim.open(forged)
        self.assertFalse(hmac.compare_digest(transfer._frame_mac(KEY, body), offer._mac(body)))


class ProfileDeltaTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.database = self.root / "relay.sqlite3"
        self.store = CredentialStore(self.database, PEPPER)
        self.store.initialize()
        self.devices = [self.store.claim_enrollment(self.store.create_enrollment().code, "XENO " + str(index), "1.0.0")
                        for index in range(3)]
        self.copy = self.root / "copy.sqlite3"

    def test_only_rows_changed_since_the_snapshot_travel_and_are_applied(self):
        first, second, third = (device.device_id for device in self.devices)
        self.store.set_quick_chat_profile(first, "Early", "paladin")
        before = transfer.profile_rows(self.database)
        shutil.copyfile(self.database, self.copy)
        self.assertEqual([], transfer.profile_delta(before, self.database))
        self.store.set_quick_chat_name(second, "Late N\u00e4me")
        self.store.revoke_device(third)
        delta = transfer.profile_delta(before, self.database)
        self.assertEqual(sorted([second, third]), [row[0] for row in delta])
        delta = json.loads(json.dumps(delta))     # as it arrives from a control frame
        self.assertEqual(2, transfer.apply_profile_delta(self.copy, delta))
        moved = CredentialStore(self.copy, PEPPER)
        self.assertEqual("Late N\u00e4me", moved.authenticate_device_record(
            second, self.devices[1].device_token).quick_chat_name)
        self.assertEqual("Early", moved.authenticate_device_record(first, self.devices[0].device_token).quick_chat_name)
        self.assertFalse(moved.authenticate_device(third, self.devices[2].device_token))
        # A delta can revoke; it can never bring a revoked device back.
        self.assertEqual(1, transfer.apply_profile_delta(self.copy, [[third, None, "", None]]))
        self.assertFalse(moved.authenticate_device(third, self.devices[2].device_token))
        # Unknown devices are not created.
        self.assertEqual(0, transfer.apply_profile_delta(self.copy, [["ghdev_unknown", "X", "", None]]))
        self.assertEqual(3, len(moved.list_devices()))

    def test_a_malformed_delta_changes_nothing(self):
        device = self.devices[0].device_id
        self.store.set_quick_chat_name(device, "Kept")
        good = [device, "Changed", "", None]
        for bad in ("row", [device, "x"], [device, 5, "", None], ["bad id!", "x", "", None],
                    [device, "x", "DROP TABLE", None], [device, "x", "", "now"], [device, "x\x00", "", None],
                    [device, "x" * 65, "", None], [device, "x", "", -1.0]):
            with self.subTest(bad=bad), self.assertRaises(TransferError):
                transfer.apply_profile_delta(self.database, [good, bad])
        with self.assertRaises(TransferError):
            transfer.apply_profile_delta(self.database, "rows")
        with self.assertRaises(TransferError):
            transfer.apply_profile_delta(self.root / "missing.sqlite3", [good])
        self.assertEqual("Kept", transfer.profile_rows(self.database)[device][0])

    @unittest.skipUnless(os.name == "nt", "Windows AES helper")
    def test_deltas_travel_encrypted_and_large_ones_are_split_to_fit_control_frames(self):
        keys, other = mock.Mock(bundle_enc=b"\x44" * 32), mock.Mock(bundle_enc=b"\x45" * 32)
        rows = [["ghdev_" + format(index, "024d"), "N\u00e4me " * 6, "paladin", 1.5] for index in range(300)]
        parts = transfer.seal_delta(rows, keys)
        self.assertGreater(len(parts), 1)
        self.assertEqual([False] * (len(parts) - 1) + [True], [part["final"] for part in parts])
        offer = transfer.ControlChannel(TRANSFER, KEY, transfer_pake.ROLE_OFFER)
        claim = transfer.ControlChannel(TRANSFER, KEY, transfer_pake.ROLE_CLAIM)
        sealed = b""
        for part in parts:
            frame = offer.seal("delta", **part)
            self.assertLessEqual(len(frame), transfer.FRAME_LIMIT)
            # Nothing a member could be recognised by is readable on the wire.
            for private in (b"ghdev_", b"paladin", b"me N"):
                self.assertNotIn(private, frame)
            data, final = transfer.delta_part(claim.open(frame))
            self.assertEqual(part["final"], final)
            sealed += data
        self.assertEqual(rows, transfer.open_delta(sealed, keys))
        # An empty delta is still one final part, and two seals never look alike.
        empty = transfer.seal_delta([], keys)
        self.assertEqual(1, len(empty))
        self.assertTrue(empty[0]["final"])
        self.assertEqual([], transfer.open_delta(transfer.delta_part(empty[0])[0], keys))
        self.assertNotEqual(empty[0]["data"], transfer.seal_delta([], keys)[0]["data"])
        # The wrong session key, a cut or a padded delta, and malformed parts are refused.
        for bad in (sealed[:-16], sealed[:-1], sealed + bytes(16), b"", sealed[:16]):
            with self.subTest(length=len(bad)), self.assertRaises(TransferError):
                transfer.open_delta(bad, keys)
        with self.assertRaises(TransferError):
            transfer.open_delta(sealed, other)
        for message in ({"rows": rows, "final": True}, {"data": "", "final": True}, {"data": "AAAA"},
                        {"data": "not base64!", "final": True}, {"data": 5, "final": True},
                        {"data": "AAAA", "final": 1}, {"data": "A" * 20_000, "final": True}):
            with self.subTest(message=str(message)[:30]), self.assertRaises(TransferError):
                transfer.delta_part(message)
        with self.assertRaises(TransferError):
            transfer.seal_delta([["ghdev_x", "N", "", None]] * (transfer.MAX_DELTA_ROWS + 1), keys)


class ArtifactTests(GuardCase):
    def downloads(self):
        return self.state / "downloads"

    def test_rolling_downloads_are_listed_with_the_path_that_serves_them(self):
        (self.downloads() / transfer.POINTER_FILENAME).unlink()
        self.assertEqual([], transfer.served_artifacts(self.state))
        (self.downloads() / "Vemals-Ears-Setup.exe").write_bytes(b"XENO installer")
        (self.downloads() / "vemals-ears-member.zip").write_bytes(b"XENO source")
        (self.downloads() / "vemals-ears-member-1.2.3.zip").write_bytes(b"XENO versioned source")
        (self.downloads() / "vemals-ears-member-9.9.9.zip").write_bytes(b"XENO not the configured version")
        listed = transfer.served_artifacts(self.state)
        self.assertEqual([("Vemals-Ears-Setup.exe", "/download"), ("vemals-ears-member.zip", "/source"),
                          ("vemals-ears-member-1.2.3.zip", "/source/1.2.3")],
                         [(entry["name"], entry["path"]) for entry in listed])
        self.assertEqual(hashlib.sha256(b"XENO installer").hexdigest(), listed[0]["sha256"])
        self.assertEqual(len(b"XENO source"), listed[1]["size"])
        self.assertEqual(listed, transfer.card_artifacts(json.loads(json.dumps(listed))))

    def test_pointer_names_the_installer_and_source_and_a_damaged_pointer_stops_the_move(self):
        with self.assertRaisesRegex(TransferError, "damaged"):
            transfer.served_artifacts(self.state)      # the synthetic pointer is not a valid one
        files = {"Vemals-Ears-Setup-2.0.0.exe": b"XENO pointed installer",
                 "vemals-ears-member-2.0.0.zip": b"XENO pointed source"}
        for name, data in files.items():
            (self.downloads() / name).write_bytes(data)
        with mock.patch("GuildHub.release_pointer.load_release_pointer") as load:
            load.return_value = mock.Mock(
                installer=mock.Mock(path=self.downloads() / "Vemals-Ears-Setup-2.0.0.exe"),
                source=mock.Mock(path=self.downloads() / "vemals-ears-member-2.0.0.zip"))
            listed = transfer.served_artifacts(self.state)
        self.assertEqual([("Vemals-Ears-Setup-2.0.0.exe", "/download"), ("vemals-ears-member-2.0.0.zip", "/source")],
                         [(entry["name"], entry["path"]) for entry in listed])

    def test_card_artifacts_reject_other_paths_duplicates_and_extra_fields(self):
        good = {"name": "Vemals-Ears-Setup.exe", "size": 5, "sha256": "a" * 64, "path": "/download"}
        self.assertEqual([good], transfer.card_artifacts([good]))
        for bad in ([{**good, "path": "/healthz"}], [{**good, "path": "/download/../x"}],
                    [{**good, "path": "https://elsewhere.example/x"}], [{**good, "extra": 1}],
                    [{**good, "size": 0}], [{**good, "name": "../x.exe"}], [good, good],
                    [good, {**good, "name": "other.exe"}], [good] * 9, "list", [{"name": "x"}]):
            with self.subTest(bad=str(bad)[:60]), self.assertRaises(TransferError):
                transfer.card_artifacts(bad)

    def test_archive_is_a_protected_sibling_that_can_be_erased(self):
        archive = transfer.archive_path(self.state)
        self.assertEqual(self.state.parent, archive.parent)
        self.assertFalse(transfer.erase_archive(self.state))
        token = (self.state / "secrets/discord-bot-token").read_bytes()
        with mock.patch.object(setup, "protect_directory") as protect:
            self.assertEqual(archive, transfer.archive_state(self.state))
        protect.assert_called_once_with(archive)
        self.assertFalse(self.state.exists())
        self.assertEqual(token, (archive / "secrets/discord-bot-token").read_bytes())
        # A second archive never overwrites the first.
        self.state.mkdir()
        with self.assertRaisesRegex(TransferError, "older archive"):
            transfer.archive_state(self.state)
        overwritten = []
        erase = transfer._erase
        with mock.patch.object(transfer, "_erase", side_effect=lambda path: (overwritten.append(path.name),
                                                                              erase(path))[1]):
            self.assertTrue(transfer.erase_archive(self.state))
        self.assertFalse(archive.exists())
        for name in ("discord-bot-token", "pepper", "publisher-token", "guild-hub.sqlite3", "consent.json",
                     "relay.env"):
            self.assertIn(name, overwritten)

    @unittest.skipUnless(os.name == "nt", "the state path rules are the Windows ones")
    def test_discard_published_only_removes_a_pending_host(self):
        with self.assertRaisesRegex(TransferError, "not a pending transfer"):
            transfer.discard_published(self.state)
        self.assertTrue((self.state / "relay.env").is_file())
        self.settings(activation="active")
        with self.assertRaisesRegex(TransferError, "not a pending transfer"):
            transfer.discard_published(self.state)
        self.settings(activation="pending")
        self.assertTrue(transfer.discard_published(self.state))
        self.assertFalse(self.state.exists())
        self.assertFalse(transfer.discard_published(self.state))


class NetworkEdgeTests(unittest.TestCase):
    def test_one_spelling_of_the_public_origin_and_no_code_in_it(self):
        for typed in ("ears.example.org", " EARS.example.org ", "https://ears.example.org",
                      "https://ears.example.org/", "ears.example.org:443"):
            self.assertEqual("https://ears.example.org", transfer_net.public_origin(typed))
        self.assertEqual("https://ears.example.org:8443", transfer_net.public_origin("ears.example.org:8443"))
        self.assertEqual("wss://ears.example.org/v1/transfer-claim",
                         transfer_net.claim_url("https://ears.example.org"))
        for typed in ("", "ears.example.org/K7QM-2XHD", "http://ears.example.org", "127.0.0.1",
                      "user@ears.example.org", "ears.example.org?code=K7QM2XHD", "ears.example.org#x", None):
            with self.subTest(typed=typed), self.assertRaises(setup.SetupError):
                transfer_net.public_origin(typed)

    def serve(self, responses):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                status, body = responses[self.path].pop(0) if len(responses[self.path]) > 1 \
                    else responses[self.path][0]
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *arguments):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return "http://127.0.0.1:" + str(server.server_address[1])

    def test_download_is_bounded_by_the_authenticated_size_and_retries_a_busy_relay(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        data = b"XENO download " * 1000
        origin = self.serve({"/download": [(503, b"busy"), (503, b"busy"), (200, data)],
                             "/big": [(200, data + b"extra")], "/short": [(200, data[:-1])],
                             "/missing": [(404, b"none")]})
        target = folder / "file.bin"
        pauses = []
        digest = transfer_net.fetch(origin + "/download", target, len(data), sleep=pauses.append)
        self.assertEqual(hashlib.sha256(data).hexdigest(), digest)
        self.assertEqual(data, target.read_bytes())
        self.assertEqual(2, len(pauses))
        for path in ("/big", "/short", "/missing"):
            with self.subTest(path=path), self.assertRaises(OSError):
                transfer_net.fetch(origin + path, folder / "other.bin", len(data), sleep=pauses.append)
            self.assertFalse((folder / "other.bin").exists())
        self.assertEqual(2, len(pauses))      # only a busy relay is retried

    def test_public_health_means_a_relay_answered(self):
        origin = self.serve({"/healthz": [(200, b'{"status": "ok", "service": "vemals-ears"}')]})
        self.assertTrue(transfer_net.public_answers(origin))
        for body in (b'{"status": "ok"}', b"<html>origin unreachable</html>", b""):
            self.assertFalse(transfer_net.public_answers(self.serve({"/healthz": [(200, body)]})))
        self.assertFalse(transfer_net.public_answers(self.serve({"/healthz": [(502, b"bad gateway")]})))
        self.assertFalse(transfer_net.public_answers("http://127.0.0.1:1"))

    def state(self, origin, *, online=True, probes=1):
        return transfer_net.public_state(origin, timeout=2.0, probes=probes, sleep=self.pauses.append,
                                         online=lambda timeout: self.asked.append(timeout) or online)

    def test_public_state_needs_positive_evidence_before_it_says_quiet(self):
        self.pauses, self.asked = [], []
        health = b'{"status": "ok", "service": "vemals-ears"}'
        self.assertEqual("answers", self.state(self.serve({"/healthz": [(200, health)]})))
        # The address was reached and nothing is connected behind it: the
        # edge says the tunnel or origin is gone, or another site's page.
        for reply in ((530, b"origin unreachable"), (521, b"web server is down"), (522, b"timed out"),
                      (523, b"origin unreachable"), (200, b"<html>parked</html>"), (200, b'{"status": "ok"}'),
                      (200, b"")):
            with self.subTest(reply=reply):
                self.assertEqual("quiet", self.state(self.serve({"/healthz": [reply]})))
        self.assertEqual([], self.asked)      # an HTTP answer needs no second opinion
        # Nothing listens at the address. That is what a dead PC behind the
        # operator's own forwarding looks like, and also what every address
        # looks like from a PC that is offline: only proof that this PC is
        # online tells the two apart.
        self.assertEqual("quiet", self.state("http://127.0.0.1:1", online=True))
        self.assertEqual("unknown", self.state("http://127.0.0.1:1", online=False))
        self.assertEqual(2, len(self.asked))

    def test_public_state_never_takes_an_access_denied_or_ambiguous_status_as_quiet(self):
        # Astra finding 2: a proxy or WAF in front of a still-running relay,
        # a rate limit, or a tunnel connector that is live while its relay is
        # down says nothing about the old host being gone.
        self.pauses, self.asked = [], []
        for status in (401, 403, 404, 407, 429, 451, 500, 502, 503, 504, 520, 524):
            with self.subTest(status=status):
                self.assertEqual("unknown", self.state(self.serve({"/healthz": [(status, b"XENO denied")]})))
                self.assertEqual("unknown", self.state(self.serve({"/healthz": [(status, b"XENO denied")] * 2}),
                                                       probes=2))
        self.assertEqual([], self.asked)      # being online does not rescue these

    def test_public_state_never_takes_a_dns_or_tls_failure_as_quiet(self):
        import socket
        import ssl
        import urllib.error
        self.pauses, self.asked = [], []
        failures = (urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed")),
                    urllib.error.URLError(ssl.SSLCertVerificationError("certificate verify failed")),
                    urllib.error.URLError("unknown url type"), ValueError("unknown url type"))
        for failure in failures:
            opener = mock.Mock()
            opener.open.side_effect = failure
            with self.subTest(failure=repr(failure)[:40]), mock.patch.object(transfer_net, "_opener",
                                                                              return_value=opener):
                self.assertEqual("unknown", self.state("https://ears.example.org", online=True))
                self.assertFalse(transfer_net.public_answers("https://ears.example.org"))
        self.assertEqual([], self.asked)      # being online does not rescue these
        # The real control probe: any HTTP reply proves this PC is online, no reply does not.
        for failure, expected in ((None, True), (urllib.error.HTTPError("u", 403, "no", {}, io.BytesIO()), True),
                                  (urllib.error.URLError(socket.gaierror(11001, "x")), False),
                                  (TimeoutError("timed out"), False)):
            opener = mock.MagicMock()
            if failure is not None:
                opener.open.side_effect = failure
            with self.subTest(control=repr(failure)[:30]), mock.patch.object(transfer_net, "_opener",
                                                                              return_value=opener):
                self.assertIs(expected, transfer_net._online(2.0))
                self.assertEqual(transfer_net.CONTROL_URL, opener.open.call_args.args[0])
        self.assertTrue(transfer_net.CONTROL_URL.startswith("https://discord.com/"))
        self.assertNotIn("?", transfer_net.CONTROL_URL)

    def test_public_state_probes_more_than_once_and_one_answer_is_enough(self):
        self.pauses, self.asked = [], []
        health = b'{"status": "ok", "service": "vemals-ears"}'
        flapping = self.serve({"/healthz": [(530, b"down"), (200, health)]})
        self.assertEqual("answers", self.state(flapping, probes=3))
        self.assertEqual(1, len(self.pauses))
        self.assertEqual("quiet", self.state(self.serve({"/healthz": [(530, b"down")] * 3}), probes=3))
        self.assertEqual(3, len(self.pauses))
        # One probe that could not tell spoils a run of quiet ones.
        states = iter(("quiet", "unknown", "quiet"))
        with mock.patch.object(transfer_net, "_probe", side_effect=lambda *arguments: next(states)):
            self.assertEqual("unknown", self.state("https://ears.example.org", probes=3))
        self.assertGreaterEqual(transfer_net.STATE_PROBES, 2)

    def test_identity_card_validation_rejects_anything_unexpected(self):
        card = {"guild_name": "XENO Test Guild", "guild_id": GUILD, "application_id": APPLICATION,
                "public_origin": "https://ears.example.org", "port": 8770, "tunnel_enabled": True,
                "host_version": "0.5.17", "artifacts": []}
        self.assertEqual(card, transfer_flow._card(json.loads(json.dumps(card))))
        for change in ({"guild_id": "1"}, {"port": "8770"}, {"port": 0}, {"tunnel_enabled": 1},
                       {"public_origin": "https://ears.example.org/"}, {"public_origin": "http://ears.example.org"},
                       {"guild_name": "bad\x00name"}, {"host_version": ""}, {"bot_token": "XENO"},
                       {"artifacts": [{"name": "x.exe", "size": 1, "sha256": "a" * 64, "path": "/admin"}]}):
            with self.subTest(change=change), self.assertRaises(TransferError):
                transfer_flow._card({**card, **change})
        with self.assertRaises(TransferError):
            transfer_flow._card({key: value for key, value in card.items() if key != "port"})
        self.assertNotIn("code", transfer_flow.CARD_KEYS)


if __name__ == "__main__":
    unittest.main()
