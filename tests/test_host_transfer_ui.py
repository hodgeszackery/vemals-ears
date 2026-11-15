"""Move and receive screens: few actions, separate address and code, nothing private shown.

The screens are driven the way an operator drives them, by pressing the
buttons that are on the page, against in-memory fakes of the transfer
sequences. No disk, service or network is touched.
"""

import gc
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
import unittest
from unittest import mock

from Hosting import control, transfer, transfer_ui, wizard
from Hosting.transfer import TransferError
from tests.test_host_takeover_ui import FakeTakeover
from tests.test_host_transfer_flow import WINDOWS, FlowCase
from tests.test_host_wizard import FakeController, WizardUiTests


CODE = "K7QM-2XHD"
RELEASE = "-".join(["XENQ"] * 14)     # the shape of a real release code
PHRASE = "THE OLD PC IS GONE FOR GOOD"
CARD = {"guild_name": "Fixture guild", "guild_id": "123456789012345678",
        "application_id": "234567890123456789", "public_origin": "https://ears.example.org",
        "port": 8770, "tunnel_enabled": True, "host_version": "0.5.17", "artifacts": []}
ACTIVE = {"state": "active", "tunnel_enabled": True, "tunnel_started": True,
          "public_still_answering": False, "public_unchecked": False, "forwarding": False, "override": False}
SENDER_RUN = ("waiting", "connected", "confirmed", "sending", "worker_stopped", "bundle",
              "staged", "released", "stopping", "moved")
RECEIVER_RUN = ("downloads", "receiving", "importing", "services", "staged", "waiting_release",
                "activating", "active")


class FakeSender:
    def __init__(self, owner):
        self.owner = owner
        self.cancelled = threading.Event()

    def open_offer(self):
        self.owner.record("open_offer")
        if self.owner.offer_error:
            raise TransferError(self.owner.offer_error)
        return {"hostname": "ears.example.org", "code": CODE, "ttl_seconds": 600,
                "transfer_id": "ab" * 16, "forwarding": self.owner.forwarding}

    def run(self, progress):
        self.owner.record("run")
        for step in self.owner.sender_steps:
            if isinstance(step, threading.Event):
                while not step.wait(0.01):
                    if self.cancelled.is_set():
                        raise TransferError("The move was cancelled. Nothing was moved. This host keeps running here.")
            else:
                progress(step)
        if self.owner.run_error:
            raise TransferError(self.owner.run_error)
        self.owner.host.settings["configured"] = False
        self.owner.view = {"view": "moved", "release_code": RELEASE, "archive": "archive"}
        return {"state": "moved", "release_code": RELEASE, "archive": "archive", "acknowledged": True}

    def cancel(self):
        self.cancelled.set()


class FakeReceiver:
    def __init__(self, owner):
        self.owner = owner
        self.cancelled = threading.Event()

    def preflight(self):
        self.owner.record("preflight")
        if self.owner.preflight_error:
            raise TransferError(self.owner.preflight_error)

    def prepare(self, progress):
        self.preflight()
        self.owner.record("prepare")
        progress("Downloading the speech model...")
        self.owner.prepare_gate.wait(5)
        progress("Still downloading...")
        if self.owner.prepare_errors:
            raise TransferError(self.owner.prepare_errors.pop(0))
        self.owner.runtime_ready = True

    def connect(self, address, code):
        self.owner.record("connect")
        self.owner.connections.append((address, code))
        if code.replace("-", "").upper() != CODE.replace("-", ""):
            raise TransferError("That code did not match " + code + ". Check the code shown on the old PC.")
        return dict(self.owner.card)

    def accept(self, progress):
        self.owner.record("accept")
        for step in self.owner.receiver_steps:
            if isinstance(step, threading.Event):
                while not step.wait(0.01):
                    if self.cancelled.is_set():
                        raise TransferError("The move was cancelled. Nothing was kept on this PC.")
            else:
                progress(step)
        if self.owner.accept_error:
            if self.owner.accept_erases_runtime:
                self.owner.runtime_ready = False    # the published copy was erased, runtime and all
            raise TransferError(self.owner.accept_error)
        if not self.owner.runtime_ready:
            raise TransferError("Speech recognition is not prepared on this PC yet.")
        return self.owner.arrive(self.owner.accept_result)

    def cancel(self):
        self.owner.record("receiver_cancel")
        self.cancelled.set()


class FakeTransfer:
    """Stands in for transfer_ui.TransferBackend."""

    def __init__(self, host):
        self.host = host
        self.calls = []
        self.threads = []
        self.view = {"view": "", "role": "", "state": ""}
        self.withheld = False
        self.forwarding = False
        self.offer_error = self.run_error = self.preflight_error = self.accept_error = ""
        self.prepare_errors = []
        self.prepare_gate = threading.Event()
        self.prepare_gate.set()
        self.sender_steps = SENDER_RUN
        self.receiver_steps = RECEIVER_RUN
        self.card = CARD
        self.accept_result = ACTIVE
        self.connections = []
        self.answers = []
        self.attestations = []
        self.release_codes = []
        self.tunnel_result = None
        self.runtime_ready = False          # what is really in staging, whatever a screen believes
        self.accept_erases_runtime = False
        self.keep_error = ""
        self.status_errors = []
        self.erase_checks = []
        self.staging_discards = 0

    def record(self, name):
        self.calls.append(name)
        self.threads.append(threading.get_ident())

    def arrive(self, result):
        if result.get("state") == "active":
            self.host.settings["configured"] = True
            self.view = {"view": "", "role": "receiver", "state": "active"}
            self.withheld = bool(result["tunnel_enabled"] and not result["tunnel_started"])
        else:
            self.host.settings["configured"] = True
            self.view = {"view": "waiting", "role": "receiver", "state": "pending", "staged": True,
                         "code_failures": 0, "override": {"possible": True, "wait_remaining": 0, "phrase": PHRASE}}
        return dict(result)

    def status(self):
        self.record("status")
        if self.status_errors:
            raise TransferError(self.status_errors.pop(0))
        return dict(self.view)

    def sender(self):
        return FakeSender(self)

    def receiver(self):
        return FakeReceiver(self)

    def keep_hosting(self):
        self.record("keep_hosting")
        if self.keep_error:
            self.view = {**self.view, "can_finish": True, "taken_over": True}
            raise TransferError(self.keep_error)
        self.view = {"view": "", "role": "sender", "state": "cancelled"}
        return dict(self.view)

    def finish_move(self):
        self.record("finish_move")
        self.host.settings["configured"] = False
        self.view = {"view": "moved", "release_code": RELEASE, "archive": "archive"}
        return {"state": "moved", "release_code": RELEASE, "archive": "archive"}

    def erase_archive(self, checked_other_pc=False):
        self.record("erase_archive")
        self.erase_checks.append(checked_other_pc)
        self.view = {"view": "", "role": "", "state": ""}

    def runtime_staged(self):
        return self.runtime_ready

    def discard_unused_staging(self):
        self.record("discard_unused_staging")
        self.staging_discards += 1
        self.runtime_ready = False
        return True

    def activate(self, release_code=None):
        self.record("activate")
        self.release_codes.append(release_code)
        if release_code is not None and release_code != RELEASE:
            raise TransferError("That release code " + release_code + " does not match this transfer.")
        return self.arrive(self.accept_result)

    def activate_by_override(self, attestation):
        self.record("activate_by_override")
        self.attestations.append(attestation)
        if attestation.strip() != PHRASE:
            raise TransferError("Type the sentence exactly as shown to confirm the old PC is gone for good.")
        return self.arrive({**ACTIVE, "override": True})

    def start_tunnel(self):
        self.record("start_tunnel")
        return self.arrive(self.tunnel_result or ACTIVE)

    def discard_pending(self):
        self.record("discard_pending")
        self.host.settings["configured"] = False
        self.view = {"view": "", "role": "receiver", "state": "discarded"}

    def public_answers(self, origin):
        self.record("public_answers")
        return self.answers.pop(0) if self.answers else False

    def tunnel_withheld(self):
        self.record("tunnel_withheld")
        return self.withheld


