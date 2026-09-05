import json
import unittest

from GuildHub.protocol import (
    GuildHubProtocolError,
    caption_envelope,
    cursor_advance,
    decode_message,
    encode_message,
    parse_caption_envelope,
    parse_cursor_advance,
    parse_audience_update,
    parse_device_authentication,
    parse_pair_request,
    parse_publish,
    parse_publish_request,
    parse_publisher_command,
    parse_quick_chat,
    parse_quick_chat_acknowledgement,
    parse_quick_chat_command,
    parse_quick_chat_delivery,
    parse_quick_chat_profile,
    parse_quick_chat_profile_request,
    ViewerEnrollment,
    parse_viewer_enrollment,
    parse_viewer_link,
    parse_viewer_link_request,
    parse_viewer_link_response,
    quick_chat_acknowledgement,
    quick_chat_command,
    quick_chat_delivery,
    quick_chat_profile,
    viewer_link_response,
)
from VoiceCaptions.events import CLASS_COLORS, CaptionEvent, event_to_json


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


class ProtocolTests(unittest.TestCase):
    def test_pair_request_is_strict_and_bounded(self):
        request = parse_pair_request(
            {
                "type": "pair",
                "enrollment_code": "ghe_valid-token",
                "device_name": "Zack's desktop",
                "client_version": "0.1.0",
            }
        )
        self.assertEqual("Zack's desktop", request.device_name)

        with self.assertRaises(GuildHubProtocolError):
            parse_pair_request(
                {
                    "type": "pair",
                    "enrollment_code": "secret with spaces",
                    "device_name": "desktop",
                    "client_version": "0.1.0",
                }
            )

        with self.assertRaises(GuildHubProtocolError):
            parse_pair_request(
                {
                    "type": "pair",
                    "enrollment_code": "ghe_valid-token",
                    "device_name": "desktop",
                    "client_version": "0.1.0",
                    "unexpected_secret": "must-not-be-ignored",
                }
            )

    def test_device_name_rejects_invisible_format_controls(self):
        with self.assertRaises(GuildHubProtocolError):
            parse_pair_request(
                {
                    "type": "pair",
                    "enrollment_code": "ghe_valid-token",
                    "device_name": "admin\u202eguest",
                    "client_version": "0.1.0",
                }
            )

    def test_device_auth_rejects_negative_cursor(self):
        with self.assertRaises(GuildHubProtocolError):
            parse_device_authentication(
                {
                    "type": "authenticate",
                    "device_id": "device_1",
                    "device_token": "ghd_token",
                    "last_stream_id": "stream_one",
                    "last_cursor": -1,
                }
            )

    def test_caption_round_trip_reuses_voice_caption_validation(self):
        envelope = caption_envelope("stream_one", 9, CAPTION)
        decoded = parse_caption_envelope(
            decode_message(encode_message(envelope))
        )
        self.assertEqual("stream_one", decoded.stream_id)
        self.assertEqual(9, decoded.cursor)
        self.assertEqual(CAPTION, decoded.event)

        published = parse_publish(
            {"type": "publish", "event": json.loads(json.dumps(envelope["event"]))}
        )
        self.assertEqual(CAPTION, published)

    def test_wire_limit_is_measured_in_bytes(self):
        with self.assertRaises(GuildHubProtocolError):
            decode_message('"' + ("x" * 17000) + '"')

    def test_duplicate_json_fields_are_rejected(self):
        with self.assertRaises(GuildHubProtocolError):
            decode_message('{"type":"clear","type":"caption"}')

    def test_invalid_nested_event_never_reaches_overlay(self):
        with self.assertRaises(GuildHubProtocolError):
            parse_caption_envelope(
                {
                    "type": "caption_event",
                    "stream_id": "stream_one",
                    "cursor": 1,
                    "event": {"type": "caption"},
                }
            )

        event = json.loads(
            json.dumps(caption_envelope("stream_one", 1, CAPTION)["event"])
        )
        event["hidden"] = "not part of v1"
        with self.assertRaises(GuildHubProtocolError):
            parse_caption_envelope(
                {
                    "type": "caption_event",
                    "stream_id": "stream_one",
                    "cursor": 1,
                    "event": event,
                }
            )

    def test_unknown_local_event_type_is_not_encoded_as_clear(self):
        with self.assertRaises(GuildHubProtocolError):
            caption_envelope("stream_one", 1, object())  # type: ignore[arg-type]

    def test_quick_chat_is_strict_bounded_and_redacted(self):
        request = parse_quick_chat(
            {
                "type": "quick_chat",
                "request_id": "chat_018",
                "device_id": "device_1",
                "device_token": "ghd_private",
                "text": "Use the gateway now.",
            }
        )
        self.assertEqual("chat_018", request.request_id)
        self.assertEqual("Use the gateway now.", request.text)
        self.assertNotIn("Use the gateway now.", repr(request))
        self.assertNotIn("ghd_private", repr(request))

        for changes in (
            {"text": "x" * 321},
            {"request_id": "has spaces"},
            {"text": "line one\nline two"},
            {"character_name": "client-supplied"},
            {"unexpected": "field"},
        ):
            payload = {
                "type": "quick_chat",
                "request_id": "chat_018",
                "device_id": "device_1",
                "device_token": "ghd_private",
                "text": "Ready.",
            }
            payload.update(changes)
            with self.subTest(changes=changes), self.assertRaises(
                GuildHubProtocolError
            ):
                parse_quick_chat(payload)

    def test_quick_chat_acknowledgement_is_strict(self):
        payload = quick_chat_acknowledgement("chat_018", 7)
        acknowledgement = parse_quick_chat_acknowledgement(payload)
        self.assertEqual("chat_018", acknowledgement.request_id)
        self.assertEqual(7, acknowledgement.cursor)
        with self.assertRaises(GuildHubProtocolError):
            parse_quick_chat_acknowledgement(
                {
                    "type": "quick_chat_sent",
                    "request_id": "chat_018",
                    "cursor": 7,
                    "text": "leak",
                }
            )

    def test_quick_chat_profile_get_set_and_response_are_strict(self):
        private = "ghd_private-device-token"
        get_request = parse_quick_chat_profile_request(
            {
                "type": "quick_chat_profile",
                "device_id": "device_1",
                "device_token": private,
            }
        )
        self.assertIsNone(get_request.name)
        self.assertNotIn(private, repr(get_request))

        chosen_name = "\uff2dira"
        set_request = parse_quick_chat_profile_request(
            {
                "type": "quick_chat_profile",
                "device_id": "device_1",
                "device_token": private,
                "name": chosen_name,
            }
        )
        self.assertEqual("Mira", set_request.name)
        self.assertNotIn(chosen_name, repr(set_request))

        self.assertIsNone(
            parse_quick_chat_profile(quick_chat_profile(None)).quick_chat_name
        )
        profile = parse_quick_chat_profile(quick_chat_profile("Mira"))
        self.assertEqual("Mira", profile.quick_chat_name)
        self.assertNotIn("Mira", repr(profile))

        invalid_requests = (
            {
                "type": "quick_chat_profile",
                "device_id": "device_1",
                "device_token": private,
                "name": None,
            },
            {
                "type": "quick_chat_profile",
                "device_id": "device_1",
                "device_token": private,
                "name": "Mira\u202eAdmin",
            },
            {
                "type": "quick_chat_profile",
                "device_id": "device_1",
                "device_token": private,
                "name": "Mira",
                "target_device_id": "device_2",
            },
        )
        for payload in invalid_requests:
            with self.subTest(payload=payload), self.assertRaises(
                GuildHubProtocolError
            ):
                parse_quick_chat_profile_request(payload)

        for payload in (
            {"type": "quick_chat_profile", "name": "Mira", "extra": True},
            {"type": "quick_chat_profile", "name": 1},
        ):
            with self.subTest(payload=payload), self.assertRaises(
                GuildHubProtocolError
            ):
                parse_quick_chat_profile(payload)

    def test_worker_quick_chat_contract_is_strict_and_redacted(self):
        command_payload = quick_chat_command(
            "qcw_018", "Miralyn", "mage", "Use the gateway now."
        )
        command = parse_quick_chat_command(command_payload)
        self.assertEqual("qcw_018", command.request_id)
        self.assertEqual("Miralyn", command.speaker)
        self.assertEqual("mage", command.wow_class)
        self.assertEqual("Use the gateway now.", command.text)
        self.assertNotIn(command.text, repr(command))

        for change in (
            {"request_id": "bad id"},
            {"wow_class": "bard"},
            {"status": "delivered"},
        ):
            payload = dict(command_payload)
            payload.update(change)
            with self.subTest(change=change), self.assertRaises(
                GuildHubProtocolError
            ):
                parse_quick_chat_command(payload)

    def test_worker_delivery_contract_carries_no_message_body(self):
        for status in ("delivered", "no_active_session", "delivery_failed"):
            with self.subTest(status=status):
                payload = quick_chat_delivery("qcw_018", status)
                delivery = parse_quick_chat_delivery(payload)
                self.assertEqual("qcw_018", delivery.request_id)
                self.assertEqual(status, delivery.status)
                self.assertNotIn("text", payload)

        with self.assertRaises(GuildHubProtocolError):
            parse_quick_chat_delivery(
                {
                    "type": "quick_chat_delivery",
                    "request_id": "qcw_018",
                    "status": "maybe",
                }
            )


