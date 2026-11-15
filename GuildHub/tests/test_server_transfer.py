"""The relay's side of a Host-to-Host transfer.

The relay is a rendezvous and nothing more: one pending offer in memory,
one claim socket at a time, opaque binary frames piped between them, and a
freeze that holds pairing and viewer linking still while the database is
copied. It never sees the one-time code, a key or any plaintext, and typed
chat keeps working throughout because a typed line depends only on the
relay.
"""

import asyncio
import os
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

import GuildHub.server as server
import GuildHub.tests.test_server as relay_tests
from GuildHub.protocol import (
    SUBPROTOCOL,
    TRANSFER_CLAIM_PATH,
    TRANSFER_OFFER_PATH,
    decode_message,
    encode_message,
)
from GuildHub.server import GuildHubServer, ServerConfig
from GuildHub.store import CredentialStore


# Synthetic values only. The key has the shape the Host writes: URL-safe
# text of at least 32 characters.
TRANSFER_KEY = "transfer_" + ("k" * 40)
WRONG_KEY = "transfer_" + ("w" * 40)
VIEWER = "0123456789abcdef01234567"
# Not valid UTF-8 and not JSON: the relay has no business reading it.
OPAQUE = b"\x00\xff\xfe opaque-frame-marker \x80\x81"