class EntryTests(unittest.TestCase):
    def test_address_and_code_are_sorted_out_whichever_box_they_were_typed_or_pasted_in(self):
        expected = ("ears.example.org", "K7QM-2XHD")
        for address, code in (
            ("ears.example.org", "K7QM-2XHD"),
            ("K7QM-2XHD", "ears.example.org"),
            ("ears.example.org K7QM-2XHD", ""),
            ("", "K7QM-2XHD   ears.example.org"),
            ("  https://ears.example.org/  ", " K7QM-2XHD "),
            ("ears.example.org/K7QM-2XHD", ""),
            ("Address: ears.example.org, Code: K7QM-2XHD", ""),
        ):
            with self.subTest(address=address, code=code):
                self.assertEqual(expected, transfer_ui.split_entry(address, code))
        self.assertEqual(("ears.example.org", "K7QM2XHD"), transfer_ui.split_entry("ears.example.org", "K7QM 2XHD"))
        self.assertEqual(("ears.example.org:8443", "K7QM-2XHD"),
                         transfer_ui.split_entry("K7QM-2XHD ears.example.org:8443", ""))
        self.assertEqual(("", "K7QM-2XHD"), transfer_ui.split_entry("", "K7QM-2XHD"))
        self.assertEqual(("ears.example.org", ""), transfer_ui.split_entry("ears.example.org", ""))

    def test_only_operator_wording_is_ever_shown_for_a_failure(self):
        from Hosting.control import HostError
        self.assertEqual("Start this host first.", transfer_ui.error_text(TransferError("Start this host first.")))
        self.assertEqual("Port in use.", transfer_ui.error_text(HostError("Port in use.")))
        for error in (KeyError("XENO_private_bot_token"), OSError("C:/ProgramData/secret path"), ValueError("raw")):
            self.assertEqual(transfer_ui.GENERIC_ERROR, transfer_ui.error_text(error))

    def test_every_stage_the_sequences_report_has_fixed_wording(self):
        from Hosting import transfer_flow
        import inspect
        import re
        source = inspect.getsource(transfer_flow)
        reported = set(re.findall(r'progress\("([a-z_]+)"\)', source))
        self.assertIn("waiting_release", reported)
        peer = {"peer_" + stage for stage in transfer_flow.PROGRESS_STAGES}
        known = set(transfer_ui.SENDER_STAGES) | set(transfer_ui.RECEIVER_STAGES)
        self.assertEqual(set(), (reported | peer) - known)
        self.assertEqual(peer, {stage for stage in transfer_ui.SENDER_STAGES if stage.startswith("peer_")})

    def test_the_real_backend_passes_this_host_to_every_sequence(self):
        from Hosting import transfer_flow, transfer_net
        host = object()
        backend = transfer_ui.TransferBackend(host)
        with mock.patch.multiple(transfer_flow, status=mock.DEFAULT, Sender=mock.DEFAULT, Receiver=mock.DEFAULT,
                                 keep_hosting=mock.DEFAULT, finish_move=mock.DEFAULT, erase_archive=mock.DEFAULT,
                                 activate=mock.DEFAULT, activate_by_override=mock.DEFAULT,
                                 start_tunnel=mock.DEFAULT, discard_pending=mock.DEFAULT) as flow, \
                mock.patch.object(transfer_net, "public_answers", return_value=True) as answers:
            backend.status()
            backend.sender()
            backend.receiver()
            backend.keep_hosting()
            backend.finish_move()
            backend.erase_archive()
            backend.start_tunnel()
            backend.discard_pending()
            for name in ("status", "Sender", "Receiver", "keep_hosting", "finish_move", "erase_archive",
                         "start_tunnel", "discard_pending"):
                flow[name].assert_called_once_with(host)
            backend.activate(RELEASE)
            flow["activate"].assert_called_once_with(host, release_code=RELEASE)
            backend.activate()
            flow["activate"].assert_called_with(host, release_code=None)
            backend.activate_by_override(PHRASE)
            flow["activate_by_override"].assert_called_once_with(host, attestation=PHRASE)
            backend.erase_archive(checked_other_pc=True)
            flow["erase_archive"].assert_called_with(host, checked_other_pc=True)
            self.assertTrue(backend.public_answers("https://ears.example.org"))
            answers.assert_called_once_with("https://ears.example.org")
        self.assertIs(transfer_flow, backend.flow)

    def test_the_real_backend_signatures_match_the_sequences(self):
        from Hosting import transfer_flow, transfer_net
        import inspect
        for name, keyword in (("activate", "release_code"), ("activate_by_override", "attestation")):
            self.assertIn(keyword, inspect.signature(getattr(transfer_flow, name)).parameters)
        for name in ("status", "keep_hosting", "finish_move", "erase_archive", "start_tunnel", "discard_pending",
                     "discard_unused_staging"):
            self.assertEqual("host", next(iter(inspect.signature(getattr(transfer_flow, name)).parameters)))
        self.assertIn("checked_other_pc", inspect.signature(transfer_flow.erase_archive).parameters)
        # Everything that decides to start something asks the three-state
        # check, never the yes/no one that is only for display.
        for name in ("keep_hosting", "activate", "activate_by_override", "start_tunnel"):
            parameters = inspect.signature(getattr(transfer_flow, name)).parameters
            self.assertIs(transfer_net.public_state, parameters["public_state"].default)
            self.assertNotIn("public_answers", parameters)
        for name in ("open_offer", "run", "cancel"):
            self.assertTrue(callable(getattr(transfer_flow.Sender, name)))
        for name in ("preflight", "prepare", "connect", "accept", "cancel"):
            self.assertTrue(callable(getattr(transfer_flow.Receiver, name)))

    def test_a_withheld_tunnel_is_read_from_the_durable_mark_not_from_the_receipt(self):
        from Hosting import control

        class Host:
            state = "unused"

            def __init__(self, withheld, tunnel=True, configured=True):
                self.settings = {"configured": configured, "tunnel_enabled": tunnel, "tunnel_withheld": withheld}

            def read_settings(self):
                return self.settings

        def withheld(host):
            backend = transfer_ui.TransferBackend(host)
            # Whatever the receipt says by now plays no part.
            with mock.patch.object(transfer_ui.TransferBackend, "status",
                                   side_effect=AssertionError("the receipt was asked")):
                return backend.tunnel_withheld()

        self.assertIs(True, withheld(Host(True)))
        self.assertIs(False, withheld(Host(False)))
        self.assertIs(False, withheld(Host(True, tunnel=False)))
        self.assertIs(False, withheld(Host(True, configured=False)))
        broken = Host(True)
        broken.read_settings = mock.Mock(side_effect=control.HostError("The saved host settings could not be read."))
        self.assertIs(False, withheld(broken))

    def test_the_real_backend_asks_the_disk_whether_the_speech_runtime_is_staged(self):
        host = mock.Mock()
        host.state = transfer.staging_path  # replaced below; only passed through
        host.state = __import__("pathlib").Path("C:/XENO/VemalsEarsHost")
        backend = transfer_ui.TransferBackend(host)
        host.staged_runtime_ready.return_value = True
        self.assertIs(True, backend.runtime_staged())
        host.staged_runtime_ready.assert_called_once_with(transfer.staging_path(host.state))
        host.staged_runtime_ready.return_value = False
        self.assertIs(False, backend.runtime_staged())
        host.staged_runtime_ready.side_effect = control.HostError("unreadable")
        self.assertIs(False, backend.runtime_staged())


class TransferUiCase(unittest.TestCase):
    wizard_setup = WizardUiTests.setUp
    close_root = WizardUiTests.close_root
    ready = WizardUiTests.ready

    def setUp(self):
        # Registered first, so it runs last: the finished window is collected
        # here, on the Tk thread, and never by a leftover background thread.
        self.addCleanup(self.release)
        self.wizard_setup()

    def release(self):
        for name in ("app", "backend", "transfer", "root"):
            self.__dict__.pop(name, None)
        gc.collect()

    def create_transfer(self, *, configured=False, view=None, withheld=False, status_errors=(), **settings):
        self.backend = FakeController(configured=configured)
        self.backend.settings.update(public_http_url="https://ears.example.org", **settings)
        self.transfer = FakeTransfer(self.backend)
        if view is not None:
            self.transfer.view = view
        self.transfer.withheld = withheld
        self.transfer.status_errors = list(status_errors)
        self.opened = []
        self.clicks = []
        self.app = wizard.HostWizard(self.root, self.backend, open_url=self.opened.append,
                                    takeover_backend=FakeTakeover(self.backend),
                                    transfer_backend=self.transfer)
        self.addCleanup(self.app.transfer_ui.stop_timers)
        self.ready()
        return self.app.transfer_ui

    def widgets(self, kind):
        def walk(parent):
            for child in parent.winfo_children():
                if isinstance(child, kind):
                    yield child
                yield from walk(child)
        return [*walk(self.app.body), *walk(self.app.navigation)]

    def button(self, text):
        matches = [button for button in self.widgets(tk.Button) if button.cget("text") == text]
        self.assertEqual(1, len(matches), "Expected one button named " + repr(text) + " on " + self.app.page)
        return matches[0]

    def has_button(self, text):
        return any(button.cget("text") == text for button in self.widgets(tk.Button))

    def click(self, text):
        button = self.button(text)
        self.assertEqual("normal", str(button.cget("state")), text + " is disabled")
        self.clicks.append(text)
        button.invoke()

    def shown(self):
        """Every piece of text an operator can read on the page, the notice included."""
        texts = [widget.cget("text") for widget in self.widgets((tk.Label, tk.Button, tk.Checkbutton))]
        texts += [entry.get() for entry in self.widgets(tk.Entry)]
        return [*texts, self.app.notice.get(), self.app.step_label.cget("text")]

    def page_text(self):
        return "\n".join(self.shown())

    def settle(self, condition, message="The screen did not settle"):
        deadline = time.monotonic() + 5
        while not condition() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.root.update()
        self.assertTrue(condition(), message)

    def off_ui_thread(self, *names):
        main = threading.get_ident()
        for name, thread in zip(self.transfer.calls, self.transfer.threads):
            if name in names:
                self.assertNotEqual(main, thread, name + " ran on the UI thread")

    def prepared(self):
        self.settle(lambda: self.app.transfer_ui.prepare_state in ("ready", "failed", ""))