class PublishContractTests(unittest.TestCase):
    """The relay's publish parser and event_to_json are one contract.

    They live in different packages and are edited by different pieces of
    work. Adding wow_class to the encoder without adding it here rejected
    every caption from a MAPPED speaker while unmapped ones went through,
    so the bot stayed up until the one person with a character mapped
    spoke - then the publisher failed and the capture pipeline came down.
    From Discord it looked like the bot joining and leaving at random.

    This test walks every shape the encoder can actually produce, so the
    next field added to a caption fails here instead of in a raid.
    """

    def caption(self, **overrides):
        fields = dict(
            speaker_id="discord-user-1",
            speaker="Mira",
            character="Miralyn",
            role="HEALER",
            text="Stack on me.",
            final=True,
            sequence=3,
            timestamp=9.5,
        )
        fields.update(overrides)
        return CaptionEvent(**fields)

    def published(self, event):
        return parse_publish(
            {"type": "publish", "event": json.loads(event_to_json(event))}
        )

    def test_an_unmapped_speaker_publishes(self):
        restored = self.published(self.caption())
        self.assertEqual("Stack on me.", restored.text)
        self.assertEqual("", restored.wow_class)

    def test_a_mapped_speaker_publishes_and_keeps_the_class(self):
        restored = self.published(self.caption(wow_class="paladin"))
        self.assertEqual("paladin", restored.wow_class)

    def test_every_class_the_encoder_can_emit_is_accepted(self):
        for wow_class in CLASS_COLORS:
            with self.subTest(wow_class=wow_class):
                self.assertEqual(
                    wow_class, self.published(self.caption(wow_class=wow_class)).wow_class
                )

    def test_an_unknown_field_is_still_refused(self):
        """The check stayed strict; it only learned one optional key."""
        payload = json.loads(event_to_json(self.caption()))
        payload["something_new"] = "x"
        with self.assertRaises(GuildHubProtocolError):
            parse_publish({"type": "publish", "event": payload})

    def test_a_missing_required_field_is_still_refused(self):
        payload = json.loads(event_to_json(self.caption()))
        payload.pop("role")
        with self.assertRaises(GuildHubProtocolError):
            parse_publish({"type": "publish", "event": payload})


