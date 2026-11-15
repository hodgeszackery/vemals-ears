"""Whole host moves with fakes: a real in-process relay, an in-memory service manager.

Synthetic values and temp directories only. No Windows service is touched:
FakeHost answers every service command from memory, and refuses a start the
way Windows and the real service process would.
"""

import asyncio
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
from urllib.parse import urlsplit

from websockets.asyncio.server import serve

from GuildHub.protocol import CAPTION_SUBPROTOCOLS
from GuildHub.server import WEBSOCKET_LOGGER, GuildHubServer, ServerConfig
from GuildHub.store import CredentialStore
from Hosting import control, service, setup, transfer, transfer_flow, transfer_net, transfer_pake
from Hosting.transfer import TransferError

from tests.test_host_transfer import APPLICATION, CONSENT, GUILD, MAPPING, PEPPER, PUBLISHER, make_state


RELAY, WORKER, TUNNEL = control.RELAY, control.WORKER, control.TUNNEL
INSTALLER = b"XENO synthetic member installer " * 4096
SOURCE = b"XENO synthetic source bundle " * 512
WINDOWS = unittest.skipUnless(os.name == "nt", "Windows AES, ACL and rename integration")


class Crash(BaseException):
    """The Host app dying: nothing catches it, exactly like a killed process."""


def frame_kind(frame: bytes) -> str:
    if frame[:4] == transfer.CONTROL_MAGIC:
        return json.loads(frame[transfer.CONTROL_HEADER.size:-32])["type"]
    if frame[:4] == transfer.FRAME_MAGIC:
        return "bundle"
    return "pre:" + json.loads(frame)["type"]


class SpySocket:
    """Records what the old host sends, and can lose a frame or kill the app at one."""

    def __init__(self, inner):
        self.inner, self.sent = inner, []
        self.raw = {}       # kind -> control frames as they went on the wire
        self.crash_on = self.drop = None
        self.ttl_seconds = inner.ttl_seconds

    def control(self, kind, **fields):
        self.sent.append("control:" + kind)
        self.inner.control(kind, **fields)

    def send(self, frame):
        kind = frame_kind(frame)
        if kind == self.crash_on:
            raise Crash()
        if kind == self.drop:
            raise transfer_net.Closed()
        if not (kind == "bundle" and self.sent and self.sent[-1] == "bundle"):
            self.sent.append(kind)
        if kind != "bundle":
            self.raw.setdefault(kind, []).append(frame)
        self.inner.send(frame)

    def receive(self, timeout):
        return self.inner.receive(timeout)

    def close(self):
        self.inner.close()


class FakeHost(control.HostController):
    """The real controller over an in-memory service manager."""

    def __init__(self, folder, state, label, log, watch=lambda: None):
        (folder / "Program/app").mkdir(parents=True, exist_ok=True)
        super().__init__(folder / "Program/app", state)
        self.label, self.log, self.watch = label, log, watch
        self.scm, self.hooks = {}, {}
        self.fail_install = False
        self.before_install = None

    def _service_status(self, name):
        return self.scm[name]["status"] if name in self.scm else "Not installed"

    def _nssm(self, *arguments, check=True):
        action, name = arguments[0], arguments[1]
        entry = self.scm.get(name)
        reply = SimpleNamespace(returncode=0, stdout="", stderr="")
        if action == "install":
            assert entry is None, "installed over an existing fake service"
            self.scm[name] = {"status": "Stopped",
                              "settings": {"Application": arguments[2], "Start": control.AUTOMATIC}}
        elif entry is None:
            raise control.HostError("fake: the service is not installed")
        elif action == "get":
            reply.stdout = entry["settings"].get(arguments[2], "") + "\n"
            return reply
        elif action == "set":
            entry["settings"][arguments[2]] = arguments[3]
            return reply
        elif action == "start":
            if entry["settings"]["Start"] == control.DISABLED:
                raise control.HostError("fake: Windows does not start a disabled service")
            if name != TUNNEL:
                # The first thing the real service process does.
                try:
                    service.validate_activation(service.load_configuration(self.state),
                                                "relay" if name == RELAY else "worker")
                except service.ConfigurationError as error:
                    raise control.HostError("fake: the service process refused: " + str(error)) from None
            entry["status"] = "Running"
        elif action == "stop":
            entry["status"] = "Stopped"
        elif action == "remove":
            del self.scm[name]
        else:
            raise AssertionError(arguments)
        self.log.append((self.label, action, name))
        hook = self.hooks.get((action, name))
        if hook:
            hook()
        self.watch()
        return reply

    def _run(self, arguments, *, timeout=30, check=True):
        arguments = [str(item) for item in arguments]
        assert any(item.endswith("Install-Host.ps1") for item in arguments), arguments
        staged = "-Staged" in arguments
        failed = SimpleNamespace(returncode=1, stdout="", stderr="")
        if self.before_install:
            self.before_install()
        if self.fail_install:
            return failed
        try:
            values = service.load_configuration(self.state)
            (service.validate_staged if staged else service.validate_activation)(values)
        except service.ConfigurationError:
            return failed
        for name, role in ((RELAY, "relay"), (WORKER, "worker")):
            self._nssm("install", name, str(self.python))
            self._nssm("set", name, "AppDirectory", str(self.package_root))
            self._nssm("set", name, "AppParameters",
                       '-B -m Hosting.service --state-directory "' + str(self.state) + '" ' + role)
            self._nssm("set", name, "Start", control.DISABLED if staged else control.AUTOMATIC)
        if not staged:
            for name in (RELAY, WORKER):
                self._nssm("start", name)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def prepare_staged_runtime(self, staging, progress=lambda message: None):
        runtime = Path(staging) / "profile/WoWVoiceCaptions/runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / "XENO-runtime-marker").write_text("prepared", encoding="ascii")

    def staged_runtime_ready(self, staging):
        return (Path(staging) / "profile/WoWVoiceCaptions/runtime/XENO-runtime-marker").is_file()

    def start_modes(self):
        return {name: entry["settings"]["Start"] for name, entry in self.scm.items()}

    def statuses(self):
        return {name: entry["status"] for name, entry in self.scm.items()}


class Relay:
    """A real relay instance on a loopback port, configured exactly as Hosting.service derives it."""

    def __init__(self, state):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.running = False
        self._call(self._start(state))

    def _call(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(30)

    async def _start(self, state):
        values = service.load_configuration(state)
        self.store = CredentialStore(values["VEMALS_EARS_DATABASE"],
                                     Path(values["VEMALS_EARS_PEPPER_FILE"]).read_text(encoding="ascii").strip())
        self.store.initialize()
        self.application = GuildHubServer(self.store, ServerConfig(
            guild_id=values["VEMALS_EARS_GUILD_ID"], guild_name=values["VEMALS_EARS_GUILD_NAME"],
            publisher_token=Path(values["VEMALS_EARS_PUBLISHER_TOKEN_FILE"]).read_text(encoding="ascii").strip(),
            public_server_url=values["VEMALS_EARS_PUBLIC_SERVER_URL"],
            client_download_url=values["VEMALS_EARS_CLIENT_DOWNLOAD_URL"],
            auth_timeout=5.0, connection_limit=32,
            download_file=values.get("VEMALS_EARS_DOWNLOAD_FILE", ""),
            transfer_key_file=values["VEMALS_EARS_TRANSFER_KEY_FILE"],
            transfer_freeze_file=values["VEMALS_EARS_TRANSFER_FREEZE_FILE"]))
        self.server = await serve(
            self.application.handle, "127.0.0.1", 0, subprotocols=list(CAPTION_SUBPROTOCOLS),
            process_request=self.application.process_request, compression=None, max_size=16_384,
            logger=WEBSOCKET_LOGGER)
        port = self.server.sockets[0].getsockname()[1]
        self.ws, self.http = "ws://127.0.0.1:" + str(port), "http://127.0.0.1:" + str(port)
        self.running = True

    async def _stop(self):
        self.server.close()
        await self.server.wait_closed()

    def stop(self):
        if self.running:
            self.running = False
            self._call(self._stop())

    def close(self):
        self.stop()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)
        self.loop.close()


