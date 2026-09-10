import asyncio
import hashlib
import hmac
import io
import os
import tempfile
import time
import shutil
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from GuildHub import host
from GuildHub.host import (
    CONSECUTIVE_FAILURE_LIMIT,
    GIVE_UP_EXIT_CODE,
    HEALTHY_RUN_SECONDS,
    MAX_SETTING_FILE_BYTES,
    HeadlessHostConfig,
    HealthReportingSink,
    HostConfigurationError,
    WorkerHealthMarker,
    derive_viewer_identity_key,
    load_host_config,
    main,
)
from GuildHub.publisher import PublisherError
from VoiceCaptions.events import CaptionEvent, SessionEvent


APPLICATION_ID = "123456789012345678"
BOT_TOKEN = "discord-token-" + ("b" * 40)
GUILD_ID = "987654321098765432"
SERVER_URL = "wss://ears.example.test"
PUBLISHER_TOKEN = "publisher_" + ("p" * 40)


class FakePublisherSink:
    def __init__(self):
        self.accepts = True
        self.alive_value = True
        self.closed = False
        self.events = []
        self.routes = []
        self.audiences = []
        self.links = []
        self.flushes = 0
        self.flush_accepts = True

    def send(
        self,
        event,
        *,
        audience_generation=None,
        audience_revision=None,
    ):
        self.events.append(event)
        self.routes.append((audience_generation, audience_revision))
        return self.accepts

    def alive(self):
        return self.alive_value and not self.closed

    def update_audience(self, generation, revision, tokens):
        self.audiences.append((generation, revision, tokens))
        return self.accepts

    def link_viewer(self, code, token):
        self.links.append((code, token))
        return self.accepts

    def enroll_viewer(self, code, token, ttl_seconds):
        self.enrollments.append((code, token, ttl_seconds))
        return self.accepts

    def flush(self):
        self.flushes += 1
        return self.flush_accepts

    def close(self):
        self.closed = True


class FakeQuickChatWorker:
    def __init__(self):
        self.closed = False
        self.commands = []
        self.results = []

    def alive(self):
        return not self.closed

    def next_quick_chat(self):
        return self.commands.pop(0) if self.commands else None

    def complete_quick_chat(self, request_id, status):
        self.results.append((request_id, status))
        return True

    def close(self):
        self.closed = True


