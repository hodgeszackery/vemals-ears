from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from GuildHub.credentials import DeviceCredentials
from GuildHub import updater
from VoiceCaptions.events import SessionEvent


class MemoryStore:
    def __init__(self, credentials=None):
        self.credentials = credentials

    def load(self):
        return self.credentials


class Response:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.offset = 0

    def read(self, amount=-1):
        if amount < 0:
            amount = len(self.payload)
        block = self.payload[self.offset:self.offset + amount]
        self.offset += len(block)
        return block

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def credentials(server_url="wss://ears.example.invalid"):
    return DeviceCredentials(
        server_url=server_url,
        device_id="dev_123",
        device_token="secret-device-token",
        guild_id="guild_1",
        guild_name="Raid",
    )


class DiscoveryTests(unittest.TestCase):
    def offer(self):
        return {
            "version": "9.9.9",
            "sha256": "a" * 64,
            "bytes": 123,
            "url": "https://downloads.example.invalid/Vemals-Ears-Setup.exe",
        }

    def test_device_authenticated_check_has_no_secret_in_url(self):
        seen = {}

        def open_request(request, timeout):
            seen["url"] = request.full_url
            seen["headers"] = dict(request.header_items())
            seen["timeout"] = timeout
            return Response(json.dumps({
                "type": "update_manifest",
                "update": self.offer(),
            }).encode("utf-8"))

        result = updater.fetch_update_offer(
            MemoryStore(credentials()),
            current_version="0.5.10",
            opener=open_request,
        )

        self.assertEqual(self.offer(), result)
        self.assertEqual("https://ears.example.invalid/v1/update", seen["url"])
        self.assertNotIn("secret-device-token", seen["url"])
        headers = {key.casefold(): value for key, value in seen["headers"].items()}
        self.assertEqual("Bearer secret-device-token", headers["authorization"])
        self.assertEqual("dev_123", headers["x-vemals-ears-device-id"])
        self.assertEqual("0.5.10", headers["x-vemals-ears-client-version"])

    def test_no_pairing_or_no_new_release_is_not_an_error(self):
        self.assertIsNone(updater.fetch_update_offer(MemoryStore(), opener=mock.Mock()))
        body = json.dumps({"type": "update_manifest", "update": None}).encode()
        self.assertIsNone(
            updater.fetch_update_offer(
                MemoryStore(credentials()),
                opener=lambda _request, timeout: Response(body),
            )
        )

    def test_valid_current_and_older_manifests_are_not_errors(self):
        for advertised, current in (
            ("0.5.12", "0.5.12"),
            ("0.5.12.0", "0.5.12"),
            ("0.5.11", "0.5.12"),
        ):
            with self.subTest(advertised=advertised, current=current):
                offer = self.offer()
                offer["version"] = advertised
                body = json.dumps({
                    "type": "update_manifest",
                    "update": offer,
                }).encode()
                self.assertIsNone(
                    updater.fetch_update_offer(
                        MemoryStore(credentials()),
                        current_version=current,
                        opener=lambda _request, timeout, body=body: Response(body),
                    )
                )

    def test_malformed_non_null_manifest_body_fails_closed(self):
        malformed = (
            [],
            "not-an-object",
            {"version": "0.5.13"},
            {**self.offer(), "version": "not-a-version"},
            {**self.offer(), "version": "0.5.12", "sha256": "not-a-digest"},
            {**self.offer(), "version": "0.5.11", "bytes": 0},
            {**self.offer(), "url": "http://ears.example.invalid/update.exe"},
        )
        for offer in malformed:
            with self.subTest(offer=offer):
                body = json.dumps({
                    "type": "update_manifest",
                    "update": offer,
                }).encode()
                with self.assertRaises(updater.AutomaticUpdateError):
                    updater.fetch_update_offer(
                        MemoryStore(credentials()),
                        current_version="0.5.12",
                        opener=lambda _request, timeout, body=body: Response(body),
                    )

    def test_invalid_or_oversized_response_fails_closed(self):
        invalid = (
            b"not json",
            json.dumps({"type": "update_manifest"}).encode(),
            json.dumps({
                "type": "update_manifest",
                "update": None,
                "extra": True,
            }).encode(),
            json.dumps({"type": "wrong", "update": None}).encode(),
            b"x" * (updater.MAX_MANIFEST_BYTES + 1),
        )
        for body in invalid:
            with self.subTest(size=len(body)):
                with self.assertRaises(updater.AutomaticUpdateError):
                    updater.fetch_update_offer(
                        MemoryStore(credentials()),
                        opener=lambda _request, timeout, body=body: Response(body),
                    )

    def test_plain_remote_transport_is_refused_but_loopback_development_works(self):
        with self.assertRaises(updater.AutomaticUpdateError):
            updater.update_endpoint("ws://ears.example.invalid")
        self.assertEqual(
            "http://127.0.0.1:8765/v1/update",
            updater.update_endpoint("ws://127.0.0.1:8765"),
        )