class VoiceMembershipContractTests(unittest.TestCase):
    TOKEN = "0123456789abcdef01234567"
    CODE = "29CFGHJK"

    def test_viewer_link_request_and_response_are_exact(self):
        request = parse_viewer_link_request(
            {
                "type": "viewer_link_request",
                "device_id": "ghdev_member",
                "device_token": "ghd_secret",
            }
        )
        self.assertNotIn("ghd_secret", repr(request))
        response = parse_viewer_link_response(
            viewer_link_response(self.CODE, 1_700_000_300)
        )
        self.assertEqual(self.CODE, response.code)
        self.assertEqual(1_700_000_300, response.expires_at)

        for payload in (
            {"type": "viewer_link", "code": self.CODE, "expires_at": True},
            {
                "type": "viewer_link",
                "code": self.CODE,
                "expires_at": 123,
                "extra": "rejected",
            },
            {"type": "viewer_link", "code": "ILOU1234", "expires_at": 123},
        ):
            with self.subTest(payload=payload), self.assertRaises(
                GuildHubProtocolError
            ):
                parse_viewer_link_response(payload)

    def test_audience_and_link_controls_accept_only_opaque_tokens(self):
        audience = parse_audience_update(
            {
                "type": "audience_update",
                "generation": 4,
                "revision": 7,
                "viewer_tokens": [self.TOKEN],
            }
        )
        self.assertEqual((self.TOKEN,), audience.viewer_tokens)
        link = parse_viewer_link(
            {
                "type": "viewer_link",
                "code": self.CODE,
                "viewer_token": self.TOKEN,
            }
        )
        self.assertEqual(self.TOKEN, link.viewer_token)
        self.assertEqual(audience, parse_publisher_command({
            "type": "audience_update",
            "generation": 4,
            "revision": 7,
            "viewer_tokens": [self.TOKEN],
        }))

        for tokens in (
            [self.TOKEN, self.TOKEN],
            ["123456789012345678"],
            ["0123456789ABCDEF01234567"],
            ["discord-user-123456789"],
        ):
            with self.subTest(tokens=tokens), self.assertRaises(
                GuildHubProtocolError
            ):
                parse_audience_update(
                    {
                        "type": "audience_update",
                        "generation": 4,
                        "revision": 8,
                        "viewer_tokens": tokens,
                    }
                )

    def test_caption_routing_metadata_never_enters_caption_envelopes(self):
        request = parse_publish_request(
            {
                "type": "publish",
                "event": json.loads(event_to_json(CAPTION)),
                "audience_generation": 4,
                "audience_revision": 7,
            }
        )
        self.assertEqual(4, request.audience_generation)
        self.assertEqual(7, request.audience_revision)
        envelope = caption_envelope("stream_one", 9, request.event)
        self.assertNotIn("audience_generation", envelope)
        self.assertNotIn("audience_revision", envelope)
        self.assertNotIn("viewer_tokens", envelope)

        partial = {
            "type": "publish",
            "event": json.loads(event_to_json(CAPTION)),
            "audience_generation": 4,
        }
        with self.assertRaises(GuildHubProtocolError):
            parse_publish_request(partial)

    def test_cursor_advance_is_strict_and_has_no_content_payload(self):
        payload = cursor_advance("stream_one", 10)
        self.assertEqual(
            {"type": "cursor_advance", "stream_id": "stream_one", "cursor": 10},
            payload,
        )
        parsed = parse_cursor_advance(payload)
        self.assertEqual("stream_one", parsed.stream_id)
        self.assertEqual(10, parsed.cursor)
        for extra in ({"event": {}}, {"text": "private"}):
            with self.subTest(extra=extra), self.assertRaises(
                GuildHubProtocolError
            ):
                parse_cursor_advance(payload | extra)


