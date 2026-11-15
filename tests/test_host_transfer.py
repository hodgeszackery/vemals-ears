"""Synthetic host transfers only: temp directories, XENO_ values, no services or network."""

import base64
from contextlib import closing
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import struct
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from GuildHub import relay_env
from GuildHub.store import CredentialStore
from Hosting import handoff, service, transfer, transfer_pake

from tests.test_host_handoff import PASSWORD, inspect_acl, payload as worker_payload


GUILD = "123456789012345678"
APPLICATION = "234567890123456789"
STAMP = "2026-11-01T20:00:00.000Z"
TRANSFER = "0123456789abcdef0123456789abcdef"
BOT_TOKEN = "XENO_synthetic_discord_bot_token"
PUBLISHER = "XENO_synthetic_stable_publisher_token"
PEPPER = "XENO_synthetic_pepper_0123456789_abcdefghijklmnop"
TUNNEL = "XENO_synthetic_tunnel_token_0123456789abcdef"
POINTER = b'{"XENO":"synthetic pointer"}\n'
KEYS = SimpleNamespace(bundle_enc=b"\x11" * 32, bundle_mac=b"\x22" * 32, control_mac=b"\x33" * 32)
SECRET = b"\x5a" * 32
COMMITMENT = transfer_pake.release_commitment(TRANSFER, SECRET)
CONSENT = {"version": 1, "updatedAt": STAMP, "entries": {GUILD + ":1": "granted", GUILD + ":2": "declined"}}
MAPPING = {"version": 1, "updatedAt": STAMP, "entries": {
    GUILD + ":1": {"characterName": "Xenotest", "realm": "Test Realm",
                   "wowClass": "paladin", "role": "tank", "mappedAt": STAMP}}}


def make_database(path, pepper=PEPPER):
    """A real relay database with one paired device; returns its credentials."""
    path.parent.mkdir(parents=True, exist_ok=True)
    store = CredentialStore(path, pepper)
    store.initialize()
    enrollment = store.create_enrollment()
    return store.claim_enrollment(enrollment.code, "XENO device", "1.2.3")


def database_bytes(folder):
    make_database(Path(folder) / "source/guild-hub.sqlite3")
    return (Path(folder) / "source/guild-hub.sqlite3").read_bytes()


def settings():
    return {
        "VEMALS_EARS_GUILD_ID": GUILD, "VEMALS_EARS_GUILD_NAME": "XENO Test Guild",
        "VEMALS_EARS_PORT": "8770",
        "VEMALS_EARS_PUBLIC_SERVER_URL": "wss://ears.example.org",
        "VEMALS_EARS_PUBLIC_HTTP_URL": "https://ears.example.org",
        "VEMALS_EARS_CLIENT_DOWNLOAD_URL": "https://ears.example.org/download",
        "VEMALS_EARS_CLIENT_VERSION": "1.2.3", "VEMALS_EARS_CONNECTION_LIMIT": "64",
    }


def blobs(database, *, tunnel=True, pointer=True):
    result = {
        "secrets/discord-bot-token": BOT_TOKEN.encode(), "secrets/discord-application-id": APPLICATION.encode(),
        "secrets/publisher-token": PUBLISHER.encode(), "secrets/pepper": PEPPER.encode(),
        transfer.DATABASE: database,
        "consent/consent.json": handoff._json_bytes(CONSENT),
        "consent/mapping.json": handoff._json_bytes(MAPPING),
    }
    if tunnel:
        result[transfer.TUNNEL_TOKEN] = TUNNEL.encode()
    if pointer:
        result[transfer.POINTER] = POINTER
    return result


def bundle(database, **changes):
    arguments = {"transfer_id": TRANSFER, "release_commitment": COMMITMENT, "settings": settings(),
                 "tunnel_enabled": True, "blobs": blobs(database), "artifacts": ()}
    arguments.update(changes)
    return transfer.build_bundle(**arguments)


def make_state(root):
    """A synthetic standalone host state, shaped like Hosting.setup creates it."""
    state = Path(root).resolve() / "OldHost"
    for relative in ("secrets", "relay", "downloads", "logs", "consent", "profile/WoWVoiceCaptions"):
        (state / relative).mkdir(parents=True)
    for name, value in (("discord-bot-token", BOT_TOKEN), ("discord-application-id", APPLICATION),
                        ("publisher-token", PUBLISHER), ("pepper", PEPPER), ("cloudflare-tunnel-token", TUNNEL)):
        (state / "secrets" / name).write_text(value + "\n", encoding="ascii")
    relay_env.save({
        **settings(), "VEMALS_EARS_BIND": "127.0.0.1",
        "VEMALS_EARS_DOWNLOAD_FILE": (state / "downloads/Vemals-Ears-Setup.exe").as_posix(),
        "VEMALS_EARS_DATABASE": (state / "relay/guild-hub.sqlite3").as_posix(),
        "VEMALS_EARS_PEPPER_FILE": (state / "secrets/pepper").as_posix(),
        "VEMALS_EARS_PUBLISHER_TOKEN_FILE": (state / "secrets/publisher-token").as_posix(),
        "VEMALS_EARS_DISCORD_APPLICATION_ID_FILE": (state / "secrets/discord-application-id").as_posix(),
        "VEMALS_EARS_DISCORD_BOT_TOKEN_FILE": (state / "secrets/discord-bot-token").as_posix(),
    }, state / "relay.env")
    (state / "host-settings.json").write_text(json.dumps(
        {"schema": 1, "tunnel_enabled": True, "client_installer": "C:/XENO/Setup.exe"}), encoding="ascii")
    (state / "consent/consent.json").write_text(json.dumps(CONSENT, indent=2), encoding="ascii")
    (state / "consent/mapping.json").write_text(json.dumps(MAPPING, indent=2), encoding="ascii")
    (state / "consent/sessions.json").write_text('{"XENO":"never moved"}', encoding="ascii")
    (state / "logs/worker.log").write_text("XENO log never moved", encoding="ascii")
    (state / "downloads" / transfer.POINTER_FILENAME).write_bytes(POINTER)
    device = make_database(state / "relay/guild-hub.sqlite3")
    return state, device


class BundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as folder:
            cls.database = database_bytes(folder)

    def test_container_round_trips_exact_blobs_in_a_detached_copy(self):
        original = bundle(self.database)
        packed = transfer.pack_bundle(original)
        self.assertTrue(packed.startswith(transfer.CONTAINER_MAGIC))
        # Binary and length-prefixed: the database travels raw, not as base64 in JSON.
        self.assertIn(self.database, packed)
        opened = transfer.unpack_bundle(packed)
        self.assertEqual(original.manifest, opened.manifest)
        self.assertEqual(original.blobs, opened.blobs)
        self.assertEqual([name for name in transfer.BLOBS], [entry["name"] for entry in opened.manifest["blobs"]])
        self.assertEqual(COMMITMENT.hex(), opened.manifest["release_commitment"])
        opened.manifest["settings"].clear()
        self.assertEqual(settings(), original.manifest["settings"])
        self.assertNotIn(BOT_TOKEN, repr(original))
        without = bundle(self.database, tunnel_enabled=False, blobs=blobs(self.database, tunnel=False, pointer=False))
        self.assertEqual(without.blobs, transfer.unpack_bundle(transfer.pack_bundle(without)).blobs)

    def test_session_resume_audio_transcripts_and_unknown_names_are_rejected(self):
        for field in ("sessions", "audio", "transcripts", "replay", "logs", "path", "files", "release_secret"):
            with self.subTest(field=field):
                value = bundle(self.database)
                value.manifest[field] = "XENO_never_moved"
                with self.assertRaisesRegex(transfer.TransferError, "invalid or unsupported"):
                    transfer.validate_bundle(value)
        for name in ("consent/sessions.json", "logs/worker.log", "audio/capture.wav", "transcripts/log.json",
                     "worker.state", "../secrets/pepper", "relay.env"):
            with self.subTest(name=name):
                extra = blobs(self.database)
                extra[name] = b"XENO_never_moved"
                with self.assertRaises(transfer.TransferError):
                    bundle(self.database, blobs=extra)
                value = bundle(self.database)
                value.blobs[name] = b"XENO_never_moved"
                value.manifest["blobs"].append(
                    {"name": name, "size": 16, "sha256": hashlib.sha256(b"XENO_never_moved").hexdigest()})
                with self.assertRaises(transfer.TransferError):
                    transfer.validate_bundle(value)
        for key in ("VEMALS_EARS_DATABASE", "VEMALS_EARS_PEPPER_FILE", "VEMALS_EARS_BIND", "VEMALS_EARS_PEPPER",
                    "VEMALS_EARS_HOST_MODE", "VEMALS_EARS_TRANSFER_KEY_FILE"):
            with self.subTest(key=key):
                with self.assertRaises(transfer.TransferError):
                    bundle(self.database, settings={**settings(), key: "C:/XENO"})

    def test_required_blobs_sizes_hashes_and_order_are_enforced(self):
        for name, (required, _) in transfer.BLOBS.items():
            if required:
                with self.subTest(missing=name):
                    partial = blobs(self.database)
                    del partial[name]
                    with self.assertRaises(transfer.TransferError):
                        bundle(self.database, blobs=partial)
        too_large = b"SQLite format 3\x00" + bytes(transfer.MAX_DATABASE_BYTES)
        with self.assertRaises(transfer.TransferError):
            bundle(self.database, blobs={**blobs(self.database), transfer.DATABASE: too_large})
        with self.assertRaises(transfer.TransferError):
            bundle(self.database, blobs={**blobs(self.database), transfer.DATABASE: b"not a database at all"})
        with self.assertRaises(transfer.TransferError):
            bundle(self.database, blobs={**blobs(self.database), transfer.POINTER: bytes(transfer.MAX_POINTER_BYTES + 1)})
        with self.assertRaises(transfer.TransferError):
            bundle(self.database, blobs=blobs(self.database, tunnel=False))
        for change in (lambda entry: entry.update(size=entry["size"] + 1),
                       lambda entry: entry.update(sha256="0" * 64),
                       lambda entry: entry.update(sha256="\u00e9" * 64),
                       lambda entry: entry.update(extra=1)):
            value = bundle(self.database)
            change(value.manifest["blobs"][0])
            with self.assertRaises(transfer.TransferError):
                transfer.validate_bundle(value)
        value = bundle(self.database)
        value.manifest["blobs"].reverse()
        with self.assertRaises(transfer.TransferError):
            transfer.validate_bundle(value)
        value = bundle(self.database)
        value.manifest["blobs"].append(dict(value.manifest["blobs"][0]))
        with self.assertRaises(transfer.TransferError):
            transfer.validate_bundle(value)

    def test_secret_and_ledger_validators_match_the_worker_handoff(self):
        cases = {
            "secrets/discord-bot-token": [b"XENO token with space", b"XENO\n", "\u00e9".encode("utf-8")],
            "secrets/discord-application-id": [b"12345", b"abc45678901234567"],
            "secrets/publisher-token": [b"short", b"XENO+invalid/publisher=token+0123456789abcdef"],
            "secrets/pepper": [b"XENO_too_short"],
            transfer.TUNNEL_TOKEN: [b"cloudflared service install XENO_0123456789abcdef0123456789"],
            transfer.POINTER: [b"[1]", b'{"a":1,"a":2}', b"not json"],
        }
        for name, values in cases.items():
            for value in values:
                with self.subTest(name=name, value=value[:16]):
                    with self.assertRaises(transfer.TransferError):
                        bundle(self.database, blobs={**blobs(self.database), name: value})
        other_guild = {"version": 1, "updatedAt": STAMP, "entries": {"999999999999999999:1": "granted"}}
        untrusted = {"version": 1, "updatedAt": STAMP, "entries": {GUILD + ":1": "maybe"}}
        with_sessions = {**CONSENT, "sessions": {}}
        for ledger in (other_guild, untrusted, with_sessions):
            with self.assertRaises(transfer.TransferError):
                bundle(self.database, blobs={**blobs(self.database), "consent/consent.json": handoff._json_bytes(ledger)})
        # Only the canonical encoding travels.
        with self.assertRaises(transfer.TransferError):
            bundle(self.database, blobs={**blobs(self.database),
                                         "consent/consent.json": json.dumps(CONSENT, indent=2).encode()})
        declined = transfer.unpack_bundle(transfer.pack_bundle(bundle(self.database)))
        self.assertEqual("declined", json.loads(declined.blobs["consent/consent.json"])["entries"][GUILD + ":2"])

    def test_settings_identity_and_artifacts_are_strict(self):
        for key, value in (("VEMALS_EARS_GUILD_ID", "123"), ("VEMALS_EARS_GUILD_NAME", " "),
                           ("VEMALS_EARS_PORT", "0"), ("VEMALS_EARS_PORT", "65536"), ("VEMALS_EARS_PORT", "8770 "),
                           ("VEMALS_EARS_PUBLIC_SERVER_URL", "ws://ears.example.org"),
                           ("VEMALS_EARS_PUBLIC_HTTP_URL", "https://other.example.org"),
                           ("VEMALS_EARS_CLIENT_DOWNLOAD_URL", "http://ears.example.org/download"),
                           ("VEMALS_EARS_CONNECTION_LIMIT", "many"), ("VEMALS_EARS_CLIENT_VERSION", "1.2.3\nX=1"),
                           ("VEMALS_EARS_PORT", 8770)):
            with self.subTest(key=key, value=value):
                with self.assertRaises(transfer.TransferError):
                    bundle(self.database, settings={**settings(), key: value})
        incomplete = settings()
        del incomplete["VEMALS_EARS_GUILD_ID"]
        with self.assertRaises(transfer.TransferError):
            bundle(self.database, settings=incomplete)
        for change in ({"transfer_id": "XENO"}, {"transfer_id": TRANSFER.upper()}, {"release_commitment": b"short"},
                       {"tunnel_enabled": 1}):
            with self.assertRaises(transfer.TransferError):
                bundle(self.database, **change)
        good = {"name": "Vemals-Ears-Setup.exe", "size": 5, "sha256": "a" * 64}
        self.assertEqual([good], bundle(self.database, artifacts=[good]).manifest["artifacts"])
        for change in ({"name": "../Setup.exe"}, {"name": "sub/Setup.exe"}, {"name": transfer.POINTER_FILENAME},
                       {"name": "C:Setup.exe"}, {"name": "Setup.exe."}, {"size": 0}, {"size": "5"},
                       {"sha256": "A" * 64}, {"path": "C:/XENO"}):
            with self.subTest(change=change):
                with self.assertRaises(transfer.TransferError):
                    bundle(self.database, artifacts=[{**good, **change}])
        with self.assertRaises(transfer.TransferError):
            bundle(self.database, artifacts=[good, {**good, "name": "VEMALS-EARS-SETUP.EXE"}])
        with self.assertRaises(transfer.TransferError):
            bundle(self.database, artifacts=[{**good, "name": "a%d.zip" % index} for index in range(9)])

    def test_malformed_containers_are_rejected_without_salvage(self):
        packed = transfer.pack_bundle(bundle(self.database))
        magic = len(transfer.CONTAINER_MAGIC)
        (length,) = struct.unpack_from(">I", packed, magic)
        manifest = packed[magic + 4:magic + 4 + length]
        rest = packed[magic + 4 + length:]

        def rebuilt(text):
            return transfer.CONTAINER_MAGIC + struct.pack(">I", len(text)) + text + rest

        self.assertEqual(packed, rebuilt(manifest))
        duplicate = manifest[:-1] + b',"schema":1}'
        huge = struct.pack(">Q", 2 ** 63)
        for name, data in (
                ("trailing", packed + b"\x00"), ("truncated", packed[:-1]), ("magic", b"X" + packed[1:]),
                ("empty", b""), ("text", packed.decode("latin-1")), ("duplicate key", rebuilt(duplicate)),
                ("not json", rebuilt(b"{" * 64)), ("deep", rebuilt(b"[" * 100_000)),
                ("manifest length", packed[:magic] + struct.pack(">I", 2 ** 31) + packed[magic + 4:]),
                ("blob length", packed[:magic + 4 + length] + huge + rest[8:]),
                ("non ascii", rebuilt(manifest.replace(b"XENO Test Guild", b"XENO Test Guil\xc3"))),
        ):
            with self.subTest(case=name):
                with self.assertRaisesRegex(transfer.TransferError, "invalid or unsupported"):
                    transfer.unpack_bundle(data)


class FrameTests(unittest.TestCase):
    DATA = bytes(range(256)) * 200 + b"XENO tail"

    def assemble(self, frames, key=KEYS.control_mac, transfer_id=TRANSFER):
        assembler = transfer.FrameAssembler(transfer_id, key)
        results = [assembler.feed(frame) for frame in frames]
        return assembler, results

    def test_frames_fit_the_relay_limit_and_reassemble_exactly(self):
        frames = transfer.split_frames(self.DATA, TRANSFER, KEYS.control_mac)
        self.assertGreater(len(frames), 3)
        self.assertTrue(all(len(frame) <= 16384 for frame in frames))
        self.assertEqual(16384, len(frames[0]))
        # Binary only: a text frame on the transfer sockets is relay control.
        self.assertTrue(all(type(frame) is bytes for frame in frames))
        with self.assertRaises(TypeError):
            transfer.split_frames(self.DATA, TRANSFER, KEYS.control_mac, text=True)
        assembler = transfer.FrameAssembler(TRANSFER, KEYS.control_mac)
        for frame in frames[:-1]:
            self.assertIsNone(assembler.feed(frame))
            self.assertFalse(assembler.complete)
        self.assertEqual(self.DATA, assembler.feed(frames[-1]))
        self.assertTrue(assembler.complete)
        with self.assertRaises(transfer.TransferError):
            assembler.feed(frames[-1])
        self.assertEqual(transfer.FRAME_LIMIT, 16384)
        one = transfer.split_frames(b"X", TRANSFER, KEYS.control_mac)
        self.assertEqual(2, len(one))
        self.assertEqual(b"X", self.assemble(one)[1][-1])

    def test_final_frame_carries_length_and_sha256_of_the_whole_bundle(self):
        last = transfer.split_frames(self.DATA, TRANSFER, KEYS.control_mac)[-1]
        body = last[transfer.FRAME_HEADER.size:-32]
        self.assertEqual((len(self.DATA), hashlib.sha256(self.DATA).digest()), transfer.FRAME_END_BODY.unpack(body))

    def test_tampered_replayed_reordered_foreign_and_unfinished_frames_fail(self):
        frames = transfer.split_frames(self.DATA, TRANSFER, KEYS.control_mac)
        flipped = frames[1][:100] + bytes((frames[1][100] ^ 1,)) + frames[1][101:]
        cases = {
            "tampered": [frames[0], flipped],
            "replayed": [frames[0], frames[0]],
            "reordered": [frames[1], frames[0]],
            "skipped": [frames[0], frames[2]],
            "end first": [frames[-1]],
            "dropped chunk": frames[:-2] + frames[-1:],
            "truncated": [frames[0][:40]],
            "oversized": [frames[0] + b"\x00"],
            "not bytes": [None],
            "text": ["!" * 100],
            "base64 text of a valid frame": [base64.urlsafe_b64encode(frames[0][:3000]).decode("ascii")],
            "bytearray": [bytearray(frames[0])],
        }
        for name, sequence in cases.items():
            with self.subTest(case=name):
                assembler = transfer.FrameAssembler(TRANSFER, KEYS.control_mac)
                with self.assertRaisesRegex(transfer.TransferError, "invalid transfer message"):
                    for frame in sequence:
                        assembler.feed(frame)
                self.assertFalse(assembler.complete)
                # A failed assembler stays failed, even for a now-valid frame.
                with self.assertRaises(transfer.TransferError):
                    assembler.feed(frames[0])
        for key, transfer_id in ((b"\x34" * 32, TRANSFER), (KEYS.control_mac, "f" * 32)):
            with self.assertRaises(transfer.TransferError):
                self.assemble(frames, key=key, transfer_id=transfer_id)
        # An attacker who cannot make the MAC cannot forge "sending complete".
        forged = frames[-1][:-32] + hmac.digest(b"\x00" * 32, frames[-1][:-32], "sha256")
        assembler = transfer.FrameAssembler(TRANSFER, KEYS.control_mac)
        with self.assertRaises(transfer.TransferError):
            for frame in frames[:-1] + [forged]:
                assembler.feed(frame)
        self.assertFalse(assembler.complete)


