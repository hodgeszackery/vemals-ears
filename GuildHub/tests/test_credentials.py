import json
import os
import tempfile
import unittest
from pathlib import Path

from GuildHub.credentials import (
    CREDENTIAL_TARGET,
    DOWNTIME_CREDENTIAL_TARGET,
    DeviceCredentialStoreError,
    DeviceCredentialValidationError,
    DeviceCredentials,
    WindowsDeviceCredentialStore,
    WindowsDowntimeCredentialStore,
    normalize_server_url,
    validate_device_credentials,
)

DEVICE_ID = "device_123"
DEVICE_TOKEN = "ghd_private-device-token"
GUILD_ID = "guild_456"
GUILD_NAME = "Vemal's Ears"
SERVER_URL = "wss://guild.example"


class FakeCredentialApi:
    def __init__(self):
        self.record = None
        self.deleted = []

    def write(self, target, username, secret):
        self.record = (target, username, secret)

    def read(self, target):
        if self.record is None or self.record[0] != target:
            return None
        return self.record[1], self.record[2]

    def delete(self, target):
        self.deleted.append(target)
        if self.record is not None and self.record[0] == target:
            self.record = None


class FailingWriteApi(FakeCredentialApi):
    def write(self, target, username, secret):
        raise OSError(username + secret.decode("utf-8"))


class MultiCredentialApi:
    def __init__(self):
        self.records = {}
        self.writes = []

    def write(self, target, username, secret):
        self.records[target] = (username, secret)
        self.writes.append(target)

    def read(self, target):
        return self.records.get(target)

    def delete(self, target):
        self.records.pop(target, None)


def credentials(token=DEVICE_TOKEN):
    return DeviceCredentials(
        server_url=SERVER_URL,
        device_id=DEVICE_ID,
        device_token=token,
        guild_id=GUILD_ID,
        guild_name=GUILD_NAME,
    )