class ChooserTests(TransferUiCase):
    def test_chooser_keeps_its_page_and_title_and_touches_nothing_until_receive_is_chosen(self):
        self.create_transfer()
        self.assertEqual("choose-host", self.app.page)
        self.assertEqual("Vemal's Ears - Host", self.root.title())
        self.assertEqual(["status", "tunnel_withheld"], self.transfer.calls)
        self.assertTrue(self.has_button("Set up"))
        self.assertTrue(self.has_button("Receive"))
        self.assertIn("Receive a host from another PC", self.page_text())
        self.assertFalse(self.has_button("Export"))
        self.assertFalse(self.has_button("Import"))

    def test_the_file_handoff_stays_reachable_behind_the_secondary_link(self):
        self.create_transfer()
        self.click("Older worker-only handoff")
        self.assertEqual("takeover-legacy", self.app.page)
        self.assertEqual([], self.app.takeover_ui.backend.legacy.calls)
        self.click("Export")
        self.ready()
        self.assertEqual("takeover-export", self.app.page)
        # "Transfer" is the word for a whole-host move; this older path has its own.
        self.assertEqual("Worker handoff / Original PC", self.app.step_label.cget("text"))
        self.click("Back")
        self.assertEqual("takeover-legacy", self.app.page)
        self.click("Import")
        self.assertEqual("takeover-import", self.app.page)
        self.click("Back")
        self.click("Back")
        self.assertEqual("choose-host", self.app.page)

    def test_a_failed_opening_read_offers_retry_only_and_does_not_talk_about_setup(self):
        self.create_transfer(status_errors=["Another transfer operation is running. Wait for it to finish."])
        self.assertEqual("progress", self.app.page)
        self.assertIn("Another transfer operation is running", self.app.notice.get())
        self.assertNotIn("Setup stopped", self.page_text())
        self.assertIn("could not be opened. Nothing was changed.", self.app.progress_text.get())
        # No way from here into new-host setup: the only next step is to try again.
        self.assertFalse(self.has_button("Back"))
        self.assertEqual(["Retry"], [button.cget("text") for button in self.widgets(tk.Button)])
        self.assertNotIn("configure", self.backend.calls)
        self.click("Retry")
        self.ready()
        self.assertEqual("choose-host", self.app.page)

    def test_a_failed_setup_still_says_setup_stopped_and_offers_retry(self):
        self.create_transfer()
        self.backend.fail_prepare = True
        self.click("Set up")
        for key, value in (("guild_name", "Fixture guild"), ("hostname", "ears.example.org"),
                           ("guild_id", "123456789012345678"), ("application_id", "234567890123456789"),
                           ("bot_token", "XENO_SYNTHETIC_BOT_TOKEN_ONLY"), ("tunnel_token", "X" * 40),
                           ("client_download_url", "https://downloads.example.org/Setup.exe")):
            self.app.values[key].set(value)
        self.app.values["installer_mode"].set("url")
        self.app.begin_setup()
        self.ready()
        self.assertEqual("progress", self.app.page)
        self.assertIn("Setup stopped", self.app.progress_text.get())
        self.assertTrue(self.has_button("Retry"))

    def test_preview_without_a_host_offers_receive_disabled(self):
        self.backend = wizard.PreviewController()
        self.app = wizard.HostWizard(self.root, self.backend, open_url=lambda url: None)
        self.ready()
        self.assertEqual("choose-host", self.app.page)
        self.assertIsNone(self.app.transfer_ui.backend)
        buttons = {button.cget("text"): str(button.cget("state")) for button in self.widgets(tk.Button)}
        self.assertEqual("disabled", buttons["Receive"])
        self.assertEqual("normal", buttons["Set up"])

    def test_transfer_fields_never_enter_fresh_host_settings(self):
        self.create_transfer()
        self.app.values["transfer_code"].set(CODE)
        self.app.values["transfer_release_code"].set(RELEASE)
        self.assertFalse(any(key.startswith("transfer_") for key in self.app.collect_settings()))