class TransferRelayTests(unittest.IsolatedAsyncioTestCase):
    start_application = relay_tests.ServerTests.start_application
    wait_until = relay_tests.ServerTests.wait_until
    pair_code = relay_tests.ServerTests.pair_code
    pair = relay_tests.ServerTests.pair
    admit_viewer = relay_tests.ServerTests.admit_viewer
    refresh_audience = relay_tests.ServerTests.refresh_audience
    open_captions = relay_tests.ServerTests.open_captions
    open_publisher = relay_tests.ServerTests.open_publisher
    assign_quick_chat_profile = relay_tests.ServerTests.assign_quick_chat_profile
    send_quick_chat = relay_tests.ServerTests.send_quick_chat
    quick_chat_profile = relay_tests.ServerTests.quick_chat_profile
    fetch = relay_tests.ServerTests.fetch
    request_viewer_link = relay_tests.VoiceMembershipTests.request_viewer_link

    async def asyncSetUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary_directory.name)
        self.database = self.directory / "guild-hub.sqlite3"
        self.key_file = self.directory / "transfer-key"
        self.freeze_file = self.directory / "transfer-frozen"
        self.store = CredentialStore(self.database, relay_tests.PEPPER)
        self.store.initialize()
        self.config = ServerConfig(
            guild_id="guild-one",
            guild_name="Vemal's Ears",
            publisher_token=relay_tests.PUBLISHER_TOKEN,
            public_server_url=relay_tests.PUBLIC_SERVER_URL,
            client_download_url=relay_tests.CLIENT_DOWNLOAD_URL,
            auth_timeout=5.0,
            replay_limit=8,
            client_queue_limit=4,
            connection_limit=16,
            revalidation_seconds=0.05,
            quick_chat_response_timeout=5.0,
            transfer_key_file=str(self.key_file),
            transfer_freeze_file=str(self.freeze_file),
        )
        self.application = GuildHubServer(self.store, self.config)
        self.server, self.base_url = await self.start_application(self.application)
        self.extra_servers = []
        self.sockets = []
        self.viewer_tokens = []
        self.audience_revision = 0

    async def asyncTearDown(self):
        for websocket in self.sockets:
            await websocket.close()
        for extra in (*self.extra_servers, self.server):
            extra.close()
            await extra.wait_closed()
        self.temporary_directory.cleanup()

    def write_key(self, value=TRANSFER_KEY):
        self.key_file.write_text(value + "\n", encoding="ascii")

    async def open_offer_socket(self, key=TRANSFER_KEY, base_url=None):
        websocket = await connect(
            (base_url or self.base_url) + TRANSFER_OFFER_PATH,
            subprotocols=[SUBPROTOCOL],
        )
        self.sockets.append(websocket)
        await websocket.send(
            encode_message({"type": "authenticate", "transfer_key": key})
        )
        return websocket

    async def open_offer(self):
        self.write_key()
        offer = await self.open_offer_socket()
        self.assertEqual(
            {"type": "transfer_offered", "ttl_seconds": 600},
            await self.notice(offer),
        )
        return offer

    async def open_claim(self):
        websocket = await connect(
            self.base_url + TRANSFER_CLAIM_PATH, subprotocols=[SUBPROTOCOL]
        )
        self.sockets.append(websocket)
        return websocket

    async def notice(self, websocket, timeout=10):
        raw = await asyncio.wait_for(websocket.recv(), timeout)
        self.assertIsInstance(raw, str)
        return decode_message(raw)

    async def control(self, offer, kind, **fields):
        await offer.send(encode_message({"type": kind, **fields}))

    async def refused_status(self, path, base_url=None):
        with self.assertRaises(InvalidStatus) as refused:
            async with connect(
                (base_url or self.base_url) + path, subprotocols=[SUBPROTOCOL]
            ):
                pass
        return refused.exception.response.status_code

    async def closed(self, websocket, timeout=10):
        """Drain a socket the relay is expected to close; return the code."""
        try:
            while True:
                await asyncio.wait_for(websocket.recv(), timeout)
        except ConnectionClosed as error:
            self.last_close_reason = error.rcvd.reason if error.rcvd else None
            return error.rcvd.code if error.rcvd else None

    async def ended(self, offer):
        """Why the relay destroyed an offer, as its close frame says it.

        The transfer_ended notice goes out first, but whether a client
        reads a frame that is followed at once by a close is a race, so
        the close reason is the part a Host can rely on.
        """
        self.assertEqual(1008, await self.closed(offer))
        prefix = "Transfer ended: "
        self.assertTrue(self.last_close_reason.startswith(prefix))
        return self.last_close_reason[len(prefix):]

    async def bridged(self):
        """An offer with one claim that has sent its hello and been confirmed."""
        offer = await self.open_offer()
        claim = await self.open_claim()
        self.assertEqual(
            {"type": "transfer_claim_opened", "claim": 1}, await self.notice(offer)
        )
        await claim.send(b"hello")
        self.assertEqual(b"hello", await asyncio.wait_for(offer.recv(), 10))
        await self.control(offer, "transfer_confirmed", claim=1)
        await self.wait_until(lambda: self.application._transfer_offer.confirmed)
        return offer, claim

    # -- feature gate ------------------------------------------------------

    async def test_both_paths_are_404_without_the_key_file_setting(self):
        application = GuildHubServer(
            self.store, replace(self.config, transfer_key_file="")
        )
        ungated, base_url = await self.start_application(application)
        self.extra_servers.append(ungated)
        # Even a correct key on disk does not bring the paths into being.
        self.write_key()

        for path in (TRANSFER_OFFER_PATH, TRANSFER_CLAIM_PATH):
            self.assertEqual(404, await self.refused_status(path, base_url))
        self.assertFalse(application.transfer_offer_pending)
        self.assertEqual(0, application.active_handler_count)

    async def test_default_configuration_has_no_transfer_paths(self):
        self.assertEqual("", ServerConfig.transfer_key_file)
        self.assertEqual("", ServerConfig.transfer_freeze_file)

    async def test_claim_is_404_before_the_upgrade_when_no_offer_is_pending(self):
        self.write_key()
        self.assertEqual(404, await self.refused_status(TRANSFER_CLAIM_PATH))
        status, _headers, body = await self.fetch(TRANSFER_CLAIM_PATH)
        self.assertEqual((404, b"Not found.\n"), (status, body))
        # A query string is not a way in either: the code is never in a URL.
        self.assertEqual(
            404, await self.refused_status(TRANSFER_OFFER_PATH + "?code=ABCDEFGH")
        )

    async def test_wrong_key_and_absent_key_file_are_the_same_refusal(self):
        async def attempt(key):
            websocket = await self.open_offer_socket(key)
            frame = decode_message(await asyncio.wait_for(websocket.recv(), 10))
            code = await self.closed(websocket)
            return frame, code, self.last_close_reason

        # No key file at all, and the caller happens to send what the key
        # would have been.
        absent = await attempt(TRANSFER_KEY)
        self.write_key()
        wrong = await attempt(WRONG_KEY)
        # A file that is not a key is no key.
        self.write_key("short")
        malformed = await attempt("short")
        self.key_file.write_bytes(b"\xff" * 64)
        unreadable = await attempt(TRANSFER_KEY)

        self.assertEqual(
            {
                "type": "error",
                "code": "authentication_failed",
                "message": "Authentication failed.",
            },
            wrong[0],
        )
        self.assertEqual(1008, wrong[1])
        self.assertEqual(wrong, absent)
        self.assertEqual(wrong, malformed)
        self.assertEqual(wrong, unreadable)
        self.assertFalse(self.application.transfer_offer_pending)
        self.assertEqual(404, await self.refused_status(TRANSFER_CLAIM_PATH))

    async def test_key_is_read_per_attempt(self):
        self.write_key(WRONG_KEY)
        first = await self.open_offer_socket(TRANSFER_KEY)
        self.assertEqual("error", (await self.notice(first))["type"])
        # The Host wrote a fresh key after the relay started.
        self.write_key(TRANSFER_KEY)
        second = await self.open_offer_socket(TRANSFER_KEY)
        self.assertEqual("transfer_offered", (await self.notice(second))["type"])

    async def test_offer_must_authenticate_in_its_first_frame(self):
        self.write_key()
        self.application.config = replace(self.config, auth_timeout=0.2)
        websocket = await connect(
            self.base_url + TRANSFER_OFFER_PATH, subprotocols=[SUBPROTOCOL]
        )
        self.sockets.append(websocket)
        self.assertEqual("authentication_failed", (await self.notice(websocket))["code"])
        self.assertFalse(self.application.transfer_offer_pending)

    def test_key_reader_is_never_fatal(self):
        read = server._read_transfer_key
        self.assertIsNone(read(""))
        self.assertIsNone(read(str(self.directory / "missing")))
        self.assertIsNone(read(str(self.directory)))
        self.write_key()
        self.assertEqual(TRANSFER_KEY, read(str(self.key_file)))
        for content in (
            b"",
            b"short",
            b"\xff" * 64,
            b"k" * 31,
            b"k" * 193,
            b"spaces are not a key " + b"k" * 32,
            b"k" * (server.MAX_SECRET_FILE_BYTES + 1),
        ):
            self.key_file.write_bytes(content)
            self.assertIsNone(read(str(self.key_file)), content[:16])

    # -- rendezvous and piping ----------------------------------------------

    async def test_offer_and_claim_are_paired_and_frames_pipe_opaque_both_ways(self):
        with self.assertLogs("GuildHub.server", level="DEBUG") as logs:
            offer = await self.open_offer()
            self.assertTrue(self.application.transfer_offer_pending)
            claim = await self.open_claim()
            self.assertEqual(
                {"type": "transfer_claim_opened", "claim": 1},
                await self.notice(offer),
            )

            await claim.send(OPAQUE)
            self.assertEqual(OPAQUE, await asyncio.wait_for(offer.recv(), 10))
            await offer.send(OPAQUE[::-1])
            self.assertEqual(OPAQUE[::-1], await asyncio.wait_for(claim.recv(), 10))

            await self.control(offer, "transfer_confirmed", claim=1)
            # Largest frame the relay admits, both directions, in order.
            large = bytes(range(256)) * 64
            self.assertEqual(server.MAX_WIRE_BYTES, len(large))
            for index in range(8):
                await offer.send(bytes([index]) + large[1:])
                await claim.send(bytes([index + 100]) + large[1:])
            for index in range(8):
                self.assertEqual(
                    bytes([index]) + large[1:],
                    await asyncio.wait_for(claim.recv(), 10),
                )
                self.assertEqual(
                    bytes([index + 100]) + large[1:],
                    await asyncio.wait_for(offer.recv(), 10),
                )

            # Control text is the relay's; the claim never sees it.
            await self.control(offer, "transfer_freeze")
            self.assertEqual(
                {"type": "transfer_frozen", "frozen": True}, await self.notice(offer)
            )
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(claim.recv(), 0.05)

        # Nothing that passed through was written down or logged.
        self.assertNotIn("opaque-frame-marker", "\n".join(logs.output))
        self.assertNotIn(TRANSFER_KEY, "\n".join(logs.output))
        for path in self.directory.iterdir():
            if path == self.key_file:
                continue
            self.assertNotIn(b"opaque-frame-marker", path.read_bytes(), path.name)
        self.assertEqual(
            {"guild-hub.sqlite3", "transfer-key"},
            {path.name.split("-wal")[0].split("-shm")[0] for path in self.directory.iterdir()},
        )

    async def test_only_one_claim_socket_at_a_time(self):
        offer = await self.open_offer()
        await self.open_claim()
        self.assertEqual("transfer_claim_opened", (await self.notice(offer))["type"])
        # The slot is taken: to anyone else there is no such path.
        self.assertEqual(404, await self.refused_status(TRANSFER_CLAIM_PATH))
        self.assertEqual(1, self.application._transfer_offer.claim_number)

    async def test_claim_that_loses_the_race_after_the_upgrade_is_closed(self):
        offer = await self.open_offer()
        first = await self.open_claim()
        self.assertEqual("transfer_claim_opened", (await self.notice(offer))["type"])
        # Admitted by process_request while the slot looked free.
        # The handler asks again on arrival and acts on that answer.
        with patch.object(
            self.application,
            "_transfer_claimable",
            side_effect=[True, False],
        ):
            second = await connect(
                self.base_url + TRANSFER_CLAIM_PATH, subprotocols=[SUBPROTOCOL]
            )
            self.sockets.append(second)
            self.assertEqual(1008, await self.closed(second))
        await first.send(b"still-first")
        self.assertEqual(b"still-first", await asyncio.wait_for(offer.recv(), 10))
        self.assertEqual(1, self.application._transfer_offer.claim_number)

    async def test_claim_text_frame_is_dropped_without_counting(self):
        offer = await self.open_offer()
        claim = await self.open_claim()
        self.assertEqual("transfer_claim_opened", (await self.notice(offer))["type"])
        # A claim cannot speak the relay's control language to the offer.
        await claim.send(encode_message({"type": "transfer_ended", "reason": "x"}))
        self.assertEqual(
            {
                "type": "transfer_claim_closed",
                "claim": 1,
                "reason": "invalid_frame",
                "failures": 0,
            },
            await self.notice(offer),
        )
        self.assertEqual(1008, await self.closed(claim))
        self.assertTrue(self.application.transfer_offer_pending)

    async def test_new_offer_replaces_the_pending_one(self):
        first = await self.open_offer()
        second = await self.open_offer()
        self.assertEqual("replaced", await self.ended(first))
        claim = await self.open_claim()
        self.assertEqual("transfer_claim_opened", (await self.notice(second))["type"])
        await claim.send(b"to-second")
        self.assertEqual(b"to-second", await asyncio.wait_for(second.recv(), 10))

    async def test_offer_is_destroyed_when_its_socket_closes(self):
        offer = await self.open_offer()
        claim = await self.open_claim()
        self.assertEqual("transfer_claim_opened", (await self.notice(offer))["type"])
        await offer.close()
        self.assertEqual(1008, await self.closed(claim))
        await self.wait_until(lambda: not self.application.transfer_offer_pending)
        self.assertEqual(404, await self.refused_status(TRANSFER_CLAIM_PATH))

    async def test_unknown_or_malformed_control_ends_the_offer(self):
        for frame in (
            {"type": "transfer_everything"},
            {"type": "transfer_freeze", "extra": 1},
            {"type": "transfer_failed"},
            {"type": "transfer_failed", "claim": True},
            {"type": "transfer_confirmed", "claim": "1"},
        ):
            offer = await self.open_offer()
            await offer.send(encode_message(frame))
            self.assertEqual("invalid_request", (await self.notice(offer))["code"])
            self.assertEqual(1008, await self.closed(offer))
            await self.wait_until(
                lambda: not self.application.transfer_offer_pending
            )

    async def test_maintenance_control_tells_viewers_and_keeps_the_transfer_open(self):
        offer, claim = await self.bridged()
        paired, _code = await self.pair()
        captions, response = await self.open_captions(paired)
        self.sockets.append(captions)
        self.assertEqual("authenticated", response["type"])
        await self.control(offer, "transfer_maintenance")
        self.assertEqual(
            {"type": "transfer_maintenance", "announced": True},
            await self.notice(offer),
        )
        with self.assertRaises(ConnectionClosed) as caught:
            await asyncio.wait_for(captions.recv(), timeout=5)
        self.assertEqual(1001, caught.exception.rcvd.code)
        self.assertIn("Paused for updates", caught.exception.rcvd.reason)
        # The two hosts are still connected: the release frame travels next.
        await offer.send(OPAQUE)
        self.assertEqual(OPAQUE, await asyncio.wait_for(claim.recv(), 5))
        await self.control(offer, "transfer_maintenance", extra=1)
        self.assertEqual("invalid_request", (await self.notice(offer))["code"])

    async def test_confirmed_claim_leaving_spends_the_offer(self):
        offer, claim = await self.bridged()
        await claim.close()
        self.assertEqual(
            {
                "type": "transfer_claim_closed",
                "claim": 1,
                "reason": "closed",
                "failures": 0,
            },
            await self.notice(offer),
        )
        # The one-time code is used up, but the old Host keeps its socket
        # for freeze control until it lets go.
        self.assertEqual(404, await self.refused_status(TRANSFER_CLAIM_PATH))
        await self.control(offer, "transfer_freeze")
        self.assertEqual(
            {"type": "transfer_frozen", "frozen": True}, await self.notice(offer)
        )

    # -- time limits ----------------------------------------------------------

    async def test_pending_offer_expires(self):
        self.assertEqual(600, server.TRANSFER_OFFER_TTL_SECONDS)
        with patch.object(server, "TRANSFER_OFFER_TTL_SECONDS", 0.2):
            self.write_key()
            offer = await self.open_offer_socket()
            self.assertEqual("transfer_offered", (await self.notice(offer))["type"])
            claim = await self.open_claim()
            self.assertEqual(
                "transfer_claim_opened", (await self.notice(offer))["type"]
            )
            self.assertEqual("expired", await self.ended(offer))
            self.assertEqual(1008, await self.closed(claim))
        self.assertFalse(self.application.transfer_offer_pending)
        self.assertEqual(404, await self.refused_status(TRANSFER_CLAIM_PATH))

    async def test_confirmed_transfer_is_not_cut_off_by_the_offer_ttl(self):
        # One second, not a fifth: on a loaded runner the handshake below has
        # to finish inside the TTL for the test to be about what it says.
        with patch.object(server, "TRANSFER_OFFER_TTL_SECONDS", 1.0):
            self.write_key()
            offer = await self.open_offer_socket()
            self.assertEqual("transfer_offered", (await self.notice(offer))["type"])
            claim = await self.open_claim()
            self.assertEqual(
                "transfer_claim_opened", (await self.notice(offer))["type"]
            )
            await claim.send(b"hello")
            self.assertEqual(b"hello", await asyncio.wait_for(offer.recv(), 10))
            await self.control(offer, "transfer_confirmed", claim=1)
            await self.wait_until(
                lambda: self.application._transfer_offer.confirmed
            )
            await asyncio.sleep(1.5)
        await offer.send(b"after-ttl")
        self.assertEqual(b"after-ttl", await asyncio.wait_for(claim.recv(), 10))

    async def test_silent_claim_is_dropped_without_counting(self):
        self.assertEqual(5.0, server.TRANSFER_CLAIM_HELLO_SECONDS)
        offer = await self.open_offer()
        with patch.object(server, "TRANSFER_CLAIM_HELLO_SECONDS", 0.2):
            claim = await self.open_claim()
            self.assertEqual(
                {"type": "transfer_claim_opened", "claim": 1},
                await self.notice(offer),
            )
            self.assertEqual(
                {
                    "type": "transfer_claim_closed",
                    "claim": 1,
                    "reason": "hello_timeout",
                    "failures": 0,
                },
                await self.notice(offer),
            )
            self.assertEqual(1008, await self.closed(claim))
        # The slot is free again and the offer has lost nothing.
        self.assertEqual(0, self.application._transfer_offer.failures)
        await self.open_claim()
        self.assertEqual(
            {"type": "transfer_claim_opened", "claim": 2}, await self.notice(offer)
        )

    async def test_unconfirmed_claim_is_dropped_without_counting(self):
        self.assertEqual(10.0, server.TRANSFER_CLAIM_CONFIRM_SECONDS)
        offer = await self.open_offer()
        with patch.object(server, "TRANSFER_CLAIM_CONFIRM_SECONDS", 0.2):
            claim = await self.open_claim()
            self.assertEqual(
                "transfer_claim_opened", (await self.notice(offer))["type"]
            )
            await claim.send(b"hello-and-share")
            self.assertEqual(
                b"hello-and-share", await asyncio.wait_for(offer.recv(), 10)
            )
            # The offer side answers, the claim never completes.
            await offer.send(b"hello-back")
            self.assertEqual(b"hello-back", await asyncio.wait_for(claim.recv(), 10))
            self.assertEqual(
                {
                    "type": "transfer_claim_closed",
                    "claim": 1,
                    "reason": "confirm_timeout",
                    "failures": 0,
                },
                await self.notice(offer),
            )
            self.assertEqual(1008, await self.closed(claim))
        self.assertTrue(self.application.transfer_offer_pending)
        self.assertEqual(0, self.application._transfer_offer.failures)

    async def test_confirmed_claim_outlives_the_confirmation_window(self):
        with patch.object(server, "TRANSFER_CLAIM_CONFIRM_SECONDS", 0.2):
            offer, claim = await self.bridged()
            await asyncio.sleep(0.4)
        await claim.send(b"still-here")
        self.assertEqual(b"still-here", await asyncio.wait_for(offer.recv(), 10))

    # -- budgets ----------------------------------------------------------------

    async def test_three_reported_wrong_confirmations_destroy_the_offer(self):
        self.assertEqual(3, server.TRANSFER_CLAIM_FAILURE_BUDGET)
        offer = await self.open_offer()
        for number in (1, 2, 3):
            claim = await self.open_claim()
            self.assertEqual(
                {"type": "transfer_claim_opened", "claim": number},
                await self.notice(offer),
            )
            await claim.send(b"hello")
            self.assertEqual(b"hello", await asyncio.wait_for(offer.recv(), 10))
            await claim.send(b"wrong-confirmation")
            self.assertEqual(
                b"wrong-confirmation", await asyncio.wait_for(offer.recv(), 10)
            )
            if number == 1:
                # A verdict on a claim that is not the one in the slot is
                # ignored rather than landing on its successor.
                await self.control(offer, "transfer_failed", claim=7)
            await self.control(offer, "transfer_failed", claim=number)
            if number == 3:
                # The last notice is followed at once by the close; the
                # close is what is asserted below.
                break
            self.assertEqual(
                {
                    "type": "transfer_claim_closed",
                    "claim": number,
                    "reason": "failed",
                    "failures": number,
                },
                await self.notice(offer),
            )
            self.assertEqual(1008, await self.closed(claim))
        self.assertEqual("failures", await self.ended(offer))
        self.assertFalse(self.application.transfer_offer_pending)
        self.assertEqual(404, await self.refused_status(TRANSFER_CLAIM_PATH))

    async def test_connection_budget_destroys_the_offer_without_any_failure(self):
        self.assertEqual(20, server.TRANSFER_CLAIM_CONNECTION_BUDGET)
        self.assertGreater(
            server.TRANSFER_CLAIM_CONNECTION_BUDGET,
            server.TRANSFER_CLAIM_FAILURE_BUDGET,
        )
        offer = await self.open_offer()
        for number in range(1, 21):
            claim = await self.open_claim()
            self.assertEqual(
                {"type": "transfer_claim_opened", "claim": number},
                await self.notice(offer),
            )
            await claim.close()
            if number == 20:
                break
            self.assertEqual(
                {
                    "type": "transfer_claim_closed",
                    "claim": number,
                    "reason": "closed",
                    "failures": 0,
                },
                await self.notice(offer),
            )
            self.assertTrue(self.application.transfer_offer_pending)
        self.assertEqual("claims", await self.ended(offer))
        self.assertEqual(404, await self.refused_status(TRANSFER_CLAIM_PATH))

    async def test_last_budgeted_claim_can_still_complete(self):
        offer = await self.open_offer()
        with patch.object(server, "TRANSFER_CLAIM_CONNECTION_BUDGET", 2):
            first = await self.open_claim()
            await self.notice(offer)
            await first.close()
            self.assertEqual("closed", (await self.notice(offer))["reason"])
            second = await self.open_claim()
            self.assertEqual(
                {"type": "transfer_claim_opened", "claim": 2}, await self.notice(offer)
            )
            await second.send(b"hello")
            self.assertEqual(b"hello", await asyncio.wait_for(offer.recv(), 10))
            await self.control(offer, "transfer_confirmed", claim=2)
            await self.wait_until(
                lambda: self.application._transfer_offer.confirmed
            )
            await second.close()
            self.assertEqual("closed", (await self.notice(offer))["reason"])
        # Spent, not destroyed: the offer socket is still the Host's.
        self.assertTrue(self.application.transfer_offer_pending)

    async def test_transfer_sockets_count_against_the_connection_limit(self):
        offer = await self.open_offer()
        await self.open_claim()
        await self.notice(offer)
        self.assertEqual(2, self.application.active_handler_count)

    # -- freeze -------------------------------------------------------------------

    async def freeze(self, offer):
        await self.control(offer, "transfer_freeze")
        self.assertEqual(
            {"type": "transfer_frozen", "frozen": True}, await self.notice(offer)
        )

    async def test_freeze_refuses_pairing_and_keeps_the_invitation(self):
        offer = await self.open_offer()
        enrollment = self.store.create_enrollment(300)
        await self.freeze(offer)
        self.assertTrue(self.application.transfer_frozen)

        refused = await self.pair_code(enrollment.code)
        self.assertEqual(
            {
                "type": "error",
                "code": "transfer_in_progress",
                "message": "The guild server is moving. Try again in a minute.",
            },
            refused,
        )
        self.assertEqual([], self.store.list_devices())

        await self.control(offer, "transfer_unfreeze")
        self.assertEqual(
            {"type": "transfer_frozen", "frozen": False}, await self.notice(offer)
        )
        # The same code, untouched by the refusal.
        paired = await self.pair_code(enrollment.code)
        self.assertEqual("paired", paired["type"])

    async def test_freeze_clears_pending_viewer_links_and_refuses_binds(self):
        paired, _code = await self.pair()
        before = await self.request_viewer_link(paired)
        self.assertEqual("viewer_link", before["type"])
        offer = await self.open_offer()
        publisher, _authentication = await self.open_publisher()
        self.sockets.append(publisher)

        async def bind(code):
            await publisher.send(
                encode_message(
                    {"type": "viewer_link", "code": code, "viewer_token": VIEWER}
                )
            )
            return decode_message(await asyncio.wait_for(publisher.recv(), 10))

        bound_before = self.store.authenticate_device_viewer_token(
            paired["device_id"], paired["device_token"]
        )
        await self.freeze(offer)
        self.assertEqual({}, self.application._pending_viewer_links)
        self.assertEqual({}, self.application._viewer_link_by_device)

        # A link requested during the freeze cannot be bound either.
        during = await self.request_viewer_link(paired)
        for code in (before["code"], during["code"]):
            self.assertEqual(
                {"type": "viewer_linked", "code": code, "linked": False},
                await bind(code),
            )
        self.assertEqual(
            bound_before,
            self.store.authenticate_device_viewer_token(
                paired["device_id"], paired["device_token"]
            ),
        )

        await self.control(offer, "transfer_unfreeze")
        await self.notice(offer)
        # Cleared means cleared: neither old code comes back to life.
        for code in (before["code"], during["code"]):
            self.assertFalse((await bind(code))["linked"])
        after = await self.request_viewer_link(paired)
        self.assertTrue((await bind(after["code"]))["linked"])
        self.assertEqual(
            VIEWER,
            self.store.authenticate_device_viewer_token(
                paired["device_id"], paired["device_token"]
            ),
        )

    async def start_blocked_bind(self):
        """A viewer bind admitted before the freeze, its write held mid-way."""
        paired, _code = await self.pair()
        link = await self.request_viewer_link(paired)
        offer = await self.open_offer()
        publisher, _authentication = await self.open_publisher()
        self.sockets.append(publisher)
        entered = threading.Event()
        release = threading.Event()
        original = self.store.bind_viewer_token

        def held(*arguments):
            entered.set()
            release.wait(30)
            return original(*arguments)

        patcher = patch.object(self.store, "bind_viewer_token", held)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(release.set)
        await publisher.send(
            encode_message(
                {"type": "viewer_link", "code": link["code"], "viewer_token": VIEWER}
            )
        )
        await self.wait_until(entered.is_set)
        return paired, offer, publisher, release

    async def test_freeze_waits_for_an_admitted_viewer_bind(self):
        paired, offer, publisher, release = await self.start_blocked_bind()
        await self.control(offer, "transfer_freeze")
        # The write is still running: confirming now would let it commit
        # after the sender's snapshot, outside the delta.
        with self.assertRaises(TimeoutError):
            await self.notice(offer, timeout=0.3)
        release.set()
        self.assertEqual(
            {"type": "transfer_frozen", "frozen": True}, await self.notice(offer)
        )
        # By the time the freeze is confirmed the binding is on disk.
        self.assertEqual(
            VIEWER,
            CredentialStore(self.database, relay_tests.PEPPER)
            .authenticate_device_viewer_token(
                paired["device_id"], paired["device_token"]
            ),
        )
        self.assertTrue(
            decode_message(await asyncio.wait_for(publisher.recv(), 10))["linked"]
        )

    async def test_freeze_reports_not_frozen_when_an_admitted_bind_overruns(self):
        _paired, offer, _publisher, release = await self.start_blocked_bind()
        self.assertGreater(10.0, server.TRANSFER_FREEZE_DRAIN_SECONDS)
        with patch.object(server, "TRANSFER_FREEZE_DRAIN_SECONDS", 0.2):
            await self.control(offer, "transfer_freeze")
            self.assertEqual(
                {"type": "transfer_frozen", "frozen": False},
                await self.notice(offer),
            )
        # Still refusing new binds and pairing: the Host decides what next.
        self.assertTrue(self.application.transfer_frozen)
        release.set()

    async def test_freeze_leaves_profile_writes_and_typed_chat_alone(self):
        paired, _code = await self.pair()
        outsider, _code = await self.pair()
        self.assign_quick_chat_profile(paired, "Old name", "mage")
        offer = await self.open_offer()
        await self.freeze(offer)

        # Typed chat depends only on the relay: no publisher, no worker,
        # and a transfer holding the database still.
        self.assertEqual(0, self.application.active_publisher_count)
        self.assertEqual(0, self.application.active_quick_chat_worker_count)
        updated = await self.quick_chat_profile(paired, name="Mira Lyn")
        self.assertEqual({"type": "quick_chat_profile", "name": "Mira Lyn"}, updated)
        record = self.store.authenticate_device_record(
            paired["device_id"], paired["device_token"]
        )
        self.assertEqual("Mira Lyn", record.quick_chat_name)

        # Only the first device stays in the voice channel.
        await self.refresh_audience(viewer_tokens=self.viewer_tokens[:1])
        captions, authentication = await self.open_captions(paired)
        self.sockets.append(captions)
        self.assertEqual("authenticated", authentication["type"])
        outside, refusal = await self.open_captions(outsider)
        self.sockets.append(outside)
        self.assertEqual("voice_channel_required", refusal["code"])

        response = await self.send_quick_chat(paired, text="Still typing.")
        self.assertEqual("quick_chat_sent", response["type"])
        caption = decode_message(await asyncio.wait_for(captions.recv(), 10))
        self.assertEqual("Still typing.", caption["event"]["text"])
        self.assertEqual("Mira Lyn [typed]", caption["event"]["speaker"])
        self.assertTrue(self.application.transfer_frozen)

    async def test_frozen_relay_never_pipes_member_traffic_into_the_transfer(self):
        offer, claim = await self.bridged()
        await self.freeze(offer)
        paired, _code = await self.pair_after_unfreeze(offer)
        self.assign_quick_chat_profile(paired)
        await self.freeze(offer)
        response = await self.send_quick_chat(paired, text="private typed line")
        self.assertEqual("quick_chat_sent", response["type"])
        for websocket in (offer, claim):
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(websocket.recv(), 0.05)

    async def pair_after_unfreeze(self, offer):
        await self.control(offer, "transfer_unfreeze")
        self.assertEqual(
            {"type": "transfer_frozen", "frozen": False}, await self.notice(offer)
        )
        return await self.pair()

    async def test_freeze_ends_with_the_offer_socket(self):
        offer = await self.open_offer()
        await self.freeze(offer)
        await offer.close()
        await self.wait_until(lambda: not self.application.transfer_frozen)
        paired, _code = await self.pair()
        self.assertEqual("paired", paired["type"])

    async def test_flag_file_freezes_without_an_offer_and_survives_a_restart(self):
        enrollment = self.store.create_enrollment(300)
        self.assertFalse(self.application.transfer_frozen)
        self.freeze_file.write_bytes(b"")
        self.assertTrue(self.application.transfer_frozen)
        refused = await self.pair_code(enrollment.code)
        self.assertEqual("transfer_in_progress", refused["code"])

        # A new relay process has no memory of any offer; the file is enough.
        restarted = GuildHubServer(self.store, self.config)
        second, base_url = await self.start_application(restarted)
        self.extra_servers.append(second)
        self.assertTrue(restarted.transfer_frozen)
        refused = await self.pair_code(enrollment.code, base_url)
        self.assertEqual("transfer_in_progress", refused["code"])

        # The offer socket letting go does not thaw what the file holds.
        offer = await self.open_offer()
        await self.control(offer, "transfer_unfreeze")
        self.assertEqual(
            {"type": "transfer_frozen", "frozen": True}, await self.notice(offer)
        )

        self.freeze_file.unlink()
        self.assertFalse(restarted.transfer_frozen)
        paired = await self.pair_code(enrollment.code, base_url)
        self.assertEqual("paired", paired["type"])

    async def test_flag_file_refuses_binds_and_clears_links_made_under_it(self):
        paired, _code = await self.pair()
        self.freeze_file.write_bytes(b"")
        link = await self.request_viewer_link(paired)
        self.assertFalse(
            await self.application._bind_viewer(
                server.ViewerLink(code=link["code"], viewer_token=VIEWER)
            )
        )
        self.assertEqual({}, self.application._pending_viewer_links)
        self.freeze_file.unlink()
        self.assertFalse(
            await self.application._bind_viewer(
                server.ViewerLink(code=link["code"], viewer_token=VIEWER)
            )
        )

    async def test_flag_file_setting_is_optional(self):
        application = GuildHubServer(
            self.store, replace(self.config, transfer_freeze_file="")
        )
        self.freeze_file.write_bytes(b"")
        self.assertFalse(application.transfer_frozen)

    async def test_housekeeping_waits_while_frozen(self):
        application = GuildHubServer(
            self.store, replace(self.config, housekeeping_seconds=0.02)
        )
        self.freeze_file.write_bytes(b"")
        with patch.object(
            application.store, "prune_enrollments", return_value=0
        ) as prune, patch.object(
            application.store, "revoke_unused_devices", return_value=(0, 0)
        ), patch.object(
            application.store, "forget_revoked_devices", return_value=0
        ):
            housekeeping = asyncio.create_task(
                server._housekeeping_loop(application)
            )
            try:
                await asyncio.sleep(0.2)
                self.assertEqual(0, prune.call_count)
                self.freeze_file.unlink()
                await self.wait_until(lambda: prune.call_count > 0)
            finally:
                housekeeping.cancel()
                await asyncio.gather(housekeeping, return_exceptions=True)

    async def test_nothing_changes_for_existing_endpoints_when_no_transfer_is_active(self):
        self.write_key()
        paired, _code = await self.pair()
        self.assertEqual("paired", paired["type"])
        link = await self.request_viewer_link(paired)
        self.assertTrue(
            await self.application._bind_viewer(
                server.ViewerLink(code=link["code"], viewer_token=VIEWER)
            )
        )
        status, _headers, _body = await self.fetch("/healthz")
        self.assertEqual(200, status)
        status, _headers, _body = await self.fetch("/v1/unknown")
        self.assertEqual(404, status)


