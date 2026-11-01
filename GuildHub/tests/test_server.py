import asyncio
import dataclasses
import base64
import hashlib
import io
import json
import os
import re
import tempfile
import shutil
import signal
import socket
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed, InvalidStatus

import GuildHub.server as server
from GuildHub.client import GuildCaptionClient
from GuildHub.credentials import DeviceCredentials
from GuildHub.protocol import (
    CAPTION_SUBPROTOCOLS,
    QUICK_CHAT_PROFILE_PATH,
    SUBPROTOCOL,
    SUBPROTOCOL_V2,
    AudienceUpdate,
    decode_message,
    encode_message,
)
from GuildHub.publisher import RemoteCaptionSink
from GuildHub.server import (
    WEBSOCKET_LOGGER,
    CaptionBroker,
    GuildHubServer,
    ServerConfig,
    build_parser,
    main,
    run_server,
    understands_captions_stopped,
)
from GuildHub.store import CredentialStore, DeviceRecord
from VoiceCaptions.events import CaptionEvent, ClearEvent, SessionEvent, event_to_json


PEPPER = "test-pepper-is-at-least-thirty-two-characters-long"
PUBLISHER_TOKEN = "publisher_" + ("p" * 40)
PUBLIC_SERVER_URL = "wss://ears.example.org"
CLIENT_DOWNLOAD_URL = (
    "https://ears.example.org/downloads/Vemals-Ears-Setup.exe"
    "?channel=pilot&source=guild"
)
CAPTION = CaptionEvent(
    speaker_id="discord-user-1",
    speaker="Mira",
    character="Miralyn",
    role="HEALER",
    text="Move left.",
    final=True,
    sequence=7,
    timestamp=12.5,
)
_PROFILE_READ = object()


class BrokerTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_id_is_random_safe_and_process_local(self):
        first = CaptionBroker()
        second = CaptionBroker()
        self.assertRegex(first.stream_id, r"^[A-Za-z0-9_-]+$")
        self.assertNotEqual(first.stream_id, second.stream_id)

    async def test_latest_session_tracks_only_the_most_recent_session_event(self):
        broker = CaptionBroker(stream_id="ghs_test_stream")
        active = SessionEvent(state="active")
        paused = SessionEvent(state="paused", message="Raid paused")

        self.assertIsNone(broker.latest_session)
        broker.publish(active)
        self.assertEqual(active, broker.latest_session)
        broker.publish(CAPTION)
        self.assertEqual(active, broker.latest_session)
        broker.publish(ClearEvent())
        self.assertEqual(active, broker.latest_session)
        broker.publish(paused)
        self.assertEqual(paused, broker.latest_session)

    async def test_stopped_session_erases_caption_replay(self):
        broker = CaptionBroker(stream_id="ghs_test_stream")
        stopped = SessionEvent(state="stopped")
        sensitive = replace(CAPTION, text="old session private text")

        async def close_slow():
            return None

        live, _baseline, _replay = broker.subscribe("", 0, close_slow)
        broker.publish(SessionEvent(state="active"))
        broker.publish(sensitive)

        broker.publish(stopped)

        delivered = [decode_message(await live.queue.get()) for _index in range(3)]
        self.assertEqual("stopped", delivered[-1]["event"]["state"])
        broker.unsubscribe(live)

        subscriber, baseline, replay = broker.subscribe(
            broker.stream_id, 0, close_slow
        )
        try:
            self.assertEqual(broker.cursor - 1, baseline)
            self.assertEqual(1, len(replay))
            self.assertEqual(
                "stopped", decode_message(replay[0])["event"]["state"]
            )
            self.assertNotIn(sensitive.text, "".join(replay))
            self.assertEqual(stopped, broker.latest_session)
        finally:
            broker.unsubscribe(subscriber)

    async def test_new_session_replay_starts_after_the_stopped_boundary(self):
        broker = CaptionBroker(stream_id="ghs_test_stream")
        broker.publish(SessionEvent(state="active"))
        broker.publish(replace(CAPTION, text="old session private text"))
        broker.publish(SessionEvent(state="stopped"))
        broker.publish(SessionEvent(state="active"))
        broker.publish(replace(CAPTION, text="new session text"))

        async def close_slow():
            return None

        subscriber, baseline, replay = broker.subscribe(
            broker.stream_id, 0, close_slow
        )
        try:
            decoded = [decode_message(frame) for frame in replay]
            self.assertEqual(3, baseline)
            self.assertEqual([4, 5], [frame["cursor"] for frame in decoded])
            self.assertEqual("active", decoded[0]["event"]["state"])
            self.assertNotIn("old session private text", "".join(replay))
            self.assertIn("new session text", "".join(replay))
        finally:
            broker.unsubscribe(subscriber)

    async def test_delayed_caption_after_stop_becomes_a_cursor_advance(self):
        broker = CaptionBroker(stream_id="ghs_test_stream")
        broker.publish(SessionEvent(state="active"))
        broker.publish(replace(CAPTION, text="old session private text"))
        broker.publish(SessionEvent(state="stopped"))
        cursor = broker.publish(replace(CAPTION, text="late private text"))

        async def close_slow():
            return None

        subscriber, baseline, replay = broker.subscribe(
            broker.stream_id, 0, close_slow
        )
        try:
            decoded = [decode_message(frame) for frame in replay]
            self.assertEqual(2, baseline)
            self.assertEqual(cursor, 4)
            self.assertEqual([3, 4], [frame["cursor"] for frame in decoded])
            self.assertEqual("stopped", decoded[0]["event"]["state"])
            self.assertEqual("cursor_advance", decoded[1]["type"])
            self.assertNotIn("private text", "".join(replay))
        finally:
            broker.unsubscribe(subscriber)

    async def test_session_independent_text_remains_live_after_stop(self):
        broker = CaptionBroker(stream_id="ghs_test_stream")
        broker.publish(SessionEvent(state="stopped"))
        typed = replace(CAPTION, text="typed while captions are stopped")
        broker.publish(typed, session_bound=False)

        async def close_slow():
            return None

        subscriber, _baseline, replay = broker.subscribe(
            broker.stream_id, 0, close_slow
        )
        try:
            self.assertIn(typed.text, "".join(replay))
        finally:
            broker.unsubscribe(subscriber)

    async def test_pause_rejects_late_speech_without_erasing_session_replay(self):
        broker = CaptionBroker(stream_id="ghs_test_stream", replay_limit=16)
        broker.publish(SessionEvent(state="active"))
        broker.publish(replace(CAPTION, text="before pause"))
        broker.publish(SessionEvent(state="paused"))
        delayed_cursor = broker.publish(replace(CAPTION, text="late during pause"))
        broker.publish(SessionEvent(state="active"))
        broker.publish(replace(CAPTION, text="after resume"))

        async def close_slow():
            return None

        subscriber, _baseline, replay = broker.subscribe(
            broker.stream_id, 0, close_slow
        )
        try:
            decoded = [decode_message(frame) for frame in replay]
            rendered = "".join(replay)
            self.assertIn("before pause", rendered)
            self.assertIn("after resume", rendered)
            self.assertNotIn("late during pause", rendered)
            self.assertIn(
                {
                    "type": "cursor_advance",
                    "stream_id": broker.stream_id,
                    "cursor": delayed_cursor,
                },
                decoded,
            )
        finally:
            broker.unsubscribe(subscriber)

    async def test_reset_requires_a_new_session_before_caption_text(self):
        broker = CaptionBroker(stream_id="ghs_test_stream")
        broker.publish(SessionEvent(state="active"))
        broker.publish(replace(CAPTION, text="old session private text"))
        broker.reset_session()
        cursor = broker.publish(replace(CAPTION, text="late private text"))

        async def close_slow():
            return None

        subscriber, baseline, replay = broker.subscribe(
            broker.stream_id, 0, close_slow
        )
        try:
            self.assertEqual(cursor - 1, baseline)
            self.assertEqual("cursor_advance", decode_message(replay[0])["type"])
            self.assertNotIn("private text", "".join(replay))
        finally:
            broker.unsubscribe(subscriber)

        broker.publish(SessionEvent(state="active"))
        broker.publish(replace(CAPTION, text="new session text"))
        subscriber, baseline, replay = broker.subscribe(
            broker.stream_id, 0, close_slow
        )
        try:
            self.assertEqual(cursor, baseline)
            self.assertIn("new session text", "".join(replay))
            self.assertNotIn("late private text", "".join(replay))
        finally:
            broker.unsubscribe(subscriber)

    async def test_ready_and_error_do_not_reopen_a_stopped_session(self):
        for state in ("ready", "error"):
            with self.subTest(state=state):
                broker = CaptionBroker(stream_id="ghs_test_stream")
                broker.publish(SessionEvent(state="active"))
                broker.publish(replace(CAPTION, text="old private text"))
                broker.publish(SessionEvent(state="stopped"))
                broker.publish(SessionEvent(state=state))
                broker.publish(replace(CAPTION, text="late private text"))

                async def close_slow():
                    return None

                subscriber, _baseline, replay = broker.subscribe(
                    broker.stream_id, 0, close_slow
                )
                try:
                    decoded = [decode_message(frame) for frame in replay]
                    self.assertEqual(state, decoded[0]["event"]["state"])
                    self.assertEqual("cursor_advance", decoded[1]["type"])
                    self.assertNotIn("private text", "".join(replay))
                finally:
                    broker.unsubscribe(subscriber)

    async def test_matching_stream_replay_has_contiguous_baseline(self):
        broker = CaptionBroker(
            replay_limit=3, queue_limit=2, stream_id="ghs_test_stream"
        )
        broker.publish(CAPTION, session_bound=False)
        broker.publish(CAPTION, session_bound=False)

        async def close_slow():
            return None

        subscriber, baseline, replay = broker.subscribe(
            broker.stream_id, 0, close_slow
        )
        self.assertEqual(0, baseline)
        self.assertEqual(2, len(replay))
        broker.unsubscribe(subscriber)

        subscriber, baseline, replay = broker.subscribe(
            broker.stream_id, broker.cursor, close_slow
        )
        self.assertEqual(broker.cursor, baseline)
        self.assertEqual([], replay)
        broker.unsubscribe(subscriber)

    async def test_truncated_replay_advances_baseline_to_oldest_retained(self):
        broker = CaptionBroker(
            replay_limit=2, queue_limit=2, stream_id="ghs_test_stream"
        )
        broker.publish(CAPTION, session_bound=False)
        broker.publish(CAPTION, session_bound=False)
        broker.publish(CAPTION, session_bound=False)

        async def close_slow():
            return None

        subscriber, baseline, replay = broker.subscribe(
            broker.stream_id, 0, close_slow
        )
        self.assertEqual(1, baseline)
        self.assertEqual(2, len(replay))
        broker.unsubscribe(subscriber)

    async def test_unknown_stream_never_receives_pre_auth_replay(self):
        broker = CaptionBroker(stream_id="ghs_current")
        broker.publish(CAPTION, session_bound=False)

        async def close_slow():
            return None

        subscriber, baseline, replay = broker.subscribe("ghs_old", 0, close_slow)
        self.assertEqual(1, baseline)
        self.assertEqual([], replay)
        broker.unsubscribe(subscriber)

        subscriber, baseline, replay = broker.subscribe("", 0, close_slow)
        self.assertEqual(1, baseline)
        self.assertEqual([], replay)
        broker.unsubscribe(subscriber)

    async def test_slow_subscriber_is_removed_and_closed(self):
        broker = CaptionBroker(queue_limit=1, stream_id="ghs_test_stream")
        closed = asyncio.Event()

        async def close_slow():
            closed.set()

        subscriber, _baseline, _replay = broker.subscribe("", 0, close_slow)
        broker.publish(CAPTION, session_bound=False)
        broker.publish(CAPTION, session_bound=False)
        await asyncio.wait_for(closed.wait(), timeout=1)
        self.assertEqual(0, broker.subscriber_count)
        self.assertFalse(subscriber.active)
        self.assertEqual(1, subscriber.queue.qsize())

    async def test_unsubscribe_revokes_a_retained_subscriber_immediately(self):
        broker = CaptionBroker()

        async def close_slow():
            return None

        subscriber, _baseline, _replay = broker.subscribe("", 0, close_slow)
        self.assertTrue(subscriber.active)
        broker.unsubscribe(subscriber)
        self.assertFalse(subscriber.active)
        self.assertEqual(0, broker.subscriber_count)

    async def test_private_cursor_is_advanced_live_and_in_replay(self):
        broker = CaptionBroker(
            replay_limit=4, queue_limit=4, stream_id="ghs_private"
        )

        async def close_slow():
            return None

        viewer = "b" * 24
        subscriber, _baseline, _replay = broker.subscribe(
            "", 0, close_slow, viewer_token=viewer
        )
        broker.publish(
            CAPTION, frozenset({"a" * 24}), session_bound=False
        )
        broker.publish(CAPTION, frozenset({viewer}), session_bound=False)
        first = decode_message(await subscriber.queue.get())
        second = decode_message(await subscriber.queue.get())
        self.assertEqual(
            {"type": "cursor_advance", "stream_id": "ghs_private", "cursor": 1},
            first,
        )
        self.assertEqual("caption_event", second["type"])
        self.assertEqual(2, second["cursor"])
        broker.unsubscribe(subscriber)

        subscriber, baseline, replay = broker.subscribe(
            broker.stream_id, 0, close_slow, viewer_token=viewer
        )
        self.assertEqual(0, baseline)
        self.assertEqual(
            ["cursor_advance", "caption_event"],
            [decode_message(frame)["type"] for frame in replay],
        )
        broker.unsubscribe(subscriber)

    async def test_replay_uses_each_subscriber_caption_version(self):
        broker = CaptionBroker(stream_id="ghs_versions")
        broker.publish(
            replace(CAPTION, speech_style="whisper"), session_bound=False
        )

        async def close_slow():
            return None

        old_subscriber, _baseline, old_replay = broker.subscribe(
            broker.stream_id,
            0,
            close_slow,
        )
        new_subscriber, _baseline, new_replay = broker.subscribe(
            broker.stream_id,
            0,
            close_slow,
            subprotocol=SUBPROTOCOL_V2,
        )
        try:
            old_event = decode_message(old_replay[0])["event"]
            new_event = decode_message(new_replay[0])["event"]
        finally:
            broker.unsubscribe(old_subscriber)
            broker.unsubscribe(new_subscriber)

        self.assertNotIn("speech_style", old_event)
        self.assertEqual("whisper", new_event["speech_style"])


class SubprotocolNegotiationTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_peer_falls_back_to_a_v1_only_server(self):
        async def hold_open(connection):
            await connection.wait_closed()

        old_server = await serve(
            hold_open,
            "127.0.0.1",
            0,
            subprotocols=[SUBPROTOCOL],
        )
        port = old_server.sockets[0].getsockname()[1]
        connection = None
        try:
            connection = await connect(
                "ws://127.0.0.1:" + str(port),
                subprotocols=list(CAPTION_SUBPROTOCOLS),
            )
            self.assertEqual(SUBPROTOCOL, connection.subprotocol)
        finally:
            if connection is not None:
                await connection.close()
            old_server.close()
            await old_server.wait_closed()


class ServerConfigTests(unittest.TestCase):
    def config(self, **changes):
        values = {
            "publisher_token": PUBLISHER_TOKEN,
            "public_server_url": PUBLIC_SERVER_URL,
            "client_download_url": CLIENT_DOWNLOAD_URL,
        }
        values.update(changes)
        return ServerConfig(**values)

    def test_public_urls_are_canonical_and_download_query_is_safe(self):
        config = self.config(
            public_server_url="WSS://Ears.Example.Org:443/",
            client_download_url=(
                "https://EARs.example.org:443/downloads/setup.exe"
                "?label=Raid Night&note=\"ready\""
            ),
        )
        self.assertEqual("wss://ears.example.org", config.public_server_url)
        self.assertEqual(
            "https://ears.example.org/downloads/setup.exe"
            "?label=Raid%20Night&note=%22ready%22",
            config.client_download_url,
        )

    def test_public_urls_reject_plaintext_remote_and_active_content(self):
        with self.assertRaises(ValueError):
            self.config(public_server_url="ws://ears.example.org")
        with self.assertRaises(ValueError):
            self.config(client_download_url="javascript:alert(1)")

    def test_security_limits_are_bounded(self):
        with self.assertRaises(ValueError):
            self.config(connection_limit=0)
        with self.assertRaises(ValueError):
            self.config(revalidation_seconds=61)
        with self.assertRaises(ValueError):
            self.config(quick_chat_response_timeout=0)
        with self.assertRaises(ValueError):
            self.config(trust_proxy="yes")
        with self.assertRaises(ValueError):
            self.config(audience_lease_seconds=0)

    def test_voice_gate_notice_states_the_fixed_policy(self):
        notice = server.voice_gate_notice()
        self.assertIn("PRIVACY", notice)
        self.assertIn("active Discord voice channel", notice)
        self.assertIn("not a setting", notice)
        self.assertTrue(notice.isascii())

    def test_voice_membership_is_not_a_setting(self):
        # Shino, 2026-09-06: a privacy concern that cannot be flipped
        # lightly - so nothing can flip it. No config field, no flag, and
        # the old environment variable changes nothing.
        with self.assertRaises(TypeError):
            self.config(require_voice_membership=False)
        self.assertFalse(hasattr(self.config(), "require_voice_membership"))
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(["serve", "--require-voice-membership"])
        with patch.dict(os.environ, {"VEMALS_EARS_REQUIRE_VOICE_MEMBERSHIP": "0"}):
            arguments = build_parser().parse_args(["serve"])
        self.assertFalse(hasattr(arguments, "require_voice_membership"))