class DeviceCredentialTests(unittest.TestCase):
    def test_store_round_trips_one_active_device(self):
        api = FakeCredentialApi()
        store = WindowsDeviceCredentialStore(api)

        store.save(credentials())

        self.assertEqual(CREDENTIAL_TARGET, api.record[0])
        self.assertEqual(DEVICE_ID, api.record[1])
        self.assertEqual(credentials(), store.load())
        self.assertIn("Vemal's Ears", CREDENTIAL_TARGET)

    def test_save_replaces_the_single_credential(self):
        api = FakeCredentialApi()
        store = WindowsDeviceCredentialStore(api)
        store.save(credentials("ghd_first-token"))

        store.save(credentials("ghd_second-token"))

        self.assertEqual("ghd_second-token", store.load().device_token)

    def test_delete_only_targets_the_product_device(self):
        api = FakeCredentialApi()
        store = WindowsDeviceCredentialStore(api)
        store.save(credentials())

        store.delete()

        self.assertEqual([CREDENTIAL_TARGET], api.deleted)
        self.assertIsNone(store.load())

    def test_repr_redacts_every_device_value(self):
        rendered = repr(credentials())

        for private_value in (SERVER_URL, DEVICE_ID, DEVICE_TOKEN, GUILD_ID, GUILD_NAME):
            self.assertNotIn(private_value, rendered)
        self.assertEqual(rendered, str(credentials()))
        self.assertIn("redacted", rendered)

    def test_remote_server_requires_wss_but_loopback_allows_ws(self):
        self.assertEqual(
            "ws://127.0.0.1:8765", normalize_server_url("ws://127.0.0.1:8765/")
        )
        self.assertEqual("ws://[::1]:9000", normalize_server_url("ws://[::1]:9000"))
        self.assertEqual("ws://localhost", normalize_server_url("ws://LOCALHOST/"))

        with self.assertRaises(DeviceCredentialValidationError):
            normalize_server_url("ws://guild.example")

    def test_server_rejects_userinfo_paths_queries_and_bad_schemes(self):
        rejected = (
            "https://guild.example",
            "wss://user:pass@guild.example",
            "wss://guild.example/private",
            "wss://guild.example?token=secret",
            "wss://guild.example/#fragment",
            "wss://bad host",
            "wss://_bad.example",
            "wss://-bad.example",
        )
        for value in rejected:
            with self.subTest(value=value):
                with self.assertRaises(DeviceCredentialValidationError):
                    normalize_server_url(value)

    def test_invalid_values_are_never_echoed_in_validation_errors(self):
        bad_token = "private token with spaces"
        with self.assertRaises(DeviceCredentialValidationError) as caught:
            validate_device_credentials(
                SERVER_URL, DEVICE_ID, bad_token, GUILD_ID, GUILD_NAME
            )

        self.assertNotIn(bad_token, str(caught.exception))

    def test_guild_name_rejects_invisible_format_controls(self):
        with self.assertRaises(DeviceCredentialValidationError):
            validate_device_credentials(
                SERVER_URL,
                DEVICE_ID,
                DEVICE_TOKEN,
                GUILD_ID,
                "Vemal\u202e's Ears",
            )

    def test_storage_failure_does_not_echo_device_secrets(self):
        store = WindowsDeviceCredentialStore(FailingWriteApi())

        with self.assertRaises(DeviceCredentialStoreError) as caught:
            store.save(credentials())

        message = str(caught.exception)
        self.assertNotIn(DEVICE_ID, message)
        self.assertNotIn(DEVICE_TOKEN, message)

    def test_corrupted_saved_payload_fails_closed_without_echoing_token(self):
        api = FakeCredentialApi()
        api.record = (
            CREDENTIAL_TARGET,
            DEVICE_ID,
            json.dumps(
                {
                    "version": 1,
                    "server_url": SERVER_URL,
                    "device_token": DEVICE_TOKEN,
                    "guild_id": GUILD_ID,
                    "guild_name": GUILD_NAME,
                    "unexpected": "field",
                }
            ).encode("utf-8"),
        )
        store = WindowsDeviceCredentialStore(api)

        with self.assertRaises(DeviceCredentialStoreError) as caught:
            store.load()

        self.assertNotIn(DEVICE_TOKEN, str(caught.exception))

    def test_save_has_no_plaintext_file_fallback(self):
        api = FakeCredentialApi()
        store = WindowsDeviceCredentialStore(api)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            previous = Path.cwd()
            try:
                os.chdir(root)
                store.save(credentials())
            finally:
                os.chdir(previous)

            self.assertEqual([], list(root.iterdir()))