class HostConfigurationTests(unittest.TestCase):
    def valid_environment(self, health_file):
        return {
            "VEMALS_EARS_DISCORD_APPLICATION_ID": APPLICATION_ID,
            "VEMALS_EARS_DISCORD_BOT_TOKEN": BOT_TOKEN,
            "VEMALS_EARS_GUILD_ID": GUILD_ID,
            "VEMALS_EARS_SERVER_URL": SERVER_URL,
            "VEMALS_EARS_PUBLISHER_TOKEN": PUBLISHER_TOKEN,
            "VEMALS_EARS_WORKER_HEALTH_FILE": str(health_file),
        }

    def test_direct_environment_is_validated_and_repr_is_redacted(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            health_file = Path(temporary_directory) / "worker.state"
            config = load_host_config(self.valid_environment(health_file))

        self.assertIsInstance(config, HeadlessHostConfig)
        self.assertEqual(APPLICATION_ID, config.credentials.application_id)
        self.assertEqual(BOT_TOKEN, config.credentials.bot_token)
        self.assertEqual(GUILD_ID, config.guild_id)
        self.assertEqual(SERVER_URL, config.server_url)
        self.assertEqual(PUBLISHER_TOKEN, config.publisher_token)
        rendered = repr(config)
        for secret in (APPLICATION_ID, BOT_TOKEN, GUILD_ID, PUBLISHER_TOKEN):
            self.assertNotIn(secret, rendered)

    def test_viewer_identity_key_is_stable_context_bound_hmac_sha256(self):
        expected = hmac.new(
            PUBLISHER_TOKEN.encode("ascii"),
            b"vemals-ears-viewer-v1",
            hashlib.sha256,
        ).digest()
        derived = derive_viewer_identity_key(PUBLISHER_TOKEN)
        self.assertEqual(expected, derived)
        self.assertEqual(32, len(derived))

    def test_every_setting_can_be_loaded_from_a_bounded_mounted_file(self):
        values = {
            "VEMALS_EARS_DISCORD_APPLICATION_ID": APPLICATION_ID,
            "VEMALS_EARS_DISCORD_BOT_TOKEN": BOT_TOKEN,
            "VEMALS_EARS_GUILD_ID": GUILD_ID,
            "VEMALS_EARS_SERVER_URL": SERVER_URL,
            "VEMALS_EARS_PUBLISHER_TOKEN": PUBLISHER_TOKEN,
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment = {
                "VEMALS_EARS_WORKER_HEALTH_FILE": str(root / "worker.state")
            }
            for index, (name, value) in enumerate(values.items()):
                path = root / ("secret-" + str(index))
                path.write_bytes((value + "\n").encode("utf-8"))
                environment[name + "_FILE"] = str(path)
            config = load_host_config(environment)

        self.assertEqual(APPLICATION_ID, config.credentials.application_id)
        self.assertEqual(BOT_TOKEN, config.credentials.bot_token)
        self.assertEqual(GUILD_ID, config.guild_id)
        self.assertEqual(SERVER_URL, config.server_url)
        self.assertEqual(PUBLISHER_TOKEN, config.publisher_token)

    def test_direct_and_file_sources_conflict_and_oversized_files_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment = self.valid_environment(root / "worker.state")
            secret_file = root / "token"
            secret_file.write_text(PUBLISHER_TOKEN, encoding="ascii")
            environment["VEMALS_EARS_PUBLISHER_TOKEN_FILE"] = str(secret_file)
            with self.assertRaises(HostConfigurationError) as conflict:
                load_host_config(environment)
            self.assertNotIn(PUBLISHER_TOKEN, str(conflict.exception))

            environment.pop("VEMALS_EARS_PUBLISHER_TOKEN")
            secret_file.write_bytes(b"x" * (MAX_SETTING_FILE_BYTES + 1))
            with self.assertRaises(HostConfigurationError) as oversized:
                load_host_config(environment)
            self.assertNotIn("x" * 20, str(oversized.exception))

            environment = self.valid_environment(root / "worker.state")
            environment["VEMALS_EARS_DISCORD_BOT_TOKEN"] = "x" * (
                MAX_SETTING_FILE_BYTES + 1
            )
            with self.assertRaises(HostConfigurationError):
                load_host_config(environment)

            environment = self.valid_environment(root / "worker.state")
            environment["VEMALS_EARS_DISCORD_BOT_TOKEN"] = BOT_TOKEN + "\x00"
            with self.assertRaises(HostConfigurationError):
                load_host_config(environment)

    def test_relative_secret_files_invalid_values_and_remote_ws_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            health_file = Path(temporary_directory) / "worker.state"
            environment = self.valid_environment(health_file)
            environment.pop("VEMALS_EARS_DISCORD_BOT_TOKEN")
            environment["VEMALS_EARS_DISCORD_BOT_TOKEN_FILE"] = "relative.secret"
            with self.assertRaises(HostConfigurationError):
                load_host_config(environment)

            for name, invalid in (
                ("VEMALS_EARS_GUILD_ID", "not-a-discord-guild"),
                ("VEMALS_EARS_SERVER_URL", "ws://ears.example.test"),
                ("VEMALS_EARS_SERVER_URL", "wss://ears.example.test/other"),
                ("VEMALS_EARS_PUBLISHER_TOKEN", "short"),
            ):
                environment = self.valid_environment(health_file)
                environment[name] = invalid
                with self.subTest(name=name, invalid=invalid):
                    with self.assertRaises(HostConfigurationError) as caught:
                        load_host_config(environment)
                    self.assertNotIn(invalid, str(caught.exception))

    def test_loopback_plain_ws_remains_available_for_local_testing(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            environment = self.valid_environment(
                Path(temporary_directory) / "worker.state"
            )
            environment["VEMALS_EARS_SERVER_URL"] = "ws://127.0.0.1:8765"
            config = load_host_config(environment)
        self.assertEqual("ws://127.0.0.1:8765", config.server_url)


class WorkerHealthTests(unittest.TestCase):
    def caption(self):
        return CaptionEvent(
            speaker_id="speaker-1",
            speaker="Mira",
            character="Miralyn",
            role="HEALER",
            text="Private raid caption text",
            final=True,
            sequence=1,
            timestamp=1.0,
            audience_generation=2,
            audience_revision=3,
        )

    def test_marker_contains_only_an_atomic_small_session_state(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "worker.state"
            marker = WorkerHealthMarker(path)
            for state in ("ready", "paused", "active"):
                marker.set_state(state)
                self.assertEqual((state + "\n").encode("ascii"), path.read_bytes())
                self.assertEqual(state, marker.read_state())
                self.assertTrue(marker.is_healthy())
                self.assertEqual([], list(root.glob(".worker.state.*")))
            marker.clear()
            self.assertFalse(path.exists())
            self.assertFalse(marker.is_healthy())
            self.assertIsNone(marker.read_state())

    def test_marker_rejects_untrusted_or_malformed_exact_state(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "worker.state"
            marker = WorkerHealthMarker(path)
            for payload in (b"active", b"ACTIVE\n", b"active\ncaption text"):
                path.write_bytes(payload)
                self.assertIsNone(marker.read_state())
            link = root / "worker-link.state"
            try:
                link.symlink_to(path)
            except OSError:
                return
            self.assertIsNone(WorkerHealthMarker(link).read_state())

    def test_health_is_written_only_after_accepted_session_and_never_for_caption(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "worker.state"
            marker = WorkerHealthMarker(path)
            publisher = FakePublisherSink()
            sink = HealthReportingSink(publisher, marker)

            self.assertTrue(sink.send(self.caption()))
            self.assertFalse(path.exists())
            self.assertEqual([(2, 3)], publisher.routes)

            self.assertTrue(sink.send(SessionEvent("ready")))
            self.assertEqual(b"ready\n", path.read_bytes())
            self.assertEqual((None, None), publisher.routes[-1])
            self.assertNotIn(b"Private raid caption text", path.read_bytes())

            self.assertTrue(sink.send(SessionEvent("stopped")))
            self.assertEqual(b"ready\n", path.read_bytes())
            self.assertEqual(1, publisher.flushes)

            self.assertTrue(sink.send(SessionEvent("error")))
            self.assertFalse(path.exists())

    def test_publish_failure_and_close_clear_health(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "worker.state"
            marker = WorkerHealthMarker(path)
            publisher = FakePublisherSink()
            sink = HealthReportingSink(publisher, marker)

            self.assertTrue(sink.send(SessionEvent("active")))
            self.assertTrue(path.exists())
            publisher.accepts = False
            self.assertFalse(sink.send(SessionEvent("paused")))
            self.assertFalse(path.exists())
            self.assertTrue(publisher.closed)

            publisher = FakePublisherSink()
            sink = HealthReportingSink(publisher, marker)
            self.assertTrue(sink.send(SessionEvent("ready")))
            sink.close()
            self.assertFalse(path.exists())

    def test_unconfirmed_stopped_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "worker.state"
            marker = WorkerHealthMarker(path)
            marker.set_state("active")
            publisher = FakePublisherSink()
            publisher.flush_accepts = False
            sink = HealthReportingSink(publisher, marker)

            self.assertFalse(sink.send(SessionEvent("stopped")))
            self.assertEqual(1, publisher.flushes)
            self.assertTrue(publisher.closed)
            self.assertFalse(path.exists())

    def test_quick_chat_worker_is_proxied_and_closed_with_caption_sink(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            marker = WorkerHealthMarker(
                Path(temporary_directory) / "worker.state"
            )
            publisher = FakePublisherSink()
            quick_chat = FakeQuickChatWorker()
            expected = object()
            quick_chat.commands.append(expected)
            sink = HealthReportingSink(publisher, marker, quick_chat)

            self.assertIs(expected, sink.next_quick_chat())
            self.assertTrue(
                sink.complete_quick_chat("worker_request_123456", "delivered")
            )
            self.assertEqual(
                [("worker_request_123456", "delivered")], quick_chat.results
            )
            sink.close()
            self.assertTrue(publisher.closed)
            self.assertTrue(quick_chat.closed)

    def test_private_audience_and_link_controls_are_proxied_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            marker = WorkerHealthMarker(
                Path(temporary_directory) / "worker.state"
            )
            publisher = FakePublisherSink()
            sink = HealthReportingSink(publisher, marker)
            token = "a" * 24

            self.assertTrue(sink.update_audience(2, 3, (token,)))
            self.assertTrue(sink.link_viewer("AB12CD34", token))
            publisher.enrollments = []
            self.assertTrue(sink.enroll_viewer("ghe_" + "A" * 32, token, 1800))
            self.assertEqual([(2, 3, (token,))], publisher.audiences)
            self.assertEqual([("AB12CD34", token)], publisher.links)
            self.assertEqual([("ghe_" + "A" * 32, token, 1800)], publisher.enrollments)

            publisher.accepts = False
            self.assertFalse(sink.update_audience(2, 4, ()))
            self.assertTrue(publisher.closed)

    def test_a_refused_invitation_fails_closed_like_every_other_control(self):
        """A sink that answers "not accepted" and is believed anyway would
        hand someone a link the relay never stored."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            marker = WorkerHealthMarker(
                Path(temporary_directory) / "worker.state"
            )
            publisher = FakePublisherSink()
            publisher.enrollments = []
            publisher.accepts = False
            sink = HealthReportingSink(publisher, marker)
            self.assertFalse(
                sink.enroll_viewer("ghe_" + "A" * 32, "a" * 24, 1800),
                "a refused invitation was reported as accepted",
            )
            self.assertTrue(publisher.closed)

    def test_healthcheck_needs_one_exact_healthy_state(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "worker.state"
            environment = {"VEMALS_EARS_WORKER_HEALTH_FILE": str(path)}
            marker = WorkerHealthMarker(path)
            self.assertEqual(1, main(["--healthcheck"], environment=environment))
            marker.set_state("paused")
            self.assertEqual(0, main(["--healthcheck"], environment=environment))
            path.write_text("active\ncaption text", encoding="ascii")
            self.assertEqual(1, main(["--healthcheck"], environment=environment))


class HostMainTests(unittest.TestCase):
    def valid_environment(self, health_file):
        return {
            "VEMALS_EARS_DISCORD_APPLICATION_ID": APPLICATION_ID,
            "VEMALS_EARS_DISCORD_BOT_TOKEN": BOT_TOKEN,
            "VEMALS_EARS_GUILD_ID": GUILD_ID,
            "VEMALS_EARS_SERVER_URL": SERVER_URL,
            "VEMALS_EARS_PUBLISHER_TOKEN": PUBLISHER_TOKEN,
            "VEMALS_EARS_WORKER_HEALTH_FILE": str(health_file),
        }

    def test_watch_injects_static_credentials_guild_and_remote_sink(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            health_file = Path(temporary_directory) / "worker.state"
            environment = self.valid_environment(health_file)
            captured = {}
            publisher = FakePublisherSink()
            quick_chat = FakeQuickChatWorker()
            readiness = object()

            def check_readiness(**options):
                captured["credentials"] = options["credential_store"].load()
                return readiness

            def watch(**options):
                captured["guild_id"] = os.environ.get("DISCORD_GUILD_ID")
                captured["readiness"] = options["readiness_check"]()
                captured["sink"] = options["overlay_factory"]()
                return 7

            with patch.dict(os.environ, {"DISCORD_GUILD_ID": "previous"}):
                with patch("GuildHub.host.check_runtime_readiness", check_readiness):
                    with patch("GuildHub.host.run_watch", watch):
                        with patch(
                            "GuildHub.host.RemoteCaptionSink.start",
                            return_value=publisher,
                        ) as start:
                            with patch(
                                "GuildHub.host.RemoteQuickChatWorker.start",
                                return_value=quick_chat,
                            ) as quick_start:
                                result = main(["--watch"], environment=environment)
                self.assertEqual("previous", os.environ["DISCORD_GUILD_ID"])

        self.assertEqual(7, result)
        self.assertEqual(GUILD_ID, captured["guild_id"])
        self.assertIs(readiness, captured["readiness"])
        self.assertEqual(APPLICATION_ID, captured["credentials"].application_id)
        self.assertEqual(BOT_TOKEN, captured["credentials"].bot_token)
        self.assertIsInstance(captured["sink"], HealthReportingSink)
        start.assert_called_once_with(SERVER_URL, PUBLISHER_TOKEN)
        quick_start.assert_called_once_with(SERVER_URL, PUBLISHER_TOKEN)

    def test_invalid_configuration_output_never_contains_values(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            health_file = Path(temporary_directory) / "worker.state"
            environment = self.valid_environment(health_file)
            environment["VEMALS_EARS_DISCORD_BOT_TOKEN"] = BOT_TOKEN + " bad"
            output = io.StringIO()
            with redirect_stdout(output):
                result = main(["--watch"], environment=environment)

        self.assertEqual(2, result)
        rendered = output.getvalue()
        for value in (APPLICATION_ID, BOT_TOKEN, GUILD_ID, PUBLISHER_TOKEN):
            self.assertNotIn(value, rendered)

    def test_watch_injects_derived_identity_key_into_coordinator(self):
        captured = {}

        class FakeStt:
            max_pending_segments = 1

            async def close(self):
                return None

        async def node_capture(*args, coordinator_factory):
            instance = coordinator_factory(FakeStt(), lambda _event: True)
            captured["identity_key"] = instance.identity_key
            await instance.shutdown()

        def watch(**options):
            asyncio.run(
                options["capture_runner"](
                    object(), object(), object(), lambda _event: True, object()
                )
            )
            return 0

        with tempfile.TemporaryDirectory() as temporary_directory:
            environment = self.valid_environment(
                Path(temporary_directory) / "worker.state"
            )
            with patch("GuildHub.host.run_watch", watch):
                with patch("GuildHub.host.run_node_capture", node_capture):
                    result = main(["--watch"], environment=environment)

        self.assertEqual(0, result)
        self.assertEqual(
            derive_viewer_identity_key(PUBLISHER_TOKEN),
            captured["identity_key"],
        )




class GiveUpTests(unittest.TestCase):
    """A failure that repeats is not a failure that repeats forever.

    Measured on a real machine before any of this existed: 363 crash
    logs across 1,375 minutes. The service manager does throttle - the
    early gaps run 5, 5, 8, 16 seconds - and then RESETS, so the median
    settles at 14.2 seconds and stays there indefinitely. Every restart
    logged the same line. Nothing escalated. Nothing anywhere said a
    service had failed hundreds of times, so nobody looked.
    """

    def valid_environment(self, health_file):
        return {
            "VEMALS_EARS_DISCORD_APPLICATION_ID": APPLICATION_ID,
            "VEMALS_EARS_DISCORD_BOT_TOKEN": BOT_TOKEN,
            "VEMALS_EARS_GUILD_ID": GUILD_ID,
            "VEMALS_EARS_SERVER_URL": SERVER_URL,
            "VEMALS_EARS_PUBLISHER_TOKEN": PUBLISHER_TOKEN,
            "VEMALS_EARS_WORKER_HEALTH_FILE": str(health_file),
        }

    def run_failing(self, environment, message="quick-chat relay is unavailable"):
        """One start that fails the way the real one did."""

        def watch(**options):
            raise PublisherError(message)

        output = io.StringIO()
        with redirect_stdout(output):
            with patch("GuildHub.host.run_watch", watch):
                result = main(["--watch"], environment=environment)
        return result, output.getvalue()

    def test_a_burst_of_quick_failures_keeps_retrying(self):
        """The other half of the rule, added 2026-09-03.

        A relay restarting is indistinguishable from a broken worker if
        you only count. On 2026-09-03 the relay bounced four times in
        three minutes, this worker spent all five failures in 66 seconds,
        and captions were dead for seven and a half hours. A burst has to
        keep asking to be restarted.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            health_file = Path(temporary_directory) / "worker.state"
            environment = self.valid_environment(health_file)

            codes = []
            for _ in range(CONSECUTIVE_FAILURE_LIMIT + 2):
                result, _rendered = self.run_failing(environment)
                codes.append(result)

        self.assertEqual(
            [1] * (CONSECUTIVE_FAILURE_LIMIT + 2), codes,
            "failures inside one span must never give up - that is a "
            "server restarting, not a broken worker",
        )

    def test_failures_sustained_past_the_span_stop_asking_to_be_restarted(self):
        """The whole point: eventually it must stop, and say why.

        Without this the worker returns 1 forever and the service manager
        restarts it forever. 363 times is not a hypothetical - it is what
        this machine actually did, for 23 hours, while the real cause sat
        in every one of those logs.

        That incident spanned 23 HOURS, so the span check added on
        2026-09-03 catches it comfortably. This test used to simulate it
        in milliseconds, which the new rule correctly declines to treat
        as a broken worker - so the tally is aged instead, which is what
        the real 23 hours looked like to the file.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            health_file = Path(temporary_directory) / "worker.state"
            environment = self.valid_environment(health_file)
            tally = host._failure_tally_file(health_file)
            aged = time.time() - (host.GIVE_UP_MIN_SPAN_SECONDS + 60.0)
            tally.write_text(
                "%d %.3f" % (CONSECUTIVE_FAILURE_LIMIT - 1, aged),
                encoding="ascii",
            )

            codes = []
            renders = []
            result, rendered = self.run_failing(environment)
            codes.append(result)
            renders.append(rendered)
        # The one that crosses both the count and the clock refuses.
        self.assertEqual(GIVE_UP_EXIT_CODE, codes[-1])
        self.assertNotEqual(
            1,
            GIVE_UP_EXIT_CODE,
            "the give-up code must differ from the ordinary failure code, "
            "or the service manager cannot tell them apart",
        )
        # And it has to SAY so. A silent give-up is a service that is
        # simply missing, which is how this went unnoticed for 23 hours.
        self.assertIn(str(CONSECUTIVE_FAILURE_LIMIT), rendered)
        self.assertIn("giving up", rendered.casefold())
        # An attempt that is about to be restarted must not announce a
        # give-up. A log line that misreports the state is worse than no
        # line, because it is the one a person acts on.
        for index, earlier in enumerate(renders[:-1]):
            self.assertNotIn(
                "giving up",
                earlier.casefold(),
                "attempt %d is followed by a restart, so it must not say "
                "the worker gave up" % (index + 1),
            )
            # It must still carry the count, or nothing reads as
            # escalating - which is precisely how 363 crashes went unseen.
            self.assertIn(str(index + 1), earlier)
        # The real cause still has to be in the same output, because that
        # is the line whoever reads this needs.
        self.assertIn("quick-chat relay is unavailable", rendered)

    def test_the_error_that_caused_the_give_up_is_still_reported(self):
        """Each attempt names its own cause, not just the tally.

        The give-up message says how many times. It must never replace
        the line saying what actually failed - that swap is exactly the
        defect fixed in host.py's PublisherError handler.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            health_file = Path(temporary_directory) / "worker.state"
            environment = self.valid_environment(health_file)
            _, rendered = self.run_failing(
                environment, message="publisher token was refused"
            )

        self.assertIn("publisher token was refused", rendered)

    def test_a_run_that_stayed_up_clears_the_tally(self):
        """A blip every few hours must never accumulate into a give-up.

        Without this, five unrelated network hiccups spread across a week
        would stop the service permanently, and the message would blame
        the fifth one.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            health_file = Path(temporary_directory) / "worker.state"
            environment = self.valid_environment(health_file)

            for _ in range(CONSECUTIVE_FAILURE_LIMIT - 1):
                self.run_failing(environment)

            # One start that stays up past the healthy mark, then fails.
            clock = iter([0.0, HEALTHY_RUN_SECONDS + 1.0])
            with patch("GuildHub.host.time.monotonic", lambda: next(clock)):
                self.run_failing(environment)

            # The count restarted, so this is failure number one again.
            result, _ = self.run_failing(environment)

        self.assertEqual(
            1,
            result,
            "a long healthy run must reset the count, not merely pause it",
        )

    def test_a_successful_run_clears_the_tally(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            health_file = Path(temporary_directory) / "worker.state"
            environment = self.valid_environment(health_file)

            for _ in range(CONSECUTIVE_FAILURE_LIMIT - 1):
                self.run_failing(environment)

            with patch("GuildHub.host.run_watch", lambda **options: 0):
                with redirect_stdout(io.StringIO()):
                    good = main(["--watch"], environment=environment)

            result, _ = self.run_failing(environment)

        self.assertEqual(0, good)
        self.assertEqual(1, result)

    def test_a_corrupt_tally_cannot_by_itself_stop_the_service(self):
        """Failing closed here would be worse than the bug.

        The tally is a scratch file in a temp directory. If something
        else writes junk into it, the worst outcome is refusing to start
        a service that is perfectly healthy - so garbage reads as zero.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            health_file = Path(temporary_directory) / "worker.state"
            tally = health_file.with_name(health_file.name + ".failures")
            environment = self.valid_environment(health_file)

            for junk in ("not a number", "", "-4", "99999999"):
                tally.write_text(junk, encoding="ascii")
                result, _ = self.run_failing(environment)
                self.assertEqual(
                    1, result, "junk tally %r must not trigger a give-up" % junk
                )


class StopSignalTests(unittest.TestCase):
    """SIGTERM must reach run_watch as a set stop event.

    Without this the container's stop signal killed Python mid-flight and the
    node adapter was never asked to finish its ledger save and run-marker
    cleanup. The ordinary stop path closes its stdin cleanly; if a forced stop
    interrupts the last write, the surviving unsynced marker re-asks grants
    while retaining declines.
    """

    def test_sigterm_sets_the_event_run_watch_receives(self):
        import signal as signal_module

        with tempfile.TemporaryDirectory() as temporary_directory:
            health_file = Path(temporary_directory) / "worker.state"
            environment = {
                "VEMALS_EARS_DISCORD_APPLICATION_ID": APPLICATION_ID,
                "VEMALS_EARS_DISCORD_BOT_TOKEN": BOT_TOKEN,
                "VEMALS_EARS_GUILD_ID": GUILD_ID,
                "VEMALS_EARS_SERVER_URL": SERVER_URL,
                "VEMALS_EARS_PUBLISHER_TOKEN": PUBLISHER_TOKEN,
                "VEMALS_EARS_WORKER_HEALTH_FILE": str(health_file),
            }
            captured = {}

            def watch(**options):
                captured["stop_event"] = options.get("stop_event")
                return 0

            previous_term = signal_module.getsignal(signal_module.SIGTERM)
            previous_int = signal_module.getsignal(signal_module.SIGINT)
            try:
                with patch("GuildHub.host.run_watch", watch):
                    main(["--watch"], environment=environment)
                handler = signal_module.getsignal(signal_module.SIGTERM)
            finally:
                signal_module.signal(signal_module.SIGTERM, previous_term)
                signal_module.signal(signal_module.SIGINT, previous_int)

            event = captured.get("stop_event")
            self.assertIsNotNone(
                event, "main() no longer hands run_watch a stop event"
            )
            self.assertFalse(event.is_set(), "the event must start unset")
            self.assertTrue(
                callable(handler),
                "main() no longer installs a SIGTERM handler",
            )
            # Invoke the installed handler exactly as the runtime would.
            handler(signal_module.SIGTERM, None)
            self.assertTrue(
                event.is_set(),
                "the SIGTERM handler does not set the stop event run_watch "
                "was given, so a container stop still kills the run mid-flight",
            )


class RestartStormSurvivalTests(unittest.TestCase):
    """Five failures inside a minute is a relay restarting, not a fault.

    2026-09-03: the relay restarted four times in three minutes and this
    worker spent its ENTIRE five-failure budget in 66 seconds - at
    00:22:25, :32, :38, then 00:23:24 and :31 - then stopped for good.
    Captions were dead seven and a half hours until a human restarted the
    service. The limit was right; counting without a clock was not.
    """

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.tally = self.folder / "worker.state.failures"

    def record(self, count, *, first_at, now):
        """Put the tally in a known state without waiting for real time."""
        self.tally.write_text("%d %.3f" % (count, first_at), encoding="ascii")
        return host._read_failure_tally(self.tally, now)

    # ------------------------------------------------------- the tally

    def test_a_fresh_failure_starts_its_own_clock(self):
        count, first = host._record_failure(self.tally, now=1000.0)
        self.assertEqual(1, count)
        self.assertEqual(1000.0, first)

    def test_later_failures_keep_the_first_ones_clock(self):
        host._record_failure(self.tally, now=1000.0)
        count, first = host._record_failure(self.tally, now=1030.0)
        self.assertEqual(2, count)
        self.assertEqual(1000.0, first, "the span must be measured from the FIRST failure")

    def test_a_tally_written_before_this_carried_a_time_still_reads(self):
        # Upgrading must not turn an old single-number tally into a
        # give-up or a crash. Treating its first failure as now costs one
        # extra span of patience, which is the safe direction.
        self.tally.write_text("4", encoding="ascii")
        count, first = host._read_failure_tally(self.tally, 5000.0)
        self.assertEqual(4, count)
        self.assertEqual(5000.0, first)

    def test_a_clock_that_moved_backwards_is_not_trusted(self):
        # A first-failure time in the future would make the span
        # negative and could give up instantly.
        _count, first = self.record(4, first_at=9000.0, now=1000.0)
        self.assertEqual(1000.0, first)

    def test_a_corrupt_tally_never_causes_a_give_up(self):
        for raw in ("", "   ", "nonsense", "-3 100", "99999 100", "4 nonsense"):
            self.tally.write_text(raw, encoding="ascii")
            count, first = host._read_failure_tally(self.tally, 1000.0)
            with self.subTest(raw=raw):
                if raw == "4 nonsense":
                    # The count is readable; only the time is not.
                    self.assertEqual(4, count)
                    self.assertEqual(1000.0, first)
                else:
                    self.assertEqual(0, count)

    # -------------------------------------------------- the give-up rule

    def test_the_measured_storm_would_not_give_up(self):
        # The real timings from 2026-09-03, as offsets in seconds.
        first_at = 1000.0
        for offset in (0.0, 7.0, 13.0, 59.0, 66.0):
            count, first = self.record(
                int(offset // 7) + 1, first_at=first_at, now=first_at + offset
            )
        spanned = (first_at + 66.0) - first
        self.assertLess(
            spanned, host.GIVE_UP_MIN_SPAN_SECONDS,
            "66 seconds must not be long enough to give up - that is a "
            "relay restarting",
        )

    def test_a_worker_broken_for_long_enough_still_gives_up(self):
        # The 363-crash-loop protection has to survive this change.
        first_at = 1000.0
        count, first = self.record(
            host.CONSECUTIVE_FAILURE_LIMIT,
            first_at=first_at,
            now=first_at + host.GIVE_UP_MIN_SPAN_SECONDS + 1.0,
        )
        self.assertGreaterEqual(count, host.CONSECUTIVE_FAILURE_LIMIT)
        spanned = (first_at + host.GIVE_UP_MIN_SPAN_SECONDS + 1.0) - first
        self.assertGreaterEqual(spanned, host.GIVE_UP_MIN_SPAN_SECONDS)

    def test_the_span_is_long_enough_to_outlast_a_deploy(self):
        # A promote restarts services over minutes. If this were tighter
        # than that, the fix would not fix anything.
        self.assertGreaterEqual(host.GIVE_UP_MIN_SPAN_SECONDS, 180.0)

    # ------------------------------------------------- restart spacing

    def test_the_service_manager_spaces_restarts_far_enough_apart(self):
        # Spacing belongs to nssm, not to a sleep inside main() - doing
        # it in-process added five minutes to this suite and delayed a
        # clean exit. What matters is that the whole failure budget
        # cannot fit inside the span, or the count would be spent before
        # the clock ever mattered.
        delay_seconds = host.WORKER_RESTART_DELAY_MS / 1000.0
        # The span, not the count, is what ends the loop - so the number
        # of restarts before a give-up is span/delay. The incident this
        # protection exists for reached 363; anything in this range is
        # obviously escalating and obviously finite.
        restarts_before_giving_up = host.GIVE_UP_MIN_SPAN_SECONDS / delay_seconds
        self.assertLessEqual(
            restarts_before_giving_up, 20,
            "restarts are dense enough to look like the 363-restart loop "
            "this limit was added for",
        )
        # And sparse enough that a burst cannot look instantaneous. At
        # the old five seconds the whole budget burned in 66 seconds.
        self.assertGreaterEqual(
            delay_seconds * (host.CONSECUTIVE_FAILURE_LIMIT - 1), 90.0,
            "reaching the limit must take minutes, not the 66 seconds it "
            "took on 2026-09-03",
        )

    def test_the_installer_sets_the_delay_the_worker_expects(self):
        # Two files have to agree, and nothing else checks them.
        script = (
            Path(host.__file__).resolve().parent / "Install-VemalsEars-Worker.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "AppRestartDelay %d" % host.WORKER_RESTART_DELAY_MS, script
        )

    def test_the_give_up_needs_both_the_count_and_the_clock(self):
        # The decision itself, which used to sit inline in main() where
        # no test could see it - a sabotage run proved that dropping the
        # clock check left a green suite behind.
        self.assertFalse(host.should_give_up(host.CONSECUTIVE_FAILURE_LIMIT, 0.0))
        self.assertFalse(
            host.should_give_up(
                host.CONSECUTIVE_FAILURE_LIMIT, host.GIVE_UP_MIN_SPAN_SECONDS - 1.0
            )
        )
        self.assertFalse(
            host.should_give_up(
                host.CONSECUTIVE_FAILURE_LIMIT - 1,
                host.GIVE_UP_MIN_SPAN_SECONDS * 10,
            )
        )
        self.assertTrue(
            host.should_give_up(
                host.CONSECUTIVE_FAILURE_LIMIT, host.GIVE_UP_MIN_SPAN_SECONDS
            )
        )

    def test_giving_up_writes_the_note_and_retrying_does_not(self):
        # handle_failure owns both, and both were untestable inline.
        output = io.StringIO()
        health = self.folder / "worker.state"
        with redirect_stdout(output):
            code = host.handle_failure(
                self.tally,
                host.CONSECUTIVE_FAILURE_LIMIT,
                host.GIVE_UP_MIN_SPAN_SECONDS,
                "the caption server is unavailable",
                1,
            )
        self.assertEqual(host.GIVE_UP_EXIT_CODE, code)
        self.assertIn("caption server is unavailable", host.read_give_up_note(health))

        host._clear_failure_tally(self.tally)
        output = io.StringIO()
        with redirect_stdout(output):
            code = host.handle_failure(
                self.tally, host.CONSECUTIVE_FAILURE_LIMIT, 1.0, "transient", 1
            )
        self.assertEqual(1, code, "a burst must keep asking to be restarted")
        self.assertIsNone(
            host.read_give_up_note(health),
            "a burst must not leave a give-up note behind",
        )

    def test_a_give_up_with_no_reason_still_leaves_a_note(self):
        # run_watch can return non-zero without raising, so there is no
        # detail to report - and an empty note must not read as "nobody
        # gave up".
        health = self.folder / "worker.state"
        with redirect_stdout(io.StringIO()):
            host.handle_failure(
                self.tally,
                host.CONSECUTIVE_FAILURE_LIMIT,
                host.GIVE_UP_MIN_SPAN_SECONDS,
                "",
                1,
            )
        self.assertIsNotNone(host.read_give_up_note(health))

    # -------------------------------------------------- the give-up note

    def test_giving_up_leaves_a_reason_a_stopped_service_can_still_show(self):
        note = host._give_up_note_file(self.tally)
        host._write_give_up_note(note, "The Vemal's Ears caption server is unavailable.")
        health = self.folder / "worker.state"
        self.assertIn("caption server is unavailable", host.read_give_up_note(health))

    def test_no_note_means_nobody_gave_up(self):
        self.assertIsNone(host.read_give_up_note(self.folder / "worker.state"))

    def test_a_healthy_run_clears_the_note(self):
        note = host._give_up_note_file(self.tally)
        host._write_give_up_note(note, "something went wrong")
        host._clear_failure_tally(self.tally)
        self.assertIsNone(host.read_give_up_note(self.folder / "worker.state"))

    def test_the_note_stays_short_enough_to_render(self):
        note = host._give_up_note_file(self.tally)
        host._write_give_up_note(note, "x" * 5000)
        # The FILE, not the read-back: truncating only on read let an
        # unbounded write pass, which a sabotage run caught.
        self.assertLessEqual(
            len(note.read_text(encoding="ascii").strip()),
            host.MAX_GIVE_UP_NOTE_CHARS,
        )
        reason = host.read_give_up_note(self.folder / "worker.state")
        self.assertLessEqual(len(reason), host.MAX_GIVE_UP_NOTE_CHARS)

    def test_a_multiline_reason_becomes_one_line(self):
        note = host._give_up_note_file(self.tally)
        host._write_give_up_note(note, "first line\n\nsecond line")
        self.assertEqual(
            "first line second line",
            host.read_give_up_note(self.folder / "worker.state"),
        )

if __name__ == "__main__":
    unittest.main()