class OldPcTests(TransferUiCase):
    def test_move_button_is_on_the_dashboard_of_a_standalone_host_only(self):
        self.create_transfer(configured=True)
        self.assertEqual("dashboard", self.app.page)
        self.assertTrue(self.has_button("Move this host to another PC"))
        self.app.settings["mode"] = "remote-worker"
        self.app.show_dashboard(self.app.current_status)
        self.assertFalse(self.has_button("Move this host to another PC"))

    def test_two_actions_move_the_host_and_address_and_code_are_separate_fields(self):
        ui = self.create_transfer(configured=True)
        connected, go = threading.Event(), threading.Event()
        self.transfer.sender_steps = ("waiting", connected, "connected", "confirmed", go, *SENDER_RUN[3:])
        self.click("Move this host to another PC")
        self.assertEqual("transfer-card", self.app.page)
        for value in ("Fixture guild", "123456789012345678", "234567890123456789", "ears.example.org"):
            self.assertIn(value, self.page_text())
        # What happens, in the order it happens, and the app by its real name.
        self.assertIn("stops hosting as soon as the other PC has a full copy", self.page_text())
        self.assertIn("install Vemal's Ears Host and", self.page_text())
        self.assertNotIn("Vemal's Ears - Host", self.page_text())
        self.assertEqual([], [call for call in self.transfer.calls if call in ("open_offer", "run")])
        self.click("Show transfer code")
        self.settle(lambda: self.app.page == "transfer-code")
        fields = [entry.get() for entry in self.widgets(tk.Entry)]
        self.assertEqual(["ears.example.org", CODE], fields)
        self.assertEqual(1, sum(CODE in text for text in self.shown()))
        self.assertRegex(ui.clock.get(), r"^This code works for another (10:00|9:5[0-9])\.$")
        self.assertTrue(self.has_button("Cancel and keep hosting here"))
        self.assertIn(CODE, self.app.secrets)
        connected.set()
        self.settle(lambda: self.app.page == "transfer-moving")
        self.assertNotIn(CODE, self.page_text())
        self.assertTrue(self.has_button("Cancel and keep hosting here"))
        go.set()
        self.ready()
        self.assertEqual("transfer-moved", self.app.page)
        self.assertEqual(["Move this host to another PC", "Show transfer code"], self.clicks)
        self.assertIn(RELEASE, [entry.get() for entry in self.widgets(tk.Entry)])
        self.assertIn("confirmed that it has taken over", self.page_text())
        self.assertNotIn(CODE, self.page_text())
        self.off_ui_thread("open_offer", "run")
        self.assertIsNone(ui.sender)

    def test_cancel_is_offered_until_release_and_keeps_the_host_here(self):
        ui = self.create_transfer(configured=True)
        hold = threading.Event()
        self.transfer.sender_steps = ("waiting", "connected", "confirmed", "sending", hold)
        ui.show_move()
        self.click("Show transfer code")
        self.settle(lambda: self.app.page == "transfer-moving" and "Pausing" in ui.progress.get())
        self.click("Cancel and keep hosting here")
        self.assertIn("Cancelling", ui.progress.get())
        self.ready()
        self.assertEqual("dashboard", self.app.page)
        self.assertIn("Nothing was moved", self.app.notice.get())
        self.assertIsNone(ui.sender)

    def test_cancel_is_gone_once_the_host_is_released(self):
        ui = self.create_transfer(configured=True)
        hold = threading.Event()
        self.transfer.sender_steps = (*SENDER_RUN[:8], hold, "stopping", "moved")
        ui.show_move()
        self.click("Show transfer code")
        self.settle(lambda: ui.released)
        self.assertFalse(self.has_button("Cancel and keep hosting here"))
        ui.cancel_move()
        self.assertFalse(ui.sender.cancelled.is_set())
        self.assertIn("finishing its move", ui.close_question())
        hold.set()
        self.ready()
        self.assertEqual("transfer-moved", self.app.page)

    def test_a_wrong_code_on_the_other_pc_is_reported_in_plain_words_and_the_offer_stays(self):
        ui = self.create_transfer(configured=True)
        hold = threading.Event()
        self.transfer.sender_steps = ("waiting", "wrong_code", hold)
        ui.show_move()
        self.click("Show transfer code")
        self.settle(lambda: "wrong code" in ui.progress.get())
        self.assertEqual("transfer-code", self.app.page)
        self.assertIn(CODE, [entry.get() for entry in self.widgets(tk.Entry)])
        self.assertIn("cancels it and keeps this host here", ui.close_question())
        self.click("Cancel and keep hosting here")
        self.ready()
        self.assertEqual("dashboard", self.app.page)

    def test_a_refused_offer_stays_on_the_card_and_a_failed_move_returns_to_the_dashboard(self):
        ui = self.create_transfer(configured=True)
        self.transfer.offer_error = "Start this host first. The move travels through its own relay."
        ui.show_move()
        self.click("Show transfer code")
        self.ready()
        self.assertEqual("transfer-card", self.app.page)
        self.assertIn("Start this host first", self.app.notice.get())
        self.transfer.offer_error = ""
        self.transfer.sender_steps = ("waiting",)
        self.transfer.run_error = "The transfer code " + CODE + " expired before it was used. Nothing was moved."
        self.click("Show transfer code")
        self.ready()
        self.assertEqual("dashboard", self.app.page)
        self.assertIn("expired before it was used", self.app.notice.get())
        self.assertNotIn(CODE, self.app.notice.get())
        self.assertIn("[private value]", self.app.notice.get())

    def test_forwarding_hosts_are_told_about_the_repoint_before_and_with_the_code(self):
        ui = self.create_transfer(configured=True, tunnel_enabled=False)
        self.transfer.forwarding = True
        hold = threading.Event()
        self.transfer.sender_steps = ("waiting", hold)
        ui.show_move()
        self.assertIn("your own HTTPS forwarding", self.page_text())
        self.click("Show transfer code")
        self.settle(lambda: self.app.page == "transfer-code")
        self.assertIn("point ears.example.org at the other PC", self.page_text())
        ui.cancel_move()
        self.ready()

    def test_an_interrupted_move_opens_on_its_two_choices(self):
        self.create_transfer(configured=True, view={"view": "interrupted", "role": "sender", "state": "sending",
                                                    "can_finish": True})
        self.assertEqual("transfer-interrupted", self.app.page)
        self.assertIn("Transfer interrupted", self.page_text())
        self.assertIn("two bots", self.page_text())
        self.assertNotIn("keep_hosting", self.transfer.calls)
        self.click("Finish the move")
        self.ready()
        self.assertEqual("transfer-moved", self.app.page)
        self.assertIn(RELEASE, [entry.get() for entry in self.widgets(tk.Entry)])
        self.assertIn("type this release code there", self.page_text())
        self.off_ui_thread("finish_move")

    def test_keep_hosting_here_returns_to_the_dashboard(self):
        self.create_transfer(configured=True, view={"view": "interrupted", "role": "sender", "state": "sending",
                                                    "can_finish": False})
        self.assertEqual("disabled", str(self.button("Finish the move").cget("state")))
        self.assertIn("never received a full copy", self.page_text())
        self.click("Keep hosting here")
        self.ready()
        self.assertEqual("dashboard", self.app.page)
        self.assertIn("keeps running here", self.app.notice.get())
        self.assertEqual(1, self.transfer.calls.count("keep_hosting"))
        self.off_ui_thread("keep_hosting")

    def test_keep_hosting_refused_because_another_pc_hosts_offers_to_archive_this_copy(self):
        self.create_transfer(configured=True, view={"view": "interrupted", "role": "sender", "state": "sending",
                                                    "can_finish": False})
        self.assertIn("Captions stay stopped on this PC", self.page_text())
        self.assertNotIn("Nothing runs on this PC", self.page_text())
        self.assertIn("checks that no other PC answers", self.page_text())
        self.assertEqual("disabled", str(self.button("Finish the move").cget("state")))
        self.transfer.keep_error = ("Another PC is already hosting at this host's public address, so nothing "
                                    "was started here.")
        self.click("Keep hosting here")
        self.ready()
        # Still the interrupted page, with the reason, and now the way out.
        self.assertEqual("transfer-interrupted", self.app.page)
        self.assertIn("Another PC is already hosting", self.app.notice.get())
        self.assertIn("lock its copy as an archive", self.page_text())
        self.assertNotIn("start_services", self.backend.calls)
        self.click("Finish the move")
        self.ready()
        self.assertEqual("transfer-moved", self.app.page)

    def test_finish_the_move_warns_against_a_discarded_copy(self):
        self.create_transfer(configured=True, view={"view": "interrupted", "role": "sender", "state": "sending",
                                                    "can_finish": True})
        self.assertIn("Never choose this if the copy on the other PC was discarded", self.page_text())

    def test_released_and_half_restored_hosts_each_open_on_their_one_step(self):
        self.create_transfer(view={"view": "released", "role": "sender", "state": "released",
                                   "release_code": RELEASE})
        self.assertEqual("transfer-released", self.app.page)
        self.assertNotIn(RELEASE, self.page_text())
        self.click("Finish the move")
        self.ready()
        self.assertEqual("transfer-moved", self.app.page)

    def test_a_cancelled_move_with_services_still_off_offers_keep_hosting_here(self):
        self.create_transfer(configured=True, view={"view": "restore", "role": "sender", "state": "cancelled"})
        self.assertEqual("transfer-restore", self.app.page)
        self.click("Keep hosting here")
        self.ready()
        self.assertEqual("dashboard", self.app.page)

    def test_an_unused_code_from_a_closed_app_is_withdrawn_without_a_question(self):
        self.create_transfer(configured=True, view={"view": "interrupted", "role": "sender", "state": "offered",
                                                    "can_finish": False})
        self.assertEqual("dashboard", self.app.page)
        self.assertEqual(1, self.transfer.calls.count("keep_hosting"))
        self.off_ui_thread("keep_hosting", "status")

    def test_a_moved_pc_opens_on_the_moved_page_and_erasing_needs_a_tick(self):
        self.create_transfer(view={"view": "moved", "role": "sender", "state": "moved",
                                   "release_code": RELEASE, "archive": "archive"})
        self.assertEqual("transfer-moved", self.app.page)
        self.assertIn("This host has moved", self.page_text())
        self.assertIn(RELEASE, [entry.get() for entry in self.widgets(tk.Entry)])
        self.assertFalse(self.has_button("Set up"))
        self.click("Erase archive")
        self.assertNotIn("erase_archive", self.transfer.calls)
        self.assertIn("Tick the box", self.app.notice.get())
        # The other PC never confirmed that it took over, and erasing also
        # destroys the release code: one tick is not enough.
        self.assertIn("never confirmed the move", self.page_text())
        self.app.transfer_ui.erase_confirmed.set(True)
        self.click("Erase archive")
        self.assertNotIn("erase_archive", self.transfer.calls)
        self.assertIn("tick the second box", self.app.notice.get())
        self.app.transfer_ui.erase_checked.set(True)
        self.click("Erase archive")
        self.ready()
        self.assertEqual("choose-host", self.app.page)
        self.assertIn("archive was erased", self.app.notice.get())
        self.assertEqual([True], self.transfer.erase_checks)
        self.off_ui_thread("erase_archive")
        self.app.transfer_ui.show_moved({"release_code": RELEASE})
        self.assertFalse(self.app.transfer_ui.erase_confirmed.get())
        self.assertFalse(self.app.transfer_ui.erase_checked.get())

    def test_an_acknowledged_move_needs_only_the_one_tick_to_erase(self):
        self.create_transfer(view={"view": "moved", "role": "sender", "state": "moved", "acknowledged": True,
                                   "release_code": RELEASE, "archive": "archive"})
        self.assertIn("confirmed that it has taken over", self.page_text())
        self.assertNotIn("never confirmed the move", self.page_text())
        self.assertEqual(1, len(self.widgets(tk.Checkbutton)))
        self.app.transfer_ui.erase_confirmed.set(True)
        self.click("Erase archive")
        self.ready()
        self.assertEqual("choose-host", self.app.page)
        self.assertEqual([False], self.transfer.erase_checks)