class FlowCase(unittest.TestCase):
    """Two PCs: A hosts, B is an empty Host install."""

    protect = False

    def setUp(self):
        if not self.protect:
            # Real ACL calls are exercised by the full-move test; elsewhere they only cost time.
            self.enterContext(mock.patch.object(setup, "protect_directory"))
            self.enterContext(mock.patch.object(setup, "restrict_descendants"))
            self.enterContext(mock.patch.object(setup, "protect_tree"))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.log, self.violations = [], []
        state, self.device = make_state(self.root / "A")
        (state / "downloads" / transfer.POINTER_FILENAME).unlink()
        (state / "downloads/Vemals-Ears-Setup.exe").write_bytes(INSTALLER)
        (state / "downloads/vemals-ears-member.zip").write_bytes(SOURCE)
        self.a = FakeHost(self.root / "A", state, "A", self.log, self.check)
        self.b = FakeHost(self.root / "B", self.root / "B" / "VemalsEarsHost", "B", self.log, self.check)
        self.a.install_services()
        self.relays = []
        self.relay = self.start_relay(self.a)
        self.spy = None

    def tearDown(self):
        for relay in self.relays:
            relay.close()
        self.assertEqual([], self.violations)

    def start_relay(self, host):
        relay = Relay(host.state)
        self.relays.append(relay)
        host.hooks[("stop", RELAY)] = relay.stop
        return relay

    # -- the invariant -------------------------------------------------------

    @staticmethod
    def startable(host):
        """Could this PC's caption worker run now, by any route?"""
        worker = host.scm.get(WORKER)
        if worker is None:
            return False
        if worker["status"] == "Running":
            return True
        if worker["settings"].get("Start") == control.DISABLED:
            return False
        try:
            service.validate_activation(service.load_configuration(host.state), "worker")
        except service.ConfigurationError:
            return False
        return True

    def check(self, where=""):
        if self.startable(self.a) and self.startable(self.b):
            self.violations.append("two startable workers " + where)

    # -- one move ------------------------------------------------------------

    def sender(self, old, relay, **arguments):
        def open_offer(port, key):
            self.spy = SpySocket(transfer_net.OfferSocket(port, key, url=relay.ws + "/v1/transfer-offer"))
            return self.spy
        return transfer_flow.Sender(old, open_offer=open_offer, **arguments)

    def receiver(self, new, relay, **arguments):
        arguments.setdefault("public_state", lambda origin: "answers" if relay.running else "quiet")
        arguments.setdefault("port_free", lambda port: True)
        arguments.setdefault("sleep", lambda seconds: None)
        return transfer_flow.Receiver(
            new, open_claim=lambda origin: transfer_net.ClaimSocket(relay.ws + "/v1/transfer-claim"),
            fetch=lambda url, target, size: transfer_net.fetch(relay.http + urlsplit(url).path, target, size),
            **arguments)

    def move(self, old, new, relay, *, sender=None, receiver=None, sender_progress=None,
             receiver_progress=None, before_run=None, prepare=True):
        sender = sender or self.sender(old, relay)
        receiver = receiver or self.receiver(new, relay)
        outcome = {"sender_stages": [], "receiver_stages": []}
        offer = sender.open_offer()
        outcome["offer"] = offer
        if before_run:
            before_run()

        def stage(name, key, callback):
            outcome[key].append(name)
            self.check(key + ":" + name)
            if callback:
                callback(name)

        def run_sender():
            try:
                outcome["sender"] = sender.run(lambda name: stage(name, "sender_stages", sender_progress))
            except BaseException as error:  # noqa: BLE001 - reported to the test
                outcome["sender_error"] = error

        thread = threading.Thread(target=run_sender, daemon=True)
        thread.start()
        try:
            if prepare:
                receiver.prepare()
            outcome["card"] = receiver.connect(offer["hostname"], offer["code"])
            outcome["receiver"] = receiver.accept(lambda name: stage(name, "receiver_stages", receiver_progress))
        except TransferError as error:
            outcome["receiver_error"] = error
        thread.join(90)
        self.assertFalse(thread.is_alive(), "the sender never finished")
        return outcome

    def store(self, host):
        return CredentialStore(host.state / "relay/guild-hub.sqlite3", PEPPER)

    def receipt(self, host):
        return transfer.TransferRecords(transfer.records_directory(host.state)).read()

    def assert_hosting(self, host):
        """Running and startable exactly as a normal host."""
        self.assertEqual({RELAY: "Running", WORKER: "Running", TUNNEL: "Running"}, host.statuses())
        self.assertEqual({control.AUTOMATIC}, set(host.start_modes().values()))
        host.start_services()
        self.assertTrue(self.startable(host))

    def assert_empty(self, host):
        self.assertEqual({}, host.scm)
        self.assertFalse(host.state.exists())


