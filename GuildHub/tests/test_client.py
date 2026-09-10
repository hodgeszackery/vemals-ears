import asyncio
import json
import types
import unittest
from dataclasses import replace
from unittest import mock
from urllib.parse import urlencode

import GuildHub.client as client_module
from GuildHub.client import (
    GuildCaptionClient,
    GuildHubAuthenticationError,
    GuildHubClientError,
    GuildHubPairingCancelledError,
    GuildHubPairingConfirmationError,
    GuildHubCaptionsStoppedError,
    GuildHubVoiceChannelRequiredError,
    create_viewer_link,
    get_quick_chat_profile,
    pair_device,
    parse_pair_link,
    send_quick_chat,
    set_quick_chat_profile,
    watch_captions,
)
from GuildHub.credentials import DeviceCredentials
from GuildHub.protocol import (
    CAPTION_SUBPROTOCOLS,
    SUBPROTOCOL,
    SUBPROTOCOL_V2,
    caption_envelope,
    cursor_advance,
    decode_message,
    encode_message,
)
from VoiceCaptions.events import CaptionEvent, ClearEvent, SessionEvent
from VoiceCaptions.member_overlay import OverlaySink

DEVICE_ID = "device_123"
DEVICE_TOKEN = "ghd_private-device-token"
GUILD_ID = "guild_456"
GUILD_NAME = "Vemal's Ears"
SERVER_URL = "ws://127.0.0.1:8765"
STREAM_ONE = "stream_one"
STREAM_TWO = "stream_two"

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
ACTIVE_SESSION = SessionEvent(state="active")
UNKNOWN_SESSION = SessionEvent(
    state="unknown",
    message="Listening status unavailable",
)


def pairing_link(server=SERVER_URL, code="ghe_one-use-code"):
    return "vemalsears://pair?" + urlencode({"server": server, "code": code})


def paired_payload(**changes):
    payload = {
        "type": "paired",
        "device_id": DEVICE_ID,
        "device_token": DEVICE_TOKEN,
        "guild_id": GUILD_ID,
        "guild_name": GUILD_NAME,
    }
    payload.update(changes)
    return payload


def authenticated_payload(stream_id=STREAM_ONE, cursor=0, **changes):
    payload = {
        "type": "authenticated",
        "stream_id": stream_id,
        "cursor": cursor,
    }
    payload.update(changes)
    return payload


def device_credentials():
    return DeviceCredentials(
        server_url=SERVER_URL,
        device_id=DEVICE_ID,
        device_token=DEVICE_TOKEN,
        guild_id=GUILD_ID,
        guild_name=GUILD_NAME,
    )


class MemoryStore:
    def __init__(self, value=None):
        self.value = value
        self.saved = []

    def save(self, value):
        self.value = value
        self.saved.append(value)

    def load(self):
        return self.value

    def delete(self):
        self.value = None


class MemberOverlayImportTests(unittest.TestCase):
    def test_client_uses_shared_member_overlay_sink(self):
        self.assertIs(OverlaySink, client_module.OverlaySink)


class FakeSocket:
    def __init__(self, incoming=(), *, subprotocol=SUBPROTOCOL):
        self.incoming = list(incoming)
        self.subprotocol = subprotocol
        self.sent = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.closed = True

    async def send(self, payload):
        self.sent.append(payload)

    async def recv(self):
        if not self.incoming:
            raise OSError("connection ended")
        return self.incoming.pop(0)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.incoming:
            raise StopAsyncIteration
        return self.incoming.pop(0)

    async def close(self, **_kwargs):
        self.closed = True


class BlockingSocket(FakeSocket):
    def __init__(self, incoming=(), *, subprotocol=SUBPROTOCOL):
        super().__init__(incoming, subprotocol=subprotocol)
        self.iterating = asyncio.Event()
        self.close_requested = asyncio.Event()

    async def __anext__(self):
        if self.incoming:
            return self.incoming.pop(0)
        self.iterating.set()
        await self.close_requested.wait()
        raise StopAsyncIteration

    async def close(self, **_kwargs):
        self.closed = True
        self.close_requested.set()


class FakeConnector:
    def __init__(self, *sockets):
        self.sockets = list(sockets)
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.sockets:
            raise OSError("no endpoint")
        return self.sockets.pop(0)


class FailingConnector:
    def __init__(self):
        self.calls = 0

    def __call__(self, _url, **_kwargs):
        self.calls += 1
        raise OSError("offline")


class RecordingSink:
    def __init__(self, accepted=True):
        self.accepted = accepted
        self.events = []

    def send(self, event):
        self.events.append(event)
        return self.accepted


class ClosableRecordingSink(RecordingSink):
    def __init__(self, accepted=True):
        super().__init__(accepted)
        self.closed = False

    def close(self):
        self.closed = True


class PairLinkTests(unittest.TestCase):
    def test_parses_canonical_pair_link_and_redacts_repr(self):
        parsed = parse_pair_link(pairing_link("wss://Guild.Example/"))

        self.assertEqual("wss://guild.example", parsed.server_url)
        self.assertEqual("ghe_one-use-code", parsed.enrollment_code)
        self.assertNotIn(parsed.enrollment_code, repr(parsed))
        self.assertNotIn(parsed.server_url, repr(parsed))

    def test_rejects_bad_scheme_fragment_userinfo_and_wrong_action(self):
        rejected = (
            pairing_link().replace("vemalsears:", "guildhub:", 1),
            pairing_link() + "#private",
            pairing_link().replace("//pair", "//user@pair", 1),
            pairing_link().replace("//pair", "//run", 1),
        )
        for value in rejected:
            with self.subTest(value=value):
                with self.assertRaises(GuildHubClientError):
                    parse_pair_link(value)

    def test_rejects_duplicates_unknown_fields_and_unsafe_codes(self):
        rejected = (
            pairing_link() + "&code=second",
            pairing_link() + "&extra=value",
            pairing_link(code="secret code"),
            "vemalsears://pair?server=ws%3A%2F%2F127.0.0.1",
        )
        for value in rejected:
            with self.subTest(value=value):
                with self.assertRaises(GuildHubClientError):
                    parse_pair_link(value)

    def test_rejects_unencrypted_remote_server(self):
        with self.assertRaises(GuildHubClientError):
            parse_pair_link(pairing_link("ws://guild.example"))


