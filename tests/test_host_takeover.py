"""Synthetic takeover checks never access a registered service or live secret."""

from copy import deepcopy
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from Hosting import handoff, takeover


GUILD = "123456789012345678"
APPLICATION = "234567890123456789"
STAMP = "2026-11-01T12:00:00Z"
PASSWORD = "synthetic-handoff-password"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=True), encoding="ascii")


class FakeWorker:
    def __init__(self, directory, *, running=True, start="delayed-auto"):
        self.record = {
            "wrapper": str(directory / "tools/nssm.exe"),
            "python": str(directory / "python/python.exe"),
            "source": str(directory / "source"),
            "state": str(directory / "legacy-state"),
            "profile": str(directory / "registered-profile"),
            "parameters": "-m GuildHub.worker_launcher", "start": start,
        }
        self.running = "RUNNING" if running else "STOPPED"
        self.calls = []

    def registration(self):
        return dict(self.record)

    def status(self):
        return self.running

    def set_start(self, value):
        self.calls.append(("set_start", value))
        self.record["start"] = value

    def stop(self):
        self.calls.append(("stop",))
        self.running = "STOPPED"

    def start(self):
        self.calls.append(("start",))
        self.running = "RUNNING"


class LegacyTakeoverTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(mock.patch.object(takeover.setup, "protect_directory"))
        self.worker = FakeWorker(self.directory)
        self.state = Path(self.worker.record["state"])
        self.profile = Path(self.worker.record["profile"]) / "WoWVoiceCaptions"
        self.state.mkdir()
        (self.state / "secrets").mkdir()
        self.values = {
            "VEMALS_EARS_GUILD_ID": GUILD,
            "VEMALS_EARS_GUILD_NAME": "Fixture Guild",
            "VEMALS_EARS_PUBLIC_SERVER_URL": "wss://ears.example.org",
            "VEMALS_EARS_PUBLIC_HTTP_URL": "https://ears.example.org",
            "VEMALS_EARS_CLIENT_DOWNLOAD_URL": "https://ears.example.org/download",
            "VEMALS_EARS_DATABASE": str(self.state / "relay/guild-hub.sqlite3"),
            "VEMALS_EARS_PEPPER_FILE": str(self.state / "secrets/pepper"),
            "WCL_CLIENT_SECRET_FILE": str(self.state / "secrets/wcl-client-secret"),
        }
        self.secrets = {
            "discord-application-id": APPLICATION,
            "discord-bot-token": "fixture-discord-secret",
            "publisher-token": "fixture_publisher_secret_12345678901234567890",
            "pepper": "excluded-relay-pepper",
            "wcl-client-secret": "excluded-wcl-secret",
        }
        for name, content in self.secrets.items():
            (self.state / "secrets" / name).write_text(content, encoding="ascii")
        self.consent = {"version": 1, "updatedAt": STAMP, "entries": {
            GUILD + ":345678901234567890": "granted", GUILD + ":456789012345678901": "declined",
        }}
        self.mapping = {"version": 1, "updatedAt": STAMP, "entries": {
            GUILD + ":345678901234567890": {"characterName": "Fixture", "realm": "Moon Guard",
                "wowClass": "priest", "role": "healer", "mappedAt": STAMP},
        }}
        write_json(self.profile / "consent-state.json", self.consent)
        write_json(self.profile / "mapping-state.json", self.mapping)
        write_json(self.profile / "sessions.json", {"private": "excluded-session"})
        write_json(self.state / "relay/guild-hub.sqlite3", {"private": "excluded-device-db"})
        self.save_config()
        self.exporter = takeover.LegacyTakeover(self.worker, self.directory / "recovery")
        self.destination = self.directory / "handoff.vehost"
        self.writer = self.enterContext(mock.patch.object(handoff, "write_bundle", side_effect=self.write_bundle))

    def save_config(self):
        (self.state / "relay.env").write_text("".join(key + "=" + value + "\n"
            for key, value in self.values.items()), encoding="ascii")

    def write_bundle(self, destination, payload, password):
        self.assertEqual("disabled", self.worker.record["start"])
        self.assertEqual("STOPPED", self.worker.status())
        self.assertEqual(PASSWORD, password)
        Path(destination).write_bytes(b"synthetic encrypted output")
        return Path(destination)

    def export(self):
        return self.exporter.export(self.destination, PASSWORD)

    def test_export_preserves_only_worker_credentials_and_registered_profile_ledgers(self):
        with mock.patch.object(takeover, "_regular", wraps=takeover._regular) as read:
            result = self.export()
        payload = self.writer.call_args.args[1]
        self.assertEqual(set(handoff.PAYLOAD_KEYS), set(payload))
        self.assertEqual(self.consent, payload["consent"])
        self.assertEqual(self.mapping, payload["mapping"])
        self.assertEqual(self.secrets["publisher-token"], payload["publisher_token"])
        self.assertEqual(self.secrets["discord-bot-token"], payload["bot_token"])
        self.assertEqual(APPLICATION, payload["application_id"])
        opened = {call.args[0].resolve() for call in read.call_args_list}
        self.assertNotIn((self.state / "relay/guild-hub.sqlite3").resolve(), opened)
        self.assertNotIn((self.state / "secrets/pepper").resolve(), opened)
        self.assertNotIn((self.state / "secrets/wcl-client-secret").resolve(), opened)
        self.assertNotIn((self.profile / "sessions.json").resolve(), opened)
        public = json.dumps(result) + self.exporter.receipt.read_text(encoding="ascii")
        for value in (self.secrets["discord-bot-token"], self.secrets["publisher-token"],
                      "excluded-relay-pepper", "excluded-device-db", "excluded-session"):
            self.assertNotIn(value, public)
        self.assertEqual([("set_start", "disabled"), ("stop",)], self.worker.calls)
        self.assertEqual("exported", json.loads(self.exporter.receipt.read_text())["status"])

    def test_explicit_ledger_paths_override_registered_profile(self):
        alternate = self.directory / "explicit-ledgers"
        consent = deepcopy(self.consent)
        consent["entries"][GUILD + ":345678901234567890"] = "declined"
        write_json(alternate / "consent.json", consent)
        write_json(alternate / "mapping.json", self.mapping)
        self.values["CAPTIONS_CONSENT_STATE_PATH"] = str(alternate / "consent.json")
        self.values["CAPTIONS_MAPPING_STATE_PATH"] = str(alternate / "mapping.json")
        self.save_config()
        self.export()
        self.assertEqual(consent, self.writer.call_args.args[1]["consent"])

    def test_unsafe_dirty_marker_refuses_export_and_restores_running_source(self):
        for marker in ({"version": 1, "synced": False}, {"version": 2, "synced": True}, [], "broken"):
            with self.subTest(marker=marker):
                write_json(self.profile / "consent-state.json.dirty", marker)
                self.worker.calls.clear()
                with self.assertRaises(takeover.TakeoverError):
                    self.export()
                self.assertEqual("RUNNING", self.worker.status())
                self.assertEqual("delayed-auto", self.worker.record["start"])
                self.assertFalse(self.destination.exists())
                self.assertEqual([("set_start", "disabled"), ("stop",),
                                  ("set_start", "delayed-auto"), ("start",)], self.worker.calls)
        self.writer.assert_not_called()

    def test_synced_marker_preserves_declines_and_is_not_exported(self):
        write_json(self.profile / "consent-state.json.dirty", {"version": 1, "synced": True})
        self.export()
        self.assertEqual(self.consent, self.writer.call_args.args[1]["consent"])
        self.assertNotIn("dirty", self.writer.call_args.args[1])

    def test_invalid_ledger_or_outside_secret_restores_without_writing_handoff(self):
        for invalid in ("ledger", "secret"):
            with self.subTest(invalid=invalid):
                if invalid == "ledger":
                    broken = deepcopy(self.consent)
                    broken["entries"]["999999999999999999:1"] = "granted"
                    write_json(self.profile / "consent-state.json", broken)
                else:
                    write_json(self.profile / "consent-state.json", self.consent)
                    self.values["VEMALS_EARS_DISCORD_BOT_TOKEN_FILE"] = str(self.directory / "foreign-token")
                    self.save_config()
                with self.assertRaises((takeover.TakeoverError, handoff.HandoffError)):
                    self.export()
                self.assertEqual("RUNNING", self.worker.status())
                self.assertEqual("delayed-auto", self.worker.record["start"])
                self.assertFalse(self.destination.exists())
        self.writer.assert_not_called()

    def test_prepublication_failure_restores_stopped_source_without_starting_it(self):
        self.worker.running = "STOPPED"
        self.worker.record["start"] = "demand"
        self.writer.side_effect = handoff.HandoffError("Synthetic encryption failure")
        with self.assertRaises(handoff.HandoffError):
            self.export()
        self.assertEqual("STOPPED", self.worker.status())
        self.assertEqual("demand", self.worker.record["start"])
        self.assertNotIn(("start",), self.worker.calls)

    def test_failure_after_output_exists_never_restarts_source(self):
        def publish_then_fail(destination, payload, password):
            self.write_bundle(destination, payload, password)
            raise handoff.HandoffError("Synthetic post-publication cleanup failure")

        self.writer.side_effect = publish_then_fail
        with self.assertRaises((takeover.TakeoverError, handoff.HandoffError)):
            self.export()
        self.assertTrue(self.destination.exists())
        self.assertEqual("STOPPED", self.worker.status())
        self.assertEqual("disabled", self.worker.record["start"])
        self.assertNotIn(("start",), self.worker.calls)
        self.assertIn(json.loads(self.exporter.receipt.read_text())["status"], {"stopping", "exported"})

    def test_receipt_failure_after_publication_leaves_recoverable_stopped_source(self):
        original = takeover._atomic_json

        def write_receipt(path, value):
            if value.get("status") == "exported":
                raise OSError("Synthetic receipt failure")
            original(path, value)

        with mock.patch.object(takeover, "_atomic_json", side_effect=write_receipt):
            with self.assertRaisesRegex(takeover.TakeoverError, "remains stopped"):
                self.export()
        self.assertTrue(self.destination.exists())
        self.assertEqual("disabled", self.worker.record["start"])
        self.assertEqual("STOPPED", self.worker.status())
        self.assertTrue(self.exporter.inspect()["handoff_pending"])

    def test_overwrite_and_state_directory_destinations_are_rejected_before_stop(self):
        self.destination.write_bytes(b"existing file")
        with self.assertRaises(takeover.TakeoverError):
            self.export()
        self.assertEqual(b"existing file", self.destination.read_bytes())
        with self.assertRaises(takeover.TakeoverError):
            self.exporter.export(self.state / "downloads/handoff.vehost", PASSWORD)
        self.assertEqual([], self.worker.calls)

    def test_pending_export_blocks_a_second_export(self):
        self.export()
        self.worker.calls.clear()
        with self.assertRaisesRegex(takeover.TakeoverError, "already pending"):
            self.exporter.export(self.directory / "second.vehost", PASSWORD)
        self.assertEqual([], self.worker.calls)

    def test_registration_change_before_stop_never_mutates_changed_service(self):
        original = self.worker.registration
        count = 0

        def changed_registration():
            nonlocal count
            count += 1
            record = original()
            if count > 1:
                record["source"] = str(self.directory / "unrelated-source")
            return record

        with mock.patch.object(self.worker, "registration", side_effect=changed_registration):
            with self.assertRaises(takeover.TakeoverError):
                self.export()
        self.assertEqual([], self.worker.calls)
        self.writer.assert_not_called()

    def test_cancel_requires_explicit_unused_confirmation_and_same_registration(self):
        self.export()
        self.worker.calls.clear()
        with self.assertRaisesRegex(takeover.TakeoverError, "unused handoff"):
            self.exporter.cancel_unused(replacement_never_started=False)
        self.worker.record["source"] = str(self.directory / "changed-source")
        with self.assertRaisesRegex(takeover.TakeoverError, "registration changed"):
            self.exporter.cancel_unused(replacement_never_started=True)
        self.assertEqual([], self.worker.calls)

    def test_cancel_unused_restores_original_startup_and_running_state(self):
        self.export()
        self.worker.calls.clear()
        result = self.exporter.cancel_unused(replacement_never_started=True)
        self.assertEqual([("set_start", "delayed-auto"), ("start",)], self.worker.calls)
        self.assertFalse(result["handoff_pending"])
        self.assertEqual("cancelled", json.loads(self.exporter.receipt.read_text())["status"])

    def test_crash_receipt_can_resume_only_stopped_unchanged_source(self):
        record = self.worker.registration()
        write_json(self.exporter.receipt, {"schema": 1, "status": "stopping",
            "identity": takeover._fingerprint(record), "start": record["start"],
            "was_running": True, "bundle": str(self.destination)})
        self.destination.write_bytes(b"synthetic published handoff")
        with self.assertRaisesRegex(takeover.TakeoverError, "no longer stopped"):
            self.exporter.cancel_unused(replacement_never_started=True)
        self.assertEqual([], self.worker.calls)
        self.worker.record["start"] = "disabled"
        self.worker.running = "STOPPED"
        self.exporter.cancel_unused(replacement_never_started=True)
        self.assertEqual("RUNNING", self.worker.status())
        self.assertEqual("delayed-auto", self.worker.record["start"])

    def test_crash_before_stop_can_cancel_without_restarting_running_source(self):
        record = self.worker.registration()
        write_json(self.exporter.receipt, {"schema": 1, "status": "stopping",
            "identity": takeover._fingerprint(record), "start": record["start"],
            "was_running": True, "bundle": str(self.destination)})
        self.worker.record["start"] = "disabled"
        self.assertFalse(self.destination.exists())
        result = self.exporter.cancel_unused(replacement_never_started=True)
        self.assertEqual([("set_start", "delayed-auto")], self.worker.calls)
        self.assertEqual("RUNNING", self.worker.status())
        self.assertFalse(result["handoff_pending"])

    def test_duplicate_private_json_key_is_rejected_before_export(self):
        (self.profile / "consent-state.json.dirty").write_text(
            '{"version":1,"synced":false,"synced":true}', encoding="ascii")
        with self.assertRaisesRegex(takeover.TakeoverError, "damaged"):
            self.export()
        self.writer.assert_not_called()
        self.assertEqual("RUNNING", self.worker.status())

    def test_windows_adapter_commands_target_only_the_legacy_worker(self):
        adapter = takeover.WindowsLegacyWorker()
        with mock.patch.object(adapter, "_run", return_value=SimpleNamespace(stdout="STATE : 1 STOPPED")) as run:
            adapter.set_start("disabled")
            adapter.stop()
        self.assertEqual(mock.call("config", "VemalsEarsWorker", "start=", "disabled"), run.call_args_list[0])
        for call in run.call_args_list:
            self.assertEqual("VemalsEarsWorker", call.args[1])
            self.assertNotIn("DowntimeRelay", call.args)
            self.assertNotIn("VemalsEarsTunnel", call.args)


class TakeoverControllerTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.host = SimpleNamespace(state=self.directory / "new-worker",
                                    read_settings=mock.Mock(return_value={"configured": False}))
        self.controller = takeover.TakeoverController(self.host, legacy=mock.Mock())
        self.payload = {"guild_name": "Fixture Guild", "guild_id": GUILD,
            "application_id": APPLICATION, "public_http_url": "https://ears.example.org",
            "bot_token": "fixture-bot-secret", "publisher_token": "fixture-publisher-secret"}

    def test_preview_returns_public_identity_only_and_never_imports_or_starts(self):
        with mock.patch.object(handoff, "read_bundle", return_value=self.payload), \
             mock.patch.object(handoff, "import_worker") as imported:
            result = self.controller.preview(self.directory / "handoff.vehost", PASSWORD)
        self.assertEqual({"guild_name", "guild_id", "application_id", "public_http_url"}, set(result))
        self.assertNotIn("secret", json.dumps(result))
        imported.assert_not_called()
        self.assertFalse(self.host.state.exists())

    def test_failed_preview_forgets_previous_decrypted_payload(self):
        self.controller.pending = self.payload
        with mock.patch.object(handoff, "read_bundle", side_effect=handoff.HandoffError("Cannot open")):
            with self.assertRaises(handoff.HandoffError):
                self.controller.preview(self.directory / "bad.vehost", PASSWORD)
        self.assertIsNone(self.controller.pending)

    def test_existing_host_cannot_preview_a_replacement(self):
        self.host.read_settings.return_value = {"configured": True}
        with mock.patch.object(handoff, "read_bundle") as read:
            with self.assertRaisesRegex(takeover.TakeoverError, "already has host state"):
                self.controller.preview(self.directory / "handoff.vehost", PASSWORD)
        read.assert_not_called()

    def test_import_requires_confirmation_and_verified_payload(self):
        with mock.patch.object(handoff, "import_worker") as imported:
            self.controller.pending = self.payload
            with self.assertRaisesRegex(takeover.TakeoverError, "original caption worker is stopped"):
                self.controller.import_pending(original_stopped=False)
            self.controller.pending = None
            with self.assertRaisesRegex(takeover.TakeoverError, "Open and verify"):
                self.controller.import_pending(original_stopped=True)
        imported.assert_not_called()

    def test_import_confirms_only_remote_worker_after_protected_state_exists(self):
        self.controller.pending = self.payload

        def imported(payload, state):
            self.assertEqual(self.payload, payload)
            write_json(state / "host-settings.json", {"schema": 1, "mode": "remote-worker",
                                                       "takeover_confirmed": False})
            self.host.read_settings.return_value = {"configured": True, "mode": "remote-worker"}

        with mock.patch.object(handoff, "import_worker", side_effect=imported):
            result = self.controller.import_pending(original_stopped=True)
        self.assertIsNone(self.controller.pending)
        self.assertEqual("remote-worker", result["mode"])
        self.assertIs(True, json.loads((self.host.state / "host-settings.json").read_text())["takeover_confirmed"])

    def test_activation_refuses_unconfirmed_or_nonremote_installation(self):
        with self.assertRaises(takeover.TakeoverError):
            self.controller.confirm_start(original_stopped=False)
        self.host.read_settings.return_value = {"configured": True, "mode": "standalone"}
        with self.assertRaisesRegex(takeover.TakeoverError, "not a replacement"):
            self.controller.confirm_start(original_stopped=True)
        self.assertFalse(self.host.state.exists())


