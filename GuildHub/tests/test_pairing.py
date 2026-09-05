"""The shared enrollment boundary is usable without a caption application."""

import json
import subprocess
import sys
import unittest
from pathlib import Path
from urllib.parse import urlencode

from GuildHub import pairing
from GuildHub.credentials import WindowsDeviceCredentialStore, WindowsDowntimeCredentialStore
from GuildHub.protocol import SUBPROTOCOL
from GuildHub.tests.test_credentials import MultiCredentialApi, credentials


def invitation(scheme, **overrides):
    fields = {"server": "wss://guild.example", "code": "one_use_example"}
    fields.update(overrides)
    return scheme + "://pair?" + urlencode(fields)


class PairingSchemeTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_product_schemes_resolve_locally(self):
        def no_network(origin, timeout):
            self.fail("App pairing links must resolve without fetching public config")

        for scheme in ("downtime", "vemalsears", "DOWNTIME"):
            with self.subTest(scheme=scheme):
                link = await pairing.resolve_pair_link(invitation(scheme), config_fetch=no_network)
                self.assertEqual("wss://guild.example", link.server_url)
                self.assertEqual("one_use_example", link.enrollment_code)
                self.assertNotIn("one_use_example", repr(link))

    def test_downtime_link_keeps_strict_security_validation(self):
        rejected = (
            invitation("downtime", server="ws://remote.example"),
            invitation("downtime") + "&code=second_code",
            invitation("downtime") + "#one_use_example",
            invitation("downtime") + "&unknown=secret",
            invitation("downtime", code="unsafe code"),
            invitation("https"),
            invitation("downtime").replace("://pair?", "://user:secret@pair?"),
            invitation("downtime").replace("://pair?", "://other?"),
        )
        for link in rejected:
            with self.subTest(link=link):
                with self.assertRaises(pairing.GuildHubClientError):
                    pairing.parse_pair_link(link)

    async def test_downtime_enrollment_saves_only_downtime_and_uses_existing_protocol(self):
        api = MultiCredentialApi()
        ears = WindowsDeviceCredentialStore(api)
        ears.save(credentials("ghd_existing_ve"))
        downtime = WindowsDowntimeCredentialStore(api)
        sent = []
        connections = []

        class Socket:
            subprotocol = SUBPROTOCOL

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def send(self, raw):
                sent.append(json.loads(raw))

            async def recv(self):
                return json.dumps({
                    "type": "paired", "device_id": "downtime_device",
                    "device_token": "ghd_new_downtime", "guild_id": "guild_1",
                    "guild_name": "Example Guild",
                })

        def connector(url, **kwargs):
            connections.append((url, kwargs))
            return Socket()

        result = await pairing.pair_device(
            invitation("downtime"), downtime, connector=connector,
        )
        self.assertEqual(result, downtime.load())
        self.assertEqual("ghd_existing_ve", ears.load().device_token)
        self.assertEqual("wss://guild.example/v1/pair", connections[0][0])
        self.assertEqual([SUBPROTOCOL], connections[0][1]["subprotocols"])
        self.assertEqual("one_use_example", sent[0]["enrollment_code"])

    def test_shared_pairing_import_never_loads_caption_client_or_overlay(self):
        root = Path(__file__).resolve().parents[2]
        check = (
            "import sys; from GuildHub import pairing; "
            "assert 'GuildHub.client' not in sys.modules; "
            "assert 'VoiceCaptions.member_overlay' not in sys.modules; "
            "assert 'VoiceCaptions.settings' not in sys.modules"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", check], cwd=root,
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