class NewPcTests(TransferUiCase):
    def receive(self, **options):
        ui = self.create_transfer(**options)
        self.click("Receive")
        self.assertEqual("transfer-receive", self.app.page)
        return ui

    def test_three_actions_receive_the_host_and_preflight_runs_in_the_background(self):
        ui = self.receive()
        self.prepared()
        self.assertEqual(["preflight", "preflight", "prepare"], self.transfer.calls[2:])
        self.off_ui_thread("preflight", "prepare")
        self.assertEqual("This PC is ready.", ui.prepare_text.get())
        self.assertIn("open Vemal's Ears Host,", self.page_text())
        self.assertNotIn("Vemal's Ears - Host", self.page_text())
        self.app.values["transfer_address"].set("ears.example.org")
        self.app.values["transfer_code"].set("k7qm-2xhd")
        self.click("Connect")
        self.ready()
        self.assertEqual("transfer-confirm", self.app.page)
        self.assertEqual([("ears.example.org", "k7qm-2xhd")], self.transfer.connections)
        self.assertEqual("", self.app.values["transfer_code"].get())
        for value in ("Fixture guild", "123456789012345678", "234567890123456789", "ears.example.org"):
            self.assertIn(value, self.page_text())
        self.assertNotIn("accept", self.transfer.calls)
        self.click("Move it here")
        self.ready()
        self.assertEqual("dashboard", self.app.page)
        self.assertEqual(["Receive", "Connect", "Move it here"], self.clicks)
        self.assertIn("/vem start", self.app.notice.get())
        self.assertTrue(self.app.configured)
        self.assertFalse(ui.tunnel_withheld)
        self.off_ui_thread("connect", "accept")
        for text in ("k7qm-2xhd", "K7QM-2XHD", "K7QM2XHD"):
            self.assertIn(text, self.app.secrets)
            self.assertNotIn(text, self.page_text())

    def test_one_pasted_line_works_in_either_box_and_either_order(self):
        self.receive()
        self.prepared()
        for address, code in (("ears.example.org K7QM-2XHD", ""), ("", "K7QM-2XHD ears.example.org")):
            with self.subTest(address=address, code=code):
                self.app.values["transfer_address"].set(address)
                self.app.values["transfer_code"].set(code)
                self.click("Connect")
                self.ready()
                self.assertEqual("transfer-confirm", self.app.page)
                self.assertEqual(("ears.example.org", "K7QM-2XHD"), self.transfer.connections[-1])
                self.click("Cancel")
                self.assertEqual("transfer-receive", self.app.page)
                self.assertEqual("", self.app.values["transfer_code"].get())

    def test_connect_waits_for_both_fields_and_a_wrong_code_is_never_echoed(self):
        self.receive()
        self.prepared()
        self.click("Connect")
        self.assertIn("Type the address and the code", self.app.notice.get())
        self.assertEqual([], self.transfer.connections)
        self.app.values["transfer_address"].set("ears.example.org")
        self.app.values["transfer_code"].set("ZZZZ-9999")
        self.click("Connect")
        self.ready()
        self.assertEqual("transfer-receive", self.app.page)
        self.assertIn("did not match", self.app.notice.get())
        self.assertNotIn("ZZZZ", self.page_text())
        self.assertEqual("", self.app.values["transfer_code"].get())
        self.assertEqual("ears.example.org", self.app.values["transfer_address"].get())

    def test_a_pc_that_is_not_empty_cannot_connect_and_can_be_checked_again(self):
        self.create_transfer()
        self.transfer.preflight_error = "This PC already has a host. A transfer needs an empty Host install."
        self.click("Receive")
        self.settle(lambda: self.has_button("Check again"))
        self.assertEqual("disabled", str(self.button("Connect").cget("state")))
        self.assertIn("already has a host", self.page_text())
        self.assertNotIn("prepare", self.transfer.calls)
        self.transfer.preflight_error = ""
        self.click("Check again")
        self.prepared()
        self.assertEqual("normal", str(self.button("Connect").cget("state")))
        self.assertFalse(self.has_button("Check again"))

    def test_connect_is_usable_while_speech_downloads_and_the_move_waits_for_it(self):
        ui = self.create_transfer()
        self.transfer.prepare_gate.clear()
        self.click("Receive")
        self.assertEqual("disabled", str(self.button("Connect").cget("state")))
        self.settle(lambda: ui.prepare_state == "preparing" and "Downloading" in ui.prepare_text.get())
        self.assertIn("still downloading", ui.close_question())
        self.app.values["transfer_address"].set("ears.example.org " + CODE)
        self.click("Connect")
        self.ready()
        self.click("Move it here")
        self.settle(lambda: self.app.page == "transfer-receiving")
        time.sleep(0.1)
        self.root.update()
        self.assertNotIn("accept", self.transfer.calls)
        self.assertIn("speech download", ui.progress.get())
        self.transfer.prepare_gate.set()
        self.ready()
        self.assertEqual("dashboard", self.app.page)
        self.assertEqual(1, self.transfer.calls.count("prepare"))

    def test_a_failed_speech_download_is_tried_again_before_the_move(self):
        ui = self.create_transfer()
        self.transfer.prepare_errors = ["Check your internet connection and retry."]
        self.click("Receive")
        self.settle(lambda: ui.prepare_state == "failed")
        self.assertIn("Speech setup stopped", ui.prepare_text.get())
        self.app.values["transfer_address"].set("ears.example.org " + CODE)
        self.click("Connect")
        self.ready()
        self.click("Move it here")
        self.ready()
        self.assertEqual("dashboard", self.app.page)
        self.assertEqual(2, self.transfer.calls.count("prepare"))
        self.assertLess(self.transfer.calls.index("accept"), len(self.transfer.calls))
        self.assertGreater(self.transfer.calls.index("accept"),
                           max(index for index, name in enumerate(self.transfer.calls) if name == "prepare"))

    def connected(self, configure=None, **options):
        ui = self.receive(**options)
        if configure is not None:
            configure()
        self.prepared()
        self.app.values["transfer_address"].set("ears.example.org " + CODE)
        self.click("Connect")
        self.ready()
        return ui

    def test_cancel_on_the_card_and_during_the_move_keeps_nothing_and_returns_to_receive(self):
        ui = self.connected()
        self.click("Cancel")
        self.assertEqual("transfer-receive", self.app.page)
        self.settle(lambda: "receiver_cancel" in self.transfer.calls)
        self.app.values["transfer_address"].set("ears.example.org " + CODE)
        self.click("Connect")
        self.ready()
        hold = threading.Event()
        self.transfer.receiver_steps = ("downloads", "receiving", hold)
        self.click("Move it here")
        self.settle(lambda: "sending its copy" in ui.progress.get())
        self.click("Cancel")
        self.ready()
        self.assertEqual("transfer-receive", self.app.page)
        self.assertIn("Nothing was kept on this PC", self.app.notice.get())
        self.assertIsNone(ui.receiver)

    def test_cancel_is_gone_once_this_pc_holds_a_full_copy(self):
        ui = self.connected()
        hold = threading.Event()
        self.transfer.receiver_steps = (*RECEIVER_RUN[:5], hold, *RECEIVER_RUN[5:])
        self.click("Move it here")
        self.settle(lambda: ui.committed)
        self.assertFalse(self.has_button("Cancel"))
        ui.cancel_receive()
        self.assertNotIn("receiver_cancel", self.transfer.calls)
        self.assertIn("being received", ui.close_question())
        hold.set()
        self.ready()
        self.assertEqual("dashboard", self.app.page)

    def test_a_failed_move_returns_to_receive_with_the_reason(self):
        self.connected()
        self.transfer.accept_error = "Port 8770 is in use on this PC. Nothing was kept on this PC."
        self.click("Move it here")
        self.ready()
        self.assertEqual("transfer-receive", self.app.page)
        self.assertIn("Port 8770 is in use", self.app.notice.get())
        # This PC is checked again after a failed move, then Connect is back.
        self.prepared()
        self.assertEqual("normal", str(self.button("Connect").cget("state")))

    def test_a_retry_after_the_published_copy_was_erased_prepares_speech_again(self):
        ui = self.connected()
        self.assertEqual(1, self.transfer.calls.count("prepare"))
        # The move fails after the copy was published: erasing it took the
        # prepared speech runtime with it.
        self.transfer.accept_error = "Synthetic service registration failure. Nothing was kept on this PC."
        self.transfer.accept_erases_runtime = True
        self.click("Move it here")
        self.ready()
        self.assertEqual("transfer-receive", self.app.page)
        self.prepared()
        self.assertEqual(2, self.transfer.calls.count("prepare"))
        self.assertTrue(self.transfer.runtime_ready)
        self.transfer.accept_error = ""
        self.app.values["transfer_address"].set("ears.example.org " + CODE)
        self.click("Connect")
        self.ready()
        self.click("Move it here")
        self.ready()
        self.assertEqual("dashboard", self.app.page)

    def fail_keeping(self, view, error):
        """The connected move fails after this PC already holds the host in `view`."""
        receiver = self.app.transfer_ui.receiver
        failing = receiver.accept

        def accept(progress):
            self.backend.settings["configured"] = True
            self.transfer.view = view
            return failing(progress)
        receiver.accept = accept
        self.transfer.accept_error = error

    def test_a_failure_that_keeps_the_released_copy_reopens_on_its_recovery_step_not_on_receive(self):
        # Astra finding 3: starting services failed after RELEASE. The copy is
        # still here, released and not started; Receive would call it an old
        # host to uninstall.
        self.connected()
        self.fail_keeping({"view": "activating", "role": "receiver", "state": "active"},
                          "Synthetic service start failure.")
        preflights = self.transfer.calls.count("preflight")
        self.click("Move it here")
        self.ready()
        self.assertEqual("transfer-activating", self.app.page)
        self.assertIn("Synthetic service start failure.", self.app.notice.get())
        self.assertEqual(preflights, self.transfer.calls.count("preflight"))
        self.transfer.accept_error = ""
        self.click("Start this host")
        self.ready()
        self.assertEqual("dashboard", self.app.page)

    def test_a_failure_that_kept_a_started_host_reopens_on_the_dashboard(self):
        self.connected()
        self.fail_keeping({"view": "", "role": "receiver", "state": "active"}, "Synthetic tunnel start failure.")
        preflights = self.transfer.calls.count("preflight")
        self.click("Move it here")
        self.ready()
        self.assertEqual("dashboard", self.app.page)
        self.assertEqual(preflights, self.transfer.calls.count("preflight"))
        self.assertTrue(self.app.configured)

    def test_a_speech_preparation_failure_after_connect_closes_the_connection_to_the_old_pc(self):
        # Astra finding 5: _wait_prepared() fails before accept() runs, so
        # accept's own cleanup never closes the connected receiver.
        ui = self.connected()
        self.transfer.runtime_ready = False
        self.transfer.prepare_errors = ["Check your internet connection and retry."]
        self.click("Move it here")
        self.ready()
        self.assertEqual("transfer-receive", self.app.page)
        self.assertIn("Check your internet connection", self.app.notice.get())
        self.assertNotIn("accept", self.transfer.calls)
        self.assertEqual(1, self.transfer.calls.count("receiver_cancel"))
        self.off_ui_thread("receiver_cancel")
        self.assertIsNone(ui.receiver)

    def test_a_failed_accept_is_not_cancelled_again_by_the_screen(self):
        self.connected()
        self.transfer.accept_error = "Port 8770 is in use on this PC. Nothing was kept on this PC."
        self.click("Move it here")
        self.ready()
        self.assertIn("accept", self.transfer.calls)
        self.assertNotIn("receiver_cancel", self.transfer.calls)

    def test_the_move_trusts_the_disk_not_the_screen_about_the_speech_runtime(self):
        ui = self.connected()
        self.assertTrue(ui.prepared)
        self.assertEqual("ready", ui.prepare_state)
        self.transfer.runtime_ready = False     # gone from staging behind the screen's back
        self.click("Move it here")
        self.ready()
        self.assertEqual("dashboard", self.app.page)
        self.assertEqual(2, self.transfer.calls.count("prepare"))
        self.assertLess(max(index for index, name in enumerate(self.transfer.calls) if name == "prepare"),
                        self.transfer.calls.index("accept"))

    def test_setting_up_a_new_host_instead_stops_the_speech_download_and_removes_its_staging(self):
        ui = self.create_transfer()
        self.transfer.prepare_gate.clear()
        self.click("Receive")
        self.settle(lambda: ui.prepare_state == "preparing")
        thread = ui.prepare_thread
        self.click("Back")
        self.assertEqual("choose-host", self.app.page)
        self.assertEqual(0, self.transfer.staging_discards)
        self.click("Set up")
        for key, value in (("guild_name", "Fixture guild"), ("hostname", "ears.example.org"),
                           ("guild_id", "123456789012345678"), ("application_id", "234567890123456789"),
                           ("bot_token", "XENO_SYNTHETIC_BOT_TOKEN_ONLY"), ("tunnel_token", "X" * 40),
                           ("client_download_url", "https://downloads.example.org/Setup.exe")):
            self.app.values[key].set(value)
        self.app.values["installer_mode"].set("url")
        self.transfer.prepare_gate.set()        # the download's next progress report stops it
        self.app.begin_setup()
        self.ready()
        self.assertEqual("dashboard", self.app.page, self.app.notice.get())
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertGreaterEqual(self.transfer.staging_discards, 1)
        self.assertFalse(self.transfer.runtime_ready)
        self.assertEqual(("", None, False), (ui.prepare_state, ui.prepare_thread, ui.prepared))
        self.off_ui_thread("discard_unused_staging")
        # An existing host's Retry setup has no receive to abandon.
        discards = self.transfer.staging_discards
        self.app.begin_setup()
        self.ready()
        self.assertEqual(discards, self.transfer.staging_discards)

    def test_forwarding_hosts_get_the_repoint_page_with_a_live_public_check(self):
        def forwarding():
            self.transfer.card = {**CARD, "tunnel_enabled": False}
            self.transfer.accept_result = {**ACTIVE, "tunnel_enabled": False, "tunnel_started": False,
                                           "forwarding": True}
            self.transfer.answers = [False, True]

        ui = self.connected(forwarding, tunnel_enabled=False)
        self.assertIn("point ears.example.org at this PC, port 8770", self.page_text())
        with mock.patch.object(ui, "_tick", lambda page, update, delay=500: update()):
            self.click("Move it here")
            self.ready()
            self.assertEqual("transfer-repoint", self.app.page)
            self.assertIn("Point ears.example.org at this PC", self.page_text())
            self.assertIn("http://127.0.0.1:8770", self.page_text())
            self.settle(lambda: "does not reach a host yet" in ui.reach.get())
            ui._watch_public("transfer-repoint", "ears.example.org answers. Members can connect.", "quiet")
            self.settle(lambda: "Members can connect" in ui.reach.get())
        self.off_ui_thread("public_answers")
        self.assertFalse(ui.tunnel_withheld)
        self.click("Open dashboard")
        self.assertEqual("dashboard", self.app.page)

    def test_the_public_tunnel_is_withheld_while_the_old_pc_still_answers(self):
        ui = self.connected()
        self.transfer.accept_result = {**ACTIVE, "tunnel_started": False, "public_still_answering": True}
        self.click("Move it here")
        self.ready()
        self.assertEqual("transfer-tunnel", self.app.page)
        self.assertTrue(ui.tunnel_withheld)
        self.assertIn("still answers at ears.example.org", self.page_text())
        self.click("Open dashboard")
        self.assertEqual("dashboard", self.app.page)
        self.assertIn("Members cannot connect yet", self.page_text())
        self.assertNotIn("could not check", self.page_text())
        self.app.control_services("start")
        self.assertEqual("transfer-tunnel", self.app.page)
        self.assertNotIn("start_services", self.backend.calls)
        self.app.show_dashboard(self.app.current_status)
        self.app.begin_setup()
        self.assertEqual("transfer-tunnel", self.app.page)
        self.app.edit_settings()
        self.app.begin_setup()
        self.assertEqual("transfer-tunnel", self.app.page)
        self.app.cancel_edit()
        self.assertNotIn("install_services", self.backend.calls)
        self.assertNotIn("configure", self.backend.calls)
        self.transfer.tunnel_result = {**ACTIVE, "tunnel_started": False, "public_still_answering": True}
        self.click("Connect members")
        self.ready()
        self.assertEqual("transfer-tunnel", self.app.page)
        self.transfer.tunnel_result = ACTIVE
        self.click("Connect members")
        self.ready()
        self.assertEqual("dashboard", self.app.page)
        self.assertFalse(ui.tunnel_withheld)
        self.assertNotIn("Members cannot connect yet", self.page_text())
        self.off_ui_thread("start_tunnel")

    def test_a_new_pc_that_could_not_check_the_old_address_says_so(self):
        ui = self.connected()
        self.transfer.accept_result = {**ACTIVE, "tunnel_started": False, "public_unchecked": True}
        self.click("Move it here")
        self.ready()
        self.assertEqual("transfer-tunnel", self.app.page)
        self.assertTrue(ui.tunnel_withheld)
        self.assertIn("could not check that the old PC stopped answering at ears.example.org", self.page_text())
        self.assertIn("Check this PC's internet connection", self.page_text())
        self.assertNotIn("still answers at", self.page_text())

    def test_a_withheld_tunnel_is_remembered_when_the_app_reopens(self):
        ui = self.create_transfer(configured=True, withheld=True,
                                  view={"view": "", "role": "receiver", "state": "active"})
        self.assertEqual("dashboard", self.app.page)
        self.assertTrue(ui.tunnel_withheld)
        self.assertTrue(self.has_button("Connect members"))
        self.off_ui_thread("tunnel_withheld")

    def test_a_silent_old_pc_leads_to_the_waiting_page_and_the_release_code_starts_the_host(self):
        self.connected()
        self.transfer.accept_result = {"state": "waiting"}
        self.click("Move it here")
        self.ready()
        self.assertEqual("transfer-waiting", self.app.page)
        self.assertIn("Waiting for the old PC", self.page_text())
        self.assertNotIn("start_services", self.backend.calls)
        self.click("Start this host")
        self.assertIn("Type the release code", self.app.notice.get())
        self.assertNotIn("activate", self.transfer.calls)
        wrong = "-".join(["WRNG"] * 14)
        self.app.values["transfer_release_code"].set(wrong)
        self.click("Start this host")
        self.ready()
        self.assertEqual("transfer-waiting", self.app.page)
        self.assertIn("does not match", self.app.notice.get())
        self.assertNotIn(wrong, self.app.notice.get())
        self.transfer.accept_result = ACTIVE
        self.app.values["transfer_release_code"].set("  " + RELEASE + "  ")
        self.click("Start this host")
        self.ready()
        self.assertEqual("dashboard", self.app.page)
        self.assertEqual([wrong, RELEASE], self.transfer.release_codes)
        self.assertEqual("", self.app.values["transfer_release_code"].get())
        self.assertNotIn(RELEASE, self.page_text())
        self.off_ui_thread("activate")

    def waiting(self, **view):
        status = {"view": "waiting", "role": "receiver", "state": "pending", "staged": True, "code_failures": 0,
                  "override": {"possible": False, "wait_remaining": 0}, **view}
        return self.create_transfer(configured=True, view=status)

    def test_a_pending_host_opens_on_waiting_never_on_the_dashboard(self):
        self.waiting(code_failures=2)
        self.assertEqual("transfer-waiting", self.app.page)
        # A count for information; nothing says or implies a limit.
        self.assertIn("Wrong release codes so far: 2. The right code still works", self.page_text())
        self.assertNotIn("of 5", self.page_text())
        self.assertIn("open Vemal's Ears Host.", self.page_text())
        self.assertNotIn("iscard this transfer", self.page_text())
        self.assertFalse(self.has_button("Start"))
        self.assertFalse(self.has_button("The old PC is gone for good"))
        self.assertTrue(self.has_button("Discard and start again"))
        self.assertNotIn("status", self.backend.calls)

    def test_an_incomplete_copy_can_only_be_discarded(self):
        self.waiting(staged=False, override={"possible": True, "wait_remaining": 0, "phrase": PHRASE})
        self.assertFalse(self.has_button("Start this host"))
        self.assertFalse(self.has_button("The old PC is gone for good"))
        self.assertEqual([], self.widgets(tk.Entry))
        self.assertTrue(self.has_button("Discard and start again"))

    def test_discard_warns_about_a_moved_old_pc_and_needs_a_tick(self):
        self.waiting()
        self.click("Discard and start again")
        self.assertEqual("transfer-discard", self.app.page)
        self.assertIn("do NOT discard", self.page_text())
        self.click("Discard and start again")
        self.assertNotIn("discard_pending", self.transfer.calls)
        self.click("Back")
        self.assertEqual("transfer-waiting", self.app.page)
        self.click("Discard and start again")
        self.assertFalse(self.app.transfer_ui.discard_confirmed.get())
        self.app.transfer_ui.discard_confirmed.set(True)
        self.click("Discard and start again")
        self.ready()
        self.assertEqual("choose-host", self.app.page)
        self.assertIn("copy was erased", self.app.notice.get())
        self.off_ui_thread("discard_pending")

    def test_the_emergency_override_waits_then_needs_the_typed_sentence(self):
        ui = self.waiting(override={"possible": True, "wait_remaining": 480, "phrase": PHRASE})
        self.click("The old PC is gone for good")
        self.assertEqual("transfer-override", self.app.page)
        text = self.page_text()
        self.assertIn("Emergency", text)
        self.assertIn("two bots", text)
        self.assertIn("never host again", text)
        self.assertIn("needs this PC to be online to check", text)
        self.assertIn(PHRASE, [entry.get() for entry in self.widgets(tk.Entry)])
        self.assertRegex(ui.clock.get(), r"unlocks in (8:00|7:5[0-9])\.$")
        self.assertEqual("disabled", str(self.button("Start this host without the old PC").cget("state")))
        self.app.values["transfer_attestation"].set(PHRASE)
        ui.activate_override()
        self.assertEqual([], self.transfer.attestations)
        self.click("Back")
        ui.waiting["override"] = {"possible": True, "wait_remaining": 0, "phrase": PHRASE}
        self.click("The old PC is gone for good")
        self.assertIn("unlocked", ui.clock.get())
        self.app.values["transfer_attestation"].set("the old pc is gone")
        self.click("Start this host without the old PC")
        self.ready()
        self.assertEqual("transfer-override", self.app.page)
        self.assertIn("Type the sentence exactly", self.app.notice.get())
        self.app.values["transfer_attestation"].set(PHRASE)
        self.click("Start this host without the old PC")
        self.ready()
        self.assertEqual("dashboard", self.app.page)
        self.assertEqual(["the old pc is gone", PHRASE], self.transfer.attestations)
        self.off_ui_thread("activate_by_override")

    def test_a_released_host_that_did_not_finish_starting_opens_on_one_step(self):
        self.create_transfer(configured=True, view={"view": "activating", "role": "receiver", "state": "active"})
        self.assertEqual("transfer-activating", self.app.page)
        self.assertNotIn("activate", self.transfer.calls)
        self.click("Start this host")
        self.ready()
        self.assertEqual("dashboard", self.app.page)
        self.assertEqual([None], self.transfer.release_codes)