class StateFileTests(unittest.TestCase):
    def test_session_file_contains_only_enum_and_time(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "state.json"
            updater.write_session_state("active", target, now=lambda: 123.5)
            payload = json.loads(target.read_text(encoding="ascii"))
            self.assertEqual(
                {"observed_at": 123.5, "schema": 1, "state": "active"},
                payload,
            )
            self.assertNotIn("guild", json.dumps(payload).casefold())
            self.assertEqual(
                updater.SessionState("active", 123.5),
                updater.read_session_state(target),
            )

    def test_unknown_values_and_extra_private_fields_are_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "state.json"
            target.write_text(
                '{"schema":1,"state":"ready","observed_at":1,"caption":"secret"}',
                encoding="ascii",
            )
            self.assertIsNone(updater.read_session_state(target))
            updater.write_session_state("invented", target, now=lambda: 2)
            self.assertEqual("unknown", updater.read_session_state(target).state)

    def test_relaunch_marker_is_version_bound_fresh_and_one_shot(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            updater.write_relaunch_state(
                root, "0.5.11", "hidden", now=lambda: 100
            )
            self.assertIsNone(
                updater.consume_relaunch_state(
                    root, current_version="0.5.10", now=lambda: 101
                )
            )
            self.assertFalse(updater.relaunch_state_path(root).exists())

            updater.write_relaunch_state(
                root, "0.5.11", "minimized", now=lambda: 100
            )
            self.assertEqual(
                "minimized",
                updater.consume_relaunch_state(
                    root, current_version="0.5.11", now=lambda: 101
                ),
            )
            self.assertIsNone(
                updater.consume_relaunch_state(
                    root, current_version="0.5.11", now=lambda: 102
                )
            )

    def test_caption_client_reports_only_session_enum_to_update_scheduler(self):
        from GuildHub.client import GuildCaptionClient

        states = []

        class Sink:
            def send(self, _event):
                return True

        subject = GuildCaptionClient(
            MemoryStore(),
            Sink(),
            session_status=states.append,
        )
        self.assertTrue(subject._send_session_state(SessionEvent("ready", "private text")))
        self.assertTrue(subject._send_session_state(SessionEvent("ready", "private text")))
        self.assertEqual(["ready"], states)


class TrustTests(unittest.TestCase):
    def make_install(self, root: Path, *, trust=True):
        app = root / "app"
        app.mkdir()
        payload = {
            "schema": 1,
            "product": "Vemal's Ears",
            "version": "0.5.10",
        }
        if trust:
            payload["update_trust"] = {
                "schema": 1,
                "authenticode_publishers": ["CN=Vemal's Ears"],
            }
        (root / "payload-manifest.json").write_text(
            json.dumps(payload), encoding="ascii"
        )
        return app

    def test_only_installed_signed_manifest_seeds_automatic_trust(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            app = self.make_install(root)
            self.assertEqual(
                ("CN=Vemal's Ears",),
                updater.load_update_publishers(app, current_version="0.5.10"),
            )
            self.assertEqual((), updater.load_update_publishers(root))
            (root / "payload-manifest.json").write_text(
                json.dumps({
                    "schema": 1,
                    "product": "Vemal's Ears",
                    "version": "0.5.10",
                }),
                encoding="ascii",
            )
            self.assertEqual((), updater.load_update_publishers(app, current_version="0.5.10"))

    def test_automatic_verification_binds_bytes_version_and_publisher(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "setup.exe"
            body = b"MZ signed installer"
            target.write_bytes(body)
            offer = {
                "version": "0.5.11",
                "sha256": hashlib.sha256(body).hexdigest(),
                "bytes": len(body),
                "url": "https://example.invalid/setup.exe",
            }
            seen = []

            def verify(path, publishers, *, script):
                seen.append((path, publishers, script))

            updater.verify_automatic_installer(
                target,
                offer,
                ("CN=Publisher",),
                script=Path("verify.ps1"),
                signature_verifier=verify,
                version_reader=lambda _path: (0, 5, 11, 0),
            )
            self.assertEqual(("CN=Publisher",), seen[0][1])
            with self.assertRaises(updater.AutomaticUpdateError):
                updater.verify_automatic_installer(
                    target,
                    offer,
                    ("CN=Publisher",),
                    script=Path("verify.ps1"),
                    signature_verifier=verify,
                    version_reader=lambda _path: (0, 5, 10, 0),
                )
            target.write_bytes(body + b"tampered")
            with self.assertRaises(updater.AutomaticUpdateError):
                updater.verify_automatic_installer(
                    target,
                    offer,
                    ("CN=Publisher",),
                    script=Path("verify.ps1"),
                    signature_verifier=verify,
                    version_reader=lambda _path: (0, 5, 11, 0),
                )

    def test_manual_verification_still_binds_the_embedded_version(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "setup.exe"
            body = b"MZ unsigned installer"
            target.write_bytes(body)
            offer = {
                "version": "0.5.11",
                "sha256": hashlib.sha256(body).hexdigest(),
                "bytes": len(body),
                "url": "https://example.invalid/setup.exe",
            }
            updater.verify_manual_installer(
                target,
                offer,
                version_reader=lambda _path: (0, 5, 11, 0),
            )
            with self.assertRaises(updater.AutomaticUpdateError):
                updater.verify_manual_installer(
                    target,
                    offer,
                    version_reader=lambda _path: (0, 5, 12, 0),
                )

    def test_installer_versions_require_three_or_four_numeric_parts(self):
        self.assertEqual((0, 5, 11, 0), updater._numeric_version("0.5.11"))
        self.assertEqual((0, 5, 11, 2), updater._numeric_version("0.5.11.2"))
        self.assertIsNone(updater._numeric_version("5.11"))
        self.assertIsNone(updater._numeric_version("0.5.11.2.1"))
        self.assertIsNone(updater._numeric_version("0.5.65536"))

    def test_publisher_verifier_passes_values_as_separate_arguments(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / "weird $(name).exe"
            script = root / "verify.ps1"
            target.write_bytes(b"MZ")
            script.write_text("# fixture\n", encoding="ascii")
            calls = []

            def runner(arguments, **kwargs):
                calls.append((arguments, kwargs))
                return SimpleNamespace(returncode=0)

            updater.verify_authenticode_publisher(
                target,
                ("CN=Publisher $(literal)",),
                script=script,
                runner=runner,
            )
            self.assertIn(str(target), calls[0][0])
            self.assertIn("CN=Publisher $(literal)", calls[0][0])
            self.assertIsInstance(calls[0][0], list)
            self.assertFalse(calls[0][1]["check"])


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = self.root / "install" / "app"
        self.app.mkdir(parents=True)
        self.state = self.root / "state"
        self.body = b"MZ automatic installer bytes"
        self.offer = {
            "version": "0.5.11",
            "sha256": hashlib.sha256(self.body).hexdigest(),
            "bytes": len(self.body),
            "url": "https://downloads.example.invalid/setup.exe",
        }
        (self.app.parent / "payload-manifest.json").write_text(
            json.dumps({
                "schema": 1,
                "product": "Vemal's Ears",
                "version": "0.5.10",
                "update_trust": {
                    "schema": 1,
                    "authenticode_publishers": ["CN=Publisher"],
                },
            }),
            encoding="ascii",
        )
        self.clock_value = 1000.0
        self.download_threads = []
        self.download_count = 0

    def clock(self):
        return self.clock_value

    def download(self, offer, target):
        self.download_threads.append(threading.get_ident())
        self.download_count += 1
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.body)
        return target

    def fetch(self, _store, *, current_version):
        self.assertEqual("0.5.10", current_version)
        return dict(self.offer)

    @staticmethod
    def verify(_path, _offer, _publishers, *, script):
        return None

    def subject(
        self,
        *,
        trusted=True,
        fetch=None,
        download=None,
        automatic_verifier=None,
        manual_verifier=None,
    ):
        if not trusted:
            (self.app.parent / "payload-manifest.json").write_text(
                json.dumps({
                    "schema": 1,
                    "product": "Vemal's Ears",
                    "version": "0.5.10",
                }),
                encoding="ascii",
            )
        return updater.UpdateCoordinator(
            MemoryStore(credentials()),
            app_root=self.app,
            state_root=self.state,
            installed=True,
            current_version="0.5.10",
            fetch_offer=fetch or self.fetch,
            download=download or self.download,
            automatic_verifier=automatic_verifier or self.verify,
            manual_verifier=manual_verifier or (lambda _path, _offer: None),
            clock=self.clock,
            jitter=lambda: 0,
        )

    def finish_worker(self, subject, *, safe=False):
        self.assertIsNotNone(subject._thread)
        subject._thread.join(timeout=2)
        self.assertFalse(subject._thread.is_alive())
        return subject.tick(safe_to_apply=safe)

    def test_download_is_background_single_flight_then_auto_launch_waits_for_safe(self):
        subject = self.subject()
        main_thread = threading.get_ident()
        subject.offer_from_caption_client(dict(self.offer))
        self.assertTrue(subject.busy)
        self.assertIsNone(subject.tick(safe_to_apply=False))
        self.finish_worker(subject, safe=False)
        self.assertEqual(1, self.download_count)
        self.assertNotEqual(main_thread, self.download_threads[0])
        self.assertTrue(subject.auto_trusted)
        self.assertIsNone(subject.tick(safe_to_apply=False))

        self.assertIsNone(subject.tick(safe_to_apply=True))
        request = self.finish_worker(subject, safe=True)
        self.assertTrue(request.automatic)
        self.assertEqual("0.5.11", request.version)
        self.assertEqual(subject.staged, request.path)

    def test_cached_stage_is_reverified_without_redownload(self):
        subject = self.subject()
        target = updater.staged_update_path(self.offer, self.state)
        target.parent.mkdir(parents=True)
        target.write_bytes(self.body)
        subject.offer_from_caption_client(dict(self.offer))
        self.finish_worker(subject)
        self.assertEqual(0, self.download_count)
        self.assertEqual(target, subject.staged)

    def test_unsigned_install_stages_but_requires_explicit_manual_request(self):
        subject = self.subject(trusted=False)
        subject.offer_from_caption_client(dict(self.offer))
        self.finish_worker(subject, safe=True)
        self.assertFalse(subject.auto_trusted)
        self.assertIsNone(subject.tick(safe_to_apply=True))
        subject.request_manual_install()
        request = self.finish_worker(subject, safe=True)
        self.assertFalse(request.automatic)

    def test_trusted_signature_failure_never_becomes_a_manual_installer(self):
        verifier_calls = []

        def reject(*args, **kwargs):
            verifier_calls.append((args, kwargs))
            raise updater.AutomaticUpdateError("wrong publisher")

        subject = self.subject(automatic_verifier=reject)
        subject.offer_from_caption_client(dict(self.offer))
        self.assertIsNone(self.finish_worker(subject, safe=True))
        self.assertEqual(1, len(verifier_calls))
        self.assertIsNone(subject.staged)
        self.assertFalse(subject.auto_trusted)
        self.assertIn("publisher", subject.error)

        subject.request_manual_install()
        self.assertIsNone(self.finish_worker(subject, safe=True))
        self.assertEqual(2, len(verifier_calls))
        self.assertIsNone(subject.staged)

    def test_trusted_manual_preflight_rechecks_publisher_and_cannot_launch(self):
        calls = []

        def verifier(*_args, **_kwargs):
            calls.append(None)
            if len(calls) > 1:
                raise updater.AutomaticUpdateError("signature changed")

        subject = self.subject(automatic_verifier=verifier)
        subject.offer_from_caption_client(dict(self.offer))
        self.finish_worker(subject)
        self.assertTrue(subject.auto_trusted)

        subject.request_manual_install()
        self.assertIsNone(self.finish_worker(subject, safe=True))
        self.assertEqual(2, len(calls))
        self.assertIn("signature", subject.error)
        self.assertIsNone(subject.staged)

    def test_unsigned_stage_fails_closed_when_embedded_version_is_wrong(self):
        manual = mock.Mock(
            side_effect=updater.AutomaticUpdateError("embedded version mismatch")
        )
        subject = self.subject(trusted=False, manual_verifier=manual)
        subject.offer_from_caption_client(dict(self.offer))

        self.assertIsNone(self.finish_worker(subject, safe=True))
        manual.assert_called_once()
        self.assertIsNone(subject.staged)
        subject.request_manual_install()
        self.assertIsNone(self.finish_worker(subject, safe=True))
        self.assertEqual(2, manual.call_count)

    def test_withdrawn_release_cannot_pass_preflight(self):
        responses = [None]

        def fetch(_store, *, current_version):
            return responses.pop(0)

        subject = self.subject(fetch=fetch)
        subject.offer_from_caption_client(dict(self.offer))
        self.finish_worker(subject)
        subject.request_manual_install()
        self.assertIsNone(self.finish_worker(subject, safe=True))
        self.assertIsNone(subject.offer)
        self.assertIsNone(subject.staged)
        self.assertFalse(
            updater.staged_update_path(self.offer, self.state).exists()
        )

    def test_replaced_release_is_accepted_and_staged_immediately(self):
        replacement_body = b"MZ replacement installer"
        replacement = dict(self.offer)
        replacement.update({
            "version": "0.5.12",
            "sha256": hashlib.sha256(replacement_body).hexdigest(),
            "bytes": len(replacement_body),
        })
        responses = [replacement]

        def fetch(_store, *, current_version):
            return dict(responses.pop(0))

        def download(offer, target):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(
                replacement_body if offer["version"] == "0.5.12" else self.body
            )

        subject = self.subject(fetch=fetch, download=download)
        subject.offer_from_caption_client(dict(self.offer))
        self.finish_worker(subject)
        subject.request_manual_install()
        self.assertIsNone(self.finish_worker(subject, safe=True))
        self.assertEqual("0.5.12", subject.offer["version"])
        self.assertTrue(subject.busy)
        self.assertFalse(
            updater.staged_update_path(self.offer, self.state).exists()
        )
        self.finish_worker(subject)
        self.assertEqual("0.5.12", subject.offer["version"])
        self.assertIsNotNone(subject.staged)

    def test_duplicate_offer_does_not_redownload_or_launch_twice(self):
        subject = self.subject()
        subject.offer_from_caption_client(dict(self.offer))
        self.finish_worker(subject)
        subject.offer_from_caption_client(dict(self.offer))
        self.assertEqual(1, self.download_count)
        self.assertFalse(subject.busy)
        subject.mark_launched(automatic=True)
        self.assertIsNone(subject.tick(safe_to_apply=True))

    def test_failed_launcher_does_not_spin_on_every_tick(self):
        subject = self.subject()
        subject.offer_from_caption_client(dict(self.offer))
        self.finish_worker(subject)
        subject.next_check = self.clock_value + 10 * updater.HEALTHY_CHECK_SECONDS
        subject.mark_launch_failed(
            "The update could not be started.", automatic=True
        )
        self.assertIsNone(subject.tick(safe_to_apply=True))
        self.assertFalse(subject.busy)
        self.clock_value += updater.FAILED_CHECK_SECONDS - 1
        self.assertIsNone(subject.tick(safe_to_apply=True))
        self.assertFalse(subject.busy)

    def test_failed_download_waits_for_stage_backoff(self):
        calls = []

        def fail(_offer, _target):
            calls.append(self.clock_value)
            raise OSError("offline")

        subject = self.subject(download=fail)
        subject.next_check = self.clock_value + updater.HEALTHY_CHECK_SECONDS
        subject.offer_from_caption_client(dict(self.offer))
        self.finish_worker(subject)
        self.assertEqual(1, len(calls))
        self.assertFalse(subject.busy)

        subject.tick(safe_to_apply=True)
        self.clock_value += updater.FAILED_CHECK_SECONDS - 1
        subject.tick(safe_to_apply=True)
        self.assertEqual(1, len(calls))
        self.clock_value += 1
        subject.tick(safe_to_apply=True)
        self.finish_worker(subject)
        self.assertEqual(2, len(calls))

    def test_automatic_installer_timeout_honors_apply_backoff(self):
        subject = self.subject()
        subject.next_check = self.clock_value + 10 * updater.HEALTHY_CHECK_SECONDS
        subject.offer_from_caption_client(dict(self.offer))
        self.finish_worker(subject)
        subject.tick(safe_to_apply=True)
        request = self.finish_worker(subject, safe=True)
        self.assertTrue(request.automatic)
        subject.mark_launched(automatic=True)

        self.clock_value += updater.INSTALLER_TIMEOUT_SECONDS
        subject.tick(safe_to_apply=True)
        self.assertFalse(subject.busy)
        self.assertEqual(
            self.clock_value + updater.FAILED_CHECK_SECONDS,
            subject.apply_retry_at,
        )
        subject.tick(safe_to_apply=True)
        self.assertFalse(subject.busy)
        self.clock_value += updater.FAILED_CHECK_SECONDS - 1
        subject.tick(safe_to_apply=True)
        self.assertFalse(subject.busy)

    def test_automatic_preflight_that_becomes_unsafe_uses_busy_retry_gate(self):
        subject = self.subject()
        subject.offer_from_caption_client(dict(self.offer))
        self.finish_worker(subject)
        subject.tick(safe_to_apply=True)

        self.assertIsNone(self.finish_worker(subject, safe=False))
        self.assertEqual(
            self.clock_value + updater.BUSY_RETRY_SECONDS,
            subject.apply_retry_at,
        )
        self.assertFalse(subject.busy)
        subject.tick(safe_to_apply=True)
        self.assertFalse(subject.busy)
        self.clock_value += updater.BUSY_RETRY_SECONDS
        subject.tick(safe_to_apply=True)
        self.assertTrue(subject.busy)
        request = self.finish_worker(subject, safe=True)
        self.assertTrue(request.automatic)

    def test_manual_installer_timeout_never_converts_to_automatic_retry(self):
        subject = self.subject()
        subject.next_check = self.clock_value + 10 * updater.HEALTHY_CHECK_SECONDS
        subject.offer_from_caption_client(dict(self.offer))
        self.finish_worker(subject)
        subject.request_manual_install()
        request = self.finish_worker(subject, safe=True)
        self.assertFalse(request.automatic)
        subject.mark_launched(automatic=False)
        subject.next_check = self.clock_value + 100 * updater.HEALTHY_CHECK_SECONDS

        self.clock_value += updater.INSTALLER_TIMEOUT_SECONDS
        subject.tick(safe_to_apply=True)
        self.assertTrue(subject.auto_suppressed)
        self.clock_value += 10 * updater.FAILED_CHECK_SECONDS
        subject.tick(safe_to_apply=True)
        self.assertFalse(subject.busy)

    def test_successful_stage_prunes_old_installer_cache(self):
        update_root = self.state / "updates"
        update_root.mkdir(parents=True)
        old_exe = update_root / ("0.5.9-" + "b" * 16 + ".exe")
        old_part = update_root / ("0.5.9-" + "c" * 16 + ".exe.part")
        unrelated = update_root / "keep.txt"
        old_exe.write_bytes(b"old")
        old_part.write_bytes(b"partial")
        unrelated.write_text("keep", encoding="ascii")

        subject = self.subject()
        subject.offer_from_caption_client(dict(self.offer))
        self.finish_worker(subject)

        self.assertFalse(old_exe.exists())
        self.assertFalse(old_part.exists())
        self.assertTrue(unrelated.exists())
        self.assertTrue(subject.staged.exists())

    def test_successful_remote_none_withdraws_caption_notice_authoritatively(self):
        subject = self.subject(fetch=lambda _store, **_kw: None)
        subject.offer_from_caption_client(dict(self.offer))
        self.finish_worker(subject)
        subject.next_check = self.clock_value
        subject.tick(safe_to_apply=False)
        self.finish_worker(subject)
        self.assertTrue(subject.remote_authoritative)
        self.assertIsNone(subject.offer)
        subject.offer_from_caption_client(dict(self.offer))
        self.assertIsNone(subject.offer)


if __name__ == "__main__":
    unittest.main()