class ClientVersionReportingTests(unittest.TestCase):
    """client_version is the one optional key on the authenticate frame.

    It is accepted here BEFORE any client sends it. Adding a field to one
    side of this wire contract and shipping it first is what broke the
    relay twice on 2026-08-30, in both directions.
    """

    BASE = {
        "type": "authenticate",
        "device_id": "device_1",
        "device_token": "ghd_token",
        "last_stream_id": "stream_one",
        "last_cursor": 4,
    }

    def test_a_client_that_sends_no_version_still_authenticates(self):
        """The compatibility guarantee. Every client in the field today
        sends exactly this shape, and must keep working untouched."""
        parsed = parse_device_authentication(dict(self.BASE))
        self.assertEqual("device_1", parsed.device_id)
        self.assertEqual(4, parsed.last_cursor)
        self.assertEqual("", parsed.client_version)

    def test_a_reported_version_is_carried_through(self):
        parsed = parse_device_authentication(
            dict(self.BASE, client_version="0.2.2")
        )
        self.assertEqual("0.2.2", parsed.client_version)
        self.assertEqual("device_1", parsed.device_id)

    def test_any_other_extra_key_is_still_rejected(self):
        """Optional means exactly one key, not open season."""
        with self.assertRaises(GuildHubProtocolError):
            parse_device_authentication(dict(self.BASE, surprise="1"))
        with self.assertRaises(GuildHubProtocolError):
            parse_device_authentication(
                dict(self.BASE, client_version="0.2.2", surprise="1")
            )

    def test_a_hostile_version_string_is_rejected_not_stored(self):
        for bad in ("x" * 200, "0.2.2\u202ereversed", 12, None):
            with self.subTest(value=bad):
                with self.assertRaises(GuildHubProtocolError):
                    parse_device_authentication(
                        dict(self.BASE, client_version=bad)
                    )


