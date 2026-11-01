import concurrent.futures
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

from GuildHub.store import (
    HOSTED_JOBS_CAPABILITY,
    CredentialStore,
    PairingError,
)


PEPPER = "test-pepper-is-at-least-thirty-two-characters-long"


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "guild-hub.sqlite3"
        self.now = [1_000.0]
        self.store = CredentialStore(
            self.database,
            PEPPER,
            clock=lambda: self.now[0],
        )
        self.store.initialize()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_database_never_contains_raw_enrollment_or_device_secret(self):
        enrollment = self.store.create_enrollment(300)
        credentials = self.store.claim_enrollment(
            enrollment.code, "Raid desktop", "0.1.0"
        )

        self.assertNotIn(enrollment.code, repr(enrollment))
        self.assertNotIn(credentials.device_token, repr(credentials))

        database_bytes = self.database.read_bytes()
        self.assertNotIn(enrollment.code.encode("ascii"), database_bytes)
        self.assertNotIn(credentials.device_token.encode("ascii"), database_bytes)

        with closing(sqlite3.connect(self.database)) as connection:
            code_hash = connection.execute(
                "SELECT code_hash FROM enrollments"
            ).fetchone()[0]
            token_hash = connection.execute(
                "SELECT token_hash FROM devices"
            ).fetchone()[0]
        self.assertEqual(32, len(code_hash))
        self.assertEqual(32, len(token_hash))

    def test_expired_and_reused_enrollments_fail_the_same_store_operation(self):
        expired = self.store.create_enrollment(10)
        self.now[0] += 11
        with self.assertRaises(PairingError):
            self.store.claim_enrollment(expired.code, "Desktop", "0.1.0")

        valid = self.store.create_enrollment(10)
        self.store.claim_enrollment(valid.code, "Desktop", "0.1.0")
        with self.assertRaises(PairingError):
            self.store.claim_enrollment(valid.code, "Other", "0.1.0")

    def test_only_one_concurrent_claim_can_succeed(self):
        enrollment = self.store.create_enrollment(300)
        barrier = threading.Barrier(2)

        def claim(name):
            barrier.wait(timeout=2)
            try:
                return self.store.claim_enrollment(enrollment.code, name, "0.1.0")
            except PairingError:
                return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, ["one", "two"]))

        successful = [result for result in results if result is not None]
        self.assertEqual(1, len(successful))
        self.assertEqual(1, len(self.store.list_devices()))

    def test_authentication_updates_last_seen_and_revocation_is_immediate(self):
        enrollment = self.store.create_enrollment(300)
        credentials = self.store.claim_enrollment(
            enrollment.code, "Raid desktop", "0.1.0"
        )
        self.now[0] += 5

        self.assertFalse(
            self.store.authenticate_device(credentials.device_id, "ghd_wrong")
        )
        self.assertFalse(
            self.store.authenticate_device("ghdev_unknown", credentials.device_token)
        )
        self.assertTrue(
            self.store.authenticate_device(
                credentials.device_id, credentials.device_token
            )
        )
        self.assertEqual(self.now[0], self.store.list_devices()[0].last_seen_at)

        self.assertTrue(self.store.revoke_device(credentials.device_id))
        self.assertFalse(self.store.revoke_device(credentials.device_id))
        self.assertFalse(
            self.store.authenticate_device(
                credentials.device_id, credentials.device_token
            )
        )
        self.assertIsNotNone(self.store.list_devices()[0].revoked_at)

    def test_hosted_compute_is_operator_granted_and_defaults_off(self):
        enrollment = self.store.create_enrollment(300)
        credentials = self.store.claim_enrollment(
            enrollment.code, "Raid desktop", "0.1.0"
        )

        self.assertFalse(
            self.store.device_has_capability(
                credentials.device_id, HOSTED_JOBS_CAPABILITY
            )
        )
        self.assertFalse(self.store.list_devices()[0].hosted_jobs)

        self.assertTrue(
            self.store.set_device_capability(
                credentials.device_id, HOSTED_JOBS_CAPABILITY, True
            )
        )
        self.assertTrue(
            self.store.device_has_capability(
                credentials.device_id, HOSTED_JOBS_CAPABILITY
            )
        )
        self.assertTrue(self.store.list_devices()[0].hosted_jobs)

        self.assertTrue(
            self.store.set_device_capability(
                credentials.device_id, HOSTED_JOBS_CAPABILITY, False
            )
        )
        self.assertFalse(self.store.list_devices()[0].hosted_jobs)

    def test_revoked_or_unknown_device_cannot_gain_hosted_compute(self):
        enrollment = self.store.create_enrollment(300)
        credentials = self.store.claim_enrollment(
            enrollment.code, "Raid desktop", "0.1.0"
        )
        self.assertTrue(self.store.revoke_device(credentials.device_id))
        self.assertFalse(
            self.store.set_device_capability(
                credentials.device_id, HOSTED_JOBS_CAPABILITY, True
            )
        )
        self.assertFalse(
            self.store.set_device_capability(
                "ghdev_unknown", HOSTED_JOBS_CAPABILITY, True
            )
        )
        self.assertFalse(
            self.store.device_has_capability(
                credentials.device_id, HOSTED_JOBS_CAPABILITY
            )
        )

    def test_unknown_capability_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.set_device_capability("ghdev_unknown", "admin", True)

    def test_admin_assigned_quick_chat_profile_is_validated_and_authenticated(self):
        enrollment = self.store.create_enrollment(300)
        credentials = self.store.claim_enrollment(
            enrollment.code, "Raid desktop", "0.1.0"
        )
        initial = self.store.list_devices()[0]
        self.assertIsNone(initial.quick_chat_name)
        self.assertEqual("", initial.quick_chat_wow_class)

        self.assertTrue(
            self.store.set_quick_chat_profile(
                credentials.device_id, "  Mira   Lyn  ", "MAGE"
            )
        )
        record = self.store.authenticate_device_record(
            credentials.device_id, credentials.device_token
        )
        self.assertIsNotNone(record)
        self.assertEqual("Mira Lyn", record.quick_chat_name)
        self.assertEqual("mage", record.quick_chat_wow_class)
        self.assertIsNone(
            self.store.authenticate_device_record(
                credentials.device_id, "ghd_wrong"
            )
        )

        for name, wow_class in (
            ("", "mage"),
            ("x" * 33, "mage"),
            ("Mira\nLyn", "mage"),
            ("Mira\u202eLyn", "mage"),
            ("Mira", "bard"),
        ):
            with self.subTest(name=name, wow_class=wow_class), self.assertRaises(
                ValueError
            ):
                self.store.set_quick_chat_profile(
                    credentials.device_id, name, wow_class
                )
        self.assertFalse(
            self.store.set_quick_chat_profile("ghdev_unknown", "Mira", "mage")
        )

    def test_member_name_update_is_canonical_and_preserves_operator_class(self):
        enrollment = self.store.create_enrollment(300)
        credentials = self.store.claim_enrollment(
            enrollment.code, "Raid desktop", "0.1.0"
        )
        self.assertTrue(
            self.store.set_quick_chat_profile(
                credentials.device_id, "Old name", "mage"
            )
        )

        canonical = self.store.set_quick_chat_name(
            credentials.device_id, "  \uff2dira   Lyn  "
        )
        self.assertEqual("Mira Lyn", canonical)
        record = self.store.authenticate_device_record(
            credentials.device_id, credentials.device_token
        )
        self.assertIsNotNone(record)
        self.assertEqual("Mira Lyn", record.quick_chat_name)
        self.assertEqual("mage", record.quick_chat_wow_class)

    def test_member_name_update_refuses_unknown_or_revoked_devices(self):
        self.assertIsNone(
            self.store.set_quick_chat_name("ghdev_unknown", "Mira")
        )
        enrollment = self.store.create_enrollment(300)
        credentials = self.store.claim_enrollment(
            enrollment.code, "Raid desktop", "0.1.0"
        )
        self.assertTrue(self.store.revoke_device(credentials.device_id))
        self.assertIsNone(
            self.store.set_quick_chat_name(credentials.device_id, "Mira")
        )

    def test_initialize_migrates_existing_devices_without_assigning_identity(self):
        legacy_database = (
            Path(self.temporary_directory.name) / "legacy-guild-hub.sqlite3"
        )
        with closing(sqlite3.connect(legacy_database)) as connection:
            connection.execute(
                """
                CREATE TABLE devices (
                    device_id TEXT PRIMARY KEY,
                    token_hash BLOB NOT NULL UNIQUE,
                    device_name TEXT NOT NULL,
                    client_version TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_seen_at REAL,
                    revoked_at REAL
                )
                """
            )
            connection.execute(
                "INSERT INTO devices VALUES (?, ?, ?, ?, ?, NULL, NULL)",
                ("ghdev_legacy", sqlite3.Binary(bytes(32)), "Legacy", "0.1", 1.0),
            )
            connection.commit()

        legacy_store = CredentialStore(legacy_database, PEPPER)
        legacy_store.initialize()
        record = legacy_store.list_devices()[0]
        self.assertEqual("ghdev_legacy", record.device_id)
        self.assertIsNone(record.quick_chat_name)
        self.assertEqual("", record.quick_chat_wow_class)
        self.assertIsNone(record.viewer_token)
        with closing(sqlite3.connect(legacy_database)) as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(devices)")
            }
        self.assertIn("quick_chat_name", columns)
        self.assertIn("quick_chat_wow_class", columns)
        self.assertIn("viewer_token", columns)

    def test_opaque_viewer_binding_requires_device_authentication_and_persists(self):
        enrollment = self.store.create_enrollment(300)
        credentials = self.store.claim_enrollment(
            enrollment.code, "Raid desktop", "0.1.0"
        )
        viewer_token = "0123456789abcdef01234567"

        self.assertIsNone(
            self.store.authenticate_device_viewer_token(
                credentials.device_id, credentials.device_token
            )
        )
        self.assertIsNone(
            self.store.authenticate_device_viewer_token(
                credentials.device_id, "ghd_wrong"
            )
        )
        self.assertTrue(
            self.store.bind_viewer_token(credentials.device_id, viewer_token)
        )

        reopened = CredentialStore(self.database, PEPPER)
        reopened.initialize()
        self.assertEqual(
            viewer_token,
            reopened.authenticate_device_viewer_token(
                credentials.device_id, credentials.device_token
            ),
        )
        self.assertEqual(viewer_token, reopened.list_devices()[0].viewer_token)

        with self.assertRaises(ValueError):
            reopened.bind_viewer_token(
                credentials.device_id, "discord-user-123456789"
            )
        self.assertFalse(
            reopened.bind_viewer_token("ghdev_unknown", "f" * 24)
        )

    def test_pepper_is_required_and_enrollment_lifetime_is_bounded(self):
        with self.assertRaises(ValueError):
            CredentialStore(self.database, "too-short")
        with self.assertRaises(ValueError):
            self.store.create_enrollment(0)
        with self.assertRaises(ValueError):
            self.store.create_enrollment(8 * 24 * 60 * 60)