@unittest.skipUnless(os.name == "nt", "Windows AES integration")
class WindowsCryptoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as folder:
            cls.database = database_bytes(folder)
        offer = transfer_pake.Session(transfer_pake.ROLE_OFFER, "XEN0-7E5T", TRANSFER, "https://ears.example.org")
        claim = transfer_pake.Session(transfer_pake.ROLE_CLAIM, "xen0 7e5t", TRANSFER, "https://ears.example.org")
        cls.offer_keys, cls.claim_keys = offer.finish(claim.share), claim.finish(offer.share)
        cls.sealed = transfer.encrypt_bundle(bundle(cls.database), cls.offer_keys)

    def test_pake_keys_seal_and_open_the_bundle_end_to_end_through_frames(self):
        frames = transfer.split_frames(self.sealed, TRANSFER, self.offer_keys.control_mac)
        assembler = transfer.FrameAssembler(TRANSFER, self.claim_keys.control_mac)
        received = [assembler.feed(frame) for frame in frames][-1]
        self.assertEqual(self.sealed, received)
        opened = transfer.decrypt_bundle(received, self.claim_keys, TRANSFER)
        self.assertEqual(bundle(self.database).manifest, opened.manifest)
        self.assertEqual(blobs(self.database), opened.blobs)
        for value in (BOT_TOKEN, PUBLISHER, PEPPER, TUNNEL, GUILD, "XENO Test Guild", "SQLite format 3"):
            self.assertNotIn(value.encode("ascii"), self.sealed)
        again = transfer.encrypt_bundle(bundle(self.database), self.offer_keys)
        self.assertNotEqual(transfer.HEADER.unpack_from(self.sealed)[2], transfer.HEADER.unpack_from(again)[2])
        self.assertTrue(self.sealed.startswith(transfer.MAGIC))

    def test_tampered_or_foreign_bundles_never_reach_decryption(self):
        sealed, size = self.sealed, transfer.HEADER.size

        def flip(index):
            return sealed[:index] + bytes((sealed[index] ^ 1,)) + sealed[index + 1:]

        shorter = sealed[:size - 8] + struct.pack(">Q", len(sealed) - size - 48) + sealed[size:-48] + sealed[-32:]
        cases = {
            "magic": flip(0), "transfer id": flip(12), "iv": flip(28), "length": flip(size - 1),
            "ciphertext": flip(size + 5), "mac": flip(len(sealed) - 1), "truncated": sealed[:-1],
            "appended": sealed + b"\x00", "shorter": shorter, "empty": b"", "text": "XENO",
        }
        wrong = SimpleNamespace(bundle_enc=self.claim_keys.bundle_enc, bundle_mac=b"\x00" * 32)
        with mock.patch.object(handoff, "transfer_aes") as aes:
            for name, data in cases.items():
                with self.subTest(case=name):
                    with self.assertRaisesRegex(transfer.TransferError, "could not be opened"):
                        transfer.decrypt_bundle(data, self.claim_keys, TRANSFER)
            with self.assertRaisesRegex(transfer.TransferError, "could not be opened"):
                transfer.decrypt_bundle(sealed, wrong, TRANSFER)
            with self.assertRaisesRegex(transfer.TransferError, "could not be opened"):
                transfer.decrypt_bundle(sealed, self.claim_keys, "f" * 32)
            aes.assert_not_called()
        # A wrong code gives other keys; the MAC fails before any AES call.
        other = transfer_pake.Session(transfer_pake.ROLE_CLAIM, "XEN0-7E5V", TRANSFER, "https://ears.example.org")
        mallory = other.finish(transfer_pake.Session(
            transfer_pake.ROLE_OFFER, "XEN0-7E5T", TRANSFER, "https://ears.example.org").share)
        with mock.patch.object(handoff, "transfer_aes") as aes:
            with self.assertRaises(transfer.TransferError):
                transfer.decrypt_bundle(sealed, mallory, TRANSFER)
            aes.assert_not_called()

    def test_authenticated_plaintext_is_still_validated(self):
        # A correctly MACed container holding excluded data is refused.
        plain = transfer.pack_bundle(bundle(self.database))[:-1] + b"X"
        iv = b"\x07" * 16
        ciphertext = handoff.transfer_aes(plain + b"extra", self.offer_keys.bundle_enc, iv, decrypt=False)
        header = transfer.HEADER.pack(transfer.MAGIC, bytes.fromhex(TRANSFER), iv, len(ciphertext)) + ciphertext
        forged = header + hmac.digest(self.offer_keys.bundle_mac, header, "sha256")
        with self.assertRaisesRegex(transfer.TransferError, "could not be opened"):
            transfer.decrypt_bundle(forged, self.claim_keys, TRANSFER)
        # The manifest's own transfer id must match the header's.
        other = transfer.encrypt_bundle(bundle(self.database, transfer_id="f" * 32,
                                               release_commitment=COMMITMENT), self.offer_keys)
        header = other[:12] + bytes.fromhex(TRANSFER) + other[28:-32]
        with self.assertRaisesRegex(transfer.TransferError, "could not be opened"):
            transfer.decrypt_bundle(header + hmac.digest(self.offer_keys.bundle_mac, header, "sha256"),
                                    self.claim_keys, TRANSFER)

    def test_transfer_aes_matches_nist_vector_and_uses_its_own_operations(self):
        # NIST SP 800-38A F.2.5, first block.
        key = bytes.fromhex("603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4")
        iv = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        clear = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a")
        encrypted = handoff.transfer_aes(clear, key, iv, decrypt=False)
        self.assertEqual("f58c4c04d6e5f1ba779eabfb5f7bfbd6", encrypted[:16].hex())
        self.assertEqual(clear, handoff.transfer_aes(encrypted, key, iv, decrypt=True))
        # Same cipher as the worker handoff, reached through separate op codes.
        self.assertEqual(encrypted, handoff._aes(clear, key, iv, decrypt=False))
        for decrypt, operation, data in ((False, 3, b"XENO"), (True, 4, b"c" * 32)):
            with mock.patch.object(handoff.subprocess, "run",
                                   return_value=SimpleNamespace(returncode=0, stdout=b"c" * 16)) as run:
                handoff.transfer_aes(data, b"k" * 32, b"i" * 16, decrypt=decrypt)
            args, kwargs = run.call_args
            self.assertEqual(bytes((1, operation)) + b"k" * 32 + b"i" * 16 + struct.pack("<I", len(data)) + data,
                             kwargs["input"])
            self.assertNotIn("XENO", " ".join(args[0]))
            self.assertEqual(subprocess.CREATE_NO_WINDOW, kwargs["creationflags"])
            self.assertEqual(handoff.TRANSFER_AES_TIMEOUT, kwargs["timeout"])
            self.assertNotIn("env", kwargs)
        with mock.patch.object(handoff.subprocess, "run") as run:
            for arguments in ((bytes(handoff.MAX_TRANSFER_PAYLOAD_BYTES + 17), b"k" * 32, b"i" * 16, False),
                              (b"XENO", b"k" * 31, b"i" * 16, False), (b"XENO", b"k" * 32, b"i" * 15, False),
                              (b"c" * 17, b"k" * 32, b"i" * 16, True), (b"", b"k" * 32, b"i" * 16, True),
                              ("XENO", b"k" * 32, b"i" * 16, False)):
                with self.assertRaisesRegex(handoff.HandoffError, "host transfer data"):
                    handoff.transfer_aes(*arguments[:3], decrypt=arguments[3])
            run.assert_not_called()

    def test_larger_limit_applies_to_transfer_operations_only(self):
        self.assertEqual(24 * 1024 * 1024, handoff.MAX_TRANSFER_PAYLOAD_BYTES)
        self.assertEqual(2 * 1024 * 1024, handoff.MAX_PAYLOAD_BYTES)
        key, iv = b"k" * 32, b"i" * 16
        large = bytes(3 * 1024 * 1024)
        encrypted = handoff.transfer_aes(large, key, iv, decrypt=False)
        self.assertEqual(len(large) + 16, len(encrypted))
        self.assertEqual(large, handoff.transfer_aes(encrypted, key, iv, decrypt=True))
        with mock.patch.object(handoff.subprocess, "run") as run:
            with self.assertRaisesRegex(handoff.HandoffError, "invalid or unsupported"):
                handoff._aes(large, key, iv, decrypt=False)
            run.assert_not_called()
        command = [str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"),
                   "-NoProfile", "-NonInteractive", "-File", str(handoff.HELPER)]

        def helper(operation, length, data):
            request = bytes((1, operation)) + key + iv + struct.pack("<I", length) + data
            return subprocess.run(command, input=request, capture_output=True, timeout=60,
                                  creationflags=subprocess.CREATE_NO_WINDOW)

        # The worker-handoff operations keep their 2 MiB + 16 limit exactly.
        for operation, length, data, accepted in (
                (1, 2097168, bytes(2097168), True), (1, 2097169, bytes(2097169), False),
                (2, 2097184, bytes(2097184), False), (1, len(large), large, False),
                (3, 2097169, bytes(2097169), True), (3, 25165841, b"", False), (4, 25165856, b"", False),
                (4, 17, bytes(17), False), (0, 4, b"XENO", False), (5, 4, b"XENO", False)):
            with self.subTest(operation=operation, length=length):
                result = helper(operation, length, data)
                self.assertEqual(b"", result.stderr)
                if accepted:
                    self.assertEqual(0, result.returncode)
                    self.assertEqual(length // 16 * 16 + 16, len(result.stdout))
                else:
                    self.assertEqual(1, result.returncode)
                    self.assertEqual(b"", result.stdout)

    def test_worker_handoff_format_is_unchanged_and_not_interchangeable(self):
        self.assertEqual(b"VE-HANDOFF\x00\x01", handoff.MAGIC)
        self.assertEqual(600_000, handoff.ITERATIONS)
        self.assertEqual(">12sI32s16sI", handoff.HEADER.format)
        self.assertEqual(handoff.HEADER.size + 2 * 1_048_576 + 48, handoff.MAX_BUNDLE_BYTES)
        with mock.patch.object(handoff.subprocess, "run", wraps=subprocess.run) as run:
            vehost = handoff.encrypt_bundle(worker_payload(), PASSWORD)
            self.assertEqual(worker_payload(), handoff.decrypt_bundle(vehost, PASSWORD))
        # A .vehost file still uses operations 1 and 2 with the 30 second limit.
        self.assertEqual([1, 2], [call.kwargs["input"][1] for call in run.call_args_list])
        self.assertEqual([30, 30], [call.kwargs["timeout"] for call in run.call_args_list])
        magic, iterations, salt, iv, length = handoff.HEADER.unpack_from(vehost)
        keys = hashlib.pbkdf2_hmac("sha256", PASSWORD.encode(), salt, iterations, dklen=64)
        self.assertEqual(hmac.digest(keys[32:], vehost[:-32], "sha256"), vehost[-32:])
        self.assertEqual(len(vehost), handoff.HEADER.size + length + 32)
        with self.assertRaises(transfer.TransferError):
            transfer.decrypt_bundle(vehost, self.claim_keys, TRANSFER)
        with self.assertRaises(handoff.HandoffError):
            handoff.decrypt_bundle(self.sealed, PASSWORD)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name).resolve()
        self.database = self.root / "relay/guild-hub.sqlite3"
        self.device = make_database(self.database)
        self.work = self.root / "work"

    def restore(self, data, name="restored.sqlite3"):
        target = self.root / name
        target.write_bytes(data)
        return target

    def test_snapshot_is_consistent_while_a_writer_holds_a_transaction(self):
        before = self.database.read_bytes()
        writer = sqlite3.connect(self.database, isolation_level=None)
        self.addCleanup(writer.close)
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO devices (device_id, token_hash, device_name, client_version, created_at) "
                       "VALUES ('ghdev_XENO_uncommitted', x'00', 'XENO', '1', 0)")
        data = transfer.snapshot_database(self.database, self.work)
        writer.execute("ROLLBACK")
        restored = self.restore(data)
        with closing(sqlite3.connect(restored)) as connection:
            self.assertEqual([("ok",)], connection.execute("PRAGMA integrity_check").fetchall())
            self.assertEqual([(self.device.device_id,)], connection.execute("SELECT device_id FROM devices").fetchall())
        # The pairing survives: the old device token authenticates against the
        # copy with the moved pepper, and not with any other pepper.
        self.assertTrue(CredentialStore(restored, PEPPER).authenticate_device(
            self.device.device_id, self.device.device_token))
        self.assertFalse(CredentialStore(restored, "XENO_another_pepper_0123456789_abcdefghijkl").authenticate_device(
            self.device.device_id, self.device.device_token))
        # The source was only read.
        self.assertEqual(before, self.database.read_bytes())
        self.assertEqual([], list(self.work.iterdir()))

    def test_snapshot_includes_rows_committed_just_before_it(self):
        store = CredentialStore(self.database, PEPPER)
        late = store.claim_enrollment(store.create_enrollment().code, "XENO late device", "1.2.3")
        restored = self.restore(transfer.snapshot_database(self.database, self.work))
        self.assertTrue(CredentialStore(restored, PEPPER).authenticate_device(late.device_id, late.device_token))

    def test_plaintext_copy_is_overwritten_before_it_is_removed(self):
        erased = []
        unlink = Path.unlink

        def checked(path, *args, **kwargs):
            if path.name == "relay.sqlite3":
                content = path.read_bytes()
                erased.append((len(content), content.count(0) == len(content)))
            return unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", checked):
            data = transfer.snapshot_database(self.database, self.work)
        self.assertEqual([(len(data), True)], erased)
        self.assertEqual([], list(self.work.iterdir()))

    def test_damaged_incomplete_missing_or_oversized_databases_abort_and_leave_no_copy(self):
        garbage = self.root / "garbage.sqlite3"
        garbage.write_bytes(b"SQLite format 3\x00" + b"XENO" * 4096)
        wrong = self.root / "wrong.sqlite3"
        with closing(sqlite3.connect(wrong)) as connection:
            connection.execute("CREATE TABLE notes (value TEXT)")
            connection.commit()
        for path in (garbage, wrong, self.root / "missing.sqlite3", self.root):
            with self.subTest(path=path.name):
                with self.assertRaisesRegex(transfer.TransferError, "Nothing was moved"):
                    transfer.snapshot_database(path, self.work)
        with mock.patch.object(transfer, "MAX_DATABASE_BYTES", 1024):
            with self.assertRaisesRegex(transfer.TransferError, "too large"):
                transfer.snapshot_database(self.database, self.work)
        self.assertEqual([], list(self.work.iterdir()) if self.work.exists() else [])
        self.assertFalse((self.root / "missing.sqlite3").exists())

    def test_integrity_failure_is_reported_and_the_copy_erased(self):
        with mock.patch.object(transfer, "_check_database",
                               side_effect=transfer.TransferError("XENO integrity failure")):
            with self.assertRaisesRegex(transfer.TransferError, "XENO integrity failure"):
                transfer.snapshot_database(self.database, self.work)
        self.assertEqual([], list(self.work.iterdir()))

    def test_erase_is_retried_once_and_a_leftover_copy_is_named_then_swept(self):
        erase_tree = transfer._erase_tree
        for failures, leftover in ((1, False), (2, True)):
            with self.subTest(failures=failures):
                calls = []

                def held(folder):
                    calls.append(folder)
                    if len(calls) <= failures:
                        raise PermissionError("XENO sharing violation")
                    erase_tree(folder)

                with mock.patch.object(transfer, "_erase_tree", held), mock.patch.object(transfer.time, "sleep"):
                    if not leftover:
                        self.assertTrue(transfer.snapshot_database(self.database, self.work).startswith(b"SQLite"))
                    else:
                        with self.assertRaises(transfer.TransferError) as raised:
                            transfer.snapshot_database(self.database, self.work)
                self.assertEqual(2, len(calls))
                if leftover:
                    # Not the generic copy error: it says what is left and where.
                    self.assertIn("could not be erased", str(raised.exception))
                    self.assertNotIn("could not be copied", str(raised.exception))
                    self.assertIn(str(calls[0]), str(raised.exception))
                    self.assertTrue((calls[0] / "relay.sqlite3").is_file())
        (self.work / "unrelated").mkdir()
        self.assertEqual(1, transfer.erase_leftover_snapshots(self.work))
        self.assertEqual(["unrelated"], [path.name for path in self.work.iterdir()])
        self.assertEqual(0, transfer.erase_leftover_snapshots(self.work))
        self.assertEqual(0, transfer.erase_leftover_snapshots(self.root / "never made"))
        with mock.patch.object(transfer, "_erase_tree", side_effect=PermissionError("XENO")), \
                mock.patch.object(transfer.time, "sleep"):
            leftover = self.work / (transfer.SNAPSHOT_PREFIX + "XENO")
            leftover.mkdir()
            with self.assertRaisesRegex(transfer.TransferError, "could not be erased"):
                transfer.erase_leftover_snapshots(self.work)
        self.assertEqual(1, transfer.erase_leftover_snapshots(self.work))

    def test_a_failed_copy_still_reports_a_leftover_it_could_not_erase(self):
        with mock.patch.object(transfer, "_check_database", side_effect=sqlite3.DatabaseError("XENO")), \
                mock.patch.object(transfer, "_erase_tree", side_effect=PermissionError("XENO")), \
                mock.patch.object(transfer.time, "sleep"):
            with self.assertRaisesRegex(transfer.TransferError, "could not be erased"):
                transfer.snapshot_database(self.database, self.work)
        self.assertEqual(1, transfer.erase_leftover_snapshots(self.work))
        self.assertEqual([], list(self.work.iterdir()))


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name).resolve()
        self.state, self.device = make_state(self.root)
        self.work = self.root / "work"

    def collect(self, **changes):
        arguments = {"transfer_id": TRANSFER, "release_commitment": COMMITMENT, "work_directory": self.work}
        arguments.update(changes)
        return transfer.collect_state(self.state, **arguments)

    def snapshot_of_state(self):
        return {path.relative_to(self.state).as_posix(): path.read_bytes()
                for path in self.state.rglob("*") if path.is_file()}

    def test_everything_a_host_needs_is_collected_and_nothing_else(self):
        before = self.snapshot_of_state()
        collected = self.collect()
        self.assertEqual(before, self.snapshot_of_state())
        self.assertEqual(set(transfer.BLOBS), set(collected.blobs))
        self.assertEqual(BOT_TOKEN.encode(), collected.blobs["secrets/discord-bot-token"])
        self.assertEqual(APPLICATION.encode(), collected.blobs["secrets/discord-application-id"])
        self.assertEqual(PUBLISHER.encode(), collected.blobs["secrets/publisher-token"])
        self.assertEqual(PEPPER.encode(), collected.blobs["secrets/pepper"])
        self.assertEqual(TUNNEL.encode(), collected.blobs[transfer.TUNNEL_TOKEN])
        self.assertEqual(POINTER, collected.blobs[transfer.POINTER])
        self.assertEqual(CONSENT, json.loads(collected.blobs["consent/consent.json"]))
        self.assertEqual(MAPPING, json.loads(collected.blobs["consent/mapping.json"]))
        self.assertEqual(settings(), collected.manifest["settings"])
        self.assertTrue(collected.manifest["tunnel_enabled"])
        self.assertEqual(TRANSFER, collected.manifest["transfer_id"])
        self.assertEqual(COMMITMENT.hex(), collected.manifest["release_commitment"])
        restored = self.root / "restored.sqlite3"
        restored.write_bytes(collected.blobs[transfer.DATABASE])
        self.assertTrue(CredentialStore(restored, collected.blobs["secrets/pepper"].decode()).authenticate_device(
            self.device.device_id, self.device.device_token))
        # No machine path, session resume, log or installer path leaves the PC.
        packed = transfer.pack_bundle(collected)
        for value in (self.state.as_posix(), str(self.state), "never moved", "C:/XENO"):
            self.assertNotIn(value.encode("ascii"), packed)
        self.assertEqual([], list(self.work.iterdir()))

    def test_optional_items_are_omitted_when_the_host_has_none(self):
        (self.state / "secrets/cloudflare-tunnel-token").unlink()
        (self.state / "downloads" / transfer.POINTER_FILENAME).unlink()
        (self.state / "host-settings.json").write_text('{"schema":1,"tunnel_enabled":false}', encoding="ascii")
        (self.state / "consent/consent.json").unlink()
        (self.state / "consent/mapping.json").unlink()
        collected = self.collect(artifacts=[{"name": "Vemals-Ears-Setup.exe", "size": 4, "sha256": "b" * 64}])
        self.assertNotIn(transfer.TUNNEL_TOKEN, collected.blobs)
        self.assertNotIn(transfer.POINTER, collected.blobs)
        self.assertFalse(collected.manifest["tunnel_enabled"])
        self.assertEqual({}, json.loads(collected.blobs["consent/consent.json"])["entries"])
        self.assertEqual("Vemals-Ears-Setup.exe", collected.manifest["artifacts"][0]["name"])

    def test_unsynced_consent_damaged_ledgers_and_foreign_paths_abort(self):
        marker = self.state / "consent/consent.json.dirty"
        marker.write_text('{"version":1,"synced":false}', encoding="ascii")
        with self.assertRaisesRegex(transfer.TransferError, "unfinished write"):
            self.collect()
        marker.write_text('{"version":1,"synced":true}', encoding="ascii")
        self.collect()
        marker.unlink()
        ledger = self.state / "consent/consent.json"
        good = ledger.read_bytes()
        for damaged in (b"{", json.dumps({**CONSENT, "entries": {"999999999999999999:1": "granted"}}).encode(),
                        json.dumps({**CONSENT, "entries": {GUILD + ":1": "maybe"}}).encode()):
            ledger.write_bytes(damaged)
            with self.assertRaises(transfer.TransferError):
                self.collect()
        ledger.write_bytes(good)
        values = relay_env.load(self.state / "relay.env")
        explicit = {key: value for key, value in values.items()
                    if key in (self.state / "relay.env").read_text(encoding="ascii")}
        outside = self.root / "outside-pepper"
        outside.write_text(PEPPER + "\n", encoding="ascii")
        relay_env.save({**explicit, "VEMALS_EARS_PEPPER_FILE": outside.as_posix()}, self.state / "relay.env")
        with self.assertRaisesRegex(transfer.TransferError, "outside its expected private location"):
            self.collect()
        relay_env.save({**explicit, "VEMALS_EARS_DATABASE": (self.root / "other.sqlite3").as_posix()},
                       self.state / "relay.env")
        with self.assertRaisesRegex(transfer.TransferError, "outside its expected private location"):
            self.collect()
        relay_env.save(explicit, self.state / "relay.env")
        self.collect()
        self.assertEqual([], list(self.work.iterdir()))

    def test_only_a_configured_standalone_host_can_be_moved(self):
        text = (self.state / "relay.env").read_text(encoding="ascii")
        remote = "\n".join(line for line in text.splitlines() if not line.startswith((
            "VEMALS_EARS_BIND", "VEMALS_EARS_PORT", "VEMALS_EARS_DATABASE", "VEMALS_EARS_PEPPER_FILE",
            "VEMALS_EARS_DOWNLOAD_FILE"))) + "\nVEMALS_EARS_HOST_MODE=remote-worker\n"
        (self.state / "relay.env").write_text(remote, encoding="ascii")
        self.assertEqual(service.REMOTE_WORKER, service.load_configuration(self.state)["VEMALS_EARS_HOST_MODE"])
        with self.assertRaisesRegex(transfer.TransferError, "Only a standalone host"):
            self.collect()
        (self.state / "relay.env").unlink()
        with self.assertRaisesRegex(transfer.TransferError, "configuration could not be read"):
            self.collect()
        with self.assertRaises(transfer.TransferError):
            transfer.collect_state(Path("relative/state"), transfer_id=TRANSFER, release_commitment=COMMITMENT,
                                   work_directory=self.work)