class _ExpirySignal(asyncio.Event):
    """An expiry a test drives, that also counts the waits it ended.

    The count is the only thing standing between this and a helper that
    silently stops being wired in. Every test using it would still reach
    its delivery_pending assertion by waiting out the real deadline - just
    slowly - so a pass proves nothing about whether the expiry was DRIVEN.
    """

    def __init__(self):
        super().__init__()
        self.expired = 0


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.store = CredentialStore(
            Path(self.temporary_directory.name) / "guild-hub.sqlite3",
            PEPPER,
        )
        self.store.initialize()
        self.application = GuildHubServer(
            self.store,
            ServerConfig(
                guild_id="guild-one",
                guild_name="Vemal's Ears",
                publisher_token=PUBLISHER_TOKEN,
                public_server_url=PUBLIC_SERVER_URL,
                client_download_url=CLIENT_DOWNLOAD_URL,
                # Normal handshakes must not race loaded-runner scheduling.
                # Only the explicit first-message timeout test shortens this.
                auth_timeout=5.0,
                replay_limit=8,
                client_queue_limit=4,
                connection_limit=8,
                revalidation_seconds=0.05,
                # Production-sized so normal handshakes do not race loaded
                # runner scheduling.
                quick_chat_response_timeout=5.0,
            ),
        )
        self.server, self.base_url = await self.start_application(self.application)
        self.health_url = self.base_url.replace("ws://", "http://") + "/healthz"
        # Who may read captions is fixed, not configured (the PRIVACY note in
        # server.py): a viewer is admitted only while its viewer token is in
        # a fresh voice audience. pair() binds a token and refreshes the
        # audience, so a paired viewer in these tests is inside the channel
        # unless a test says otherwise.
        self.viewer_tokens: list[str] = []
        self.audience_revision = 0

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        self.temporary_directory.cleanup()

    async def start_application(self, application):
        server = await serve(
            application.handle,
            "127.0.0.1",
            0,
            subprotocols=list(CAPTION_SUBPROTOCOLS),
            process_request=application.process_request,
            compression=None,
            max_size=16_384,
            logger=WEBSOCKET_LOGGER,
        )
        port = server.sockets[0].getsockname()[1]
        return server, "ws://127.0.0.1:" + str(port)

    async def wait_until(self, predicate, timeout=15.0):
        """Poll until the predicate holds, or fail.

        The deadline is generous on purpose. This returns the moment the
        predicate is true, so a large budget costs nothing when the
        machine is quick - and the CI runner is not: 8 workers against 40
        locally, and whole-suite wall clock of 123s against 22s. A
        one-second budget failed there on a PR that touched neither this
        file nor the server.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.01)
        if predicate():
            # The loop can fall through mid-sleep and fail on a state that
            # became true during its own final ~16ms nap. One last look
            # before declaring a timeout costs nothing and cannot lie.
            return
        self.fail("timed out waiting for server state")

    def expire_quick_chat_responses(self):
        """Make the member response deadline a signal this test controls.

        Returns an asyncio.Event: set() expires the in-flight member wait, so
        a test wanting delivery_pending asks
        for it rather than waiting out a real deadline; clear() lets the
        next request be answered normally, which every test that continues
        past its pending assertion must do.

        Waiting a deadline out is the half that cannot be made safe by
        widening it. It has to be short enough to keep the suite quick and
        long enough that a loaded runner never crosses it by accident, and
        no value is both.
        """
        expire = _ExpirySignal()

        async def expire_on_signal(entry):
            waiter = asyncio.ensure_future(expire.wait())
            result = asyncio.ensure_future(asyncio.shield(entry.task))
            try:
                await asyncio.wait(
                    {waiter, result}, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                waiter.cancel()
            if result.done() and not result.cancelled():
                return result.result()
            result.cancel()
            # An answer landing in the expiry's own iteration is an answer.
            # The shared task outlives this wait either way, so cancelling
            # the shield above never costs the member their delivery.
            if entry.task.done():
                return entry.task.result()
            expire.expired += 1
            raise TimeoutError

        self.application._await_quick_chat_response = expire_on_signal
        return expire

    async def pair_code(self, code, base_url=None):
        async with connect(
            (base_url or self.base_url) + "/v1/pair",
            subprotocols=[SUBPROTOCOL],
        ) as websocket:
            await websocket.send(
                encode_message(
                    {
                        "type": "pair",
                        "enrollment_code": code,
                        "device_name": "Raid desktop",
                        "client_version": "0.1.0",
                    }
                )
            )
            return decode_message(await websocket.recv())

    async def pair(self):
        enrollment = self.store.create_enrollment(300)
        paired = await self.pair_code(enrollment.code)
        if "device_id" in paired:
            await ServerTests.admit_viewer(self, paired)
        return paired, enrollment.code

    async def admit_viewer(self, paired, application=None):
        """Bind a viewer token to the device and put it in the voice audience."""
        tokens = self.__dict__.setdefault("viewer_tokens", [])
        token = format(len(tokens) + 1, "024x")
        self.assertTrue(self.store.bind_viewer_token(paired["device_id"], token))
        tokens.append(token)
        await ServerTests.refresh_audience(self, application)
        return token

    async def refresh_audience(self, application=None, viewer_tokens=None):
        """Apply a fresh audience revision on the relay under test."""
        self.audience_revision = getattr(self, "audience_revision", 0) + 1
        tokens = (
            self.__dict__.setdefault("viewer_tokens", [])
            if viewer_tokens is None
            else list(viewer_tokens)
        )
        await (application or self.application)._apply_audience_update(
            AudienceUpdate(1, self.audience_revision, tuple(tokens))
        )

    def audience_fields(self):
        """The routing every caption publish must carry."""
        return {
            "audience_generation": 1,
            "audience_revision": getattr(self, "audience_revision", 0),
        }

    async def open_captions(
        self,
        paired,
        *,
        last_stream_id="",
        last_cursor=0,
        base_url=None,
        subprotocols=None,
        client_version=None,
    ):
        offered = [SUBPROTOCOL] if subprotocols is None else list(subprotocols)
        websocket = await connect(
            (base_url or self.base_url) + "/v1/captions",
            subprotocols=offered,
        )
        request = {
            "type": "authenticate",
            "device_id": paired["device_id"],
            "device_token": paired["device_token"],
            "last_stream_id": last_stream_id,
            "last_cursor": last_cursor,
        }
        # Omitted by default, so every existing case here is an OLD client
        # and keeps proving the wording those clients still receive.
        if client_version is not None:
            request["client_version"] = client_version
        await websocket.send(encode_message(request))
        response = decode_message(await websocket.recv())
        return websocket, response

    async def open_publisher(self, base_url=None, *, subprotocols=None):
        offered = [SUBPROTOCOL] if subprotocols is None else list(subprotocols)
        websocket = await connect(
            (base_url or self.base_url) + "/v1/publish",
            subprotocols=offered,
        )
        await websocket.send(
            encode_message(
                {"type": "authenticate", "publisher_token": PUBLISHER_TOKEN}
            )
        )
        response = decode_message(await websocket.recv())
        # A new publisher owns the audience lease and starts with an empty
        # roster, which closes every viewer. Production's worker sends the
        # roster straight after authenticating; this fixture does the same
        # for the relay under test, so viewers opened afterwards are inside
        # the channel. Viewers opened BEFORE the publisher stay closed - that
        # is the policy, and tests open the publisher first.
        if base_url is None and response.get("type") == "authenticated":
            await ServerTests.refresh_audience(self)
        return websocket, response

    async def send_publisher_event(self, publisher, event):
        payload = {
            "type": "publish",
            "event": json.loads(event_to_json(event)),
        }
        if isinstance(event, CaptionEvent):
            payload.update(self.audience_fields())
        await publisher.send(encode_message(payload))
        return decode_message(await publisher.recv())

    async def open_quick_chat_worker(self, base_url=None, token=PUBLISHER_TOKEN):
        websocket = await connect(
            (base_url or self.base_url) + "/v1/quick-chat-worker",
            subprotocols=[SUBPROTOCOL],
        )
        await websocket.send(
            encode_message(
                {"type": "authenticate", "publisher_token": token}
            )
        )
        response = decode_message(await websocket.recv())
        return websocket, response

    def assign_quick_chat_profile(self, paired, name="Miralyn", wow_class="mage"):
        self.assertTrue(
            self.store.set_quick_chat_profile(
                paired["device_id"], name, wow_class
            )
        )

    async def send_quick_chat(
        self,
        paired,
        *,
        request_id="chat_001",
        text="Use the gateway now.",
        base_url=None,
    ):
        async with connect(
            (base_url or self.base_url) + "/v1/quick-chat",
            subprotocols=[SUBPROTOCOL],
        ) as websocket:
            await websocket.send(
                encode_message(
                    {
                        "type": "quick_chat",
                        "request_id": request_id,
                        "device_id": paired["device_id"],
                        "device_token": paired["device_token"],
                        "text": text,
                    }
                )
            )
            return decode_message(await websocket.recv())

    async def quick_chat_profile(
        self,
        paired,
        *,
        name=_PROFILE_READ,
        device_id=None,
        device_token=None,
        base_url=None,
    ):
        async with connect(
            (base_url or self.base_url) + QUICK_CHAT_PROFILE_PATH,
            subprotocols=[SUBPROTOCOL],
        ) as websocket:
            request = {
                "type": "quick_chat_profile",
                "device_id": device_id or paired["device_id"],
                "device_token": device_token or paired["device_token"],
            }
            if name is not _PROFILE_READ:
                request["name"] = name
            await websocket.send(encode_message(request))
            return decode_message(await websocket.recv())

    async def fetch(self, path, headers=None):
        url = self.base_url.replace("ws://", "http://", 1) + path

        def request():
            outgoing = urllib.request.Request(url, headers=headers or {})
            try:
                with urllib.request.urlopen(outgoing, timeout=2) as response:
                    return response.status, dict(response.headers), response.read()
            except urllib.error.HTTPError as error:
                try:
                    return error.code, dict(error.headers), error.read()
                finally:
                    error.close()

        return await asyncio.to_thread(request)

    async def test_pairing_returns_one_device_credential_and_code_cannot_replay(self):
        paired, code = await self.pair()
        self.assertEqual("paired", paired["type"])
        self.assertTrue(paired["device_token"].startswith("ghd_"))
        self.assertEqual("guild-one", paired["guild_id"])
        self.assertEqual("Vemal's Ears", paired["guild_name"])

        failure = await self.pair_code(code)
        self.assertEqual("enrollment_failed", failure["code"])
        self.assertNotIn(code, json.dumps(failure))

    async def test_authenticated_publisher_reaches_device_and_gets_ack(self):
        paired, _code = await self.pair()
        # The publisher owns the audience lease: it connects first, then the
        # viewer is inside the channel it announces.
        publisher, publisher_auth = await self.open_publisher()
        self.assertEqual(
            {"type": "published", "cursor": 1},
            await self.send_publisher_event(
                publisher, SessionEvent(state="active")
            ),
        )
        captions, caption_auth = await self.open_captions(paired)
        try:
            self.assertEqual("authenticated", caption_auth["type"])
            self.assertEqual(
                self.application.broker.stream_id, caption_auth["stream_id"]
            )
            self.assertEqual(1, caption_auth["cursor"])
            self.assertEqual("authenticated", publisher_auth["type"])
            await publisher.send(
                encode_message(
                    {
                        "type": "publish",
                        **self.audience_fields(),
                        "event": json.loads(event_to_json(CAPTION)),
                    }
                )
            )
            acknowledgement = decode_message(await publisher.recv())
            envelope = decode_message(await captions.recv())
        finally:
            await captions.close()
            await publisher.close()

        self.assertEqual({"type": "published", "cursor": 2}, acknowledgement)
        self.assertEqual("caption_event", envelope["type"])
        self.assertEqual(self.application.broker.stream_id, envelope["stream_id"])
        self.assertEqual(2, envelope["cursor"])
        self.assertEqual("Move left.", envelope["event"]["text"])

    async def test_v2_style_is_downconverted_only_for_v1_viewers(self):
        old_paired, _code = await self.pair()
        new_paired, _code = await self.pair()
        publisher, publisher_auth = await self.open_publisher(
            subprotocols=CAPTION_SUBPROTOCOLS,
        )
        self.assertEqual(
            {"type": "published", "cursor": 1},
            await self.send_publisher_event(
                publisher, SessionEvent(state="active")
            ),
        )
        old_captions, _auth = await self.open_captions(old_paired)
        new_captions, _auth = await self.open_captions(
            new_paired,
            subprotocols=CAPTION_SUBPROTOCOLS,
        )
        try:
            self.assertEqual(SUBPROTOCOL, old_captions.subprotocol)
            self.assertEqual(SUBPROTOCOL_V2, new_captions.subprotocol)
            self.assertEqual(SUBPROTOCOL_V2, publisher.subprotocol)
            self.assertEqual(
                {"type": "authenticated", "cursor": 0},
                publisher_auth,
            )
            styled = replace(CAPTION, speech_style="shout")
            await publisher.send(
                encode_message(
                    {
                        "type": "publish",
                        **self.audience_fields(),
                        "event": json.loads(event_to_json(styled)),
                    }
                )
            )
            self.assertEqual(
                {"type": "published", "cursor": 2},
                decode_message(await publisher.recv()),
            )
            old_envelope = decode_message(await old_captions.recv())
            new_envelope = decode_message(await new_captions.recv())
        finally:
            await old_captions.close()
            await new_captions.close()
            await publisher.close()

        self.assertNotIn("speech_style", old_envelope["event"])
        self.assertEqual("shout", new_envelope["event"]["speech_style"])
        self.assertEqual(old_envelope["cursor"], new_envelope["cursor"])

    async def test_quick_chat_fans_out_and_never_reaches_compatibility_worker(self):
        paired, _code = await self.pair()
        second_paired, _code = await self.pair()
        self.assign_quick_chat_profile(paired, "Admin Mira", "mage")
        publisher, _auth = await self.open_publisher()
        first, first_auth = await self.open_captions(paired)
        second, _second_auth = await self.open_captions(second_paired)
        worker, worker_auth = await self.open_quick_chat_worker()
        retry = None
        try:
            self.assertEqual({"type": "authenticated"}, worker_auth)
            acknowledgement = await self.send_quick_chat(
                paired,
                request_id="chat_fanout_001",
                text="Use the gateway now.",
            )
            # The overlays and the member are served directly by the relay.
            first_envelope = decode_message(
                await asyncio.wait_for(first.recv(), timeout=5)
            )
            second_envelope = decode_message(
                await asyncio.wait_for(second.recv(), timeout=5)
            )
            self.assertEqual(first_envelope, second_envelope)

            self.assertEqual(1, self.application.active_quick_chat_worker_count)
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(worker.recv(), timeout=0.03)
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(first.recv(), timeout=0.03)
            self.assertEqual(1, self.application.broker.cursor)
            self.assertEqual(
                {
                    "type": "quick_chat_sent",
                    "request_id": "chat_fanout_001",
                    "cursor": 1,
                },
                acknowledgement,
            )
            self.assertEqual("Admin Mira [typed]", first_envelope["event"]["speaker"])
            self.assertEqual("mage", first_envelope["event"]["wow_class"])
            self.assertEqual("Use the gateway now.", first_envelope["event"]["text"])
            self.assertEqual("UNKNOWN", first_envelope["event"]["role"])
            self.assertTrue(first_envelope["event"]["final"])
            self.assertRegex(
                first_envelope["event"]["speaker_id"],
                r"^quick-chat-[0-9a-f]{32}$",
            )

            await second.close()
            retry, retry_auth = await self.open_captions(
                second_paired,
                last_stream_id=first_auth["stream_id"],
                last_cursor=0,
            )
            self.assertEqual(0, retry_auth["cursor"])
            replay = decode_message(await retry.recv())
            self.assertEqual(first_envelope, replay)
        finally:
            await first.close()
            await second.close()
            if retry is not None:
                await retry.close()
            await worker.close()
            await publisher.close()

    async def test_quick_chat_identity_must_be_chosen_before_sending(self):
        paired, _code = await self.pair()
        worker, _auth = await self.open_quick_chat_worker()
        try:
            response = await self.send_quick_chat(paired)
            self.assertEqual("quick_chat_profile_required", response["code"])
            self.assertIn("Preferences", response["message"])
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(worker.recv(), timeout=0.03)
            self.assertEqual(0, self.application.broker.cursor)
        finally:
            await worker.close()

    async def test_device_sets_its_canonical_name_and_quick_chat_uses_it(self):
        paired, _code = await self.pair()
        self.assertEqual(
            {"type": "quick_chat_profile", "name": None},
            await self.quick_chat_profile(paired),
        )
        self.assign_quick_chat_profile(paired, "Old name", "mage")
        initial = await self.quick_chat_profile(paired)
        self.assertEqual(
            {"type": "quick_chat_profile", "name": "Old name"}, initial
        )

        updated = await self.quick_chat_profile(
            paired, name="  \uff2dira   Lyn  "
        )
        self.assertEqual(
            {"type": "quick_chat_profile", "name": "Mira Lyn"}, updated
        )
        record = self.store.authenticate_device_record(
            paired["device_id"], paired["device_token"]
        )
        self.assertIsNotNone(record)
        self.assertEqual("Mira Lyn", record.quick_chat_name)
        self.assertEqual("mage", record.quick_chat_wow_class)

        publisher, _auth = await self.open_publisher()
        captions, _auth = await self.open_captions(paired)
        worker, _auth = await self.open_quick_chat_worker()
        try:
            sent = await self.send_quick_chat(
                paired,
                request_id="chat_self_profile_001",
                text="Use the gateway now.",
            )
            self.assertEqual("quick_chat_sent", sent["type"])
            caption = decode_message(await asyncio.wait_for(captions.recv(), 5))
            self.assertEqual("Mira Lyn [typed]", caption["event"]["speaker"])
            self.assertEqual("mage", caption["event"]["wow_class"])
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(worker.recv(), timeout=0.03)
        finally:
            await captions.close()
            await worker.close()
            await publisher.close()

    async def test_profile_auth_cannot_cross_devices_and_revocation_wins(self):
        victim, _code = await self.pair()
        attacker, _code = await self.pair()
        self.assign_quick_chat_profile(victim, "Original", "priest")

        wrong_token = await self.quick_chat_profile(
            victim,
            name="Stolen",
            device_token="ghd_wrong-token",
        )
        self.assertEqual("authentication_failed", wrong_token["code"])

        crossed = await self.quick_chat_profile(
            attacker,
            name="Stolen",
            device_id=victim["device_id"],
        )
        self.assertEqual("authentication_failed", crossed["code"])
        unchanged = self.store.authenticate_device_record(
            victim["device_id"], victim["device_token"]
        )
        self.assertIsNotNone(unchanged)
        self.assertEqual("Original", unchanged.quick_chat_name)
        self.assertEqual("priest", unchanged.quick_chat_wow_class)

        self.assertTrue(self.store.revoke_device(victim["device_id"]))
        revoked = await self.quick_chat_profile(victim, name="After revoke")
        self.assertEqual("authentication_failed", revoked["code"])
        record = next(
            item
            for item in self.store.list_devices()
            if item.device_id == victim["device_id"]
        )
        self.assertEqual("Original", record.quick_chat_name)
        self.assertEqual("priest", record.quick_chat_wow_class)

    async def test_revocation_between_profile_auth_and_write_still_wins(self):
        paired, _code = await self.pair()
        self.assign_quick_chat_profile(paired, "Original", "priest")
        entered_write = asyncio.Event()
        release_write = asyncio.Event()
        original_write = self.application._set_quick_chat_name

        async def delayed_write(device_id, name):
            entered_write.set()
            await release_write.wait()
            return await original_write(device_id, name)

        with patch.object(
            self.application, "_set_quick_chat_name", delayed_write
        ):
            updating = asyncio.create_task(
                self.quick_chat_profile(paired, name="After revoke")
            )
            await entered_write.wait()
            self.assertTrue(self.store.revoke_device(paired["device_id"]))
            release_write.set()
            response = await updating

        self.assertEqual("authentication_failed", response["code"])
        record = next(
            item
            for item in self.store.list_devices()
            if item.device_id == paired["device_id"]
        )
        self.assertEqual("Original", record.quick_chat_name)
        self.assertEqual("priest", record.quick_chat_wow_class)

    async def test_profile_storage_failure_is_not_reported_as_revocation(self):
        paired, _code = await self.pair()
        self.assign_quick_chat_profile(paired, "Original", "mage")
        private_name = "Private submitted name"
        private_diagnostic = "private database path"
        with patch.object(
            self.store,
            "set_quick_chat_name",
            side_effect=OSError(private_diagnostic),
        ):
            response = await self.quick_chat_profile(
                paired, name=private_name
            )

        self.assertEqual("profile_update_failed", response["code"])
        self.assertIn("Try again", response["message"])
        rendered = json.dumps(response)
        self.assertNotIn(private_name, rendered)
        self.assertNotIn(private_diagnostic, rendered)
        self.assertNotIn(paired["device_token"], rendered)
        record = self.store.authenticate_device_record(
            paired["device_id"], paired["device_token"]
        )
        self.assertIsNotNone(record)
        self.assertEqual("Original", record.quick_chat_name)
        self.assertEqual("mage", record.quick_chat_wow_class)

    async def test_quick_chat_survives_paused_or_stopped_capture_with_a_fresh_audience(self):
        """Typing survives unavailable capture while the voice roster stays fresh."""
        paired, _code = await self.pair()
        self.assign_quick_chat_profile(paired)
        publisher, _auth = await self.open_publisher()
        captions, _auth = await self.open_captions(paired)
        try:
            published = await self.send_publisher_event(
                publisher, SessionEvent(state="active")
            )
            self.assertEqual("published", published["type"])
            active = decode_message(await asyncio.wait_for(captions.recv(), 5))
            self.assertEqual("active", active["event"]["state"])

            for index, state in enumerate(("paused", "stopped"), 1):
                published = await self.send_publisher_event(
                    publisher, SessionEvent(state=state)
                )
                self.assertEqual("published", published["type"])
                envelope = decode_message(
                    await asyncio.wait_for(captions.recv(), 5)
                )
                self.assertEqual(state, envelope["event"]["state"])

                text = "Typed while capture is " + state + "."
                response = await self.send_quick_chat(
                    paired,
                    request_id="chat_capture_state_" + str(index),
                    text=text,
                )
                self.assertEqual("quick_chat_sent", response["type"])
                self.assertEqual(
                    self.application.broker.cursor, response["cursor"]
                )
                caption = decode_message(
                    await asyncio.wait_for(captions.recv(), 5)
                )
                self.assertEqual(text, caption["event"]["text"])
            self.assertEqual(0, self.application.active_quick_chat_worker_count)
        finally:
            await captions.close()
            await publisher.close()

    async def test_publisher_loss_accepts_quick_chat_but_revokes_delivery_and_replay(self):
        paired, _code = await self.pair()
        self.assign_quick_chat_profile(paired)
        publisher, _auth = await self.open_publisher()
        captions, _auth = await self.open_captions(paired)
        rejoined = None
        replacement = None
        try:
            stream_id = self.application.broker.stream_id
            await publisher.close()
            revoked = decode_message(
                await asyncio.wait_for(captions.recv(), timeout=1)
            )
            self.assertEqual("voice_channel_required", revoked["code"])
            await self.wait_until(
                lambda: self.application._publisher_connection is None
                and self.application.broker.subscriber_count == 0
                and self.application._audience_key is None
            )

            previous_cursor = self.application.broker.cursor
            response = await self.send_quick_chat(
                paired,
                request_id="chat_after_publisher_loss",
                text="Accepted without a stale audience.",
            )
            self.assertEqual("quick_chat_sent", response["type"])
            self.assertEqual(previous_cursor + 1, response["cursor"])

            async def close_slow():
                return None

            subscriber, baseline, replay = self.application.broker.subscribe(
                stream_id,
                previous_cursor,
                close_slow,
                viewer_token=self.viewer_tokens[0],
            )
            try:
                self.assertEqual(previous_cursor, baseline)
                self.assertEqual(1, len(replay))
                advance = decode_message(replay[0])
                self.assertEqual("cursor_advance", advance["type"])
                self.assertEqual(response["cursor"], advance["cursor"])
                self.assertNotIn("Accepted without a stale audience.", replay[0])
            finally:
                self.application.broker.unsubscribe(subscriber)

            replacement, _auth = await self.open_publisher()
            probe, baseline, replay = self.application.broker.subscribe(
                stream_id,
                previous_cursor,
                close_slow,
                viewer_token=self.viewer_tokens[0],
            )
            try:
                self.assertEqual(response["cursor"], baseline)
                self.assertEqual([], replay)
            finally:
                self.application.broker.unsubscribe(probe)

            rejoined, authentication = await self.open_captions(
                paired,
                last_stream_id=stream_id,
                last_cursor=previous_cursor,
            )
            self.assertEqual(response["cursor"], authentication["cursor"])
        finally:
            await captions.close()
            if rejoined is not None:
                await rejoined.close()
            if replacement is not None:
                await replacement.close()
            await publisher.close()

    async def test_quick_chat_without_a_worker_still_captions(self):
        paired, _code = await self.pair()
        self.assign_quick_chat_profile(paired)
        private_line = "private-line-for-a-worker-that-is-away"
        publisher, _auth = await self.open_publisher()
        captions, _auth = await self.open_captions(paired)
        try:
            response = await self.send_quick_chat(paired, text=private_line)
            self.assertEqual("quick_chat_sent", response["type"])
            self.assertEqual(1, response["cursor"])
            caption = decode_message(await asyncio.wait_for(captions.recv(), 5))
            self.assertEqual(private_line, caption["event"]["text"])
            self.assertEqual(1, self.application.broker.cursor)
        finally:
            await captions.close()
            await publisher.close()

    async def test_revoked_device_cannot_quick_chat(self):
        paired, _code = await self.pair()
        self.assign_quick_chat_profile(paired)
        self.assertTrue(self.store.revoke_device(paired["device_id"]))
        response = await self.send_quick_chat(paired)
        self.assertEqual("authentication_failed", response["code"])
        self.assertEqual(0, self.application.broker.cursor)

    async def test_quick_chat_is_rate_limited_per_device(self):
        paired, _code = await self.pair()
        self.assign_quick_chat_profile(paired)
        publisher, _auth = await self.open_publisher()
        worker, _auth = await self.open_quick_chat_worker()
        try:
            for index in range(server.QUICK_CHAT_RATE_LIMIT):
                sent = await self.send_quick_chat(
                    paired,
                    request_id="chat_rate_" + str(index),
                    text="Line " + str(index),
                )
                self.assertEqual("quick_chat_sent", sent["type"])
            limited = await self.send_quick_chat(
                paired, request_id="chat_rate_blocked", text="Too many"
            )
            self.assertEqual("rate_limited", limited["code"])
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(worker.recv(), timeout=0.03)
            self.assertEqual(
                server.QUICK_CHAT_RATE_LIMIT,
                self.application.broker.cursor,
            )
        finally:
            await worker.close()
            await publisher.close()

    async def test_quick_chat_body_is_not_persisted_or_logged(self):
        paired, _code = await self.pair()
        self.assign_quick_chat_profile(paired)
        private_line = "private-memory-only-raid-line"
        publisher, _auth = await self.open_publisher()
        worker, _auth = await self.open_quick_chat_worker()
        try:
            with self.assertNoLogs("GuildHub", level="DEBUG"):
                response = await self.send_quick_chat(
                    paired,
                    request_id="chat_private_001",
                    text=private_line,
                )
                with self.assertRaises(TimeoutError):
                    await asyncio.wait_for(worker.recv(), timeout=0.03)
            self.assertEqual("quick_chat_sent", response["type"])
            self.assertEqual(1, self.application.active_quick_chat_worker_count)
            database = Path(self.temporary_directory.name) / "guild-hub.sqlite3"
            self.assertNotIn(private_line.encode("ascii"), database.read_bytes())
            self.assertNotIn(
                private_line,
                repr(self.application._quick_chat_cache),
            )
        finally:
            await worker.close()
            await publisher.close()

    async def test_same_request_retries_after_lost_ack_without_duplicate_caption(self):
        paired, _code = await self.pair()
        self.assign_quick_chat_profile(paired)
        publisher, _auth = await self.open_publisher()
        captions, _auth = await self.open_captions(paired)
        worker, _auth = await self.open_quick_chat_worker()
        raw_client = await connect(
            self.base_url + "/v1/quick-chat", subprotocols=[SUBPROTOCOL]
        )
        request = {
            "type": "quick_chat",
            "request_id": "chat_ack_loss_001",
            "device_id": paired["device_id"],
            "device_token": paired["device_token"],
            "text": "One caption only.",
        }
        try:
            await raw_client.send(encode_message(request))
            caption = decode_message(await asyncio.wait_for(captions.recv(), 5))
            self.assertEqual("One caption only.", caption["event"]["text"])

            # Simulate losing the success acknowledgement. The completed
            # redacted cursor result still answers an identical retry.
            await raw_client.close()
            await self.wait_until(lambda: self.application.broker.cursor == 1)
            retry = await self.send_quick_chat(
                paired,
                request_id="chat_ack_loss_001",
                text="One caption only.",
            )
            self.assertEqual(
                {
                    "type": "quick_chat_sent",
                    "request_id": "chat_ack_loss_001",
                    "cursor": 1,
                },
                retry,
            )
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(captions.recv(), timeout=0.03)
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(worker.recv(), timeout=0.03)
            self.assertEqual(1, self.application.broker.cursor)
        finally:
            await raw_client.close()
            await worker.close()
            await captions.close()
            await publisher.close()

    async def test_request_id_conflict_cannot_change_a_published_message(self):
        paired, _code = await self.pair()
        self.assign_quick_chat_profile(paired)
        publisher, _auth = await self.open_publisher()
        worker, _auth = await self.open_quick_chat_worker()
        try:
            first = await self.send_quick_chat(
                paired, request_id="chat_conflict_001", text="Original"
            )
            self.assertEqual("quick_chat_sent", first["type"])
            conflict = await self.send_quick_chat(
                paired, request_id="chat_conflict_001", text="Changed"
            )
            self.assertEqual("request_conflict", conflict["code"])
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(worker.recv(), timeout=0.03)
            self.assertEqual(1, self.application.broker.cursor)
        finally:
            await worker.close()
            await publisher.close()

    async def test_real_publisher_relay_and_client_deliver_memory_only_events(self):
        paired, _code = await self.pair()
        credentials = DeviceCredentials(
            server_url=self.base_url,
            device_id=paired["device_id"],
            device_token=paired["device_token"],
            guild_id=paired["guild_id"],
            guild_name=paired["guild_name"],
        )

        class MemoryStore:
            def load(self):
                return credentials

        class RecordingSink:
            def __init__(self):
                self.events = []

            def send(self, event):
                self.events.append(event)
                return True

        client_sink = RecordingSink()
        self.enterContext(patch(
            "GuildHub.client.update_notice_path",
            return_value=Path(self.temporary_directory.name) / "update-available.json",
        ))
        client = GuildCaptionClient(
            MemoryStore(),
            client_sink,
            max_retries=0,
            retry_initial=0,
            retry_max=0,
        )
        publisher = await asyncio.to_thread(
            RemoteCaptionSink.start,
            self.base_url,
            PUBLISHER_TOKEN,
        )
        client_task = None
        try:
            # The real publisher owns the audience lease, so the roster goes
            # out before the member connects - the order production keeps.
            self.audience_revision = getattr(self, "audience_revision", 0) + 1
            self.assertTrue(
                publisher.update_audience(
                    1, self.audience_revision, self.viewer_tokens
                )
            )
            await self.wait_until(
                lambda: self.application._audience_key
                == (1, self.audience_revision)
            )
            client_task = asyncio.create_task(client.run())
            await self.wait_until(
                lambda: self.application.broker.subscriber_count == 1
            )
            self.assertTrue(publisher.send(SessionEvent(state="paused")))
            self.assertTrue(publisher.send(ClearEvent()))
            self.assertTrue(publisher.send(SessionEvent(state="active")))
            self.assertTrue(publisher.send(CAPTION, **self.audience_fields()))
            await self.wait_until(
                lambda: CAPTION in client_sink.events,
                timeout=2.0,
            )
        finally:
            # The member leaves before the publisher does. Closing the
            # publisher first drops the audience lease, which now closes
            # every viewer with voice_channel_required - and the client
            # task would re-raise that here instead of stopping cleanly.
            await client.stop()
            if client_task is not None:
                await asyncio.wait_for(client_task, timeout=2.0)
            if publisher is not None:
                await asyncio.to_thread(publisher.close)

        self.assertIsInstance(client_sink.events[0], ClearEvent)
        self.assertEqual(
            [
                SessionEvent(state="paused"),
                ClearEvent(),
                SessionEvent(state="active"),
                CAPTION,
            ],
            client_sink.events[-4:],
        )
        for path in Path(self.temporary_directory.name).glob("*"):
            if path.is_file():
                self.assertNotIn(CAPTION.text.encode("utf-8"), path.read_bytes())

    async def test_new_client_never_receives_pre_auth_buffer(self):
        paired, _code = await self.pair()
        self.application.broker.publish(CAPTION, session_bound=False)
        captions, authentication = await self.open_captions(paired)
        try:
            self.assertEqual(
                self.application.broker.stream_id, authentication["stream_id"]
            )
            self.assertEqual(1, authentication["cursor"])
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(captions.recv(), timeout=0.02)

            self.application.broker.publish(CAPTION, session_bound=False)
            envelope = decode_message(
                await asyncio.wait_for(captions.recv(), timeout=1)
            )
            self.assertEqual(2, envelope["cursor"])
        finally:
            await captions.close()

    async def test_matching_stream_replays_from_reported_baseline(self):
        paired, _code = await self.pair()
        self.application.broker.publish(CAPTION, session_bound=False)
        captions, authentication = await self.open_captions(
            paired,
            last_stream_id=self.application.broker.stream_id,
            last_cursor=0,
        )
        try:
            self.assertEqual(0, authentication["cursor"])
            envelope = decode_message(await captions.recv())
            self.assertEqual(1, envelope["cursor"])
        finally:
            await captions.close()

    async def test_a_dead_peer_closing_slowly_stalls_nobody_else(self):
        """The takeover close must happen outside the server-wide lock.

        It used to happen inside it. websockets bounds a close only by
        close_timeout against a peer that never answers - a laptop asleep
        through a wifi drop - so one dead peer parked the lock every connect
        and every disconnect cleanup must take. This wedges the predecessor's
        close on an event that is not released until cleanup. The other
        device must connect while close is blocked. The timeout is only a
        deadlock bound, not a two-second performance requirement on busy CI.
        """
        first_device, _code = await self.pair()
        second_device, _code = await self.pair()
        first, _auth = await self.open_captions(first_device)
        try:
            self.assertEqual(1, self.application.active_caption_count)
            session = next(iter(self.application._caption_sessions.values()))
            never = asyncio.Event()
            close_entered = asyncio.Event()
            original_close = session.connection.close

            async def wedged_close(*args, **kwargs):
                close_entered.set()
                await never.wait()

            session.connection.close = wedged_close
            try:
                # The same device reconnecting triggers the takeover of the
                # wedged predecessor...
                replacement, _auth = await asyncio.wait_for(
                    self.open_captions(first_device), timeout=15
                )
                try:
                    await asyncio.wait_for(close_entered.wait(), timeout=15)
                    # ...and a DIFFERENT device must get through while that
                    # close is still pending.
                    other, _auth = await asyncio.wait_for(
                        self.open_captions(second_device), timeout=15
                    )
                    self.assertFalse(never.is_set())
                    await other.close()
                finally:
                    await replacement.close()
            finally:
                session.connection.close = original_close
                never.set()
        finally:
            try:
                await first.close()
            except ConnectionClosed:
                pass

    async def test_only_one_caption_socket_per_device_survives_replacement(self):
        paired, _code = await self.pair()
        first, _auth = await self.open_captions(paired)
        # A replacement used to leave no trace at all. Two installs sharing
        # one pairing kick each other forever, and the only way to see it
        # was to notice captions blinking. The record names the device and
        # never the token.
        with self.assertLogs("GuildHub.server", level="INFO") as log:
            second, _auth = await self.open_captions(paired)
            with self.assertRaises(ConnectionClosed):
                await first.recv()
        joined = "\n".join(log.output)
        self.assertIn("replaced by a newer connection", joined)
        self.assertIn(paired["device_id"], joined)
        self.assertNotIn(paired["device_token"], joined)
        third, _auth = await self.open_captions(paired)
        with self.assertRaises(ConnectionClosed):
            await second.recv()
        try:
            await self.wait_until(lambda: self.application.active_caption_count == 1)
            self.assertEqual(1, self.application.broker.subscriber_count)
        finally:
            await third.close()
        await self.wait_until(lambda: self.application.active_caption_count == 0)
        self.assertEqual(0, self.application.broker.subscriber_count)

    async def test_first_publisher_erases_orphaned_replay(self):
        self.application.broker.publish(SessionEvent(state="active"))
        self.application.broker.publish(
            replace(CAPTION, text="orphaned publisher text")
        )
        cursor = self.application.broker.cursor

        publisher, authentication = await self.open_publisher()
        try:
            self.assertEqual(cursor, authentication["cursor"])
            self.assertEqual(cursor, self.application.broker.cursor)
            self.assertIsNone(self.application.broker.latest_session)

            async def close_slow():
                return None

            subscriber, baseline, replay = self.application.broker.subscribe(
                self.application.broker.stream_id, 0, close_slow
            )
            try:
                self.assertEqual(cursor, baseline)
                self.assertEqual([], replay)
            finally:
                self.application.broker.unsubscribe(subscriber)

            delayed = replace(CAPTION, text="orphaned publisher late text")
            self.assertEqual(
                {"type": "published", "cursor": cursor + 1},
                await self.send_publisher_event(publisher, delayed),
            )
            subscriber, baseline, replay = self.application.broker.subscribe(
                self.application.broker.stream_id, 0, close_slow
            )
            try:
                self.assertEqual(cursor, baseline)
                self.assertEqual(1, len(replay))
                self.assertEqual(
                    "cursor_advance", decode_message(replay[0])["type"]
                )
                self.assertNotIn(delayed.text, "".join(replay))
            finally:
                self.application.broker.unsubscribe(subscriber)
        finally:
            await publisher.close()

    async def test_stop_invalidates_replay_already_copied_for_authentication(self):
        paired, _code = await self.pair()
        publisher, _auth = await self.open_publisher()
        for event in (
            SessionEvent(state="active"),
            replace(CAPTION, text="copied replay private text"),
        ):
            self.assertEqual(
                "published",
                (await self.send_publisher_event(publisher, event))["type"],
            )

        original_send = self.application._send_caption_frame
        replay_send_started = asyncio.Event()
        release_replay_send = asyncio.Event()

        async def gated_send(connection, subscriber, encoded, **kwargs):
            if (
                kwargs.get("replay_generation") is not None
                and not replay_send_started.is_set()
            ):
                replay_send_started.set()
                await release_replay_send.wait()
            return await original_send(connection, subscriber, encoded, **kwargs)

        self.application._send_caption_frame = gated_send
        captions = None
        try:
            captions, authentication = await self.open_captions(
                paired,
                last_stream_id=self.application.broker.stream_id,
                last_cursor=0,
            )
            self.assertEqual(0, authentication["cursor"])
            await asyncio.wait_for(replay_send_started.wait(), timeout=1)

            stopped = SessionEvent(state="stopped")
            self.assertEqual(
                "published",
                (await self.send_publisher_event(publisher, stopped))["type"],
            )
            release_replay_send.set()

            delivered = [
                decode_message(await asyncio.wait_for(captions.recv(), timeout=1))
                for _index in range(3)
            ]
            self.assertEqual([1, 2, 3], [frame["cursor"] for frame in delivered])
            self.assertEqual(
                ["cursor_advance", "cursor_advance", "caption_event"],
                [frame["type"] for frame in delivered],
            )
            self.assertEqual("stopped", delivered[-1]["event"]["state"])
            self.assertNotIn("copied replay private text", json.dumps(delivered))
        finally:
            release_replay_send.set()
            self.application._send_caption_frame = original_send
            if captions is not None:
                await captions.close()
            await publisher.close()

    async def test_only_one_publisher_survives_replacement(self):
        _paired, _code = await self.pair()
        viewer_token = self.viewer_tokens[-1]
        first, _auth = await self.open_publisher()
        for event in (
            SessionEvent(state="active"),
            replace(CAPTION, text="replaced publisher text"),
        ):
            self.assertEqual(
                "published",
                (await self.send_publisher_event(first, event))["type"],
            )
        replaced_cursor = self.application.broker.cursor

        second, second_auth = await self.open_publisher()
        self.assertEqual(replaced_cursor, second_auth["cursor"])
        self.assertIsNone(self.application.broker.latest_session)
        with self.assertRaises(ConnectionClosed):
            await first.recv()
        try:
            self.assertEqual(1, self.application.active_publisher_count)
            for event in (
                SessionEvent(state="active"),
                replace(CAPTION, text="replacement publisher text"),
            ):
                self.assertEqual(
                    "published",
                    (await self.send_publisher_event(second, event))["type"],
                )

            async def close_slow():
                return None

            subscriber, baseline, replay = self.application.broker.subscribe(
                self.application.broker.stream_id,
                0,
                close_slow,
                viewer_token=viewer_token,
            )
            try:
                self.assertEqual(replaced_cursor, baseline)
                self.assertEqual(2, len(replay))
                joined = "".join(replay)
                self.assertNotIn("replaced publisher text", joined)
                self.assertIn("replacement publisher text", joined)
                self.assertEqual(
                    SessionEvent(state="active"),
                    self.application.broker.latest_session,
                )
            finally:
                self.application.broker.unsubscribe(subscriber)
        finally:
            await second.close()
        await self.wait_until(lambda: self.application.active_publisher_count == 0)

    async def test_replaced_publisher_cleanup_cannot_erase_successor_replay(self):
        end_of_stream = object()

        class ControlledPublisherConnection:
            def __init__(self):
                self.subprotocol = SUBPROTOCOL
                self.incoming = asyncio.Queue()
                self.outgoing = asyncio.Queue()
                self.close_started = asyncio.Event()
                self.cleanup_blocked = asyncio.Event()
                self.release_cleanup = asyncio.Event()
                self._authentication_read = False
                self._close_queued = False

            async def recv(self):
                if self._authentication_read:
                    raise AssertionError("authentication was read more than once")
                self._authentication_read = True
                return encode_message(
                    {"type": "authenticate", "publisher_token": PUBLISHER_TOKEN}
                )

            async def send(self, encoded):
                await self.outgoing.put(decode_message(encoded))

            def __aiter__(self):
                return self

            async def __anext__(self):
                message = await self.incoming.get()
                if message is end_of_stream:
                    self.cleanup_blocked.set()
                    await self.release_cleanup.wait()
                    raise StopAsyncIteration
                return message

            async def close(self, *_args, **_kwargs):
                self.close_started.set()
                if not self._close_queued:
                    self._close_queued = True
                    self.incoming.put_nowait(end_of_stream)

            async def client_send(self, payload):
                await self.incoming.put(encode_message(payload))

            async def client_recv(self):
                return await asyncio.wait_for(self.outgoing.get(), timeout=1)

        async def publish(connection, event, **routing):
            payload = {
                "type": "publish",
                "event": json.loads(event_to_json(event)),
            }
            payload.update(routing)
            await connection.client_send(payload)
            response = await connection.client_recv()
            self.assertEqual("published", response["type"])

        first = ControlledPublisherConnection()
        second = ControlledPublisherConnection()
        first_task = asyncio.create_task(self.application._publish(first))
        second_task = None
        try:
            self.assertEqual("authenticated", (await first.client_recv())["type"])

            second_task = asyncio.create_task(self.application._publish(second))
            await asyncio.wait_for(first.close_started.wait(), timeout=1)
            await asyncio.wait_for(first.cleanup_blocked.wait(), timeout=1)
            self.assertEqual("authenticated", (await second.client_recv())["type"])

            viewer_token = "1" * 24
            await second.client_send(
                {
                    "type": "audience_update",
                    "generation": 1,
                    "revision": 0,
                    "viewer_tokens": [viewer_token],
                }
            )
            self.assertEqual("audience_updated", (await second.client_recv())["type"])
            await publish(second, SessionEvent(state="active"))
            await publish(
                second,
                replace(CAPTION, text="successor replay survives"),
                audience_generation=1,
                audience_revision=0,
            )

            first.release_cleanup.set()
            await asyncio.wait_for(first_task, timeout=1)

            async def close_slow():
                return None

            subscriber, _baseline, replay = self.application.broker.subscribe(
                self.application.broker.stream_id,
                0,
                close_slow,
                viewer_token=viewer_token,
            )
            try:
                self.assertEqual(
                    SessionEvent(state="active"),
                    self.application.broker.latest_session,
                )
                self.assertIn("successor replay survives", "".join(replay))
            finally:
                self.application.broker.unsubscribe(subscriber)
        finally:
            first.release_cleanup.set()
            second.release_cleanup.set()
            await first.close()
            await second.close()
            await asyncio.gather(
                first_task,
                *(tuple() if second_task is None else (second_task,)),
                return_exceptions=True,
            )

    async def test_worker_control_authentication_and_replacement_are_exclusive(self):
        rejected, failure = await self.open_quick_chat_worker(token="wrong_token")
        try:
            self.assertEqual("authentication_failed", failure["code"])
            self.assertEqual(0, self.application.active_quick_chat_worker_count)
        finally:
            await rejected.close()

        first, first_auth = await self.open_quick_chat_worker()
        second, second_auth = await self.open_quick_chat_worker()
        self.assertEqual({"type": "authenticated"}, first_auth)
        self.assertEqual({"type": "authenticated"}, second_auth)
        try:
            with self.assertRaises(ConnectionClosed):
                await first.recv()
            self.assertEqual(1, self.application.active_quick_chat_worker_count)
        finally:
            await first.close()
            await second.close()
        await self.wait_until(
            lambda: self.application.active_quick_chat_worker_count == 0
        )

    async def test_worker_replacement_authenticates_while_old_close_is_blocked_and_stays_idle(self):
        first, _auth = await self.open_quick_chat_worker()
        previous = self.application._quick_chat_worker_session
        self.assertIsNotNone(previous)
        release_close = asyncio.Event()
        close_started = asyncio.Event()
        original_close = previous.connection.close

        async def blocked_close(*_args, **_kwargs):
            close_started.set()
            await release_close.wait()

        previous.connection.close = blocked_close
        second = None
        try:
            # Authentication must arrive even while retiring the old socket.
            second, authentication = await asyncio.wait_for(
                self.open_quick_chat_worker(), timeout=0.2
            )
            self.assertEqual({"type": "authenticated"}, authentication)
            await close_started.wait()

            paired, _code = await self.pair()
            self.assign_quick_chat_profile(paired)
            sent = await self.send_quick_chat(
                paired,
                request_id="chat_ready_replacement_001",
                text="Ready replacement",
            )
            self.assertEqual("quick_chat_sent", sent["type"])
            self.assertEqual(1, self.application.active_quick_chat_worker_count)
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(second.recv(), timeout=0.03)
        finally:
            previous.connection.close = original_close
            release_close.set()
            await first.close()
            if second is not None:
                await second.close()

    async def test_malformed_worker_result_is_fatal_and_cannot_carry_text(self):
        worker, _auth = await self.open_quick_chat_worker()
        try:
            await worker.send(
                encode_message(
                    {
                        "type": "quick_chat_delivery",
                        "request_id": "qcw_unsolicited",
                        "status": "delivered",
                        "text": "must never be accepted in a result",
                    }
                )
            )
            failure = decode_message(await worker.recv())
            self.assertEqual("invalid_request", failure["code"])
            with self.assertRaises(ConnectionClosed):
                await worker.recv()
            await self.wait_until(
                lambda: self.application.active_quick_chat_worker_count == 0
            )
        finally:
            await worker.close()

    async def test_stale_legacy_worker_receipt_is_ignored_without_closing_connection(self):
        worker, _auth = await self.open_quick_chat_worker()
        request_id = "qcw_stale_receipt_001"
        try:
            await worker.send(
                encode_message(
                    {
                        "type": "quick_chat_delivery",
                        "request_id": request_id,
                        "status": "delivered",
                    }
                )
            )
            await asyncio.sleep(0.02)
            self.assertEqual(1, self.application.active_quick_chat_worker_count)

            paired, _code = await self.pair()
            self.assign_quick_chat_profile(paired)
            sent = await self.send_quick_chat(
                paired,
                request_id="chat_after_stale_receipt_001",
                text="Feed only",
            )
            self.assertEqual("quick_chat_sent", sent["type"])
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(worker.recv(), timeout=0.03)
            self.assertEqual(1, self.application.active_quick_chat_worker_count)
        finally:
            await worker.close()

    async def test_identical_in_flight_retries_share_one_caption(self):
        paired, _code = await self.pair()
        self.assign_quick_chat_profile(paired)
        captions, _auth = await self.open_captions(paired)
        worker, _auth = await self.open_quick_chat_worker()
        try:
            first = asyncio.create_task(
                self.send_quick_chat(
                    paired, request_id="chat_inflight_001", text="Stack now"
                )
            )
            second = asyncio.create_task(
                self.send_quick_chat(
                    paired, request_id="chat_inflight_001", text="Stack now"
                )
            )
            first_ack, second_ack = await asyncio.gather(first, second)
            self.assertEqual(first_ack, second_ack)
            self.assertEqual(1, first_ack["cursor"])
            self.assertEqual(1, self.application.broker.cursor)
            caption = decode_message(await asyncio.wait_for(captions.recv(), 5))
            self.assertEqual("Stack now", caption["event"]["text"])
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(captions.recv(), timeout=0.03)
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(worker.recv(), timeout=0.03)
        finally:
            await worker.close()
            await captions.close()

    async def test_message_group_caption_order_is_arrival_order(self):
        paired, _code = await self.pair()
        self.assign_quick_chat_profile(paired)
        captions, _auth = await self.open_captions(paired)
        worker, _auth = await self.open_quick_chat_worker()
        try:
            first = await self.send_quick_chat(
                paired, request_id="chat_order_001", text="First request"
            )
            second = await self.send_quick_chat(
                paired, request_id="chat_order_002", text="Second request"
            )
            self.assertEqual(1, first["cursor"])
            self.assertEqual(2, second["cursor"])

            delivered = [
                decode_message(await asyncio.wait_for(captions.recv(), 5))
                ["event"]["text"],
                decode_message(await asyncio.wait_for(captions.recv(), 5))
                ["event"]["text"],
            ]
            self.assertEqual(["First request", "Second request"], delivered)
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(worker.recv(), timeout=0.03)
            self.assertEqual(2, self.application.broker.cursor)
        finally:
            await captions.close()
            await worker.close()

    async def test_rate_limit_state_is_bounded_and_pruned(self):
        for index in range(server.QUICK_CHAT_RATE_DEVICE_LIMIT):
            self.assertTrue(
                self.application._consume_quick_chat_rate(
                    "ghdev_rate_" + str(index), 1.0
                )
            )
        self.assertEqual(
            server.QUICK_CHAT_RATE_DEVICE_LIMIT,
            self.application.quick_chat_rate_device_count,
        )
        self.assertFalse(
            self.application._consume_quick_chat_rate("ghdev_overflow", 1.0)
        )
        self.application._prune_quick_chat_state(
            1.0 + server.QUICK_CHAT_RATE_WINDOW_SECONDS
        )
        self.assertEqual(0, self.application.quick_chat_rate_device_count)
        self.assertTrue(
            self.application._consume_quick_chat_rate("ghdev_after_prune", 20.0)
        )

    async def test_success_dedupe_cache_evicts_completed_entries_at_its_bound(self):
        paired, _code = await self.pair()
        self.assign_quick_chat_profile(paired)
        with patch.object(server, "QUICK_CHAT_CACHE_LIMIT", 2):
            for index in range(3):
                sent = await self.send_quick_chat(
                    paired,
                    request_id="chat_cache_" + str(index),
                    text="Cached " + str(index),
                )
                self.assertEqual("quick_chat_sent", sent["type"])
            self.assertEqual(2, self.application.quick_chat_cache_count)

    async def test_global_cap_includes_unauthenticated_handlers_and_cleans_up(self):
        limited = GuildHubServer(
            self.store,
            replace(self.application.config, connection_limit=1),
        )
        server, base_url = await self.start_application(limited)
        first = await connect(base_url + "/v1/captions", subprotocols=[SUBPROTOCOL])
        try:
            await self.wait_until(lambda: limited.active_handler_count == 1)
            second = await connect(
                base_url + "/v1/captions", subprotocols=[SUBPROTOCOL]
            )
            try:
                with self.assertRaises(ConnectionClosed) as closed:
                    await second.recv()
                self.assertIsNotNone(closed.exception.rcvd)
                self.assertEqual(1013, closed.exception.rcvd.code)
            finally:
                await second.close()
        finally:
            await first.close()
            await self.wait_until(lambda: limited.active_handler_count == 0)
            server.close()
            await server.wait_closed()

    async def test_wrong_subprotocol_handler_counts_toward_global_cap(self):
        class BlockingCloseConnection:
            def __init__(self):
                self.subprotocol = "wrong.v1"
                self.request = None
                self.close_started = asyncio.Event()
                self.release_close = asyncio.Event()
                self.close_calls = []

            async def close(self, code=1000, reason=""):
                self.close_calls.append((code, reason))
                self.close_started.set()
                await self.release_close.wait()

        limited = GuildHubServer(
            self.store,
            replace(self.application.config, connection_limit=1),
        )
        first = BlockingCloseConnection()
        first_task = asyncio.create_task(limited.handle(first))
        await first.close_started.wait()
        self.assertEqual([(1002, "Subprotocol required")], first.close_calls)
        self.assertEqual(1, limited.active_handler_count)

        second = BlockingCloseConnection()
        second_task = asyncio.create_task(limited.handle(second))
        await second.close_started.wait()
        self.assertEqual([(1013, "Service unavailable")], second.close_calls)
        self.assertEqual(1, limited.active_handler_count)

        second.release_close.set()
        await second_task
        self.assertEqual(1, limited.active_handler_count)

        first.release_close.set()
        await first_task
        self.assertEqual(0, limited.active_handler_count)

    async def test_idle_caption_socket_closes_promptly_after_revoke(self):
        paired, _code = await self.pair()
        captions, _auth = await self.open_captions(paired)
        self.store.revoke_device(paired["device_id"])
        with self.assertRaises(ConnectionClosed) as closed:
            await asyncio.wait_for(captions.recv(), timeout=1)
        self.assertIsNotNone(closed.exception.rcvd)
        self.assertEqual(1008, closed.exception.rcvd.code)
        await self.wait_until(lambda: self.application.active_caption_count == 0)

    async def test_caption_published_after_revoke_is_never_delivered(self):
        paired, _code = await self.pair()
        captions, _auth = await self.open_captions(paired)
        self.store.revoke_device(paired["device_id"])
        with self.assertRaises(ConnectionClosed):
            await asyncio.wait_for(captions.recv(), timeout=1)
        await self.wait_until(lambda: self.application.broker.subscriber_count == 0)
        self.application.broker.publish(CAPTION, session_bound=False)
        self.assertEqual(0, self.application.broker.subscriber_count)

    async def test_burst_and_replay_do_not_authenticate_per_caption(self):
        paired, _code = await self.pair()
        application = GuildHubServer(
            self.store,
            replace(
                self.application.config,
                replay_limit=16,
                client_queue_limit=32,
                revalidation_seconds=10,
            ),
        )
        # The viewer joins this relay's audience before the backlog exists,
        # so the replay it is owed starts at cursor 0.
        await ServerTests.refresh_audience(self, application)
        for _index in range(5):
            application.broker.publish(CAPTION, session_bound=False)
        server, base_url = await self.start_application(application)
        captions = None
        authenticate = self.store.authenticate_device_record
        try:
            with patch.object(
                self.store, "authenticate_device_record", wraps=authenticate
            ) as authentication_calls:
                captions, response = await self.open_captions(
                    paired,
                    last_stream_id=application.broker.stream_id,
                    last_cursor=0,
                    base_url=base_url,
                )
                self.assertEqual(0, response["cursor"])
                for _index in range(5):
                    await captions.recv()
                for _index in range(10):
                    application.broker.publish(CAPTION, session_bound=False)
                for _index in range(10):
                    await captions.recv()
                self.assertEqual(1, authentication_calls.call_count)
        finally:
            if captions is not None:
                await captions.close()
            server.close()
            await server.wait_closed()

    async def test_device_authentication_failures_are_generic(self):
        paired, _code = await self.pair()

        async def failure_for(device_id, token):
            websocket = await connect(
                self.base_url + "/v1/captions", subprotocols=[SUBPROTOCOL]
            )
            try:
                await websocket.send(
                    encode_message(
                        {
                            "type": "authenticate",
                            "device_id": device_id,
                            "device_token": token,
                            "last_stream_id": "",
                            "last_cursor": 0,
                        }
                    )
                )
                return decode_message(await websocket.recv())
            finally:
                await websocket.close()

        unknown = await failure_for("ghdev_unknown", paired["device_token"])
        wrong = await failure_for(paired["device_id"], "ghd_wrong")
        self.assertEqual(unknown, wrong)
        self.assertEqual("authentication_failed", unknown["code"])

    async def test_first_message_has_a_timeout(self):
        self.application.config = replace(self.application.config, auth_timeout=0.15)
        async with connect(
            self.base_url + "/v1/captions", subprotocols=[SUBPROTOCOL]
        ) as websocket:
            failure = decode_message(await websocket.recv())
        self.assertEqual("authentication_failed", failure["code"])

    async def test_required_subprotocol_is_enforced(self):
        with self.assertRaises(InvalidStatus) as captured:
            async with connect(self.base_url + "/v1/captions"):
                pass
        self.assertEqual(400, captured.exception.response.status_code)

    async def test_health_endpoint_contains_no_runtime_or_secret_data(self):
        def fetch():
            with urllib.request.urlopen(self.health_url, timeout=2) as response:
                return response.status, response.read()

        status, body = await asyncio.to_thread(fetch)
        self.assertEqual(200, status)
        self.assertEqual(
            {"status": "ok", "service": "vemals-ears"}, json.loads(body)
        )
        self.assertNotIn(PUBLISHER_TOKEN.encode("ascii"), body)

    async def test_join_page_uses_fragment_csp_and_never_logs_code(self):
        enrollment = self.store.create_enrollment(300)
        with self.assertNoLogs("GuildHub", level="DEBUG"):
            status, headers, body = await self.fetch("/join#" + enrollment.code)

        page = body.decode("ascii")
        self.assertEqual(200, status)
        self.assertEqual("no-store", headers["Cache-Control"])
        self.assertEqual("no-referrer", headers["Referrer-Policy"])
        self.assertEqual("nosniff", headers["X-Content-Type-Options"])
        self.assertNotIn(enrollment.code, page)
        self.assertIn("Download Vemal's Ears", page)
        # Shino, 2026-09-01: no Open/Pair option. The page opens the app
        # itself, so a member's whole job is "install, open the link".
        self.assertNotIn("Open / Pair", page)
        self.assertNotIn("vemalsears://", page)
        # The page still reads the fragment and clears it from the address
        # bar; it just no longer navigates anywhere with it.
        self.assertIn("window.location.hash", page)
        self.assertIn("replaceState", page)
        # The page tries by itself, but a browser may refuse to launch an
        # app without a gesture, and a page that then sits there looks
        # broken - so a click is armed and revealed as the fallback.
        # Shino, 2026-09-02: "make them copy paste the link in the
        # application. Don't try to pair or consume the key in the link
        # because they need to download the app first."
        #
        # The page hands over the link and stops. It never emits the
        # custom scheme, so a browser that refuses to launch an app
        # without a gesture no longer matters, the confirmation can no
        # longer open behind the game unanswered, and a one-use
        # invitation is not spent before the app even exists.
        self.assertIn('id="link"', page)
        self.assertIn('id="copy"', page)
        self.assertIn("Copy link", page)
        self.assertIn("paste this link", page)
        self.assertIn("Connect guild", page)
        self.assertNotIn("vemalsears://", page)
        self.assertNotIn("Finish pairing", page)
        self.assertNotIn("window.location.href = target", page)
        # A revealed button with no destination is worse than none: it
        # looks like the way forward and does nothing.
        # The copy control is armed before it is shown, so it can never
        # be a button that looks like the way forward and does nothing.
        self.assertIn("box.textContent = link", page)
        self.assertIn("copy.hidden = false", page)
        self.assertIn("channel=pilot&amp;source=guild", page)
        self.assertIn("window.location.hash.slice(1)", page)
        self.assertIn("window.history.replaceState", page)
        self.assertIn("/^ghe_[A-Za-z0-9_-]{1,188}$/", page)
        # The code is regex-gated before it reaches the DOM and is
        # written with textContent, never innerHTML - which is the
        # property that matters now that it is displayed rather than
        # placed in a URL.
        self.assertNotIn("innerHTML", page)
        self.assertIn("box.textContent", page)

        script_match = re.search(r"<script>(.*?)</script>", page, flags=re.DOTALL)
        self.assertIsNotNone(script_match)
        script = script_match.group(1).encode("ascii")
        digest = base64.b64encode(hashlib.sha256(script).digest()).decode("ascii")
        policy = headers["Content-Security-Policy"]
        self.assertIn("default-src 'none'", policy)
        self.assertIn("style-src 'unsafe-inline'", policy)
        self.assertIn("script-src 'sha256-" + digest + "'", policy)
        self.assertNotIn("script-src 'unsafe-inline'", policy)

    async def test_join_fragment_never_changes_server_request_or_page(self):
        enrollment = self.store.create_enrollment(300)
        valid = await self.fetch("/join#" + enrollment.code)
        invented = await self.fetch("/join#ghe_" + ("x" * 43))
        slash = await self.fetch("/join/#ghe_" + ("y" * 43))
        self.assertEqual(200, valid[0])
        self.assertEqual(valid[2], invented[2])
        self.assertEqual(valid[2], slash[2])
        self.assertNotIn(enrollment.code.encode("ascii"), valid[2])

        malformed = await self.fetch("/join/ghe_bad%2Fpath")
        missing = await self.fetch("/not-a-route")
        self.assertEqual(404, malformed[0])
        self.assertEqual(malformed[0], missing[0])
        self.assertEqual(malformed[2], missing[2])
        self.assertNotIn(b"ghe_bad", malformed[2])
        self.assertEqual("no-store", malformed[1]["Cache-Control"])

    async def test_viewing_fragment_join_page_does_not_consume_code(self):
        enrollment = self.store.create_enrollment(300)
        with tempfile.TemporaryDirectory() as folder:
            member = Path(folder) / "Vemals-Ears-Setup.exe"
            member.write_bytes(b"MZ member installer")
            (Path(folder) / "vemals-ears-member.zip").write_bytes(
                b"PK source-complete portable bundle"
            )
            self.application.config = dataclasses.replace(
                self.application.config, download_file=str(member)
            )
            for path in ("/join", "/join/", "/privacy", "/download", "/source"):
                with self.subTest(path=path):
                    status, _headers, _body = await self.fetch(path + "#" + enrollment.code)
                    self.assertEqual(200, status)
        paired = await self.pair_code(enrollment.code)
        self.assertEqual("paired", paired["type"])
        rejected = await self.pair_code(enrollment.code)
        self.assertEqual("enrollment_failed", rejected["code"])

    async def test_non_loopback_bind_requires_explicit_proxy_acknowledgement(self):
        with self.assertRaisesRegex(RuntimeError, "VEMALS_EARS_TRUST_PROXY=1"):
            await run_server(self.application, "0.0.0.0", 0)


class ServerCliTests(unittest.TestCase):
    def test_standalone_cli_does_not_offer_hosted_addon_jobs(self):
        parser = build_parser()
        self.assertNotIn("hosted-jobs", parser.format_help())
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["hosted-jobs", "enable", "device"])



    def test_profile_assigns_validated_quick_chat_identity_to_existing_device(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Path(temporary_directory) / "guild-hub.sqlite3"
            store = CredentialStore(database, PEPPER)
            store.initialize()
            enrollment = store.create_enrollment(300)
            credentials = store.claim_enrollment(
                enrollment.code, "Raid desktop", "0.1.0"
            )
            output = io.StringIO()
            with patch.dict(os.environ, {"VEMALS_EARS_PEPPER": PEPPER}):
                with redirect_stdout(output):
                    result = main(
                        [
                            "--database",
                            str(database),
                            "profile",
                            credentials.device_id,
                            "--name",
                            "  Mira   Lyn ",
                            "--wow-class",
                            "MAGE",
                        ]
                    )
            record = store.list_devices()[0]
        self.assertEqual(0, result)
        self.assertEqual("Mira Lyn", record.quick_chat_name)
        self.assertEqual("mage", record.quick_chat_wow_class)
        self.assertIn(credentials.device_id, output.getvalue())
        self.assertNotIn(credentials.device_token, output.getvalue())

    def test_profile_refuses_unknown_device(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = str(Path(temporary_directory) / "guild-hub.sqlite3")
            output = io.StringIO()
            with patch.dict(os.environ, {"VEMALS_EARS_PEPPER": PEPPER}):
                with redirect_stdout(output):
                    result = main(
                        [
                            "--database",
                            database,
                            "profile",
                            "ghdev_unknown",
                            "--name",
                            "Mira",
                        ]
                    )
        self.assertEqual(1, result)
        self.assertIn("No active device found", output.getvalue())

    def test_enroll_prints_pair_link_and_fragment_join_link(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = str(Path(temporary_directory) / "guild-hub.sqlite3")
            output = io.StringIO()
            with patch.dict(os.environ, {"VEMALS_EARS_PEPPER": PEPPER}):
                with redirect_stdout(output):
                    result = main(
                        [
                            "--database",
                            database,
                            "enroll",
                            "--minutes",
                            "1",
                            "--server-url",
                            PUBLIC_SERVER_URL,
                        ]
                    )
        self.assertEqual(0, result)
        rendered = output.getvalue()
        self.assertIn("Pairing link: vemalsears://pair?", rendered)
        self.assertIn("server=wss%3A%2F%2Fears.example.org", rendered)
        self.assertRegex(
            rendered,
            r"Join page: https://ears\.example\.org/join#ghe_[A-Za-z0-9_-]+",
        )
        self.assertNotRegex(rendered, r"/join/ghe_")

    def test_public_environment_defaults_feed_security_configuration(self):
        environment = {
            "VEMALS_EARS_PUBLIC_SERVER_URL": PUBLIC_SERVER_URL,
            "VEMALS_EARS_CLIENT_DOWNLOAD_URL": CLIENT_DOWNLOAD_URL,
            "VEMALS_EARS_PUBLIC_HTTP_URL": "https://join.example.org",
            "VEMALS_EARS_CONNECTION_LIMIT": "12",
            "VEMALS_EARS_REVALIDATION_SECONDS": "3",
            "VEMALS_EARS_TRUST_PROXY": "1",
        }
        with patch.dict(os.environ, environment):
            serve_arguments = build_parser().parse_args(["serve"])
            enroll_arguments = build_parser().parse_args(["enroll"])
        self.assertEqual(PUBLIC_SERVER_URL, serve_arguments.public_server_url)
        self.assertEqual(CLIENT_DOWNLOAD_URL, serve_arguments.client_download_url)
        self.assertEqual(12, serve_arguments.connection_limit)
        self.assertEqual(3.0, serve_arguments.revalidation_seconds)
        self.assertTrue(serve_arguments.trust_proxy)
        self.assertEqual(PUBLIC_SERVER_URL, enroll_arguments.server_url)
        self.assertEqual("https://join.example.org", enroll_arguments.join_url)

    def test_enroll_join_url_override_controls_fragment_origin(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = str(Path(temporary_directory) / "guild-hub.sqlite3")
            output = io.StringIO()
            with patch.dict(os.environ, {"VEMALS_EARS_PEPPER": PEPPER}):
                with redirect_stdout(output):
                    result = main(
                        [
                            "--database",
                            database,
                            "enroll",
                            "--server-url",
                            PUBLIC_SERVER_URL,
                            "--join-url",
                            "https://join.example.org/",
                        ]
                    )
        self.assertEqual(0, result)
        self.assertIn(
            "Join page: https://join.example.org/join#ghe_", output.getvalue()
        )




class PairingConfigEndpointTests(unittest.IsolatedAsyncioTestCase):
    # Borrows the harness, not the thirty tests: subclassing
    # ServerTests re-ran its whole suite against a second server
    # instance, which was pure duplication and flake surface.
    asyncSetUp = ServerTests.asyncSetUp
    asyncTearDown = ServerTests.asyncTearDown
    start_application = ServerTests.start_application
    async def test_pairing_config_is_public_and_machine_readable(self):
        """The JSON twin of the join page's data-server attribute.

        A member pastes the https join link into the desktop dialog; the
        client asks this endpoint for the websocket URL instead of making
        the member relay it by hand. Everything served here is already
        public in the join page HTML.
        """
        import json as json_module
        import urllib.request

        url = self.health_url.replace("/healthz", "/pairing-config")

        def fetch():
            # to_thread: the server shares this test's event loop, so a
            # blocking urlopen on the loop thread deadlocks into a timeout.
            with urllib.request.urlopen(url, timeout=5) as response:
                return response.status, response.read(4096)

        status, raw = await asyncio.to_thread(fetch)
        self.assertEqual(200, status)
        payload = json_module.loads(raw)
        self.assertEqual("vemals-ears", payload["service"])
        self.assertEqual(PUBLIC_SERVER_URL, payload["server_url"])
        self.assertEqual("Vemal's Ears", payload["guild_name"])


class DownloadRouteTests(unittest.IsolatedAsyncioTestCase):
    # Borrows the harness, not the thirty tests: subclassing
    # ServerTests re-ran its whole suite against a second server
    # instance, which was pure duplication and flake surface.
    asyncSetUp = ServerTests.asyncSetUp
    asyncTearDown = ServerTests.asyncTearDown
    start_application = ServerTests.start_application

    async def fetch_path(self, path):
        url = self.health_url.replace("/healthz", path)

        def fetch():
            try:
                with urllib.request.urlopen(url, timeout=5) as response:
                    return response.status, dict(response.headers), response.read()
            except urllib.error.HTTPError as error:
                with error:
                    return error.code, dict(error.headers), error.read()

        return await asyncio.to_thread(fetch)

    async def test_artifact_responses_hold_one_slot_until_connection_close(self):
        class PendingConnection:
            def __init__(self):
                self.closed = asyncio.Event()

            async def wait_closed(self):
                await self.closed.wait()

        with tempfile.TemporaryDirectory() as folder:
            artifact = Path(folder) / "small.zip"
            artifact.write_bytes(b"PK one bounded response")
            first_connection = PendingConnection()
            first = await self.application._serve_attachment(
                first_connection,
                artifact,
                "application/zip",
            )
            self.assertIsNotNone(first)

            refused = await self.application._serve_attachment(
                PendingConnection(),
                artifact,
                "application/zip",
            )
            self.assertEqual(503, refused.status_code)

            first_connection.closed.set()
            for _attempt in range(20):
                await asyncio.sleep(0)
                if not self.application._artifact_slot.locked():
                    break
            self.assertFalse(self.application._artifact_slot.locked())

            third_connection = PendingConnection()
            third = await self.application._serve_attachment(
                third_connection,
                artifact,
                "application/zip",
            )
            self.assertIsNotNone(third)
            third_connection.closed.set()
            await asyncio.sleep(0)

    async def test_canceled_artifact_request_holds_slot_until_reader_finishes(self):
        class PendingConnection:
            async def wait_closed(self):
                await asyncio.Event().wait()

        started = threading.Event()
        finish = threading.Event()
        calls = []

        def blocked_response(*_args, **_kwargs):
            calls.append(object())
            started.set()
            finish.wait(5)
            return server._http_response(b"MZ response", "application/octet-stream")

        with tempfile.TemporaryDirectory() as folder:
            artifact = Path(folder) / "blocked.exe"
            artifact.write_bytes(b"MZ fixture")
            with patch.object(server, "_attachment_response", blocked_response):
                first = asyncio.create_task(
                    self.application._serve_attachment(
                        PendingConnection(),
                        artifact,
                        "application/octet-stream",
                    )
                )
                for _attempt in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(started.is_set())
                first.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await first

                refused = await self.application._serve_attachment(
                    PendingConnection(),
                    artifact,
                    "application/octet-stream",
                )
                self.assertEqual(503, refused.status_code)
                self.assertEqual(1, len(calls))

                finish.set()
                for _attempt in range(100):
                    await asyncio.sleep(0.01)
                    if not self.application._artifact_slot.locked():
                        break
                self.assertFalse(self.application._artifact_slot.locked())

    @staticmethod
    def publish_pointer(folder, version, installer_payload, source_payload):
        folder = Path(folder)
        installer = folder / ("Vemals-Ears-Setup-" + version + ".exe")
        source = folder / ("vemals-ears-member-" + version + ".zip")
        installer.write_bytes(installer_payload)
        source.write_bytes(source_payload)
        document = {
            "schema": 1,
            "version": version,
            "installer": {
                "filename": installer.name,
                "bytes": len(installer_payload),
                "sha256": hashlib.sha256(installer_payload).hexdigest(),
            },
            "source": {
                "filename": source.name,
                "bytes": len(source_payload),
                "sha256": hashlib.sha256(source_payload).hexdigest(),
            },
        }
        temporary = folder / "vemals-ears-release.json.publishing"
        temporary.write_text(json.dumps(document) + "\n", encoding="ascii")
        os.replace(temporary, folder / "vemals-ears-release.json")
        return installer, source, document
    async def test_download_404s_plainly_when_nothing_is_published(self):
        import urllib.error
        import urllib.request

        url = self.health_url.replace("/healthz", "/download")

        def fetch():
            try:
                with urllib.request.urlopen(url, timeout=5) as response:
                    return response.status, response.read(200)
            except urllib.error.HTTPError as error:
                with error:
                    return error.code, error.read(200)

        status, body = await asyncio.to_thread(fetch)
        self.assertEqual(404, status)
        self.assertIn(b"No download is published", body)

    async def test_download_serves_the_configured_file_byte_exact(self):
        import urllib.request

        payload = b"PK\x03\x04 pretend zip " + b"z" * 4096
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "downtime.zip"
            target.write_bytes(payload)
            self.application.config = dataclasses.replace(
                self.application.config, download_file=str(target)
            )
            url = self.health_url.replace("/healthz", "/download")

            def fetch():
                with urllib.request.urlopen(url, timeout=5) as response:
                    return (
                        response.status,
                        response.headers.get("Content-Disposition", ""),
                        response.read(),
                    )

            status, disposition, body = await asyncio.to_thread(fetch)
        self.assertEqual(200, status)
        self.assertIn('filename="downtime.zip"', disposition)
        self.assertEqual(payload, body, "the download must be byte-exact")

    async def test_source_download_serves_only_the_fixed_sibling_byte_exact(self):
        import urllib.error
        import urllib.request

        url = self.health_url.replace("/healthz", "/source")

        def fetch():
            try:
                with urllib.request.urlopen(url, timeout=5) as response:
                    return (
                        response.status,
                        response.headers.get("Content-Disposition", ""),
                        response.read(),
                    )
            except urllib.error.HTTPError as error:
                with error:
                    return error.code, "", error.read(200)

        with tempfile.TemporaryDirectory() as folder:
            installer = Path(folder) / "Vemals-Ears-Setup.exe"
            installer.write_bytes(b"MZ installer")
            self.application.config = dataclasses.replace(
                self.application.config, download_file=str(installer)
            )
            status, _disposition, body = await asyncio.to_thread(fetch)
            self.assertEqual(404, status)
            self.assertIn(b"source bundle is published", body)

            source = Path(folder) / "vemals-ears-member.zip"
            payload = b"PK\x03\x04 source-complete portable bundle"
            source.write_bytes(payload)
            status, disposition, body = await asyncio.to_thread(fetch)
            self.assertEqual(200, status)
            self.assertIn('filename="vemals-ears-member.zip"', disposition)
            self.assertEqual(payload, body)

            immutable = Path(folder) / "vemals-ears-member-0.5.13.zip"
            immutable_payload = b"PK\x03\x04 immutable source for 0.5.13"
            immutable.write_bytes(immutable_payload)
            versioned_url = self.health_url.replace(
                "/healthz", "/source/0.5.13"
            )

            def fetch_versioned():
                with urllib.request.urlopen(versioned_url, timeout=5) as response:
                    return (
                        response.status,
                        response.headers.get("Content-Disposition", ""),
                        response.read(),
                    )

            status, disposition, body = await asyncio.to_thread(fetch_versioned)
            self.assertEqual(200, status)
            self.assertIn(
                'filename="vemals-ears-member-0.5.13.zip"', disposition
            )
            self.assertEqual(immutable_payload, body)

            invalid_url = self.health_url.replace(
                "/healthz", "/source/not-a-version"
            )

            def fetch_invalid():
                try:
                    urllib.request.urlopen(invalid_url, timeout=5)
                except urllib.error.HTTPError as error:
                    with error:
                        return error.code
                return 200

            self.assertEqual(404, await asyncio.to_thread(fetch_invalid))

            # A configured legacy ZIP must not alias /download and /source
            # to the same bytes.
            self.application.config = dataclasses.replace(
                self.application.config, download_file=str(source)
            )
            status, _disposition, _body = await asyncio.to_thread(fetch)
            self.assertEqual(404, status)

    async def test_pointer_controls_current_routes_and_exact_landing_links(self):
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            legacy = folder / "Vemals-Ears-Setup.exe"
            legacy.write_bytes(b"MZ stale legacy installer")
            (folder / "vemals-ears-member.zip").write_bytes(b"PK stale legacy source")
            (folder / "downtime.zip").write_bytes(b"PK downtime")
            (folder / "downtime-lite.zip").write_bytes(b"PK downtime lite")
            (folder / "DowntimeUser-Setup.exe").write_bytes(b"MZ downtime user")
            installer_payload = b"MZ current immutable installer"
            source_payload = b"PK current immutable source"
            installer, source, document = self.publish_pointer(
                folder,
                "0.5.13",
                installer_payload,
                source_payload,
            )
            self.application.config = dataclasses.replace(
                self.application.config,
                download_file=str(legacy),
                client_version="9.9.9",
                client_download_url="https://stale.invalid/wrong?release=old",
            )

            cases = (
                ("/download", installer, installer_payload),
                ("/download/0.5.13", installer, installer_payload),
                ("/source", source, source_payload),
                ("/source/0.5.13", source, source_payload),
            )
            for path, expected_file, expected_payload in cases:
                with self.subTest(path=path):
                    status, headers, body = await self.fetch_path(path)
                    self.assertEqual(200, status)
                    self.assertEqual(expected_payload, body)
                    self.assertIn(expected_file.name, headers["Content-Disposition"])

            _status, _headers, page_bytes = await self.fetch_path("/join")
            page = page_bytes.decode("utf-8")
            self.assertIn(
                'href="https://ears.example.org/download/0.5.13"',
                page,
            )
            self.assertIn('href="/source/0.5.13"', page)
            self.assertNotIn("9.9.9", page)

            with patch.object(server, "download_fingerprint") as fingerprint:
                offer = server.update_offer(
                    "9.9.9",
                    str(legacy),
                    "https://stale.invalid/wrong?release=old",
                    public_server_url=PUBLIC_SERVER_URL,
                )
            fingerprint.assert_not_called()
            self.assertEqual(
                {
                    "version": "0.5.13",
                    "sha256": document["installer"]["sha256"],
                    "bytes": len(installer_payload),
                    "url": "https://ears.example.org/download/0.5.13",
                },
                offer,
            )

    async def test_pointer_mode_rejects_every_noncanonical_artifact_path(self):
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            legacy = folder / "Vemals-Ears-Setup.exe"
            legacy.write_bytes(b"legacy")
            self.publish_pointer(folder, "0.5.13", b"installer", b"source")
            self.application.config = dataclasses.replace(
                self.application.config,
                download_file=str(legacy),
            )
            suffixes = (
                "",
                "0.5",
                "0.5.13.0",
                "00.5.13",
                "0.05.13",
                "0.5.013",
                "0.5.65536",
                "not-a-version",
                "0.5.13%2Fextra",
                "0.5.13.exe",
            )
            for prefix in ("/download/", "/source/"):
                for suffix in suffixes:
                    with self.subTest(path=prefix + suffix):
                        status, _headers, _body = await self.fetch_path(prefix + suffix)
                        self.assertEqual(404, status)

    async def test_present_invalid_pointer_never_falls_back_to_legacy_files(self):
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            legacy = folder / "Vemals-Ears-Setup.exe"
            legacy.write_bytes(b"MZ legacy installer")
            (folder / "vemals-ears-member.zip").write_bytes(b"PK legacy source")
            (folder / "vemals-ears-member-0.5.13.zip").write_bytes(
                b"PK legacy exact source"
            )
            (folder / "vemals-ears-release.json").write_text(
                "{}\n",
                encoding="ascii",
            )
            self.application.config = dataclasses.replace(
                self.application.config,
                download_file=str(legacy),
                client_version="0.5.13",
            )
            for path in (
                "/download",
                "/download/0.5.13",
                "/source",
                "/source/0.5.13",
            ):
                with self.subTest(path=path):
                    with self.assertLogs(server.LOGGER, level="WARNING"):
                        status, _headers, _body = await self.fetch_path(path)
                    self.assertEqual(404, status)

            with self.assertLogs(server.LOGGER, level="WARNING"):
                _status, _headers, page_bytes = await self.fetch_path("/join")
            page = page_bytes.decode("utf-8")
            self.assertNotIn(CLIENT_DOWNLOAD_URL, page)
            self.assertNotIn('href="/source', page)
            self.assertIn("not published", page)
            with self.assertLogs(server.LOGGER, level="WARNING"):
                self.assertIsNone(
                    server.update_offer(
                        "0.5.13",
                        str(legacy),
                        CLIENT_DOWNLOAD_URL,
                    )
                )

    async def test_same_size_artifact_corruption_suppresses_the_whole_release(self):
        for corrupt in ("installer", "source"):
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as name:
                folder = Path(name)
                legacy = folder / "Vemals-Ears-Setup.exe"
                legacy.write_bytes(b"MZ legacy installer")
                installer, source, _document = self.publish_pointer(
                    folder,
                    "0.5.13",
                    b"MZ immutable installer",
                    b"PK immutable source",
                )
                target = installer if corrupt == "installer" else source
                payload = target.read_bytes()
                target.write_bytes(bytes([payload[0] ^ 1]) + payload[1:])
                self.application.config = dataclasses.replace(
                    self.application.config,
                    download_file=str(legacy),
                    client_version="0.5.13",
                )

                for path in ("/download", "/source"):
                    with self.assertLogs(server.LOGGER, level="WARNING"):
                        status, _headers, _body = await self.fetch_path(path)
                    self.assertEqual(404, status)
                with self.assertLogs(server.LOGGER, level="WARNING"):
                    _status, _headers, page_bytes = await self.fetch_path("/join")
                page = page_bytes.decode("utf-8")
                self.assertNotIn('href="/source', page)
                self.assertNotIn('href="https://ears.example.org/download', page)

    async def test_old_immutable_links_survive_an_atomic_pointer_flip(self):
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            legacy = folder / "Vemals-Ears-Setup.exe"
            legacy.write_bytes(b"legacy")
            old_installer = b"MZ old installer"
            old_source = b"PK old source"
            self.publish_pointer(folder, "0.5.13", old_installer, old_source)
            self.application.config = dataclasses.replace(
                self.application.config,
                download_file=str(legacy),
            )
            with patch.object(
                server,
                "published_release",
                wraps=server.published_release,
            ) as resolve:
                _status, _headers, old_page_bytes = await self.fetch_path("/join")
            self.assertEqual(1, resolve.call_count)
            old_page = old_page_bytes.decode("utf-8")
            self.assertIn("/download/0.5.13", old_page)
            self.assertIn("/source/0.5.13", old_page)

            new_installer = b"MZ new installer"
            new_source = b"PK new source"
            self.publish_pointer(folder, "0.5.14", new_installer, new_source)

            old_download = await self.fetch_path("/download/0.5.13")
            old_source_download = await self.fetch_path("/source/0.5.13")
            current_download = await self.fetch_path("/download")
            current_source = await self.fetch_path("/source")
            self.assertEqual((200, old_installer), (old_download[0], old_download[2]))
            self.assertEqual(
                (200, old_source),
                (old_source_download[0], old_source_download[2]),
            )
            self.assertEqual(
                (200, new_installer),
                (current_download[0], current_download[2]),
            )
            self.assertEqual(
                (200, new_source),
                (current_source[0], current_source[2]),
            )
            _status, _headers, new_page_bytes = await self.fetch_path("/join")
            new_page = new_page_bytes.decode("utf-8")
            self.assertIn("/download/0.5.14", new_page)
            self.assertIn("/source/0.5.14", new_page)

    async def test_privacy_page_promises_only_what_the_code_does(self):
        """Every data-handling claim on /privacy, pinned.

        A privacy page that overstates is worse than none - it is the one
        the guild will be held to. If a claim here has to change, the
        page is wrong or the code is, and this test is where that
        argument happens.
        """
        import urllib.request

        url = self.health_url.replace("/healthz", "/privacy")

        def fetch():
            with urllib.request.urlopen(url, timeout=5) as response:
                return (
                    response.status,
                    dict(response.headers),
                    response.read().decode("utf-8"),
                )

        status, headers, page = await asyncio.to_thread(fetch)
        self.assertEqual(200, status)
        self.assertEqual("no-store", headers["Cache-Control"])
        self.assertIn("Guild &amp; Privacy", page)
        # The claims: nothing transcribed without consent, nothing
        # persisted, and exactly what IS kept, with the erase commands.
        self.assertIn("Nothing is transcribed without consent", page)
        self.assertIn("No voice, caption, or Message Group text is persisted", page)
        self.assertIn("never recorded or saved", page)
        self.assertRegex(
            page,
            rf"keeps up to\s+{self.application.config.replay_limit} "
            r"feed events in memory",
        )
        self.assertRegex(
            page,
            r"Ending a caption\s+session erases that session's caption and "
            r"Message Group text from replay",
        )
        self.assertIn("Stopping or restarting the relay clears all replay", page)
        self.assertIn("Voice-channel authorization applies to every replay", page)
        self.assertNotIn("When voice membership is required", page)
        self.assertIn("excludes text from before you joined", page)
        self.assertIn("while you were absent", page)
        self.assertNotIn("someone joining late sees the last few", page)
        self.assertIn("/vem unmap", page)
        self.assertIn("/vem forget", page)
        # The DISCLOSURE is load-bearing, not just the denials. The guild
        # server does keep consent choices, pairings, and mappings, and
        # the page must say so - a sabotage that swapped this sentence
        # for "Nothing at all is kept" passed the first version of this
        # test, which is precisely the overstatement the page must never
        # make.
        self.assertIn(
            "What the guild server does keep: your consent choice", page
        )
        self.assertIn("an opaque viewer link", page)
        self.assertIn("Message Group display name", page)
        self.assertRegex(
            page, r"device\s+retention process removes that record"
        )
        self.assertRegex(page, r"never stores your Discord\s+user ID")
        self.assertNotIn("Nothing at all is kept", page)
        self.assertIn("only if you trust the download's source", page)
        self.assertIn("Vemal's Ears (By Downtime)", page)
        self.assertIn("Connect guild", page)
        self.assertIn("/vem setup", page)
        self.assertIn("request a fresh invitation", page)
        self.assertNotIn("Downtime User", page)
        self.assertNotIn("Downtime Host", page)
        self.assertNotIn("Start-Downtime", page)
        # The guild's name appears; the publisher token must not.
        self.assertIn(self.application.config.guild_name, page)
        self.assertNotIn(PUBLISHER_TOKEN, page)










    async def test_standalone_host_never_offers_or_serves_other_products(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            member = root / "Vemals-Ears-Setup.exe"
            member.write_bytes(b"MZ member installer")
            for name in ("downtime.zip", "downtime-lite.zip", "DowntimeUser-Setup.exe"):
                (root / name).write_bytes(b"unrelated product bytes")
            self.application.config = dataclasses.replace(
                self.application.config, download_file=str(member)
            )
            for route in ("/join", "/privacy"):
                status, _headers, body = await self.fetch_path(route)
                self.assertEqual(200, status)
                self.assertIn(b"Vemal's Ears", body)
                for unrelated in (b"Downtime User", b"Downtime Host", b"Gear Helper", b"Sim Helper", b"Boss Talents"):
                    self.assertNotIn(unrelated, body)
            for route in ("/download/downtime", "/download/downtime-lite", "/download/downtime-lite/portable", "/v1/jobs/boss"):
                status, _headers, body = await self.fetch_path(route)
                self.assertEqual(404, status)
                self.assertNotIn(b"unrelated product bytes", body)
            status, _headers, body = await self.fetch_path("/download")
            self.assertEqual((200, b"MZ member installer"), (status, body))

    async def test_join_explains_the_complete_ve_pairing_flow(self):
        status, _headers, body = await self.fetch_path("/join")
        self.assertEqual(200, status)
        page = body.decode("ascii")
        for expected in ("Download Vemal's Ears", "/vem setup", "/vem invite", "Connect guild", "Copy link", "Downloading does not use the invitation", "active Discord voice channel"):
            self.assertIn(expected, page)
        self.assertNotIn("vemalsears://", page)
        self.assertIn('href="/privacy"', page)

    async def test_the_content_type_follows_the_file(self):
        """An installer served as application/zip is a browser's problem.

        The member download is an .exe now and was a .zip before, and it
        may change again. The type was hard-coded to zip, which a browser
        or a corporate proxy is entitled to act on - so it comes from the
        file's own extension.
        """
        import urllib.request

        cases = (
            ("Vemals-Ears-Setup.exe", b"MZ fake installer",
             "application/octet-stream"),
            ("vemals-ears-member.zip", b"PK\x03\x04 fake archive",
             "application/zip"),
            ("mystery.bin", b"no idea", "application/octet-stream"),
        )
        url = self.health_url.replace("/healthz", "/download")
        for name, payload, expected in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                target = Path(folder) / name
                target.write_bytes(payload)
                self.application.config = dataclasses.replace(
                    self.application.config, download_file=str(target)
                )

                def fetch():
                    with urllib.request.urlopen(url, timeout=5) as response:
                        return (
                            response.headers.get("Content-Type", ""),
                            response.headers.get("Content-Disposition", ""),
                            response.read(),
                        )

                content_type, disposition, body = await asyncio.to_thread(fetch)
                self.assertIn(expected, content_type)
                self.assertIn('filename="' + name + '"', disposition)
                self.assertEqual(payload, body)


class CaptionSendPrivacyTests(unittest.IsolatedAsyncioTestCase):
    VIEWER = "0123456789abcdef01234567"

    async def assert_no_next_frame(self, *, replay, expire=False, rejoin=False):
        now = [10.0]
        application = GuildHubServer(
            object(),
            ServerConfig(
                publisher_token=PUBLISHER_TOKEN,
                public_server_url=PUBLIC_SERVER_URL,
                client_download_url=CLIENT_DOWNLOAD_URL,
                audience_lease_seconds=5.0,
            ),
            monotonic=lambda: now[0],
        )
        device = DeviceRecord(
            device_id="device_test",
            device_name="Raid desktop",
            client_version="",
            created_at=0.0,
            last_seen_at=None,
            revoked_at=None,
            viewer_token=self.VIEWER,
        )
        await application._apply_audience_update(
            AudienceUpdate(1, 0, (self.VIEWER,))
        )

        class PausedConnection:
            def __init__(self):
                self.subprotocol = SUBPROTOCOL
                self.sent = []
                self.authenticated = asyncio.Event()
                self.first_caption = asyncio.Event()
                self.second_caption = asyncio.Event()
                self.release_first = asyncio.Event()
                self.close_started = asyncio.Event()
                self.release_close = asyncio.Event()
                self.closed = asyncio.Event()

            async def recv(self):
                return encode_message({
                    "type": "authenticate",
                    "device_id": device.device_id,
                    "device_token": "device_test_token",
                    "last_stream_id": application.broker.stream_id,
                    "last_cursor": 0,
                })

            async def send(self, raw):
                message = decode_message(raw)
                self.sent.append(message)
                if message["type"] == "authenticated":
                    self.authenticated.set()
                if message["type"] == "caption_event":
                    if self.first_caption.is_set():
                        self.second_caption.set()
                    else:
                        # The first send is already in flight. Keep it pending
                        # while membership changes and the peer delays close.
                        self.first_caption.set()
                        await self.release_first.wait()

            async def close(self, **_kwargs):
                self.close_started.set()
                await self.release_close.wait()
                self.closed.set()

            async def wait_closed(self):
                await self.closed.wait()

        async def no_periodic_revalidation(*_args):
            # Exercise the send-time guard, not a conveniently timed timer.
            await asyncio.Event().wait()

        def publish_backlog():
            for _index in range(2):
                application.broker.publish(
                    CAPTION,
                    frozenset({self.VIEWER}),
                    session_bound=False,
                )

        if replay:
            publish_backlog()
        connection = PausedConnection()
        tasks = []
        with (
            patch.object(application, "_device_record", AsyncMock(return_value=device)),
            patch.object(
                application,
                "_revalidate_caption_access",
                no_periodic_revalidation,
            ),
        ):
            handler = asyncio.create_task(application._captions(connection))
            tasks.append(handler)
            try:
                await asyncio.wait_for(connection.authenticated.wait(), timeout=1)
                if not replay:
                    publish_backlog()
                await asyncio.wait_for(connection.first_caption.wait(), timeout=1)
                if expire:
                    now[0] += 6.0
                    connection.release_close.set()
                else:
                    revoke = asyncio.create_task(application._apply_audience_update(
                        AudienceUpdate(1, 1, ())
                    ))
                    tasks.append(revoke)
                    await asyncio.wait_for(connection.close_started.wait(), timeout=1)
                    if rejoin:
                        await application._apply_audience_update(
                            AudienceUpdate(1, 2, (self.VIEWER,))
                        )
                connection.release_first.set()
                leaked = asyncio.create_task(connection.second_caption.wait())
                tasks.append(leaked)
                done, _pending = await asyncio.wait(
                    {handler, leaked},
                    timeout=1,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                self.assertFalse(
                    connection.second_caption.is_set(),
                    "a second caption was sent after access was revoked",
                )
                self.assertIn(handler, done, "the ineligible sender did not stop")
                handler.result()
                self.assertEqual(0, application.broker.subscriber_count)
            finally:
                connection.release_first.set()
                connection.release_close.set()
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def test_replay_stops_on_leave_while_socket_close_is_pending(self):
        await self.assert_no_next_frame(replay=True)

    async def test_live_queue_stays_revoked_after_leave_and_rejoin(self):
        await self.assert_no_next_frame(replay=False, rejoin=True)

    async def test_replay_checks_the_fresh_lease_before_each_send(self):
        await self.assert_no_next_frame(replay=True, expire=True)

    async def test_live_queue_checks_the_fresh_lease_before_each_send(self):
        await self.assert_no_next_frame(replay=False, expire=True)


class VoiceMembershipTests(unittest.IsolatedAsyncioTestCase):
    VIEWER = "0123456789abcdef01234567"

    async def asyncSetUp(self):
        await ServerTests.asyncSetUp(self)
        # These end-to-end privacy cases start a second real relay. Keep the
        # production-sized handshake budget so a loaded CI host cannot turn
        # scheduler delay into an authentication assertion.
        self.application.config = replace(
            self.application.config, auth_timeout=5.0
        )
        self.gated_servers = []

    async def asyncTearDown(self):
        for gated_server in self.gated_servers:
            gated_server.close()
            await gated_server.wait_closed()
        await ServerTests.asyncTearDown(self)

    start_application = ServerTests.start_application
    pair_code = ServerTests.pair_code
    pair = ServerTests.pair
    open_captions = ServerTests.open_captions
    open_publisher = ServerTests.open_publisher

    async def start_gated(self, *, monotonic=None):
        options = {}
        if monotonic is not None:
            options["monotonic"] = monotonic
        application = GuildHubServer(
            self.store,
            replace(
                self.application.config,
                audience_lease_seconds=5.0,
                revalidation_seconds=0.05,
            ),
            **options,
        )
        gated_server, base_url = await self.start_application(application)
        self.gated_servers.append(gated_server)
        return application, base_url

    async def request_viewer_link(self, paired, *, base_url=None):
        async with connect(
            (base_url or self.base_url) + "/v1/viewer-link",
            subprotocols=[SUBPROTOCOL],
        ) as websocket:
            await websocket.send(
                encode_message(
                    {
                        "type": "viewer_link_request",
                        "device_id": paired["device_id"],
                        "device_token": paired["device_token"],
                    }
                )
            )
            return decode_message(await websocket.recv())

    @staticmethod
    async def denial(websocket):
        """The reason a viewer was refused, however it reached them.

        The relay sends the error frame and then closes with the same
        sentence as the close reason. Which of the two a client observes
        is a race, so read whichever arrived and report the code.
        """
        try:
            payload = decode_message(
                await asyncio.wait_for(websocket.recv(), timeout=1)
            )
        except ConnectionClosed as closed:
            reason = closed.rcvd.reason if closed.rcvd else ""
            code = (
                "captions_stopped"
                if "not running" in reason
                else "voice_channel_required"
            )
            return {"code": code, "message": reason}
        return payload

    @staticmethod
    async def audience(publisher, generation, revision, viewer_tokens):
        await publisher.send(
            encode_message(
                {
                    "type": "audience_update",
                    "generation": generation,
                    "revision": revision,
                    "viewer_tokens": viewer_tokens,
                }
            )
        )
        return decode_message(await publisher.recv())

    @staticmethod
    async def publish(
        publisher,
        *,
        generation,
        revision,
        event=CAPTION,
    ):
        await publisher.send(
            encode_message(
                {
                    "type": "publish",
                    "event": json.loads(event_to_json(event)),
                    "audience_generation": generation,
                    "audience_revision": revision,
                }
            )
        )
        return decode_message(await publisher.recv())

    async def test_device_link_code_is_authenticated_one_use_and_opaque(self):
        paired, _code = await self.pair()
        response = await self.request_viewer_link(paired)
        self.assertEqual("viewer_link", response["type"])
        self.assertRegex(response["code"], r"^[0-9A-HJKMNP-TV-Z]{8}$")
        self.assertGreater(response["expires_at"], 0)

        publisher, authentication = await self.open_publisher()
        try:
            self.assertEqual("authenticated", authentication["type"])
            command = {
                "type": "viewer_link",
                "code": response["code"],
                "viewer_token": self.VIEWER,
            }
            await publisher.send(encode_message(command))
            self.assertEqual(
                {
                    "type": "viewer_linked",
                    "code": response["code"],
                    "linked": True,
                },
                decode_message(await publisher.recv()),
            )
            self.assertEqual(
                self.VIEWER,
                self.store.authenticate_device_viewer_token(
                    paired["device_id"], paired["device_token"]
                ),
            )
            await publisher.send(encode_message(command))
            self.assertFalse(decode_message(await publisher.recv())["linked"])
        finally:
            await publisher.close()

    async def test_unlinked_or_outside_device_is_rejected_before_replay(self):
        paired, _code = await self.pair()
        application, base_url = await self.start_gated()
        application.broker.publish(
            CAPTION,
            frozenset({self.VIEWER}),
            session_bound=False,
        )

        captions, response = await self.open_captions(
            paired,
            last_stream_id=application.broker.stream_id,
            last_cursor=0,
            base_url=base_url,
        )
        self.assertEqual("voice_channel_required", response["code"])
        await captions.close()

        self.assertTrue(
            self.store.bind_viewer_token(paired["device_id"], self.VIEWER)
        )
        captions, response = await self.open_captions(
            paired,
            last_stream_id=application.broker.stream_id,
            last_cursor=0,
            base_url=base_url,
        )
        self.assertEqual("voice_channel_required", response["code"])
        self.assertEqual(0, application.broker.subscriber_count)
        await captions.close()

    async def test_leave_capture_snapshot_and_rejoin_are_fail_closed(self):
        paired, _code = await self.pair()
        self.assertTrue(
            self.store.bind_viewer_token(paired["device_id"], self.VIEWER)
        )
        application, base_url = await self.start_gated()
        publisher, _authentication = await self.open_publisher(base_url)
        captions = None
        rejoined = None
        try:
            self.assertEqual(
                {
                    "type": "audience_updated",
                    "generation": 1,
                    "revision": 0,
                },
                await self.audience(publisher, 1, 0, [self.VIEWER]),
            )
            application.broker.publish(
                SessionEvent(state="active"), frozenset({self.VIEWER})
            )
            captions, authentication = await self.open_captions(
                paired, base_url=base_url, client_version="0.5.10"
            )
            self.assertEqual("authenticated", authentication["type"])
            self.assertEqual(
                {"type": "published", "cursor": 2},
                await self.publish(publisher, generation=1, revision=0),
            )
            delivered = decode_message(await captions.recv())
            self.assertEqual(2, delivered["cursor"])
            self.assertNotIn("audience_generation", delivered)
            self.assertNotIn("audience_revision", delivered)

            leave = asyncio.create_task(self.audience(publisher, 1, 1, []))
            denied = decode_message(await captions.recv())
            self.assertEqual("voice_channel_required", denied["code"])
            self.assertEqual("audience_updated", (await leave)["type"])

            # This caption was captured while the viewer was present, but
            # current membership is empty. It advances the stream without
            # becoming replayable to that viewer.
            self.assertEqual(
                {"type": "published", "cursor": 3},
                await self.publish(publisher, generation=1, revision=0),
            )
            await self.audience(publisher, 1, 2, [self.VIEWER])
            rejoined, authentication = await self.open_captions(
                paired,
                last_stream_id=application.broker.stream_id,
                last_cursor=2,
                base_url=base_url,
            )
            self.assertEqual(3, authentication["cursor"])

            # A delayed result captured before this viewer rejoined must
            # not leak its text or make the following eligible cursor look
            # discontinuous to the strict member client.
            self.assertEqual(
                {"type": "published", "cursor": 4},
                await self.publish(publisher, generation=1, revision=1),
            )
            advance = decode_message(await rejoined.recv())
            self.assertEqual(
                {
                    "type": "cursor_advance",
                    "stream_id": application.broker.stream_id,
                    "cursor": 4,
                },
                advance,
            )
            self.assertEqual(
                {"type": "published", "cursor": 5},
                await self.publish(publisher, generation=1, revision=2),
            )
            delivered = decode_message(await rejoined.recv())
            self.assertEqual(5, delivered["cursor"])
        finally:
            if captions is not None:
                await captions.close()
            if rejoined is not None:
                await rejoined.close()
            await publisher.close()

    async def test_stale_lease_and_publisher_disconnect_revoke_viewers(self):
        paired, _code = await self.pair()
        self.assertTrue(
            self.store.bind_viewer_token(paired["device_id"], self.VIEWER)
        )
        now = [10.0]
        application, base_url = await self.start_gated(
            monotonic=lambda: now[0]
        )
        publisher, _authentication = await self.open_publisher(base_url)
        captions = None
        try:
            await self.audience(publisher, 1, 0, [self.VIEWER])
            captions, authentication = await self.open_captions(
                paired, base_url=base_url
            )
            self.assertEqual("authenticated", authentication["type"])
            now[0] += 10.0
            expired = decode_message(
                await asyncio.wait_for(captions.recv(), timeout=1)
            )
            self.assertEqual("voice_channel_required", expired["code"])

            # A fresh lease permits a reconnect, but losing the publisher
            # lease clears it immediately and emits the same explicit state.
            await self.audience(publisher, 2, 0, [self.VIEWER])
            application.broker.publish(
                SessionEvent(state="active"), frozenset({self.VIEWER})
            )
            application.broker.publish(
                replace(CAPTION, text="publisher lease private text"),
                frozenset({self.VIEWER}),
            )
            captions, authentication = await self.open_captions(
                paired, base_url=base_url
            )
            self.assertEqual("authenticated", authentication["type"])
            await publisher.close()
            disconnected = decode_message(
                await asyncio.wait_for(captions.recv(), timeout=1)
            )
            self.assertEqual("voice_channel_required", disconnected["code"])
            self.assertEqual(
                "Join the active Discord voice channel to view captions.",
                disconnected["message"],
            )

            async def close_slow():
                return None

            subscriber, baseline, replay = application.broker.subscribe(
                application.broker.stream_id, 0, close_slow
            )
            try:
                self.assertEqual(application.broker.cursor, baseline)
                self.assertEqual([], replay)
                self.assertIsNone(application.broker.latest_session)
            finally:
                application.broker.unsubscribe(subscriber)
        finally:
            if captions is not None:
                await captions.close()
            await publisher.close()

    def test_the_captions_stopped_code_goes_only_to_clients_that_have_it(self):
        """An unparseable or older version is treated as not having it.

        A client that meets a code it does not know falls through to
        "pair this device again", which in the middle of a raid is worse
        advice than the sentence this change exists to fix.
        """
        self.assertTrue(understands_captions_stopped("0.5.11"))
        self.assertTrue(understands_captions_stopped("0.6.0"))
        self.assertTrue(understands_captions_stopped("1.0.0"))
        self.assertFalse(understands_captions_stopped("0.5.10"))
        self.assertFalse(understands_captions_stopped("0.5.9"))
        self.assertFalse(understands_captions_stopped("0.4.99"))
        self.assertFalse(understands_captions_stopped(""))
        self.assertFalse(understands_captions_stopped("0.5"))
        self.assertFalse(understands_captions_stopped("0.5.10.1"))
        self.assertFalse(understands_captions_stopped("zero.five.ten"))

    async def test_a_stopped_worker_says_so_instead_of_blaming_the_member(self):
        """Shino, 2026-09-07: the member never left; the worker stopped.

        The roster is published BY the worker, so its absence used to
        read to every viewer as "you left the voice channel".
        """
        paired, _code = await self.pair()
        self.assertTrue(
            self.store.bind_viewer_token(paired["device_id"], self.VIEWER)
        )
        _application, base_url = await self.start_gated()
        publisher, _authentication = await self.open_publisher(base_url)
        captions = None
        try:
            await self.audience(publisher, 1, 0, [self.VIEWER])
            captions, authentication = await self.open_captions(
                paired, base_url=base_url, client_version="0.5.11"
            )
            self.assertEqual("authenticated", authentication["type"])
            await publisher.close()
            stopped = await self.denial(captions)
            self.assertEqual("captions_stopped", stopped["code"])
            self.assertIn("not running", stopped["message"])
            # And the viewer is still CLOSED. The sentence changed; who
            # may read captions did not.
            with self.assertRaises(ConnectionClosed):
                await asyncio.wait_for(captions.recv(), timeout=1)
        finally:
            if captions is not None:
                await captions.close()
            await publisher.close()

    async def test_a_member_out_of_the_channel_is_still_told_to_join(self):
        """The worker is up and says this viewer is not in the channel."""
        paired, _code = await self.pair()
        self.assertTrue(
            self.store.bind_viewer_token(paired["device_id"], self.VIEWER)
        )
        _application, base_url = await self.start_gated()
        publisher, _authentication = await self.open_publisher(base_url)
        captions = None
        try:
            await self.audience(publisher, 1, 0, [self.VIEWER])
            captions, authentication = await self.open_captions(
                paired, base_url=base_url, client_version="0.5.10"
            )
            self.assertEqual("authenticated", authentication["type"])
            # The worker stays connected and reports a channel this
            # viewer is not in.
            await self.audience(publisher, 2, 0, [])
            denied = await self.denial(captions)
            self.assertEqual("voice_channel_required", denied["code"])
        finally:
            if captions is not None:
                await captions.close()
            await publisher.close()

    async def test_an_older_client_keeps_the_wording_it_understands(self):
        """Same stopped worker, a 0.5.10 viewer: the old code, unchanged."""
        paired, _code = await self.pair()
        self.assertTrue(
            self.store.bind_viewer_token(paired["device_id"], self.VIEWER)
        )
        _application, base_url = await self.start_gated()
        publisher, _authentication = await self.open_publisher(base_url)
        captions = None
        try:
            await self.audience(publisher, 1, 0, [self.VIEWER])
            captions, authentication = await self.open_captions(
                paired, base_url=base_url, client_version="0.5.10"
            )
            self.assertEqual("authenticated", authentication["type"])
            await publisher.close()
            stopped = await self.denial(captions)
            self.assertEqual("voice_channel_required", stopped["code"])
        finally:
            if captions is not None:
                await captions.close()
            await publisher.close()

    async def test_audience_updates_are_ordered_and_snapshots_are_bounded(self):
        now = [10.0]
        application, _base_url = await self.start_gated(
            monotonic=lambda: now[0]
        )
        for revision in range(300):
            await application._apply_audience_update(
                AudienceUpdate(1, revision, (self.VIEWER,))
            )
        self.assertEqual(256, len(application._audience_snapshots))
        # The adapter repeats an unchanged revision as its lease heartbeat.
        now[0] = 10.1
        await application._apply_audience_update(
            AudienceUpdate(1, 299, (self.VIEWER,))
        )
        self.assertEqual(10.1, application._audience_updated_at)
        self.assertEqual(256, len(application._audience_snapshots))
        with self.assertRaisesRegex(ValueError, "changed the current revision"):
            await application._apply_audience_update(
                AudienceUpdate(1, 299, ())
            )
        with self.assertRaisesRegex(ValueError, "strictly ordered"):
            await application._apply_audience_update(
                AudienceUpdate(1, 298, (self.VIEWER,))
            )


class UpdateOfferTests(unittest.TestCase):
    """What the relay advertises to an authenticated member."""

    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.file = Path(self.temp.name) / "Vemals-Ears-Setup.exe"
        self.file.write_bytes(b"MZ installer" + b"\x00" * 512)

    def test_it_reports_the_real_checksum_of_the_published_file(self):
        import hashlib
        offer = server.update_offer(
            "0.2.0", str(self.file), "https://ears.example.invalid/download"
        )
        self.assertIsNotNone(offer)
        self.assertEqual(
            hashlib.sha256(self.file.read_bytes()).hexdigest(), offer["sha256"]
        )
        self.assertEqual(self.file.stat().st_size, offer["bytes"])
        self.assertEqual("0.2.0", offer["version"])

    def test_nothing_is_offered_without_a_version(self):
        """An offer nobody can act on is worse than no offer: the member
        would be told to update and given nothing to update to."""
        self.assertIsNone(
            server.update_offer("", str(self.file), "https://h/download")
        )
        self.assertIsNone(
            server.update_offer("   ", str(self.file), "https://h/download")
        )

    def test_nothing_is_offered_without_a_published_file(self):
        self.assertIsNone(server.update_offer("0.2.0", "", "https://h/download"))
        self.assertIsNone(
            server.update_offer(
                "0.2.0", str(self.file.with_name("absent.exe")), "https://h/download"
            )
        )

    def test_nothing_is_offered_without_somewhere_to_get_it(self):
        self.assertIsNone(server.update_offer("0.2.0", str(self.file), ""))

    def test_republishing_the_file_changes_the_checksum(self):
        """The operator swaps that file by hand. Members must be offered
        the new build without the relay being restarted."""
        first = server.update_offer("0.2.0", str(self.file), "https://h/download")
        import os
        import time
        replacement = b"MZ a different installer" + b"\x00" * 900
        self.file.write_bytes(replacement)
        # Make the change visible even on a coarse filesystem clock.
        stamp = time.time() + 2
        os.utime(self.file, (stamp, stamp))
        second = server.update_offer("0.2.0", str(self.file), "https://h/download")
        self.assertNotEqual(first["sha256"], second["sha256"])
        self.assertEqual(len(replacement), second["bytes"])


class UpdateEndpointTests(unittest.IsolatedAsyncioTestCase):
    """A paired app can discover releases without gaining caption access."""

    asyncSetUp = ServerTests.asyncSetUp
    asyncTearDown = ServerTests.asyncTearDown
    start_application = ServerTests.start_application
    pair_code = ServerTests.pair_code
    fetch = ServerTests.fetch
    open_captions = ServerTests.open_captions
    open_publisher = ServerTests.open_publisher
    refresh_audience = ServerTests.refresh_audience

    async def paired_outside_channel(self):
        enrollment = self.store.create_enrollment(300)
        paired = await self.pair_code(enrollment.code)
        self.assertEqual("paired", paired["type"])
        self.assertIsNone(self.application.broker.latest_session)
        self.assertIsNone(self.application._audience_key)
        return paired

    @staticmethod
    def headers(paired, *, token=None, version="0.5.10"):
        return {
            "Authorization": "Bearer " + (token or paired["device_token"]),
            server.UPDATE_DEVICE_ID_HEADER: paired["device_id"],
            server.UPDATE_CLIENT_VERSION_HEADER: version,
        }

    async def test_paired_device_gets_no_store_manifest_without_a_session(self):
        paired = await self.paired_outside_channel()

        status, headers, raw = await self.fetch(
            server.UPDATE_PATH, self.headers(paired)
        )

        self.assertEqual(200, status)
        self.assertEqual("no-store", headers["Cache-Control"])
        self.assertEqual(
            {"type": "update_manifest", "update": None}, json.loads(raw)
        )

    async def test_manifest_contains_only_public_release_metadata(self):
        paired = await self.paired_outside_channel()
        published = Path(self.temporary_directory.name) / "Vemals-Ears-Setup.exe"
        published.write_bytes(b"MZ release installer" + b"\x00" * 512)
        self.application.config = replace(
            self.application.config,
            download_file=str(published),
            client_version="0.5.11",
        )

        status, _headers, raw = await self.fetch(
            server.UPDATE_PATH, self.headers(paired)
        )
        document = json.loads(raw)

        self.assertEqual(200, status)
        self.assertEqual({"type", "update"}, set(document))
        self.assertEqual(
            {"version", "sha256", "bytes", "url"}, set(document["update"])
        )
        self.assertEqual("0.5.11", document["update"]["version"])
        self.assertEqual(
            hashlib.sha256(published.read_bytes()).hexdigest(),
            document["update"]["sha256"],
        )
        self.assertNotIn(paired["device_id"].encode("ascii"), raw)
        self.assertNotIn(paired["device_token"].encode("ascii"), raw)
        self.assertNotIn(b"viewer_token", raw)
        self.assertNotIn(b"guild_id", raw)

    async def test_pointer_manifest_uses_public_immutable_url_once(self):
        paired = await self.paired_outside_channel()
        folder = Path(self.temporary_directory.name)
        legacy = folder / "Vemals-Ears-Setup.exe"
        legacy.write_bytes(b"MZ stale legacy")
        _installer, _source, pointer = DownloadRouteTests.publish_pointer(
            folder,
            "0.5.13",
            b"MZ immutable installer",
            b"PK immutable source",
        )
        self.application.config = replace(
            self.application.config,
            download_file=str(legacy),
            client_version="9.9.9",
            client_download_url="https://stale.invalid/wrong?old=1",
        )

        with patch.object(
            server,
            "published_release",
            wraps=server.published_release,
        ) as resolve:
            status, _headers, raw = await self.fetch(
                server.UPDATE_PATH,
                self.headers(paired),
            )

        self.assertEqual(200, status)
        self.assertEqual(1, resolve.call_count)
        self.assertEqual(
            {
                "type": "update_manifest",
                "update": {
                    "version": "0.5.13",
                    "sha256": pointer["installer"]["sha256"],
                    "bytes": pointer["installer"]["bytes"],
                    "url": "https://ears.example.org/download/0.5.13",
                },
            },
            json.loads(raw),
        )
        self.assertNotIn(b"stale.invalid", raw)

    async def test_invalid_and_revoked_credentials_are_rejected_generically(self):
        paired = await self.paired_outside_channel()
        invalid = await self.fetch(
            server.UPDATE_PATH,
            self.headers(paired, token="ghd_invalid-device-token"),
        )
        self.assertTrue(self.store.revoke_device(paired["device_id"]))
        revoked = await self.fetch(server.UPDATE_PATH, self.headers(paired))

        self.assertEqual(401, invalid[0])
        self.assertEqual(401, revoked[0])
        self.assertEqual(json.loads(invalid[2]), json.loads(revoked[2]))
        self.assertEqual(
            {"type": "error", "code": "authentication_failed"},
            json.loads(revoked[2]),
        )
        self.assertEqual("no-store", invalid[1]["Cache-Control"])
        self.assertEqual("no-store", revoked[1]["Cache-Control"])

    async def test_update_authentication_does_not_relax_caption_membership(self):
        paired = await self.paired_outside_channel()
        status, _headers, _body = await self.fetch(
            server.UPDATE_PATH, self.headers(paired)
        )
        self.assertEqual(200, status)

        publisher, publisher_response = await self.open_publisher()
        self.assertEqual("authenticated", publisher_response["type"])
        captions = None
        try:
            captions, response = await self.open_captions(
                paired, client_version="0.5.11"
            )
            self.assertEqual("voice_channel_required", response["code"])
        finally:
            if captions is not None:
                await captions.close()
            await publisher.close()


class PlannedMaintenanceTests(unittest.IsolatedAsyncioTestCase):
    """A deploy has to be distinguishable from a relay that fell over.

    Shino, 2026-09-02: the bot "disconnects and reconnects a lot during
    the deploy". It was not reconnecting badly - it was never told that
    the disconnection was on purpose, so it treated a scheduled restart
    as a fault and churned until its retry budget ran out.
    """

    asyncSetUp = ServerTests.asyncSetUp
    asyncTearDown = ServerTests.asyncTearDown
    start_application = ServerTests.start_application
    pair_code = ServerTests.pair_code
    pair = ServerTests.pair
    open_captions = ServerTests.open_captions

    async def test_viewers_are_told_the_restart_is_planned(self):
        paired, _code = await self.pair()
        websocket, response = await self.open_captions(paired)
        self.assertEqual("authenticated", response["type"])

        await self.application.announce_maintenance()

        # 1001 "going away" is the code the WebSocket spec reserves for a
        # server shutting down on purpose. Anything else and the client
        # cannot tell this apart from a crash.
        with self.assertRaises(ConnectionClosed) as caught:
            await asyncio.wait_for(websocket.recv(), timeout=5)
        self.assertEqual(1001, caught.exception.rcvd.code)
        self.assertIn("Paused for updates", caught.exception.rcvd.reason)

    async def test_announcing_releases_every_session(self):
        first, _first_code = await self.pair()
        second, _second_code = await self.pair()
        first_socket, _ = await self.open_captions(first)
        second_socket, _ = await self.open_captions(second)
        self.assertEqual(2, self.application.active_caption_count)

        await self.application.announce_maintenance()

        self.assertEqual(0, self.application.active_caption_count)
        for websocket in (first_socket, second_socket):
            with self.assertRaises(ConnectionClosed) as caught:
                await asyncio.wait_for(websocket.recv(), timeout=5)
            self.assertEqual(1001, caught.exception.rcvd.code)

    async def test_announcing_with_nobody_connected_is_quiet(self):
        # The shutdown path calls this unconditionally, so the empty case
        # is the common one and must not raise on the way out.
        self.application.broker.publish(SessionEvent(state="active"))
        self.application.broker.publish(CAPTION)
        await self.application.announce_maintenance()
        self.assertEqual(0, self.application.active_caption_count)
        self.assertIsNone(self.application.broker.latest_session)

        async def close_slow():
            return None

        subscriber, baseline, replay = self.application.broker.subscribe(
            self.application.broker.stream_id, 0, close_slow
        )
        try:
            self.assertEqual(self.application.broker.cursor, baseline)
            self.assertEqual([], replay)
        finally:
            self.application.broker.unsubscribe(subscriber)


class ShutdownWaitTests(unittest.IsolatedAsyncioTestCase):
    """serve_forever() used to hold this loop, and the relay vanished on
    Ctrl+C without a word. The replacement has to do three things: keep
    waiting, notice the signal, and put the previous handler back."""

    async def test_it_waits_until_the_signal_arrives(self):
        wait = asyncio.create_task(server._wait_for_shutdown())
        try:
            await asyncio.sleep(0.1)
            self.assertFalse(wait.done(), "the wait returned unprompted")
            signal.raise_signal(signal.SIGINT)
            await asyncio.wait_for(wait, timeout=5)
        finally:
            if not wait.done():
                wait.cancel()

    async def test_the_previous_handler_is_restored(self):
        def sentinel(signum, frame):
            raise AssertionError("the sentinel handler should not run")

        previous = signal.signal(signal.SIGINT, sentinel)
        try:
            wait = asyncio.create_task(server._wait_for_shutdown())
            await asyncio.sleep(0.05)
            # Ours is installed, not the sentinel.
            self.assertIsNot(sentinel, signal.getsignal(signal.SIGINT))
            signal.raise_signal(signal.SIGINT)
            await asyncio.wait_for(wait, timeout=5)
            # A relay that keeps our handler after shutting down would
            # swallow the next Ctrl+C too.
            self.assertIs(sentinel, signal.getsignal(signal.SIGINT))
        finally:
            signal.signal(signal.SIGINT, previous)


class ShutdownAnnouncementEndToEndTests(unittest.IsolatedAsyncioTestCase):
    """Through the real run_server, with the real signal.

    The unit tests either call announce_maintenance() directly or drive
    _wait_for_shutdown() on its own. Neither would catch the two wirings
    that matter most: that the signal actually reaches the wait, and that
    the announcement happens while the server is still listening. Closing
    one line later - after the context manager - sends nothing at all.
    """

    asyncSetUp = ServerTests.asyncSetUp
    asyncTearDown = ServerTests.asyncTearDown
    start_application = ServerTests.start_application

    @staticmethod
    def free_port():
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    async def test_the_stop_signal_pauses_every_viewer_then_exits(self):
        port = self.free_port()
        base = "ws://127.0.0.1:%d" % port
        serving = asyncio.create_task(
            server.run_server(self.application, "127.0.0.1", port)
        )
        try:
            for _ in range(200):
                try:
                    async with connect(
                        base + "/v1/captions", subprotocols=[SUBPROTOCOL]
                    ):
                        break
                except OSError:
                    await asyncio.sleep(0.02)
            else:
                self.fail("the relay never started listening")

            enrollment = self.store.create_enrollment(300)
            async with connect(
                base + "/v1/pair", subprotocols=[SUBPROTOCOL]
            ) as pairing:
                await pairing.send(
                    encode_message(
                        {
                            "type": "pair",
                            "enrollment_code": enrollment.code,
                            "device_name": "Raid desktop",
                            "client_version": "0.1.0",
                        }
                    )
                )
                paired = decode_message(await pairing.recv())
            await ServerTests.admit_viewer(self, paired)

            viewer = await connect(
                base + "/v1/captions", subprotocols=[SUBPROTOCOL]
            )
            await viewer.send(
                encode_message(
                    {
                        "type": "authenticate",
                        "device_id": paired["device_id"],
                        "device_token": paired["device_token"],
                        "last_stream_id": "",
                        "last_cursor": 0,
                    }
                )
            )
            self.assertEqual(
                "authenticated", decode_message(await viewer.recv())["type"]
            )

            # This is precisely what nssm does to stop the service: a
            # console Ctrl+C, which reaches Python as SIGINT.
            signal.raise_signal(signal.SIGINT)

            with self.assertRaises(ConnectionClosed) as caught:
                await asyncio.wait_for(viewer.recv(), timeout=10)
            self.assertEqual(1001, caught.exception.rcvd.code)
            self.assertIn("Paused for updates", caught.exception.rcvd.reason)

            # And it still has to actually stop.
            await asyncio.wait_for(serving, timeout=10)
        finally:
            if not serving.done():
                serving.cancel()
                await asyncio.gather(serving, return_exceptions=True)


class UpdateArtifactGateTests(unittest.TestCase):
    """An update has to be something the member can actually install.

    install_update() saves the offered bytes as Vemals-Ears-Setup.exe and
    hands them to Windows. The checksum proves the bytes are the ones the
    relay named - it cannot notice that what was named is an archive.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def published(self, name):
        path = self.root / name
        path.write_bytes(b"payload bytes")
        return str(path)

    def test_an_installer_is_advertised(self):
        for name in ("Vemals-Ears-Setup.exe", "Setup.MSI", "installer.Exe"):
            offer = server.update_offer(
                "0.5.1", self.published(name), "https://example.test/download"
            )
            self.assertIsNotNone(offer, name)
            self.assertEqual("0.5.1", offer["version"])

    def test_an_archive_is_never_advertised(self):
        # The exact shape of the trap: a stale vemals-ears-member.zip sits
        # in the same folder as the real installer, one config edit away.
        for name in ("vemals-ears-member.zip", "downtime.ZIP", "notes.txt", "payload"):
            self.assertIsNone(
                server.update_offer(
                    "0.5.1", self.published(name), "https://example.test/download"
                ),
                name,
            )

    def test_refusing_says_why(self):
        # The member's symptom is silence. The operator set that path
        # weeks ago and has nothing to connect it to.
        with self.assertLogs(server.LOGGER, level="WARNING") as captured:
            server.update_offer(
                "0.5.1",
                self.published("vemals-ears-member.zip"),
                "https://example.test/download",
            )
        self.assertTrue(
            any("installer" in line for line in captured.output), captured.output
        )

    def test_a_missing_installer_is_still_refused(self):
        self.assertIsNone(
            server.update_offer(
                "0.5.1",
                str(self.root / "absent.exe"),
                "https://example.test/download",
            )
        )