class ReportedVersionTests(unittest.TestCase):
    """The device list must stop lying about what a member is running.

    client_version was written once at enrollment and never again, so it
    read as the version at PAIRING. On 2026-08-31 that was mistaken for a
    live report and nearly cost five members their pairing.
    """

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.now = [1_000.0]
        self.store = CredentialStore(
            Path(self.temporary_directory.name) / "hub.sqlite3",
            PEPPER,
            clock=lambda: self.now[0],
        )
        self.store.initialize()
        enrollment = self.store.create_enrollment(300)
        self.credentials = self.store.claim_enrollment(
            enrollment.code, "Raid desktop", "0.1.0"
        )

    def version(self):
        return self.store.list_devices()[0].client_version

    def test_a_reported_version_replaces_the_pairing_time_one(self):
        self.assertEqual("0.1.0", self.version())
        self.assertTrue(
            self.store.authenticate_device(
                self.credentials.device_id,
                self.credentials.device_token,
                "0.2.2",
            )
        )
        self.assertEqual("0.2.2", self.version())

    def test_a_silent_client_never_erases_the_known_version(self):
        """A client that reports nothing must not blank the column.

        Writing "" for every quiet client would make the field worse than
        the stale value it replaced - and the 15 second revalidation loop
        calls this method with no version on every single tick.
        """
        self.store.authenticate_device(
            self.credentials.device_id, self.credentials.device_token, "0.2.2"
        )
        self.assertEqual("0.2.2", self.version())
        for _ in range(3):
            self.now[0] += 15
            self.assertTrue(
                self.store.authenticate_device(
                    self.credentials.device_id, self.credentials.device_token
                )
            )
        self.assertEqual("0.2.2", self.version())
        self.assertEqual(self.now[0], self.store.list_devices()[0].last_seen_at)

    def test_a_failed_authentication_records_nothing(self):
        self.assertFalse(
            self.store.authenticate_device(
                self.credentials.device_id, "ghd_wrong", "9.9.9"
            )
        )
        self.assertEqual("0.1.0", self.version())

    def test_a_revoked_device_cannot_report_a_version(self):
        self.store.revoke_device(self.credentials.device_id)
        self.assertFalse(
            self.store.authenticate_device(
                self.credentials.device_id,
                self.credentials.device_token,
                "0.2.2",
            )
        )
        self.assertEqual("0.1.0", self.version())