class TransferCliTests(unittest.TestCase):
    def serve_config(self, extra_environment):
        with tempfile.TemporaryDirectory() as temporary_directory:
            environment = {
                "VEMALS_EARS_PEPPER": relay_tests.PEPPER,
                "VEMALS_EARS_PUBLISHER_TOKEN": relay_tests.PUBLISHER_TOKEN,
                "VEMALS_EARS_PUBLIC_SERVER_URL": relay_tests.PUBLIC_SERVER_URL,
                "VEMALS_EARS_CLIENT_DOWNLOAD_URL": relay_tests.CLIENT_DOWNLOAD_URL,
                **extra_environment,
            }
            captured = {}

            def application(store, config):
                captured["config"] = config
                return object()

            with patch.dict(os.environ, environment, clear=True), patch.object(
                server, "GuildHubServer", side_effect=application
            ), patch.object(server, "run_server", new=AsyncMock()), patch.object(
                server.logging, "basicConfig"
            ):
                result = server.main(
                    [
                        "--database",
                        str(Path(temporary_directory) / "guild-hub.sqlite3"),
                        "serve",
                    ]
                )
            self.assertEqual(0, result)
            return captured["config"]

    def test_serve_has_no_transfer_paths_unless_the_environment_names_a_key_file(self):
        config = self.serve_config({})
        self.assertEqual("", config.transfer_key_file)
        self.assertEqual("", config.transfer_freeze_file)

    def test_serve_takes_the_transfer_files_from_the_environment(self):
        config = self.serve_config(
            {
                "VEMALS_EARS_TRANSFER_KEY_FILE": " C:/state/secrets/transfer-key ",
                "VEMALS_EARS_TRANSFER_FREEZE_FILE": "C:/state/transfer-frozen",
            }
        )
        self.assertEqual("C:/state/secrets/transfer-key", config.transfer_key_file)
        self.assertEqual("C:/state/transfer-frozen", config.transfer_freeze_file)
        # A path, never the secret: the setting does not appear as a flag.
        self.assertNotIn("transfer", server.build_parser().format_help())

    def test_transfer_file_settings_must_be_paths(self):
        for field in ("transfer_key_file", "transfer_freeze_file"):
            for value in (None, 7, "bad\x00path"):
                with self.subTest(field=field, value=value), self.assertRaises(
                    ValueError
                ):
                    ServerConfig(
                        publisher_token=relay_tests.PUBLISHER_TOKEN,
                        public_server_url=relay_tests.PUBLIC_SERVER_URL,
                        client_download_url=relay_tests.CLIENT_DOWNLOAD_URL,
                        **{field: value},
                    )


if __name__ == "__main__":
    unittest.main()
