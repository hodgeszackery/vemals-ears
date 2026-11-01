"""Synthetic handoffs only: no deployed host state, credentials or services."""

import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from Hosting import handoff


PASSWORD = "XENO synthetic password 41"
GUILD = "123456789012345678"
STAMP = "2026-11-01T20:00:00.000Z"


def payload():
    return {
        "schema": 1, "kind": handoff.KIND,
        "guild_id": GUILD, "guild_name": "XENO Test Guild",
        "application_id": "234567890123456789",
        "public_server_url": "wss://ears.example.org",
        "public_http_url": "https://ears.example.org",
        "client_download_url": "https://ears.example.org/download",
        "bot_token": "XENO_synthetic_discord_bot_token",
        "publisher_token": "XENO_synthetic_stable_publisher_token",
        "consent": {"version": 1, "updatedAt": STAMP,
                    "entries": {GUILD + ":1": "granted", GUILD + ":2": "declined"}},
        "mapping": {"version": 1, "updatedAt": STAMP, "entries": {
            GUILD + ":1": {"characterName": "Xenotest", "realm": "Test Realm",
                           "wowClass": "paladin", "role": "tank", "mappedAt": STAMP},
        }},
    }


def inspect_acl(path):
    script = (
        "$ErrorActionPreference = 'Stop'; "
        "$acl = [IO." + ("Directory" if path.is_dir() else "File") + "]::GetAccessControl($env:XENO_HANDOFF_TEST_PATH); "
        "@{ protected=$acl.AreAccessRulesProtected; sddl=$acl.GetSecurityDescriptorSddlForm([Security.AccessControl.AccessControlSections]::All); "
        "currentSid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value; "
        "sids=@($acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]) | "
        "ForEach-Object { $_.IdentityReference.Value }) } | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        env={**os.environ, "XENO_HANDOFF_TEST_PATH": str(path)},
        capture_output=True, text=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode or result.stderr:
        raise AssertionError("Synthetic ACL inspection failed: " + result.stderr)
    return json.loads(result.stdout)


class HandoffValidationTests(unittest.TestCase):
    def test_exact_fields_preserve_declines_and_unicode_mappings_in_detached_copy(self):
        original = payload()
        original["mapping"]["entries"][GUILD + ":1"]["characterName"] = "\u00c9lan"
        validated = handoff.validate_payload(original)
        self.assertEqual(original, validated)
        self.assertEqual("declined", validated["consent"]["entries"][GUILD + ":2"])
        validated["consent"]["entries"].clear()
        self.assertEqual(2, len(original["consent"]["entries"]))
        self.assertTrue(handoff._json_bytes(original).isascii())

    def test_excluded_state_and_unknown_fields_are_rejected_not_silently_copied(self):
        for field in ("database", "pepper", "tunnel", "accounts", "sessions", "transcripts", "path", "files"):
            with self.subTest(field=field):
                value = payload()
                value[field] = "XENO_never_export"
                with self.assertRaisesRegex(handoff.HandoffError, "invalid or unsupported"):
                    handoff.validate_payload(value)
        for ledger in ("consent", "mapping"):
            value = payload()
            value[ledger]["sessions"] = {}
            with self.assertRaises(handoff.HandoffError):
                handoff.validate_payload(value)
        value = payload()
        value["mapping"]["entries"][GUILD + ":1"]["path"] = "C:/XENO"
        with self.assertRaises(handoff.HandoffError):
            handoff.validate_payload(value)

    def test_mixed_guild_or_corrupt_ledger_never_salvages_grants(self):
        for ledger in ("consent", "mapping"):
            value = payload()
            value[ledger]["entries"]["345678901234567890:1"] = copy.deepcopy(next(iter(value[ledger]["entries"].values())))
            with self.assertRaises(handoff.HandoffError):
                handoff.validate_payload(value)
        for invalid in (None, True, "pending", {"state": "granted"}):
            value = payload()
            value["consent"]["entries"][GUILD + ":2"] = invalid
            with self.assertRaises(handoff.HandoffError):
                handoff.validate_payload(value)
        for ledger in ("consent", "mapping"):
            value = payload()
            value[ledger]["updatedAt"] = "invalid"
            with self.assertRaises(handoff.HandoffError):
                handoff.validate_payload(value)

    def test_origins_are_strict_matching_public_authorities(self):
        for invalid in ("ws://ears.example.org", "wss://127.0.0.1", "wss://localhost",
                        "wss://user:pass@ears.example.org", "wss://ears.example.org/",
                        "wss://ears.example.org?", "wss://ears.example.org#", "wss://ears.example.org:",
                        "wss://ears.example.org:0", "wss://ears.example.org:444", "wss://another.example.org",
                        "wss://ears.example.org\\path", "wss://ears.example.org\n"):
            with self.subTest(value=invalid):
                value = payload()
                value["public_server_url"] = invalid
                with self.assertRaises(handoff.HandoffError):
                    handoff.validate_payload(value)
        value = payload()
        value["public_server_url"] += ":8443"
        value["public_http_url"] += ":8443"
        self.assertEqual(value, handoff.validate_payload(value))

    def test_payload_types_lengths_and_entry_count_are_bounded(self):
        for key, invalid in (("schema", True), ("bot_token", "with spaces"), ("publisher_token", "x" * 513),
                             ("application_id", "123"), ("guild_name", "x" * 81),
                             ("client_download_url", "file:///C:/XENO")):
            value = payload()
            value[key] = invalid
            with self.assertRaises(handoff.HandoffError):
                handoff.validate_payload(value)
        value = payload()
        value["consent"]["entries"] = {GUILD + ":" + str(index): "declined" for index in range(10_001)}
        with self.assertRaises(handoff.HandoffError):
            handoff.validate_payload(value)

    def test_password_bounds_reject_before_crypto(self):
        self.assertIsNone(handoff.validate_password(PASSWORD))
        for password in ("", "short", "x" * 257, "XENO password\n", "XENO password\x00", "XENO password\ud800"):
            with mock.patch.object(handoff, "_aes") as aes:
                with self.assertRaises(handoff.HandoffError):
                    handoff.validate_password(password)
                with self.assertRaises(handoff.HandoffError):
                    handoff.encrypt_bundle(payload(), password)
                aes.assert_not_called()

    def test_invalid_publisher_keys_fail_before_any_private_state_write(self):
        for token in ("x", "x" * 193, "XENO_" + "!" * 40, "XENO_" + "=" * 40):
            value = payload()
            value["publisher_token"] = token
            with mock.patch.object(handoff, "_write_private") as write, mock.patch.object(handoff.setup, "protect_directory") as protect:
                with self.assertRaises(handoff.HandoffError):
                    handoff.import_worker(value, Path("C:/XENO-never-created"))
                write.assert_not_called()
                protect.assert_not_called()

    def test_untrusted_lengths_versions_and_iterations_are_checked_before_kdf(self):
        good = handoff.HEADER.pack(handoff.MAGIC, handoff.ITERATIONS, b"s" * 32, b"i" * 16, 16) + b"c" * 16 + b"m" * 32
        cases = [b"", good[:-1], good + b"trailing", b"x" * (handoff.MAX_BUNDLE_BYTES + 1)]
        for offset in (0, 11, 12, 64):
            changed = bytearray(good)
            changed[offset] ^= 1
            cases.append(bytes(changed))
        with mock.patch.object(handoff.hashlib, "pbkdf2_hmac") as kdf, mock.patch.object(handoff, "_aes") as aes:
            for value in cases:
                with self.assertRaisesRegex(handoff.HandoffError, handoff.OPEN_ERROR):
                    handoff.decrypt_bundle(value, PASSWORD)
            kdf.assert_not_called()
            aes.assert_not_called()


@unittest.skipUnless(os.name == "nt", "Windows AES and ACL integration")
class WindowsHandoffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = handoff.encrypt_bundle(payload(), PASSWORD)

    def assert_private_acl(self, path, *, protected):
        acl = inspect_acl(path)
        if protected:
            self.assertTrue(acl["protected"])
        expected = {acl["currentSid"], "S-1-5-18", "S-1-5-32-544"}
        self.assertTrue(expected.issubset(set(acl["sids"])))
        self.assertTrue(set(acl["sids"]).issubset(expected | {"S-1-3-4"}))

    def test_real_crypto_roundtrip_randomness_and_no_plaintext(self):
        self.assertEqual(payload(), handoff.decrypt_bundle(self.bundle, PASSWORD))
        second = handoff.encrypt_bundle(payload(), PASSWORD)
        first_header, second_header = handoff.HEADER.unpack_from(self.bundle), handoff.HEADER.unpack_from(second)
        self.assertNotEqual(first_header[2], second_header[2])
        self.assertNotEqual(first_header[3], second_header[3])
        self.assertNotEqual(self.bundle, second)
        for value in (PASSWORD, payload()["bot_token"], payload()["publisher_token"], GUILD):
            self.assertNotIn(value.encode("ascii"), self.bundle)

    def test_aes_matches_nist_cbc256_vector_before_pkcs7_padding(self):
        # NIST SP 800-38A F.2.5, first block. CBC padding appends another block.
        # https://nvlpubs.nist.gov/nistpubs/Legacy/SP/nistspecialpublication800-38a.pdf
        key = bytes.fromhex("603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4")
        iv = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        clear = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a")
        encrypted = handoff._aes(clear, key, iv, decrypt=False)
        self.assertEqual(bytes.fromhex("f58c4c04d6e5f1ba779eabfb5f7bfbd6"), encrypted[:16])
        self.assertEqual(32, len(encrypted))
        self.assertEqual(clear, handoff._aes(encrypted, key, iv, decrypt=True))

    def test_wrong_password_salt_iv_ciphertext_and_mac_never_reach_decryption(self):
        cases = [(self.bundle, "XENO incorrect password")]
        for offset in (16, 48, handoff.HEADER.size, len(self.bundle) - 1):
            changed = bytearray(self.bundle)
            changed[offset] ^= 1
            cases.append((bytes(changed), PASSWORD))
        with mock.patch.object(handoff, "_aes") as aes:
            for data, password in cases:
                with self.subTest(offset=data[:20], wrong_password=password != PASSWORD):
                    with self.assertRaises(handoff.HandoffError) as caught:
                        handoff.decrypt_bundle(data, password)
                    self.assertEqual(handoff.OPEN_ERROR, str(caught.exception))
            aes.assert_not_called()

    def test_keys_are_separate_and_authentication_covers_the_complete_header(self):
        _, _, salt, _, _ = handoff.HEADER.unpack_from(self.bundle)
        keys = hashlib.pbkdf2_hmac("sha256", PASSWORD.encode(), salt, handoff.ITERATIONS, dklen=64)
        self.assertNotEqual(keys[:32], keys[32:])
        self.assertEqual(hmac.digest(keys[32:], self.bundle[:-32], "sha256"), self.bundle[-32:])
        with mock.patch.object(handoff, "_aes", return_value=handoff._json_bytes(payload())) as aes:
            handoff.decrypt_bundle(self.bundle, PASSWORD)
        self.assertEqual(keys[:32], aes.call_args.args[1])

    def test_authenticated_plaintext_still_rejects_duplicate_keys_and_excluded_data(self):
        bad = payload()
        bad["sessions"] = {"XENO": "not permitted"}
        for raw in (b'{"schema":1,"schema":1}', handoff._json_bytes(bad), b"[" * 2000):
            with mock.patch.object(handoff, "_aes", return_value=raw):
                with self.assertRaisesRegex(handoff.HandoffError, handoff.OPEN_ERROR):
                    handoff.decrypt_bundle(self.bundle, PASSWORD)

    def test_crypto_process_uses_hidden_binary_pipes_without_secret_arguments(self):
        key, iv = b"k" * 32, b"i" * 16
        with mock.patch.object(handoff.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=b"c" * 16)) as run:
            handoff._aes(b"XENO", key, iv, decrypt=False)
        args, kwargs = run.call_args
        self.assertNotIn("XENO", " ".join(args[0]))
        self.assertNotIn(key.decode(), " ".join(args[0]))
        self.assertEqual(bytes((1, 1)) + key + iv + struct.pack("<I", 4) + b"XENO", kwargs["input"])
        self.assertEqual(subprocess.CREATE_NO_WINDOW, kwargs["creationflags"])
        self.assertEqual(subprocess.PIPE, kwargs["stdout"])
        self.assertNotIn("env", kwargs)

    def test_helper_rejects_oversized_binary_input_without_stdout_or_stderr(self):
        command = [str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"),
                   "-NoProfile", "-NonInteractive", "-File", str(handoff.HELPER)]
        invalid = bytes((1, 1)) + b"k" * 32 + b"i" * 16 + struct.pack("<I", 0xffffffff)
        result = subprocess.run(command, input=invalid, capture_output=True, timeout=10,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(1, result.returncode)
        self.assertEqual(b"", result.stdout)
        self.assertEqual(b"", result.stderr)

    def test_bundle_file_is_private_parent_unchanged_and_overwrite_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            target = root / "XENO-handoff.vehost"
            before_acl = inspect_acl(root)["sddl"]
            self.assertEqual(target, handoff.write_bundle(target, payload(), PASSWORD))
            self.assertEqual(payload(), handoff.read_bundle(target, PASSWORD))
            self.assertEqual(before_acl, inspect_acl(root)["sddl"])
            self.assert_private_acl(target, protected=True)
            original = target.read_bytes()
            with mock.patch.object(handoff, "encrypt_bundle") as encrypt:
                with self.assertRaises(handoff.HandoffError):
                    handoff.write_bundle(target, payload(), PASSWORD)
                encrypt.assert_not_called()
            self.assertEqual(original, target.read_bytes())
            self.assertEqual([target], list(root.iterdir()))

    def test_import_creates_only_worker_state_with_preserved_keys_and_consent(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder).resolve() / "worker"
            values = handoff.import_worker(payload(), state)
            files = {path.relative_to(state).as_posix() for path in state.rglob("*") if path.is_file()}
            self.assertEqual({"relay.env", "host-settings.json", "consent/consent.json", "consent/mapping.json",
                              "secrets/publisher-token", "secrets/discord-application-id", "secrets/discord-bot-token"}, files)
            self.assertEqual("remote-worker", values["VEMALS_EARS_HOST_MODE"])
            self.assertEqual({"schema": 1, "mode": "remote-worker", "takeover_confirmed": False},
                             json.loads((state / "host-settings.json").read_bytes()))
            for name in ("consent", "mapping"):
                self.assertEqual(payload()[name], json.loads((state / "consent" / (name + ".json")).read_bytes()))
            for field, name in (("publisher_token", "publisher-token"), ("bot_token", "discord-bot-token")):
                self.assertEqual(payload()[field] + "\n", (state / "secrets" / name).read_text())
                self.assertNotIn(payload()[field], (state / "relay.env").read_text())
                self.assert_private_acl(state / "secrets" / name, protected=False)
            self.assert_private_acl(state, protected=True)
            self.assertFalse((state / "relay").exists())
            self.assertFalse((state / "tunnel").exists())
            self.assertFalse((state / "consent/sessions.json").exists())

    def test_import_is_accepted_by_remote_worker_loader_but_activation_is_gated(self):
        from Hosting import service
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder).resolve() / "worker"
            handoff.import_worker(payload(), state)
            values = service.load_configuration(state)
            self.assertEqual("remote-worker", values["VEMALS_EARS_HOST_MODE"])
            self.assertEqual(payload()["public_server_url"], values["VEMALS_EARS_SERVER_URL"])
            self.assertEqual(str(state / "consent/consent.json"), values["CAPTIONS_CONSENT_STATE_PATH"])
            self.assertEqual(str(state / "consent/mapping.json"), values["CAPTIONS_MAPPING_STATE_PATH"])
            self.assertEqual(str(state / "profile"), values["LOCALAPPDATA"])
            with self.assertRaisesRegex(service.ConfigurationError, "previous caption worker is stopped"):
                service.validate_activation(values)

    def test_failed_acl_or_write_never_publishes_half_state_or_plaintext_export(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            with mock.patch.object(handoff.setup, "protect_directory", side_effect=handoff.setup.SetupError("XENO ACL failure")), \
                    mock.patch.object(handoff, "_write_private") as write:
                with self.assertRaises(handoff.HandoffError):
                    handoff.import_worker(payload(), root / "worker")
                with self.assertRaises(handoff.HandoffError):
                    handoff.write_bundle(root / "export.vehost", payload(), PASSWORD)
                write.assert_not_called()
            original_write = handoff._write_private
            calls = 0
            def fail_second(path, content):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("XENO disk failure")
                original_write(path, content)
            with mock.patch.object(handoff, "_write_private", side_effect=fail_second):
                with self.assertRaises(handoff.HandoffError):
                    handoff.import_worker(payload(), root / "worker")
            self.assertEqual([], list(root.iterdir()))

    def test_destination_created_during_import_or_export_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            original_rename = handoff._rename_new
            def raced(source, target):
                if source.is_dir():
                    target.mkdir()
                    (target / "XENO-existing").write_bytes(b"keep")
                else:
                    target.write_bytes(b"keep")
                original_rename(source, target)
            with mock.patch.object(handoff, "_rename_new", side_effect=raced):
                with self.assertRaises(handoff.HandoffError):
                    handoff.import_worker(payload(), root / "worker")
                with self.assertRaises(handoff.HandoffError):
                    handoff.write_bundle(root / "export.vehost", payload(), PASSWORD)
            self.assertEqual(b"keep", (root / "worker/XENO-existing").read_bytes())
            self.assertEqual(b"keep", (root / "export.vehost").read_bytes())
            self.assertEqual({"worker", "export.vehost"}, {path.name for path in root.iterdir()})

    def test_post_publication_cleanup_failure_does_not_report_failed_handoff(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            original_cleanup = tempfile.TemporaryDirectory.cleanup
            def late_failure(temporary):
                original_cleanup(temporary)
                raise OSError("XENO cleanup failed after successful publication")
            with mock.patch.object(tempfile.TemporaryDirectory, "cleanup", late_failure):
                target = handoff.write_bundle(root / "export.vehost", payload(), PASSWORD)
                values = handoff.import_worker(payload(), root / "worker")
            self.assertEqual(payload(), handoff.read_bundle(target, PASSWORD))
            self.assertEqual("remote-worker", values["VEMALS_EARS_HOST_MODE"])
            self.assertEqual({"worker", "export.vehost"}, {path.name for path in root.iterdir()})

    def test_read_rejects_large_nonregular_and_reparse_inputs_before_decryption(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            target = root / "oversized.vehost"
            with target.open("wb") as stream:
                stream.truncate(handoff.MAX_BUNDLE_BYTES + 1)
            with mock.patch.object(handoff, "decrypt_bundle") as decrypt:
                for path in (root, target):
                    with self.assertRaisesRegex(handoff.HandoffError, handoff.OPEN_ERROR):
                        handoff.read_bundle(path, PASSWORD)
                decrypt.assert_not_called()
            with mock.patch.object(Path, "lstat", return_value=SimpleNamespace(st_mode=0, st_file_attributes=0x400)):
                with self.assertRaises(handoff.HandoffError):
                    handoff._safe_path(root / "linked")


if __name__ == "__main__":
    unittest.main()