@unittest.skipUnless(os.name == "nt", "Windows protected worker-state import")
class ImportedMemberConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_imported_worker_preserves_paired_identity_and_active_channel_gate(self):
        # Reuse the local relay harness, not its test inheritance: one ephemeral
        # loopback server and database survive the old/new worker boundary.
        from GuildHub import host
        from GuildHub.protocol import SUBPROTOCOL, decode_message, encode_message
        from GuildHub.tests import test_server as relay_tests
        from Hosting import service
        from VoiceCaptions.coordinator import VoiceCaptionCoordinator
        from VoiceCaptions.events import SessionEvent
        from websockets.asyncio.client import connect

        relay = relay_tests.ServerTests()
        await relay.asyncSetUp()
        self.addAsyncCleanup(relay.asyncTearDown)
        relay.application.config = replace(relay.application.config, guild_id=GUILD)
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        state = root / "replacement"
        payload = {
            "schema": 1, "kind": handoff.KIND, "guild_id": GUILD,
            "guild_name": "Fixture Guild", "application_id": APPLICATION,
            "public_server_url": "wss://ears.example.org",
            "public_http_url": "https://ears.example.org",
            "client_download_url": "https://ears.example.org/download",
            "bot_token": "XENO_synthetic_discord_bot_token",
            "publisher_token": relay_tests.PUBLISHER_TOKEN,
            "consent": {"version": 1, "updatedAt": STAMP, "entries": {GUILD + ":1": "granted"}},
            "mapping": {"version": 1, "updatedAt": STAMP, "entries": {}},
        }
        original_key = host.derive_viewer_identity_key(payload["publisher_token"])
        original_identity = VoiceCaptionCoordinator._speaker_token(SimpleNamespace(identity_key=original_key), "1")
        outside_identity = VoiceCaptionCoordinator._speaker_token(SimpleNamespace(identity_key=original_key), "2")
        paired = await relay.pair_code(relay.store.create_enrollment(viewer_token=original_identity).code)
        outside = await relay.pair_code(relay.store.create_enrollment(viewer_token=outside_identity).code)
        self.assertEqual(GUILD, paired["guild_id"])
        original_credentials = paired["device_id"], paired["device_token"]

        async def publisher(token):
            socket = await connect(relay.base_url + "/v1/publish", subprotocols=[SUBPROTOCOL])
            self.addAsyncCleanup(socket.close)
            await socket.send(encode_message({"type": "authenticate", "publisher_token": token}))
            self.assertEqual("authenticated", decode_message(await socket.recv())["type"])
            return socket

        original = await publisher(payload["publisher_token"])
        await relay_tests.VoiceMembershipTests.audience(original, 1, 0, [original_identity])
        await relay.send_publisher_event(original, SessionEvent(state="active"))
        previous_view, previous_auth = await relay.open_captions(paired)
        self.assertEqual("authenticated", previous_auth["type"])
        await previous_view.close()
        await original.close()

        # The import creates only recipient worker state. Pairing and the
        # existing member credential are neither imported nor replaced.
        handoff.import_worker(payload, state)
        config = host.load_host_config(service.load_configuration(state))
        imported_key = host.derive_viewer_identity_key(config.publisher_token)
        imported_identity = VoiceCaptionCoordinator._speaker_token(SimpleNamespace(identity_key=imported_key), "1")
        self.assertEqual(original_identity, imported_identity)
        self.assertEqual("wss://ears.example.org", config.server_url)
        self.assertFalse((state / "relay").exists())
        self.assertFalse((state / "secrets/pepper").exists())

        replacement = await publisher(config.publisher_token)
        self.assertEqual("audience_updated", (await relay_tests.VoiceMembershipTests.audience(
            replacement, 1, 0, [imported_identity]))["type"])
        await relay.send_publisher_event(replacement, SessionEvent(state="active"))
        captions, authentication = await relay.open_captions(paired)
        self.addAsyncCleanup(captions.close)
        self.assertEqual("authenticated", authentication["type"])
        self.assertEqual(original_credentials, (paired["device_id"], paired["device_token"]))
        self.assertEqual(original_identity, relay.store.authenticate_device_viewer_token(*original_credentials))
        outsider, denied = await relay.open_captions(outside)
        await outsider.close()
        self.assertEqual("voice_channel_required", denied["code"])

        sent = await relay_tests.VoiceMembershipTests.publish(replacement, generation=1, revision=0)
        self.assertEqual("published", sent["type"])
        caption = decode_message(await asyncio.wait_for(captions.recv(), 5))
        self.assertEqual(relay_tests.CAPTION.text, caption["event"]["text"])
        relay.assign_quick_chat_profile(paired)
        chat = await relay.send_quick_chat(paired, text="Same paired device after takeover")
        self.assertEqual("quick_chat_sent", chat["type"])
        typed = decode_message(await asyncio.wait_for(captions.recv(), 5))
        self.assertEqual("Same paired device after takeover", typed["event"]["text"])

        leaving = asyncio.create_task(relay_tests.VoiceMembershipTests.audience(replacement, 1, 1, []))
        removed = await relay_tests.VoiceMembershipTests.denial(captions)
        self.assertEqual("voice_channel_required", removed["code"])
        self.assertEqual("audience_updated", (await leaving)["type"])
        # Existing Message Group acceptance intentionally survives voice loss.
        # Reading and replay remain restricted to the current voice audience.
        queued_chat = await relay.send_quick_chat(paired, request_id="chat_after_leaving",
                                                  text="No audience must receive this text")
        self.assertEqual("quick_chat_sent", queued_chat["type"])
        await relay_tests.VoiceMembershipTests.audience(replacement, 1, 2, [imported_identity])
        rejoined, rejoined_auth = await relay.open_captions(paired,
            last_stream_id=relay.application.broker.stream_id, last_cursor=typed["cursor"])
        self.addAsyncCleanup(rejoined.close)
        self.assertEqual(queued_chat["cursor"], rejoined_auth["cursor"])
        current = await relay_tests.VoiceMembershipTests.publish(replacement, generation=1, revision=2)
        first_after_rejoining = decode_message(await asyncio.wait_for(rejoined.recv(), 5))
        self.assertEqual(current["cursor"], first_after_rejoining["cursor"])
        self.assertEqual(relay_tests.CAPTION.text, first_after_rejoining["event"]["text"])


if __name__ == "__main__":
    unittest.main()