@WINDOWS
class FullMoveTests(FlowCase):
    protect = True

    def test_a_host_moves_to_an_empty_pc_then_back_onto_the_pc_that_holds_its_archive(self):
        store = self.store(self.a)
        store.set_quick_chat_name(self.device.device_id, "Before Move")
        leftover = transfer.records_directory(self.a.state) / (transfer.SNAPSHOT_PREFIX + "left-by-a-crash")
        leftover.mkdir(parents=True)
        (leftover / "relay.sqlite3").write_bytes(b"XENO plaintext copy")
        seen = {}

        def during_import(name):
            if name == "importing":
                # The snapshot is on its way; the old relay is frozen for
                # pairing, but a typed-chat profile write still lands.
                seen["frozen"] = self.relay.application.transfer_frozen
                seen["flag"] = (self.a.state / "transfer-frozen").is_file()
                seen["old_worker"] = self.a.scm[WORKER]["status"], self.a.scm[WORKER]["settings"]["Start"]
                seen["old_modes"] = self.a.start_modes()
                seen["new_services"] = dict(self.b.scm)
                self.assertTrue(store.set_quick_chat_name(self.device.device_id, "Late Name"))
            if name == "staged":
                seen["staged_modes"] = self.b.start_modes()
                seen["staged_statuses"] = self.b.statuses()
                seen["pending"] = transfer.activation(self.b.state)
                seen["withheld"] = json.loads((self.b.state / "host-settings.json").read_text())["tunnel_withheld"]
                with self.assertRaisesRegex(control.HostError, "waiting for that PC to release"):
                    self.b.start_services()

        outcome = self.move(self.a, self.b, self.relay, receiver_progress=during_import)
        self.assertNotIn("sender_error", outcome, outcome.get("sender_error"))
        self.assertNotIn("receiver_error", outcome, outcome.get("receiver_error"))

        # The operator saw two separate fields, and only a non-secret card.
        self.assertEqual("ears.example.org", outcome["offer"]["hostname"])
        self.assertRegex(outcome["offer"]["code"], r"^[0-9A-Z]{4}-[0-9A-Z]{4}$")
        self.assertNotIn(outcome["offer"]["code"], outcome["offer"]["hostname"])
        card = outcome["card"]
        self.assertEqual((GUILD, APPLICATION, "XENO Test Guild", "https://ears.example.org", 8770, True),
                         (card["guild_id"], card["application_id"], card["guild_name"], card["public_origin"],
                          card["port"], card["tunnel_enabled"]))
        self.assertEqual(["/download", "/source"], [entry["path"] for entry in card["artifacts"]])
        self.assertNotIn("XENO_synthetic", json.dumps(card))

        # While the bundle travelled: frozen, worker down for good, nothing new registered.
        self.assertEqual((True, True, ("Stopped", control.DISABLED)),
                         (seen["frozen"], seen["flag"], seen["old_worker"]))
        # Only the worker is taken down: the old relay and tunnel carry the
        # move and would come back (frozen) after a reboot.
        self.assertEqual({WORKER: control.DISABLED, RELAY: control.AUTOMATIC, TUNNEL: control.AUTOMATIC},
                         seen["old_modes"])
        self.assertEqual({}, seen["new_services"])
        # A received host's tunnel is withheld from the moment it is staged.
        self.assertTrue(seen["withheld"])
        self.assertEqual(({control.DISABLED}, {"Stopped"}, "pending"),
                         (set(seen["staged_modes"].values()), set(seen["staged_statuses"].values()),
                          seen["pending"]))

        # The old PC: stopped in order, services removed, state archived, receipt moved.
        result = outcome["sender"]
        self.assertEqual(("moved", True), (result["state"], result["acknowledged"]))
        self.assert_empty(self.a)
        archive = transfer.archive_path(self.a.state)
        self.assertEqual(str(archive), result["archive"])
        self.assertTrue((archive / "secrets/discord-bot-token").is_file())
        self.assertFalse((archive / "secrets/transfer-key").exists())
        self.assertFalse((archive / "transfer-frozen").exists())
        self.assertFalse(leftover.exists())
        self.assertEqual([], list(transfer.records_directory(self.a.state).glob(transfer.SNAPSHOT_PREFIX + "*")))
        self.assertEqual("moved", self.receipt(self.a)["state"])
        self.assertFalse(self.relay.running)
        events = [event for event in self.log if event[0] == "A"]
        stops = [event[2] for event in events if event[1] == "stop"]
        self.assertEqual([WORKER, TUNNEL, RELAY], stops)
        sent = self.spy.sent
        self.assertLess(sent.index("control:transfer_confirmed"), sent.index("pre:confirm"))
        self.assertLess(sent.index("pre:confirm"), sent.index("card"))
        self.assertLess(sent.index("control:transfer_freeze"), sent.index("bundle"))
        self.assertLess(sent.index("bundle"), sent.index("delta"))
        self.assertEqual(["release", "control:transfer_maintenance"], sent[-2:])
        # The delta names a member's device and typed-chat name: never in clear.
        delta = b"".join(self.spy.raw["delta"])
        for private in (b"Late Name", b"Late", self.device.device_id.encode("ascii")):
            self.assertNotIn(private, delta)
        self.assertNotIn(b"rows", delta)
        self.assertLess(self.log.index(("A", "stop", WORKER)), self.log.index(("B", "install", RELAY)))
        self.assertLess(self.log.index(("B", "install", WORKER)), self.log.index(("A", "stop", TUNNEL)))

        # The new PC: active on RELEASE, relay then worker, tunnel last.
        self.assertEqual({"state": "active", "tunnel_enabled": True, "tunnel_started": True,
                          "public_still_answering": False, "public_unchecked": False, "forwarding": False,
                          "override": False}, outcome["receiver"])
        self.assertFalse(self.b.read_settings()["tunnel_withheld"])
        self.assertNotIn("tunnel_withheld", json.loads((self.b.state / "host-settings.json").read_text()))
        # The new PC's confirmation is kept in the old PC's receipt.
        self.assertIs(True, self.receipt(self.a)["details"]["acknowledged"])
        self.assertTrue(transfer_flow.status(self.a)["acknowledged"])
        self.assertEqual([("B", "start", RELAY), ("B", "start", WORKER), ("B", "start", TUNNEL)],
                         [event for event in self.log if event[:2] == ("B", "start")])
        self.assertLess(self.log.index(("A", "stop", WORKER)), self.log.index(("B", "start", WORKER)))
        self.assert_hosting(self.b)
        self.assertEqual("active", transfer.activation(self.b.state))
        self.assertEqual(("receiver", "active"), (self.receipt(self.b)["role"], self.receipt(self.b)["state"]))
        self.assertFalse(transfer.staging_path(self.b.state).exists())
        self.assertTrue((self.b.state / "profile/WoWVoiceCaptions/runtime/XENO-runtime-marker").is_file())
        self.assertEqual(INSTALLER, (self.b.state / "downloads/Vemals-Ears-Setup.exe").read_bytes())
        self.assertEqual(SOURCE, (self.b.state / "downloads/vemals-ears-member.zip").read_bytes())
        self.assertEqual(CONSENT, json.loads((self.b.state / "consent/consent.json").read_text()))
        self.assertEqual(MAPPING, json.loads((self.b.state / "consent/mapping.json").read_text()))
        self.assertFalse((self.b.state / "consent/sessions.json").exists())
        self.assertFalse((self.b.state / "logs/worker.log").exists())
        self.assertEqual(PUBLISHER, (self.b.state / "secrets/publisher-token").read_text().strip())
        values = service.load_configuration(self.b.state)
        self.assertEqual(str(self.b.state / "secrets" / "transfer-key"), values["VEMALS_EARS_TRANSFER_KEY_FILE"])
        # Members keep their pairing, and the profile write made mid-move arrived.
        record = self.store(self.b).authenticate_device_record(self.device.device_id, self.device.device_token)
        self.assertEqual("Late Name", record.quick_chat_name)
        self.assertEqual(transfer_pake.parse_release_code(result["release_code"]),
                         transfer.TransferRecords(transfer.records_directory(self.a.state)).release_secret())

        # Updating the old PC later cannot bring the bot back.
        install = self.a.install_root
        (install / "host-install.json").write_text(json.dumps(
            {"schema": 1, "product": "vemals-ears-host", "test_build": False}), encoding="ascii")
        (install / "host-resume.json").write_text(json.dumps(
            {"schema": 1, "mode": "standalone", "running": [RELAY, WORKER, TUNNEL]}), encoding="ascii")
        with mock.patch.object(control.HostController, "_run") as run, \
             mock.patch.object(control.HostController, "_nssm") as nssm:
            control.installer_lifecycle(install, self.a.state)
            control.installer_lifecycle(install, self.a.state, resume=True)
        run.assert_not_called()
        nssm.assert_not_called()
        self.assertFalse((install / "host-resume.json").exists())
        self.assertEqual("moved", self.a.transfer_hold())
        with self.assertRaisesRegex(control.HostError, "locked archive"):
            self.a.configure({"hostname": "ears.example.org", "guild_id": GUILD, "application_id": APPLICATION,
                              "bot_token": "XENO_SYNTHETIC_BOT_TOKEN_ONLY", "tunnel_enabled": False,
                              "client_download_url": "https://downloads.example.org/Setup.exe"})
        self.assertFalse(self.a.state.exists())

        # Reverse: the PC that holds the archive is not empty until it is erased.
        status = transfer_flow.status(self.a)
        self.assertEqual(("moved", result["release_code"]), (status["view"], status["release_code"]))
        with self.assertRaisesRegex(TransferError, "Erase the archive first"):
            self.receiver(self.a, self.relay).preflight()
        with self.assertRaisesRegex(TransferError, "holds no archive"):
            transfer_flow.erase_archive(self.b)
        transfer_flow.erase_archive(self.a)
        self.assertFalse(archive.exists())
        self.assertEqual({}, self.receipt(self.a))
        self.assertEqual("", self.a.transfer_hold())

        self.store(self.b).set_quick_chat_name(self.device.device_id, "On The New PC")
        back = self.move(self.b, self.a, self.start_relay(self.b))
        self.assertNotIn("sender_error", back, back.get("sender_error"))
        self.assertNotIn("receiver_error", back, back.get("receiver_error"))
        self.assertEqual("moved", back["sender"]["state"])
        self.assertEqual("active", back["receiver"]["state"])
        self.assert_empty(self.b)
        self.assert_hosting(self.a)
        record = self.store(self.a).authenticate_device_record(self.device.device_id, self.device.device_token)
        self.assertEqual("On The New PC", record.quick_chat_name)
        self.assertEqual(INSTALLER, (self.a.state / "downloads/Vemals-Ears-Setup.exe").read_bytes())