@unittest.skipUnless(os.name == "nt", "Windows ACL and rename integration")
class WindowsImportTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name).resolve()
        source, self.device = make_state(self.root)
        self.installer = b"XENO synthetic member installer"
        self.artifact = {"name": "Vemals-Ears-Setup.exe", "size": len(self.installer),
                         "sha256": hashlib.sha256(self.installer).hexdigest()}
        self.bundle = transfer.collect_state(
            source, transfer_id=TRANSFER, release_commitment=COMMITMENT, work_directory=self.root / "work",
            artifacts=[self.artifact])
        self.state = self.root / "New PC" / "VemalsEarsHost"
        self.staging = self.state.with_name("VemalsEarsHost.transfer-staging")

    def stage(self, *, mirror=True):
        staging = transfer.create_staging(self.state)
        if mirror:
            (staging / "downloads").mkdir()
            (staging / "downloads" / self.artifact["name"]).write_bytes(self.installer)
        return staging

    def assert_private_acl(self, path):
        acl = inspect_acl(path)
        self.assertTrue(acl["protected"])
        expected = {acl["currentSid"], "S-1-5-18", "S-1-5-32-544"}
        self.assertTrue(expected.issubset(set(acl["sids"])))
        self.assertTrue(set(acl["sids"]).issubset(expected | {"S-1-3-4"}))

    def test_staging_is_a_protected_sibling_created_before_any_secret(self):
        self.assertEqual(self.staging, transfer.staging_path(self.state))
        staging = self.stage(mirror=False)
        self.assertEqual(self.staging, staging)
        self.assertEqual([], list(staging.iterdir()))
        self.assert_private_acl(staging)
        self.assertFalse(self.state.exists())
        with self.assertRaisesRegex(transfer.TransferError, "unfinished transfer is staged"):
            transfer.create_staging(self.state)
        events = []
        with mock.patch.object(transfer.setup, "protect_directory", side_effect=lambda path: events.append("acl")), \
                mock.patch.object(transfer.handoff, "_write_private", side_effect=lambda *a: events.append("write")):
            transfer.discard_staging(self.state)
            transfer.create_staging(self.state)
        self.assertEqual(["acl"], events)

    def test_reused_staging_and_its_descendants_get_the_private_acl_again(self):
        # Astra finding 1: a precreated directory, with entries holding their
        # own explicit grants, is restricted before any secret is written.
        self.staging.mkdir(parents=True)
        (self.staging / "profile").mkdir()
        (self.staging / "profile" / "runtime.txt").write_text("XENO runtime", encoding="ascii")
        (self.staging / "top.txt").write_text("XENO", encoding="ascii")
        subprocess.run(["icacls.exe", str(self.staging / "profile" / "runtime.txt"), "/grant", "*S-1-1-0:(F)"],
                       check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertIn("S-1-1-0", inspect_acl(self.staging / "profile" / "runtime.txt")["sids"])
        self.assertEqual(self.staging, transfer.protect_staging(self.state))
        self.assert_private_acl(self.staging)
        expected = {inspect_acl(self.staging)["currentSid"], "S-1-5-18", "S-1-5-32-544", "S-1-3-4"}
        for entry in ("profile", "profile/runtime.txt", "top.txt"):
            acl = inspect_acl(self.staging / entry)
            self.assertFalse(acl["protected"], entry)
            self.assertTrue(set(acl["sids"]).issubset(expected), entry)
        self.state.mkdir()
        with self.assertRaisesRegex(transfer.TransferError, "already has a host"):
            transfer.protect_staging(self.state)

    def test_import_regenerates_paths_marks_pending_and_publishes_by_one_rename(self):
        staging = self.stage()
        (staging / "profile/WoWVoiceCaptions/runtime").mkdir(parents=True)
        (staging / "profile/WoWVoiceCaptions/runtime/XENO-runtime.bin").write_bytes(b"prepared runtime")
        values = transfer.import_state(self.state, self.bundle)
        self.assertFalse(self.state.exists())
        self.assertEqual(transfer.ACTIVATION_PENDING, transfer.activation(staging))
        text = (staging / "relay.env").read_text(encoding="ascii")
        # Every path is the destination's, never the old PC's or the staging folder's.
        self.assertNotIn("OldHost", text)
        self.assertNotIn("transfer-staging", text)
        for key, relative in (("VEMALS_EARS_DATABASE", "relay/guild-hub.sqlite3"),
                              ("VEMALS_EARS_PEPPER_FILE", "secrets/pepper"),
                              ("VEMALS_EARS_PUBLISHER_TOKEN_FILE", "secrets/publisher-token"),
                              ("VEMALS_EARS_DISCORD_APPLICATION_ID_FILE", "secrets/discord-application-id"),
                              ("VEMALS_EARS_DISCORD_BOT_TOKEN_FILE", "secrets/discord-bot-token"),
                              ("VEMALS_EARS_DOWNLOAD_FILE", "downloads/Vemals-Ears-Setup.exe")):
            self.assertEqual((self.state / relative).as_posix(), values[key])
        self.assertEqual("127.0.0.1", values["VEMALS_EARS_BIND"])
        for key, value in settings().items():
            self.assertEqual(value, values[key])
        with mock.patch.object(handoff, "_rename_new", wraps=handoff._rename_new) as rename:
            self.assertEqual(self.state, transfer.publish_staging(self.state))
        rename.assert_called_once_with(self.staging, self.state)
        self.assertFalse(self.staging.exists())
        self.assert_private_acl(self.state)
        loaded = service.load_configuration(self.state)
        self.assertEqual(service.STANDALONE, loaded["VEMALS_EARS_HOST_MODE"])
        for key in ("VEMALS_EARS_DATABASE", "VEMALS_EARS_PEPPER_FILE", "VEMALS_EARS_DISCORD_BOT_TOKEN_FILE"):
            self.assertTrue(Path(loaded[key]).is_file())
        self.assertEqual(BOT_TOKEN + "\n", (self.state / "secrets/discord-bot-token").read_text(encoding="ascii"))
        self.assertEqual(PUBLISHER + "\n", (self.state / "secrets/publisher-token").read_text(encoding="ascii"))
        self.assertEqual(TUNNEL + "\n", (self.state / "secrets/cloudflare-tunnel-token").read_text(encoding="ascii"))
        self.assertEqual(CONSENT, json.loads((self.state / "consent/consent.json").read_text(encoding="ascii")))
        self.assertEqual(MAPPING, json.loads((self.state / "consent/mapping.json").read_text(encoding="ascii")))
        self.assertEqual(POINTER, (self.state / "downloads" / transfer.POINTER_FILENAME).read_bytes())
        self.assertEqual(self.installer, (self.state / "downloads/Vemals-Ears-Setup.exe").read_bytes())
        self.assertEqual(b"prepared runtime",
                         (self.state / "profile/WoWVoiceCaptions/runtime/XENO-runtime.bin").read_bytes())
        # Members keep their pairing: old token, moved pepper, moved database.
        pepper = Path(loaded["VEMALS_EARS_PEPPER_FILE"]).read_text(encoding="ascii").strip()
        self.assertTrue(CredentialStore(loaded["VEMALS_EARS_DATABASE"], pepper).authenticate_device(
            self.device.device_id, self.device.device_token))
        metadata = json.loads((self.state / "host-settings.json").read_text(encoding="ascii"))
        self.assertEqual({"schema": 1, "tunnel_enabled": True, "client_installer": "",
                          "activation": "pending", "transfer_id": TRANSFER, "tunnel_withheld": True}, metadata)
        # Nothing that is never moved appeared, and no temp or marker file.
        files = {path.relative_to(self.state).as_posix() for path in self.state.rglob("*") if path.is_file()}
        self.assertEqual({"relay.env", "host-settings.json", "relay/guild-hub.sqlite3",
                          "consent/consent.json", "consent/mapping.json",
                          "secrets/discord-bot-token", "secrets/discord-application-id",
                          "secrets/publisher-token", "secrets/pepper", "secrets/cloudflare-tunnel-token",
                          "downloads/Vemals-Ears-Setup.exe", "downloads/" + transfer.POINTER_FILENAME,
                          "profile/WoWVoiceCaptions/runtime/XENO-runtime.bin"}, files)
        transfer.mark_active(self.state)
        self.assertEqual(transfer.ACTIVATION_ACTIVE, transfer.activation(self.state))
        with self.assertRaisesRegex(transfer.TransferError, "not waiting"):
            transfer.mark_active(self.state)

    def test_existing_state_directory_is_refused_at_every_step(self):
        self.stage()
        transfer.import_state(self.state, self.bundle)
        for occupied in ("empty", "host"):
            with self.subTest(occupied=occupied):
                self.state.mkdir()
                if occupied == "host":
                    (self.state / "relay.env").write_text("XENO existing host\n", encoding="ascii")
                with self.assertRaisesRegex(transfer.TransferError, "never overwritten"):
                    transfer.publish_staging(self.state)
                with self.assertRaisesRegex(transfer.TransferError, "never overwritten"):
                    transfer.import_state(self.state, self.bundle)
                with self.assertRaisesRegex(transfer.TransferError, "already has a host"):
                    transfer.create_staging(self.state)
                self.assertTrue((self.staging / "relay.env").is_file())
                if occupied == "host":
                    self.assertEqual("XENO existing host\n", (self.state / "relay.env").read_text(encoding="ascii"))
                    (self.state / "relay.env").unlink()
                self.state.rmdir()
        # Created after the check: the rename itself still refuses to replace it.
        real = handoff._rename_new

        def race(source, target):
            target.mkdir()
            real(source, target)

        with mock.patch.object(handoff, "_rename_new", side_effect=race):
            with self.assertRaisesRegex(transfer.TransferError, "was not replaced"):
                transfer.publish_staging(self.state)
        self.assertEqual([], list(self.state.iterdir()))
        self.assertTrue((self.staging / "secrets/pepper").is_file())

    def test_artifacts_must_be_mirrored_and_match_before_anything_is_written(self):
        for case in ("missing", "wrong bytes", "wrong size"):
            with self.subTest(case=case):
                staging = self.stage(mirror=False)
                (staging / "downloads").mkdir()
                if case == "wrong bytes":
                    (staging / "downloads" / self.artifact["name"]).write_bytes(self.installer[:-1] + b"X")
                elif case == "wrong size":
                    (staging / "downloads" / self.artifact["name"]).write_bytes(self.installer + b"X")
                with self.assertRaises(transfer.TransferError):
                    transfer.import_state(self.state, self.bundle)
                self.assertFalse((staging / "secrets").exists())
                self.assertFalse((staging / "relay.env").exists())
                self.assertFalse((staging / "downloads" / transfer.POINTER_FILENAME).exists())
                transfer.discard_staging(self.state)

    def test_release_pointer_is_written_last(self):
        self.stage()
        written = []
        real = handoff._write_private

        def record(path, content):
            written.append(path.relative_to(self.staging).as_posix())
            real(path, content)

        with mock.patch.object(handoff, "_write_private", side_effect=record):
            transfer.import_state(self.state, self.bundle)
        self.assertEqual("downloads/" + transfer.POINTER_FILENAME, written[-1])
        self.assertEqual("host-settings.json", written[-2])
        self.assertEqual(len(self.bundle.blobs) + 1, len(written))

    def test_failed_import_erases_what_it_wrote_and_keeps_the_runtime(self):
        for failing in ("relay.env", "integrity", "write"):
            with self.subTest(failing=failing):
                staging = self.stage()
                (staging / "profile").mkdir()
                (staging / "profile/XENO-runtime.bin").write_bytes(b"prepared runtime")
                if failing == "relay.env":
                    patch = mock.patch.object(transfer.relay_env, "save", side_effect=OSError("XENO"))
                elif failing == "integrity":
                    patch = mock.patch.object(transfer, "_check_database",
                                              side_effect=transfer.TransferError("XENO integrity failure"))
                else:
                    real = handoff._write_private

                    def fail(path, content):
                        if path.name == "mapping.json":
                            raise OSError("XENO")
                        real(path, content)

                    patch = mock.patch.object(handoff, "_write_private", side_effect=fail)
                with patch, self.assertRaises(transfer.TransferError) as raised:
                    transfer.import_state(self.state, self.bundle)
                self.assertNotIn(str(self.root), str(raised.exception))
                remaining = {path.relative_to(staging).as_posix() for path in staging.rglob("*") if path.is_file()}
                self.assertEqual({"profile/XENO-runtime.bin", "downloads/Vemals-Ears-Setup.exe"}, remaining)
                self.assertFalse(self.state.exists())
                with self.assertRaisesRegex(transfer.TransferError, "incomplete"):
                    transfer.publish_staging(self.state)
                # The same staging directory accepts a clean retry.
                transfer.import_state(self.state, self.bundle)
                transfer.discard_staging(self.state)
                self.assertFalse(self.staging.exists())

    def test_staging_must_exist_be_fresh_and_be_pending_to_publish(self):
        with self.assertRaisesRegex(transfer.TransferError, "missing or already holds"):
            transfer.import_state(self.state, self.bundle)
        with self.assertRaisesRegex(transfer.TransferError, "incomplete"):
            transfer.publish_staging(self.state)
        staging = self.stage()
        with self.assertRaisesRegex(transfer.TransferError, "incomplete"):
            transfer.publish_staging(self.state)
        transfer.import_state(self.state, self.bundle)
        with self.assertRaisesRegex(transfer.TransferError, "missing or already holds"):
            transfer.import_state(self.state, self.bundle)
        # A staged copy that is not marked pending is never published.
        transfer.mark_active(staging)
        with self.assertRaisesRegex(transfer.TransferError, "incomplete"):
            transfer.publish_staging(self.state)
        self.assertFalse(self.state.exists())
        with self.assertRaises(transfer.TransferError):
            transfer.import_state(Path("relative/state"), self.bundle)
        with self.assertRaises(transfer.TransferError):
            transfer.import_state(self.state, transfer.Bundle({**self.bundle.manifest, "sessions": {}},
                                                              self.bundle.blobs))

    def test_discard_overwrites_secrets_and_never_touches_a_published_state(self):
        staging = self.stage()
        transfer.import_state(self.state, self.bundle)
        erased = {}
        unlink = Path.unlink

        def checked(path, *args, **kwargs):
            content = path.read_bytes()
            erased[path.relative_to(staging).as_posix()] = content.count(0) == len(content)
            return unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", checked):
            transfer.discard_staging(self.state)
        self.assertFalse(self.staging.exists())
        for name in ("secrets/discord-bot-token", "secrets/pepper", "relay/guild-hub.sqlite3", "relay.env"):
            self.assertTrue(erased[name])
        self.stage()
        transfer.import_state(self.state, self.bundle)
        transfer.publish_staging(self.state)
        transfer.discard_staging(self.state)
        self.assertTrue((self.state / "secrets/pepper").is_file())


class RecordsTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name).resolve()
        self.old = transfer.TransferRecords(self.root / "old/VemalsEarsTransfer")
        self.new = transfer.TransferRecords(self.root / "new/VemalsEarsTransfer")

    def test_sender_states_and_release_secret_only_after_released_is_on_disk(self):
        self.assertEqual({}, self.old.read())
        transfer_id, commitment = self.old.offer(start_modes={"VemalsEarsWorker": "delayed-auto"})
        receipt = self.old.read()
        self.assertEqual(("sender", "offered", transfer_id, commitment.hex()),
                         (receipt["role"], receipt["state"], receipt["transfer_id"], receipt["commitment"]))
        self.assertEqual({"start_modes": {"VemalsEarsWorker": "delayed-auto"}}, receipt["details"])
        for state in ("offered", "sending"):
            if state == "sending":
                self.assertEqual("sending", self.old.advance("sending")["state"])
            with self.assertRaisesRegex(transfer.TransferError, "not available before this host is released"):
                self.old.release_secret()
        # The secret is handed out only after the receipt already says released.
        order = []
        write, read = self.old._write, self.old.release_secret
        with mock.patch.object(self.old, "_write", side_effect=lambda value: order.append(value["state"]) or write(value)), \
                mock.patch.object(self.old, "release_secret", side_effect=lambda: order.append("secret") or read()):
            secret = self.old.release()
        self.assertEqual(["released", "secret"], order)
        self.assertEqual("released", json.loads(self.old.receipt.read_text(encoding="ascii"))["state"])
        self.assertTrue(transfer_pake.verify_release(transfer_id, commitment, secret))
        self.assertEqual(secret, self.old.release())
        self.assertEqual("moved", self.old.advance("moved")["state"])
        self.assertEqual(secret, self.old.release_secret())
        self.assertEqual(secret, transfer_pake.parse_release_code(transfer_pake.release_code(secret)))
        # The receipt itself never contains the secret.
        text = self.old.receipt.read_text(encoding="ascii")
        self.assertNotIn(secret.hex(), text)
        self.assertNotIn(transfer_pake.release_code(secret).replace("-", ""), text)

    def test_released_receipt_is_flushed_after_the_rename_and_before_the_secret_is_read(self):
        self.old.offer()
        self.old.advance("sending")
        events = []
        fsync, replace, read = os.fsync, transfer._replace, self.old.release_secret

        def on_disk():
            return json.loads(self.old.receipt.read_text(encoding="ascii"))["state"]

        def flushed(descriptor):
            events.append(("fsync", on_disk()))
            return fsync(descriptor)

        def renamed(source, target):
            self.assertEqual(self.old.receipt, target)
            events.append("rename")
            return replace(source, target)

        with mock.patch.object(transfer.os, "fsync", flushed), mock.patch.object(transfer, "_replace", renamed), \
                mock.patch.object(self.old, "release_secret", side_effect=lambda: events.append("secret") or read()):
            self.old.release()
        # Content first, then the rename, then a flush that sees "released"
        # as the published receipt, and only then the secret.
        self.assertEqual([("fsync", "sending"), "rename", ("fsync", "released")], events[:3])
        self.assertEqual("secret", events[-1])
        self.assertTrue(all(event == ("fsync", "released") for event in events[2:-1]))

    def test_release_withholds_the_secret_until_the_published_receipt_is_flushed(self):
        transfer_id, commitment = self.old.offer()
        self.old.advance("sending")
        fsync = os.fsync
        flushes = []

        def failing(descriptor):
            flushes.append(json.loads(self.old.receipt.read_text(encoding="ascii"))["state"])
            if flushes[-1] == "released":
                raise OSError("XENO flush failed")
            return fsync(descriptor)

        for _ in range(2):    # the first write, then a retry that finds "released" already there
            with mock.patch.object(transfer.os, "fsync", failing), \
                    mock.patch.object(self.old, "release_secret") as secret:
                with self.assertRaisesRegex(transfer.TransferError, "is on disk"):
                    self.old.release()
            secret.assert_not_called()
        self.assertEqual(["sending", "released", "released"], flushes)
        with mock.patch.object(transfer.os, "fsync", side_effect=lambda descriptor: flushes.append("ok") or fsync(descriptor)):
            released = self.old.release()
        self.assertIn("ok", flushes)
        self.assertTrue(transfer_pake.verify_release(transfer_id, commitment, released))

    def test_moved_receipt_locks_the_archive_until_it_is_erased(self):
        self.old.offer()
        self.old.advance("sending")
        secret = self.old.release()
        self.old.advance("moved")
        for call in (self.old.offer, lambda: self.old.receive(TRANSFER, COMMITMENT), self.old.erase,
                     lambda: self.old.erase(archive_erased="yes")):
            with self.assertRaisesRegex(transfer.TransferError, "locked archive"):
                call()
        self.assertEqual("moved", self.old.read()["state"])
        self.assertEqual(secret, self.old.release_secret())
        self.old.erase(archive_erased=True)
        self.assertEqual({}, self.old.read())
        self.assertFalse((self.old.directory / "release-secret").exists())
        self.old.offer()

    def test_released_is_one_way_and_shortcuts_are_refused(self):
        self.old.offer()
        for state in ("released", "active", "moved", "pending", "discarded", "offered", "XENO"):
            with self.assertRaisesRegex(transfer.TransferError, "not allowed"):
                self.old.advance(state)
        self.old.advance("sending")
        self.old.release()
        for state in ("cancelled", "sending", "offered", "released", "active"):
            with self.assertRaisesRegex(transfer.TransferError, "not allowed"):
                self.old.advance(state)
        self.assertEqual("released", self.old.read()["state"])
        with self.assertRaisesRegex(transfer.TransferError, "already in progress"):
            self.old.offer()
        with self.assertRaisesRegex(transfer.TransferError, "already in progress"):
            self.old.receive(TRANSFER, COMMITMENT)
        with self.assertRaisesRegex(transfer.TransferError, "still in progress"):
            self.old.erase()
        self.old.advance("moved")
        with self.assertRaisesRegex(transfer.TransferError, "not allowed"):
            self.old.advance("cancelled")

    def test_cancel_erases_the_release_secret_for_good(self):
        for cancel_from in ("offered", "sending"):
            with self.subTest(cancel_from=cancel_from):
                transfer_id, commitment = self.old.offer()
                secret = (self.old.directory / "release-secret").read_bytes()
                self.assertTrue(transfer_pake.verify_release(transfer_id, commitment, secret))
                if cancel_from == "sending":
                    self.old.advance("sending")
                self.assertEqual("cancelled", self.old.advance("cancelled", reason="receiver left")["state"])
                self.assertFalse((self.old.directory / "release-secret").exists())
                for call in (self.old.release, self.old.release_secret):
                    with self.assertRaises(transfer.TransferError):
                        call()
                self.assertEqual("cancelled", self.old.read()["state"])
        # A new offer after a terminal state gets a new id and a new secret.
        first, _ = self.old.offer()
        self.old.advance("cancelled")
        second, _ = self.old.offer()
        self.assertNotEqual(first, second)

    def test_receiver_cannot_produce_the_release_proof_before_release(self):
        offer = transfer_pake.Session(transfer_pake.ROLE_OFFER, "XEN0-7E5T", TRANSFER, "https://ears.example.org")
        claim = transfer_pake.Session(transfer_pake.ROLE_CLAIM, "XEN0-7E5T", TRANSFER, "https://ears.example.org")
        offer_keys, claim_keys = offer.finish(claim.share), claim.finish(offer.share)
        transfer_id, commitment = self.old.offer()
        self.old.advance("sending")
        self.new.receive(transfer_id, commitment)
        # Everything the receiver has: all session keys, the transcript, the
        # commitment from the authenticated bundle, and its own receipt.
        self.assertEqual(["transfer.json"], sorted(path.name for path in self.new.directory.iterdir()))
        receipt = self.new.read()
        self.assertEqual(("receiver", "pending", commitment.hex()),
                         (receipt["role"], receipt["state"], receipt["commitment"]))
        keys = [claim_keys.confirm_offer, claim_keys.confirm_claim, claim_keys.control_mac,
                claim_keys.bundle_enc, claim_keys.bundle_mac, claim_keys.transcript, commitment]
        guesses = keys + [hmac.digest(key, bytes.fromhex(transfer_id), "sha256") for key in keys]
        guesses += [hashlib.sha256(key + bytes.fromhex(transfer_id)).digest() for key in keys]
        for guess in guesses:
            with self.assertRaisesRegex(transfer.TransferError, "has not released"):
                self.new.activate(guess)
        for call in (self.new.release, self.new.release_secret):
            with self.assertRaises(transfer.TransferError):
                call()
        self.assertEqual("pending", self.new.read()["state"])
        self.assertEqual(0, self.new.read()["code_failures"])
        # The old host itself cannot hand it out before it is released...
        with self.assertRaises(transfer.TransferError):
            self.old.release_secret()
        # ...and after RELEASE the same call on the new host succeeds.
        secret = self.old.release()
        self.assertNotIn(secret, guesses)
        self.assertEqual("active", self.new.activate(secret)["state"])
        with self.assertRaises(transfer.TransferError):
            self.new.activate(secret)
        self.assertEqual(offer_keys.bundle_enc, claim_keys.bundle_enc)

    def test_cancelled_offer_leaves_a_pending_receiver_without_any_valid_proof(self):
        transfer_id, commitment = self.old.offer()
        self.old.advance("sending")
        self.new.receive(transfer_id, commitment)
        self.old.advance("cancelled")
        with self.assertRaises(transfer.TransferError):
            self.old.release()
        self.assertEqual("discarded", self.new.advance("discarded")["state"])
        with self.assertRaises(transfer.TransferError):
            self.new.activate(SECRET)

    def test_release_code_activates_and_wrong_codes_never_lock_out_the_genuine_one(self):
        transfer_id, commitment = self.old.offer()
        self.old.advance("sending")
        code = transfer_pake.release_code(self.old.release())
        self.new.receive(transfer_id, commitment)
        # A mistyped shape is not a guess.
        for typed in ("", "XENO", code[:-2]):
            with self.assertRaisesRegex(transfer.TransferError, "complete release code"):
                self.new.activate_with_code(typed)
        # Nor is a slip of the finger: the check group catches it first.
        for position in (0, 7, 30, len(code) - 1):
            typo = code[:position] + ("0" if code[position] != "0" else "1") + code[position + 1:]
            with self.assertRaisesRegex(transfer.TransferError, "typing mistake"):
                self.new.activate_with_code(typo)
        self.assertEqual(0, self.new.read()["code_failures"])
        wrong = transfer_pake.release_code(b"\x01" * 32)
        for attempt in range(1, 5):
            with self.assertRaisesRegex(transfer.TransferError, "does not match"):
                self.new.activate_with_code(wrong)
            self.assertEqual(attempt, self.new.read()["code_failures"])
        self.assertEqual("active", self.new.activate_with_code(code.lower().replace("-", " "))["state"])

        # When a release code is typed the old PC has usually stopped for
        # good, so this copy may be the only one left. No number of wrong
        # codes may lock it, and no message may say to discard it.
        self.new.receive(transfer_id, commitment)
        for attempt in range(1, 26):
            with self.assertRaisesRegex(transfer.TransferError, "does not match") as refused:
                self.new.activate_with_code(wrong)
            self.assertNotIn("iscard this transfer", str(refused.exception))
            self.assertNotIn("Too many", str(refused.exception))
            self.assertEqual(attempt, self.new.read()["code_failures"])
        self.assertEqual("pending", self.new.read()["state"])
        self.assertFalse(hasattr(transfer, "MAX_CODE_FAILURES"))
        self.assertEqual("active", self.new.activate_with_code(code)["state"])

    def test_override_needs_an_authenticated_sending_complete(self):
        self.new.receive(TRANSFER, COMMITMENT)
        with self.assertRaisesRegex(transfer.TransferError, "never finished sending"):
            self.new.activate_by_override()
        self.new.note(sending_complete="yes")
        with self.assertRaisesRegex(transfer.TransferError, "never finished sending"):
            self.new.activate_by_override()
        self.new.note(sending_complete=True)
        receipt = self.new.activate_by_override()
        self.assertEqual(("active", True), (receipt["state"], receipt["details"]["override"]))
        with self.assertRaises(transfer.TransferError):
            self.new.note(late=True)
        with self.assertRaises(transfer.TransferError):
            self.old.activate_by_override()

    def test_receipt_survives_a_restart_and_a_damaged_one_is_never_read_as_none(self):
        transfer_id, _ = self.old.offer()
        self.old.advance("sending")
        reopened = transfer.TransferRecords(self.old.directory)
        self.assertEqual(("sending", transfer_id), (reopened.read()["state"], reopened.read()["transfer_id"]))
        self.assertEqual([], [path.name for path in self.old.directory.iterdir() if path.suffix == ".tmp"])
        good = self.old.receipt.read_bytes()
        value = json.loads(good)
        for damaged in (b"", b"{", b"[]", json.dumps({**value, "state": "pending"}).encode(),
                        json.dumps({**value, "role": "XENO"}).encode(),
                        json.dumps({**value, "extra": 1}).encode(),
                        json.dumps({**value, "transfer_id": "XENO"}).encode(),
                        good[:-2] + b',"state":"cancelled"}\n', bytes(70_000)):
            self.old.receipt.write_bytes(damaged)
            with self.assertRaisesRegex(transfer.TransferError, "cannot be read"):
                self.old.read()
            for call in (self.old.offer, lambda: self.old.advance("cancelled"), self.old.release, self.old.erase):
                with self.assertRaises(transfer.TransferError):
                    call()
        self.old.receipt.write_bytes(good)
        self.old.advance("cancelled")
        self.old.erase()
        self.assertEqual({}, self.old.read())
        self.assertFalse((self.old.directory / "release-secret").exists())

    def test_roles_do_not_mix_and_inputs_are_checked(self):
        self.new.receive(TRANSFER, COMMITMENT, guild_id=GUILD)
        self.assertEqual({"guild_id": GUILD}, self.new.read()["details"])
        for call in (self.new.release, self.new.release_secret, lambda: self.new.advance("sending"),
                     lambda: self.new.advance("moved"), lambda: self.new.advance("active")):
            with self.assertRaises(transfer.TransferError):
                call()
        self.old.offer(TRANSFER)
        for call in (lambda: self.old.activate(SECRET), lambda: self.old.activate_with_code("XENO"),
                     lambda: self.old.advance("discarded")):
            with self.assertRaises(transfer.TransferError):
                call()
        empty = transfer.TransferRecords(self.root / "empty")
        for call in (lambda: empty.advance("sending"), empty.release, lambda: empty.activate(SECRET),
                     lambda: empty.note(x=1), lambda: empty.receive("XENO", COMMITMENT),
                     lambda: empty.receive(TRANSFER, b"short"), lambda: empty.offer("XENO")):
            with self.assertRaises(transfer.TransferError):
                call()
        self.assertEqual({}, empty.read())
        empty.erase()

    def test_lock_admits_one_operation_at_a_time(self):
        with self.old.locked() as records:
            self.assertIs(self.old, records)
            records.offer()
            with self.assertRaisesRegex(transfer.TransferError, "Another transfer operation"):
                with transfer.TransferRecords(self.old.directory).locked():
                    self.fail("second lock was granted")
        with self.old.locked():
            self.assertEqual("offered", self.old.read()["state"])

    @unittest.skipUnless(os.name == "nt", "Windows ACL integration")
    def test_record_directory_is_protected_before_the_secret_is_written(self):
        events = []
        protect, write = transfer.setup.protect_directory, handoff._write_private
        with mock.patch.object(transfer.setup, "protect_directory",
                               side_effect=lambda path: events.append("acl") or protect(path)), \
                mock.patch.object(handoff, "_write_private",
                                  side_effect=lambda path, data: events.append("secret") or write(path, data)):
            self.old.offer()
        self.assertEqual("acl", events[0])
        self.assertIn("secret", events)
        acl = inspect_acl(self.old.directory)
        self.assertTrue(acl["protected"])
        self.assertTrue(set(acl["sids"]).issubset({acl["currentSid"], "S-1-5-18", "S-1-5-32-544", "S-1-3-4"}))


if __name__ == "__main__":
    unittest.main()
