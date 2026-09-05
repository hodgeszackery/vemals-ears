# SPDX-License-Identifier: GPL-3.0-only
"""The uninstaller's quit request, acknowledgement and event-loop boundary."""

import os
import queue
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.support.companion import isolated_companion


class QuitSignalTests(unittest.TestCase):
    def setUp(self):
        self.companion = self.enterContext(isolated_companion())
        self.now = 1_700_000_000.0
        self.enterContext(mock.patch.object(self.companion.time, "time", return_value=self.now))

    def request(self, *, age=0, contents="quit"):
        signal = self.companion.QUIT_SIGNAL
        signal.parent.mkdir(parents=True, exist_ok=True)
        signal.write_text(contents, encoding="ascii")
        timestamp = self.now - age
        os.utime(signal, (timestamp, timestamp))
        return signal

    def window(self):
        # Exercise real tick/consume methods without starting Tk, services,
        # credentials or a tray thread. The queue is real so a lost shutdown
        # return would visibly consume and dispatch its pending command.
        window = object.__new__(self.companion.CompanionWindow)
        window.root = mock.Mock()
        window.services = []
        window.update_tick = 1
        window.drain_quick_chat_results = mock.Mock()
        window.refresh_update_banner = mock.Mock()
        window.consume_pairing_signal = mock.Mock(return_value=False)
        window.drain_quick_chat_profile_results = mock.Mock()
        window.tray = SimpleNamespace(commands=queue.Queue())
        window.quit_now = mock.Mock()
        window.show = mock.Mock()
        window.open_speak_box = mock.Mock()
        return window

    def test_fresh_request_is_acknowledged_before_cleanup(self):
        signal = self.request()
        window = self.window()
        self.assertTrue(window.consume_quit_request())
        window.quit_now.assert_called_once_with()
        # Check while the fixture is live. TemporaryDirectory cleanup must
        # not be mistaken for the acknowledgement written by the app.
        self.assertFalse(signal.exists(), "the app must acknowledge by removing the request")
        self.assertFalse(window.consume_quit_request())
        window.quit_now.assert_called_once_with()

    def test_stale_request_is_acknowledged_without_quitting(self):
        signal = self.request(age=3600)
        window = self.window()
        self.assertFalse(window.consume_quit_request())
        window.quit_now.assert_not_called()
        self.assertFalse(signal.exists(), "old requests must also be cleared by the app")

    def test_missing_request_does_not_quit(self):
        window = self.window()
        self.assertFalse(window.consume_quit_request())
        window.quit_now.assert_not_called()
        self.assertFalse(self.companion.QUIT_SIGNAL.exists())

    def test_mtime_controls_freshness_on_both_sides_of_the_clock(self):
        for age, should_quit in ((9.9, True), (10.0, False), (-9.9, True), (-10.0, False)):
            with self.subTest(age=age):
                signal = self.request(age=age)
                window = self.window()
                self.assertEqual(window.consume_quit_request(), should_quit)
                self.assertEqual(window.quit_now.call_count, int(should_quit))
                self.assertFalse(signal.exists())

    def test_request_contents_are_not_a_python_only_protocol(self):
        for contents in ("", "quit\r\n", "uninstaller request"):
            with self.subTest(contents=contents):
                signal = self.request(contents=contents)
                window = self.window()
                self.assertTrue(window.consume_quit_request())
                window.quit_now.assert_called_once_with()
                self.assertFalse(signal.exists())

    def test_unreadable_timestamp_clears_request_without_quitting(self):
        signal = self.request()
        boundary = mock.Mock(wraps=signal)
        boundary.stat.side_effect = OSError("timestamp unavailable")
        window = self.window()
        with mock.patch.object(self.companion, "QUIT_SIGNAL", boundary):
            self.assertFalse(window.consume_quit_request())
        window.quit_now.assert_not_called()
        self.assertFalse(signal.exists())

    def test_acknowledgement_failure_does_not_prevent_a_fresh_quit(self):
        signal = self.request()
        boundary = mock.Mock(wraps=signal)
        boundary.unlink.side_effect = PermissionError("request is locked")
        window = self.window()
        with mock.patch.object(self.companion, "QUIT_SIGNAL", boundary):
            self.assertTrue(window.consume_quit_request())
        window.quit_now.assert_called_once_with()
        self.assertTrue(signal.exists(), "the locked request remains for installer fallback")

    def test_tick_stops_after_a_fresh_quit_request(self):
        for edition in ("full-local", "member", "downtime-hosted"):
            with self.subTest(edition=edition), isolated_companion(edition):
                signal = self.request()
                window = self.window()
                window.tray.commands.put("show")
                window.tick()
                window.quit_now.assert_called_once_with()
                self.assertFalse(signal.exists())
                window.show.assert_not_called()
                window.open_speak_box.assert_not_called()
                self.assertEqual(window.tray.commands.get_nowait(), "show")
                window.root.after.assert_not_called()

    def test_tick_continues_after_a_stale_or_missing_request(self):
        for pending in (False, True):
            with self.subTest(pending=pending):
                signal = self.request(age=3600) if pending else self.companion.QUIT_SIGNAL
                window = self.window()
                window.tray.commands.put("show")
                window.tick()
                window.quit_now.assert_not_called()
                window.show.assert_called_once_with()
                self.assertTrue(window.tray.commands.empty())
                self.assertFalse(signal.exists())
                window.root.after.assert_called_once_with(120, window.tick)


if __name__ == "__main__":
    unittest.main()
