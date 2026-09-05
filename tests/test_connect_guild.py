"""The all-in-one Connect guild helper.

Shino, 2026-09-04: "bring out the Connect guild button and make it an all
in one connect guild helper. Let's make sure UI elements are from user
perspective. Guild and Privacy is a strong enough label without the
extra". The button lives under the guild name in the header; the panel
it opens walks a member from "where do I get an invitation" to "what do
I do now that I am connected", and the Guild & Privacy card is the
label, the status line, the buttons and the privacy link - no paragraph.
"""

from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from tests.support.companion import load_companion


def pump(root, seconds=0.6, until=None):
    """Run the Tk loop for a while, or until a condition holds."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        root.update()
        if until is not None and until():
            return True
        time.sleep(0.02)
    return until() if until is not None else True


class InvitationShapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    def test_the_two_real_shapes_are_recognised(self):
        for text in (
            "vemalsears://pair?server=wss%3A%2F%2Fears.example.org&code=ABCDEFGHJK",
            "downtime://pair?server=wss%3A%2F%2Fears.example.org&code=ABCDEFGHJK",
            "  https://ears.example.org/join#ABCDEFGHJK  ",
        ):
            with self.subTest(text=text):
                self.assertTrue(self.companion.looks_like_invitation(text), text)

    def test_stray_clipboard_contents_are_not(self):
        for text in (
            "", None, 42, "hello", "https://example.org/", "https://ears.example.org/join",
            "ftp://ears.example.org/join#ABC", "vemalsears://pair?x=1\nrm -rf",
            "x" * 3000,
        ):
            with self.subTest(text=str(text)[:30]):
                self.assertFalse(self.companion.looks_like_invitation(text))


class HeaderButtonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    def window(self, edition):
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        window = self.companion.CompanionWindow(root, edition=edition, preview=True)
        root.update_idletasks()
        return root, window

    def test_managed_editions_carry_connect_guild_in_the_header(self):
        for edition in (self.companion.MEMBER_EDITION, self.companion.HOSTED_EDITION):
            with self.subTest(edition=edition):
                root, window = self.window(edition)
                try:
                    button = window.hero_connect_button
                    self.assertIsNotNone(button)
                    # A preview window is connected to a stand-in guild,
                    # so the button already offers the switch.
                    self.assertEqual("Change guild", button.cget("text"))
                    self.assertTrue(button.is_accent)
                    # Right under the guild name, in the same header block.
                    self.assertIs(button.master, window.hero_guild_label.master)
                    # And gone from the card, where it used to be the
                    # emphasised control.
                    card_texts = [
                        b.cget("text") for b in window.managed_cards["guild"]["buttons"]
                    ]
                    self.assertNotIn("Connect guild", card_texts)
                finally:
                    root.destroy()

    def test_the_full_edition_keeps_it_on_the_captions_page_card(self):
        # No guild block in the full edition's header, so the card is
        # still the way in there - and it opens the same panel.
        root, window = self.window(self.companion.FULL_EDITION)
        try:
            self.assertIsNone(window.hero_connect_button)
            card_texts = [
                b.cget("text") for b in window.managed_cards["guild"]["buttons"]
            ]
            self.assertIn("Connect guild", card_texts)
            button = next(
                b for b in window.managed_cards["guild"]["buttons"]
                if b.cget("text") == "Connect guild"
            )
            self.assertIsNone(window.connect_guild_helper)
            button.invoke()
            self.assertIsInstance(window.connect_guild_helper, self.companion.ConnectGuildHelper)
            window.connect_guild_helper.dismiss()
        finally:
            root.destroy()

    def test_the_label_follows_the_connection(self):
        root, window = self.window(self.companion.MEMBER_EDITION)
        try:
            # Preview windows are "connected" to a stand-in guild.
            self.assertEqual("Moonwell Raiders", window.hero_guild_label.cget("text"))
            self.assertEqual("Change guild", window.hero_connect_button.cget("text"))
            window.preview = False
            with mock.patch.object(self.companion, "paired_guild_name", return_value=""):
                window.refresh_pairing_identity()
            self.assertEqual("Not connected", window.hero_guild_label.cget("text"))
            self.assertEqual("Connect guild", window.hero_connect_button.cget("text"))
            with mock.patch.object(self.companion, "paired_guild_name", return_value="Nightwatch"):
                window.refresh_pairing_identity()
            self.assertEqual("Change guild", window.hero_connect_button.cget("text"))
        finally:
            root.destroy()

    def test_the_guild_card_is_the_label_and_nothing_extra(self):
        import tkinter as tk

        root, window = self.window(self.companion.MEMBER_EDITION)
        try:
            content = window.managed_cards["guild"]["content"]
            texts = []

            def walk(widget):
                for child in widget.winfo_children():
                    if isinstance(child, tk.Label):
                        texts.append(child.cget("text"))
                    walk(child)

            walk(content)
            self.assertIn("Guild & Privacy", texts)
            self.assertIn("What your guild keeps - and what it never keeps", texts)
            # No description paragraph, and no blank label standing where
            # one used to be: title, status, link - three labels.
            self.assertNotIn("", texts)
            self.assertEqual(3, len(texts), texts)
            for stale in (
                "Connect this computer",
                "Set your class",
                "install steps",
                "what is, and is not, kept",
            ):
                self.assertFalse(
                    any(stale in text for text in texts), stale + " is still on the card",
                )
            # The header block reads from the member's side too.
            hero_texts = [
                child.cget("text")
                for child in window.hero_guild_label.master.winfo_children()
                if isinstance(child, tk.Label)
            ]
            self.assertIn("Your guild", hero_texts)
            self.assertNotIn("Guild connection", hero_texts)
        finally:
            root.destroy()

    def test_the_header_button_opens_one_panel_and_raises_it_after(self):
        root, window = self.window(self.companion.MEMBER_EDITION)
        try:
            window.hero_connect_button.invoke()
            first = window.connect_guild_helper
            self.assertIsNotNone(first)
            self.assertTrue(first.winfo_exists())
            window.hero_connect_button.invoke()
            self.assertIs(first, window.connect_guild_helper)
            first.dismiss()
            self.assertIsNone(window.connect_guild_helper)
        finally:
            root.destroy()

    def test_an_open_panel_follows_a_pairing_made_elsewhere(self):
        # The vemalsears:// link pairs without this panel. The header
        # learns from the pairing signal; the panel must not keep
        # offering Connect for an invitation that would be the second.
        root, window = self.window(self.companion.MEMBER_EDITION)
        try:
            window.hero_connect_button.invoke()
            helper = window.connect_guild_helper
            helper.connected_name = ""
            helper.refresh_state()
            self.assertEqual("Connect to your guild", helper.title_label.cget("text"))
            helper.connected_name = "Nightwatch"
            window.refresh_pairing_identity()
            self.assertEqual("Your guild", helper.title_label.cget("text"))
            self.assertEqual("Connected to Nightwatch.", helper.status_label.cget("text"))
            helper.connected_name = ""
            window.hero_connect_button.invoke()
            self.assertEqual("Connect to your guild", helper.title_label.cget("text"))
            helper.dismiss()
        finally:
            root.destroy()


class HelperFlowTests(unittest.TestCase):
    """The panel, driven with a stand-in exchange: no network, no store."""

    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    INVITATION = "https://ears.example.org/join#ABCDEFGHJK"

    def open(self, *, pair=None, clipboard=None, connected=""):
        import tempfile
        import tkinter as tk
        from pathlib import Path

        # The success path writes the pairing signal the main window
        # watches. Redirected, or a unit test would poke the real app.
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.signal = Path(folder) / "paired.signal"
        self.enterContext(mock.patch.object(self.companion, "STATE_DIR", Path(folder)))
        self.enterContext(mock.patch.object(self.companion, "PAIR_SIGNAL", self.signal))
        root = tk.Tk()
        root.withdraw()
        window = self.companion.CompanionWindow(
            root, edition=self.companion.MEMBER_EDITION, preview=True,
        )
        window.refresh_pairing_identity = mock.Mock()
        window.reset_quick_chat_profile_for_pairing = mock.Mock()
        helper = self.companion.ConnectGuildHelper(
            window, pair=pair, clipboard=clipboard or (lambda: ""),
        )
        window.connect_guild_helper = helper
        helper.connected_name = connected
        helper.refresh_state()
        root.update_idletasks()
        return root, window, helper

    def exchange(self, *, guild="Moonwell Raiders", replacing=False, fail=None):
        """A stand-in for pair_invitation that asks the confirm question."""
        seen = {}

        def pair(invitation, confirm):
            seen["invitation"] = invitation
            if fail is not None:
                raise self.companion.PairingFailed(fail)
            credentials = SimpleNamespace(
                guild_name=guild, server_url="wss://ears.example.org",
            )
            existing = SimpleNamespace(guild_name="Old") if replacing else None
            seen["confirmed"] = confirm(credentials, existing)
            if not seen["confirmed"]:
                raise self.companion.PairingFailed(
                    "Vemal's Ears pairing was cancelled. Ask for a new one-use link to try again."
                )
            return guild, "ears.example.org"

        return pair, seen

    def test_step_one_names_the_command_that_really_hands_out_invitations(self):
        # The bot answers /vem setup privately; it never posts a link for
        # a member to find. A step that sent people looking for one would
        # have them scrolling Discord for something that is not there.
        root, _window, helper = self.open()
        try:
            for connected in ("", "Old Guild"):
                helper.connected_name = connected
                helper.refresh_state()
                copy = helper.invitation_copy.cget("text")
                self.assertIn("/vem setup", copy)
                self.assertIn("works once", copy)
                self.assertNotIn("Connect link", copy)
                self.assertNotIn("posts", copy)
        finally:
            root.destroy()

    def test_not_connected_reads_as_a_first_step(self):
        root, _window, helper = self.open()
        try:
            self.assertEqual("Connect to your guild", helper.title_label.cget("text"))
            self.assertIn("Not connected yet", helper.status_label.cget("text"))
            self.assertEqual("", helper.next_frame.winfo_manager())
            self.assertEqual("", helper.confirm_frame.winfo_manager())
            # Only member words: no pairing, devices, credentials or codes.
            import tkinter as tk

            texts = []

            def walk(widget):
                for child in widget.winfo_children():
                    if isinstance(child, (tk.Label, tk.Button)):
                        texts.append(str(child.cget("text")).lower())
                    walk(child)

            walk(helper)
            joined = " ".join(texts)
            for jargon in ("pair", "credential", "enrollment", "device", "token"):
                self.assertNotIn(jargon, joined, jargon)
        finally:
            root.destroy()

    def test_connected_reads_as_your_guild_with_next_steps(self):
        root, _window, helper = self.open(connected="Moonwell Raiders")
        try:
            self.assertEqual("Your guild", helper.title_label.cget("text"))
            self.assertEqual("Connected to Moonwell Raiders.", helper.status_label.cget("text"))
            self.assertEqual("pack", helper.next_frame.winfo_manager())
            self.assertIn("switch to another guild", helper.invitation_copy.cget("text"))
        finally:
            root.destroy()

    def test_an_invitation_on_the_clipboard_is_offered_at_once(self):
        root, _window, helper = self.open(clipboard=lambda: "  " + self.INVITATION + "\n")
        try:
            self.assertEqual(self.INVITATION, helper.invitation.get())
            self.assertIn("found an invitation on your clipboard", helper.message.cget("text"))
        finally:
            root.destroy()

    def test_paste_takes_only_an_invitation(self):
        clip = {"text": "meeting notes"}
        root, _window, helper = self.open(clipboard=lambda: clip["text"])
        try:
            self.assertEqual("", helper.invitation.get())
            helper.paste_from_clipboard()
            self.assertEqual("", helper.invitation.get())
            self.assertIn("Copy your invitation link first", helper.message.cget("text"))
            clip["text"] = self.INVITATION
            helper.paste_from_clipboard()
            self.assertEqual(self.INVITATION, helper.invitation.get())
        finally:
            root.destroy()

    def test_connect_with_nothing_or_nonsense_says_so_without_a_request(self):
        pair, seen = self.exchange()
        root, _window, helper = self.open(pair=pair)
        try:
            helper.connect()
            self.assertIn("Paste your invitation first", helper.message.cget("text"))
            helper.invitation.set_text("not a link")
            helper.connect()
            self.assertIn("does not look like", helper.message.cget("text"))
            self.assertEqual("", helper.invitation.get(), "the field is cleared either way")
            self.assertNotIn("invitation", seen)
            self.assertFalse(helper.busy)
        finally:
            root.destroy()

    def test_the_whole_flow_confirm_then_connected(self):
        pair, seen = self.exchange(guild="Moonwell Raiders", replacing=True)
        root, window, helper = self.open(pair=pair)
        try:
            helper.invitation.set_text(self.INVITATION)
            helper.connect()
            self.assertEqual("", helper.invitation.get(), "the invitation leaves the screen at once")
            self.assertTrue(helper.busy)
            self.assertEqual("disabled", str(helper.connect_button.cget("state")))
            # The question arrives on the Tk thread, in the panel.
            self.assertTrue(pump(root, 5, until=lambda: helper.confirm_frame.winfo_manager() == "pack"))
            asked = helper.confirm_text.cget("text")
            self.assertIn("Connect this computer to Moonwell Raiders?", asked)
            self.assertIn("ears.example.org", asked)
            self.assertIn("replaces the guild you are connected to now", asked)
            helper.confirm_yes.invoke()
            self.assertTrue(pump(root, 5, until=lambda: not helper.busy))
            self.assertEqual(self.INVITATION, seen["invitation"])
            self.assertTrue(seen["confirmed"])
            self.assertEqual("", helper.confirm_frame.winfo_manager())
            self.assertEqual("Connected to Moonwell Raiders.", helper.message.cget("text"))
            self.assertEqual("Your guild", helper.title_label.cget("text"))
            self.assertEqual("pack", helper.next_frame.winfo_manager())
            self.assertEqual("normal", str(helper.connect_button.cget("state")))
            window.refresh_pairing_identity.assert_called_once_with()
            window.reset_quick_chat_profile_for_pairing.assert_called_once_with()
            # The main window's own signal was written, so a companion
            # that lost this panel still learns about the pairing.
            self.assertTrue(self.signal.is_file())
        finally:
            root.destroy()

    def test_saying_no_is_a_choice_not_an_error(self):
        pair, seen = self.exchange()
        root, window, helper = self.open(pair=pair)
        try:
            helper.invitation.set_text(self.INVITATION)
            helper.connect()
            self.assertTrue(pump(root, 5, until=lambda: helper.confirm_frame.winfo_manager() == "pack"))
            helper.confirm_no.invoke()
            self.assertTrue(pump(root, 5, until=lambda: not helper.busy))
            self.assertFalse(seen["confirmed"])
            said = helper.message.cget("text")
            # The library's "pairing was cancelled ... one-use link" never
            # shows; the member is told the one thing that matters.
            self.assertEqual(self.companion.INVITATION_DECLINED, said)
            self.assertNotIn("pairing", said.lower())
            self.assertEqual(self.companion.WARNING, helper.message.cget("fg"))
            self.assertEqual("Connect to your guild", helper.title_label.cget("text"))
            window.refresh_pairing_identity.assert_not_called()
            self.assertFalse(self.signal.exists())
        finally:
            root.destroy()

    def test_return_belongs_to_the_field_not_to_every_button(self):
        # A keyboard user tabbing to Paste or Close and pressing Return
        # must get Paste or Close - not a connection attempt with
        # whatever is in the field. This is an accessibility app.
        pair, seen = self.exchange()
        # Connected, so the next-steps block (and its link) is on screen;
        # switching guilds is the flow where every control is reachable.
        root, window, helper = self.open(
            pair=pair, clipboard=lambda: self.INVITATION, connected="Old Guild",
        )
        try:
            self.assertEqual(self.INVITATION, helper.invitation.get(), "prefilled from the clipboard")
            self.assertEqual("pack", helper.next_frame.winfo_manager())
            window.open_guild_privacy = mock.Mock()
            for widget in (helper.paste_button, helper.close_button, helper.consent_button):
                widget.focus_force()
                widget.event_generate("<Return>")
                root.update()
            self.assertEqual(self.INVITATION, helper.invitation.get(), "Return on a button must not spend the field")
            self.assertNotIn("invitation", seen)
            self.assertTrue(helper.winfo_exists())
            helper.privacy_link.focus_force()
            root.update()
            helper.privacy_link.event_generate("<Return>")
            root.update()
            window.open_guild_privacy.assert_called_once_with()
            self.assertNotIn("Paste your invitation first", helper.message.cget("text"))
            self.assertEqual(self.INVITATION, helper.invitation.get(), "the link must not spend the field either")
            # Return in the field itself is Connect.
            helper.invitation.entry.focus_force()
            helper.invitation.entry.event_generate("<Return>")
            self.assertTrue(pump(root, 5, until=lambda: helper.confirm_frame.winfo_manager() == "pack"))
            self.assertEqual(self.INVITATION, seen["invitation"])
            helper.confirm_yes.invoke()
            pump(root, 5, until=lambda: not helper.busy)
        finally:
            root.destroy()

    def test_closing_while_connecting_waits_for_the_answer(self):
        # The guild's server may already have accepted the one-use
        # invitation; a panel that vanished now could never say what
        # happened to it. Close waits, and says so.
        pair, seen = self.exchange()
        root, window, helper = self.open(pair=pair)
        try:
            helper.invitation.set_text(self.INVITATION)
            helper.connect()
            self.assertTrue(pump(root, 5, until=lambda: helper.confirm_frame.winfo_manager() == "pack"))
            helper.dismiss()
            self.assertTrue(helper.winfo_exists())
            self.assertIs(window.connect_guild_helper, helper)
            self.assertIn("Still checking", helper.message.cget("text"))
            helper.confirm_yes.invoke()
            self.assertTrue(pump(root, 5, until=lambda: not helper.busy))
            self.assertTrue(seen["confirmed"])
            helper.dismiss()
            self.assertFalse(helper.winfo_exists())
            self.assertIsNone(window.connect_guild_helper)
            self.assertIsNone(helper.poll_id)
        finally:
            root.destroy()

    def test_a_panel_torn_down_mid_exchange_does_not_hold_the_worker(self):
        # The window itself can go (the app closes) while the exchange is
        # on the wire. The worker's question then has nobody to ask, and
        # must not sit out the five-minute timeout waiting for one.
        gate = threading.Event()
        seen = {}

        def pair(invitation, confirm):
            gate.wait(5)
            started = time.monotonic()
            seen["confirmed"] = confirm(
                SimpleNamespace(guild_name="G", server_url="wss://h"), None,
            )
            seen["waited"] = time.monotonic() - started
            raise self.companion.PairingFailed("x")

        root, _window, helper = self.open(pair=pair)
        try:
            helper.CONFIRM_TIMEOUT_SECONDS = 3.0
            helper.invitation.set_text(self.INVITATION)
            helper.connect()
            helper.destroy()
            root.update()
            gate.set()
            deadline = time.monotonic() + 5
            while "waited" not in seen and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertIn("waited", seen, "the worker never got its answer")
            self.assertFalse(seen["confirmed"])
            self.assertLess(seen["waited"], 1.0)
        finally:
            root.destroy()

    def test_a_refused_invitation_is_explained_and_the_button_comes_back(self):
        pair, _seen = self.exchange(
            fail="The one-use pairing link was rejected or has already been used.",
        )
        root, _window, helper = self.open(pair=pair)
        try:
            helper.invitation.set_text(self.INVITATION)
            helper.connect()
            self.assertTrue(pump(root, 5, until=lambda: not helper.busy))
            self.assertIn("already been used", helper.message.cget("text"))
            self.assertEqual("normal", str(helper.connect_button.cget("state")))
            self.assertEqual("Connect", helper.connect_button.cget("text"))
        finally:
            root.destroy()

    def test_an_exchange_that_blows_up_never_shows_its_own_words(self):
        def pair(_invitation, _confirm):
            raise RuntimeError("Traceback: enrollment_code=SECRET at ws://x")

        root, _window, helper = self.open(pair=pair)
        try:
            helper.invitation.set_text(self.INVITATION)
            helper.connect()
            self.assertTrue(pump(root, 5, until=lambda: not helper.busy))
            said = helper.message.cget("text")
            self.assertNotIn("SECRET", said)
            self.assertIn("Ask your guild for a new invitation", said)
        finally:
            root.destroy()

    def test_a_second_connect_while_busy_is_ignored(self):
        gate = threading.Event()
        calls = []

        def pair(invitation, _confirm):
            calls.append(invitation)
            gate.wait(5)
            return "Guild", "host"

        root, _window, helper = self.open(pair=pair)
        try:
            helper.invitation.set_text(self.INVITATION)
            helper.connect()
            helper.invitation.set_text(self.INVITATION)
            helper.connect()
            gate.set()
            self.assertTrue(pump(root, 5, until=lambda: not helper.busy))
            self.assertEqual(1, len(calls))
        finally:
            root.destroy()

    def test_the_poll_timer_dies_with_the_panel(self):
        root, _window, helper = self.open()
        try:
            self.assertIsNotNone(helper.poll_id)
            helper.dismiss()
            self.assertTrue(helper.closed)
            self.assertIsNone(helper.poll_id)
            # Nothing left to fire: a pending timer on a destroyed panel
            # is a Tcl background error on every close.
            pump(root, 0.3)
        finally:
            root.destroy()

    def test_preview_never_reaches_a_server(self):
        root, _window, helper = self.open()
        try:
            with mock.patch.object(self.companion, "pair_invitation") as real:
                helper.pair = self.companion.pair_invitation
                helper.invitation.set_text(self.INVITATION)
                helper.connect()
                real.assert_not_called()
            self.assertIn("Preview only", helper.message.cget("text"))
            self.assertFalse(helper.busy)
        finally:
            root.destroy()


class MemberWordsTests(unittest.TestCase):
    """The pairing code's sentences say pairing, device, credential and
    token. The member sees none of those."""

    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    def test_each_library_situation_has_its_own_member_sentence(self):
        words = self.companion.member_words_for
        cases = (
            ("The one-use pairing link was rejected or has already been used.",
             "already been used"),
            ("Vemal's Ears could not securely reach the guild server.", "internet connection"),
            ("Could not securely reach the guild server.", "internet connection"),
            ("Could not reach the guild server behind that invitation: HTTP 403",
             "internet connection"),
            ("That link does not point at a Vemal's Ears server.", "does not lead to"),
            ("The guild server returned an invalid pairing response.", "answered in a way"),
            ("The guild server did not select the Vemal's Ears protocol.", "does not lead to"),
            ("Vemal's Ears needs the websockets package, which is not installed. "
             "Reinstall with the Vemal's Ears installer, or from a source checkout "
             "run: python -m pip install -r GuildHub/requirements-client.txt",
             "missing a part"),
            ("The pairing link has an invalid one-use code.", "does not look like"),
            ("The invitation link has an invalid action.", "does not look like"),
            ("Vemal's Ears pairing was cancelled. Ask for a new one-use link to try again.",
             "invitation was used"),
            ("something entirely new", "Ask your guild for a new invitation"),
        )
        for library, expected in cases:
            with self.subTest(library=library[:40]):
                said = words(RuntimeError(library))
                self.assertIn(expected, said, said)
                for jargon in ("pairing", "device", "credential", "token", "pip ", "enrollment"):
                    self.assertNotIn(jargon, said.lower(), said)

    def test_the_store_and_validation_kinds_have_their_own(self):
        words = self.companion.member_words_for
        self.assertIn("Windows could not save", words(RuntimeError("DPAPI"), kind="store"))
        self.assertIn("check its server setup", words(RuntimeError("bad"), kind="validation"))


class PairInvitationTests(unittest.TestCase):
    """The default exchange keeps the pairing code's promises."""

    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    def fakes(self, pair_device):
        import sys

        fake_client = SimpleNamespace(
            GuildHubClientError=type("GuildHubClientError", (RuntimeError,), {}),
            pair_device=pair_device,
        )
        fake_credentials = SimpleNamespace(
            DeviceCredentialStoreError=type("DeviceCredentialStoreError", (RuntimeError,), {}),
            DeviceCredentialValidationError=type("DeviceCredentialValidationError", (RuntimeError,), {}),
            WindowsDeviceCredentialStore=lambda: object(),
            WindowsDowntimeCredentialStore=lambda: object(),
        )
        return fake_client, fake_credentials, mock.patch.dict(sys.modules, {
            "GuildHub.pairing": fake_client, "GuildHub.credentials": fake_credentials,
        })

    def test_errors_are_the_members_sentence_never_a_traceback(self):
        holder = {}

        async def pair_device(invitation, store, *, confirm):
            raise holder["error"]

        fake_client, fake_credentials, patched = self.fakes(pair_device)
        with patched:
            for error, expected in (
                (fake_client.GuildHubClientError(
                    "The one-use pairing link was rejected or has already been used."),
                 "already been used"),
                (fake_client.GuildHubClientError("The invitation link is invalid."),
                 "does not look like"),
                (fake_credentials.DeviceCredentialStoreError("DPAPI refused"),
                 "Windows could not save"),
                (fake_credentials.DeviceCredentialValidationError("guild_id"),
                 "check its server setup"),
                (ValueError("code=ABC leaked"), "Ask your guild for a new invitation"),
            ):
                with self.subTest(error=str(error)):
                    holder["error"] = error
                    with self.assertRaises(self.companion.PairingFailed) as caught:
                        self.companion.pair_invitation("https://x/join#ABC", lambda *_: True)
                    said = str(caught.exception)
                    self.assertIn(expected, said, said)
                    self.assertNotIn("ABC", said)
                    self.assertNotIn("DPAPI", said)

    def test_without_the_network_component_the_member_is_told_to_reinstall(self):
        import sys

        with mock.patch.dict(sys.modules, {"GuildHub.client": None, "GuildHub.credentials": None}):
            with self.assertRaises(self.companion.PairingFailed) as caught:
                self.companion.pair_invitation("https://x/join#ABC", lambda *_: True)
        self.assertIn("Reinstall", str(caught.exception))

    def test_success_returns_the_guild_and_its_host(self):
        async def pair_device(invitation, store, *, confirm):
            credentials = SimpleNamespace(
                guild_name="Moonwell Raiders", server_url="wss://ears.example.org/ws",
            )
            self.assertTrue(confirm(credentials, None))
            return credentials

        _client, _credentials, patched = self.fakes(pair_device)
        with patched:
            self.assertEqual(
                ("Moonwell Raiders", "ears.example.org"),
                self.companion.pair_invitation("https://x/join#ABC", lambda *_: True),
            )


if __name__ == "__main__":
    unittest.main()