@WINDOWS
class FailureTests(FlowCase):
    def assert_restored(self, outcome):
        self.assertIn("Nothing was moved", str(outcome["sender_error"]))
        self.assert_hosting(self.a)
        self.assertEqual("cancelled", self.receipt(self.a)["state"])
        records = transfer.records_directory(self.a.state)
        self.assertFalse((records / "release-secret").exists())
        self.assertEqual([], list(records.glob(transfer.SNAPSHOT_PREFIX + "*")))
        self.assertFalse((self.a.state / "transfer-frozen").exists())
        self.assertFalse((self.a.state / "secrets/transfer-key").exists())
        self.assertFalse(self.relay.application.transfer_frozen)
        self.assertEqual("", transfer_flow.status(self.a)["view"])

    def test_three_wrong_codes_end_the_offer_and_the_old_host_never_confirms_first(self):
        sender = self.sender(self.a, self.relay)
        offer = sender.open_offer()
        outcome = {}
        thread = threading.Thread(target=lambda: outcome.update(error=self.capture(sender.run)), daemon=True)
        thread.start()
        wrong = "0000-0000" if offer["code"] != "0000-0000" else "1111-1111"
        for _attempt in range(3):
            with self.assertRaisesRegex(TransferError, "did not match"):
                self.receiver(self.b, self.relay).connect("ears.example.org", wrong)
        thread.join(30)
        self.assertRegex(str(outcome["error"]), "wrongly too many times.*Nothing was moved")
        # Three hellos answered, three wrong confirmations reported, and
        # nothing derived from the session ever left the old host.
        self.assertEqual(["pre:hello", "control:transfer_failed"] * 3, self.spy.sent[:6])
        self.assertNotIn("pre:confirm", self.spy.sent)
        self.assertNotIn("card", self.spy.sent)
        with self.assertRaisesRegex(TransferError, "No transfer is waiting"):
            self.receiver(self.b, self.relay).connect("ears.example.org", offer["code"])
        self.assertEqual("cancelled", self.receipt(self.a)["state"])
        self.assertEqual([], [event for event in self.log if event[1] == "stop"])
        self.assert_hosting(self.a)
        self.assertFalse((self.a.state / "secrets/transfer-key").exists())
        self.assert_empty(self.b)

    @staticmethod
    def capture(call):
        try:
            return call()
        except TransferError as error:
            return error

    def test_new_pc_fails_before_staged_then_the_move_is_run_again_from_a_fresh_snapshot(self):
        self.b.fail_install = True
        outcome = self.move(self.a, self.b, self.relay)
        self.assertIn("Nothing was kept on this PC", str(outcome["receiver_error"]))
        self.assert_restored(outcome)
        self.assert_empty(self.b)
        self.assertEqual("discarded", self.receipt(self.b)["state"])
        # The copy had been published (pending) when it failed: all of it is erased.
        self.assertFalse(transfer.staging_path(self.b.state).exists())
        self.assertEqual("", transfer_flow.status(self.b)["view"])

        # The speech runtime went with it. A retry that skips preparing is
        # refused before READY, so the old host is never stopped for a new PC
        # that would then have to download 1.2 GB first.
        self.b.fail_install = False
        stops = len([event for event in self.log if event[1] == "stop"])
        unprepared = self.move(self.a, self.b, self.relay, prepare=False)
        self.assertIn("Speech recognition is not prepared", str(unprepared["receiver_error"]))
        self.assertIn("Nothing was moved", str(unprepared["sender_error"]))
        self.assertEqual(stops, len([event for event in self.log if event[1] == "stop"]))
        self.assertNotIn("control:transfer_freeze", self.spy.sent)
        self.assert_hosting(self.a)
        self.assert_empty(self.b)

        self.b.fail_install = False
        self.assertTrue(self.store(self.a).set_quick_chat_name(self.device.device_id, "After The Failure"))
        again = self.move(self.a, self.b, self.relay)
        self.assertEqual("moved", again["sender"]["state"])
        self.assertEqual("active", again["receiver"]["state"])
        self.assertNotEqual(outcome["offer"]["transfer_id"], again["offer"]["transfer_id"])
        record = self.store(self.b).authenticate_device_record(self.device.device_id, self.device.device_token)
        self.assertEqual("After The Failure", record.quick_chat_name)
        self.assert_empty(self.a)
        self.assert_hosting(self.b)

    def test_a_name_changed_and_changed_back_around_the_snapshot_arrives_as_it_ends(self):
        # Astra round 3: the delta's baseline must be the snapshot that was
        # sent. A name changed to "Changed" just before the snapshot and back
        # to "Original" before the delta is in the snapshot as "Changed"; a
        # baseline read earlier still says "Original" and would omit the row.
        store = self.store(self.a)
        self.assertTrue(store.set_quick_chat_name(self.device.device_id, "Original"))
        collect, delta = transfer.collect_state, transfer.profile_delta

        def collect_state(*arguments, **options):
            self.assertTrue(store.set_quick_chat_name(self.device.device_id, "Changed"))
            return collect(*arguments, **options)

        def profile_delta(*arguments, **options):
            self.assertTrue(store.set_quick_chat_name(self.device.device_id, "Original"))
            return delta(*arguments, **options)

        with mock.patch.object(transfer, "collect_state", collect_state), \
             mock.patch.object(transfer, "profile_delta", profile_delta):
            outcome = self.move(self.a, self.b, self.relay)
        self.assertNotIn("sender_error", outcome, outcome.get("sender_error"))
        self.assertNotIn("receiver_error", outcome, outcome.get("receiver_error"))
        self.assertEqual(("moved", "active"), (outcome["sender"]["state"], outcome["receiver"]["state"]))
        record = self.store(self.b).authenticate_device_record(self.device.device_id, self.device.device_token)
        self.assertEqual("Original", record.quick_chat_name)

    def test_a_name_changed_back_just_after_the_snapshot_still_arrives_as_it_ends(self):
        # Skeptic round 3: pins the baseline to the snapshot bytes, not to a
        # live read after collect_state(). The snapshot holds "Changed"; the
        # live name is "Original" again before any later read, so only a
        # snapshot-based baseline emits the row the new PC needs.
        store = self.store(self.a)
        self.assertTrue(store.set_quick_chat_name(self.device.device_id, "Original"))
        collect = transfer.collect_state

        def collect_state(*arguments, **options):
            self.assertTrue(store.set_quick_chat_name(self.device.device_id, "Changed"))
            try:
                return collect(*arguments, **options)
            finally:
                self.assertTrue(store.set_quick_chat_name(self.device.device_id, "Original"))

        with mock.patch.object(transfer, "collect_state", collect_state):
            outcome = self.move(self.a, self.b, self.relay)
        self.assertNotIn("sender_error", outcome, outcome.get("sender_error"))
        self.assertNotIn("receiver_error", outcome, outcome.get("receiver_error"))
        self.assertEqual(("moved", "active"), (outcome["sender"]["state"], outcome["receiver"]["state"]))
        record = self.store(self.b).authenticate_device_record(self.device.device_id, self.device.device_token)
        self.assertEqual("Original", record.quick_chat_name)

    def test_silent_new_pc_makes_the_old_host_restore_itself(self):
        sender = self.sender(self.a, self.relay)
        sender.silence = 1.5
        restored = threading.Event()
        self.a.hooks[("start", WORKER)] = restored.set
        self.b.before_install = lambda: restored.wait(30)
        outcome = self.move(self.a, self.b, self.relay, sender=sender)
        self.assertTrue(restored.is_set())
        self.assertIn("went silent", str(outcome["sender_error"]))
        # The old host cancelled durably and said so; the new PC erased its copy by itself.
        self.assertIn("Nothing was kept on this PC", str(outcome["receiver_error"]))
        self.assert_restored(outcome)
        self.assert_empty(self.b)
        self.assertEqual("discarded", self.receipt(self.b)["state"])

    def test_cancel_on_the_old_pc_restores_it_and_the_new_pc_discards(self):
        sender = self.sender(self.a, self.relay)
        leftover = transfer.records_directory(self.a.state) / (transfer.SNAPSHOT_PREFIX + "left-mid-move")

        def cancel(name):
            if name == "importing":
                leftover.mkdir()
                (leftover / "relay.sqlite3").write_bytes(b"XENO plaintext copy")
                sender.cancel()

        outcome = self.move(self.a, self.b, self.relay, sender=sender, receiver_progress=cancel)
        self.assertIn("cancelled", str(outcome["sender_error"]))
        self.assertFalse(leftover.exists())      # erased again when the move is cancelled
        self.assert_restored(outcome)
        self.assertIn("receiver_error", outcome)
        self.assert_empty(self.b)

    def test_a_download_that_does_not_match_stops_the_move_before_anything_is_stopped(self):
        receiver = self.receiver(self.b, self.relay)
        receiver._fetch = lambda url, target, size: Path(target).write_bytes(b"X" * size)
        outcome = self.move(self.a, self.b, self.relay, receiver=receiver)
        self.assertIn("does not match the one the old host serves", str(outcome["receiver_error"]))
        self.assertIn("Check the internet connection on both PCs", str(outcome["receiver_error"]))
        self.assertIn("Nothing was moved", str(outcome["sender_error"]))
        self.assertEqual([], [event for event in self.log if event[1] == "stop"])
        self.assertNotIn("bundle", self.spy.sent)
        self.assert_hosting(self.a)
        self.assert_empty(self.b)
        # Nothing secret had arrived, so the prepared runtime waits for the retry.
        staging = transfer.staging_path(self.b.state)
        self.assertTrue((staging / "profile/WoWVoiceCaptions/runtime/XENO-runtime-marker").is_file())
        self.assertFalse(any((staging / name).exists() for name in transfer._state_files()))
        self.assertEqual([], list((staging / "downloads").iterdir()))
        self.assertEqual({}, self.receipt(self.b))

    def test_busy_port_on_the_new_pc_stops_the_move_before_ready(self):
        outcome = self.move(self.a, self.b, self.relay,
                            receiver=self.receiver(self.b, self.relay, port_free=lambda port: False))
        self.assertIn("Port 8770 is in use", str(outcome["receiver_error"]))
        self.assertNotIn("control:transfer_freeze", self.spy.sent)
        self.assert_hosting(self.a)

    def test_lost_release_leaves_both_stopped_until_the_release_code_is_typed(self):
        sender = self.sender(self.a, self.relay)
        outcome = self.move(self.a, self.b, self.relay, sender=sender,
                            before_run=lambda: setattr(self.spy, "drop", "release"))
        # The old host still completes its stop; neither runs: the safe outcome.
        self.assertEqual(("moved", False), (outcome["sender"]["state"], outcome["sender"]["acknowledged"]))
        self.assertEqual({"state": "waiting"}, outcome["receiver"])
        self.assert_empty(self.a)
        self.assertEqual({"Stopped"}, set(self.b.statuses().values()))
        self.assertEqual({control.DISABLED}, set(self.b.start_modes().values()))
        status = transfer_flow.status(self.b)
        self.assertEqual(("waiting", True, 0), (status["view"], status["staged"], status["code_failures"]))
        for action in (self.b.start_services, self.b.install_services, self.b.enable_services):
            with self.assertRaisesRegex(control.HostError, "waiting for that PC to release"):
                action()
        code = outcome["sender"]["release_code"]
        self.assertRegex(code, "^([0-9A-Z]{4}-){13}[0-9A-Z]{4}$")
        wrong = transfer_pake.release_code(bytes(32))
        with self.assertRaisesRegex(TransferError, "does not match") as refused:
            transfer_flow.activate(self.b, release_code=wrong, public_state=lambda origin: "quiet")
        # The old PC is gone: no message may ever send the operator to Discard.
        self.assertNotIn("iscard this transfer", str(refused.exception))
        self.assertIn("Do not discard", str(refused.exception))
        self.assertEqual(1, transfer_flow.status(self.b)["code_failures"])
        self.assertEqual({"Stopped"}, set(self.b.statuses().values()))
        # The old PC is released, stopped and archived: this copy is the only
        # one that can ever host again. However many wrong codes were typed,
        # the genuine code must still start it.
        for _attempt in range(9):
            with self.assertRaisesRegex(TransferError, "does not match"):
                transfer_flow.activate(self.b, release_code=wrong, public_state=lambda origin: "quiet")
        self.assertEqual(10, transfer_flow.status(self.b)["code_failures"])
        # One mistyped symbol is caught by the check group and not counted.
        typo = code[:5] + ("0" if code[5] != "0" else "1") + code[6:]
        with self.assertRaisesRegex(TransferError, "typing mistake"):
            transfer_flow.activate(self.b, release_code=typo, public_state=lambda origin: "quiet")
        self.assertEqual(10, transfer_flow.status(self.b)["code_failures"])
        self.assertEqual("pending", self.receipt(self.b)["state"])
        result = transfer_flow.activate(self.b, release_code=code, public_state=lambda origin: "quiet")
        self.assertEqual(("active", True), (result["state"], result["tunnel_started"]))
        self.assert_hosting(self.b)
        # The new PC never acknowledged, so erasing the archive (and with it
        # the release code) needs the operator's extra confirmation.
        self.assertFalse(transfer_flow.status(self.a)["acknowledged"])
        with self.assertRaisesRegex(TransferError, "never confirmed that it took over"):
            transfer_flow.erase_archive(self.a)
        self.assertTrue(transfer.archive_path(self.a.state).is_dir())
        self.assertEqual("moved", self.receipt(self.a)["state"])
        transfer_flow.erase_archive(self.a, checked_other_pc=True)
        self.assertFalse(transfer.archive_path(self.a.state).exists())

    def crashed_after_staged(self):
        """The old Host app dies after the new PC confirmed its staged copy."""
        outcome = self.move(self.a, self.b, self.relay,
                            before_run=lambda: setattr(self.spy, "crash_on", "delta"))
        self.assertIsInstance(outcome["sender_error"], Crash)
        self.assertEqual({"state": "waiting"}, outcome["receiver"])
        receipt = self.receipt(self.a)
        self.assertEqual(("sending", True), (receipt["state"], receipt["details"]["staged"]))
        # Nothing can run on either PC until a person decides.
        self.assertEqual(("Stopped", control.DISABLED),
                         (self.a.scm[WORKER]["status"], self.a.scm[WORKER]["settings"]["Start"]))
        # Only the worker is disabled. The relay and tunnel keep their start
        # types, and the relay process is allowed to come back after a reboot
        # (frozen by the flag file), so typed chat never waits for the app.
        self.assertEqual({WORKER: control.DISABLED, RELAY: control.AUTOMATIC, TUNNEL: control.AUTOMATIC},
                         self.a.start_modes())
        self.assertTrue((self.a.state / "transfer-frozen").is_file())
        values = service.load_configuration(self.a.state)
        service.validate_activation(values, "relay")
        for process in ("worker", "admin", None):
            with self.assertRaises(service.ConfigurationError):
                service.validate_activation(values, process)
        for name in (RELAY, TUNNEL):     # the PC is switched off
            self.a.scm[name]["status"] = "Stopped"
        self.relay.stop()
        for host in (self.a, self.b):
            self.assertFalse(self.startable(host))
            with self.assertRaises(control.HostError):
                host.start_services()
        self.assertEqual({"view": "interrupted", "can_finish": True},
                         {key: transfer_flow.status(self.a)[key] for key in ("view", "can_finish")})
        return outcome

    def test_old_app_dies_after_staged_and_the_operator_finishes_the_move(self):
        self.crashed_after_staged()
        result = transfer_flow.finish_move(self.a)
        self.assertEqual("moved", result["state"])
        self.assert_empty(self.a)
        self.assertTrue(transfer.archive_path(self.a.state).is_dir())
        self.assertEqual(result, transfer_flow.finish_move(self.a))      # safe to repeat
        with self.assertRaisesRegex(TransferError, "already released"):
            transfer_flow.keep_hosting(self.a)
        activated = transfer_flow.activate(self.b, release_code=result["release_code"],
                                           public_state=lambda origin: "quiet")
        self.assertEqual("active", activated["state"])
        self.assert_hosting(self.b)

    def test_activation_that_stops_before_services_are_enabled_reopens_on_start_this_host(self):
        # Astra finding 4: both activation marks say active while the relay
        # and the worker are still registered disabled. That must reopen on
        # the activation step, never on the ordinary dashboard.
        self.crashed_after_staged()
        code = transfer_flow.finish_move(self.a)["release_code"]
        for failure in (control.HostError("XENO synthetic enable failure"), Crash()):
            with self.subTest(failure=type(failure).__name__), \
                    mock.patch.object(self.b, "enable_services", side_effect=failure), \
                    self.assertRaises((TransferError, Crash)):
                transfer_flow.activate(self.b, release_code=code, public_state=lambda origin: "quiet")
            self.assertEqual("active", self.receipt(self.b)["state"])
            self.assertEqual("active", transfer.activation(self.b.state))
            self.assertEqual({WORKER: control.DISABLED, RELAY: control.DISABLED},
                             {name: mode for name, mode in self.b.start_modes().items() if name != TUNNEL})
            self.assertEqual("activating", transfer_flow.status(self.b)["view"])
        result = transfer_flow.activate(self.b, public_state=lambda origin: "quiet")
        self.assertEqual(("active", True), (result["state"], result["tunnel_started"]))
        self.assert_hosting(self.b)
        self.assertEqual("", transfer_flow.status(self.b)["view"])
        self.assertEqual(result["state"], transfer_flow.activate(self.b, public_state=lambda origin: "quiet")["state"])
        self.assertEqual("", transfer_flow.status(self.b)["view"])

    def test_old_app_dies_after_staged_and_the_operator_keeps_hosting(self):
        self.crashed_after_staged()
        relay = self.start_relay(self.a)
        self.a.hooks[("start", RELAY)] = lambda: None
        asked = []
        self.assertEqual("", transfer_flow.keep_hosting(
            self.a, public_state=lambda origin: asked.append(origin) or "quiet", sleep=lambda seconds: None)["view"])
        self.assertEqual(["https://ears.example.org"], asked)
        self.assert_hosting(self.a)
        self.assertEqual("cancelled", self.receipt(self.a)["state"])
        # R is gone, so the copy on the new PC can never be released.
        with self.assertRaisesRegex(TransferError, "has not been released|no move to finish"):
            transfer_flow.finish_move(self.a)
        state = transfer_flow.override_state(self.b)
        self.assertTrue(state["possible"])
        self.assertGreater(state["wait_remaining"], 0)
        later = lambda: 10 ** 10      # noqa: E731 - long after the wait
        with self.assertRaisesRegex(TransferError, "still answering"):
            transfer_flow.activate_by_override(self.b, attestation=transfer_flow.OVERRIDE_PHRASE, wall=later,
                                               public_state=lambda origin: "answers" if relay.running else "quiet")
        self.assertFalse(self.startable(self.b))
        transfer_flow.discard_pending(self.b)
        self.assert_empty(self.b)
        self.assertEqual("discarded", self.receipt(self.b)["state"])
        self.assertFalse(transfer.staging_path(self.b.state).exists())

    def test_old_app_dies_before_the_new_pc_staged_and_only_keeping_is_offered(self):
        outcome = self.move(self.a, self.b, self.relay,
                            before_run=lambda: setattr(self.spy, "crash_on", "bundle"))
        self.assertIsInstance(outcome["sender_error"], Crash)
        self.assertIn("receiver_error", outcome)
        self.assert_empty(self.b)
        status = transfer_flow.status(self.a)
        self.assertEqual(("interrupted", False), (status["view"], status["can_finish"]))
        with self.assertRaisesRegex(TransferError, "never confirmed a complete copy"):
            transfer_flow.finish_move(self.a)
        self.assertEqual("sending", self.receipt(self.a)["state"])
        transfer_flow.keep_hosting(self.a, public_state=lambda origin: "quiet", sleep=lambda seconds: None)
        self.assert_hosting(self.a)

    def test_dead_old_pc_override_needs_every_condition(self):
        clock = [1_000_000.0]
        receiver = self.receiver(self.b, self.relay, wall=lambda: clock[0])
        outcome = self.move(self.a, self.b, self.relay, receiver=receiver,
                            before_run=lambda: setattr(self.spy, "crash_on", "delta"))
        self.assertEqual({"state": "waiting"}, outcome["receiver"])
        self.relay.stop()       # the old PC is gone
        wall = lambda: clock[0]     # noqa: E731
        quiet = lambda origin: "quiet"    # noqa: E731
        phrase = transfer_flow.OVERRIDE_PHRASE
        self.assertGreater(transfer_flow.OVERRIDE_WAIT_SECONDS, transfer_flow.SENDER_RESTORE_SECONDS)
        self.assertEqual(transfer_flow.OVERRIDE_WAIT_SECONDS,
                         transfer_flow.override_state(self.b, wall=wall)["wait_remaining"])
        with self.assertRaisesRegex(TransferError, "unlocks in 10 minute"):
            transfer_flow.activate_by_override(self.b, attestation=phrase, public_state=quiet, wall=wall)
        clock[0] += transfer_flow.OVERRIDE_WAIT_SECONDS - 1
        with self.assertRaisesRegex(TransferError, "unlocks in 1 minute"):
            transfer_flow.activate_by_override(self.b, attestation=phrase, public_state=quiet, wall=wall)
        clock[0] += 1
        with self.assertRaisesRegex(TransferError, "exactly as shown"):
            transfer_flow.activate_by_override(self.b, attestation="yes", public_state=quiet, wall=wall)
        with self.assertRaisesRegex(TransferError, "still answering"):
            transfer_flow.activate_by_override(self.b, attestation=phrase, public_state=lambda origin: "answers",
                                               wall=wall)
        # Not being able to reach the address is not proof that the old host
        # is gone: an offline new PC cannot override. Anything that is not a
        # clear "quiet" counts as not knowing, a bare False included.
        for unsure in ("unknown", "", None, False, True, "QUIET"):
            with self.subTest(unsure=unsure), self.assertRaisesRegex(TransferError, "cannot reach the internet"):
                transfer_flow.activate_by_override(self.b, attestation=phrase, public_state=lambda origin: unsure,
                                                   wall=wall)
        self.assertEqual("pending", self.receipt(self.b)["state"])
        self.assertEqual({"Stopped"}, set(self.b.statuses().values()))
        self.assertEqual({control.DISABLED}, set(self.b.start_modes().values()))
        result = transfer_flow.activate_by_override(self.b, attestation=phrase, public_state=quiet, wall=wall)
        self.assertEqual(("active", True, True), (result["state"], result["override"], result["tunnel_started"]))
        self.assertEqual({RELAY: "Running", WORKER: "Running", TUNNEL: "Running"}, self.b.statuses())
        self.assertTrue(self.receipt(self.b)["details"]["override"])

    def test_override_is_never_offered_without_a_complete_staged_copy(self):
        self.assertEqual({"possible": False, "wait_remaining": 0}, transfer_flow.override_state(self.b))
        with self.assertRaisesRegex(TransferError, "never finished sending"):
            transfer_flow.activate_by_override(self.b, attestation=transfer_flow.OVERRIDE_PHRASE,
                                               public_state=lambda origin: "quiet", wall=lambda: 10 ** 10)
        # A receipt alone (the app died before the state was published) is not enough either.
        records = transfer.TransferRecords(transfer.records_directory(self.b.state))
        with records.locked():
            records.receive("ab" * 16, bytes(32), sending_complete=True, staged_at=0.0,
                            public_origin="https://ears.example.org", tunnel_enabled=True)
        self.assertFalse(transfer_flow.override_state(self.b, wall=lambda: 10 ** 10)["possible"])
        with self.assertRaisesRegex(TransferError, "never finished sending"):
            transfer_flow.activate_by_override(self.b, attestation=transfer_flow.OVERRIDE_PHRASE,
                                               public_state=lambda origin: "quiet", wall=lambda: 10 ** 10)
        with self.assertRaisesRegex(TransferError, "not completely staged"):
            transfer_flow.activate(self.b, release_code=transfer_pake.release_code(bytes(32)))

    def test_old_public_address_still_answering_keeps_the_new_tunnel_stopped(self):
        receiver = self.receiver(self.b, self.relay, public_state=lambda origin: "answers")
        receiver.quiet_wait = 0.2
        outcome = self.move(self.a, self.b, self.relay, receiver=receiver)
        self.assertEqual("moved", outcome["sender"]["state"])
        result = outcome["receiver"]
        self.assertEqual(("active", False, True), (result["state"], result["tunnel_started"],
                                                   result["public_still_answering"]))
        self.assertEqual({RELAY: "Running", WORKER: "Running", TUNNEL: "Stopped"}, self.b.statuses())
        self.assertEqual(control.DISABLED, self.b.start_modes()[TUNNEL])
        self.assertTrue(self.b.read_settings()["tunnel_withheld"])

        def withheld():
            self.assertEqual(("Stopped", control.DISABLED), (self.b.statuses()[TUNNEL], self.b.start_modes()[TUNNEL]))

        # No ordinary path starts a withheld tunnel: not Start, not the
        # tunnel step of setup, not re-enabling the services.
        self.b.start_services()
        withheld()
        self.b._install_tunnel()
        withheld()
        self.b.enable_services()
        withheld()
        # Nor does saving settings drop the mark.
        self.b.configure({"hostname": "ears.example.org", "guild_id": GUILD, "application_id": APPLICATION,
                          "guild_name": "XENO Test Guild", "tunnel_enabled": True,
                          "client_download_url": "https://ears.example.org/download"})
        self.assertTrue(self.b.read_settings()["tunnel_withheld"])
        still = transfer_flow.start_tunnel(self.b, public_state=lambda origin: "answers")
        self.assertEqual((False, True, False),
                         (still["tunnel_started"], still["public_still_answering"], still["public_unchecked"]))
        withheld()
        # This PC cannot see the address at all: that is not "quiet" either.
        unsure = transfer_flow.start_tunnel(self.b, public_state=lambda origin: "unknown")
        self.assertEqual((False, False, True),
                         (unsure["tunnel_started"], unsure["public_still_answering"], unsure["public_unchecked"]))
        withheld()
        # The mark, not the receipt, is what is remembered: the receipt may be gone by now.
        records = transfer.TransferRecords(transfer.records_directory(self.b.state))
        with records.locked():
            records.erase()
        self.assertEqual({}, self.receipt(self.b))
        self.b._install_tunnel()
        withheld()
        started = transfer_flow.start_tunnel(self.b, public_state=lambda origin: "quiet")
        self.assertTrue(started["tunnel_started"])
        self.assertFalse(self.b.read_settings()["tunnel_withheld"])
        self.assert_hosting(self.b)
        self.assertEqual(("B", "start", TUNNEL), self.log[-1])

    def test_a_new_pc_that_cannot_check_the_old_address_keeps_its_tunnel_withheld(self):
        receiver = self.receiver(self.b, self.relay, public_state=lambda origin: "unknown")
        receiver.quiet_wait = 0.2
        outcome = self.move(self.a, self.b, self.relay, receiver=receiver)
        self.assertEqual("moved", outcome["sender"]["state"])
        result = outcome["receiver"]
        self.assertEqual(("active", False, False, True), (result["state"], result["tunnel_started"],
                                                          result["public_still_answering"],
                                                          result["public_unchecked"]))
        self.assertEqual({RELAY: "Running", WORKER: "Running", TUNNEL: "Stopped"}, self.b.statuses())
        self.assertEqual(control.DISABLED, self.b.start_modes()[TUNNEL])
        self.assertTrue(self.b.read_settings()["tunnel_withheld"])

    def test_another_pc_hosting_stops_keep_hosting_here_and_nothing_is_started(self):
        # The old app died after STAGED and the PC was switched off. The new
        # PC was then started with the emergency step and is hosting.
        clock = [1_000_000.0]
        outcome = self.move(self.a, self.b, self.relay,
                            receiver=self.receiver(self.b, self.relay, wall=lambda: clock[0]),
                            before_run=lambda: setattr(self.spy, "crash_on", "delta"))
        self.assertEqual({"state": "waiting"}, outcome["receiver"])
        self.relay.stop()
        for name in (RELAY, TUNNEL):
            self.a.scm[name]["status"] = "Stopped"
        clock[0] += transfer_flow.OVERRIDE_WAIT_SECONDS
        transfer_flow.activate_by_override(self.b, attestation=transfer_flow.OVERRIDE_PHRASE,
                                           public_state=lambda origin: "quiet", wall=lambda: clock[0])
        self.assertEqual("Running", self.b.statuses()[WORKER])

        # Days later the old PC is switched on again: its relay and tunnel
        # come back by themselves, and the operator picks the first choice.
        for name in (RELAY, TUNNEL):
            self.a.scm[name]["status"] = "Running"
        self.assertEqual("interrupted", transfer_flow.status(self.a)["view"])
        seen = []

        def other_pc_answers(origin):
            # This PC took itself off the address before it asked.
            seen.append((self.a.statuses()[RELAY], self.a.statuses()[TUNNEL]))
            return "answers"

        pauses = []
        with self.assertRaisesRegex(TransferError, "Another PC is already hosting"):
            transfer_flow.keep_hosting(self.a, public_state=other_pc_answers, sleep=pauses.append)
        self.assertEqual([("Stopped", "Stopped")], seen)
        self.assertEqual([transfer_flow.KEEP_SETTLE_SECONDS], pauses)
        self.assertEqual([], self.violations)
        self.assertEqual({RELAY: "Stopped", WORKER: "Stopped", TUNNEL: "Stopped"}, self.a.statuses())
        self.assertEqual({control.DISABLED}, set(self.a.start_modes().values()))
        self.assertFalse(self.startable(self.a))
        self.assertEqual("sending", self.receipt(self.a)["state"])
        self.assertEqual("Running", self.b.statuses()[WORKER])
        # Asking again changes nothing, and neither does not being able to ask.
        with self.assertRaisesRegex(TransferError, "Another PC is already hosting"):
            transfer_flow.keep_hosting(self.a, public_state=other_pc_answers, sleep=pauses.append)
        with self.assertRaisesRegex(TransferError, "cannot reach the internet"):
            transfer_flow.keep_hosting(self.a, public_state=lambda origin: "unknown", sleep=pauses.append)
        self.assertEqual({control.DISABLED}, set(self.a.start_modes().values()))
        self.assertEqual("Stopped", self.a.statuses()[WORKER])
        # What is left for this PC: lock its copy as an archive.
        status = transfer_flow.status(self.a)
        self.assertEqual(("interrupted", True, True), (status["view"], status["can_finish"], status["taken_over"]))
        self.assertEqual("moved", transfer_flow.finish_move(self.a)["state"])
        self.assert_empty(self.a)
        self.assertTrue(transfer.archive_path(self.a.state).is_dir())

    def test_a_copy_seen_hosting_can_be_archived_even_if_its_staged_message_was_lost(self):
        self.crashed_after_staged()
        records = transfer.TransferRecords(transfer.records_directory(self.a.state))
        with records.locked():
            records.note(staged=False)      # STAGED never reached this PC's receipt
        self.assertFalse(transfer_flow.status(self.a)["can_finish"])
        with self.assertRaisesRegex(TransferError, "never confirmed a complete copy"):
            transfer_flow.finish_move(self.a)
        with self.assertRaisesRegex(TransferError, "Another PC is already hosting"):
            transfer_flow.keep_hosting(self.a, public_state=lambda origin: "answers", sleep=lambda seconds: None)
        self.assertTrue(transfer_flow.status(self.a)["can_finish"])
        self.assertEqual("moved", transfer_flow.finish_move(self.a)["state"])
        self.assert_empty(self.a)

    def test_an_unused_offer_is_withdrawn_without_asking_the_network(self):
        sender = self.sender(self.a, self.relay)
        sender.open_offer()
        sender._stack.close()       # the app was closed with the code on screen
        self.spy.close()
        self.assertEqual("offered", self.receipt(self.a)["state"])

        def never(origin):
            raise AssertionError("an unused offer must not probe the public address")

        self.assertEqual("", transfer_flow.keep_hosting(self.a, public_state=never)["view"])
        self.assertEqual("cancelled", self.receipt(self.a)["state"])
        self.assert_hosting(self.a)

    def test_member_downloads_that_changed_since_the_code_was_shown_stop_the_move(self):
        def publish(name):
            if name == "sending":
                # A new member release lands after the card was made.
                (self.a.state / "downloads/vemals-ears-member.zip").write_bytes(SOURCE + b"newer")

        outcome = self.move(self.a, self.b, self.relay, sender_progress=publish)
        self.assertIn("member downloads on this host changed", str(outcome["sender_error"]))
        self.assert_restored(outcome)
        self.assertNotIn("bundle", self.spy.sent)
        self.assertIn("receiver_error", outcome)
        self.assert_empty(self.b)

    def test_close_from_another_thread_only_cancels_once_the_move_runs(self):
        sender = self.sender(self.a, self.relay)
        sender.open_offer()
        waiting, outcome = threading.Event(), {}

        def run():
            outcome["error"] = self.capture(lambda: sender.run(lambda name: waiting.set()))

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.assertTrue(waiting.wait(30))
        with mock.patch.object(sender, "_abandon", side_effect=AssertionError("close() changed the receipt")):
            sender.close()
        thread.join(30)
        self.assertFalse(thread.is_alive())
        self.assertRegex(str(outcome["error"]), "cancelled.*Nothing was moved")
        self.assertEqual("cancelled", self.receipt(self.a)["state"])
        self.assert_hosting(self.a)
        with self.assertRaisesRegex(TransferError, "Show a transfer code first"):
            sender.run()

    def test_staging_left_by_the_receive_page_is_removed_only_when_it_holds_no_host(self):
        self.assertFalse(transfer_flow.discard_unused_staging(self.b))
        self.assertFalse(transfer.records_directory(self.b.state).exists())
        self.receiver(self.b, self.relay).prepare()
        staging = transfer.staging_path(self.b.state)
        self.assertTrue(self.b.staged_runtime_ready(staging))
        (staging / "relay.env").write_text("XENO=1\n", encoding="ascii")
        self.assertFalse(transfer_flow.discard_unused_staging(self.b))
        self.assertTrue(staging.is_dir())
        (staging / "relay.env").unlink()
        self.assertTrue(transfer_flow.discard_unused_staging(self.b))
        self.assertFalse(staging.exists())

    def test_receiving_pc_must_be_empty(self):
        with self.assertRaisesRegex(TransferError, "already has a host.*Uninstall.*Check again"):
            self.receiver(self.a, self.relay).preflight()
        self.b.scm[WORKER] = {"status": "Stopped", "settings": {
            "Application": str(self.b.python), "AppDirectory": str(self.b.package_root),
            "AppParameters": '-B -m Hosting.service --state-directory "' + str(self.b.state) + '" worker',
            "Start": control.AUTOMATIC}}
        with self.assertRaisesRegex(TransferError, "already has a host"):
            self.receiver(self.b, self.relay).preflight()
        del self.b.scm[WORKER]
        self.assertEqual(transfer.staging_path(self.b.state), self.receiver(self.b, self.relay).preflight())

    @WINDOWS
    def test_reused_or_precreated_staging_is_protected_again_before_any_state_is_written(self):
        # Astra finding 1: a staging directory whose ACL step failed, or one
        # something else created, must not be trusted as already private.
        staging = transfer.staging_path(self.b.state)
        with mock.patch.object(setup, "protect_directory", side_effect=setup.SetupError("XENO acl failed")), \
                self.assertRaisesRegex(TransferError, "protected staging directory"):
            self.receiver(self.b, self.relay).preflight()
        self.assertTrue(staging.is_dir())
        (staging / "profile").mkdir()
        (staging / "profile" / "runtime.txt").write_text("XENO runtime", encoding="ascii")
        with mock.patch.object(setup, "protect_tree") as protect:
            self.assertEqual(staging, self.receiver(self.b, self.relay).preflight())
        protect.assert_called_once_with(staging)
        with mock.patch.object(setup, "protect_tree", side_effect=setup.SetupError("XENO acl failed")), \
                self.assertRaisesRegex(TransferError, "Could not protect the staging directory"):
            self.receiver(self.b, self.relay).preflight()

    def test_old_host_must_be_a_running_standalone_host_without_another_move(self):
        self.a.scm[RELAY]["status"] = "Stopped"
        with self.assertRaisesRegex(TransferError, "Start this host first"):
            self.sender(self.a, self.relay).open_offer()
        self.a.scm[RELAY]["status"] = "Running"
        with self.assertRaisesRegex(TransferError, "configured standalone host"):
            self.sender(self.b, self.relay).open_offer()
        first = self.sender(self.a, self.relay)
        first.open_offer()
        with self.assertRaisesRegex(TransferError, "Another transfer operation is running"):
            self.sender(self.a, self.relay).open_offer()
        first.close()
        self.assertEqual("cancelled", self.receipt(self.a)["state"])
        self.assertFalse((self.a.state / "secrets/transfer-key").exists())
        self.assert_hosting(self.a)


if __name__ == "__main__":
    unittest.main()