if __name__ == "__main__":
    unittest.main(verbosity=2)


class UpdateAdvertisementContractTests(unittest.IsolatedAsyncioTestCase):
    """The relay's authenticated frame and the client's validator are one
    contract, walked end to end through a REAL server.

    The first time a version was ever advertised on the live relay, the
    authenticated frame gained an "update" key and the client's
    exact-shape validator rejected the whole frame - every member's
    client reported "invalid authentication response" in a retry loop,
    and the feature that exists to ship fixes was what stopped anyone
    connecting. The unit suites all passed, because no test ever
    authenticated against a relay that was actually advertising.
    """

    asyncSetUp = ServerTests.asyncSetUp
    asyncTearDown = ServerTests.asyncTearDown
    start_application = ServerTests.start_application
    pair_code = ServerTests.pair_code
    pair = ServerTests.pair

    async def authenticated_frame(self):
        paired, _code = await self.pair()
        websocket = await connect(
            self.base_url + "/v1/captions", subprotocols=[SUBPROTOCOL]
        )
        try:
            await websocket.send(
                encode_message(
                    {
                        "type": "authenticate",
                        "device_id": paired["device_id"],
                        "device_token": paired["device_token"],
                        "last_stream_id": "",
                        "last_cursor": 0,
                    }
                )
            )
            return decode_message(await websocket.recv())
        finally:
            await websocket.close()

    async def test_a_quiet_relay_still_authenticates(self):
        from GuildHub.client import _authenticated_response, parse_update_offer

        frame = await self.authenticated_frame()
        self.assertNotIn("update", frame)
        stream_id, cursor = _authenticated_response(frame)
        self.assertTrue(stream_id)
        self.assertIsNone(parse_update_offer(frame, "0.1.0"))

    async def test_an_advertising_relay_still_authenticates(self):
        """The case production hit: an update offer must never cost the
        member their connection."""
        from GuildHub.client import _authenticated_response, parse_update_offer

        with tempfile.TemporaryDirectory() as folder:
            published = Path(folder) / "Vemals-Ears-Setup.exe"
            published.write_bytes(b"MZ pretend installer" + b"\x00" * 2048)
            self.application.config = dataclasses.replace(
                self.application.config,
                download_file=str(published),
                client_version="9.9.9",
            )
            frame = await self.authenticated_frame()
            expected_digest = hashlib.sha256(published.read_bytes()).hexdigest()

        self.assertIn("update", frame)
        # The frame must still authenticate...
        stream_id, _cursor = _authenticated_response(frame)
        self.assertTrue(stream_id)
        # ...and the offer inside it must be usable as-is.
        offer = parse_update_offer(frame, "0.1.0")
        self.assertIsNotNone(offer)
        self.assertEqual("9.9.9", offer["version"])
        self.assertEqual(expected_digest, offer["sha256"])

    async def test_pointer_offer_in_authenticated_frame_uses_public_exact_url(self):
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            legacy = folder / "Vemals-Ears-Setup.exe"
            legacy.write_bytes(b"MZ stale legacy")
            _installer, _source, pointer = DownloadRouteTests.publish_pointer(
                folder,
                "0.5.13",
                b"MZ immutable installer",
                b"PK immutable source",
            )
            self.application.config = dataclasses.replace(
                self.application.config,
                download_file=str(legacy),
                client_version="9.9.9",
                client_download_url="https://stale.invalid/wrong?old=1",
            )
            frame = await self.authenticated_frame()

        self.assertEqual(
            {
                "version": "0.5.13",
                "sha256": pointer["installer"]["sha256"],
                "bytes": pointer["installer"]["bytes"],
                "url": "https://ears.example.org/download/0.5.13",
            },
            frame["update"],
        )

    async def test_a_frame_with_an_unknown_extra_key_is_still_refused(self):
        """Only "update" was made optional; the shape check keeps teeth."""
        from GuildHub.client import _authenticated_response

        frame = await self.authenticated_frame()
        frame["surprise"] = 1
        from GuildHub.client import GuildHubClientError

        with self.assertRaises(GuildHubClientError):
            _authenticated_response(frame)