class InvitedPairingTests(unittest.TestCase):
    """Pairing carries the Discord identity, so presence can gate captions.

    Shino, 2026-09-01: the "link Discord" step is the wrong way round.
    An invitation issued in Discord already knows who asked for it, so the
    device that claims it is theirs from the first second, and leaving the
    voice channel can end their captions with nothing else to configure.
    """

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = CredentialStore(
            Path(self.folder.name) / "state.sqlite3", PEPPER
        )
        self.store.initialize()

    def viewer_token_of(self, device_id):
        with closing(self.store._connect()) as connection:
            row = connection.execute(
                "SELECT viewer_token FROM devices WHERE device_id = ?",
                (device_id,),
            ).fetchone()
        return None if row is None else row[0]

    def test_a_device_inherits_the_identity_of_its_invitation(self):
        token = "a1b2c3d4e5f6a7b8c9d0e1f2"
        invitation = self.store.create_enrollment(600.0, viewer_token=token)
        device = self.store.claim_enrollment(invitation.code, "Raid PC", "0.4.4")
        self.assertEqual(self.viewer_token_of(device.device_id), token)

    def test_an_ordinary_enrollment_still_carries_no_identity(self):
        invitation = self.store.create_enrollment(600.0)
        device = self.store.claim_enrollment(invitation.code, "Raid PC", "0.4.4")
        self.assertIsNone(self.viewer_token_of(device.device_id))

    def test_the_relay_chooses_the_code_only_when_discord_already_showed_it(self):
        token = "a1b2c3d4e5f6a7b8c9d0e1f2"
        code = "ghe_" + "A" * 32
        invitation = self.store.create_enrollment(600.0, code=code, viewer_token=token)
        self.assertEqual(invitation.code, code)
        # One invitation, one device: a repeat of the same code is refused
        # rather than quietly minting a second way in.
        with self.assertRaises(PairingError):
            self.store.create_enrollment(600.0, code=code, viewer_token=token)

    def test_a_malformed_code_or_identity_is_refused(self):
        with self.assertRaises(ValueError):
            self.store.create_enrollment(600.0, code="not-ours")
        with self.assertRaises(ValueError):
            self.store.create_enrollment(600.0, viewer_token="nope")
        with self.assertRaises(ValueError):
            self.store.create_enrollment(600.0, viewer_token="A" * 24)