class ViewerEnrollmentMessageTests(unittest.TestCase):
    """The Discord-issued pairing invitation, on the wire.

    It carries a code the adapter already showed someone and the viewer
    token of the person who asked, so the device that claims it inherits
    their identity and voice presence alone can gate captions afterwards.
    """

    def message(self, **overrides):
        base = {
            "type": "viewer_enrollment",
            "code": "ghe_" + "A" * 32,
            "viewer_token": "a1b2c3d4e5f6a7b8c9d0e1f2",
            "ttl_seconds": 1800,
        }
        base.update(overrides)
        return base

    def test_a_well_formed_invitation_parses(self):
        parsed = parse_viewer_enrollment(self.message())
        self.assertEqual(parsed.code, "ghe_" + "A" * 32)
        self.assertEqual(parsed.viewer_token, "a1b2c3d4e5f6a7b8c9d0e1f2")
        self.assertEqual(parsed.ttl_seconds, 1800)
        self.assertIs(type(parse_publisher_command(self.message())), ViewerEnrollment)

    def test_a_lifetime_outside_the_bounds_is_refused(self):
        for ttl in (0, -1, 7 * 24 * 60 * 60 + 1, True, 1.5, "1800", None):
            with self.assertRaises(GuildHubProtocolError):
                parse_viewer_enrollment(self.message(ttl_seconds=ttl))
        # The edges themselves are fine.
        self.assertEqual(parse_viewer_enrollment(self.message(ttl_seconds=1)).ttl_seconds, 1)
        self.assertEqual(
            parse_viewer_enrollment(self.message(ttl_seconds=7 * 24 * 60 * 60)).ttl_seconds,
            7 * 24 * 60 * 60,
        )

    def test_a_code_or_token_that_is_not_ours_is_refused(self):
        for code in ("", "not-ours", "ghe_short", "ghe_" + "A" * 128, "ghe_" + "!" * 32):
            with self.assertRaises(GuildHubProtocolError):
                parse_viewer_enrollment(self.message(code=code))
        for token in ("", "nope", "A" * 24, "a" * 23, "a" * 25):
            with self.assertRaises(GuildHubProtocolError):
                parse_viewer_enrollment(self.message(viewer_token=token))

    def test_extra_or_missing_fields_are_refused(self):
        with self.assertRaises(GuildHubProtocolError):
            parse_viewer_enrollment(self.message(guild_id="222222222222222222"))
        short = self.message()
        del short["viewer_token"]
        with self.assertRaises(GuildHubProtocolError):
            parse_viewer_enrollment(short)
        with self.assertRaises(GuildHubProtocolError):
            parse_viewer_enrollment(self.message(type="viewer_link"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