class LayoutTests(TransferUiCase):
    def test_every_transfer_page_fits_a_compact_window(self):
        ui = self.create_transfer(configured=True)
        hold = threading.Event()
        self.transfer.sender_steps = ("waiting", hold)
        self.root.deiconify()
        self.root.geometry("680x560")
        self.root.update()
        waiting = {"view": "waiting", "staged": True, "code_failures": 3,
                   "override": {"possible": True, "wait_remaining": 480, "phrase": PHRASE}}
        forwarding = {**CARD, "tunnel_enabled": False}

        def code_page():
            ui.show_move()
            ui.show_code()
            self.settle(lambda: self.app.page == "transfer-code")

        def withheld_dashboard():
            ui.tunnel_withheld = True
            self.app.show_dashboard(self.app.current_status)

        pages = (
            ("dashboard", withheld_dashboard),
            ("transfer-card", ui.show_move),
            ("transfer-moved", lambda: ui.show_moved({"release_code": RELEASE}, acknowledged=True)),
            ("transfer-interrupted", lambda: ui.show_interrupted({"can_finish": True})),
            ("transfer-released", ui.show_released),
            ("transfer-restore", ui.show_restore),
            ("choose-host", self.app.takeover_ui.show_choices),
            ("takeover-legacy", self.app.takeover_ui.show_legacy),
            ("transfer-receive", ui.show_receive),
            ("transfer-confirm", lambda: ui._connected((FakeReceiver(self.transfer), forwarding))),
            ("transfer-repoint", ui.show_repoint),
            ("transfer-tunnel", ui.show_tunnel_wait),
            ("transfer-waiting", lambda: ui.show_waiting(waiting)),
            ("transfer-discard", ui.show_discard),
            ("transfer-override", ui.show_override),
            ("transfer-activating", ui.show_activating),
            ("transfer-code", code_page),
        )
        for page, show in pages:
            with self.subTest(page=page):
                show()
                self.root.update()
                self.assertEqual(page, self.app.page)
                navigation = self.app.navigation
                for child in navigation.winfo_children():
                    self.assertGreaterEqual(child.winfo_width(), child.winfo_reqwidth(), child.cget("text"))
                    self.assertLessEqual(child.winfo_x() + child.winfo_width(), navigation.winfo_width())
                self.assertLessEqual(navigation.winfo_y() + navigation.winfo_height(), self.root.winfo_height())
                right = self.app.canvas.winfo_rootx() + self.app.canvas.winfo_width()

                def inside(parent):
                    for child in parent.winfo_children():
                        if isinstance(child, (tk.Button, tk.Entry)):
                            self.assertLessEqual(child.winfo_rootx() + child.winfo_width(), right,
                                                 child.cget("text") if isinstance(child, tk.Button) else "entry")
                        if isinstance(child, tk.Button):
                            # A clipped button would be narrower than its own label.
                            self.assertGreaterEqual(child.winfo_width(), child.winfo_reqwidth(), child.cget("text"))
                        if isinstance(child, tk.Entry) and str(child.cget("state")) == "readonly":
                            needed = tkfont.Font(root=self.root, font=child.cget("font")).measure(child.get())
                            self.assertLessEqual(needed, child.winfo_width(), child.get())
                        inside(child)

                inside(self.app.body)
        ui.cancel_move()
        self.ready()