class AppCredentialIsolationTests(unittest.TestCase):
    def test_existing_pairing_copies_once_without_consuming_or_removing_ve(self):
        api = MultiCredentialApi()
        ears = WindowsDeviceCredentialStore(api)
        downtime = WindowsDowntimeCredentialStore(api)
        ears.save(credentials())
        original = api.records[CREDENTIAL_TARGET]

        self.assertEqual(credentials(), downtime.load())
        self.assertEqual(original, api.records[CREDENTIAL_TARGET])
        self.assertEqual(original, api.records[DOWNTIME_CREDENTIAL_TARGET])
        self.assertEqual(1, api.writes.count(DOWNTIME_CREDENTIAL_TARGET))
        self.assertEqual(credentials(), downtime.load())
        self.assertEqual(1, api.writes.count(DOWNTIME_CREDENTIAL_TARGET))

    def test_repairing_either_app_does_not_change_the_other(self):
        api = MultiCredentialApi()
        ears = WindowsDeviceCredentialStore(api)
        downtime = WindowsDowntimeCredentialStore(api)
        ears.save(credentials("ghd_old"))
        downtime.load()
        ears.save(credentials("ghd_new_ve"))
        self.assertEqual("ghd_old", downtime.load().device_token)

        downtime.save(credentials("ghd_new_downtime"))
        self.assertEqual("ghd_new_ve", ears.load().device_token)
        self.assertEqual("ghd_new_downtime", downtime.load().device_token)

    def test_downtime_unlink_removes_secret_and_never_recopies_legacy(self):
        api = MultiCredentialApi()
        ears = WindowsDeviceCredentialStore(api)
        downtime = WindowsDowntimeCredentialStore(api)
        ears.save(credentials())
        downtime.load()

        downtime.delete()

        self.assertIsNone(WindowsDowntimeCredentialStore(api).load())
        self.assertEqual(credentials(), ears.load())
        self.assertNotIn(DEVICE_TOKEN.encode("ascii"), api.records[DOWNTIME_CREDENTIAL_TARGET][1])
        ears.save(credentials("ghd_replaced_ve"))
        self.assertIsNone(WindowsDowntimeCredentialStore(api).load())
        downtime.save(credentials("ghd_relinked"))
        self.assertEqual("ghd_relinked", downtime.load().device_token)

    def test_ve_unlink_does_not_remove_downtime_pairing(self):
        api = MultiCredentialApi()
        ears = WindowsDeviceCredentialStore(api)
        downtime = WindowsDowntimeCredentialStore(api)
        ears.save(credentials())
        downtime.load()
        ears.delete()
        self.assertIsNone(ears.load())
        self.assertEqual(credentials(), downtime.load())

    def test_first_launch_without_legacy_does_not_follow_future_ve_pairing(self):
        api = MultiCredentialApi()
        downtime = WindowsDowntimeCredentialStore(api)
        self.assertIsNone(downtime.load())
        WindowsDeviceCredentialStore(api).save(credentials())
        self.assertIsNone(WindowsDowntimeCredentialStore(api).load())

    def test_corrupt_downtime_entry_does_not_fall_back_to_legacy(self):
        api = MultiCredentialApi()
        WindowsDeviceCredentialStore(api).save(credentials())
        api.records[DOWNTIME_CREDENTIAL_TARGET] = (DEVICE_ID, b"broken")
        with self.assertRaises(DeviceCredentialStoreError):
            WindowsDowntimeCredentialStore(api).load()
        self.assertEqual((DEVICE_ID, b"broken"), api.records[DOWNTIME_CREDENTIAL_TARGET])

    def test_corrupt_legacy_does_not_block_fresh_downtime_pairing(self):
        api = MultiCredentialApi()
        api.records[CREDENTIAL_TARGET] = (DEVICE_ID, b"broken")
        downtime = WindowsDowntimeCredentialStore(api)
        self.assertIsNone(downtime.load())
        self.assertEqual((DEVICE_ID, b"broken"), api.records[CREDENTIAL_TARGET])
        downtime.save(credentials("ghd_fresh_downtime"))
        self.assertEqual("ghd_fresh_downtime", downtime.load().device_token)

    def test_legacy_storage_read_failure_is_not_ignored(self):
        api = MultiCredentialApi()

        def fail_legacy_read(target):
            if target == CREDENTIAL_TARGET:
                raise OSError(DEVICE_TOKEN)
            return None

        api.read = fail_legacy_read
        with self.assertRaises(DeviceCredentialStoreError) as caught:
            WindowsDowntimeCredentialStore(api).load()
        self.assertNotIn(DEVICE_TOKEN, str(caught.exception))
        self.assertEqual({}, api.records)

    def test_failed_copy_never_returns_unsaved_legacy_or_echoes_secrets(self):
        api = MultiCredentialApi()
        ears = WindowsDeviceCredentialStore(api)
        ears.save(credentials())

        def fail_write(target, username, secret):
            raise OSError(secret.decode("utf-8"))

        api.write = fail_write
        with self.assertRaises(DeviceCredentialStoreError) as caught:
            WindowsDowntimeCredentialStore(api).load()
        self.assertNotIn(DEVICE_TOKEN, str(caught.exception))
        self.assertEqual(credentials(), ears.load())
        self.assertNotIn(DOWNTIME_CREDENTIAL_TARGET, api.records)


if __name__ == "__main__":
    unittest.main(verbosity=2)