class PairDeviceTests(unittest.IsolatedAsyncioTestCase):
    async def test_pair_uses_protocol_and_saves_only_strict_response(self):
        socket = FakeSocket([encode_message(paired_payload())])
        connector = FakeConnector(socket)
        store = MemoryStore()

        result = await pair_device(
            pairing_link(),
            store,
            device_name="Zack's desktop",
            connector=connector,
        )

        self.assertEqual(device_credentials(), result)
        self.assertEqual([result], store.saved)
        sent = decode_message(socket.sent[0])
        self.assertEqual("pair", sent["type"])
        self.assertEqual("ghe_one-use-code", sent["enrollment_code"])
        self.assertEqual("Zack's desktop", sent["device_name"])
        self.assertEqual("ws://127.0.0.1:8765/v1/pair", connector.calls[0][0])
        self.assertEqual([SUBPROTOCOL], connector.calls[0][1]["subprotocols"])

    async def test_default_device_name_never_sends_the_windows_hostname(self):
        socket = FakeSocket([encode_message(paired_payload())])

        await pair_device(
            pairing_link(),
            MemoryStore(),
            connector=FakeConnector(socket),
        )

        sent = decode_message(socket.sent[0])
        self.assertEqual("Windows PC", sent["device_name"])

    async def test_confirmation_receives_new_and_existing_device_before_save(self):
        existing = DeviceCredentials(
            server_url="wss://old-guild.example",
            device_id="old_device",
            device_token="ghd_old-device-token",
            guild_id="old_guild",
            guild_name="Old Guild",
        )
        store = MemoryStore(existing)
        received = []

        def confirm(new_credentials, existing_credentials):
            received.append((new_credentials, existing_credentials, list(store.saved)))
            return True

        result = await pair_device(
            pairing_link(),
            store,
            connector=FakeConnector(
                FakeSocket([encode_message(paired_payload())])
            ),
            confirm=confirm,
        )

        self.assertEqual([(result, existing, [])], received)
        self.assertEqual([result], store.saved)

    async def test_confirmation_is_not_called_for_invalid_pairing_response(self):
        called = []
        store = MemoryStore(device_credentials())

        with self.assertRaises(GuildHubClientError):
            await pair_device(
                pairing_link(),
                store,
                connector=FakeConnector(
                    FakeSocket(
                        [encode_message(paired_payload(extra="not allowed"))]
                    )
                ),
                confirm=lambda _new, _existing: called.append(True) or True,
            )

        self.assertEqual([], called)
        self.assertEqual([], store.saved)

    async def test_cancelled_confirmation_never_saves_new_device(self):
        store = MemoryStore(device_credentials())

        with self.assertRaises(GuildHubPairingCancelledError) as caught:
            await pair_device(
                pairing_link(),
                store,
                connector=FakeConnector(
                    FakeSocket([encode_message(paired_payload())])
                ),
                confirm=lambda _new, _existing: False,
            )

        self.assertEqual([], store.saved)
        self.assertNotIn(DEVICE_TOKEN, str(caught.exception))

    async def test_confirmation_exception_is_redacted_and_never_saves(self):
        private = "ghe_private-confirmation-detail"
        store = MemoryStore(device_credentials())

        def fail_confirmation(_new, _existing):
            raise RuntimeError(private)

        with self.assertRaises(GuildHubPairingConfirmationError) as caught:
            await pair_device(
                pairing_link(),
                store,
                connector=FakeConnector(
                    FakeSocket([encode_message(paired_payload())])
                ),
                confirm=fail_confirmation,
            )

        self.assertEqual([], store.saved)
        self.assertNotIn(private, str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    async def test_confirmation_result_conversion_is_also_redacted(self):
        private = "private-confirmation-result"
        store = MemoryStore(device_credentials())

        class UnsafeDecision:
            def __bool__(self):
                raise RuntimeError(private)

        with self.assertRaises(GuildHubPairingConfirmationError) as caught:
            await pair_device(
                pairing_link(),
                store,
                connector=FakeConnector(
                    FakeSocket([encode_message(paired_payload())])
                ),
                confirm=lambda _new, _existing: UnsafeDecision(),
            )

        self.assertEqual([], store.saved)
        self.assertNotIn(private, str(caught.exception))

    async def test_pair_rejects_extra_response_field_without_saving(self):
        payload = paired_payload(extra="not allowed")
        store = MemoryStore()

        with self.assertRaises(GuildHubClientError):
            await pair_device(
                pairing_link(),
                store,
                connector=FakeConnector(FakeSocket([encode_message(payload)])),
            )

        self.assertEqual([], store.saved)

    async def test_pair_rejection_never_echoes_server_message_or_code(self):
        private = "ghe_one-use-code"
        payload = {
            "type": "error",
            "code": "invalid_code",
            "message": private,
        }

        with self.assertRaises(GuildHubClientError) as caught:
            await pair_device(
                pairing_link(code=private),
                MemoryStore(),
                connector=FakeConnector(FakeSocket([json.dumps(payload)])),
            )

        self.assertNotIn(private, str(caught.exception))

    async def test_pair_requires_negotiated_subprotocol(self):
        socket = FakeSocket([encode_message(paired_payload())], subprotocol=None)

        with self.assertRaises(GuildHubClientError):
            await pair_device(
                pairing_link(), MemoryStore(), connector=FakeConnector(socket)
            )


class QuickChatClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_sends_one_authenticated_line_and_requires_acknowledgement(self):
        request_id = "client_request_123456"
        socket = FakeSocket(
            [
                encode_message(
                    {
                        "type": "quick_chat_sent",
                        "request_id": request_id,
                        "cursor": 9,
                    }
                )
            ]
        )
        connector = FakeConnector(socket)

        acknowledgement = await send_quick_chat(
            MemoryStore(device_credentials()),
            "Use the gateway.",
            request_id=request_id,
            connector=connector,
        )

        self.assertEqual(9, acknowledgement.cursor)
        self.assertEqual(request_id, acknowledgement.request_id)
        self.assertEqual(
            "ws://127.0.0.1:8765/v1/quick-chat",
            connector.calls[0][0],
        )
        request = decode_message(socket.sent[0])
        self.assertEqual("quick_chat", request["type"])
        self.assertEqual(DEVICE_ID, request["device_id"])
        self.assertEqual(DEVICE_TOKEN, request["device_token"])
        self.assertEqual(request_id, request["request_id"])
        self.assertEqual("Use the gateway.", request["text"])

    async def test_requires_pairing_without_opening_a_connection(self):
        connector = FakeConnector()
        with self.assertRaises(GuildHubClientError):
            await send_quick_chat(
                MemoryStore(),
                "Ready.",
                request_id="client_request_123456",
                connector=connector,
            )
        self.assertEqual([], connector.calls)

    async def test_relay_rejection_is_actionable_without_echoing_the_line(self):
        private_line = "private typed raid line"
        socket = FakeSocket(
            [
                encode_message(
                    {
                        "type": "error",
                        "code": "rate_limited",
                        "message": "Wait a moment before sending another quick-chat message.",
                    }
                )
            ]
        )
        with self.assertRaises(GuildHubClientError) as caught:
            await send_quick_chat(
                MemoryStore(device_credentials()),
                private_line,
                request_id="client_request_123456",
                connector=FakeConnector(socket),
            )
        self.assertIn("Wait a moment", str(caught.exception))
        self.assertNotIn(private_line, str(caught.exception))

    async def test_network_and_malformed_responses_never_echo_the_line(self):
        private_line = "private typed raid line"
        connectors = (
            FailingConnector(),
            FakeConnector(FakeSocket([encode_message({"type": "wrong"})])),
        )
        for connector in connectors:
            with self.subTest(connector=type(connector).__name__):
                with self.assertRaises(GuildHubClientError) as caught:
                    await send_quick_chat(
                        MemoryStore(device_credentials()),
                        private_line,
                        request_id="client_request_123456",
                        connector=connector,
                    )
                self.assertNotIn(private_line, str(caught.exception))

    async def test_acknowledgement_must_match_the_request_id(self):
        socket = FakeSocket(
            [
                encode_message(
                    {
                        "type": "quick_chat_sent",
                        "request_id": "different_request_123456",
                        "cursor": 9,
                    }
                )
            ]
        )
        with self.assertRaises(GuildHubClientError):
            await send_quick_chat(
                MemoryStore(device_credentials()),
                "Ready.",
                request_id="client_request_123456",
                connector=FakeConnector(socket),
            )


class QuickChatProfileClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_an_unassigned_profile_with_saved_device_credentials(self):
        socket = FakeSocket(
            [encode_message({"type": "quick_chat_profile", "name": None})]
        )
        connector = FakeConnector(socket)

        profile = await get_quick_chat_profile(
            MemoryStore(device_credentials()), connector=connector
        )

        self.assertIsNone(profile.quick_chat_name)
        self.assertEqual(
            "ws://127.0.0.1:8765/v1/quick-chat-profile",
            connector.calls[0][0],
        )
        self.assertEqual(
            {
                "type": "quick_chat_profile",
                "device_id": DEVICE_ID,
                "device_token": DEVICE_TOKEN,
            },
            decode_message(socket.sent[0]),
        )

    async def test_updates_and_returns_the_server_canonical_name(self):
        socket = FakeSocket(
            [encode_message({"type": "quick_chat_profile", "name": "Mira Lyn"})]
        )
        connector = FakeConnector(socket)
        submitted = "  \uff2dira   Lyn  "

        profile = await set_quick_chat_profile(
            MemoryStore(device_credentials()),
            submitted,
            connector=connector,
        )

        self.assertEqual("Mira Lyn", profile.quick_chat_name)
        request = decode_message(socket.sent[0])
        self.assertEqual("quick_chat_profile", request["type"])
        self.assertEqual(DEVICE_ID, request["device_id"])
        self.assertEqual(DEVICE_TOKEN, request["device_token"])
        self.assertEqual(submitted, request["name"])

    async def test_profile_requires_pairing_without_opening_a_connection(self):
        connector = FakeConnector()
        with self.assertRaises(GuildHubClientError):
            await get_quick_chat_profile(MemoryStore(), connector=connector)
        self.assertEqual([], connector.calls)

    async def test_invalid_name_is_rejected_before_opening_a_connection(self):
        connector = FakeConnector()
        private_name = "Mira\u202eAdmin"
        with self.assertRaises(GuildHubClientError) as caught:
            await set_quick_chat_profile(
                MemoryStore(device_credentials()),
                private_name,
                connector=connector,
            )
        self.assertEqual([], connector.calls)
        self.assertNotIn(private_name, str(caught.exception))

    async def test_authentication_failure_is_actionable_and_redacted(self):
        socket = FakeSocket(
            [
                encode_message(
                    {
                        "type": "error",
                        "code": "authentication_failed",
                        "message": "Authentication failed.",
                    }
                )
            ]
        )
        with self.assertRaises(GuildHubAuthenticationError) as caught:
            await set_quick_chat_profile(
                MemoryStore(device_credentials()),
                "Private name",
                connector=FakeConnector(socket),
            )
        self.assertNotIn(DEVICE_TOKEN, str(caught.exception))
        self.assertNotIn("Private name", str(caught.exception))

    async def test_storage_failure_does_not_claim_the_device_was_revoked(self):
        socket = FakeSocket(
            [
                encode_message(
                    {
                        "type": "error",
                        "code": "profile_update_failed",
                        "message": (
                            "The typed-chat profile could not be saved. Try again."
                        ),
                    }
                )
            ]
        )
        private_name = "Private name"
        with self.assertRaises(GuildHubClientError) as caught:
            await set_quick_chat_profile(
                MemoryStore(device_credentials()),
                private_name,
                connector=FakeConnector(socket),
            )
        message = str(caught.exception)
        self.assertIn("Try again", message)
        self.assertNotIn("authorized", message)
        self.assertNotIn("Pair", message)
        self.assertNotIn(DEVICE_TOKEN, message)
        self.assertNotIn(private_name, message)

    async def test_update_rejects_a_null_or_malformed_profile_response(self):
        responses = (
            {"type": "quick_chat_profile", "name": None},
            {"type": "quick_chat_profile", "name": "Mira", "extra": True},
        )
        for response in responses:
            with self.subTest(response=response), self.assertRaises(
                GuildHubClientError
            ):
                await set_quick_chat_profile(
                    MemoryStore(device_credentials()),
                    "Mira",
                    connector=FakeConnector(FakeSocket([encode_message(response)])),
                )


class ViewerLinkClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_creates_one_authenticated_short_lived_discord_link_code(self):
        socket = FakeSocket(
            [
                encode_message(
                    {
                        "type": "viewer_link",
                        "code": "7KM4P2QX",
                        "expires_at": 123456,
                    }
                )
            ]
        )
        connector = FakeConnector(socket)

        link = await create_viewer_link(
            MemoryStore(device_credentials()), connector=connector
        )

        self.assertEqual("7KM4P2QX", link.code)
        self.assertEqual(123456, link.expires_at)
        self.assertEqual(
            "ws://127.0.0.1:8765/v1/viewer-link",
            connector.calls[0][0],
        )
        self.assertEqual(
            {
                "type": "viewer_link_request",
                "device_id": DEVICE_ID,
                "device_token": DEVICE_TOKEN,
            },
            decode_message(socket.sent[0]),
        )

    async def test_requires_pairing_without_opening_a_connection(self):
        connector = FakeConnector()
        with self.assertRaises(GuildHubClientError):
            await create_viewer_link(MemoryStore(), connector=connector)
        self.assertEqual([], connector.calls)

    async def test_malformed_response_and_authentication_failure_are_distinct(self):
        cases = (
            (
                {"type": "viewer_link", "code": "bad-code", "expires_at": 1},
                GuildHubClientError,
            ),
            (
                {
                    "type": "error",
                    "code": "authentication_failed",
                    "message": "Authentication failed.",
                },
                GuildHubAuthenticationError,
            ),
        )
        for response, expected in cases:
            with self.subTest(response=response), self.assertRaises(expected):
                await create_viewer_link(
                    MemoryStore(device_credentials()),
                    connector=FakeConnector(
                        FakeSocket([encode_message(response)])
                    ),
                )


class AuthenticationResponseTests(unittest.TestCase):
    def test_optional_session_coexists_with_an_update_offer(self):
        update = {
            "version": "0.3.0",
            "sha256": "a" * 64,
            "bytes": 1024,
            "url": "https://ears.example.invalid/download",
        }
        for extras in (
            {"session": {"type": "session", "state": "active"}},
            {
                "session": {"type": "session", "state": "active"},
                "update": update,
            },
        ):
            with self.subTest(keys=tuple(extras)):
                payload = authenticated_payload(**extras)
                stream_id, cursor = client_module._authenticated_response(payload)
                snapshot = client_module._session_snapshot(payload["session"])

                self.assertEqual(STREAM_ONE, stream_id)
                self.assertEqual(0, cursor)
                self.assertEqual(ACTIVE_SESSION, snapshot)

    def test_present_session_snapshot_is_strictly_validated(self):
        invalid = (
            "active",
            {"type": "caption", "state": "active"},
            {"type": "session"},
            {"type": "session", "state": "not-a-state"},
            {"type": "session", "state": "active", "extra": True},
            None,
            {"type": "session", "state": "active", "message": 7},
        )
        for session in invalid:
            with self.subTest(session=session):
                with self.assertRaises(GuildHubClientError):
                    client_module._authenticated_response(
                        authenticated_payload(session=session)
                    )


class CaptionClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_caption_stream_offers_v2_then_v1_and_keeps_v2_style(self):
        styled = replace(CAPTION, speech_style="shout")
        socket = FakeSocket(
            [
                encode_message(authenticated_payload(cursor=0)),
                encode_message(
                    caption_envelope(
                        STREAM_ONE,
                        1,
                        styled,
                        subprotocol=SUBPROTOCOL_V2,
                    )
                ),
            ],
            subprotocol=SUBPROTOCOL_V2,
        )
        connector = FakeConnector(socket)
        sink = RecordingSink()
        client = GuildCaptionClient(
            MemoryStore(device_credentials()),
            sink,
            connector=connector,
        )

        self.assertEqual(1, await client._consume_once(device_credentials()))

        self.assertEqual(
            list(CAPTION_SUBPROTOCOLS),
            connector.calls[0][1]["subprotocols"],
        )
        self.assertEqual("shout", sink.events[-1].speech_style)

    async def test_caption_stream_accepts_v1_fallback(self):
        socket = FakeSocket(
            [
                encode_message(authenticated_payload(cursor=0)),
                encode_message(caption_envelope(STREAM_ONE, 1, CAPTION)),
            ],
            subprotocol=SUBPROTOCOL,
        )
        connector = FakeConnector(socket)
        sink = RecordingSink()
        client = GuildCaptionClient(
            MemoryStore(device_credentials()),
            sink,
            connector=connector,
        )

        self.assertEqual(1, await client._consume_once(device_credentials()))
        self.assertEqual(
            list(CAPTION_SUBPROTOCOLS),
            connector.calls[0][1]["subprotocols"],
        )
        self.assertEqual(CAPTION, sink.events[-1])

    async def test_caption_stream_requires_a_negotiated_version(self):
        socket = FakeSocket(
            [encode_message(authenticated_payload(cursor=0))],
            subprotocol=None,
        )
        client = GuildCaptionClient(
            MemoryStore(device_credentials()),
            RecordingSink(),
            connector=FakeConnector(socket),
        )

        with self.assertRaises(GuildHubClientError):
            await client._consume_once(device_credentials())
        self.assertEqual([], socket.sent)

    async def test_leaving_voice_midstream_clears_text_and_keeps_access_error(self):
        socket = FakeSocket(
            [
                encode_message(authenticated_payload(cursor=0)),
                encode_message(caption_envelope(STREAM_ONE, 1, CAPTION)),
                encode_message(
                    {
                        "type": "error",
                        "code": "voice_channel_required",
                        "message": "Join the active Discord voice channel to view captions.",
                    }
                ),
            ]
        )
        sink = RecordingSink()
        client = GuildCaptionClient(
            MemoryStore(device_credentials()),
            sink,
            connector=FakeConnector(socket),
        )

        with self.assertRaises(GuildHubVoiceChannelRequiredError):
            await client._consume_once(device_credentials())

        self.assertEqual(CAPTION, sink.events[-3])
        self.assertIsInstance(sink.events[-2], ClearEvent)
        self.assertEqual("paused", sink.events[-1].state)
        self.assertEqual(
            "Captions paused - join the active Discord voice channel. If this device "
            "was paired without an invitation made for your Discord account, pair it "
            "again with one.",
            sink.events[-1].message,
        )
        self.assertNotIn("linked", sink.events[-1].message.lower())

    async def test_a_stopped_worker_does_not_tell_the_member_they_left(self):
        """Shino, 2026-09-07. They never moved; the worker stopped."""
        socket = FakeSocket(
            [
                encode_message(authenticated_payload(cursor=0)),
                encode_message(caption_envelope(STREAM_ONE, 1, CAPTION)),
                encode_message(
                    {
                        "type": "error",
                        "code": "captions_stopped",
                        "message": "Vemal's Ears is not running. Captions return when it starts.",
                    }
                ),
            ]
        )
        sink = RecordingSink()
        client = GuildCaptionClient(
            MemoryStore(device_credentials()),
            sink,
            connector=FakeConnector(socket),
        )

        with self.assertRaises(GuildHubCaptionsStoppedError):
            await client._consume_once(device_credentials())

        self.assertEqual(CAPTION, sink.events[-3])
        self.assertIsInstance(sink.events[-2], ClearEvent)
        self.assertEqual("paused", sink.events[-1].state)
        self.assertIn("not running", sink.events[-1].message)
        self.assertNotIn("you left", sink.events[-1].message)

    async def test_the_stopped_error_is_handled_wherever_the_voice_one_is(self):
        """A subclass, so every retry and wait path already covers it.

        If it stopped being one, the reconnect loop would fall through to
        the generic handler and the window would report a fault instead
        of waiting for the worker to come back.
        """
        self.assertTrue(
            issubclass(
                GuildHubCaptionsStoppedError, GuildHubVoiceChannelRequiredError
            )
        )

    async def test_a_stopped_worker_is_named_at_the_handshake_too(self):
        socket = FakeSocket(
            [
                encode_message(
                    {
                        "type": "error",
                        "code": "captions_stopped",
                        "message": "Vemal's Ears is not running. Captions return when it starts.",
                    }
                ),
            ]
        )
        sink = RecordingSink()
        client = GuildCaptionClient(
            MemoryStore(device_credentials()),
            sink,
            connector=FakeConnector(socket),
        )

        with self.assertRaises(GuildHubCaptionsStoppedError):
            await client._consume_once(device_credentials())
        self.assertEqual("paused", sink.events[-1].state)
        self.assertIn("not running", sink.events[-1].message)

    async def test_private_cursor_advance_preserves_continuity_without_display(self):
        socket = FakeSocket(
            [
                encode_message(authenticated_payload(cursor=0)),
                encode_message(cursor_advance(STREAM_ONE, 1)),
                encode_message(caption_envelope(STREAM_ONE, 2, CAPTION)),
            ]
        )
        sink = RecordingSink()
        client = GuildCaptionClient(
            MemoryStore(device_credentials()),
            sink,
            connector=FakeConnector(socket),
        )

        self.assertEqual(1, await client._consume_once(device_credentials()))

        self.assertEqual(2, client.cursor)
        self.assertIsInstance(sink.events[0], ClearEvent)
        self.assertEqual([UNKNOWN_SESSION, CAPTION], sink.events[1:])

    async def test_voice_channel_denial_clears_prior_text_without_revoking_pairing(self):
        socket = FakeSocket(
            [
                encode_message(
                    {
                        "type": "error",
                        "code": "voice_channel_required",
                        "message": "Join the active Discord voice channel to view captions.",
                    }
                )
            ]
        )
        sink = RecordingSink()
        client = GuildCaptionClient(
            MemoryStore(device_credentials()),
            sink,
            connector=FakeConnector(socket),
        )

        with self.assertRaises(GuildHubVoiceChannelRequiredError):
            await client._consume_once(device_credentials())

        self.assertIsInstance(sink.events[0], ClearEvent)
        self.assertEqual("paused", sink.events[1].state)
        self.assertIn("voice channel", sink.events[1].message)

    async def test_authenticate_reports_the_running_client_version(self):
        socket = FakeSocket(
            [
                encode_message(
                    authenticated_payload(
                        session={"type": "session", "state": "ready"}
                    )
                )
            ]
        )
        client = GuildCaptionClient(
            MemoryStore(device_credentials()),
            RecordingSink(),
            connector=FakeConnector(socket),
        )

        await client._consume_once(device_credentials())

        authentication = decode_message(socket.sent[0])
        self.assertEqual(client_module.CLIENT_VERSION, authentication["client_version"])

    async def test_discontinuity_orders_clear_snapshot_then_caption(self):
        socket = FakeSocket(
            [
                encode_message(
                    authenticated_payload(
                        session={"type": "session", "state": "active"}
                    )
                ),
                encode_message(caption_envelope(STREAM_ONE, 1, CAPTION)),
            ]
        )
        sink = RecordingSink()
        client = GuildCaptionClient(
            MemoryStore(device_credentials()),
            sink,
            connector=FakeConnector(socket),
        )

        self.assertEqual(1, await client._consume_once(device_credentials()))

        self.assertIsInstance(sink.events[0], ClearEvent)
        self.assertEqual(
            [ACTIVE_SESSION, CAPTION, UNKNOWN_SESSION],
            sink.events[1:],
        )

    async def test_disconnect_replaces_known_session_with_local_unknown(self):
        socket = FakeSocket(
            [
                encode_message(
                    authenticated_payload(
                        session={"type": "session", "state": "active"}
                    )
                )
            ]
        )
        sink = RecordingSink()
        client = GuildCaptionClient(
            MemoryStore(device_credentials()),
            sink,
            connector=FakeConnector(socket),
        )
        client._stream_id = STREAM_ONE

        self.assertEqual(0, await client._consume_once(device_credentials()))

        self.assertEqual([ACTIVE_SESSION, UNKNOWN_SESSION], sink.events)

    async def test_caption_is_strictly_validated_sent_and_cursor_resumed(self):
        first = FakeSocket(
            [
                encode_message(authenticated_payload(cursor=0)),
                encode_message(caption_envelope(STREAM_ONE, 1, CAPTION)),
            ]
        )
        second_caption = CaptionEvent(
            speaker_id="discord-user-2",
            speaker="Tess",
            character="",
            role="DAMAGE",
            text="Stack now.",
            final=True,
            sequence=1,
            timestamp=13.0,
        )
        second = FakeSocket(
            [
                encode_message(authenticated_payload(cursor=1)),
                encode_message(caption_envelope(STREAM_ONE, 2, second_caption)),
            ]
        )
        connector = FakeConnector(first, second)
        sink = RecordingSink()
        client = GuildCaptionClient(
            MemoryStore(device_credentials()), sink, connector=connector
        )

        self.assertEqual(1, await client._consume_once(device_credentials()))
        self.assertEqual(1, await client._consume_once(device_credentials()))

        self.assertIsInstance(sink.events[0], ClearEvent)
        self.assertEqual(
            [UNKNOWN_SESSION, CAPTION, second_caption],
            sink.events[1:],
        )
        self.assertEqual(STREAM_ONE, client.stream_id)
        self.assertEqual(2, client.cursor)
        first_auth = decode_message(first.sent[0])
        second_auth = decode_message(second.sent[0])
        self.assertEqual("", first_auth["last_stream_id"])
        self.assertEqual(0, first_auth["last_cursor"])
        self.assertEqual(STREAM_ONE, second_auth["last_stream_id"])
        self.assertEqual(1, second_auth["last_cursor"])

    async def test_new_stream_clears_overlay_and_uses_authenticated_baseline(self):
        socket = FakeSocket(
            [
                encode_message(authenticated_payload(STREAM_TWO, cursor=4)),
                encode_message(caption_envelope(STREAM_TWO, 5, CAPTION)),
            ]
        )
        sink = RecordingSink()
        client = GuildCaptionClient(
            MemoryStore(device_credentials()), sink, connector=FakeConnector(socket)
        )
        client._stream_id = STREAM_ONE
        client._cursor = 9

        self.assertEqual(1, await client._consume_once(device_credentials()))

        authentication = decode_message(socket.sent[0])
        self.assertEqual(STREAM_ONE, authentication["last_stream_id"])
        self.assertEqual(9, authentication["last_cursor"])
        self.assertEqual(STREAM_TWO, client.stream_id)
        self.assertEqual(5, client.cursor)
        self.assertIsInstance(sink.events[0], ClearEvent)
        self.assertEqual([UNKNOWN_SESSION, CAPTION], sink.events[1:])

    async def test_truncated_replay_baseline_clears_before_continuing_same_stream(self):
        socket = FakeSocket(
            [
                encode_message(authenticated_payload(cursor=6)),
                encode_message(caption_envelope(STREAM_ONE, 7, CAPTION)),
            ]
        )
        sink = RecordingSink()
        client = GuildCaptionClient(
            MemoryStore(device_credentials()), sink, connector=FakeConnector(socket)
        )
        client._stream_id = STREAM_ONE
        client._cursor = 2

        self.assertEqual(1, await client._consume_once(device_credentials()))

        self.assertIsInstance(sink.events[0], ClearEvent)
        self.assertEqual([UNKNOWN_SESSION, CAPTION], sink.events[1:])
        self.assertEqual(7, client.cursor)

    async def test_cursor_gap_is_rejected_without_delivering_caption(self):
        socket = FakeSocket(
            [
                encode_message(authenticated_payload(cursor=0)),
                encode_message(caption_envelope(STREAM_ONE, 2, CAPTION)),
            ]
        )
        sink = RecordingSink()
        client = GuildCaptionClient(
            MemoryStore(device_credentials()), sink, connector=FakeConnector(socket)
        )
        client._stream_id = STREAM_ONE

        with self.assertRaises(GuildHubClientError):
            await client._consume_once(device_credentials())

        self.assertEqual([UNKNOWN_SESSION], sink.events)
        self.assertEqual(0, client.cursor)

    async def test_envelope_from_different_stream_is_rejected(self):
        socket = FakeSocket(
            [
                encode_message(authenticated_payload(cursor=0)),
                encode_message(caption_envelope(STREAM_TWO, 1, CAPTION)),
            ]
        )
        sink = RecordingSink()
        client = GuildCaptionClient(
            MemoryStore(device_credentials()), sink, connector=FakeConnector(socket)
        )
        client._stream_id = STREAM_ONE

        with self.assertRaises(GuildHubClientError):
            await client._consume_once(device_credentials())

        self.assertEqual([UNKNOWN_SESSION], sink.events)
        self.assertEqual(STREAM_ONE, client.stream_id)
        self.assertEqual(0, client.cursor)

    async def test_authentication_response_requires_exact_stream_epoch_fields(self):
        invalid_responses = (
            {"type": "authenticated", "cursor": 0},
            authenticated_payload(extra="not allowed"),
            authenticated_payload(stream_id="bad stream"),
        )
        for payload in invalid_responses:
            with self.subTest(payload=payload):
                client = GuildCaptionClient(
                    MemoryStore(device_credentials()),
                    RecordingSink(),
                    connector=FakeConnector(
                        FakeSocket([encode_message(payload)])
                    ),
                )
                with self.assertRaises(GuildHubClientError):
                    await client._consume_once(device_credentials())

    async def test_failed_sink_does_not_advance_cursor(self):
        socket = FakeSocket(
            [
                encode_message(authenticated_payload(cursor=0)),
                encode_message(caption_envelope(STREAM_ONE, 1, CAPTION)),
            ]
        )
        sink = RecordingSink(accepted=False)
        client = GuildCaptionClient(
            MemoryStore(device_credentials()), sink, connector=FakeConnector(socket)
        )
        client._stream_id = STREAM_ONE

        with self.assertRaises(GuildHubClientError):
            await client._consume_once(device_credentials())

        self.assertEqual(0, client.cursor)

    async def test_connection_status_reports_authentication_and_disconnect(self):
        statuses = []
        socket = FakeSocket([encode_message(authenticated_payload(cursor=0))])
        client = GuildCaptionClient(
            MemoryStore(device_credentials()),
            RecordingSink(),
            connector=FakeConnector(socket),
            connection_status=statuses.append,
        )

        self.assertEqual(0, await client._consume_once(device_credentials()))

        self.assertEqual([True, False], statuses)

    async def test_connection_status_exception_never_stops_captions(self):
        calls = []

        def broken_status(authenticated):
            calls.append(authenticated)
            raise RuntimeError("private UI detail")

        socket = FakeSocket(
            [
                encode_message(authenticated_payload(cursor=0)),
                encode_message(caption_envelope(STREAM_ONE, 1, CAPTION)),
            ]
        )
        sink = RecordingSink()
        client = GuildCaptionClient(
            MemoryStore(device_credentials()),
            sink,
            connector=FakeConnector(socket),
            connection_status=broken_status,
        )

        self.assertEqual(1, await client._consume_once(device_credentials()))

        self.assertEqual([True, False], calls)
        self.assertIsInstance(sink.events[0], ClearEvent)
        self.assertEqual([UNKNOWN_SESSION, CAPTION], sink.events[1:])

    async def test_authentication_error_is_generic_and_not_retried(self):
        socket = FakeSocket(
            [
                encode_message(
                    {
                        "type": "error",
                        "code": "authentication_failed",
                        "message": DEVICE_TOKEN,
                    }
                )
            ]
        )
        connector = FakeConnector(socket)
        client = GuildCaptionClient(
            MemoryStore(device_credentials()),
            RecordingSink(),
            connector=connector,
            retry_initial=0,
            retry_max=0,
        )

        with self.assertRaises(GuildHubAuthenticationError) as caught:
            await client.run()

        self.assertNotIn(DEVICE_TOKEN, str(caught.exception))
        self.assertEqual(1, len(connector.calls))

    async def test_retry_count_is_bounded(self):
        connector = FailingConnector()
        client = GuildCaptionClient(
            MemoryStore(device_credentials()),
            RecordingSink(),
            connector=connector,
            max_retries=2,
            retry_initial=0,
            retry_max=0,
        )

        with self.assertRaises(GuildHubClientError) as caught:
            await client.run()

        self.assertEqual(3, connector.calls)
        self.assertNotIn(DEVICE_TOKEN, str(caught.exception))

    async def test_stop_closes_socket_and_ends_blocked_consumer(self):
        socket = BlockingSocket(
            [encode_message(authenticated_payload(cursor=0))]
        )
        client = GuildCaptionClient(
            MemoryStore(device_credentials()),
            RecordingSink(),
            connector=FakeConnector(socket),
            retry_initial=0,
            retry_max=0,
        )
        task = asyncio.create_task(client.run())
        await asyncio.wait_for(socket.iterating.wait(), timeout=1)

        await client.stop()
        await asyncio.wait_for(task, timeout=1)

        self.assertTrue(socket.closed)


class WatchModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_watch_waits_for_pairing_then_starts_and_closes_caption_client(self):
        store = MemoryStore()
        stopped = asyncio.Event()
        statuses = []
        sinks = []
        clients = []

        class OneShotClient:
            def __init__(
                self,
                received_store,
                received_sink,
                *,
                connection_status,
            ):
                self.store = received_store
                self.sink = received_sink
                self.connection_status = connection_status
                self.stopped = False
                clients.append(self)

            async def run(self):
                self.connection_status(True)
                stopped.set()

            async def stop(self):
                self.stopped = True

        def make_sink():
            sink = ClosableRecordingSink()
            sinks.append(sink)
            return sink

        def report(message):
            statuses.append(message)
            if "waiting" in message:
                store.save(device_credentials())

        await watch_captions(
            store,
            make_sink,
            client_factory=OneShotClient,
            stop_event=stopped,
            poll_interval=0,
            report=report,
        )

        self.assertEqual(1, len(clients))
        self.assertEqual(1, len(sinks))
        self.assertTrue(sinks[0].closed)
        self.assertTrue(any("waiting" in message for message in statuses))
        self.assertTrue(any("opening" in message for message in statuses))
        self.assertTrue(any("connected" in message for message in statuses))
        self.assertTrue(all(DEVICE_TOKEN not in message for message in statuses))

    async def test_a_dead_caption_window_is_reopened_without_waiting_for_words(self):
        """The overlay child dying used to be invisible until a caption
        failed to DELIVER - on a quiet night, hours of a member believing
        captions were armed while nothing could ever appear. The watch
        loop now notices the sink itself and recycles within seconds."""
        stopped = asyncio.Event()
        statuses = []
        sinks = []

        class DyingSink(ClosableRecordingSink):
            def __init__(self):
                super().__init__()
                self.is_alive = True

            def alive(self):
                return self.is_alive

        class IdleClient:
            def __init__(self, *_args, **kwargs):
                self._stop = asyncio.Event()

            async def run(self):
                await self._stop.wait()

            async def stop(self):
                self._stop.set()

        def make_sink():
            sink = DyingSink()
            sinks.append(sink)
            if len(sinks) == 1:
                # Kill the first window shortly after it opens.
                asyncio.get_running_loop().call_later(
                    0.05, lambda: setattr(sink, "is_alive", False)
                )
            else:
                asyncio.get_running_loop().call_later(0.05, stopped.set)
            return sink

        store = MemoryStore(device_credentials())
        with mock.patch.object(
            client_module._wait_for_sink_death, "__defaults__", (0.01,)
        ):
            await watch_captions(
                store,
                make_sink,
                client_factory=IdleClient,
                stop_event=stopped,
                poll_interval=0,
                report=statuses.append,
            )

        self.assertEqual(2, len(sinks), "the dead window was never reopened")
        self.assertTrue(
            any("closed; reopening" in message for message in statuses),
            statuses,
        )

    async def test_watch_is_interruptible_while_no_credential_exists(self):
        stopped = asyncio.Event()
        stopped.set()
        statuses = []

        await watch_captions(
            MemoryStore(),
            ClosableRecordingSink,
            stop_event=stopped,
            poll_interval=60,
            report=statuses.append,
        )

        self.assertEqual([], statuses)

    async def test_watch_switches_pairing_without_restarting_the_process(self):
        store = MemoryStore(device_credentials())
        stopped = asyncio.Event()
        first_connected = asyncio.Event()
        statuses = []
        clients = []

        class SwitchingClient:
            def __init__(self, _store, _sink, *, connection_status):
                self.connection_status = connection_status
                self.release = asyncio.Event()
                self.stopped = False
                clients.append(self)

            async def run(self):
                self.connection_status(True)
                if len(clients) == 1:
                    first_connected.set()
                    await self.release.wait()
                else:
                    stopped.set()

            async def stop(self):
                self.stopped = True
                self.release.set()

        task = asyncio.create_task(
            watch_captions(
                store,
                ClosableRecordingSink,
                client_factory=SwitchingClient,
                stop_event=stopped,
                poll_interval=0.01,
                report=statuses.append,
            )
        )
        await asyncio.wait_for(first_connected.wait(), timeout=1)
        store.save(
            DeviceCredentials(
                server_url=SERVER_URL,
                device_id="device_789",
                device_token="ghd_second-private-device-token",
                guild_id="guild_789",
                guild_name="Second Guild",
            )
        )
        await asyncio.wait_for(task, timeout=1)

        self.assertEqual(2, len(clients))
        self.assertTrue(clients[0].stopped)
        self.assertTrue(any("newly paired guild" in item for item in statuses))

    async def test_rejected_pairing_keeps_rec_question_until_local_stop(self):
        stopped = asyncio.Event()
        rejected = asyncio.Event()
        sinks = []

        class RejectingClient:
            def __init__(self, _store, _sink, *, connection_status):
                self.connection_status = connection_status

            async def run(self):
                raise GuildHubAuthenticationError("Pair it again.")

            async def stop(self):
                return None

        def make_sink():
            sink = ClosableRecordingSink()
            sinks.append(sink)
            return sink

        def report(message):
            if "needs a new pairing link" in message:
                rejected.set()

        task = asyncio.create_task(
            watch_captions(
                MemoryStore(device_credentials()),
                make_sink,
                client_factory=RejectingClient,
                stop_event=stopped,
                poll_interval=0.01,
                report=report,
            )
        )
        await asyncio.wait_for(rejected.wait(), timeout=1)
        await asyncio.sleep(0.03)

        self.assertEqual(1, len(sinks))
        self.assertFalse(sinks[0].closed)
        self.assertEqual(UNKNOWN_SESSION, sinks[0].events[-1])

        stopped.set()
        await asyncio.wait_for(task, timeout=1)
        self.assertTrue(sinks[0].closed)




class VoiceChannelWatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stopped = asyncio.Event()
        self.pause_seen = asyncio.Event()
        self.pause_count = 0
        self.pause_target = 3
        self.sinks = []
        self.store = MemoryStore(device_credentials())
        self.watch_task = None
        self.enterContext(mock.patch.object(client_module, "write_update_notice"))

    async def asyncTearDown(self):
        self.stopped.set()
        if self.watch_task is not None and not self.watch_task.cancelled():
            await asyncio.wait_for(self.watch_task, timeout=5)

    @staticmethod
    def denial():
        return encode_message(
            {
                "type": "error",
                "code": "voice_channel_required",
                "message": "Join the active Discord voice channel to view captions.",
            }
        )

    def start_watch(self, *sockets, report=None, **client_options):
        connector = FakeConnector(*sockets)
        owner = self

        class VisibleSink(ClosableRecordingSink):
            def __init__(self):
                super().__init__()
                self.visible_captions = []

            def send(self, event):
                if isinstance(event, ClearEvent):
                    self.visible_captions.clear()
                elif isinstance(event, CaptionEvent):
                    self.visible_captions.append(event)
                elif isinstance(event, SessionEvent) and event.state == "paused":
                    owner.pause_count += 1
                    if owner.pause_count >= owner.pause_target:
                        owner.pause_seen.set()
                return super().send(event)

        def make_sink():
            sink = VisibleSink()
            self.sinks.append(sink)
            return sink

        def make_client(store, sink, **kwargs):
            return GuildCaptionClient(
                store, sink, connector=connector, **client_options, **kwargs
            )

        self.watch_task = asyncio.create_task(
            watch_captions(
                self.store,
                make_sink,
                client_factory=make_client,
                stop_event=self.stopped,
                poll_interval=0.01,
                report=report or (lambda _message: None),
            )
        )

    def active_then_denied(self):
        return FakeSocket(
            [
                encode_message(
                    authenticated_payload(
                        session={"type": "session", "state": "active"}
                    )
                ),
                encode_message(caption_envelope(STREAM_ONE, 1, CAPTION)),
                self.denial(),
            ]
        )

    async def test_repeated_voice_denial_keeps_one_paused_window_until_stop(self):
        self.start_watch(
            self.active_then_denied(),
            FakeSocket([self.denial()]),
            FakeSocket([self.denial()]),
        )
        await asyncio.wait_for(self.pause_seen.wait(), timeout=5)

        self.assertEqual(1, len(self.sinks), "denied retries recreated the window")
        sink = self.sinks[0]
        self.assertFalse(sink.closed)
        self.assertEqual([], sink.visible_captions, "denial must clear prior captions")
        states = [
            event.state for event in sink.events if isinstance(event, SessionEvent)
        ]
        first_pause = states.index("paused")
        self.assertTrue(all(state == "paused" for state in states[first_pause:]))

        self.stopped.set()
        await asyncio.wait_for(self.watch_task, timeout=5)
        self.assertTrue(sink.closed)

    async def test_voice_denial_report_explains_account_bound_pairing(self):
        self.pause_target = 1
        reports = []
        guidance_seen = asyncio.Event()

        def report(message):
            reports.append(message)
            if "account-bound invitation" in message:
                guidance_seen.set()

        self.start_watch(FakeSocket([self.denial()]), report=report)
        await asyncio.wait_for(guidance_seen.wait(), timeout=5)

        message = next(
            item for item in reports if "account-bound invitation" in item
        )
        self.assertIn("join the active Discord voice channel", message)
        self.assertIn(
            "Re-pair first if this device has no account-bound invitation.",
            message,
        )
        self.assertNotIn("rejoin", message.lower())
        self.assertNotIn("link discord", message.lower())
        self.assertNotIn("linked your vemal's ears application", message.lower())

    async def test_rejoin_resumes_same_window_from_authoritative_snapshot(self):
        fresh_caption = CaptionEvent(
            speaker_id="discord-user-1",
            speaker="Mira",
            character="Miralyn",
            role="HEALER",
            text="Back in the channel.",
            final=True,
            sequence=8,
            timestamp=13.0,
        )
        rejoined = BlockingSocket(
            [
                encode_message(
                    authenticated_payload(
                        cursor=1, session={"type": "session", "state": "active"}
                    )
                ),
                encode_message(caption_envelope(STREAM_ONE, 2, fresh_caption)),
            ]
        )
        self.start_watch(self.active_then_denied(), rejoined)
        await asyncio.wait_for(rejoined.iterating.wait(), timeout=5)

        self.assertEqual(1, len(self.sinks), "rejoining recreated the window")
        sink = self.sinks[0]
        self.assertFalse(sink.closed)
        self.assertEqual([fresh_caption], sink.visible_captions)
        states = [
            event.state for event in sink.events if isinstance(event, SessionEvent)
        ]
        first_pause = states.index("paused")
        self.assertEqual(["paused", "active"], states[first_pause:])

    async def test_pairing_change_closes_denied_window_before_new_guild(self):
        self.pause_target = 1
        replacement = DeviceCredentials(
            server_url=SERVER_URL,
            device_id="device_789",
            device_token="ghd_second-private-device-token",
            guild_id="guild_789",
            guild_name="Second Guild",
        )
        second_guild = BlockingSocket(
            [
                encode_message(
                    authenticated_payload(
                        session={"type": "session", "state": "ready"}
                    )
                )
            ]
        )
        self.start_watch(FakeSocket([self.denial()]), second_guild)
        await asyncio.wait_for(self.pause_seen.wait(), timeout=5)
        self.store.save(replacement)
        await asyncio.wait_for(second_guild.iterating.wait(), timeout=5)

        self.assertEqual(2, len(self.sinks))
        self.assertTrue(self.sinks[0].closed)
        self.assertFalse(self.sinks[1].closed)
        self.assertEqual(UNKNOWN_SESSION, self.sinks[1].events[0])
        authentication = decode_message(second_guild.sent[0])
        self.assertEqual(replacement.device_id, authentication["device_id"])

    async def test_cancelling_voice_channel_wait_closes_the_window(self):
        loop = asyncio.get_running_loop()
        unexpected_errors = []
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(
            lambda _loop, context: unexpected_errors.append(context)
        )
        self.addCleanup(loop.set_exception_handler, previous_handler)
        self.pause_target = 1
        self.start_watch(FakeSocket([self.denial()]))
        await asyncio.wait_for(self.pause_seen.wait(), timeout=5)

        self.watch_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await self.watch_task

        self.assertEqual(1, len(self.sinks))
        self.assertTrue(self.sinks[0].closed)
        await asyncio.sleep(0)
        self.assertEqual(
            [], [context.get("message") for context in unexpected_errors],
            "a completed retry was not reaped",
        )
        pending_helpers = [
            task.get_coro().__name__ for task in asyncio.all_tasks()
            if task.get_coro().__name__ in {
                "_wait_for_sink_death", "_wait_for_credential_change"
            }
        ]
        self.assertEqual([], pending_helpers, "cancelled watcher leaked helper tasks")

    async def test_network_retry_does_not_replace_known_access_pause(self):
        class OfflineSocket(FakeSocket):
            async def recv(self):
                raise OSError("synthetic offline connection")

        self.pause_target = 2
        self.start_watch(
            FakeSocket([self.denial()]),
            OfflineSocket(),
            FakeSocket([self.denial()]),
            max_retries=0,
        )
        await asyncio.wait_for(self.pause_seen.wait(), timeout=5)

        self.assertEqual(1, len(self.sinks), "network retry replaced the paused window")
        sink = self.sinks[0]
        self.assertFalse(sink.closed)
        states = [
            event.state for event in sink.events if isinstance(event, SessionEvent)
        ]
        first_pause = states.index("paused")
        self.assertTrue(all(state == "paused" for state in states[first_pause:]))


class ResolvePairLinkTests(unittest.IsolatedAsyncioTestCase):
    """The dialog accepts the link the console mints, not just the deep link."""

    async def test_deep_link_resolves_locally_without_any_fetch(self):
        def forbidden(_origin, _timeout):
            raise AssertionError("a vemalsears:// link must not fetch anything")

        link = await client_module.resolve_pair_link(
            "vemalsears://pair?server=wss%3A%2F%2Fears.test&code=ghe_" + "a" * 30,
            config_fetch=forbidden,
        )
        self.assertEqual("wss://ears.test", link.server_url)

    async def test_https_join_link_resolves_via_the_relay_own_origin(self):
        seen = {}

        def fetch(origin, timeout):
            seen["origin"] = origin
            return {"service": "vemals-ears", "server_url": "wss://ears.test",
                    "guild_name": "Raid Friends"}

        code = "ghe_" + "b" * 30
        link = await client_module.resolve_pair_link(
            "https://ears.test/join#" + code, config_fetch=fetch,
        )
        self.assertEqual("https://ears.test", seen["origin"])
        self.assertEqual("wss://ears.test", link.server_url)
        self.assertEqual(code, link.enrollment_code)

    async def test_http_is_loopback_only(self):
        def fetch(_origin, _timeout):
            return {"service": "vemals-ears", "server_url": "ws://127.0.0.1:9/x"}

        with self.assertRaises(client_module.GuildHubClientError):
            await client_module.resolve_pair_link(
                "http://ears.test/join#ghe_" + "c" * 30, config_fetch=fetch,
            )

    async def test_wrong_origin_service_is_named_plainly(self):
        def fetch(_origin, _timeout):
            return {"hello": "world"}

        with self.assertRaises(client_module.GuildHubClientError) as caught:
            await client_module.resolve_pair_link(
                "https://ears.test/join#ghe_" + "d" * 30, config_fetch=fetch,
            )
        self.assertIn("does not point at", str(caught.exception))

    async def test_garbage_gets_guidance_naming_both_accepted_forms(self):
        with self.assertRaises(client_module.GuildHubClientError) as caught:
            await client_module.resolve_pair_link("ftp://nope.example/thing")
        message = str(caught.exception)
        self.assertIn("invitation link", message)
        self.assertIn("vemalsears://", message)

    async def test_join_link_with_query_or_wrong_path_is_refused(self):
        for bad in (
            "https://ears.test/join?code=x#ghe_" + "e" * 30,
            "https://ears.test/other#ghe_" + "e" * 30,
            "https://ears.test/join",
        ):
            with self.assertRaises(client_module.GuildHubClientError):
                await client_module.resolve_pair_link(bad, config_fetch=lambda o, t: {})


class UpdateOfferTests(unittest.TestCase):
    """What the relay says about a newer build, and what we do with it."""

    def offer(self, **overrides):
        payload = {
            "version": "0.2.0",
            "sha256": "a" * 64,
            "bytes": 1024,
            "url": "https://ears.example.invalid/download",
        }
        payload.update(overrides)
        return {"type": "authenticated", "update": payload}

    def test_a_newer_version_is_offered(self):
        parsed = client_module.parse_update_offer(self.offer(), "0.1.0")
        self.assertIsNotNone(parsed)
        self.assertEqual("0.2.0", parsed["version"])

    def test_the_version_already_running_is_not_offered(self):
        self.assertIsNone(client_module.parse_update_offer(self.offer(), "0.2.0"))
        self.assertIsNone(
            client_module.parse_update_offer(
                self.offer(version="0.2.0.0"), "0.2.0"
            )
        )

    def test_an_older_version_is_never_offered(self):
        """A rollback must not hand every member a downgrade.

        The check here used to be equality alone, so "different" counted
        as "newer". Advertise an older build after a bad release and
        everyone on the newer one was offered it. The installer refuses a
        downgrade, so nothing broke - but the banner could never clear,
        because the versions still differed. It re-offered the same dead
        end every session, after a fresh 21 MB download each time.
        """
        for running in ("0.2.1", "0.3.0", "1.0.0", "0.2.0.1"):
            with self.subTest(running=running):
                self.assertIsNone(
                    client_module.parse_update_offer(self.offer(), running),
                    running + " was offered the older 0.2.0",
                )

    def test_ordering_is_numeric_not_lexicographic(self):
        """0.10.0 is newer than 0.9.0, and string comparison disagrees."""
        self.assertIsNotNone(
            client_module.parse_update_offer(self.offer(version="0.10.0"), "0.9.0")
        )
        self.assertIsNone(
            client_module.parse_update_offer(self.offer(version="0.9.0"), "0.10.0")
        )

    def test_versions_that_cannot_be_ranked_are_refused(self):
        """Automatic downloads never let the relay invent version ordering."""
        self.assertIsNone(
            client_module.parse_update_offer(self.offer(version="2026.09-beta"), "0.2.0")
        )
        self.assertIsNone(
            client_module.parse_update_offer(self.offer(), "not-a-version")
        )
        for version in ("1", "0.2", "0.2.0.1.2", "0.2.65536"):
            with self.subTest(version=version):
                self.assertIsNone(
                    client_module.parse_update_offer(
                        self.offer(version=version), "0.1.0"
                    )
                )

    def test_update_url_rejects_userinfo_fragments_and_unbounded_text(self):
        for url in (
            "https://user:secret@ears.example.invalid/download",
            "https://ears.example.invalid/download#fragment",
            "https://ears.example.invalid/" + "x" * 2048,
        ):
            with self.subTest(url=url[:80]):
                self.assertIsNone(
                    client_module.parse_update_offer(self.offer(url=url), "0.1.0")
                )

    def test_no_offer_at_all_is_fine(self):
        self.assertIsNone(
            client_module.parse_update_offer({"type": "authenticated"}, "0.1.0")
        )

    def test_a_plain_http_update_is_refused(self):
        """An update is the one thing a member is invited to RUN."""
        for url in ("http://ears.example.invalid/download",
                    "file:///C:/evil.exe",
                    "ftp://ears.example.invalid/x",
                    "https:///nohost"):
            self.assertIsNone(
                client_module.parse_update_offer(self.offer(url=url), "0.1.0"),
                url,
            )

    def test_malformed_offers_are_ignored_rather_than_raised(self):
        for bad in (
            {"version": "", "sha256": "a" * 64, "bytes": 1, "url": "https://h/x"},
            {"version": "1", "sha256": "nope", "bytes": 1, "url": "https://h/x"},
            {"version": "1", "sha256": "A" * 64, "bytes": 1, "url": "https://h/x"},
            {"version": "1", "sha256": "a" * 64, "bytes": 0, "url": "https://h/x"},
            {"version": "1", "sha256": "a" * 64, "bytes": True, "url": "https://h/x"},
            {"version": "1", "sha256": "a" * 64, "bytes": 10 ** 12, "url": "https://h/x"},
            {"version": "x" * 40, "sha256": "a" * 64, "bytes": 1, "url": "https://h/x"},
            "not-an-object",
            None,
        ):
            self.assertIsNone(
                client_module.parse_update_offer({"update": bad}, "0.1.0"), repr(bad)
            )


class VerifiedDownloadTests(unittest.TestCase):
    """The download is public; only the checksum makes it trustworthy."""

    def setUp(self):
        import hashlib
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.payload = b"MZ pretend installer" + b"\x00" * 4096
        self.digest = hashlib.sha256(self.payload).hexdigest()

    def fetcher(self, body):
        class Response:
            def __init__(self, data):
                self._data = data
                self._offset = 0

            def read(self, size):
                block = self._data[self._offset:self._offset + size]
                self._offset += len(block)
                return block

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        def fetch(_request, timeout=None):
            return Response(body)

        return fetch

    def offer(self, **overrides):
        payload = {
            "version": "0.2.0",
            "sha256": self.digest,
            "bytes": len(self.payload),
            "url": "https://ears.example.invalid/download",
        }
        payload.update(overrides)
        return payload

    def test_a_matching_download_lands_at_its_real_name(self):
        from pathlib import Path
        target = Path(self.temp.name) / "Setup.exe"
        result = client_module.download_verified_update(
            self.offer(), target, fetch=self.fetcher(self.payload)
        )
        self.assertEqual(target, result)
        self.assertEqual(self.payload, target.read_bytes())
        self.assertFalse(target.with_name("Setup.exe.part").exists())

    def test_a_tampered_download_is_refused_and_left_behind(self):
        from pathlib import Path
        target = Path(self.temp.name) / "Setup.exe"
        tampered = b"MZ evil installer" + b"\x00" * 4099
        self.assertEqual(len(tampered), len(self.payload))
        with self.assertRaises(client_module.GuildHubClientError):
            client_module.download_verified_update(
                self.offer(), target, fetch=self.fetcher(tampered)
            )
        self.assertFalse(target.exists(), "a wrong file must never take the real name")
        self.assertFalse(target.with_name("Setup.exe.part").exists())

    def test_a_download_longer_than_advertised_is_cut_off(self):
        from pathlib import Path
        target = Path(self.temp.name) / "Setup.exe"
        with self.assertRaises(client_module.GuildHubClientError):
            client_module.download_verified_update(
                self.offer(), target,
                fetch=self.fetcher(self.payload + b"x" * 100_000),
            )
        self.assertFalse(target.exists())

    def test_a_truncated_download_is_refused(self):
        from pathlib import Path
        target = Path(self.temp.name) / "Setup.exe"
        with self.assertRaises(client_module.GuildHubClientError):
            client_module.download_verified_update(
                self.offer(), target, fetch=self.fetcher(self.payload[:-10])
            )
        self.assertFalse(target.exists())

    def test_a_redirect_that_leaves_https_is_refused(self):
        from pathlib import Path
        target = Path(self.temp.name) / "Setup.exe"

        class Response:
            def read(self, _size):
                return b""

            def geturl(self):
                return "http://downloads.example.invalid/Setup.exe"

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        with self.assertRaises(client_module.GuildHubClientError):
            client_module.download_verified_update(
                self.offer(), target, fetch=lambda _request, timeout=None: Response()
            )
        self.assertFalse(target.exists())
        self.assertFalse(target.with_name("Setup.exe.part").exists())


class UpdateNoticeTests(unittest.TestCase):
    """The small file the caption client and the companion agree on."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "update-available.json"

    def offer(self, version=None):
        # Derived, never hard-coded: pinning a literal here collided with
        # CLIENT_VERSION the moment a release was cut, and the test then
        # failed for the correct behaviour - an offer matching the running
        # version is not an offer.
        return {
            "version": version or (client_module.CLIENT_VERSION + ".1"),
            "sha256": "b" * 64,
            "bytes": 4096,
            "url": "https://ears.example.invalid/download",
        }

    def test_an_offer_survives_the_round_trip(self):
        client_module.write_update_notice(self.offer(), self.path)
        self.assertEqual(self.offer(), client_module.read_update_notice(self.path))

    def test_no_offer_removes_the_notice(self):
        client_module.write_update_notice(self.offer(), self.path)
        client_module.write_update_notice(None, self.path)
        self.assertFalse(self.path.exists())
        self.assertIsNone(client_module.read_update_notice(self.path))

    def test_a_missing_or_corrupt_notice_reads_as_nothing(self):
        self.assertIsNone(client_module.read_update_notice(self.path))
        self.path.write_text("{not json", encoding="ascii")
        self.assertIsNone(client_module.read_update_notice(self.path))

    def test_the_notice_is_re_validated_on_the_way_in(self):
        """A sibling process writes this and the companion is about to
        download whatever it names, so trusting it unread would make the
        file itself the weak link."""
        import json
        for bad in (
            {**self.offer(), "url": "http://ears.example.invalid/download"},
            {**self.offer(), "url": "file:///C:/evil.exe"},
            {**self.offer(), "sha256": "nope"},
            {**self.offer(), "bytes": -1},
        ):
            self.path.write_text(json.dumps(bad), encoding="ascii")
            self.assertIsNone(
                client_module.read_update_notice(self.path), repr(bad)
            )

    def test_the_running_version_is_never_offered_back(self):
        import json
        self.path.write_text(
            json.dumps(self.offer(client_module.CLIENT_VERSION)), encoding="ascii"
        )
        self.assertIsNone(client_module.read_update_notice(self.path))


def _planned_close(code=1001):
    """A ConnectionClosed carrying a close code, built without touching
    the websockets constructor - its signature differs across versions
    and this test is about our reading of the code, not theirs."""
    closed = client_module.ConnectionClosed.__new__(
        client_module.ConnectionClosed
    )
    Exception.__init__(closed, "going away")
    closed.rcvd = types.SimpleNamespace(code=code, reason="Paused for updates")
    closed.sent = None
    return closed


class PlannedCloseReadingTests(unittest.TestCase):
    def test_going_away_and_restart_codes_are_planned(self):
        for code in (1001, 1012, 1013):
            self.assertTrue(
                client_module._is_planned_close(_planned_close(code)), code
            )

    def test_a_fault_close_is_not_planned(self):
        # 1006 is an abnormal closure - the relay really did fall over,
        # and the retry budget SHOULD apply to it.
        for code in (1006, 1008, 1011):
            self.assertFalse(
                client_module._is_planned_close(_planned_close(code)), code
            )


class MaintenancePauseTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def notice():
        return encode_message(
            {
                "type": "error",
                "code": "server_maintenance",
                "message": "Paused for updates - the guild server is restarting.",
            }
        )

    async def test_the_notice_is_not_mistaken_for_a_revoked_pairing(self):
        # The bug this guards: every unrecognised server error fell through
        # to "this device is no longer authorized. Pair it again." Telling
        # someone to re-pair because of a deploy is worse than saying
        # nothing at all.
        socket = FakeSocket([self.notice()])
        client = GuildCaptionClient(
            MemoryStore(device_credentials()),
            RecordingSink(),
            connector=FakeConnector(socket),
        )

        with self.assertRaises(client_module.GuildHubMaintenanceError):
            await client._consume_once(device_credentials())

    async def test_the_overlay_says_paused_for_updates(self):
        sink = RecordingSink()
        client = GuildCaptionClient(
            MemoryStore(device_credentials()),
            sink,
            connector=FakeConnector(FakeSocket([self.notice()])),
        )
        client._maintenance_retry = 0.01

        run = asyncio.create_task(client.run())
        try:
            await self._wait_for_pause(sink)
        finally:
            await client.stop()
            await asyncio.wait_for(run, timeout=5)

        paused = [
            event
            for event in sink.events
            if isinstance(event, SessionEvent) and event.state == "paused"
        ]
        self.assertTrue(paused)
        self.assertIn("Paused for updates", paused[-1].message)

    async def test_a_restart_outlasting_the_retry_budget_is_not_an_error(self):
        # This is the actual complaint. With max_retries=1 the old loop
        # raised "could not reconnect" on the second failed attempt; a
        # deploy easily outlasts that, and the viewer was shown a fault
        # for a restart that finished seconds later.
        sink = RecordingSink()
        connector = FakeConnector(FakeSocket([self.notice()]))
        client = GuildCaptionClient(
            MemoryStore(device_credentials()),
            sink,
            connector=connector,
            max_retries=1,
            retry_initial=0.01,
            retry_max=0.01,
        )
        client._maintenance_retry = 0.01

        run = asyncio.create_task(client.run())
        try:
            await self._wait_for_pause(sink)
            # Well past the budget: the connector is empty now, so every
            # one of these is a failed reconnect attempt.
            await asyncio.sleep(0.2)
            self.assertFalse(run.done(), "the client gave up on a deploy")
        finally:
            await client.stop()
            await asyncio.wait_for(run, timeout=5)

        self.assertGreater(len(connector.calls), 2)

    async def test_a_bare_1001_close_pauses_without_an_error_frame(self):
        # A relay killed mid-frame has no chance to send the notice first,
        # so the close code has to carry the same meaning on its own.
        class ClosingSocket(FakeSocket):
            async def __anext__(self):
                raise _planned_close()

        sink = RecordingSink()
        client = GuildCaptionClient(
            MemoryStore(device_credentials()),
            sink,
            connector=FakeConnector(
                ClosingSocket([encode_message(authenticated_payload(cursor=0))])
            ),
            max_retries=1,
            retry_initial=0.01,
            retry_max=0.01,
        )
        client._maintenance_retry = 0.01

        run = asyncio.create_task(client.run())
        try:
            await self._wait_for_pause(sink)
            self.assertFalse(run.done())
        finally:
            await client.stop()
            await asyncio.wait_for(run, timeout=5)

    async def test_a_quiet_connection_that_drops_does_not_spend_the_budget(self):
        """The disconnect-reconnect bug.

        Every one of these connections is ACCEPTED by the relay and then
        drops for an unplanned reason - a tunnel hiccup, a sleeping
        laptop, a reset. Nobody speaks in between, so nothing is
        delivered. The old loop counted each one as a failed attempt,
        because it reset the budget only on a delivered caption; with the
        default budget of six, seven quiet drops raised "could not
        reconnect" against a relay that took every single connection.
        """

        class QuietThenDropped(FakeSocket):
            async def __anext__(self):
                if self.iterated:
                    raise _planned_close(1006)
                self.iterated = True
                return await super().__anext__()

        class Endless:
            """A relay that accepts every connection, forever."""

            def __init__(self):
                self.calls = 0

            def __call__(self, url, **kwargs):
                self.calls += 1
                socket = QuietThenDropped(
                    [encode_message(authenticated_payload(cursor=0))]
                )
                socket.iterated = False
                return socket

        connector = Endless()
        client = GuildCaptionClient(
            MemoryStore(device_credentials()),
            RecordingSink(),
            connector=connector,
            max_retries=1,
            retry_initial=0.01,
            retry_max=0.01,
        )

        run = asyncio.create_task(client.run())
        try:
            for _ in range(200):
                if connector.calls >= 8:
                    break
                await asyncio.sleep(0.01)
            self.assertGreaterEqual(connector.calls, 8)
            self.assertFalse(
                run.done(),
                "an accepted-then-dropped connection was counted against "
                "the retry budget",
            )
        finally:
            await client.stop()
            await asyncio.wait_for(run, timeout=5)

    async def test_a_drop_after_acceptance_reconnects_at_the_initial_delay(self):
        # The other half of the same defect. Escalating the delay after an
        # accepted connection dropped meant captions went dark for up to
        # retry_max seconds after every quiet hiccup, for no reason.
        class Dropping(FakeSocket):
            async def __anext__(self):
                raise _planned_close(1006)

        class Endless:
            def __call__(self, url, **kwargs):
                return Dropping(
                    [encode_message(authenticated_payload(cursor=0))]
                )

        client = GuildCaptionClient(
            MemoryStore(device_credentials()),
            RecordingSink(),
            connector=Endless(),
            max_retries=3,
            retry_initial=0.01,
            retry_max=8.0,
        )
        delays = []
        real_wait = client._wait_for_retry

        async def recording_wait(delay):
            delays.append(delay)
            if len(delays) >= 6:
                client._stop_event.set()
                return False
            return await real_wait(0)

        client._wait_for_retry = recording_wait
        await asyncio.wait_for(client.run(), timeout=5)

        self.assertEqual(6, len(delays))
        self.assertEqual(
            [0.01] * 6,
            delays,
            "the reconnect delay escalated after connections the relay "
            "had accepted",
        )

    async def test_reconnecting_ends_the_pause(self):
        # Without this the client would sit on the calm cadence forever
        # and never restore the exponential backoff a real outage needs.
        sink = RecordingSink()
        client = GuildCaptionClient(
            MemoryStore(device_credentials()),
            sink,
            connector=FakeConnector(
                FakeSocket([self.notice()]),
                FakeSocket([encode_message(authenticated_payload(cursor=0))]),
            ),
        )
        client._maintenance_retry = 0.01

        run = asyncio.create_task(client.run())
        try:
            await self._wait_for_pause(sink)
            for _ in range(200):
                if not client._in_maintenance:
                    break
                await asyncio.sleep(0.01)
            self.assertFalse(
                client._in_maintenance, "the pause outlived the restart"
            )
        finally:
            await client.stop()
            await asyncio.wait_for(run, timeout=5)

    @staticmethod
    async def _wait_for_pause(sink):
        for _ in range(500):
            if any(
                isinstance(event, SessionEvent) and event.state == "paused"
                for event in sink.events
            ):
                return
            await asyncio.sleep(0.01)
        raise AssertionError("the overlay was never told about the pause")

if __name__ == "__main__":
    unittest.main(verbosity=2)