class WiredBackend(transfer_ui.TransferBackend):
    """The real backend; only the relay sockets and downloads point at the test relay."""

    def __init__(self, case, host, relay):
        super().__init__(host)
        self.case, self.relay = case, relay

    def sender(self):
        return self.case.sender(self.host, self.relay)

    def receiver(self):
        return self.case.receiver(self.host, self.relay)

    def public_answers(self, origin):
        return self.relay.running


@WINDOWS
class RealMoveTests(FlowCase):
    """Two Host windows over the real sequences, a real relay and the fake service manager."""

    def setUp(self):
        self.apps = []
        self.clicks = {"A": [], "B": []}
        self.addCleanup(self.release)
        super().setUp()

    def release(self):
        for app in self.apps:
            app.transfer_ui.stop_timers()
            app.closed = True
            try:
                app.root.after_cancel(app.poll_id)
                app.root.destroy()
            except tk.TclError:
                pass
        self.apps = []
        gc.collect()

    def window(self, host):
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest("A Tk display is required: " + str(error))
        root.withdraw()
        app = wizard.HostWizard(root, host, open_url=lambda url: None, takeover_backend=FakeTakeover(host),
                                transfer_backend=WiredBackend(self, host, self.relay))
        app.label = host.label
        self.apps.append(app)
        self.pump(lambda: not app.busy and app.page != "progress")
        return app

    def pump(self, condition, seconds=90):
        deadline = time.monotonic() + seconds
        while not condition() and time.monotonic() < deadline:
            for app in self.apps:
                app.root.update()
            time.sleep(0.01)
        for app in self.apps:
            app.root.update()
        self.assertTrue(condition(), "The windows did not settle: " + repr([app.page for app in self.apps])
                        + " " + repr([app.notice.get() for app in self.apps]))

    @staticmethod
    def widgets(app, kind):
        def walk(parent):
            for child in parent.winfo_children():
                if isinstance(child, kind):
                    yield child
                yield from walk(child)
        return [*walk(app.body), *walk(app.navigation)]

    def click(self, app, text):
        matches = [button for button in self.widgets(app, tk.Button) if button.cget("text") == text]
        self.assertEqual(1, len(matches), "Expected one button named " + repr(text) + " on " + app.page)
        self.assertEqual("normal", str(matches[0].cget("state")), text + " is disabled")
        self.clicks[app.label].append(text)
        matches[0].invoke()

    def text(self, app):
        texts = [widget.cget("text") for widget in self.widgets(app, (tk.Label, tk.Button, tk.Checkbutton))]
        return "\n".join([*texts, *(entry.get() for entry in self.widgets(app, tk.Entry)), app.notice.get()])

    def test_two_windows_move_a_real_host_in_five_actions_and_the_archive_is_erased(self):
        old, new = self.window(self.a), self.window(self.b)
        self.assertEqual(("dashboard", "choose-host"), (old.page, new.page))
        self.click(old, "Move this host to another PC")
        self.assertIn("XENO Test Guild", self.text(old))
        self.click(old, "Show transfer code")
        self.pump(lambda: old.page == "transfer-code")
        address, code = [entry.get() for entry in self.widgets(old, tk.Entry)]
        self.assertEqual("ears.example.org", address)
        self.assertRegex(code, r"^[0-9A-Z]{4}-[0-9A-Z]{4}$")
        self.assertEqual("offered", self.receipt(self.a)["state"])

        self.click(new, "Receive")
        self.pump(lambda: new.transfer_ui.prepare_state == "ready")
        new.values["transfer_address"].set(code + " " + address)
        self.click(new, "Connect")
        self.pump(lambda: new.page == "transfer-confirm")
        self.assertIn("XENO Test Guild", self.text(new))
        self.assertEqual("transfer-moving", old.page)
        self.assertNotIn(code, self.text(old))
        # Nothing but the card has moved: the old host still runs untouched.
        self.assertEqual({control.RELAY: "Running", control.WORKER: "Running", control.TUNNEL: "Running"},
                         self.a.statuses())
        self.assertFalse(self.b.state.exists())
        self.click(new, "Move it here")
        self.pump(lambda: old.page == "transfer-moved" and new.page == "dashboard" and not new.busy)

        self.assertEqual({"A": ["Move this host to another PC", "Show transfer code"],
                          "B": ["Receive", "Connect", "Move it here"]}, self.clicks)
        self.assertEqual([], self.violations)
        self.assert_empty(self.a)
        self.assertEqual({control.RELAY: "Running", control.WORKER: "Running", control.TUNNEL: "Running"},
                         self.b.statuses())
        self.assertEqual("moved", self.receipt(self.a)["state"])
        self.assertEqual("active", self.receipt(self.b)["state"])
        self.assertIn("confirmed that it has taken over", self.text(old))
        self.assertTrue(new.configured)
        self.assertFalse(new.transfer_ui.tunnel_withheld)
        for app in (old, new):
            self.assertNotIn(code, self.text(app))
            self.assertNotIn("XENO_synthetic", self.text(app))

        # Reopened later, each PC lands where it stands: moved, and hosting.
        again_old, again_new = self.window(self.a), self.window(self.b)
        self.assertEqual(("transfer-moved", "dashboard"), (again_old.page, again_new.page))
        self.assertTrue(transfer.archive_path(self.a.state).exists())
        self.click(again_old, "Erase archive")
        self.assertTrue(transfer.archive_path(self.a.state).exists())
        again_old.transfer_ui.erase_confirmed.set(True)
        self.click(again_old, "Erase archive")
        self.pump(lambda: again_old.page == "choose-host" and not again_old.busy)
        self.assertFalse(transfer.archive_path(self.a.state).exists())
        self.assertEqual({}, self.receipt(self.a))

    def test_a_retry_after_a_failure_that_erased_the_published_copy_moves_the_speech_runtime_too(self):
        marker = "profile/WoWVoiceCaptions/runtime/XENO-runtime-marker"
        attempts = []

        def fail_once():
            attempts.append(1)
            self.b.fail_install = len(attempts) == 1

        self.b.before_install = fail_once
        old, new = self.window(self.a), self.window(self.b)

        def attempt():
            self.click(old, "Move this host to another PC")
            self.click(old, "Show transfer code")
            self.pump(lambda: old.page == "transfer-code")
            address, code = [entry.get() for entry in self.widgets(old, tk.Entry)]
            if new.page != "transfer-receive":
                self.click(new, "Receive")
            self.pump(lambda: new.transfer_ui.prepare_state == "ready")
            new.values["transfer_address"].set(address)
            new.values["transfer_code"].set(code)
            self.click(new, "Connect")
            self.pump(lambda: new.page == "transfer-confirm")
            self.click(new, "Move it here")

        attempt()
        self.pump(lambda: new.page == "transfer-receive" and not new.busy and old.page == "dashboard"
                  and not old.busy)
        self.assertIn("Nothing was kept on this PC", new.notice.get())
        self.assert_hosting(self.a)
        # The published copy was erased together with the prepared runtime.
        self.assertFalse(self.b.state.exists())
        self.pump(lambda: new.transfer_ui.prepare_state == "ready")
        self.assertTrue((transfer.staging_path(self.b.state) / marker).is_file())

        attempt()
        self.pump(lambda: old.page == "transfer-moved" and new.page == "dashboard" and not new.busy)
        self.assertEqual(2, len(attempts))
        self.assertTrue((self.b.state / marker).is_file())
        self.assertEqual({control.RELAY: "Running", control.WORKER: "Running", control.TUNNEL: "Running"},
                         self.b.statuses())
        self.assert_empty(self.a)
        self.assertEqual([], self.violations)

    def test_cancel_on_the_old_window_restores_the_host_and_the_new_pc_keeps_nothing(self):
        old, new = self.window(self.a), self.window(self.b)
        self.click(old, "Move this host to another PC")
        self.click(old, "Show transfer code")
        self.pump(lambda: old.page == "transfer-code")
        address, code = [entry.get() for entry in self.widgets(old, tk.Entry)]
        self.click(new, "Receive")
        self.pump(lambda: new.transfer_ui.prepare_state == "ready")
        new.values["transfer_address"].set(address)
        new.values["transfer_code"].set(code)
        self.click(new, "Connect")
        self.pump(lambda: new.page == "transfer-confirm" and old.page == "transfer-moving")
        self.click(old, "Cancel and keep hosting here")
        self.pump(lambda: old.page == "dashboard" and not old.busy)
        self.assertIn("Nothing was moved", old.notice.get())
        self.assert_hosting(self.a)
        self.assertEqual("cancelled", self.receipt(self.a)["state"])
        self.click(new, "Move it here")
        self.pump(lambda: new.page == "transfer-receive" and not new.busy)
        self.assertIn("Nothing was kept on this PC", new.notice.get())
        self.assertFalse(self.b.state.exists())
        self.assertEqual({}, self.b.scm)


del WizardUiTests, FlowCase


if __name__ == "__main__":
    unittest.main()
