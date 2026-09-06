"""A failed voice join reaches worker.log with what the adapter knew.

Shino, 2026-09-06: fifty-two consecutive joins failed between 07:11 and
08:20 and every line read "capture pipeline stopped
(adapter_failed:voice_connection_failed)". The adapter now sends the
connection's last state, Discord's close code and the error class along
with the reason; these pin that they arrive, and that nothing else does.
"""

from __future__ import annotations

import unittest

from VoiceCaptions.coordinator import CoordinatorError, VoiceCaptionCoordinator
from VoiceCaptions.tests.test_coordinator import (
    ConcurrentFakeSTT,
    EventLines,
    FakeSink,
)


def blocked(payload: dict) -> dict:
    return {"component": "capture_pipeline", "status": "blocked", **payload}


class JoinFailureDetailTests(unittest.IsolatedAsyncioTestCase):
    async def code_for(self, payload: dict) -> str:
        instance = VoiceCaptionCoordinator(ConcurrentFakeSTT(), FakeSink())
        try:
            with self.assertRaises(CoordinatorError) as caught:
                await instance.feed_line(
                    EventLines().line("health.changed", blocked(payload))
                )
        finally:
            await instance.shutdown()
        return caught.exception.code

    async def test_the_state_close_code_and_cause_reach_the_log(self):
        self.assertEqual(
            "adapter_failed:voice_connection_failed:signalling:4014:AbortError",
            await self.code_for({
                "reason": "voice_connection_failed",
                "voiceState": "signalling",
                "closeCode": 4014,
                "cause": "AbortError",
            }),
        )

    async def test_a_reason_alone_reads_exactly_as_it_did_before(self):
        self.assertEqual(
            "adapter_failed:voice_connection_failed",
            await self.code_for({"reason": "voice_connection_failed"}),
        )

    async def test_free_text_never_becomes_a_log_word(self):
        """A message could carry an endpoint or a token; only words pass."""
        self.assertEqual(
            "adapter_failed:voice_connection_failed",
            await self.code_for({
                "reason": "voice_connection_failed",
                "voiceState": "wss://voice.example/?token=abc",
                "closeCode": "4014",
                "cause": "Error with spaces",
            }),
        )

    async def test_a_close_code_outside_the_websocket_range_is_dropped(self):
        self.assertEqual(
            "adapter_failed:voice_connection_failed:disconnected",
            await self.code_for({
                "reason": "voice_connection_failed",
                "voiceState": "Disconnected",
                "closeCode": 123456,
            }),
        )

    async def test_a_boolean_is_not_a_close_code(self):
        self.assertEqual(
            "adapter_failed:voice_connection_failed",
            await self.code_for({"reason": "voice_connection_failed", "closeCode": True}),
        )


if __name__ == "__main__":
    unittest.main()