class CredentialHousekeepingTests(unittest.TestCase):
    """Shino, 2026-09-02: "our approval tokens are cluttered again - can we
    auto revoke or delete unused keys". The clutter was real: 17 devices,
    3 of them never once connected, and 11 spent invitations that
    prune_enrollments would have removed if anything had ever called it.
    """

    DAY = 24 * 3600.0

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.now = [1_000_000.0]
        self.store = CredentialStore(
            Path(self.temporary_directory.name) / "guild-hub.sqlite3",
            PEPPER,
            clock=lambda: self.now[0],
        )
        self.store.initialize()

    def pair(self, name):
        enrollment = self.store.create_enrollment(3600.0)
        return self.store.claim_enrollment(enrollment.code, name, "0.4.9")

    def connect(self, device):
        """Authenticating IS what stamps last_seen_at, so use the real path."""
        return self.store.authenticate_device(
            device.device_id, device.device_token, "0.4.9"
        )

    def state(self, device_id):
        with closing(self.store._connect()) as connection:
            row = connection.execute(
                "SELECT revoked_at FROM devices WHERE device_id = ?", (device_id,)
            ).fetchone()
        return "gone" if row is None else ("revoked" if row[0] else "live")

    def test_a_device_that_never_connected_is_retired_after_the_window(self):
        device = self.pair("Orphan PC")
        self.now[0] += 47 * 3600.0
        self.assertEqual((0, 0), self.store.revoke_unused_devices(
            unused_after=48 * 3600.0, idle_after=30 * self.DAY))
        self.assertEqual("live", self.state(device.device_id),
                         "a device was retired before its window elapsed")
        self.now[0] += 2 * 3600.0
        self.assertEqual((1, 0), self.store.revoke_unused_devices(
            unused_after=48 * 3600.0, idle_after=30 * self.DAY))
        self.assertEqual("revoked", self.state(device.device_id))

    def test_a_device_in_use_is_never_retired(self):
        device = self.pair("Raid PC")
        self.now[0] += 10 * self.DAY
        self.connect(device)
        self.now[0] += 60 * self.DAY
        # Seen 60 days ago is idle; seen just now is not. Prove the recent
        # one survives a sweep that retires the stale one beside it.
        stale = self.pair("Old PC")
        self.connect(stale)
        self.now[0] += 31 * self.DAY
        self.connect(device)
        never_used, idle = self.store.revoke_unused_devices(
            unused_after=2 * self.DAY, idle_after=30 * self.DAY)
        self.assertEqual(1, idle)
        self.assertEqual("revoked", self.state(stale.device_id))
        self.assertEqual("live", self.state(device.device_id),
                         "a device seen moments ago was retired")

    def test_zero_windows_disable_each_half_independently(self):
        never = self.pair("Never PC")
        seen = self.pair("Seen PC")
        self.connect(seen)
        self.now[0] += 90 * self.DAY
        self.assertEqual((0, 1), self.store.revoke_unused_devices(
            unused_after=0.0, idle_after=30 * self.DAY))
        self.assertEqual("live", self.state(never.device_id))
        self.assertEqual((1, 0), self.store.revoke_unused_devices(
            unused_after=2 * self.DAY, idle_after=0.0))

    def test_revoked_devices_are_forgotten_only_after_the_retention(self):
        device = self.pair("Old PC")
        self.store.revoke_device(device.device_id)
        self.now[0] += 29 * self.DAY
        self.assertEqual(0, self.store.forget_revoked_devices(older_than=30 * self.DAY))
        self.assertEqual("revoked", self.state(device.device_id),
                         "a recent revocation was erased before anyone could see it")
        self.now[0] += 2 * self.DAY
        self.assertEqual(1, self.store.forget_revoked_devices(older_than=30 * self.DAY))
        self.assertEqual("gone", self.state(device.device_id))

        # A zero retention DISABLES forgetting; it must not mean "forget
        # everything now". Prove it against a revoked device that exists.
        standing = self.pair("Revoked PC")
        self.store.revoke_device(standing.device_id)
        self.now[0] += 400 * self.DAY
        self.assertEqual(0, self.store.forget_revoked_devices(older_than=0.0))
        self.assertEqual("revoked", self.state(standing.device_id),
                         "a zero retention erased a revoked device instead of disabling")

    def test_a_live_device_is_never_forgotten(self):
        device = self.pair("Raid PC")
        self.now[0] += 400 * self.DAY
        self.assertEqual(0, self.store.forget_revoked_devices(older_than=1.0))
        self.assertEqual("live", self.state(device.device_id))

    def test_retiring_a_credential_actually_stops_it_working(self):
        """Revocation has to mean something, or this is just tidying a list."""
        orphan = self.pair("Orphan PC")
        kept = self.pair("Raid PC")
        self.assertTrue(self.connect(kept))
        self.now[0] += 3 * self.DAY
        self.assertTrue(self.connect(kept))
        never_used, _idle = self.store.revoke_unused_devices(
            unused_after=2 * self.DAY, idle_after=0.0)
        self.assertEqual(1, never_used)
        self.assertFalse(self.connect(orphan), "a retired credential still authenticated")
        self.assertTrue(self.connect(kept), "housekeeping retired a credential in use")

if __name__ == "__main__":
    unittest.main(verbosity=2)