class ReportedVersionContractTests(unittest.IsolatedAsyncioTestCase):
    """A real device authenticating against a real relay, both shapes.

    The parser and the store are unit tested, but this system has twice
    shipped a wire change whose unit tests all passed because nothing
    ever exercised the actual handshake. The compatibility case here is
    the one that matters: a client sending the OLD frame - which is every
    client that exists today - must authenticate exactly as before.
    """

    asyncSetUp = ServerTests.asyncSetUp
    asyncTearDown = ServerTests.asyncTearDown
    start_application = ServerTests.start_application
    pair_code = ServerTests.pair_code
    pair = ServerTests.pair

    async def authenticate(self, paired, version=None):
        message = {
            "type": "authenticate",
            "device_id": paired["device_id"],
            "device_token": paired["device_token"],
            "last_stream_id": "",
            "last_cursor": 0,
        }
        if version is not None:
            message["client_version"] = version
        websocket = await connect(
            self.base_url + "/v1/captions", subprotocols=[SUBPROTOCOL]
        )
        try:
            await websocket.send(encode_message(message))
            return decode_message(await websocket.recv())
        finally:
            await websocket.close()

    def stored_version(self, device_id):
        for device in self.store.list_devices():
            if device.device_id == device_id:
                return device.client_version
        raise AssertionError("device vanished: " + device_id)

    async def test_a_client_sending_the_old_frame_still_connects(self):
        paired, _code = await self.pair()
        frame = await self.authenticate(paired)
        self.assertEqual("authenticated", frame.get("type"))
        self.assertNotIn("session", frame)
        # Untouched, not blanked: a quiet client must not erase what is
        # known about it.
        self.assertEqual("0.1.0", self.stored_version(paired["device_id"]))

    async def test_old_auth_frame_omits_an_active_session_snapshot(self):
        paired, _code = await self.pair()
        self.application.broker.publish(SessionEvent(state="active"))

        frame = await self.authenticate(paired)

        self.assertEqual("authenticated", frame.get("type"))
        self.assertNotIn("session", frame)

    async def test_versioned_late_join_receives_the_active_session_snapshot(self):
        paired, _code = await self.pair()
        self.application.broker.publish(SessionEvent(state="active"))

        frame = await self.authenticate(paired, "0.2.2")

        self.assertEqual("authenticated", frame.get("type"))
        self.assertEqual(
            {"type": "session", "state": "active"},
            frame.get("session"),
        )
        self.assertEqual(1, frame.get("cursor"))

    async def test_truncated_replay_keeps_snapshot_and_contiguous_cursors(self):
        paired, _code = await self.pair()
        self.application.broker.publish(SessionEvent(state="active"))
        for _index in range(10):
            self.application.broker.publish(CAPTION)

        websocket = await connect(
            self.base_url + "/v1/captions",
            subprotocols=[SUBPROTOCOL],
        )
        try:
            await websocket.send(
                encode_message(
                    {
                        "type": "authenticate",
                        "device_id": paired["device_id"],
                        "device_token": paired["device_token"],
                        "last_stream_id": self.application.broker.stream_id,
                        "last_cursor": 0,
                        "client_version": "0.2.2",
                    }
                )
            )
            frame = decode_message(await websocket.recv())
            replay = [
                decode_message(await websocket.recv())
                for _index in range(8)
            ]
        finally:
            await websocket.close()

        self.assertEqual(
            {"type": "session", "state": "active"},
            frame.get("session"),
        )
        self.assertEqual(3, frame.get("cursor"))
        self.assertEqual(list(range(4, 12)), [item["cursor"] for item in replay])

    async def test_a_reported_version_reaches_the_device_list(self):
        paired, _code = await self.pair()
        self.assertEqual("0.1.0", self.stored_version(paired["device_id"]))
        frame = await self.authenticate(paired, "0.2.2")
        self.assertEqual("authenticated", frame.get("type"))
        self.assertEqual("0.2.2", self.stored_version(paired["device_id"]))

    async def test_a_junk_version_is_refused_at_the_handshake(self):
        paired, _code = await self.pair()
        frame = await self.authenticate(paired, "x" * 400)
        self.assertNotEqual("authenticated", frame.get("type"))
        self.assertEqual("0.1.0", self.stored_version(paired["device_id"]))
