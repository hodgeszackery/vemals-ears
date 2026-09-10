import json
import time
import sys
import tempfile
import os
import shutil
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
# The companion imports GuildHub, which is only importable from the
# repository root. Discover happens to run from there; invoking this file
# directly does not, and five tests errored out looking like real
# failures. A test that lies when you run it by hand is worse than one
# that is slow.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.support.companion import isolated_companion, load_companion


class CompanionIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    def test_vemals_ears_is_a_first_class_service(self):
        by_key = {service.key: service for service in self.companion.SERVICES}
        self.assertIn("voice", by_key)
        voice = by_key["voice"]
        self.assertEqual("Vemal's Ears", voice.label)
        self.assertEqual("client.py", voice.script.name)
        self.assertEqual("setup.py", voice.action_script.name)
        self.assertEqual("GuildHub", voice.action_script.parent.name)
        self.assertEqual("Pair", voice.action_label)
        self.assertEqual("placement.py", voice.second_action_script.name)
        self.assertEqual("Move", voice.second_action_label)
        self.assertEqual("setup.py", voice.third_action_script.name)
        self.assertEqual("VoiceCaptions", voice.third_action_script.parent.name)
        self.assertEqual("Voice setup", voice.third_action_label)

    def test_member_distribution_marker_selects_the_member_shell(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            marker = root / "vemals-ears-member.json"
            marker.write_text(
                '{"edition":"member","schema":1}\n',
                encoding="ascii",
            )
            self.assertTrue(
                self.companion.is_member_edition(root=root, installed=False)
            )
            marker.write_text("damaged\n", encoding="ascii")
            self.assertFalse(
                self.companion.is_member_edition(root=root, installed=False)
            )
            self.assertTrue(
                self.companion.is_member_edition(root=root, installed=True)
            )

    def test_hosted_distribution_marker_selects_the_hosted_shell(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            marker = root / "downtime-hosted.json"
            marker.write_text(
                '{"edition":"downtime-hosted","schema":1}\n',
                encoding="ascii",
            )
            self.assertEqual(
                self.companion.HOSTED_EDITION,
                self.companion.resolve_edition(root=root, installed=False),
            )
            marker.write_text("damaged\n", encoding="ascii")
            self.assertEqual(
                self.companion.HOSTED_EDITION,
                self.companion.resolve_edition(root=root, installed=False),
                "a damaged hosted bundle must not fall back to local watchers",
            )

    def test_member_shell_runs_only_the_thin_caption_client(self):
        member_keys = [
            spec.key for spec in self.companion.service_specs_for_edition(True)
        ]
        suite_keys = [
            spec.key for spec in self.companion.service_specs_for_edition(False)
        ]
        self.assertEqual(["voice"], member_keys)
        self.assertEqual(
            ["gear", "talents"],
            suite_keys,
        )

    def test_discord_worker_controls_are_owned_by_the_shared_server(self):
        for edition in (
            self.companion.FULL_EDITION,
            self.companion.MEMBER_EDITION,
            self.companion.HOSTED_EDITION,
        ):
            with self.subTest(edition=edition):
                keys = {
                    spec.key
                    for spec in self.companion.service_specs_for_edition(edition)
                }
                self.assertNotIn("voice_discord", keys)

    def test_member_caption_client_has_only_its_personal_pause_control(self):
        voice = self.companion.service_specs_for_edition(
            self.companion.MEMBER_EDITION
        )[0]
        self.assertTrue(voice.start_on_launch)
        self.assertFalse(voice.show_lifecycle_controls)
        self.assertFalse(voice.external_control)

    def test_hosted_shell_forwards_only_addon_jobs(self):
        specs = self.companion.service_specs_for_edition(
            self.companion.HOSTED_EDITION
        )
        by_key = {spec.key: spec for spec in specs}
        self.assertEqual({"gear", "talents"}, set(by_key))
        for kind in ("gear", "talents"):
            spec = by_key[kind]
            self.assertEqual("jobs_client.py", spec.script.name)
            self.assertEqual(("--watch", "--kind", kind), spec.arguments)
            self.assertTrue(spec.auto_restart)
            self.assertFalse(spec.show_lifecycle_controls)
            self.assertNotIn("Tools", spec.script.parts)

    def test_member_shell_does_not_carry_the_local_voice_tools_action(self):
        voice = self.companion.service_specs_for_edition(
            self.companion.MEMBER_EDITION
        )[0]
        self.assertEqual("voice", voice.key)
        self.assertIsNone(voice.third_action_script)
        self.assertEqual("", voice.third_action_label)

    def test_voice_setup_action_keeps_its_name_when_the_tool_is_missing(self):
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / "VoiceCaptions" / "setup.py"
            spec = replace(
                next(item for item in self.companion.SERVICES if item.key == "voice"),
                third_action_script=missing,
            )
            service = self.companion.Service(
                spec=spec,
                window=SimpleNamespace(preview=False),
            )
            service.set_status = mock.Mock()

            service.run_action(missing)

            service.set_status.assert_called_once_with(
                "Voice setup tool is not installed",
                self.companion.DANGER,
            )

    def test_hosted_job_process_receives_watch_and_kind_arguments(self):
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "GuildHub" / "jobs_client.py"
            script.parent.mkdir()
            script.write_text("", encoding="ascii")
            spec = replace(
                self.companion.HOSTED_SERVICES[0],
                script=script,
            )
            service = self.companion.Service(spec=spec, window=mock.Mock())
            process = mock.Mock()
            with mock.patch.object(
                self.companion, "child_python", return_value="pythonw.exe"
            ), mock.patch.object(
                self.companion.subprocess, "Popen", return_value=process
            ) as launch, mock.patch.object(
                self.companion, "assign_to_watcher_job"
            ), mock.patch.object(self.companion.threading, "Thread"):
                service.start()

            command = launch.call_args.args[0]
            self.assertEqual(
                [
                    "pythonw.exe", "-E", "-s", "-B", "-u",
                    str(script), "--watch", "--kind", "gear",
                ],
                command,
            )

    def test_hosted_job_progress_format_updates_the_status_row(self):
        line = "[########----------------] 8/20 Simulating candidates"
        self.assertIsNotNone(self.companion.PROGRESS.match(line))

    def test_hosted_job_output_drives_visible_status(self):
        window = mock.Mock()
        service = self.companion.Service(
            spec=self.companion.HOSTED_SERVICES[0],
            window=window,
        )
        process = mock.Mock()
        service.process = process
        service.set_status = mock.Mock()

        cases = (
            (
                "Watching for hosted Gear Helper jobs.\n",
                ("Waiting for a request", self.companion.SUCCESS, "ready"),
            ),
            (
                "Hosted Gear Helper job queued: gh-1.\n",
                ("Queued on the server", self.companion.ACCENT, "queued"),
            ),
            (
                "[########----------------] 8/20 Simulating candidates\n",
                (
                    "[########----------------] 8/20 Simulating candidates",
                    self.companion.WARNING,
                    "checking",
                ),
            ),
            (
                "Hosted Gear Helper result ready to load in WoW. Choose Load Results.\n",
                (
                    "Results ready - choose Load Results",
                    self.companion.SUCCESS,
                    "done",
                ),
            ),
            (
                "Hosted Gear Helper result written. Restart WoW, then choose Load Results.\n",
                (
                    "Restart WoW, then Load Results",
                    self.companion.WARNING,
                    "done",
                ),
            ),
        )
        for line, expected in cases:
            with self.subTest(line=line):
                service.set_status.reset_mock()
                service.lines.put((process, line))
                service.drain()
                service.set_status.assert_called_once_with(*expected)

    def test_full_result_uses_reload_copy_when_carrier_preflight_failed(self):
        window = SimpleNamespace(
            hosted_edition=False,
            result_carrier_available=False,
            append=lambda *_args, **_kwargs: None,
        )
        service = self.companion.Service(
            spec=next(spec for spec in self.companion.SERVICES if spec.key == "gear"),
            window=window,
        )
        process = object()
        service.process = process
        service.set_status = mock.Mock()

        service.lines.put(
            (process, "Generated 12 recommendations for spec 253.\n")
        )
        service.drain()

        service.set_status.assert_called_once_with(
            "Results ready - reload WoW",
            self.companion.SUCCESS,
            "done",
        )

    def test_local_reload_fallback_overrides_the_generic_result_marker(self):
        window = SimpleNamespace(
            hosted_edition=False,
            result_carrier_available=True,
            append=lambda *_args, **_kwargs: None,
        )
        cases = (
            (
                "gear",
                "Generated 12 recommendations for spec 253. "
                "Reload fallback: use /reload in WoW to load the finished result.\n",
            ),
            (
                "talents",
                "Ready: 8/8 encounters. Reload fallback: use /reload in WoW "
                "to load the finished result.\n",
            ),
        )
        for key, line in cases:
            with self.subTest(key=key):
                service = self.companion.Service(
                    spec=next(spec for spec in self.companion.SERVICES if spec.key == key),
                    window=window,
                )
                process = object()
                service.process = process
                service.set_status = mock.Mock()
                service.lines.put((process, line))
                service.drain()
                service.set_status.assert_called_once_with(
                    "Results ready - reload WoW",
                    self.companion.SUCCESS,
                    "done",
                )


    def test_hosted_shell_ignores_the_caption_only_installer_offer(self):
        banner = mock.Mock()
        window = SimpleNamespace(
            member_edition=False,
            hosted_edition=True,
            captions_page=False,
            update_offer={"version": "99.0.0"},
            update_banner=banner,
            update_busy=False,
        )
        with mock.patch("GuildHub.client.read_update_notice") as read_notice:
            self.companion.CompanionWindow.refresh_update_banner(window)
        self.assertIsNone(window.update_offer)
        banner.pack_forget.assert_called_once_with()
        read_notice.assert_not_called()

    def test_the_full_edition_hides_the_member_installer_offer(self):
        """A voice update must never replace the player's raid-tools app."""
        banner = mock.Mock()
        window = SimpleNamespace(
            member_edition=False,
            hosted_edition=False,
            captions_page=True,
            update_offer={"version": "99.0.0"},
            update_banner=banner,
            update_busy=False,
        )
        with mock.patch("GuildHub.client.read_update_notice") as read_notice:
            self.companion.CompanionWindow.refresh_update_banner(window)
        self.assertIsNone(window.update_offer)
        banner.pack_forget.assert_called_once_with()
        read_notice.assert_not_called()

    def test_update_offer_is_promoted_below_the_hero(self):
        banner = mock.Mock()
        message = mock.Mock()
        service_section = mock.Mock()
        window = SimpleNamespace(
            member_edition=True,
            hosted_edition=False,
            captions_page=False,
            update_offer=None,
            update_banner=banner,
            update_message=message,
            service_section=service_section,
            update_busy=False,
        )
        offer = {"version": "9.9.9"}
        with mock.patch(
            "GuildHub.client.read_update_notice", return_value=offer
        ) as read_notice:
            self.companion.CompanionWindow.refresh_update_banner(window)

        self.assertIs(window.update_offer, offer)
        read_notice.assert_called_once_with()
        message.configure.assert_called_once_with(
            text=(
                "Vemal's Ears 9.9.9 is available. "
                "Updating closes this window and reopens the installer."
            )
        )
        banner.pack.assert_called_once_with(
            fill="x",
            padx=24,
            pady=(12, 0),
            before=service_section,
        )

    def test_paired_guild_name_is_safe_display_only(self):
        store = mock.Mock()
        store.load.return_value = SimpleNamespace(guild_name="Raid Friends")
        self.assertEqual(
            "Raid Friends",
            self.companion.paired_guild_name(credential_store=store),
        )
        store.load.side_effect = ValueError("private credential detail")
        self.assertEqual(
            "",
            self.companion.paired_guild_name(credential_store=store),
        )

    def test_consent_helper_opens_the_paired_discord_guild(self):
        credentials = SimpleNamespace(
            guild_id="123456789012345678",
            guild_name="Raid Friends",
        )
        store = mock.Mock()
        store.load.return_value = credentials
        opened = []

        result = self.companion.prepare_discord_consent(
            credential_store=store,
            opener=lambda url: opened.append(url) or True,
        )

        self.assertTrue(result.paired)
        self.assertTrue(result.discord_opened)
        self.assertEqual("Raid Friends", result.guild_name)
        self.assertEqual(
            ["discord://-/channels/123456789012345678"],
            opened,
        )

    def test_consent_helper_accepts_a_native_launch_that_returns_none(self):
        store = mock.Mock()
        store.load.return_value = SimpleNamespace(
            guild_id="123456789012345678",
            guild_name="Raid Friends",
        )
        browser_opener = mock.Mock(return_value=True)

        with mock.patch.object(
            self.companion.os,
            "startfile",
            return_value=None,
        ) as startfile:
            result = self.companion.prepare_discord_consent(
                credential_store=store,
                browser_opener=browser_opener,
            )

        self.assertTrue(result.discord_opened)
        self.assertFalse(result.browser_opened)
        startfile.assert_called_once_with("discord://-/channels/123456789012345678")
        browser_opener.assert_not_called()

    def test_class_picker_helper_opens_only_the_paired_discord_guild(self):
        store = mock.Mock()
        store.load.return_value = SimpleNamespace(
            guild_id="123456789012345678",
            guild_name="Raid Friends",
        )
        opened = []

        result = self.companion.prepare_discord_class_picker(
            credential_store=store,
            opener=lambda url: opened.append(url) or True,
        )

        self.assertTrue(result.paired)
        self.assertTrue(result.discord_opened)
        self.assertEqual("Raid Friends", result.guild_name)
        self.assertEqual(
            ["discord://-/channels/123456789012345678"],
            opened,
        )

    def test_consent_helper_uses_the_web_only_when_discord_desktop_is_unavailable(self):
        store = mock.Mock()
        store.load.return_value = SimpleNamespace(
            guild_id="123456789012345678",
            guild_name="Raid Friends",
        )
        browser_urls = []

        with mock.patch.object(
            self.companion.os,
            "startfile",
            side_effect=OSError("Discord is not installed"),
        ) as startfile:
            result = self.companion.prepare_discord_consent(
                credential_store=store,
                browser_opener=lambda url: browser_urls.append(url) or True,
            )

        self.assertTrue(result.paired)
        self.assertFalse(result.discord_opened)
        self.assertTrue(result.browser_opened)
        startfile.assert_called_once_with("discord://-/channels/123456789012345678")
        self.assertEqual(
            ["https://discord.com/channels/123456789012345678"],
            browser_urls,
        )

    def test_class_picker_helper_rejects_an_unverified_guild_id(self):
        store = mock.Mock()
        store.load.return_value = SimpleNamespace(
            guild_id="typed-user-or-guild-id",
            guild_name="Raid Friends",
        )
        opener = mock.Mock(return_value=True)

        result = self.companion.prepare_discord_class_picker(
            credential_store=store,
            opener=opener,
        )

        self.assertTrue(result.paired)
        self.assertFalse(result.discord_opened)
        opener.assert_not_called()

    def test_class_picker_instructions_cover_active_and_mixed_version_messages(self):
        from tkinter import messagebox

        window = SimpleNamespace(preview=False, root=object())
        result = self.companion.ConsentLaunchResult(
            paired=True,
            guild_name="Raid Friends",
            discord_opened=True,
        )
        with (
            mock.patch.object(
                self.companion,
                "prepare_discord_class_picker",
                return_value=result,
            ),
            mock.patch.object(messagebox, "showinfo") as showinfo,
        ):
            self.companion.CompanionWindow.open_discord_class_picker(window)

        title, message = showinfo.call_args.args
        self.assertEqual("Choose your class", title)
        self.assertIn("Discord has been asked to open Raid Friends.", message)
        self.assertIn("active Vemal's Ears raid message", message)
        self.assertIn('"Choose your class"', message)
        self.assertIn("If you do not see Choose your class", message)
        self.assertIn("run /vem map in Discord", message)

    def test_unpaired_class_picker_never_claims_discord_opened(self):
        from tkinter import messagebox

        window = SimpleNamespace(preview=False, root=object())
        result = self.companion.ConsentLaunchResult(False, "", False)
        with (
            mock.patch.object(
                self.companion,
                "prepare_discord_class_picker",
                return_value=result,
            ),
            mock.patch.object(messagebox, "showinfo") as showinfo,
        ):
            self.companion.CompanionWindow.open_discord_class_picker(window)

        message = showinfo.call_args.args[1]
        self.assertIn("Pair this computer with your guild first", message)
        self.assertNotIn("Discord is open", message)
        self.assertNotIn("/vem map", message)

    def test_consent_helper_never_opens_an_unverified_guild_id(self):
        store = mock.Mock()
        store.load.return_value = SimpleNamespace(
            guild_id="typed-user-or-guild-id",
            guild_name="Raid Friends",
        )
        opener = mock.Mock(return_value=True)

        result = self.companion.prepare_discord_consent(
            credential_store=store,
            opener=opener,
        )

        self.assertTrue(result.paired)
        self.assertFalse(result.discord_opened)
        opener.assert_not_called()

    def test_unpaired_consent_helper_does_not_open_discord(self):
        store = mock.Mock()
        store.load.return_value = None
        opener = mock.Mock(return_value=True)

        result = self.companion.prepare_discord_consent(
            credential_store=store,
            opener=opener,
        )

        self.assertFalse(result.paired)
        self.assertFalse(result.discord_opened)
        opener.assert_not_called()

    def test_old_reader_eof_cannot_stop_a_restarted_service(self):
        spec = self.companion.SERVICES[0]
        service = self.companion.Service(spec=spec, window=object())
        old_process = object()
        new_process = object()
        service.process = new_process
        service.lines.put((old_process, None))
        service.drain()
        self.assertIs(new_process, service.process)

    def test_member_service_recovers_without_a_restart_button(self):
        window = SimpleNamespace(member_edition=True)
        service = self.companion.Service(
            spec=self.companion.service_specs_for_edition(
                self.companion.MEMBER_EDITION
            )[0],
            window=window,
        )
        process = mock.Mock()
        process.poll.return_value = 1
        service.process = process
        service.lines.put((process, None))

        before = self.companion.time.monotonic()
        service.drain()

        self.assertIsNone(service.process)
        self.assertGreaterEqual(service.restart_at, before + 3.0)

    def test_pairing_argument_is_consumed_without_echoing_the_link(self):
        private_link = "vemalsears://pair?server=wss%3A%2F%2Fears.test&code=ghe_private"

        confirmations = []

        async def pair(_link, _store, *, confirm):
            credentials = SimpleNamespace(
                guild_name="Raid Friends",
                server_url="wss://ears.test",
            )
            self.assertTrue(confirm(credentials, None))
            return credentials

        with mock.patch("GuildHub.pairing.pair_device", side_effect=pair), mock.patch(
            "GuildHub.credentials.WindowsDeviceCredentialStore", return_value=object()
        ):
            notice = self.companion.consume_pairing_argument(
                ["--vemals-ears-pair", private_link],
                confirm_pairing=lambda guild, host, replacing: confirmations.append(
                    (guild, host, replacing)
                ) or True,
            )

        self.assertIsNotNone(notice)
        self.assertTrue(notice.success)
        self.assertEqual("This computer is now paired with Raid Friends.", notice.message)
        self.assertEqual([("Raid Friends", "ears.test", False)], confirmations)
        self.assertNotIn(private_link, repr(notice))
        self.assertNotIn("ghe_private", notice.message)

    def test_pairing_argument_requires_explicit_confirmation(self):
        private_link = "vemalsears://pair?server=wss%3A%2F%2Fears.test&code=ghe_private"

        async def pair(_link, _store, *, confirm):
            from GuildHub.client import GuildHubClientError

            credentials = SimpleNamespace(
                guild_name="Raid Friends",
                server_url="wss://ears.test",
            )
            self.assertFalse(confirm(credentials, None))
            raise GuildHubClientError("Pairing was canceled.")

        with mock.patch("GuildHub.pairing.pair_device", side_effect=pair), mock.patch(
            "GuildHub.credentials.WindowsDeviceCredentialStore", return_value=object()
        ):
            notice = self.companion.consume_pairing_argument(
                ["--vemals-ears-pair", private_link]
            )

        self.assertIsNotNone(notice)
        self.assertFalse(notice.success)
        self.assertEqual("Pairing was canceled.", notice.message)

    def test_pairing_confirmation_discloses_replacement(self):
        seen = []

        async def pair(_link, _store, *, confirm):
            credentials = SimpleNamespace(
                guild_name="New Guild",
                server_url="wss://ears.test:8443",
            )
            existing = SimpleNamespace(guild_name="Old Guild")
            self.assertTrue(confirm(credentials, existing))
            return credentials

        with mock.patch("GuildHub.pairing.pair_device", side_effect=pair), mock.patch(
            "GuildHub.credentials.WindowsDeviceCredentialStore", return_value=object()
        ):
            notice = self.companion.consume_pairing_argument(
                [
                    "--vemals-ears-pair",
                    "vemalsears://pair?server=wss%3A%2F%2Fears.test%3A8443&code=ghe_x",
                ],
                confirm_pairing=lambda guild, host, replacing: seen.append(
                    (guild, host, replacing)
                ) or True,
            )

        self.assertTrue(notice.success)
        self.assertEqual([("New Guild", "ears.test:8443", True)], seen)

    def test_unknown_pairing_failure_is_redacted(self):
        secret = "ghe_exception_secret"

        async def fail(_link, _store, **_kwargs):
            raise RuntimeError(secret)

        with mock.patch("GuildHub.pairing.pair_device", side_effect=fail), mock.patch(
            "GuildHub.credentials.WindowsDeviceCredentialStore", return_value=object()
        ):
            notice = self.companion.consume_pairing_argument(
                ["--vemals-ears-pair", "vemalsears://pair?server=x&code=" + secret]
            )

        self.assertIsNotNone(notice)
        self.assertFalse(notice.success)
        self.assertNotIn(secret, notice.message)

    def test_pairing_flag_must_be_the_only_argument_pair(self):
        notice = self.companion.consume_pairing_argument(
            ["extra", "--vemals-ears-pair", "private"]
        )
        self.assertIsNotNone(notice)
        self.assertFalse(notice.success)
        self.assertNotIn("private", notice.message)

    def test_single_instance_matches_frozen_executable_image(self):
        with tempfile.TemporaryDirectory() as folder:
            lock = Path(folder) / "window.lock"
            lock.write_text("4242", encoding="ascii")
            tasklist = SimpleNamespace(
                stdout='"VemalsEars.exe","4242","Console","1","12,000 K"\n'
            )
            with mock.patch.object(self.companion, "LOCK", lock), mock.patch.object(
                self.companion.os, "getpid", return_value=100
            ), mock.patch.object(
                self.companion.sys, "executable", r"C:\Apps\VemalsEars.exe"
            ), mock.patch.object(
                self.companion.subprocess, "run", return_value=tasklist
            ):
                self.assertTrue(self.companion.another_instance_running())

    def test_single_instance_rejects_recycled_pid_for_other_image(self):
        with tempfile.TemporaryDirectory() as folder:
            lock = Path(folder) / "window.lock"
            lock.write_text("4242", encoding="ascii")
            tasklist = SimpleNamespace(
                stdout='"Discord.exe","4242","Console","1","12,000 K"\n'
            )
            with mock.patch.object(self.companion, "LOCK", lock), mock.patch.object(
                self.companion.os, "getpid", return_value=100
            ), mock.patch.object(
                self.companion.sys, "executable", r"C:\Apps\VemalsEars.exe"
            ), mock.patch.object(
                self.companion.subprocess, "run", return_value=tasklist
            ):
                self.assertFalse(self.companion.another_instance_running())

    def test_installed_launcher_is_used_for_protocol_and_autostart(self):
        with tempfile.TemporaryDirectory() as folder:
            install = Path(folder)
            app = install / "app"
            runtime = install / "runtime"
            app.mkdir()
            runtime.mkdir()
            launcher = install / "VemalsEars.exe"
            launcher.write_bytes(b"launcher")
            (runtime / "pythonw.exe").write_bytes(b"python")

            register = mock.Mock()
            with mock.patch.object(self.companion, "ROOT", app), mock.patch(
                "GuildHub.windows_integration.register_current_user_protocol",
                register,
            ):
                self.assertEqual(launcher, self.companion.installed_launcher())
                self.assertEqual(f'"{launcher}"', self.companion.autostart_command())
                self.assertTrue(self.companion.register_pairing_links(Path("ignored.ico")))

            register.assert_called_once_with(
                launcher,
                pythonw_path=None,
                icon_path=None,
                app="vemalsears",
            )

    def test_instance_mutex_reports_existing_owner(self):
        create = mock.Mock(return_value=123)
        close = mock.Mock()
        kernel = SimpleNamespace(CreateMutexW=create, CloseHandle=close)
        with mock.patch.object(self.companion.ctypes, "WinDLL", return_value=kernel), mock.patch.object(
            self.companion.ctypes, "get_last_error", return_value=183
        ):
            self.assertTrue(self.companion.claim_instance_mutex())
        close.assert_called_once_with(123)

    def test_hosted_mutex_conflict_only_refers_to_downtime(self):
        message = self.companion.instance_conflict_message(
            self.companion.HOSTED_EDITION
        )
        self.assertIn("Downtime Lite is already running", message)
        self.assertNotIn("Vemal", message)
        self.assertNotIn("do not run side by side", message)
        self.assertNotIn("mutex", message.lower())

    def test_release_instance_mutex_closes_the_handle_and_forgets_it(self):
        close = mock.Mock()
        kernel = SimpleNamespace(CloseHandle=close)
        with mock.patch.object(
            self.companion, "INSTANCE_MUTEX_HANDLE", 4242
        ), mock.patch.object(
            self.companion.ctypes, "WinDLL", return_value=kernel
        ):
            self.assertTrue(self.companion.release_instance_mutex())
            close.assert_called_once_with(4242)
            # Forgotten, so a second call cannot close the same handle
            # again after Windows has handed the number to something else.
            self.assertIsNone(self.companion.INSTANCE_MUTEX_HANDLE)
            self.assertFalse(self.companion.release_instance_mutex())
        self.assertEqual(1, close.call_count)

    def test_release_for_shutdown_drops_the_mutex_after_the_services(self):
        """The middle link: 'closed' has to mean the mutex is actually gone.

        Ordering matters inside here too - the mutex is the flag an
        installer reads, so it must not come down while the services it
        stands for are still running.
        """
        events = []
        window = SimpleNamespace(
            close_menu=lambda: events.append("menu"),
            services=[
                SimpleNamespace(stop=lambda: events.append("service-a")),
                SimpleNamespace(stop=lambda: events.append("service-b")),
            ],
            tray=SimpleNamespace(stop=lambda: events.append("tray")),
        )
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.object(
                self.companion, "LOCK", Path(folder) / "absent.lock"
            ), mock.patch.object(
                self.companion,
                "release_instance_mutex",
                lambda: events.append("mutex"),
            ):
                self.companion.CompanionWindow.release_for_shutdown(window)

        self.assertEqual(
            ["menu", "service-a", "service-b", "tray", "mutex"], events
        )

    def test_quitting_goes_through_the_same_stand_down_as_updating(self):
        """One definition of closed, so the update path cannot drift from it."""
        events = []
        window = SimpleNamespace(
            preview=False,
            release_for_shutdown=lambda: events.append("closed"),
            root=SimpleNamespace(destroy=lambda: events.append("destroyed")),
        )
        self.companion.CompanionWindow.quit_now(window)
        self.assertEqual(["closed", "destroyed"], events)

    def test_update_leaves_this_copy_running_for_the_installer_to_close(self):
        """The regression, twice over: this used to close itself.

        The installer owns the closing, at PrepareToInstall, and [Setup]
        declares no AppMutex - so there is no startup check to race and
        nothing to be gained by exiting first. Exiting first would only
        commit to going down before knowing the install worked, which is
        the tradeoff Xenon's agent updater already settled on real
        machines.

        So this pins the absence: downloaded, launched, and still here.
        """
        events = []
        window = SimpleNamespace(
            update_offer={"version": "9.9.9"},
            update_busy=False,
            update_button=SimpleNamespace(configure=lambda **_kw: None),
            update_message=SimpleNamespace(configure=lambda **_kw: None),
            root=SimpleNamespace(
                update_idletasks=lambda: None,
                destroy=lambda: events.append("destroyed"),
            ),
            release_for_shutdown=lambda: events.append("closed"),
        )
        with mock.patch(
            "GuildHub.client.download_verified_update",
            lambda _offer, _target: events.append("downloaded"),
        ), mock.patch.object(
            self.companion.os,
            "startfile",
            lambda _path: events.append("launched"),
            create=True,
        ):
            self.companion.CompanionWindow.install_update(window)

        self.assertEqual(["downloaded", "launched"], events)
        # Still busy, so the banner cannot offer a second installer while
        # the first one is on screen.
        self.assertTrue(window.update_busy)

    def test_a_launch_that_fails_leaves_the_companion_working(self):
        """The reason the installer owns the closing.

        Nothing has been torn down at this point, so a failed launch can
        hand the member back a companion that still captions, instead of
        stopped services and an apology.
        """
        shown = []
        closed = []
        states = []
        window = SimpleNamespace(
            update_offer={"version": "9.9.9"},
            update_busy=False,
            update_button=SimpleNamespace(
                configure=lambda **kw: states.append(kw.get("state"))
            ),
            update_message=SimpleNamespace(configure=lambda **_kw: None),
            root=SimpleNamespace(
                update_idletasks=lambda: None,
                destroy=lambda: closed.append("destroyed"),
            ),
            release_for_shutdown=lambda: closed.append("closed"),
        )

        def explode(_path):
            raise OSError("no shell association")

        with mock.patch(
            "GuildHub.client.download_verified_update", lambda _offer, _target: None
        ), mock.patch.object(
            self.companion.os, "startfile", explode, create=True
        ), mock.patch(
            "tkinter.messagebox.showerror",
            lambda title, message: shown.append((title, message)),
        ):
            self.companion.CompanionWindow.install_update(window)

        self.assertEqual([], closed, "a failed launch must not close anything")
        self.assertIn("no shell association", shown[0][1])
        # And the member can try again.
        self.assertFalse(window.update_busy)
        self.assertEqual("normal", states[-1])

    def test_source_legacy_autostart_remains_truthful(self):
        legacy = self.companion.legacy_source_autostart_commands()[0]
        with mock.patch.object(
            self.companion,
            "read_autostart_value",
            return_value=legacy,
        ), mock.patch.object(self.companion, "installed_launcher", return_value=None):
            self.assertTrue(self.companion.autostart_enabled())


    def test_the_voice_row_builds_a_focus_toggle(self):
        """The focus toggle must exist, be wired to the voice card only, and
        flip the persisted setting - the watch process reads it by mtime.

        Source-level because building a real Tk window starts child
        services. The assertions pin the pieces that would break silently:
        the guard on the voice key, the builder, and the save-then-relabel
        order (relabelling before a failed save would show a state that was
        never persisted).
        """
        source = (ROOT / "WoW-Companion.pyw").read_text(encoding="utf-8")
        self.assertIn('if service.spec.key == "voice":', source)
        self.assertIn(
            "self.build_focus_toggle(display_controls, compact=True)",
            source,
        )
        self.assertIn("def build_focus_toggle", source)
        # Split at the next CLASS-level method, not the first nested def -
        # the builder legitimately contains local helpers.
        builder = source.split("def build_focus_toggle", 1)[1]
        builder = builder.split("\n    def ", 1)[0]
        self.assertIn("focus_mode=not current.focus_mode", builder)
        self.assertIn("if save_settings(updated):", builder)
        save_index = builder.index("if save_settings(updated):")
        relabel_index = builder.index("button.config(text=", save_index)
        self.assertLess(
            save_index, relabel_index,
            "the label must only change after the setting is actually saved",
        )

    def test_the_voice_note_exposes_only_discord_authenticated_consent(self):
        source = (ROOT / "WoW-Companion.pyw").read_text(encoding="utf-8")
        self.assertIn('"My voice consent"', source)
        self.assertIn("self.open_discord_consent", source)
        self.assertIn("Only a Discord interaction can prove", source)
        self.assertIn("takefocus=True", source)
        self.assertNotIn('"Consent", self.open_discord_consent', source)

    def test_managed_shells_have_no_manual_service_lifecycle_control(self):
        source = (ROOT / "WoW-Companion.pyw").read_text(encoding="utf-8")
        builder = source.split("def build_row", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("if service.spec.show_lifecycle_controls:", builder)
        self.assertIn('action_controls, "Stop", service.toggle', builder)
        self.assertIn("service_specs_for_edition(self.edition)", source)
        self.assertIn("get_theme_for_edition(selected)", source)
        self.assertIn("self.theme.badge_label", source)
        self.assertIn("Captions reconnect automatically.", source)
        tick = source.split("def tick", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("self.refresh_pairing_identity()", tick)
        self.assertIn("self.reset_quick_chat_profile_for_pairing()", tick)
        self.assertNotIn("service.stop()", tick)
        pairing = tick.split("if self.consume_pairing_signal()", 1)[-1]
        self.assertNotIn("service.start()", pairing)


class CaptionDisplaySettingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    def test_caption_controls_persist_each_value_without_losing_the_other(self):
        from VoiceCaptions import settings as caption_settings

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "overlay-settings.json"
            window = object.__new__(self.companion.CompanionWindow)
            with mock.patch.object(
                caption_settings, "default_settings_path", return_value=path
            ):
                self.assertEqual(6.0, window.set_caption_hold_seconds(6.0))
                self.assertEqual(4, window.set_max_visible_speakers(4))
                self.assertEqual(18, window.set_caption_font_size(18))
                loaded = caption_settings.load_settings(path)
            self.assertEqual(6.0, loaded.caption_hold_seconds)
            self.assertEqual(4, loaded.max_visible_speakers)
            self.assertEqual(18, loaded.caption_font_size)

    def test_failed_caption_setting_write_reports_the_previous_value(self):
        from VoiceCaptions import settings as caption_settings

        window = object.__new__(self.companion.CompanionWindow)
        current = caption_settings.OverlaySettings(caption_hold_seconds=6.0)
        with mock.patch.object(
            caption_settings, "load_settings", return_value=current
        ), mock.patch.object(
            caption_settings, "save_settings", return_value=False
        ):
            result = window.set_caption_hold_seconds(10.0)
        self.assertEqual(6.0, result)

    def test_font_controls_live_in_move_preview_not_preferences(self):
        source = (ROOT / "WoW-Companion.pyw").read_text(encoding="utf-8")
        self.assertIn('"Caption duration"', source)
        self.assertIn('"Speakers on screen"', source)
        self.assertNotIn('"Caption text size"', source)
        # Typed entry rows, not the old stepped option lists.
        self.assertIn("self.entry_row(", source)
        placement_source = (
            ROOT / "VoiceCaptions" / "placement.py"
        ).read_text(encoding="utf-8")
        self.assertIn('text="Font"', placement_source)
        self.assertIn('text="Size"', placement_source)
        self.assertIn('text="Backdrop"', placement_source)
        self.assertIn('text="Colours"', placement_source)
        self.assertNotIn("CAPTION_HOLD_OPTIONS", source)
        self.assertNotIn("VISIBLE_SPEAKER_OPTIONS", source)

    def test_typed_value_clamps_into_range_and_rejects_non_numbers(self):
        """A text box takes anything. A number past the range is pulled to
        the nearest end - the reach was clear even when the value was not -
        and text that is not a number returns None so the caller restores
        the value in effect rather than writing a guess over the only
        channel a deaf member has."""
        clamp = self.companion.clamp_display_value
        whole = self.companion.whole_number
        # Lines: type past the top, land on the top; a real value passes.
        self.assertEqual(20, clamp("30", 1, 20, whole))
        self.assertEqual(1, clamp("0", 1, 20, whole))
        self.assertEqual(14, clamp("14", 1, 20, whole))
        # Font and hold clamp the same way, hold as a float.
        self.assertEqual(48, clamp("500", 6, 48, whole))
        self.assertEqual(0.5, clamp("0", 0.5, 60.0, float))
        self.assertEqual(30.0, clamp("30", 0.5, 60.0, float))
        # Whitespace is trimmed, not treated as garbage.
        self.assertEqual(12, clamp("  12  ", 6, 48, whole))
        # A typed decimal rounds rather than being thrown away, so the
        # member sees a number near what they asked for.
        self.assertEqual(13, clamp("12.6", 1, 20, whole))
        self.assertEqual(12.5, clamp("12.5", 0.5, 60.0, float))
        # Not a number at all - None, so the field snaps back to what was
        # showing rather than writing a guess over a live setting.
        self.assertIsNone(clamp("big", 6, 48, whole))
        self.assertIsNone(clamp("", 1, 20, whole))
        self.assertIsNone(clamp("nan", 0.5, 60.0, float))
        self.assertIsNone(clamp("inf", 1, 20, whole))
        # An infinite hold is still a number, so it clamps to the ceiling
        # instead of being rejected.
        self.assertEqual(60.0, clamp("inf", 0.5, 60.0, float))

    def test_keyboard_focus_inside_settings_does_not_dismiss_it(self):
        container = SimpleNamespace(master=None)
        row = SimpleNamespace(master=container)
        arrow = SimpleNamespace(master=row)
        outside = SimpleNamespace(master=None)
        contains = self.companion.SettingsMenu.contains_widget
        self.assertTrue(contains(container, arrow))
        self.assertFalse(contains(container, outside))

    def test_settings_popup_flips_above_and_respects_negative_monitors(self):
        position = self.companion.anchored_popup_position(
            (1100, 680, 30, 30),
            (400, 292),
            (0, 0, 1280, 720),
        )
        self.assertEqual((730, 384), position)

        negative = self.companion.anchored_popup_position(
            (-100, -50, 30, 30),
            (400, 292),
            (-1920, -1080, 1920, 1080),
        )
        self.assertLess(negative[0], 0)
        self.assertGreaterEqual(negative[0], -1920)
        self.assertGreaterEqual(negative[1], -1080)


class CaptionTargetPickerTests(unittest.TestCase):
    """The companion only saves durable caption-target identities."""

    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    class Widget:
        def __init__(self):
            self.configured = []

        def config(self, **kwargs):
            self.configured.append(kwargs)

    def test_target_identity_excludes_ephemeral_window_details(self):
        target = SimpleNamespace(
            mode="window",
            executable="game.exe",
            monitor_index=0,
            hwnd=101,
            process_id=202,
        )
        replacement = SimpleNamespace(
            mode="window",
            executable="game.exe",
            monitor_index=0,
            hwnd=303,
            process_id=404,
        )
        self.assertEqual(
            self.companion.caption_target_key(target),
            self.companion.caption_target_key(replacement),
        )

    def test_target_summary_is_understandable_without_a_live_enumeration(self):
        from VoiceCaptions.overlay import CaptionTargetChoice

        summary = self.companion.caption_target_summary
        self.assertEqual("Auto - World of Warcraft", summary(CaptionTargetChoice()))
        self.assertEqual(
            "Window - eldenring",
            summary(CaptionTargetChoice("window", "EldenRing.exe")),
        )
        self.assertEqual(
            "Monitor 2",
            summary(CaptionTargetChoice("monitor", monitor_index=1)),
        )

    def test_selecting_a_target_persists_to_overlay_settings(self):
        from VoiceCaptions import overlay
        from VoiceCaptions import settings as caption_settings

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "overlay-settings.json"
            window = object.__new__(self.companion.CompanionWindow)
            window.preview = False
            target = overlay.CaptionTargetChoice("window", "game.exe")
            with mock.patch.object(
                caption_settings,
                "default_settings_path",
                return_value=path,
            ):
                self.assertTrue(window.set_caption_target(target))
                saved = caption_settings.load_settings(path)
                self.assertEqual(target, window.caption_target_choice())
            self.assertEqual("window", saved.anchor_mode)
            self.assertEqual("game.exe", saved.anchor_executable)
            self.assertEqual(0, saved.anchor_monitor)

    def test_auto_target_clears_an_old_window_anchor(self):
        from VoiceCaptions import overlay
        from VoiceCaptions import settings as caption_settings

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "overlay-settings.json"
            window = object.__new__(self.companion.CompanionWindow)
            window.preview = False
            original = caption_settings.OverlaySettings(
                anchor_mode="window",
                anchor_executable="old-game.exe",
                anchor_monitor=3,
            )
            with mock.patch.object(
                caption_settings,
                "default_settings_path",
                return_value=path,
            ):
                self.assertTrue(caption_settings.save_settings(original))
                self.assertTrue(window.set_caption_target(overlay.CaptionTargetChoice()))
                saved = caption_settings.load_settings(path)
            self.assertEqual("auto", saved.anchor_mode)
            self.assertEqual("", saved.anchor_executable)
            self.assertEqual(0, saved.anchor_monitor)

    def test_monitor_target_persists_the_candidate_index(self):
        from VoiceCaptions import overlay
        from VoiceCaptions import settings as caption_settings

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "overlay-settings.json"
            window = object.__new__(self.companion.CompanionWindow)
            window.preview = False
            original = caption_settings.OverlaySettings(
                anchor_mode="window",
                anchor_executable="old-game.exe",
                anchor_monitor=0,
            )
            target = overlay.CaptionTargetChoice("monitor", monitor_index=2)
            with mock.patch.object(
                caption_settings,
                "default_settings_path",
                return_value=path,
            ):
                self.assertTrue(caption_settings.save_settings(original))
                self.assertTrue(window.set_caption_target(target))
                saved = caption_settings.load_settings(path)
            self.assertEqual("monitor", saved.anchor_mode)
            self.assertEqual("", saved.anchor_executable)
            self.assertEqual(2, saved.anchor_monitor)

    def test_failed_target_save_keeps_the_previous_target(self):
        from VoiceCaptions import overlay
        from VoiceCaptions import settings as caption_settings

        window = object.__new__(self.companion.CompanionWindow)
        window.preview = False
        current = caption_settings.OverlaySettings()
        target = overlay.CaptionTargetChoice("monitor", monitor_index=0)
        with mock.patch.object(
            caption_settings,
            "load_settings",
            return_value=current,
        ), mock.patch.object(
            caption_settings,
            "save_settings",
            return_value=False,
        ):
            self.assertFalse(window.set_caption_target(target))

    def test_refresh_lists_windows_and_monitors_separately(self):
        from VoiceCaptions import overlay

        window_target = overlay.CaptionTargetChoice("window", "game.exe")
        monitor_target = overlay.CaptionTargetChoice("monitor", monitor_index=0)
        menu = object.__new__(self.companion.CaptionTargetMenu)
        menu.window = SimpleNamespace(
            preview=False,
            caption_target_choice=lambda: overlay.CaptionTargetChoice(),
            theme=self.companion.CompanionWindow.theme,
        )
        menu.refresh_button = self.Widget()
        menu.status = self.Widget()
        menu.current_target = overlay.CaptionTargetChoice()
        menu.update_idletasks = mock.Mock()
        menu.rebuild_options = mock.Mock()
        menu.current_target_is_listed = mock.Mock(return_value=True)
        windows = [
            SimpleNamespace(
                target=window_target,
                label="Game - game.exe",
            )
        ]
        monitors = [
            SimpleNamespace(
                target=monitor_target,
                label="Display 1 - Primary",
            )
        ]
        with mock.patch.object(
            overlay,
            "enumerate_caption_windows",
            return_value=windows,
        ), mock.patch.object(
            overlay,
            "enumerate_caption_monitors",
            return_value=monitors,
        ):
            self.companion.CaptionTargetMenu.refresh_choices(menu)

        self.assertEqual(tuple(windows), menu.window_candidates)
        self.assertEqual(tuple(monitors), menu.monitor_candidates)
        self.assertFalse(menu.window_listing_failed)
        self.assertFalse(menu.monitor_listing_failed)
        menu.rebuild_options.assert_called_once_with()
        self.assertIn(
            {"text": "Choose where the caption overlay should appear.", "fg": self.companion.MUTED},
            menu.status.configured,
        )

    def test_missing_choice_explains_the_automatic_fallback(self):
        from VoiceCaptions import overlay

        target = overlay.CaptionTargetChoice("window", "game.exe")
        menu = object.__new__(self.companion.CaptionTargetMenu)
        menu.window = SimpleNamespace(
            preview=False,
            caption_target_choice=lambda: target,
            theme=self.companion.CompanionWindow.theme,
        )
        menu.refresh_button = self.Widget()
        menu.status = self.Widget()
        menu.current_target = target
        menu.update_idletasks = mock.Mock()
        menu.rebuild_options = mock.Mock()
        menu.current_target_is_listed = mock.Mock(return_value=False)
        with mock.patch.object(
            overlay,
            "enumerate_caption_windows",
            return_value=[],
        ), mock.patch.object(
            overlay,
            "enumerate_caption_monitors",
            return_value=[],
        ):
            self.companion.CaptionTargetMenu.refresh_choices(menu)

        self.assertIn(
            {
                "text": (
                    "Your selected target is not available right now. Captions "
                    "will use automatic game detection until it returns."
                ),
                "fg": self.companion.WARNING,
            },
            menu.status.configured,
        )

    def test_failed_selection_keeps_the_current_target_and_explains_it(self):
        from VoiceCaptions.overlay import CaptionTargetChoice

        current = CaptionTargetChoice()
        menu = object.__new__(self.companion.CaptionTargetMenu)
        menu.current_target = current
        menu.status = self.Widget()
        menu.window = SimpleNamespace(
            set_caption_target=mock.Mock(return_value=False),
            refresh_caption_target_card=mock.Mock(),
        )
        menu.rebuild_options = mock.Mock()

        self.companion.CaptionTargetMenu.select_target(
            menu,
            CaptionTargetChoice("monitor", monitor_index=0),
            "Display 1",
        )

        self.assertEqual(current, menu.current_target)
        menu.window.refresh_caption_target_card.assert_not_called()
        menu.rebuild_options.assert_not_called()
        self.assertEqual(
            "Could not save this target. Your previous target is still active.",
            menu.status.configured[-1]["text"],
        )

    def test_successful_selection_updates_the_card_and_rebuilds_the_picker(self):
        from VoiceCaptions.overlay import CaptionTargetChoice

        target = CaptionTargetChoice("monitor", monitor_index=0)
        menu = object.__new__(self.companion.CaptionTargetMenu)
        menu.current_target = CaptionTargetChoice()
        menu.status = self.Widget()
        menu.window = SimpleNamespace(
            set_caption_target=mock.Mock(return_value=True),
            refresh_caption_target_card=mock.Mock(),
        )
        menu.rebuild_options = mock.Mock()

        self.companion.CaptionTargetMenu.select_target(menu, target, "Display 1")

        self.assertEqual(target, menu.current_target)
        menu.window.refresh_caption_target_card.assert_called_once_with()
        menu.rebuild_options.assert_called_once_with()
        self.assertEqual(
            "Display 1 is now your caption target.",
            menu.status.configured[-1]["text"],
        )

    def test_specific_voice_mute_updates_only_the_local_display_setting(self):
        from VoiceCaptions import settings as caption_settings

        speaker_id = "a" * 24
        current = caption_settings.OverlaySettings()
        saved = []
        menu = object.__new__(self.companion.SpeakerMuteMenu)
        menu.window = SimpleNamespace(
            preview=False,
            refresh_speaker_mute_card=mock.Mock(),
        )
        menu.status = self.Widget()
        menu.rebuild = mock.Mock()
        with mock.patch.object(
            caption_settings,
            "load_settings",
            return_value=current,
        ), mock.patch.object(
            caption_settings,
            "save_settings",
            side_effect=lambda value: saved.append(value) or True,
        ):
            self.companion.SpeakerMuteMenu.toggle(menu, speaker_id)

        self.assertEqual((speaker_id,), saved[0].muted_speaker_ids)
        self.assertFalse(saved[0].muted)
        menu.window.refresh_speaker_mute_card.assert_called_once_with()
        menu.rebuild.assert_called_once_with()

    def test_specific_voice_unmute_preserves_other_muted_people(self):
        from VoiceCaptions import settings as caption_settings

        first = "a" * 24
        second = "b" * 24
        current = caption_settings.OverlaySettings(
            muted_speaker_ids=(first, second),
        )
        saved = []
        menu = object.__new__(self.companion.SpeakerMuteMenu)
        menu.window = SimpleNamespace(
            preview=False,
            refresh_speaker_mute_card=mock.Mock(),
        )
        menu.status = self.Widget()
        menu.rebuild = mock.Mock()
        with mock.patch.object(
            caption_settings,
            "load_settings",
            return_value=current,
        ), mock.patch.object(
            caption_settings,
            "save_settings",
            side_effect=lambda value: saved.append(value) or True,
        ):
            self.companion.SpeakerMuteMenu.toggle(menu, first)

        self.assertEqual((second,), saved[0].muted_speaker_ids)

    def test_mute_card_summary_reports_the_persisted_count(self):
        from VoiceCaptions import settings as caption_settings

        window = object.__new__(self.companion.CompanionWindow)
        window.preview = False
        with mock.patch.object(
            caption_settings,
            "load_settings",
            return_value=caption_settings.OverlaySettings(
                muted_speaker_ids=("a" * 24, "b" * 24),
            ),
        ):
            summary = self.companion.CompanionWindow.speaker_mute_summary(window)
        self.assertEqual("2 voices hidden on this computer", summary)


class HotkeyRebindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    def test_capture_canonicalizes_supported_keys_and_rejects_typing(self):
        capture = self.companion.captured_hotkey_spec
        self.assertEqual("Ctrl+Alt+G", capture({"Alt", "Ctrl"}, "g"))
        self.assertEqual("Shift+F9", capture({"Shift"}, "F9"))
        self.assertEqual(
            "Ctrl+Shift+9",
            capture({"Ctrl", "Shift"}, "parenleft", 0x39),
        )
        self.assertIsNone(capture({"Shift"}, "v"))
        self.assertIsNone(capture({"Ctrl"}, "space"))
        self.assertIsNone(capture(set(), "v"))

    def test_candidate_registers_before_the_working_shortcut_is_released(self):
        events = []
        fake = SimpleNamespace(
            RegisterHotKey=lambda hwnd, hotkey_id, modifiers, key: events.append(
                ("register", hotkey_id, modifiers, key)
            ),
            UnregisterHotKey=lambda hwnd, hotkey_id: events.append(
                ("unregister", hotkey_id)
            ),
        )
        tray = self.companion.TrayIcon("Vemal's Ears", None)
        tray.hwnd = 44
        tray.hotkey = self.companion.hotkey_support()[1]("Ctrl+Alt+V")
        tray.hotkey_registered = True
        candidate = self.companion.hotkey_support()[1]("Ctrl+Shift+G")

        with mock.patch.dict(sys.modules, {"win32gui": fake}):
            self.assertTrue(tray._replace_hotkey_on_thread(candidate))

        self.assertEqual("register", events[0][0])
        self.assertEqual(tray.SPEAK_HOTKEY_ALT_ID, events[0][1])
        self.assertEqual(("unregister", tray.SPEAK_HOTKEY_ID), events[1])
        self.assertEqual(candidate, tray.hotkey)
        self.assertEqual(tray.SPEAK_HOTKEY_ALT_ID, tray.hotkey_id)

    def test_conflicting_candidate_leaves_the_working_shortcut_intact(self):
        events = []

        def occupied(*_args):
            events.append("register")
            raise OSError("already registered")

        fake = SimpleNamespace(
            RegisterHotKey=occupied,
            UnregisterHotKey=lambda *_args: events.append("unregister"),
        )
        tray = self.companion.TrayIcon("Vemal's Ears", None)
        tray.hwnd = 44
        old = self.companion.hotkey_support()[1]("Ctrl+Alt+V")
        tray.hotkey = old
        tray.hotkey_registered = True

        with mock.patch.dict(sys.modules, {"win32gui": fake}):
            self.assertFalse(
                tray._replace_hotkey_on_thread(
                    self.companion.hotkey_support()[1]("Ctrl+Shift+G")
                )
            )

        self.assertEqual(["register"], events)
        self.assertEqual(old, tray.hotkey)
        self.assertEqual(tray.SPEAK_HOTKEY_ID, tray.hotkey_id)
        self.assertTrue(tray.hotkey_registered)

    def test_successful_change_persists_the_canonical_shortcut(self):
        window = object.__new__(self.companion.CompanionWindow)
        window.managed_edition = True
        window.caption_suite = True
        window.settings = {
            "minimize_to_taskbar": True,
            "speak_hotkey": "Ctrl+Alt+V",
        }
        window.tray = SimpleNamespace(
            hotkey_registered=True,
            update_hotkey=mock.Mock(return_value=True),
        )
        saved = []
        with mock.patch.object(
            self.companion,
            "save_settings",
            side_effect=lambda settings: saved.append(dict(settings)) or True,
        ):
            success, message = window.set_speak_hotkey(" shift + control + g ")

        self.assertTrue(success)
        self.assertEqual("Ctrl+Shift+G", window.settings["speak_hotkey"])
        self.assertEqual("Ctrl+Shift+G", saved[0]["speak_hotkey"])
        self.assertIn("Ctrl+Shift+G", message)

    def test_conflict_does_not_change_the_saved_shortcut(self):
        window = object.__new__(self.companion.CompanionWindow)
        window.managed_edition = True
        window.caption_suite = True
        window.settings = {
            "minimize_to_taskbar": True,
            "speak_hotkey": "Ctrl+Alt+V",
        }
        window.tray = SimpleNamespace(
            hotkey_registered=True,
            update_hotkey=mock.Mock(return_value=False),
        )
        with mock.patch.object(self.companion, "save_settings") as save:
            success, message = window.set_speak_hotkey("Ctrl+Shift+G")

        self.assertFalse(success)
        self.assertIn("previous shortcut is unchanged", message)
        self.assertEqual("Ctrl+Alt+V", window.settings["speak_hotkey"])
        save.assert_not_called()

    def test_save_failure_rolls_the_live_registration_back(self):
        window = object.__new__(self.companion.CompanionWindow)
        window.managed_edition = True
        window.caption_suite = True
        window.settings = {
            "minimize_to_taskbar": True,
            "speak_hotkey": "Ctrl+Alt+V",
        }
        update = mock.Mock(side_effect=(True, True))
        window.tray = SimpleNamespace(
            hotkey_registered=True,
            update_hotkey=update,
        )
        parse = self.companion.hotkey_support()[1]
        with mock.patch.object(self.companion, "save_settings", return_value=False):
            success, message = window.set_speak_hotkey("Ctrl+Shift+G")

        self.assertFalse(success)
        self.assertIn("Nothing changed", message)
        self.assertEqual(
            [mock.call(parse("Ctrl+Shift+G")), mock.call(parse("Ctrl+Alt+V"))],
            update.call_args_list,
        )
        self.assertEqual("Ctrl+Alt+V", window.settings["speak_hotkey"])

    def test_failed_rollback_reports_the_session_shortcut_truthfully(self):
        window = object.__new__(self.companion.CompanionWindow)
        window.managed_edition = True
        window.caption_suite = True
        window.settings = {
            "minimize_to_taskbar": True,
            "speak_hotkey": "Ctrl+Alt+V",
        }
        window.tray = SimpleNamespace(
            hotkey_registered=True,
            update_hotkey=mock.Mock(side_effect=(True, False)),
        )
        with mock.patch.object(self.companion, "save_settings", return_value=False):
            success, message = window.set_speak_hotkey("Ctrl+Shift+G")

        self.assertTrue(success)
        self.assertIn("works for this session", message)
        self.assertEqual("Ctrl+Shift+G", window.settings["speak_hotkey"])

    def test_shutdown_closes_the_tray_window_even_without_an_icon(self):
        posted = mock.Mock()
        tray = self.companion.TrayIcon("Vemal's Ears", None)
        tray.hwnd = 77
        tray.available = False
        with mock.patch.dict(
            sys.modules,
            {
                "win32con": SimpleNamespace(WM_CLOSE=16),
                "win32gui": SimpleNamespace(PostMessage=posted),
            },
        ):
            tray.stop()
        posted.assert_called_once_with(77, 16, 0, 0)

    def test_hotkey_dispatch_is_suppressed_while_recording(self):
        tray = self.companion.TrayIcon("Vemal's Ears", None)
        tray.hotkey_registered = True
        tray.hotkey_suppressed = True
        tray._on_hotkey(None, None, tray.SPEAK_HOTKEY_ID, None)
        with self.assertRaises(self.companion.queue.Empty):
            tray.commands.get_nowait()

    def test_resume_barrier_discards_the_capture_key_before_dispatch_resumes(self):
        tray = self.companion.TrayIcon("Vemal's Ears", None)
        tray.hotkey_registered = True
        tray.hotkey_suppressed = True
        request = self.companion.HotkeySuppressionRequest(False)
        request_id = tray._store_hotkey_request(request)

        # The same shortcut's pending WM_HOTKEY sits ahead of the resume
        # control message in the tray window queue.
        tray._on_hotkey(None, None, tray.SPEAK_HOTKEY_ID, None)
        tray._on_hotkey_update(None, None, request_id, None)

        self.assertTrue(request.success)
        self.assertFalse(tray.hotkey_suppressed)
        with self.assertRaises(self.companion.queue.Empty):
            tray.commands.get_nowait()

    def test_stale_control_message_cannot_consume_a_later_resume_barrier(self):
        tray = self.companion.TrayIcon("Vemal's Ears", None)
        tray.hotkey_registered = True
        tray.hotkey_suppressed = True
        stale = self.companion.HotkeySuppressionRequest(True)
        stale.cancelled = True
        resume = self.companion.HotkeySuppressionRequest(False)
        stale_id = tray._store_hotkey_request(stale)
        resume_id = tray._store_hotkey_request(resume)

        tray._on_hotkey_update(None, None, stale_id, None)
        self.assertTrue(tray.hotkey_suppressed)
        self.assertFalse(resume.done.is_set())
        tray._on_hotkey(None, None, tray.SPEAK_HOTKEY_ID, None)
        with self.assertRaises(self.companion.queue.Empty):
            tray.commands.get_nowait()

        tray._on_hotkey_update(None, None, resume_id, None)
        self.assertFalse(tray.hotkey_suppressed)

    def test_resume_timeout_leaves_the_ordered_barrier_live(self):
        tray = self.companion.TrayIcon("Vemal's Ears", None)
        tray.hwnd = 44
        tray._hotkey_message = 55
        tray.hotkey_suppressed = True
        posted = mock.Mock()
        with mock.patch.dict(
            sys.modules,
            {"win32gui": SimpleNamespace(PostMessage=posted)},
        ):
            self.assertFalse(tray.set_hotkey_suppressed(False, timeout=0))

        self.assertTrue(tray.hotkey_suppressed)
        request_id = posted.call_args.args[2]
        tray._on_hotkey_update(None, None, request_id, None)
        self.assertFalse(tray.hotkey_suppressed)

    def test_capture_cancels_when_focus_leaves_preferences(self):
        cancel = mock.Mock()
        menu = SimpleNamespace(
            capture_active=True,
            focus_get=lambda: SimpleNamespace(master=None),
            contains_widget=lambda _container, _widget: False,
            cancel_hotkey_capture=cancel,
        )
        self.companion.SettingsMenu.cancel_capture_if_focus_left(menu)
        cancel.assert_called_once_with()


class SupportLinkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    def test_support_links_open_only_owner_approved_destinations(self):
        for url in (
            self.companion.PATREON_CUSTOM_PLEDGE_URL,
            self.companion.PAYPAL_DONATE_URL,
        ):
            with self.subTest(url=url):
                opener = mock.Mock(return_value=True)
                self.assertTrue(self.companion.open_support_url(url, opener=opener))
                opener.assert_called_once_with(url)

    def test_support_links_reject_unknown_or_failed_destinations(self):
        opener = mock.Mock(return_value=True)
        self.assertFalse(
            self.companion.open_support_url("https://example.invalid", opener=opener)
        )
        opener.assert_not_called()

        failed = mock.Mock(side_effect=OSError("browser unavailable"))
        self.assertFalse(
            self.companion.open_support_url(
                self.companion.PATREON_CUSTOM_PLEDGE_URL,
                opener=failed,
            )
        )

    def test_preview_never_opens_a_support_destination(self):
        window = SimpleNamespace(preview=True, footer_status=mock.Mock())
        with mock.patch.object(self.companion, "open_support_url") as open_url:
            self.companion.CompanionWindow.open_support_link(
                window,
                self.companion.PATREON_CUSTOM_PLEDGE_URL,
                "Patreon",
            )
            self.companion.CompanionWindow.open_support_link(
                window,
                self.companion.PAYPAL_DONATE_URL,
                "PayPal",
            )
        open_url.assert_not_called()


class NightwatchUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    def test_activity_starts_collapsed_and_opens_before_the_footer(self):
        window = object.__new__(self.companion.CompanionWindow)
        window.activity_expanded = False
        window.activity_panel = mock.Mock()
        window.activity_button = mock.Mock()
        window.footer = object()
        window.main_surface = mock.Mock()
        window.root = mock.Mock()

        window.toggle_activity()

        self.assertTrue(window.activity_expanded)
        window.activity_panel.pack.assert_called_once_with(
            fill="x",
            padx=24,
            pady=(6, 0),
            before=window.footer,
        )
        window.root.after_idle.assert_called_once_with(
            window.main_surface.show_bottom
        )
        window.activity_button.config.assert_called_with(text="Hide")

        window.toggle_activity()
        self.assertFalse(window.activity_expanded)
        window.activity_panel.pack_forget.assert_called_once_with()
        window.activity_button.config.assert_called_with(text="Show")

    def test_every_tier_keeps_support_links_in_the_titlebar(self):
        import tkinter as tk

        for edition in (
            self.companion.MEMBER_EDITION,
            self.companion.HOSTED_EDITION,
            self.companion.FULL_EDITION,
        ):
            with self.subTest(edition=edition):
                root = tk.Tk()
                root.withdraw()
                try:
                    window = self.companion.CompanionWindow(
                        root,
                        edition=edition,
                        preview=True,
                    )
                    root.update()
                    self.assertTrue(window.support_frame.winfo_ismapped())
                    self.assertEqual("Patreon", window.patreon_button.cget("text"))
                    self.assertEqual("PayPal", window.paypal_button.cget("text"))
                    self.assertEqual(1, int(window.patreon_button.cget("takefocus")))
                    self.assertEqual(1, int(window.paypal_button.cget("takefocus")))
                    support_right = (
                        window.support_frame.winfo_rootx()
                        + window.support_frame.winfo_width()
                    )
                    self.assertLessEqual(
                        support_right,
                        window.settings_button.winfo_rootx(),
                    )
                finally:
                    root.destroy()

    def test_preview_service_never_starts_a_child_process(self):
        window = SimpleNamespace(preview=True, update_service_signal=lambda *_args: None)
        service = self.companion.Service(
            spec=self.companion.SERVICES[0],
            window=window,
        )
        service.set_status = mock.Mock()
        with mock.patch.object(self.companion.subprocess, "Popen") as launch:
            service.start()
        launch.assert_not_called()
        service.set_status.assert_called_once_with(
            "Ready",
            self.companion.SUCCESS,
            "ready",
        )

    def test_downtime_has_no_voice_service_or_recording_controls(self):
        import tkinter as tk

        for edition in (self.companion.FULL_EDITION, self.companion.HOSTED_EDITION):
            with self.subTest(edition=edition):
                root = tk.Tk()
                root.withdraw()
                try:
                    window = self.companion.CompanionWindow(
                        root, edition=edition, preview=True,
                    )
                    self.assertEqual(
                        {"gear", "talents"},
                        {service.spec.key for service in window.services},
                    )
                    for service in window.services:
                        self.assertIsNone(service.listening_indicator)
                    self.assertIsNone(window.member_caption_button)
                finally:
                    root.destroy()

    def test_every_service_keeps_actions_left_of_right_edge_statuses(self):
        import tkinter as tk

        def x_within(widget, ancestor):
            position = 0
            current = widget
            while current is not ancestor:
                position += current.winfo_x()
                current = current.master
            return position

        def descends_from(widget, ancestor):
            current = widget
            while current is not None:
                if current is ancestor:
                    return True
                current = getattr(current, "master", None)
            return False

        for edition in (
            self.companion.FULL_EDITION,
            self.companion.HOSTED_EDITION,
            self.companion.MEMBER_EDITION,
        ):
            with self.subTest(edition=edition):
                root = tk.Tk()
                root.withdraw()
                try:
                    window = self.companion.CompanionWindow(
                        root,
                        edition=edition,
                        preview=True,
                    )
                    width, height = window.minimum_size
                    root.geometry(f"{max(width, 1100)}x{height}")
                    root.update_idletasks()

                    for service in window.services:
                        with self.subTest(service=service.spec.key):
                            card = service.rail.master
                            # Older preview harnesses can still call the
                            # single-page compatibility method.
                            on_captions_page = (
                                window.captions_surface is not None
                                and descends_from(card, window.captions_surface)
                            )
                            window.show_page(
                                1 if on_captions_page else 0, remember=False
                            )
                            root.update_idletasks()
                            rail_right = (
                                service.rail.winfo_x()
                                + service.rail.winfo_width()
                            )
                            self.assertLessEqual(
                                abs(card.winfo_width() - rail_right),
                                1,
                            )

                            indicators = [service.dot]
                            for widget in (
                                service.result_badge,
                                service.lifecycle_badge,
                                service.listening_indicator,
                            ):
                                if widget is None or not widget.winfo_manager():
                                    continue
                                indicators.append(
                                    widget.master
                                    if isinstance(widget, tk.Label)
                                    else widget
                                )

                            actions = [
                                widget
                                for widget in (
                                    service.button,
                                    service.start_button,
                                    service.stop_button,
                                )
                                if widget is not None
                            ]
                            if (
                                service is window.member_caption_service
                                and window.member_caption_button is not None
                                and descends_from(window.member_caption_button, card)
                            ):
                                # Every edition puts the pause on the voice
                                # row now; the guard stays so a button that
                                # moved off the card is not measured against it.
                                actions.append(window.member_caption_button)

                            dot_left = x_within(service.dot, card)
                            for indicator in indicators:
                                if indicator is service.dot:
                                    continue
                                self.assertLessEqual(
                                    x_within(indicator, card)
                                    + indicator.winfo_width(),
                                    dot_left,
                                )
                            for action in actions:
                                action_right = (
                                    x_within(action, card)
                                    + action.winfo_width()
                                )
                                for indicator in indicators:
                                    if indicator is service.listening_indicator:
                                        continue
                                    self.assertLessEqual(
                                        action_right,
                                        x_within(indicator, card),
                                    )
                            if (
                                service.listening_indicator is not None
                                and service.listening_indicator.winfo_manager()
                                and service.start_button is not None
                            ):
                                self.assertLessEqual(
                                    x_within(service.listening_indicator, card)
                                    + service.listening_indicator.winfo_width(),
                                    x_within(service.start_button, card),
                                )
                            self.assertLessEqual(
                                dot_left + service.dot.winfo_width(),
                                service.rail.winfo_x(),
                            )

                            if (
                                service.start_button is not None
                                and service.stop_button is not None
                            ):
                                self.assertLess(
                                    x_within(service.start_button, card),
                                    x_within(service.stop_button, card),
                                )
                finally:
                    root.destroy()

    def test_hosted_guild_card_exposes_console_on_the_guild_host(self):
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        try:
            with mock.patch.object(
                self.companion,
                "operator_console_available",
                return_value=True,
            ):
                window = self.companion.CompanionWindow(
                    root,
                    edition=self.companion.HOSTED_EDITION,
                    preview=True,
                )
            labels = {
                button.cget("text")
                for button in window.managed_cards["guild"]["buttons"]
            }
            self.assertIn("Open console", labels)
        finally:
            root.destroy()


    def test_managed_voice_rows_keep_the_single_caption_pause(self):
        import tkinter as tk

        for edition in (
            self.companion.MEMBER_EDITION,
        ):
            with self.subTest(edition=edition):
                root = tk.Tk()
                root.withdraw()
                try:
                    window = self.companion.CompanionWindow(
                        root,
                        edition=edition,
                        preview=True,
                    )
                    voice = next(
                        service
                        for service in window.services
                        if service.spec.key == "voice"
                    )
                    self.assertIsNone(voice.lifecycle_badge)
                    self.assertIsNone(voice.start_button)
                    self.assertIsNone(voice.stop_button)
                    self.assertEqual(
                        "Pause caption feed",
                        window.member_caption_button.cget("text"),
                    )
                finally:
                    root.destroy()

    def test_managed_editions_use_task_focused_standard_cards(self):
        import tkinter as tk

        # Connect guild left the card for the header on 2026-09-04
        # (Shino: "bring out the Connect guild button"); the card keeps
        # consent, and the console on a guild host.
        expected_buttons = {
            self.companion.MEMBER_EDITION: {
                "display": {"Move captions", "Hide captions", "Tanks + healers"},
                "message": {"Message Group"},
                "guild": {"My voice consent"},
            },
        }
        for edition, expected in expected_buttons.items():
            with self.subTest(edition=edition):
                root = tk.Tk()
                root.withdraw()
                try:
                    with mock.patch.object(
                        self.companion,
                        "operator_console_available",
                        return_value=False,
                    ):
                        window = self.companion.CompanionWindow(
                            root,
                            edition=edition,
                            preview=True,
                        )
                    root.update_idletasks()
                    self.assertEqual(
                        ["captions", "display", "message", "guild"],
                        window.managed_card_order,
                    )
                    self.assertEqual(
                        {"display", "message", "guild"},
                        set(window.managed_cards),
                    )
                    self.assertIs(
                        window.hero_chat_button.master,
                        window.managed_cards["message"]["controls"],
                    )
                    self.assertIs(
                        window.member_guild_label,
                        window.managed_cards["guild"]["status"],
                    )
                    self.assertEqual(
                        ["My voice consent"],
                        [
                            button.cget("text")
                            for button in window.managed_cards["guild"]["buttons"]
                        ],
                    )
                    # The emphasis moved up with Connect guild: it is the
                    # accented button under the guild name in the header,
                    # and nothing on the card competes with it.
                    guild_buttons = window.managed_cards["guild"]["buttons"]
                    # CompactButton expresses accent through is_accent and
                    # its foreground, not its background, so ask the flag.
                    accented = [
                        button.cget("text") for button in guild_buttons
                        if getattr(button, "is_accent", False)
                    ]
                    self.assertEqual([], accented)
                    self.assertIsNotNone(window.hero_connect_button)
                    self.assertTrue(window.hero_connect_button.is_accent)
                    # A preview is connected to a stand-in guild, so the
                    # button already offers the switch.
                    self.assertEqual("Change guild", window.hero_connect_button.cget("text"))
                    self.assertEqual(
                        window.theme.conversation,
                        window.managed_cards["message"]["rail"].cget("bg"),
                    )
                    self.assertEqual(
                        window.accent,
                        window.managed_cards["display"]["rail"].cget("bg"),
                    )
                    self.assertIsInstance(
                        window.managed_cards["display"]["shell"],
                        self.companion.CardShell,
                    )
                    self.assertIsInstance(
                        window.service_section,
                        self.companion.SectionHeader,
                    )
                    self.assertIsInstance(
                        window.service_signal,
                        self.companion.SignalBar,
                    )
                    self.assertEqual(window.theme.accent, window.accent)
                    self.assertEqual(
                        window.theme.badge_label,
                        window.hero_edition_badge.cget("text"),
                    )
                    self.assertEqual(
                        window.accent,
                        window.hero_edition_badge.cget("fg"),
                    )
                    for key, labels in expected.items():
                        expected_labels = set(labels)
                        if key == "guild" and window.has_operator_console:
                            expected_labels.add("Open console")
                        buttons = window.managed_cards[key]["buttons"]
                        self.assertEqual(
                            expected_labels,
                            {button.cget("text") for button in buttons},
                        )
                        self.assertNotIn(
                            "Choose your class",
                            {button.cget("text") for button in buttons},
                        )
                        card = window.managed_cards[key]["card"]
                        rail = window.managed_cards[key]["rail"]
                        self.assertIs(card, rail.master)
                        self.assertEqual(
                            card.winfo_width(),
                            rail.winfo_x() + rail.winfo_width(),
                        )
                finally:
                    root.destroy()





    def test_member_icon_ignores_the_legacy_clock_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            stale = Path(folder) / "vemals-ears.ico"
            stale.write_bytes(b"old clock cache")
            with mock.patch.object(self.companion, "MEMBER_ICON_FILE", stale):
                result = self.companion.ensure_icon(self.companion.MEMBER_EDITION)
            self.assertEqual(ROOT / "Windows/VemalsEars.ico", result)

    def test_recreated_windows_receive_their_own_native_relaunch_path(self):
        from GuildHub import windows_taskbar

        for member in (True, False):
            window = object.__new__(self.companion.CompanionWindow)
            window.root = object()
            window.preview = False
            window.member_edition = member
            window.product_name = "Vemal's Ears" if member else "Downtime"
            launcher = Path("C:/Apps/VemalsEars.exe" if member else "C:/Apps/Downtime.exe")
            with mock.patch.object(self.companion, "window_handle", side_effect=[101, 101, 202]), \
                    mock.patch.object(self.companion, "installed_launcher", return_value=launcher), \
                    mock.patch.object(self.companion, "full_launcher", return_value=launcher), \
                    mock.patch.object(windows_taskbar, "apply_taskbar_identity", return_value=True) as apply:
                self.assertEqual([101, 101, 202], [window.handle() for _ in range(3)])
            self.assertEqual([101, 202], [call.args[0] for call in apply.call_args_list])
            self.assertEqual(launcher, apply.call_args.kwargs["launcher"])
            self.assertEqual(member, apply.call_args.kwargs["member"])

    def test_preview_never_sets_taskbar_properties(self):
        from GuildHub import windows_taskbar

        window = object.__new__(self.companion.CompanionWindow)
        window.root = object()
        window.preview = True
        with mock.patch.object(self.companion, "window_handle", return_value=101), \
                mock.patch.object(windows_taskbar, "apply_taskbar_identity") as apply:
            window.handle()
        apply.assert_not_called()

    def test_windows_process_id_matches_native_launcher_id(self):
        setter = mock.Mock(return_value=0)
        with mock.patch.object(self.companion.ctypes, "WinDLL", return_value=
                               SimpleNamespace(SetCurrentProcessExplicitAppUserModelID=setter)):
            self.assertTrue(self.companion.set_windows_app_identity(True))
            self.assertTrue(self.companion.set_windows_app_identity(False))
        self.assertEqual(["VemalsEars.Companion", "Downtime.Companion"],
                         [call.args[0] for call in setter.call_args_list])

    def test_preview_surfaces_never_cross_into_live_state(self):
        import tkinter as tk
        from tkinter import messagebox

        from VoiceCaptions import settings as caption_settings

        forbidden = []
        with ExitStack() as stack:
            for owner, name in (
                (self.companion, "ensure_icon"),
                (self.companion, "load_settings"),
                (self.companion, "save_settings"),
                (self.companion, "paired_guild_name"),
                (self.companion, "prepare_discord_consent"),
                (self.companion, "prepare_discord_class_picker"),
                (self.companion, "open_support_url"),
                (self.companion, "transmit_quick_chat"),
                (self.companion, "autostart_enabled"),
                (self.companion, "set_autostart"),
                (self.companion.subprocess, "Popen"),
                (self.companion.TrayIcon, "start"),
                (caption_settings, "load_settings"),
                (caption_settings, "save_settings"),
            ):
                guard = stack.enter_context(
                    mock.patch.object(
                        owner,
                        name,
                        side_effect=AssertionError("preview called " + name),
                    )
                )
                forbidden.append(guard)
            stack.enter_context(mock.patch.object(messagebox, "showinfo"))

            for edition in (
                self.companion.MEMBER_EDITION,
                self.companion.HOSTED_EDITION,
                self.companion.FULL_EDITION,
            ):
                root = tk.Tk()
                root.withdraw()
                try:
                    window = self.companion.CompanionWindow(
                        root,
                        edition=edition,
                        preview=True,
                    )
                    root.update()
                    for service in window.services:
                        service.start()
                        service.run_action(service.spec.action_script)
                    window.open_discord_consent()
                    window.open_discord_class_picker()
                    window.open_support_link(
                        self.companion.PATREON_CUSTOM_PLEDGE_URL,
                        "Patreon",
                    )
                    window.open_support_link(
                        self.companion.PAYPAL_DONATE_URL,
                        "PayPal",
                    )
                    window.open_settings()
                    if window.caption_suite:
                        window.open_caption_target_picker()
                        window.caption_target_menu.dismiss()
                    window.menu.on_autostart(True)
                    window.menu.on_minimize(False)
                    window.set_caption_hold_seconds(7.0)
                    window.set_max_visible_speakers(5)
                    window.set_caption_font_size(15)
                    if window.caption_suite:
                        window.menu.begin_hotkey_capture()
                        window.menu.cancel_hotkey_capture()
                        window.set_speak_hotkey("Ctrl+Shift+9")
                        window.open_speak_box()
                        window.speak_entry.insert(0, "Stack on Vemal")
                        window.send_spoken_text()
                    window.quit_now()
                finally:
                    try:
                        root.destroy()
                    except tk.TclError:
                        pass

        for guard in forbidden:
            guard.assert_not_called()

    def test_activity_is_scrollable_without_starving_its_log_or_footer(self):
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        try:
            window = self.companion.CompanionWindow(
                root,
                edition=self.companion.HOSTED_EDITION,
                preview=True,
            )
            width, height = window.minimum_size
            root.geometry(f"{width}x{height}+80+80")
            root.deiconify()
            root.update()
            window.toggle_activity()
            root.update()

            self.assertTrue(window.activity_panel.winfo_ismapped())
            self.assertTrue(window.footer.winfo_ismapped())
            self.assertGreaterEqual(window.log.winfo_height(), 120)
            self.assertGreater(window.main_surface.canvas.yview()[0], 0.0)

            window.main_surface.show_bottom()
            root.update()
            canvas_bottom = (
                window.main_surface.canvas.winfo_rooty()
                + window.main_surface.canvas.winfo_height()
            )
            footer_bottom = window.footer.winfo_rooty() + window.footer.winfo_height()
            self.assertLessEqual(footer_bottom, canvas_bottom + 2)
        finally:
            root.destroy()

    def scroll_focus_fixture(self):
        import tkinter as tk

        root = tk.Tk()
        self.addCleanup(root.destroy)
        root.withdraw()
        root.attributes("-alpha", 0.0)
        root.geometry("400x260+-15000+-15000")
        outside = tk.Button(root, text="Outside")
        outside.pack()
        surface = self.companion.ScrollSurface(root, background="#16181d")
        surface.pack(fill="both", expand=True)
        invoked = []
        first = self.companion.VelvetButton(
            surface.content, "Visible action", lambda: invoked.append(1),
            theme=self.companion.get_theme_for_edition(self.companion.MEMBER_EDITION),
        )
        first.pack(pady=(60, 0))
        tk.Frame(surface.content, height=400).pack()
        last = tk.Button(surface.content, text="Offscreen action")
        last.pack()
        root.deiconify()
        root.update()
        outside.focus_force()
        root.update()
        return root, surface, outside, first, last, invoked

    def test_visible_button_first_click_keeps_viewport_and_invokes_once(self):
        root, surface, _outside, first, _last, invoked = self.scroll_focus_fixture()
        before_view = surface.canvas.yview()
        before_y = first.winfo_rooty()
        pointer_x = first.winfo_width() // 2
        pointer_y = before_y + first.winfo_height() // 2
        first.event_generate("<Enter>", x=pointer_x, y=first.winfo_height() // 2)
        first.event_generate("<ButtonPress-1>", x=pointer_x, y=first.winfo_height() // 2)
        # FocusIn also arrives for the content ancestor. It must not scroll
        # that oversized frame under the stationary pointer between clicks.
        root.update()
        release_y = pointer_y - first.winfo_rooty()
        if not 0 <= release_y < first.winfo_height():
            first.event_generate("<Leave>", x=pointer_x, y=release_y)
        first.event_generate("<ButtonRelease-1>", x=pointer_x, y=release_y)
        root.update()

        self.assertEqual(before_view, surface.canvas.yview())
        self.assertEqual(before_y, first.winfo_rooty())
        self.assertIs(first, root.focus_get())
        self.assertEqual([1], invoked)

    def test_keyboard_tab_still_reveals_the_offscreen_focused_button(self):
        root, surface, _outside, first, last, _invoked = self.scroll_focus_fixture()
        first.focus_force()
        root.update()
        viewport_bottom = surface.canvas.winfo_rooty() + surface.canvas.winfo_height()
        self.assertGreater(last.winfo_rooty(), viewport_bottom)

        first.event_generate("<Tab>")
        root.update()

        self.assertIs(last, root.focus_get())
        self.assertGreater(surface.canvas.yview()[0], 0.0)
        self.assertGreaterEqual(last.winfo_rooty(), surface.canvas.winfo_rooty())
        self.assertLessEqual(last.winfo_rooty() + last.winfo_height(), viewport_bottom)

    def test_queued_reveal_does_not_scroll_after_focus_leaves_the_surface(self):
        root, surface, outside, _first, last, _invoked = self.scroll_focus_fixture()
        pending = []
        before_view = surface.canvas.yview()
        with mock.patch.object(surface, "after_idle", side_effect=pending.append):
            last.focus_force()
            root.update()
            self.assertTrue(pending, "The focused leaf must schedule a reveal")
            outside.focus_force()
            root.update()
        for callback in pending:
            callback()
        root.update()

        self.assertIs(outside, root.focus_get())
        self.assertEqual(before_view, surface.canvas.yview())

    def test_long_quick_chat_error_keeps_the_compact_popup_in_place(self):
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        try:
            window = self.companion.CompanionWindow(
                root,
                edition=self.companion.MEMBER_EDITION,
                preview=True,
            )
            root.deiconify()
            root.update()
            window.open_speak_box()
            root.update()
            before = (window.speak_box.winfo_width(), window.speak_box.winfo_height())
            window.speak_status.config(
                text=(
                    "The guild message could not be delivered. Check your guild "
                    "connection, then try again. Your draft is safe."
                )
            )
            root.update()
            after = (window.speak_box.winfo_width(), window.speak_box.winfo_height())
            self.assertEqual(before, after)
            self.assertGreaterEqual(window.speak_box.winfo_rootx(), root.winfo_rootx())
            self.assertLessEqual(
                window.speak_box.winfo_rootx() + after[0],
                root.winfo_rootx() + root.winfo_width(),
            )
        finally:
            root.destroy()

    def test_managed_minimum_width_keeps_every_voice_action_visible(self):
        import tkinter as tk

        for edition in (
            self.companion.MEMBER_EDITION,
            self.companion.HOSTED_EDITION,
        ):
            with self.subTest(edition=edition):
                root = tk.Tk()
                root.withdraw()
                try:
                    window = self.companion.CompanionWindow(
                        root,
                        edition=edition,
                        preview=True,
                    )
                    width, height = window.minimum_size
                    root.geometry(f"{width}x{height}+80+80")
                    root.deiconify()
                    root.update()
                    for details in window.managed_cards.values():
                        for button in details["buttons"]:
                            self.assertGreaterEqual(
                                button.winfo_width(),
                                button.winfo_reqwidth(),
                            )
                            self.assertEqual(1, button.cget("takefocus"))
                finally:
                    root.destroy()

    def test_job_status_badges_replace_request_actions(self):
        import tkinter as tk

        cases = (
            (self.companion.FULL_EDITION, "Downtime", 2),
            (self.companion.HOSTED_EDITION, "Downtime Lite", 2),
            (self.companion.MEMBER_EDITION, "Vemal's Ears", 0),
        )
        for edition, product_name, expected in cases:
            with self.subTest(edition=edition):
                root = tk.Tk()
                root.withdraw()
                try:
                    window = self.companion.CompanionWindow(
                        root,
                        edition=edition,
                        preview=True,
                    )
                    root.update()
                    button_labels = []

                    def walk(widget):
                        for child in widget.winfo_children():
                            if isinstance(child, tk.Button):
                                button_labels.append(child.cget("text"))
                            walk(child)

                    walk(window.main)
                    badges = [
                        service.result_badge
                        for service in window.services
                        if service.result_badge is not None
                    ]
                    self.assertEqual(product_name, window.product_name)
                    self.assertNotIn("Request", button_labels)
                    self.assertEqual(expected, len(badges))
                    for badge in badges:
                        self.assertIsInstance(badge, tk.Label)
                        self.assertEqual("Ready", badge.cget("text"))
                        self.assertEqual(15, int(badge.cget("width")))
                finally:
                    root.destroy()

    def test_apps_do_not_install_server_voice_tools(self):
        import tkinter as tk

        # Server voice dependencies are managed outside the player apps.
        cases = (
            (self.companion.FULL_EDITION, 0, 0),
            (self.companion.HOSTED_EDITION, 0, 0),
            (self.companion.MEMBER_EDITION, 0, 0),
        )
        for edition, on_main, on_captions in cases:
            with self.subTest(edition=edition):
                root = tk.Tk()
                root.withdraw()
                try:
                    window = self.companion.CompanionWindow(
                        root,
                        edition=edition,
                        preview=True,
                    )
                    root.update()

                    def labels_under(widget, found=None):
                        found = [] if found is None else found
                        if widget is None:
                            return found
                        for child in widget.winfo_children():
                            if isinstance(child, tk.Button):
                                found.append(child.cget("text"))
                            labels_under(child, found)
                        return found

                    self.assertEqual(
                        on_main,
                        labels_under(window.main).count("Install voice tools"),
                    )
                    self.assertEqual(
                        on_captions,
                        labels_under(window.captions_page_frame).count(
                            "Install voice tools"
                        ),
                    )
                finally:
                    root.destroy()

    def test_job_status_badge_styles_are_textual_and_semantic(self):
        expected = {
            "ready": ("Ready", self.companion.ACCENT),
            "queued": ("Queued", self.companion.LITE_ACCENT),
            "checking": ("Checking", self.companion.WARNING),
            "done": ("Done", self.companion.SUCCESS),
            "stopped": ("Stopped", self.companion.MUTED),
            "attention": ("Needs attention", self.companion.DANGER),
        }
        for state, style in expected.items():
            with self.subTest(state=state):
                self.assertEqual(style, self.companion.job_status_badge(state))
        with self.assertRaisesRegex(ValueError, "unknown job status"):
            self.companion.job_status_badge("mystery")

    def test_done_badge_lasts_without_claiming_the_result_was_imported(self):
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        try:
            window = self.companion.CompanionWindow(
                root,
                edition=self.companion.HOSTED_EDITION,
                preview=True,
            )
            gear = next(
                service for service in window.services if service.spec.key == "gear"
            )
            gear.set_status("Queued on the server", self.companion.ACCENT, "queued")
            self.assertEqual("Queued", gear.result_badge.cget("text"))
            gear.set_status("Running on the server", self.companion.WARNING, "checking")
            self.assertEqual("Checking", gear.result_badge.cget("text"))
            gear.set_status(
                "Results ready - choose Load Results",
                self.companion.SUCCESS,
                "done",
            )
            self.assertEqual("Done", gear.result_badge.cget("text"))
            gear.set_status(
                "[########] 1/1 Result written for WoW.",
                self.companion.WARNING,
                "checking",
            )
            self.assertEqual("Done", gear.result_badge.cget("text"))
            # "Done" means a result is on disk ready for an explicit action
            # in WoW, so another request queuing does not end it.
            # A member with a gear sim finished and a vault request
            # still queued used to see the badge flip to "Queued"
            # five seconds later, was never told to reload, and the
            # finished result sat unimported.
            gear.set_status("Queued on the server", self.companion.ACCENT, "queued")
            self.assertEqual("Done", gear.result_badge.cget("text"))
            # A log line cannot prove a player actually loaded the result.
            # Keep Done rather than fabricating an import state.
            gear.set_status("Result imported", self.companion.ACCENT, "ready")
            self.assertEqual("Done", gear.result_badge.cget("text"))
            gear.set_status("Queued on the server", self.companion.ACCENT, "queued")
            self.assertEqual("Done", gear.result_badge.cget("text"))
        finally:
            root.destroy()

    def test_local_watchers_reach_checking_done_and_attention(self):
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        try:
            window = self.companion.CompanionWindow(
                root,
                edition=self.companion.FULL_EDITION,
                preview=True,
            )
            by_key = {service.spec.key: service for service in window.services}
            talents = by_key["talents"]
            talent_process = object()
            talents.process = talent_process
            talents.lines.put((talent_process, "Fetching BT-1 for class 2...\n"))
            talents.drain()
            self.assertEqual("Checking", talents.result_badge.cget("text"))
            talents.lines.put((talent_process, "Ready: 8/8 encounters.\n"))
            talents.drain()
            self.assertEqual("Done", talents.result_badge.cget("text"))
            talents.lines.put(
                (
                    talent_process,
                    "BossTalents result loaded by WoW. Ready for another request.\n",
                )
            )
            talents.drain()
            self.assertEqual("Done", talents.result_badge.cget("text"))
            talents.lines.put(
                (talent_process, "Unexpected error: response could not be parsed.\n")
            )
            talents.drain()
            self.assertEqual(
                "Needs attention", talents.result_badge.cget("text")
            )

            gear = by_key["gear"]
            gear_process = object()
            gear.process = gear_process
            gear.lines.put(
                (
                    gear_process,
                    "Gear Helper request queued: next sim can start in 3 minutes.\n",
                )
            )
            gear.drain()
            self.assertEqual("Queued", gear.result_badge.cget("text"))
            gear.lines.put(
                (
                    gear_process,
                    "Gear Helper queue cleared. Watching for a sim request.\n",
                )
            )
            gear.drain()
            self.assertEqual("Ready", gear.result_badge.cget("text"))
            gear.lines.put((gear_process, "Starting Gear Helper request GH-1.\n"))
            gear.drain()
            self.assertEqual("Checking", gear.result_badge.cget("text"))
            gear.lines.put(
                (gear_process, "Great Vault comparison completed in 42.0 seconds.\n")
            )
            gear.drain()
            self.assertEqual("Done", gear.result_badge.cget("text"))
            gear.lines.put(
                (
                    gear_process,
                    "Gear Helper result loaded by WoW. Ready for another request.\n",
                )
            )
            gear.drain()
            self.assertEqual("Done", gear.result_badge.cget("text"))
            gear.lines.put((gear_process, "Gear Helper: request could not run.\n"))
            gear.drain()
            self.assertEqual("Needs attention", gear.result_badge.cget("text"))
        finally:
            root.destroy()

    def test_preview_status_surface_does_not_mutate_live_state(self):
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        try:
            window = self.companion.CompanionWindow(
                root,
                edition=self.companion.HOSTED_EDITION,
                preview=True,
            )
            for service in window.services:
                if service.spec.key == "gear":
                    service.set_status("Done", self.companion.SUCCESS, "done")
                elif service.spec.key == "talents":
                    service.set_status("Checking", self.companion.WARNING, "checking")
            root.update()
            states = {
                service.spec.key: service.result_badge.cget("text")
                for service in window.services
                if service.result_badge is not None
            }
            self.assertEqual({"gear": "Done", "talents": "Checking"}, states)
        finally:
            root.destroy()

    def test_settings_uses_native_named_checkboxes_and_parent_stacking(self):
        import tkinter as tk

        self.assertTrue(issubclass(self.companion.Toggle, tk.Checkbutton))
        root = tk.Tk()
        root.withdraw()
        try:
            window = self.companion.CompanionWindow(
                root,
                edition=self.companion.HOSTED_EDITION,
                preview=True,
            )
            root.deiconify()
            root.update()
            window.open_settings()
            root.update()
            if sys.platform != "win32":
                self.assertEqual(str(root), str(window.menu.transient()))
            self.assertFalse(bool(window.menu.attributes("-topmost")))
            if sys.platform == "win32":
                import ctypes
                from ctypes import wintypes

                child_handle = self.companion.window_handle(window.menu)
                owner_handle = self.companion.window_handle(root)
                get_window = ctypes.windll.user32.GetWindow
                get_window.argtypes = [wintypes.HWND, ctypes.c_uint]
                get_window.restype = wintypes.HWND
                self.assertEqual(
                    owner_handle,
                    int(get_window(child_handle, 4) or 0),
                    "Preferences must be a real Win32-owned popup",
                )
                get_style = ctypes.windll.user32.GetWindowLongPtrW
                get_style.argtypes = [wintypes.HWND, ctypes.c_int]
                get_style.restype = ctypes.c_ssize_t
                style = get_style(child_handle, -16)  # GWL_STYLE
                self.assertTrue(style & 0x80000000, "WS_POPUP is required")
                self.assertFalse(style & 0x40000000, "WS_CHILD breaks popup activation")
                ex_style = get_style(child_handle, -20)  # GWL_EXSTYLE
                self.assertFalse(ex_style & 0x00040000, "No separate taskbar button")
                self.assertFalse(ex_style & 0x00000008, "No system-wide topmost")
            labels = []

            def walk(widget):
                for child in widget.winfo_children():
                    if isinstance(child, tk.Checkbutton):
                        labels.append(child.cget("text"))
                    walk(child)

            walk(window.menu)
            self.assertIn("Open at sign-in", labels)
            self.assertIn("Keep running when closed", labels)
        finally:
            root.destroy()

    @unittest.skipUnless(sys.platform == "win32", "Windows popup styles")
    def test_popup_ownership_replaces_transient_child_without_moving_controls(self):
        import ctypes
        import tkinter as tk
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        get_style = user32.GetWindowLongPtrW
        get_style.argtypes = [wintypes.HWND, ctypes.c_int]
        get_style.restype = ctypes.c_ssize_t
        get_window = user32.GetWindow
        get_window.argtypes = [wintypes.HWND, ctypes.c_uint]
        get_window.restype = wintypes.HWND
        root = tk.Tk()
        root.withdraw()
        root.attributes("-alpha", 0.0)
        root.overrideredirect(True)
        root.geometry("400x200+-16000+-16000")
        try:
            root.deiconify()
            root.update()
            popup = tk.Toplevel(root)
            popup.withdraw()
            popup.attributes("-alpha", 0.0)
            popup.overrideredirect(True)
            popup.transient(root)
            invoked = []
            button = tk.Button(popup, text="Counter", command=lambda: invoked.append(1))
            button.pack()
            popup.geometry("300x110+-15000+-15000")
            popup.deiconify()
            root.update()
            before_handle = self.companion.window_handle(popup)
            before_geometry = popup.geometry()
            button.focus_set()
            before_focus = popup.tk.call("focus", "-lastfor", popup._w)
            # This is the real Tk/Windows shape that the old GW_OWNER-only
            # assertion accepted: a desktop child, not an owned top level.
            self.assertTrue(get_style(before_handle, -16) & 0x40000000)

            self.assertTrue(self.companion.own_popup_window(popup, root))
            root.update()
            after_handle = self.companion.window_handle(popup)
            style = get_style(after_handle, -16)
            self.assertTrue(style & 0x80000000, "A popup must use WS_POPUP")
            self.assertFalse(style & 0x40000000, "A popup must not use WS_CHILD")
            self.assertNotEqual(before_handle, after_handle, "Use Tk's new wrapper")
            self.assertEqual(
                self.companion.window_handle(root), int(get_window(after_handle, 4) or 0)
            )
            self.assertEqual(before_geometry, popup.geometry())
            self.assertEqual(before_focus, popup.tk.call("focus", "-lastfor", popup._w))
            self.assertFalse(get_style(after_handle, -20) & (0x00040000 | 0x00000008))
            # Production fades after assigning the owner; preview skips it.
            with mock.patch.object(self.companion, "animations_enabled", return_value=True), \
                    mock.patch.object(popup, "after", side_effect=lambda _delay, frame: frame()):
                self.companion.fade_in(popup)
            self.assertEqual(after_handle, self.companion.window_handle(popup))
            self.assertEqual(before_geometry, popup.geometry())
            self.assertEqual(
                self.companion.window_handle(root), int(get_window(after_handle, 4) or 0)
            )
            self.assertFalse(get_style(after_handle, -16) & 0x40000000)
            self.assertTrue(button.winfo_exists())
            button.invoke()
            self.assertEqual([1], invoked)
        finally:
            root.destroy()

    def test_subtle_text_meets_normal_text_contrast(self):
        def luminance(hex_color):
            channels = [
                int(hex_color[index:index + 2], 16) / 255.0
                for index in (1, 3, 5)
            ]
            linear = [
                value / 12.92
                if value <= 0.04045
                else ((value + 0.055) / 1.055) ** 2.4
                for value in channels
            ]
            return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

        for background in (self.companion.PANEL, self.companion.PANEL_ALT):
            foreground_luminance = luminance(self.companion.SUBTLE)
            background_luminance = luminance(background)
            ratio = (foreground_luminance + 0.05) / (background_luminance + 0.05)
            self.assertGreaterEqual(ratio, 4.5)

    def test_escape_closes_quick_chat_from_the_send_button(self):
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        try:
            window = self.companion.CompanionWindow(
                root,
                edition=self.companion.MEMBER_EDITION,
                preview=True,
            )
            root.deiconify()
            root.update()
            window.open_speak_box()
            root.update()
            window.speak_send.focus_force()
            window.speak_send.event_generate("<Escape>")
            root.update()
            self.assertIsNone(window.speak_box)
        finally:
            root.destroy()

    def test_ui_keeps_logs_collapsed_and_human_communication_warm(self):
        source = (ROOT / "WoW-Companion.pyw").read_text(encoding="utf-8")
        self.assertIn("self.activity_expanded = False", source)
        self.assertIn("def make_chat_button", source)
        self.assertIn("bg=self.theme.conversation", source)
        self.assertIn('if window.caption_suite:', source)
        self.assertIn("self.hotkey_row(chat_card)", source)
        self.assertNotIn("Quick chat: ", source)



class AppFeatureUiTests(unittest.TestCase):
    """The two products expose distinct capabilities in real Tk widgets."""

    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    def build(self, edition="full-local"):
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        try:
            window = self.companion.CompanionWindow(root, edition=edition, preview=True)
        except Exception:
            root.destroy()
            raise
        return root, window

    def labels(self, parent):
        found = []
        for child in parent.winfo_children():
            try:
                if "text" in child.keys():
                    found.append(str(child.cget("text")))
            except Exception:
                pass
            found.extend(self.labels(child))
        return found

    def test_downtime_tools_have_no_caption_tab_controls_or_shortcut(self):
        for edition in ("full-local", "downtime-hosted"):
            with self.subTest(edition=edition):
                root, window = self.build(edition)
                try:
                    self.assertFalse(window.caption_suite)
                    self.assertFalse(window.captions_page)
                    self.assertIsNone(window.tab_bar)
                    self.assertIsNone(window.captions_surface)
                    self.assertIsNone(window.tray.hotkey)
                    self.assertFalse(window.tray.hotkey_registered)
                    self.assertEqual({"guild"}, set(window.managed_cards))
                    self.assertEqual(
                        {"gear", "talents"},
                        {service.spec.key for service in window.services},
                    )
                    labels = self.labels(root)
                    for forbidden in (
                        "Live captions", "Message Group", "My voice consent",
                        "Caption display", "Muted voices", "REC", "Install voice tools",
                    ):
                        self.assertNotIn(forbidden, labels)
                    window.open_speak_box()
                    self.assertIsNone(getattr(window, "speak_box", None))
                    window.open_settings()
                    labels = self.labels(window.menu)
                    for forbidden in ("Captions", "Message Group", "Caption duration"):
                        self.assertNotIn(forbidden, labels)
                    self.assertIsNone(window.menu.message_name_entry)
                    window.close_menu()
                    window.open_connect_guild()
                    self.assertIsNone(window.connect_guild_helper.consent_button)
                    self.assertIsNone(window.connect_guild_helper.privacy_link)
                    self.assertNotIn("My voice consent", self.labels(window.connect_guild_helper))
                finally:
                    root.destroy()

    def test_ve_keeps_caption_controls_and_message_preferences(self):
        root, window = self.build("member")
        try:
            self.assertTrue(window.caption_suite)
            self.assertFalse(window.captions_page)
            self.assertIsNone(window.tab_bar)
            self.assertEqual(
                {"display", "message", "guild"}, set(window.managed_cards),
            )
            self.assertEqual(["voice"], [service.spec.key for service in window.services])
            self.assertIsNotNone(window.member_caption_button)
            self.assertIsNotNone(window.speaker_mute_button)
            self.assertIsNotNone(window.caption_target_button)
            self.assertTrue(window.tray.hotkey_registered)
            labels = self.labels(root)
            self.assertEqual(1, labels.count("Muted voices"))
            self.assertIn("Message Group", labels)
            self.assertIn("My voice consent", labels)
            window.open_settings()
            labels = self.labels(window.menu)
            self.assertIn("Captions", labels)
            self.assertIn("Message Group", labels)
            self.assertIsNotNone(window.menu.message_name_entry)
        finally:
            root.destroy()

    def test_each_app_keeps_its_palette_when_both_windows_exist(self):
        import tkinter as tk

        root, downtime = self.build("full-local")
        other = tk.Toplevel(root)
        try:
            ears = self.companion.CompanionWindow(
                other, edition="member", preview=True,
            )
            self.assertNotEqual(downtime.theme.bg, ears.theme.bg)
            self.assertNotEqual(downtime.theme.accent, ears.theme.accent)
            self.assertEqual(downtime.theme.bg, root.cget("bg"))
            self.assertEqual(ears.theme.bg, other.cget("bg"))
            self.assertEqual("(By Downtime)", ears.brand_byline.cget("text"))
            self.assertIsNone(downtime.brand_byline)
            self.assertLess(
                ears.fonts["tiny"].cget("size"),
                ears.fonts["hero"].cget("size"),
            )
            ears.hero_chat_button.event_generate("<Enter>")
            root.update_idletasks()
            ears.hero_chat_button.event_generate("<Leave>")
            root.update_idletasks()
            self.assertEqual(ears.theme.conversation, ears.hero_chat_button.cget("bg"))
            self.assertEqual(downtime.theme.bg, root.cget("bg"))
        finally:
            root.destroy()

    def test_downtime_keeps_operator_console_without_voice_controls(self):
        for available in (True, False):
            with self.subTest(available=available):
                with mock.patch.object(
                    self.companion, "operator_console_available", return_value=available,
                ):
                    root, window = self.build()
                try:
                    labels = self.labels(root)
                    self.assertEqual(available, "Open console" in labels)
                    self.assertNotIn("My voice consent", labels)
                    self.assertNotIn("REC", labels)
                    if available:
                        with mock.patch.object(
                            self.companion, "launch_operator_console", return_value=True,
                        ) as opened:
                            window.open_operator_console()
                        opened.assert_called_once_with(member_edition=False)
                finally:
                    root.destroy()


class LicenseSourcePreferencesTests(unittest.TestCase):
    """A member can read their rights and obtain source without an account."""

    def setUp(self):
        import tkinter as tk

        self.companion = self.enterContext(isolated_companion("member"))
        original_init = tk.Toplevel.__init__

        def hidden_toplevel(widget, *args, **kwargs):
            original_init(widget, *args, **kwargs)
            widget.withdraw()
            widget.attributes("-alpha", 0.0)

        self.enterContext(mock.patch.object(tk.Toplevel, "__init__", hidden_toplevel))
        self.enterContext(mock.patch.object(tk.Toplevel, "focus_force"))
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest("Native Tk is unavailable: " + str(error))
        self.root.withdraw()
        self.root.attributes("-alpha", 0.0)
        self.addCleanup(self.close_preview)

    def close_preview(self):
        for callback in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(callback)
        self.root.destroy()

    def preferences(self, edition="member"):
        window = self.companion.CompanionWindow(self.root, edition=edition, preview=True)
        window.open_settings()
        self.root.update_idletasks()
        return window, window.menu

    def test_member_can_read_rights_and_open_each_matching_local_document(self):
        window, menu = self.preferences()
        notice = menu.license_notice.cget("text")
        self.assertIn("GPL-3.0-only", notice)
        self.assertIn("Copyright (C) 2026 Zackery Hodges", notice)
        self.assertIn("modify and redistribute", notice)
        self.assertIn("WITHOUT ANY WARRANTY", notice)
        self.assertEqual(window.theme.panel_alt, menu.license_notice.cget("bg"))
        with tempfile.TemporaryDirectory() as folder:
            local_root = Path(folder)
            with mock.patch.object(self.companion, "ROOT", local_root), mock.patch.object(
                self.companion.os, "startfile", create=True,
            ) as opened, mock.patch.object(self.companion.webbrowser, "open") as browser:
                for label, name in (
                    ("License", "LICENSE"),
                    ("Source guide", "docs/SOURCE.txt"),
                    ("Source archive", "Source/vemals-ears-source.zip"),
                ):
                    with self.subTest(document=name):
                        target = local_root / name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(b"local document fixture")
                        menu.license_buttons[label].invoke()
                        opened.assert_called_with(str(target))
                        self.assertIn("Opened " + name, menu.license_status.cget("text"))
                self.assertEqual(3, opened.call_count)
                browser.assert_not_called()

    def test_missing_source_archive_explains_how_to_obtain_it(self):
        _, menu = self.preferences()
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
            self.companion, "ROOT", Path(folder),
        ), mock.patch.object(self.companion.os, "startfile", create=True) as opened:
            menu.license_buttons["Source archive"].invoke()
        opened.assert_not_called()
        detail = menu.license_status.cget("text")
        self.assertIn("not present", detail)
        self.assertIn("distributed member ZIPs and installers", detail)
        self.assertIn("Source guide", detail)
        self.assertEqual(self.companion.DANGER, menu.license_status.cget("fg"))
        self.assertTrue(menu.license_status.winfo_manager())

    def test_missing_license_and_guide_are_reported_without_starting_an_opener(self):
        _, menu = self.preferences()
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
            self.companion, "ROOT", Path(folder),
        ), mock.patch.object(self.companion.os, "startfile", create=True) as opened:
            for label, name in (("License", "LICENSE"), ("Source guide", "docs/SOURCE.txt")):
                with self.subTest(document=name):
                    menu.license_buttons[label].invoke()
                    self.assertIn(name + " is missing", menu.license_status.cget("text"))
                    self.assertEqual(self.companion.DANGER, menu.license_status.cget("fg"))
        opened.assert_not_called()

    def test_open_failure_is_visible_and_a_successful_retry_clears_it(self):
        window, menu = self.preferences()
        with tempfile.TemporaryDirectory() as folder:
            local_root = Path(folder)
            (local_root / "LICENSE").write_text("local license fixture", encoding="ascii")
            with mock.patch.object(self.companion, "ROOT", local_root), mock.patch.object(
                self.companion.os, "startfile", create=True,
                side_effect=[OSError("no document association"), None],
            ):
                menu.license_buttons["License"].invoke()
                self.assertIn("Could not open LICENSE", menu.license_status.cget("text"))
                self.assertIn("no document association", menu.license_status.cget("text"))
                self.assertEqual(self.companion.DANGER, menu.license_status.cget("fg"))
                menu.license_buttons["License"].invoke()
                self.assertEqual("Opened LICENSE.", menu.license_status.cget("text"))
                self.assertEqual(window.theme.muted, menu.license_status.cget("fg"))

    def test_retained_other_edition_does_not_claim_ve_source(self):
        _, menu = self.preferences("full-local")
        self.assertFalse(hasattr(menu, "license_buttons"))


class QuickChatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    class Entry:
        def __init__(self, value=""):
            self.value = value
            self.states = []
            self.focused = False

        def get(self):
            return self.value

        def insert(self, index, text):
            self.value = self.value[:index] + text + self.value[index:]

        def config(self, **values):
            self.states.append(values)

        def focus_force(self):
            self.focused = True

    class Box:
        def __init__(self, *, visible=True):
            self.exists = True
            self.visible = visible
            self.destroyed = False
            self.withdrawn = False
            self.deiconified = False
            self.lifted = False
            self.focused = False
            self.focused_widget = None

        def winfo_exists(self):
            return self.exists

        def winfo_viewable(self):
            return self.visible

        def withdraw(self):
            self.visible = False
            self.withdrawn = True

        def destroy(self):
            self.exists = False
            self.destroyed = True

        def deiconify(self):
            self.visible = True
            self.deiconified = True

        def lift(self):
            self.lifted = True

        def focus_force(self):
            self.focused = True

        def focus_get(self):
            return self.focused_widget

        def after_idle(self, callback):
            callback()

    class Tray:
        def __init__(self):
            self.notifications = []

        def notify(self, title, message):
            self.notifications.append((title, message))
            return True

    def window(self, *, text="Stack now", visible=True):
        companion = self.companion
        window = object.__new__(companion.CompanionWindow)
        window.caption_suite = True
        window.quick_chat_results = companion.queue.Queue()
        window.quick_chat_draft_text = ""
        window.quick_chat_request_id = ""
        window.quick_chat_attempt_sequence = 0
        window.quick_chat_in_flight = {}
        window.quick_chat_failure_message = ""
        window.speak_entry = self.Entry(text)
        window.speak_status = self.Entry()
        window.speak_counter = None
        window.speak_send = None
        window.speak_box = self.Box(visible=visible)
        window.tray = self.Tray()
        return window

    def reopen(self, window, text=""):
        """Stand in for the next hotkey press after a send closed the box."""
        window.speak_entry = self.Entry(text)
        window.speak_status = self.Entry()
        window.speak_box = self.Box()

    def result(self, attempt, request_id, success, message="Posted"):
        return self.companion.QueuedQuickChatResult(
            attempt,
            self.companion.QuickChatSendResult(
                request_id, success, message, 8 if success else None
            ),
        )

    def test_hotkey_dispatches_the_quick_chat_command(self):
        tray = self.companion.TrayIcon("Vemal's Ears", None)
        tray.hotkey_registered = True
        tray._on_hotkey(None, None, tray.SPEAK_HOTKEY_ID, None)
        self.assertEqual("speak", tray.commands.get_nowait())

    def test_request_ids_are_ascii_protocol_ids_and_not_reused_globally(self):
        first = self.companion.new_quick_chat_request_id()
        second = self.companion.new_quick_chat_request_id()
        self.assertRegex(first, r"^[A-Za-z0-9_-]+$")
        self.assertLessEqual(len(first), 64)
        self.assertNotEqual(first, second)

    def test_closing_quits_by_default_except_where_it_would_cut_captions(self):
        """The default differs by edition, and the difference is the point.

        Downtime and Downtime Lite: closing a window should close it. An
        app that only pretends to close is a daily annoyance for someone
        running it beside a game.

        Vemal's Ears: the caption connection is a CHILD of this process
        (MEMBER_SERVICES), so quitting stops captions. A member closing
        the window mid-pull would lose the one thing they installed it
        for, and the tray note promises the opposite.
        """
        self.assertFalse(
            self.companion.default_minimize_to_taskbar(member_edition=False),
            "closing Downtime should quit it",
        )
        self.assertTrue(
            self.companion.default_minimize_to_taskbar(member_edition=True),
            "quitting Vemal's Ears would stop the member's captions",
        )

    def test_a_saved_choice_outranks_the_default_in_both_directions(self):
        # A default is only ever the starting point. Whichever way the
        # member set the switch has to survive, including setting it back
        # to the value the default already had.
        for edition_is_member in (False, True):
            for stored in (True, False):
                with self.subTest(member=edition_is_member, stored=stored):
                    with tempfile.TemporaryDirectory() as folder:
                        path = Path(folder) / "companion-settings.json"
                        path.write_text(
                            json.dumps({
                                "minimize_to_taskbar": stored,
                                "speak_hotkey": "Ctrl+Alt+V",
                            }),
                            encoding="ascii",
                        )
                        with mock.patch.object(
                            self.companion, "SETTINGS_FILE", path
                        ):
                            settings = self.companion.load_settings(
                                edition_is_member
                            )
                    self.assertEqual(stored, settings["minimize_to_taskbar"])

    def test_a_fresh_install_with_no_file_takes_the_edition_default(self):
        for edition_is_member, expected in ((False, False), (True, True)):
            with self.subTest(member=edition_is_member):
                with tempfile.TemporaryDirectory() as folder:
                    missing = Path(folder) / "does-not-exist.json"
                    with mock.patch.object(
                        self.companion, "SETTINGS_FILE", missing
                    ):
                        settings = self.companion.load_settings(edition_is_member)
                self.assertEqual(expected, settings["minimize_to_taskbar"])

    def test_plaintext_names_are_ignored_while_existing_hotkey_still_loads(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "companion-settings.json"
            path.write_text(
                '{"minimize_to_taskbar":true,"speak_hotkey":"Ctrl+Alt+V",'
                '"speak_name":"Miralyn","quick_chat_name":"Miralyn"}',
                encoding="ascii",
            )
            with mock.patch.object(self.companion, "SETTINGS_FILE", path):
                settings = self.companion.load_settings()
            self.assertNotIn("speak_name", settings)
            self.assertNotIn("quick_chat_name", settings)
            self.assertEqual("Ctrl+Alt+V", settings["speak_hotkey"])

    def test_transmit_requires_the_matching_ack_without_returning_contents(self):
        private_line = "private raid line"
        request_id = "qch_request_1"
        sender = mock.AsyncMock(
            return_value=SimpleNamespace(request_id=request_id, cursor=4)
        )
        store = object()
        with mock.patch("GuildHub.client.send_quick_chat", sender), mock.patch(
            "GuildHub.credentials.WindowsDeviceCredentialStore",
            return_value=store,
        ):
            result = self.companion.transmit_quick_chat(private_line, request_id)
        self.assertTrue(result.success)
        self.assertEqual(request_id, result.request_id)
        self.assertEqual(4, result.cursor)
        self.assertEqual("Accepted by the Vemal's Ears relay.", result.message)
        self.assertNotIn(private_line, repr(result))
        sender.assert_awaited_once_with(
            store,
            private_line,
            request_id=request_id,
        )

    def test_transmit_rejects_an_ack_for_a_different_draft(self):
        sender = mock.AsyncMock(
            return_value=SimpleNamespace(request_id="qch_other", cursor=4)
        )
        with mock.patch("GuildHub.client.send_quick_chat", sender):
            result = self.companion.transmit_quick_chat("Ready", "qch_current")
        self.assertFalse(result.success)
        self.assertEqual("qch_current", result.request_id)

    def test_transmit_failure_never_echoes_an_unknown_private_diagnostic(self):
        private_line = "private raid line"
        sender = mock.AsyncMock(side_effect=RuntimeError(private_line))
        with mock.patch("GuildHub.client.send_quick_chat", sender):
            result = self.companion.transmit_quick_chat(
                private_line,
                "qch_private",
            )
        self.assertFalse(result.success)
        self.assertNotIn(private_line, result.message)

    def test_transmit_keeps_a_safe_actionable_guild_error(self):
        from GuildHub.client import GuildHubClientError

        sender = mock.AsyncMock(
            side_effect=GuildHubClientError(
                "Set your typed-chat display name in Vemal's Ears Preferences."
            )
        )
        with mock.patch("GuildHub.client.send_quick_chat", sender):
            result = self.companion.transmit_quick_chat(
                "Ready", "qch_profile"
            )
        self.assertFalse(result.success)
        self.assertIn("typed-chat display name", result.message)

    def test_a_short_line_does_not_hide_the_real_failure_reason(self):
        """A substring test against the member's own words misfired.

        The relay's quick-chat errors are fixed literals that never contain
        member text, so any short line occurring inside one was coincidence.
        Typing "on" matched the "on" in "console", and a member who had
        simply had not set a typed-chat name was told to check their
        connection and try again - advice that can never work until they
        open Preferences and choose one.
        """
        from GuildHub.client import GuildHubClientError

        explanation = (
            "Set your typed-chat display name in Vemal's Ears Preferences."
        )
        for line in ("on", "the", "me", "art"):
            with self.subTest(line=line):
                sender = mock.AsyncMock(
                    side_effect=GuildHubClientError(explanation)
                )
                with mock.patch("GuildHub.client.send_quick_chat", sender):
                    result = self.companion.transmit_quick_chat(line, "qch_short")
                self.assertFalse(result.success)
                self.assertEqual(explanation, result.message)

    def test_a_genuinely_echoed_line_is_still_withheld(self):
        """The guard must still do the job it was written for.

        A line long enough to appear verbatim in a server message is an
        echo, not a coincidence, and must not be reflected back.
        """
        from GuildHub.client import GuildHubClientError

        line = "we are stacking behind the pillar now"
        sender = mock.AsyncMock(
            side_effect=GuildHubClientError("rejected: " + line)
        )
        with mock.patch("GuildHub.client.send_quick_chat", sender):
            result = self.companion.transmit_quick_chat(line, "qch_echo")
        self.assertFalse(result.success)
        self.assertNotIn(line, result.message)
        self.assertIn("could not post", result.message)

    def test_enter_destroys_the_box_before_any_result_arrives(self):
        """Shino, 2026-09-04: "there's a sending delay but the game is still
        happening. We need it to be out of the way quick." The thread is
        captured unstarted, so anything the box does here it did with no
        answer from the relay in hand.
        """
        window = self.window()
        box = window.speak_box
        entry = window.speak_entry
        status = window.speak_status
        threads = []

        class Thread:
            def __init__(self, *, target, name, daemon):
                self.target = target
                self.name = name
                self.daemon = daemon
                self.started = False
                threads.append(self)

            def start(self):
                self.started = True

        sends = []

        def transmit(text, request_id):
            sends.append((text, request_id))
            return self.companion.QuickChatSendResult(request_id, True, "Posted", 8)

        with mock.patch.object(self.companion.threading, "Thread", Thread), \
                mock.patch.object(self.companion, "transmit_quick_chat", transmit):
            window.send_spoken_text()

            self.assertTrue(box.destroyed)
            self.assertIsNone(window.speak_box)
            # No sending state was ever painted: the box was gone first.
            self.assertEqual([], entry.states)
            self.assertEqual([], status.states)
            self.assertEqual(1, len(threads))
            self.assertTrue(threads[0].started)
            self.assertTrue(threads[0].daemon)
            self.assertTrue(window.quick_chat_results.empty())

            request_id = window.quick_chat_request_id
            self.assertRegex(request_id, r"^[A-Za-z0-9_-]+$")
            self.assertEqual({1: (request_id, "Stack now")}, window.quick_chat_in_flight)
            # The draft survives the close: a failure hands it back.
            self.assertEqual("Stack now", window.quick_chat_draft_text)

            threads[0].target()
        self.assertEqual([("Stack now", request_id)], sends)
        queued = window.quick_chat_results.get_nowait()
        self.assertEqual(1, queued.attempt_id)
        self.assertEqual(request_id, queued.result.request_id)

    def test_two_lines_back_to_back_are_both_in_flight_with_their_own_ids(self):
        window = self.window()
        sent = []
        window.deliver_spoken_text = lambda text, request_id, attempt: sent.append(
            (text, request_id, attempt)
        )
        window.send_spoken_text()
        self.reopen(window, "Move left")
        window.send_spoken_text()

        self.assertEqual(2, len(sent))
        first_id, second_id = sent[0][1], sent[1][1]
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(
            {1: (first_id, "Stack now"), 2: (second_id, "Move left")},
            window.quick_chat_in_flight,
        )

        # Results land out of order and each one settles only its own line.
        window.quick_chat_results.put(
            self.result(2, second_id, False, "Check pairing and try again.")
        )
        window.drain_quick_chat_results()
        self.assertEqual({1: (first_id, "Stack now")}, window.quick_chat_in_flight)
        self.assertEqual("Move left", window.quick_chat_draft_text)
        self.assertEqual(second_id, window.quick_chat_request_id)

        window.quick_chat_results.put(self.result(1, first_id, True))
        window.drain_quick_chat_results()
        self.assertEqual({}, window.quick_chat_in_flight)
        self.assertEqual("Move left", window.quick_chat_draft_text)
        self.assertEqual(second_id, window.quick_chat_request_id)
        self.assertEqual(
            [("Quick chat could not send", "Check pairing and try again.")],
            window.tray.notifications,
        )

    def test_retry_keeps_the_id_until_the_draft_text_changes(self):
        window = self.window()
        sent = []
        window.deliver_spoken_text = lambda text, request_id, attempt: sent.append(
            (text, request_id, attempt)
        )
        window.send_spoken_text()
        first_id = window.quick_chat_request_id

        # The same line sent again while the first is still in flight is a
        # nervous double Enter, not a second message: same id, relay dedups.
        self.reopen(window, "Stack now")
        window.send_spoken_text()
        self.assertEqual(first_id, sent[-1][1])

        self.reopen(window, "Move left")
        window.send_spoken_text()
        self.assertNotEqual(first_id, sent[-1][1])
        self.assertEqual(3, sent[-1][2])

    def test_resending_the_failed_line_reuses_its_request_id(self):
        window = self.window(text="Stack now")
        box = window.speak_box
        window.quick_chat_draft_text = "Stack now"
        window.quick_chat_request_id = "qch_stable"
        window.quick_chat_failure_message = "Check pairing and try again."
        sent = []
        window.deliver_spoken_text = lambda text, request_id, attempt: sent.append(
            (text, request_id, attempt)
        )

        window.send_spoken_text()

        self.assertEqual([("Stack now", "qch_stable", 1)], sent)
        self.assertEqual({1: ("qch_stable", "Stack now")}, window.quick_chat_in_flight)
        self.assertEqual("", window.quick_chat_failure_message)
        self.assertTrue(box.destroyed)

    def test_focus_loss_hides_the_idle_box_without_destroying_the_draft(self):
        window = self.window()
        window.quick_chat_draft_text = "Stack now"
        window.quick_chat_request_id = "qch_stable"
        window.speak_box.focused_widget = SimpleNamespace(master=None)

        window.on_speak_focus_out(None)

        self.assertTrue(window.speak_box.withdrawn)
        self.assertFalse(window.speak_box.destroyed)
        self.assertEqual("Stack now", window.quick_chat_draft_text)
        self.assertEqual("qch_stable", window.quick_chat_request_id)

    def test_repeated_hotkey_focuses_the_message_field_when_idle(self):
        window = self.window(visible=False)
        original = window.speak_box

        window.open_speak_box()

        self.assertIs(original, window.speak_box)
        self.assertTrue(original.deiconified)
        self.assertTrue(original.lifted)
        self.assertTrue(window.speak_entry.focused)

    def test_hotkey_during_a_send_opens_a_fresh_empty_box(self):
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        try:
            window = self.companion.CompanionWindow(
                root,
                edition=self.companion.MEMBER_EDITION,
                preview=True,
            )
            root.deiconify()
            root.update()
            window.quick_chat_draft_text = "Stack now"
            window.quick_chat_request_id = "qch_stable"
            window.quick_chat_in_flight = {1: ("qch_stable", "Stack now")}
            window.open_speak_box()
            root.update()
            self.assertEqual("", window.speak_entry.get())
            self.assertEqual("normal", str(window.speak_entry.cget("state")))
            self.assertEqual("normal", str(window.speak_send.cget("state")))
            self.assertIn("Enter sends and closes.", window.speak_status.cget("text"))
        finally:
            root.destroy()

    def test_late_result_from_an_unknown_attempt_is_ignored(self):
        window = self.window()
        window.quick_chat_draft_text = "Stack now"
        window.quick_chat_request_id = "qch_stable"
        window.quick_chat_in_flight = {2: ("qch_stable", "Stack now")}
        window.quick_chat_results.put(self.result(1, "qch_stable", True))

        window.drain_quick_chat_results()

        self.assertEqual({2: ("qch_stable", "Stack now")}, window.quick_chat_in_flight)
        self.assertEqual("qch_stable", window.quick_chat_request_id)
        self.assertEqual("Stack now", window.quick_chat_draft_text)
        self.assertFalse(window.speak_box.destroyed)
        self.assertEqual([], window.tray.notifications)

    def test_failure_notifies_the_tray_and_keeps_the_line_as_the_draft(self):
        window = self.window(text="")
        window.speak_box = None
        window.speak_entry = None
        window.speak_status = None
        window.quick_chat_draft_text = "Move left"
        window.quick_chat_request_id = "qch_later"
        window.quick_chat_in_flight = {3: ("qch_stable", "Stack now")}
        window.quick_chat_results.put(
            self.result(3, "qch_stable", False, "Check pairing and try again.")
        )

        window.drain_quick_chat_results()

        self.assertEqual({}, window.quick_chat_in_flight)
        self.assertEqual("Stack now", window.quick_chat_draft_text)
        self.assertEqual("qch_stable", window.quick_chat_request_id)
        self.assertEqual(
            "Check pairing and try again.", window.quick_chat_failure_message
        )
        self.assertEqual(
            [("Quick chat could not send", "Check pairing and try again.")],
            window.tray.notifications,
        )

    def test_failure_fills_an_open_empty_box_but_never_a_line_being_typed(self):
        window = self.window(text="")
        window.quick_chat_in_flight = {3: ("qch_stable", "Stack now")}
        window.quick_chat_results.put(
            self.result(3, "qch_stable", False, "Check pairing and try again.")
        )
        window.drain_quick_chat_results()
        self.assertEqual("Stack now", window.speak_entry.get())
        self.assertIn(
            {"text": "Check pairing and try again.", "fg": self.companion.DANGER},
            window.speak_status.states,
        )
        self.assertFalse(window.speak_entry.focused)

        typing = self.window(text="Move lef")
        typing.quick_chat_in_flight = {4: ("qch_stable", "Stack now")}
        typing.quick_chat_results.put(
            self.result(4, "qch_stable", False, "Check pairing and try again.")
        )
        typing.drain_quick_chat_results()
        self.assertEqual("Move lef", typing.speak_entry.get())
        self.assertEqual([], typing.speak_status.states)
        self.assertEqual("Stack now", typing.quick_chat_draft_text)
        self.assertEqual(1, len(typing.tray.notifications))

    def test_next_hotkey_after_a_failure_offers_the_line_with_the_reason(self):
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        try:
            window = self.companion.CompanionWindow(
                root,
                edition=self.companion.MEMBER_EDITION,
                preview=True,
            )
            root.deiconify()
            root.update()
            window.quick_chat_draft_text = "Stack now"
            window.quick_chat_request_id = "qch_stable"
            window.quick_chat_failure_message = "Check pairing and try again."
            window.open_speak_box()
            root.update()
            self.assertEqual("Stack now", window.speak_entry.get())
            self.assertEqual(
                "Check pairing and try again.", window.speak_status.cget("text")
            )
            self.assertEqual(self.companion.DANGER, window.speak_status.cget("fg"))
            self.assertEqual("normal", str(window.speak_entry.cget("state")))
        finally:
            root.destroy()

    def test_success_clears_only_its_own_draft_and_stays_silent(self):
        window = self.window()
        box = window.speak_box
        window.quick_chat_draft_text = "Stack now"
        window.quick_chat_request_id = "qch_stable"
        window.quick_chat_in_flight = {4: ("qch_stable", "Stack now")}
        window.quick_chat_results.put(self.result(4, "qch_stable", True))

        window.drain_quick_chat_results()

        self.assertFalse(box.destroyed)
        self.assertEqual({}, window.quick_chat_in_flight)
        self.assertEqual("", window.quick_chat_request_id)
        self.assertEqual("", window.quick_chat_draft_text)
        self.assertEqual([], window.tray.notifications)

        other = self.window()
        other.quick_chat_draft_text = "Move left"
        other.quick_chat_request_id = "qch_later"
        other.quick_chat_in_flight = {5: ("qch_stable", "Stack now")}
        other.quick_chat_results.put(self.result(5, "qch_stable", True))
        other.drain_quick_chat_results()
        self.assertEqual("Move left", other.quick_chat_draft_text)
        self.assertEqual("qch_later", other.quick_chat_request_id)
        self.assertEqual([], other.tray.notifications)

    def test_popup_position_stays_inside_a_negative_coordinate_monitor(self):
        x, y = self.companion.quick_chat_popup_position(
            (-1920, -1080, 1920, 1080),
            (500, 110),
        )
        self.assertGreaterEqual(x, -1920)
        self.assertLessEqual(x + 500, 0)
        self.assertGreaterEqual(y, -1080)
        self.assertLessEqual(y + 110, 0)

    def test_member_ui_exposes_button_hotkey_and_voice_audience_boundary(self):
        source = (ROOT / "WoW-Companion.pyw").read_text(encoding="utf-8")
        # The separate VE UI reserves red emphasis for communication,
        # both on the main screen and inside its quick-message window.
        self.assertIn("self.hero_chat_button = self.make_chat_button", source)
        self.assertIn("self.speak_send = self.make_chat_button", source)
        self.assertIn('"Message Group",\n            self.open_speak_box', source)
        self.assertIn('prefix + "Message Group: " + hotkey', source)
        self.assertIn("shortcut unavailable.", source)
        self.assertIn("Accepted by the Vemal's Ears relay.", source)
        self.assertIn(
            "Enter sends and closes. Only paired members in the exact active ", source
        )
        self.assertIn("voice channel may see or replay it.", source)
        self.assertNotIn("Posted to Discord", source)
        self.assertNotIn("Discord keeps a copy", source)
        self.assertNotIn("round trip includes a Discord post", source)
        self.assertNotIn("Discord keeps them.", source)
        self.assertNotIn("Sending to your guild", source)
        self.assertNotIn("messages stay memory-only", source)
        self.assertNotIn('text="Character"', source)
        self.assertIn("self.hero_chat_button", source)
        self.assertIn("self.hero_hotkey", source)
        self.assertNotIn("Message raid", source)
        self.assertNotIn("Message your raid", source)
        self.assertNotIn("Sending to your raid", source)


class QuickChatProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    class Field:
        def __init__(self, value=""):
            self.value = value

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    class Menu:
        def __init__(self, value=""):
            self.message_name_field = QuickChatProfileTests.Field(value)
            self.message_name_entry = object()
            self.events = []
            self.exists = True

        def winfo_exists(self):
            return self.exists

        def show_message_name_loading(self):
            self.events.append(("loading",))

        def show_message_name_saving(self):
            self.events.append(("saving",))

        def show_message_name_ready(self, name, **options):
            self.message_name_field.set(name or "")
            self.events.append(("ready", name, options))

        def show_message_name_failure(self, message):
            self.events.append(("failed", message))

    class Widget:
        def __init__(self):
            self.states = []

        def config(self, **options):
            self.states.append(options)

    class Tray:
        def __init__(self):
            self.notifications = []

        def notify(self, title, message):
            self.notifications.append((title, message))
            return True

    def window(self, *, preview=False):
        companion = self.companion
        window = object.__new__(companion.CompanionWindow)
        window.caption_suite = True
        window.managed_edition = True
        window.preview = preview
        window.quick_chat_profile_results = companion.queue.Queue()
        window.quick_chat_profile_sequence = 0
        window.quick_chat_profile_active_sequence = 0
        window.quick_chat_profile_pending_operation = ""
        window.quick_chat_profile_pending_name = None
        window.quick_chat_profile_loaded = False
        window.quick_chat_profile_name = None
        window.menu = None
        window.tray = self.Tray()
        return window

    @staticmethod
    def run_thread(thread_type):
        call = thread_type.call_args
        call.kwargs["target"](*call.kwargs.get("args", ()))

    def test_name_validation_canonicalizes_without_accepting_hidden_controls(self):
        canonicalize = self.companion.canonicalize_quick_chat_name
        self.assertEqual("Mira Lyn", canonicalize("  \uff2dira   Lyn "))
        self.assertEqual("\u0645\u064a\u0631\u0627", canonicalize("\u0645\u064a\u0631\u0627"))
        for value in (
            "",
            "x" * 33,
            "Mira\nLyn",
            "Mira\u200dLyn",
            "\u202eMira",
            None,
        ):
            with self.subTest(value=repr(value)):
                self.assertIsNone(canonicalize(value))

    def test_profile_result_repr_never_contains_name_or_error_copy(self):
        result = self.companion.QuickChatProfileResult(
            7,
            "save",
            False,
            "Miralyn",
            "private profile diagnostic",
        )
        self.assertEqual("QuickChatProfileResult(<redacted>)", repr(result))
        self.assertNotIn("Miralyn", repr(result))
        self.assertNotIn("private profile diagnostic", repr(result))

    def test_thread_start_failure_clears_busy_state_and_surfaces_failure(self):
        for operation in ("load", "save"):
            with self.subTest(operation=operation):
                window = self.window()
                menu = self.Menu("Mira")
                window.menu = menu
                thread = mock.Mock()
                thread.start.side_effect = RuntimeError("private thread failure")
                with mock.patch.object(
                    self.companion.threading,
                    "Thread",
                    return_value=thread,
                ):
                    if operation == "load":
                        window.request_quick_chat_profile(menu)
                        self.assertEqual("failed", menu.events[-1][0])
                        accepted = False
                        message = menu.events[-1][1]
                    else:
                        accepted, message = window.save_quick_chat_profile_name(
                            "Mira",
                            menu,
                        )

                self.assertFalse(accepted)
                expected = "loaded" if operation == "load" else "saved"
                self.assertIn("could not be " + expected, message)
                self.assertNotIn("private thread failure", message)
                self.assertEqual("", window.quick_chat_profile_pending_operation)
                self.assertIsNone(window.quick_chat_profile_pending_name)

    def test_managed_preferences_show_the_server_backed_name_row(self):
        import tkinter as tk

        for edition in (
            self.companion.MEMBER_EDITION,
        ):
            with self.subTest(edition=edition), mock.patch(
                "GuildHub.client.get_quick_chat_profile",
                side_effect=AssertionError("preview used the network"),
            ), mock.patch(
                "GuildHub.client.set_quick_chat_profile",
                side_effect=AssertionError("preview used the network"),
            ):
                root = tk.Tk()
                root.withdraw()
                try:
                    window = self.companion.CompanionWindow(
                        root,
                        edition=edition,
                        preview=True,
                    )
                    window.open_settings()
                    root.update()
                    menu = window.menu
                    self.assertIsNotNone(menu.message_name_entry)
                    self.assertEqual("Save name", menu.message_name_save.cget("text"))
                    self.assertIn(
                        "no name was loaded or saved",
                        menu.message_name_status.cget("text"),
                    )
                    menu.dismiss()
                finally:
                    root.destroy()

    def test_fetch_is_async_and_applies_the_server_canonical_name(self):
        window = self.window()
        menu = self.Menu()
        window.menu = menu
        credentials = object()
        store = SimpleNamespace(load=mock.Mock(return_value=credentials))
        getter = mock.AsyncMock(
            return_value=SimpleNamespace(quick_chat_name="  Mira   Lyn ")
        )
        thread = mock.Mock()
        with mock.patch(
            "GuildHub.client.get_quick_chat_profile",
            getter,
        ), mock.patch.object(
            self.companion, "guild_credential_store",
            return_value=store,
        ), mock.patch.object(
            self.companion.threading,
            "Thread",
            return_value=thread,
        ) as thread_type:
            window.request_quick_chat_profile(menu)
            thread.start.assert_called_once_with()
            self.assertEqual([("loading",)], menu.events)
            self.run_thread(thread_type)

        window.drain_quick_chat_profile_results()
        getter.assert_awaited_once()
        snapshot = getter.await_args.args[0]
        self.assertIs(credentials, snapshot.load())
        self.assertNotIn(repr(credentials), repr(snapshot))
        self.assertTrue(window.quick_chat_profile_loaded)
        self.assertEqual("Mira Lyn", window.quick_chat_profile_name)
        self.assertEqual("Mira Lyn", menu.message_name_field.get())
        self.assertEqual("ready", menu.events[-1][0])

    def test_save_is_server_backed_and_never_writes_companion_settings(self):
        window = self.window()
        menu = self.Menu("  Mira   Lyn ")
        window.menu = menu
        credentials = object()
        store = SimpleNamespace(load=mock.Mock(return_value=credentials))
        setter = mock.AsyncMock(
            return_value=SimpleNamespace(quick_chat_name="Mira Lyn")
        )
        thread = mock.Mock()
        with mock.patch(
            "GuildHub.client.set_quick_chat_profile",
            setter,
        ), mock.patch.object(
            self.companion, "guild_credential_store",
            return_value=store,
        ), mock.patch.object(
            self.companion.threading,
            "Thread",
            return_value=thread,
        ) as thread_type, mock.patch.object(
            self.companion,
            "save_settings",
        ) as local_save:
            started, _message = window.save_quick_chat_profile_name(
                "  Mira   Lyn ",
                menu,
            )
            self.assertTrue(started)
            self.assertEqual("Mira Lyn", menu.message_name_field.get())
            self.assertEqual("Mira Lyn", window.quick_chat_profile_pending_name)
            self.run_thread(thread_type)
            window.drain_quick_chat_profile_results()

        setter.assert_awaited_once()
        snapshot, saved_name = setter.await_args.args
        self.assertIs(credentials, snapshot.load())
        self.assertEqual("Mira Lyn", saved_name)
        local_save.assert_not_called()
        self.assertEqual("Mira Lyn", window.quick_chat_profile_name)
        self.assertIsNone(window.quick_chat_profile_pending_name)
        self.assertEqual("ready", menu.events[-1][0])
        self.assertTrue(menu.events[-1][2]["saved"])

    def test_invalid_name_never_starts_a_request(self):
        window = self.window()
        menu = self.Menu()
        with mock.patch.object(
            window,
            "_start_quick_chat_profile_request",
        ) as start:
            for value in ("", "x" * 33, "Mira\u202eLyn"):
                with self.subTest(value=repr(value)):
                    accepted, message = window.save_quick_chat_profile_name(
                        value,
                        menu,
                    )
                    self.assertFalse(accepted)
                    self.assertIn("1-32 characters", message)
            start.assert_not_called()

    def test_stale_result_is_ignored_and_closed_menu_failure_uses_tray(self):
        window = self.window()
        window.quick_chat_profile_active_sequence = 2
        window.quick_chat_profile_pending_operation = "save"
        window.quick_chat_profile_loaded = True
        window.quick_chat_profile_name = "Current"
        window.quick_chat_profile_results.put(
            self.companion.QuickChatProfileResult(
                1,
                "save",
                True,
                "Stale",
            )
        )
        window.quick_chat_profile_results.put(
            self.companion.QuickChatProfileResult(
                2,
                "save",
                False,
                message="Your guild connection is offline.",
            )
        )

        window.drain_quick_chat_profile_results()

        self.assertEqual("Current", window.quick_chat_profile_name)
        self.assertEqual("", window.quick_chat_profile_pending_operation)
        self.assertEqual(
            [
                (
                    "Message name could not save",
                    "Your guild connection is offline.",
                )
            ],
            window.tray.notifications,
        )

    def test_closed_menu_failure_falls_back_to_footer_without_a_tray(self):
        window = self.window()
        window.tray = self.companion.TrayIcon("Vemal's Ears", None)
        window.tray.available = False
        window.footer_status = self.Widget()
        window.quick_chat_profile_active_sequence = 1
        window.quick_chat_profile_pending_operation = "save"
        window.quick_chat_profile_results.put(
            self.companion.QuickChatProfileResult(
                1,
                "save",
                False,
                message="Your message name could not be saved.",
            )
        )

        window.drain_quick_chat_profile_results()

        self.assertEqual(
            {
                "text": "Your message name could not be saved.",
                "fg": self.companion.DANGER,
            },
            window.footer_status.states[-1],
        )
        window.settings = {"speak_hotkey": "Ctrl+Alt+V"}
        window.hero_hotkey = None
        window.quick_chat_hint = lambda: "Message Group: Ctrl+Alt+V"
        window.refresh_hotkey_surfaces()
        self.assertEqual(
            {
                "text": "Message Group: Ctrl+Alt+V",
                "fg": self.companion.MUTED,
            },
            window.footer_status.states[-1],
        )

    def test_pairing_change_clears_old_guild_and_only_loads_the_new_one(self):
        window = self.window()
        menu = self.Menu("Old guild name")
        window.menu = menu
        window.quick_chat_profile_sequence = 1
        window.quick_chat_profile_active_sequence = 1
        window.quick_chat_profile_pending_operation = "save"
        window.quick_chat_profile_pending_name = "Old pending name"
        window.quick_chat_profile_loaded = True
        window.quick_chat_profile_name = "Old guild name"
        window.quick_chat_profile_results.put(
            self.companion.QuickChatProfileResult(
                1,
                "save",
                True,
                "Old result",
            )
        )
        credentials = object()
        store = SimpleNamespace(load=mock.Mock(return_value=credentials))
        getter = mock.AsyncMock(
            return_value=SimpleNamespace(quick_chat_name="New guild name")
        )
        thread = mock.Mock()
        with mock.patch(
            "GuildHub.client.get_quick_chat_profile",
            getter,
        ), mock.patch.object(
            self.companion, "guild_credential_store",
            return_value=store,
        ), mock.patch.object(
            self.companion.threading,
            "Thread",
            return_value=thread,
        ) as thread_type:
            window.reset_quick_chat_profile_for_pairing()
            self.assertFalse(window.quick_chat_profile_loaded)
            self.assertIsNone(window.quick_chat_profile_name)
            self.assertIsNone(window.quick_chat_profile_pending_name)
            self.assertEqual("", menu.message_name_field.get())
            window.drain_quick_chat_profile_results()
            self.assertFalse(window.quick_chat_profile_loaded)

            self.run_thread(thread_type)
            window.drain_quick_chat_profile_results()

        self.assertEqual("New guild name", window.quick_chat_profile_name)
        self.assertEqual("New guild name", menu.message_name_field.get())

    def test_failed_signal_unlink_is_consumed_once_until_signal_changes(self):
        window = self.window()
        window.pair_signal_identity = None
        with tempfile.TemporaryDirectory() as folder:
            signal = Path(folder) / "paired.signal"
            signal.write_text("first", encoding="ascii")
            with mock.patch.object(
                self.companion,
                "PAIR_SIGNAL",
                signal,
            ), mock.patch.object(
                Path,
                "unlink",
                side_effect=OSError("busy"),
            ):
                self.assertTrue(window.consume_pairing_signal())
                self.assertFalse(window.consume_pairing_signal())
                signal.write_text("second signal", encoding="ascii")
                self.assertTrue(window.consume_pairing_signal())

    def test_offline_load_keeps_the_field_editable_with_truthful_copy(self):
        from GuildHub.client import GuildHubClientError

        window = self.window()
        window.quick_chat_profile_loaded = True
        window.quick_chat_profile_name = "Last known name"
        menu = self.Menu("Last known name")
        window.menu = menu
        getter = mock.AsyncMock(
            side_effect=GuildHubClientError(
                "Pair this computer with a Vemal's Ears guild server first."
            )
        )
        store = SimpleNamespace(load=mock.Mock(return_value=object()))
        thread = mock.Mock()
        with mock.patch(
            "GuildHub.client.get_quick_chat_profile",
            getter,
        ), mock.patch.object(
            self.companion, "guild_credential_store",
            return_value=store,
        ), mock.patch.object(
            self.companion.threading,
            "Thread",
            return_value=thread,
        ) as thread_type:
            window.request_quick_chat_profile(menu)
            self.run_thread(thread_type)
            window.drain_quick_chat_profile_results()

        self.assertFalse(window.quick_chat_profile_loaded)
        self.assertEqual("Last known name", menu.message_name_field.get())
        self.assertEqual("failed", menu.events[-1][0])
        self.assertIn("Pair this computer", menu.events[-1][1])
        with mock.patch.object(
            window,
            "_start_quick_chat_profile_request",
            return_value=(True, ""),
        ) as retry:
            accepted, _message = window.save_quick_chat_profile_name(
                "Last known name",
                menu,
            )
        self.assertTrue(accepted)
        retry.assert_called_once_with("save", "Last known name")

    def test_explicit_save_retries_the_cached_name_after_a_failed_save(self):
        saved = mock.Mock(return_value=(True, "Saving to your guild..."))
        window = SimpleNamespace(
            quick_chat_profile_pending_operation="",
            quick_chat_profile_loaded=True,
            quick_chat_profile_name="Mira",
            preview=False,
            save_quick_chat_profile_name=saved,
        )
        menu = object.__new__(self.companion.SettingsMenu)
        menu.window = window
        menu.message_name_field = self.Field("Mira")
        menu.message_name_entry = self.Widget()
        menu.message_name_save = self.Widget()
        menu.message_name_status = self.Widget()
        menu.message_name_baseline = "Mira"
        menu.show_message_name_saving = mock.Mock()
        menu.show_message_name_ready = mock.Mock()
        menu.show_message_name_failure = mock.Mock()

        accepted = self.companion.SettingsMenu.commit_message_name(
            menu,
            force=True,
        )

        self.assertTrue(accepted)
        saved.assert_called_once_with("Mira", menu)
        menu.show_message_name_ready.assert_not_called()

    def test_preview_load_and_save_do_not_start_network_work(self):
        window = self.window(preview=True)
        menu = self.Menu("Mira")
        with mock.patch.object(
            window,
            "_start_quick_chat_profile_request",
        ) as start:
            window.request_quick_chat_profile(menu)
            accepted, message = window.save_quick_chat_profile_name("Mira", menu)

        self.assertTrue(accepted)
        self.assertIn("Preview only", message)
        start.assert_not_called()

    def test_done_commits_a_valid_typed_name_before_closing(self):
        saved = mock.Mock(return_value=(True, "Saving to your guild..."))
        window = SimpleNamespace(
            quick_chat_profile_pending_operation="",
            quick_chat_profile_loaded=False,
            quick_chat_profile_name=None,
            preview=False,
            save_quick_chat_profile_name=saved,
            menu=object(),
            menu_closed_at=0.0,
        )
        menu = object.__new__(self.companion.SettingsMenu)
        menu.window = window
        menu.capture_active = False
        menu.cancel_hotkey_capture = mock.Mock()
        menu.message_name_field = self.Field("  Mira   Lyn ")
        menu.message_name_entry = self.Widget()
        menu.message_name_save = self.Widget()
        menu.message_name_status = self.Widget()
        menu.message_name_baseline = None
        menu.entry_commits = [
            lambda: self.companion.SettingsMenu.commit_message_name(menu)
        ]
        menu.flush_typed_values = lambda: (
            self.companion.SettingsMenu.flush_typed_values(menu)
        )
        menu.destroy = mock.Mock()

        self.companion.SettingsMenu.dismiss(menu)

        saved.assert_called_once_with("Mira Lyn", menu)
        menu.destroy.assert_called_once_with()

    def test_escape_discards_an_invalid_name_and_still_closes(self):
        saved = mock.Mock()
        window = SimpleNamespace(
            quick_chat_profile_pending_operation="",
            quick_chat_profile_loaded=True,
            quick_chat_profile_name="Mira",
            preview=False,
            save_quick_chat_profile_name=saved,
            menu=object(),
            menu_closed_at=0.0,
        )
        menu = object.__new__(self.companion.SettingsMenu)
        menu.window = window
        menu.capture_active = False
        menu.cancel_hotkey_capture = mock.Mock()
        menu.message_name_field = self.Field("Mira\u202e")
        menu.message_name_baseline = "Mira"
        menu.entry_commits = [
            lambda: self.companion.SettingsMenu.commit_message_name(menu)
        ]
        menu.destroy = mock.Mock()

        result = self.companion.SettingsMenu.on_escape(menu)

        self.assertEqual("break", result)
        saved.assert_not_called()
        menu.destroy.assert_called_once_with()

    def test_blank_unverified_panel_closes_despite_a_stale_cached_name(self):
        saved = mock.Mock()
        window = SimpleNamespace(
            quick_chat_profile_pending_operation="",
            quick_chat_profile_loaded=False,
            quick_chat_profile_name="Stale guild name",
            preview=False,
            save_quick_chat_profile_name=saved,
            menu=object(),
            menu_closed_at=0.0,
        )
        menu = object.__new__(self.companion.SettingsMenu)
        menu.window = window
        menu.capture_active = False
        menu.cancel_hotkey_capture = mock.Mock()
        menu.message_name_field = self.Field("")
        menu.message_name_baseline = None
        menu.entry_commits = [
            lambda: self.companion.SettingsMenu.commit_message_name(menu)
        ]
        menu.flush_typed_values = lambda: (
            self.companion.SettingsMenu.flush_typed_values(menu)
        )
        menu.destroy = mock.Mock()

        self.companion.SettingsMenu.dismiss(menu)

        saved.assert_not_called()
        menu.destroy.assert_called_once_with()


class MemberCaptionStopTests(unittest.TestCase):
    """A member turning off their own captions, and only their own."""

    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    def service(self, *, member_edition=True):
        companion = self.companion
        edition = (
            companion.MEMBER_EDITION
            if member_edition
            else companion.FULL_EDITION
        )
        window = SimpleNamespace(
            member_edition=member_edition,
            member_caption_button=None,
            member_caption_service=None,
            refresh_member_caption_button=lambda: None,
        )
        spec = next(
            spec
            for spec in companion.service_specs_for_edition(edition)
            if spec.key == "voice"
        )
        service = companion.Service(spec=spec, window=window)
        window.member_caption_service = service
        return service

    def test_a_deliberate_stop_is_not_undone_by_the_reconnect(self):
        """The member edition reconnects a dropped receiver after three
        seconds. That is right for a network blip and wrong for a button:
        without the flag the captions would come back before the member
        let go of the mouse."""
        service = self.service()
        service.process = SimpleNamespace(poll=lambda: 0)
        with mock.patch.object(service, "request_stop") as requested:
            service.toggle_member_captions()
        requested.assert_called_once()
        self.assertTrue(service.paused_by_member)
        self.assertEqual(0.0, service.restart_at)

    def test_member_voice_uses_the_same_local_caption_pause(self):
        companion = self.companion
        spec = next(
            spec
            for spec in companion.service_specs_for_edition(
                companion.MEMBER_EDITION
            )
            if spec.key == "voice"
        )
        window = SimpleNamespace(refresh_member_caption_button=lambda: None)
        service = companion.Service(spec=spec, window=window)
        service.process = SimpleNamespace(poll=lambda: 0)
        with mock.patch.object(service, "request_stop") as requested:
            service.toggle_member_captions()
        requested.assert_called_once_with()
        self.assertTrue(service.paused_by_member)
        self.assertTrue(spec.auto_restart)
        self.assertFalse(spec.show_lifecycle_controls)

    def test_the_reader_reaching_eof_schedules_nothing_when_paused(self):
        """The suppression that actually matters.

        Stopping the process makes the reader see EOF, and that is the
        code which schedules the three-second reconnect. Testing only the
        button would pass against a version that still came back.
        """
        service = self.service()
        service.paused_by_member = True
        process = mock.Mock()
        process.poll.return_value = 0
        service.process = process
        service.lines.put((process, None))

        service.drain()

        self.assertIsNone(service.process)
        self.assertEqual(0.0, service.restart_at, "a paused member must stay paused")

    def test_an_unasked_for_drop_still_reconnects(self):
        """The pause must not break recovery from a real network blip."""
        service = self.service()
        process = mock.Mock()
        process.poll.return_value = 1
        service.process = process
        service.lines.put((process, None))

        before = self.companion.time.monotonic()
        service.drain()

        self.assertGreaterEqual(service.restart_at, before + 3.0)

    def test_starting_again_clears_the_pause(self):
        service = self.service()
        service.paused_by_member = True
        # start() bails before spawning anything when the script is absent,
        # but the intent is cleared first, which is the part that matters.
        with mock.patch.object(type(service.spec.script), "is_file",
                               return_value=False):
            service.start()
        self.assertFalse(service.paused_by_member)

    def test_the_label_says_what_pressing_it_will_do(self):
        companion = self.companion
        window = SimpleNamespace(member_caption_button=None,
                                 member_caption_service=None)
        labels = []
        window.member_caption_button = SimpleNamespace(
            config=lambda **kwargs: labels.append(kwargs.get("text"))
        )
        service = self.service()
        window.member_caption_service = service
        companion.CompanionWindow.refresh_member_caption_button(window)
        service.paused_by_member = True
        companion.CompanionWindow.refresh_member_caption_button(window)
        self.assertEqual(["Pause caption feed", "Resume caption feed"], labels)

    def test_the_label_never_reads_as_stopping_everyone(self):
        """A member who believed this silenced the whole raid's captions
        would be wrong in the worst possible direction."""
        companion = self.companion
        source = (ROOT / "WoW-Companion.pyw").read_text(encoding="utf-8")
        self.assertIn("Pause caption feed", source)
        self.assertIn("Resume caption feed", source)


class PreferencesSaveOnCloseTests(unittest.TestCase):
    """The panel says "Changes save automatically." so it has to be true.

    The caption number fields commit on <Return> and <FocusOut>. Clicking
    Done is neither: it does not reliably move keyboard focus off the
    entry before the Toplevel is destroyed, so a value the member had
    typed and could still see on screen was discarded - under a header
    telling them it was already saved.
    """

    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    def menu(self, commits):
        return SimpleNamespace(
            capture_active=False,
            entry_commits=commits,
            cancel_hotkey_capture=mock.Mock(),
            flush_typed_values=lambda: (
                self.companion.SettingsMenu.flush_typed_values(
                    SimpleNamespace(entry_commits=commits)
                )
            ),
            window=SimpleNamespace(menu=object(), menu_closed_at=0.0),
            destroy=mock.Mock(),
        )

    def test_done_saves_a_number_the_member_typed(self):
        commit = mock.Mock()
        menu = self.menu([commit])
        self.companion.SettingsMenu.dismiss(menu)
        commit.assert_called_once_with()
        menu.destroy.assert_called_once_with()

    def test_every_field_is_flushed_not_just_the_first(self):
        commits = [mock.Mock(), mock.Mock(), mock.Mock()]
        self.companion.SettingsMenu.dismiss(self.menu(commits))
        for commit in commits:
            commit.assert_called_once_with()

    def test_a_destroyed_field_does_not_block_closing(self):
        """A half-torn-down widget must not strand the panel open."""
        import tkinter as tk_module

        angry = mock.Mock(side_effect=tk_module.TclError("gone"))
        healthy = mock.Mock()
        menu = self.menu([angry, healthy])
        self.companion.SettingsMenu.dismiss(menu)
        healthy.assert_called_once_with()
        menu.destroy.assert_called_once_with()


class HotkeyCaptureIgnoresTextFieldsTests(unittest.TestCase):
    """Capture is bound on the Toplevel, so it sees the number boxes too.

    With capture armed, clicking into a caption box and pressing Ctrl+A
    to select its contents registered Ctrl+A as a SYSTEM-WIDE hotkey, and
    pressing Backspace to delete a digit reset the shortcut to its
    default. cancel_capture_if_focus_left does not cover it: an Entry
    inside the panel never leaves the window.
    """

    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    def menu_focused_on(self, widget_class):
        focused = (
            None if widget_class is None
            else SimpleNamespace(winfo_class=lambda: widget_class)
        )
        menu = SimpleNamespace(
            capture_active=True,
            focus_get=lambda: focused,
        )
        # Bind the real guard onto the stub so the key handler exercises
        # the actual implementation rather than a stand-in.
        menu.focus_is_text_field = (
            lambda: self.companion.SettingsMenu.focus_is_text_field(menu)
        )
        return menu

    def test_a_plain_entry_owns_its_keystrokes(self):
        menu = self.menu_focused_on("Entry")
        self.assertTrue(
            self.companion.SettingsMenu.focus_is_text_field(menu))

    def test_a_ttk_entry_owns_them_too(self):
        """ttk.Entry is not a tk.Entry subclass, so isinstance would miss it."""
        menu = self.menu_focused_on("TEntry")
        self.assertTrue(
            self.companion.SettingsMenu.focus_is_text_field(menu))

    def test_a_frame_does_not(self):
        menu = self.menu_focused_on("Frame")
        self.assertFalse(
            self.companion.SettingsMenu.focus_is_text_field(menu))

    def test_nothing_focused_does_not(self):
        menu = self.menu_focused_on(None)
        self.assertFalse(
            self.companion.SettingsMenu.focus_is_text_field(menu))

    def test_ctrl_a_in_a_number_box_is_not_captured_as_a_hotkey(self):
        """The reported defect, end to end through the key handler."""
        menu = self.menu_focused_on("Entry")
        menu.apply_hotkey = mock.Mock()
        menu.cancel_hotkey_capture = mock.Mock()
        event = SimpleNamespace(keysym="a", keycode=65, state=4)  # Ctrl+A
        result = self.companion.SettingsMenu.on_hotkey_keypress(menu, event)
        self.assertIsNone(
            result,
            "capture swallowed a keystroke meant for the text box",
        )
        menu.apply_hotkey.assert_not_called()

    def test_backspace_in_a_number_box_does_not_reset_the_shortcut(self):
        menu = self.menu_focused_on("Entry")
        menu.apply_hotkey = mock.Mock()
        menu.cancel_hotkey_capture = mock.Mock()
        event = SimpleNamespace(keysym="BackSpace", keycode=8, state=0)
        result = self.companion.SettingsMenu.on_hotkey_keypress(menu, event)
        self.assertIsNone(result)
        menu.apply_hotkey.assert_not_called()

    def test_escape_in_a_number_box_does_not_cancel_capture(self):
        menu = self.menu_focused_on("Entry")
        menu.apply_hotkey = mock.Mock()
        menu.cancel_hotkey_capture = mock.Mock()
        event = SimpleNamespace(keysym="Escape", keycode=27, state=0)
        result = self.companion.SettingsMenu.on_hotkey_keypress(menu, event)
        self.assertIsNone(result)
        menu.cancel_hotkey_capture.assert_not_called()


class FakeText:
    """Just enough Tk Text to drive the real append().

    Mirrors the two behaviours the trim depends on: index("end-1c")
    reports one MORE than the number of complete lines, and
    delete("1.0", "N.0") removes the first N-1 lines.
    """

    def __init__(self):
        self.lines = []
        self._partial = ""

    def config(self, **kwargs):
        pass

    def insert(self, where, chunk, tag=None):
        self._partial += chunk
        while "\n" in self._partial:
            done, self._partial = self._partial.split("\n", 1)
            self.lines.append(done)

    def index(self, spec):
        return "%d.0" % (len(self.lines) + 1)

    def delete(self, start, end):
        # Real Tk accepts "end" as well as an "N.0" index: the trim uses
        # the latter, Clear uses the former.
        if end == "end":
            self.lines = []
            self._partial = ""
            return
        cut = int(float(end)) - 1
        del self.lines[:cut]

    def get(self, start, end):
        joined = "\n".join(self.lines)
        if self._partial:
            joined = joined + ("\n" if joined else "") + self._partial
        return joined

    def see(self, where):
        pass


class ActivityLogIsBoundedTests(unittest.TestCase):
    """The log must not grow for the life of the process.

    Nothing ever deleted from this widget. Four child processes wrote
    into it for as long as the app was open, and a Tk Text keeps every
    line plus two tag ranges per line in memory until the process exits.
    """

    def test_the_log_stops_growing(self):
        companion = load_companion()
        spec = companion.SERVICES[0]
        # append() also keeps a running count, so the stub carries one.
        stub = SimpleNamespace(log=FakeText(), activity_count=0)
        for index in range(companion.MAX_ACTIVITY_LINES * 3):
            companion.CompanionWindow.append(stub, spec, "line %d" % index)
        self.assertLessEqual(
            len(stub.log.lines),
            companion.MAX_ACTIVITY_LINES,
            "the Activity log grew without bound",
        )

    def test_the_newest_lines_are_the_ones_kept(self):
        """Trimming from the wrong end would throw away what just happened."""
        companion = load_companion()
        spec = companion.SERVICES[0]
        # append() also keeps a running count, so the stub carries one.
        stub = SimpleNamespace(log=FakeText(), activity_count=0)
        for index in range(companion.MAX_ACTIVITY_LINES * 2):
            companion.CompanionWindow.append(stub, spec, "line %d" % index)
        self.assertIn(
            "line %d" % (companion.MAX_ACTIVITY_LINES * 2 - 1),
            stub.log.lines[-1],
        )


class RestartBackoffTests(unittest.TestCase):
    """A failing child must not be respawned on every tick.

    tick's guard is "restart_at and now >= restart_at". Any start() path
    that returned without clearing restart_at left it armed and in the
    past, so the intended 3-second retry became a retry on every 120ms
    tick - 8.3 process spawns a second, for the rest of the session.
    """

    def setUp(self):
        self.companion = load_companion()

    def service(self, script):
        spec = replace(self.companion.MEMBER_SERVICES[0], script=script)
        return self.companion.Service(spec=spec, window=mock.Mock())

    def test_a_missing_script_does_not_leave_the_retry_armed(self):
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / "GuildHub" / "client.py"
            service = self.service(missing)
            service.restart_at = 1.0  # armed, and in the past
            service.set_status = mock.Mock()
            before = self.companion.time.monotonic()
            service.start(automatic=True)
            # The spin came from a timestamp that was armed AND already
            # due: tick's guard is "restart_at and now >= restart_at".
            # Clearing it outright is the opposite mistake - the service
            # is then never retried at all. What must never happen is a
            # due timestamp.
            self.assertFalse(
                service.restart_at and service.restart_at <= before,
                "the retry was armed and already due, so tick would "
                "respawn it on every 120ms pass",
            )

    def test_a_refused_launch_does_not_leave_the_retry_armed(self):
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "GuildHub" / "client.py"
            script.parent.mkdir()
            script.write_text("", encoding="ascii")
            service = self.service(script)
            service.restart_at = 1.0
            service.set_status = mock.Mock()
            with mock.patch.object(
                self.companion, "child_python", return_value="pythonw.exe"
            ), mock.patch.object(
                self.companion.subprocess, "Popen",
                side_effect=OSError("refused"),
            ):
                before = self.companion.time.monotonic()
                service.start(automatic=True)
            self.assertFalse(
                service.restart_at and service.restart_at <= before,
                "the retry was armed and already due after a refused "
                "launch",
            )

    def test_repeated_failures_back_off_instead_of_repeating(self):
        service = self.service(Path("nowhere/client.py"))
        service.set_status = mock.Mock()
        delays = []
        for _ in range(4):
            before = self.companion.time.monotonic()
            service.note_death_and_schedule_restart()
            delays.append(service.restart_at - before)
        for earlier, later in zip(delays, delays[1:]):
            self.assertGreater(
                later, earlier,
                "the retry delay did not grow: %r" % (delays,),
            )

    def test_the_backoff_has_a_ceiling(self):
        service = self.service(Path("nowhere/client.py"))
        service.set_status = mock.Mock()
        for _ in range(self.companion.RESTART_GIVE_UP_AFTER):
            before = self.companion.time.monotonic()
            service.note_death_and_schedule_restart()
            if service.restart_at:
                self.assertLessEqual(
                    service.restart_at - before,
                    self.companion.RESTART_MAX_SECONDS + 1,
                )

    def test_it_eventually_gives_up_and_says_so(self):
        """A member of a managed edition has no Stop button.

        So retrying invisibly for ever is the one thing this must not do:
        the status line is their only way to learn anything is wrong.
        """
        service = self.service(Path("nowhere/client.py"))
        service.set_status = mock.Mock()
        for _ in range(self.companion.RESTART_GIVE_UP_AFTER + 1):
            service.note_death_and_schedule_restart()
        self.assertEqual(
            0.0, service.restart_at,
            "it kept rescheduling after the give-up threshold",
        )
        said = " ".join(
            str(call.args[0]) for call in service.set_status.call_args_list
        )
        self.assertIn("keeps stopping", said)

    def test_a_healthy_run_starts_the_backoff_over(self):
        service = self.service(Path("nowhere/client.py"))
        service.set_status = mock.Mock()
        for _ in range(4):
            service.note_death_and_schedule_restart()
        # it ran for a long while this time
        service.started_at = (
            self.companion.time.monotonic()
            - self.companion.RESTART_HEALTHY_SECONDS - 1
        )
        before = self.companion.time.monotonic()
        service.note_death_and_schedule_restart()
        self.assertAlmostEqual(
            self.companion.RESTART_BASE_SECONDS,
            service.restart_at - before,
            delta=1.0,
        )

    def test_a_deliberate_start_gives_a_full_set_of_attempts_again(self):
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / "GuildHub" / "client.py"
            service = self.service(missing)
            service.set_status = mock.Mock()
            service.restart_failures = 5
            service.start()  # the member pressing Start
            self.assertEqual(0, service.restart_failures)

    def test_an_automatic_start_does_not_reset_the_failure_count(self):
        """Otherwise tick's own restart flattens the backoff to nothing."""
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / "GuildHub" / "client.py"
            service = self.service(missing)
            service.set_status = mock.Mock()
            service.restart_failures = 5
            service.start(automatic=True)
            # A failed automatic start counts as another failure, so
            # this rises. What it must never do is RESET to zero -
            # that would flatten the backoff and defeat the give-up.
            self.assertGreaterEqual(service.restart_failures, 5)


class FailedStartIsRetriedTests(unittest.TestCase):
    """A launch that never spawned still has to be retried.

    Clearing restart_at at the top of start() stopped the 8.3Hz spin, but
    it also made a FAILED launch terminal: no child means no pump thread,
    no EOF, and so note_death_and_schedule_restart is never reached.
    One transient OSError from Popen - antivirus holding pythonw.exe,
    memory pressure with the game running - and the service was stopped
    for the rest of the session behind a small red line, with no
    Stop/Start button on a managed edition to force it.

    The old code retried badly, every 120ms, but it did recover.
    """

    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    def service(self, script):
        spec = replace(self.companion.MEMBER_SERVICES[0], script=script)
        service = self.companion.Service(spec=spec, window=mock.Mock())
        service.set_status = mock.Mock()
        return service

    def test_a_transient_launch_failure_is_retried(self):
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "GuildHub" / "client.py"
            script.parent.mkdir()
            script.write_text("", encoding="ascii")
            service = self.service(script)
            with mock.patch.object(
                self.companion, "child_python", return_value="pythonw.exe"
            ), mock.patch.object(
                self.companion.subprocess, "Popen",
                side_effect=OSError("WinError 8"),
            ):
                service.start(automatic=True)
            self.assertTrue(
                service.restart_at,
                "one transient Popen failure left the service stopped "
                "for the whole session",
            )
            self.assertEqual(1, service.restart_failures)

    def test_a_missing_script_is_retried_when_automatic(self):
        """An in-place update can have the script absent for a moment."""
        with tempfile.TemporaryDirectory() as folder:
            service = self.service(Path(folder) / "not-there-yet.py")
            service.start(automatic=True)
            self.assertTrue(service.restart_at)

    def test_a_deliberate_start_is_not_put_on_a_silent_retry(self):
        """A member told "Not installed" should be left alone."""
        with tempfile.TemporaryDirectory() as folder:
            service = self.service(Path(folder) / "not-there.py")
            service.start()
            self.assertEqual(
                0.0, service.restart_at,
                "pressing Start began a retry the member never asked for",
            )

    def test_a_deliberate_start_clears_a_stale_due_timestamp(self):
        """The case the clear at the top of start() actually protects.

        A service sitting in backoff has restart_at in the past by the
        time the member presses Start. If that stale value survives a
        failed deliberate start, tick sees an armed and already-due
        timestamp and fires a retry the member never asked for.
        """
        with tempfile.TemporaryDirectory() as folder:
            service = self.service(Path(folder) / "gone.py")
            service.restart_at = self.companion.time.monotonic() - 500
            service.start()
            now = self.companion.time.monotonic()
            self.assertFalse(
                service.restart_at and service.restart_at <= now,
                "a stale due timestamp survived, so tick would fire a "
                "retry the member never asked for",
            )

    def test_repeated_launch_failures_still_reach_the_give_up(self):
        """Otherwise this trades a spin for an unbounded quiet retry."""
        with tempfile.TemporaryDirectory() as folder:
            service = self.service(Path(folder) / "gone.py")
            for _ in range(self.companion.RESTART_GIVE_UP_AFTER + 1):
                service.start(automatic=True)
            self.assertEqual(0.0, service.restart_at)
            said = " ".join(
                str(call.args[0])
                for call in service.set_status.call_args_list
            )
            self.assertIn("keeps stopping", said)


class DiscordWorkerControlTests(unittest.TestCase):
    """The Discord listener is controlled without touching live services."""

    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    @staticmethod
    def response(payload):
        response = mock.MagicMock()
        response.read.return_value = json.dumps(payload).encode("ascii")
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        return response

    def spec(self):
        return next(
            spec
            for spec in self.companion.SERVICES
            if spec.key == "voice_discord"
        )

    def service(self):
        window = SimpleNamespace(
            preview=False,
            append=mock.Mock(),
            update_service_signal=mock.Mock(),
        )
        return self.companion.Service(spec=self.spec(), window=window)

    def test_status_request_is_a_validated_loopback_get(self):
        response = self.response(
            {
                "state": "running",
                "healthy": True,
                "configured": True,
                "listening": True,
                "detail": "ready",
            }
        )
        with mock.patch.object(
            self.companion.urllib_request,
            "urlopen",
            return_value=response,
        ) as opened:
            snapshot = self.companion.discord_worker_request()

        request = opened.call_args.args[0]
        headers = {key.casefold(): value for key, value in request.header_items()}
        self.assertEqual("GET", request.get_method())
        self.assertEqual(
            self.companion.DISCORD_WORKER_CONSOLE_URL,
            request.full_url,
        )
        self.assertEqual("1", headers["x-vemals-ears-console"])
        self.assertEqual("running", snapshot.state)
        self.assertTrue(snapshot.healthy)
        self.assertTrue(snapshot.configured)
        self.assertTrue(snapshot.listening)

    def test_start_and_stop_requests_send_only_the_requested_action(self):
        for action, state in (("start", "running"), ("stop", "stopped")):
            with self.subTest(action=action):
                response = self.response(
                    {
                        "state": state,
                        "healthy": action == "start",
                        "configured": True,
                        "listening": False,
                        "detail": action + "ed",
                    }
                )
                with mock.patch.object(
                    self.companion.urllib_request,
                    "urlopen",
                    return_value=response,
                ) as opened:
                    self.companion.discord_worker_request(action)

                request = opened.call_args.args[0]
                headers = {
                    key.casefold(): value
                    for key, value in request.header_items()
                }
                self.assertEqual("POST", request.get_method())
                self.assertEqual(
                    {"action": action},
                    json.loads(request.data.decode("ascii")),
                )
                self.assertEqual("1", headers["x-vemals-ears-console"])
                self.assertEqual("application/json", headers["content-type"])

    def test_malformed_worker_snapshots_are_rejected(self):
        invalid_payloads = (
            {"state": "teleporting", "healthy": True, "configured": True},
            {"state": "running", "healthy": "yes", "configured": True},
            {
                "state": "running",
                "healthy": True,
                "configured": True,
                "listening": "yes",
            },
            {
                "state": "stopped",
                "healthy": False,
                "configured": True,
                "listening": True,
            },
            ["running", True, True],
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), mock.patch.object(
                self.companion.urllib_request,
                "urlopen",
                return_value=self.response(payload),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "unreadable Discord status",
                ):
                    self.companion.discord_worker_request()

    def test_legacy_worker_status_is_explicitly_unknown(self):
        payload = {"state": "running", "healthy": True, "configured": True}
        with mock.patch.object(
            self.companion.urllib_request,
            "urlopen",
            return_value=self.response(payload),
        ):
            snapshot = self.companion.discord_worker_request()
        self.assertIsNone(snapshot.listening)

    def test_legacy_stopped_worker_is_explicitly_inactive(self):
        payload = {"state": "stopped", "healthy": False, "configured": True}
        with mock.patch.object(
            self.companion.urllib_request,
            "urlopen",
            return_value=self.response(payload),
        ):
            snapshot = self.companion.discord_worker_request()
        self.assertFalse(snapshot.listening)

    def test_wind_down_may_still_report_listening(self):
        payload = {
            "state": "stop_pending",
            "healthy": False,
            "configured": True,
            "listening": True,
        }
        with mock.patch.object(
            self.companion.urllib_request,
            "urlopen",
            return_value=self.response(payload),
        ):
            snapshot = self.companion.discord_worker_request()
        self.assertTrue(snapshot.listening)

    def test_windows_transition_states_are_valid_snapshots(self):
        for state in ("start_pending", "stop_pending", "not installed"):
            payload = {
                "state": state,
                "healthy": False,
                "configured": True,
                "listening": False,
            }
            with self.subTest(state=state), mock.patch.object(
                self.companion.urllib_request,
                "urlopen",
                return_value=self.response(payload),
            ):
                snapshot = self.companion.discord_worker_request()
            self.assertEqual(state, snapshot.state)

    def test_network_failures_are_redacted_for_the_status_row(self):
        private_detail = "http://private-host.example/secret"
        failure = self.companion.urllib_error.URLError(private_detail)
        with mock.patch.object(
            self.companion.urllib_request,
            "urlopen",
            side_effect=failure,
        ):
            with self.assertRaises(RuntimeError) as raised:
                self.companion.discord_worker_request()

        self.assertEqual(
            "Vemal's Ears console is unavailable.",
            str(raised.exception),
        )
        self.assertNotIn(private_detail, str(raised.exception))

    def test_window_shutdown_does_not_control_the_external_worker(self):
        service = self.service()
        with mock.patch.object(
            self.companion,
            "discord_worker_request",
        ) as request_worker, mock.patch.object(
            self.companion.subprocess,
            "Popen",
        ) as launch:
            service.stop()

        launch.assert_not_called()
        request_worker.assert_not_called()

    def test_duplicate_control_clicks_share_one_background_operation(self):
        service = self.service()
        thread = mock.Mock()
        with mock.patch.object(
            self.companion.threading,
            "Thread",
            return_value=thread,
        ) as thread_type, mock.patch.object(
            self.companion,
            "discord_worker_request",
        ) as request_worker:
            service.request_start()
            service.request_start()
            service.request_stop()
            thread_type.assert_called_once()
            thread.start.assert_called_once_with()
            request_worker.assert_not_called()
            thread_type.call_args.kwargs["target"]()
            request_worker.assert_called_once_with("start")


class LocalServiceStopTests(unittest.TestCase):
    """Explicit Stop never waits for taskkill on the Tk event thread."""

    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    def service(self):
        spec = next(
            spec for spec in self.companion.SERVICES if spec.key == "voice"
        )
        window = SimpleNamespace(
            preview=False,
            append=mock.Mock(),
            update_service_signal=mock.Mock(),
            refresh_member_caption_button=mock.Mock(),
            member_caption_button=None,
        )
        return self.companion.Service(spec=spec, window=window)

    def test_stop_runs_the_process_wait_behind_the_result_queue(self):
        service = self.service()
        process = mock.Mock()
        process.poll.return_value = None
        service.process = process
        thread = mock.Mock()
        with mock.patch.object(
            self.companion.threading,
            "Thread",
            return_value=thread,
        ) as thread_type, mock.patch.object(
            service,
            "terminate_process_tree",
            return_value=True,
        ) as terminate:
            service.request_stop()
            self.assertTrue(service.local_stop_in_flight)
            terminate.assert_not_called()
            thread.start.assert_called_once_with()

            thread_type.call_args.kwargs["target"]()
            terminate.assert_called_once_with(process)
            service.drain()

        self.assertFalse(service.local_stop_in_flight)
        self.assertIsNone(service.process)

    def test_repeated_stop_clicks_share_one_process_wait(self):
        service = self.service()
        service.process = mock.Mock()
        thread = mock.Mock()
        with mock.patch.object(
            self.companion.threading,
            "Thread",
            return_value=thread,
        ) as thread_type:
            service.request_stop()
            service.request_stop()

        thread_type.assert_called_once()
        thread.start.assert_called_once_with()


class ResultMarkerHonestyTests(unittest.TestCase):
    """A marker must never report a failure as a finished result.

    Markers are matched first-substring-wins. A bare "Generated" is a
    substring of "Generated.lua", and the watcher names that file in its
    own error text - so a sim whose result could not be written turned
    the badge SUCCESS green and told the player to reload.
    """

    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    def classify(self, key, line):
        spec = next(s for s in self.companion.SERVICES if s.key == key)
        for marker in spec.markers:
            if marker[0] in line:
                return marker[3]
        return None

    def test_a_failed_write_is_not_reported_as_results_ready(self):
        line = (
            "Gear Helper: unexpected error: PermissionError: [WinError 5] "
            "Access is denied: "
            "'C:/x/Data/.Generated.lua.1234.tmp' -> 'C:/x/Data/Generated.lua'"
        )
        self.assertEqual(
            "attention", self.classify("gear", line),
            "a thrown-away result was reported to the player as ready",
        )

    def test_the_real_success_line_still_reports_done(self):
        line = (
            "Generated 12 recommendations for spec 253. "
            "Use /reload in WoW to load them."
        )
        self.assertEqual("done", self.classify("gear", line))

    def test_the_success_marker_cannot_match_the_result_filename(self):
        """The marker has to stand on its own, not lean on table order.

        Ordering currently rescues this - the attention marker is first -
        but a bare "Generated" is still a substring of "Generated.lua",
        so any future reordering would silently restore the defect. Pin
        the string itself.
        """
        spec = next(s for s in self.companion.SERVICES if s.key == "gear")
        done_markers = [
            marker[0] for marker in spec.markers
            if marker[3] == "done" and "Generated" in marker[0]
        ]
        self.assertTrue(done_markers, "the success marker vanished")
        for marker in done_markers:
            with self.subTest(marker=marker):
                self.assertNotIn(
                    marker,
                    "could not write C:/x/Data/Generated.lua",
                    "this marker matches an error naming the output file",
                )

    def test_any_error_prefixed_line_reaches_attention(self):
        """That prefix is used for three things and all are errors."""
        for tail in (
            "unexpected error: boom",
            "could not read Generated.lua",
            "something about a completed comparison",
        ):
            with self.subTest(tail=tail):
                self.assertEqual(
                    "attention",
                    self.classify("gear", "Gear Helper: " + tail),
                )


class DoneSurvivesOtherWorkTests(unittest.TestCase):
    """"Done" means a result is on disk ready for an explicit WoW action.

    With two requests outstanding, the gear sim finished and five
    seconds later the still-queued vault request printed its cooldown
    line. The badge flipped from "Done" to "Queued", the player was
    never told to reload, and the result sat unimported.
    """

    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    def service(self):
        spec = self.companion.SERVICES[0]
        service = self.companion.Service(spec=spec, window=mock.Mock())
        service.result_badge = mock.Mock()
        service.result_badge_rail = None
        service.status = None
        service.dot = None
        service.rail = None
        service.job_state = "done"
        return service

    def test_a_queued_message_does_not_erase_a_finished_result(self):
        service = self.service()
        service.set_status("Queued behind the hourly limit", self.companion.ACCENT, "queued")
        self.assertEqual(
            "done", service.job_state,
            "a queued line for another request erased the result badge",
        )

    def test_work_starting_does_not_erase_it_either(self):
        service = self.service()
        service.set_status("Starting the gear check", self.companion.WARNING, "checking")
        self.assertEqual("done", service.job_state)

    def test_an_idle_log_line_does_not_claim_the_result_was_loaded(self):
        service = self.service()
        service.set_status("Ready for another request", self.companion.ACCENT, "ready")
        self.assertEqual("done", service.job_state)

    def test_a_failure_clears_it_too(self):
        service = self.service()
        service.set_status("Needs attention - open Activity", self.companion.DANGER, "attention")
        self.assertEqual("attention", service.job_state)


class ActivityLogClearTests(unittest.TestCase):
    """The member can empty the Activity log deliberately.

    MAX_ACTIVITY_LINES bounds the log by dropping the oldest lines, which
    is not the same thing: after a failure the useful move is to empty
    it, retry, and read only what the retry produced. Salvaged from the
    closed UI branch, which was the only place that had a Clear button.
    """

    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    def window(self):
        return SimpleNamespace(log=FakeText(), activity_count=7)

    def test_clearing_empties_the_log(self):
        window = self.window()
        for index in range(5):
            window.log.insert("end", "line %d\n" % index, "body")
        self.companion.CompanionWindow.clear_activity_log(window)
        self.assertEqual("", window.log.get("1.0", "end").strip())

    def test_clearing_resets_the_count(self):
        window = self.window()
        self.companion.CompanionWindow.clear_activity_log(window)
        self.assertEqual(0, window.activity_count)

    def test_clearing_needs_no_summary_widget(self):
        """There is no summary line left to reset.

        Emptying a log is a housekeeping act. It says nothing about where
        a service stands, and the cards go on saying that themselves.
        """
        window = self.window()
        self.assertFalse(hasattr(window, "activity_summary"))
        self.companion.CompanionWindow.clear_activity_log(window)
        self.assertEqual(0, window.activity_count)


class UnrecognisedFailureTests(unittest.TestCase):
    """The fallback scan must not call a measurement a failure.

    Gear Helper is a simulation tool. "error" is its accuracy target, and
    it prints that target on three separate lines during a completely
    normal run. The fallback scan matched the bare substring, so a healthy
    sim painted the card "Needs attention - open Activity" while the
    badges correctly read Running and Checking.

    The scan used to live on the Activity summary line. That line is gone
    - it repeated, word for word, the sentence already on the service card
    above it - so the scan now runs where the card status is set. Same
    judgement, one place to read it.
    """

    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    # Real lines, from gearhelper_auto.py 4505 / 4520 / 4654.
    SIM_PROGRESS = (
        "Screening 214 item and 6 enchant options at 0.6% error before"
        " the precise pass.",
        "Screen kept 12 of 214 items and 2 of 6 enchant options;"
        " refining them at 0.1% error.",
        "Running the precise Great Vault comparison at 0.1% error:"
        " 18 profilesets in parallel on 16 threads.",
    )

    # Real lines, from gearhelper_auto.py 3078 / 3100 / 4972 and
    # jobs_client.py 694 - housekeeping that names its own retry.
    SELF_HANDLED = (
        "2 source-build workspace(s) could not be deleted; something"
        " still has the files open. They will be retried next run.",
        "Could not remove the source-build workspace W; the next run"
        " will sweep it.",
        "The hosted result could not be written yet; retrying.",
        "Gear Helper note: source-build workspace sweep skipped:"
        " could not access path",
        "Skipping optional Enchants.lua: could not read file",
    )

    # These must still escalate - the scan exists to catch a failure the
    # marker table did not anticipate, and giving that up would be worse
    # than the bug being fixed.
    REAL_FAILURES = (
        "Gear Helper: unexpected error: bad profile",
        "Error: talent tree incomplete",
        "Unexpected error: boom",
        "Request failed: HTTP 500",
        "Hosted addon jobs could not read the saved pairing.",
        "Gear Helper exited unexpectedly",
        "Command failed (2): simc",
    )

    def test_simulation_progress_is_not_a_failure(self):
        for line in self.SIM_PROGRESS:
            with self.subTest(line=line):
                self.assertFalse(
                    self.companion.reads_as_a_failure(line),
                    "a sim accuracy target is a measurement, not a fault",
                )

    def test_a_measurement_followed_by_a_colon_is_still_a_measurement(self):
        """The specific case the first attempt at this fix got wrong.

        Keying on "error:" looked safe on the assumption that a
        measurement never carries a colon. Gear Helper's own line has
        one, introducing the profileset count. Punctuation does not
        separate a measured error from a reported one; context does.
        """
        self.assertFalse(
            self.companion.reads_as_a_failure(
                "Running the precise Great Vault comparison at 0.1% error:"
                " 18 profilesets in parallel on 16 threads."
            )
        )

    def test_housekeeping_that_names_its_own_retry_is_not_a_failure(self):
        for line in self.SELF_HANDLED:
            with self.subTest(line=line):
                self.assertFalse(self.companion.reads_as_a_failure(line))

    def test_real_failures_still_escalate(self):
        for line in self.REAL_FAILURES:
            with self.subTest(line=line):
                self.assertTrue(
                    self.companion.reads_as_a_failure(line),
                    "the fallback scan must keep catching genuine faults",
                )

    def spec(self):
        """A Gear Helper spec whose markers match none of the lines used."""
        return SimpleNamespace(
            key="gear",
            label="Gear Helper",
            color=self.companion.ACCENT,
            markers=(("Starting Gear Helper request", "Starting the gear"
                      " check", self.companion.WARNING, "checking"),),
            auto_restart=False,
            external_control=False,
        )

    def drain_one(self, line):
        """Push one child line through a real Service and report the card."""
        window = SimpleNamespace(
            hosted_edition=False,
            result_carrier_available=True,
            append=lambda *_args, **_kwargs: None,
        )
        service = self.companion.Service(spec=self.spec(), window=window)
        process = object()
        service.process = process
        service.set_status = mock.Mock()
        service.lines.put((process, line + "\n"))
        service.drain()
        return service.set_status

    def test_a_sim_progress_line_leaves_the_card_alone(self):
        """End to end: the card must not change on routine progress."""
        self.drain_one(self.SIM_PROGRESS[2]).assert_not_called()

    def test_a_real_failure_still_reaches_the_card(self):
        set_status = self.drain_one(
            "Gear Helper: unexpected error: bad profile"
        )
        set_status.assert_called_once_with(
            "Needs attention - open Activity",
            self.companion.DANGER,
            "attention",
        )

    def test_a_matched_marker_wins_over_the_fallback(self):
        """The scan is a last resort, not a second opinion.

        This line carries both a marker the table knows and a word the
        scan would escalate on. The named status is the better one, and
        it has to be the one that lands.
        """
        set_status = self.drain_one(
            "Starting Gear Helper request after the last error"
        )
        set_status.assert_called_once_with(
            "Starting the gear check",
            self.companion.WARNING,
            "checking",
        )


class BuildLineTests(unittest.TestCase):
    """Preferences must say which app this is, and how old it is.

    Three installs of this app live on one machine - the full Downtime
    Edition, the Vemal's Ears Member Edition, and an older member copy.
    Working out which one was open, and whether it carried a given fix,
    meant reading CLIENT_VERSION out of three different client.py files.
    """

    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    def window(self, *, member=False, hosted=False):
        return SimpleNamespace(member_edition=member, hosted_edition=hosted)

    def line(self, **kwargs):
        return self.companion.CompanionWindow.build_line(self.window(**kwargs))

    def test_the_version_is_the_one_the_client_reports(self):
        from GuildHub.version import CLIENT_VERSION

        self.assertEqual(CLIENT_VERSION, self.companion.shipped_version())
        self.assertIn(CLIENT_VERSION, self.line())

    def test_each_edition_names_itself(self):
        # Product names, capitalised. The full build is the Server
        # Edition - it is the one that runs the guild's services - and
        # "full edition" was this label's own coinage rather than a name
        # anything else in the product used.
        self.assertIn("Server Edition", self.line())
        self.assertIn("Member Edition", self.line(member=True))
        self.assertIn("Hosted Edition", self.line(hosted=True))

    def test_the_server_build_is_never_called_full(self):
        """Guards the correction, not just the current string."""
        self.assertNotIn("full", self.line().casefold())

    def test_the_member_line_says_vemals_ears_not_downtime(self):
        """The editions answer to different names on disk and on screen.

        Only the Server Edition has Gear Helper, so a bug report naming it
        is never about the member copy - but only if the panel says which
        one you have.
        """
        member = self.line(member=True)
        self.assertIn("Vemal's Ears", member)
        self.assertNotIn("Downtime", member)
        self.assertIn("Downtime", self.line())

    def test_a_label_never_stops_the_panel_opening(self):
        """shipped_version falls back rather than raising.

        It is read lazily from a module the narrow bundles have to carry
        deliberately. If some future bundle forgets it, Preferences must
        still open - a missing version line is a small problem, a
        preferences panel that raises is a large one.
        """
        with mock.patch.dict("sys.modules", {"GuildHub.version": None}):
            self.assertEqual("unknown", self.companion.shipped_version())


class OneVersionOnlyTests(unittest.TestCase):
    """There must be exactly one place the client version is written.

    There were two. GuildHub/__init__.py said 0.1.0 through two releases
    while GuildHub/client.py moved to 0.2.2, because the release tooling
    only ever updated one of them. Nothing imported the stale one, so
    nothing caught it - which is precisely why this is a test and not a
    comment.
    """

    ROOT = Path(__file__).resolve().parent.parent

    def definitions(self):
        found = []
        for path in self.ROOT.rglob("*.py"):
            parts = path.relative_to(self.ROOT).parts
            # Archived-source rebuild evidence contains independent copies.
            # It is generated output, just like dist and build.
            if {"dist", "build", "node_modules", ".ci-results"}.intersection(parts):
                continue
            for number, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
            ):
                if line.startswith("CLIENT_VERSION = "):
                    found.append("%s:%d" % (path.relative_to(self.ROOT), number))
        return found

    def test_generated_source_evidence_does_not_hide_a_real_duplicate(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for relative in ("GuildHub/version.py", ".ci-results/rebuild/GuildHub/version.py"):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('CLIENT_VERSION = "1.2.3"\n', encoding="ascii")
            with mock.patch.object(self, "ROOT", root):
                self.assertEqual(1, len(self.definitions()))
                (root / "other.py").write_text('CLIENT_VERSION = "1.2.3"\n', encoding="ascii")
                self.assertEqual(2, len(self.definitions()))

    def test_exactly_one_definition_exists(self):
        found = self.definitions()
        self.assertEqual(
            1,
            len(found),
            "the client version must be written in exactly one place; found: "
            + ", ".join(found),
        )

    def test_it_is_the_one_the_release_tooling_rewrites(self):
        """A second copy is only harmless until the tooling misses it."""
        found = self.definitions()
        self.assertTrue(
            found and found[0].replace("\\", "/").startswith("GuildHub/version.py"),
            "packaging/publish_release.py rewrites GuildHub/version.py; the "
            "definition must live there or a release will silently ship the "
            "old number. Found: " + ", ".join(found),
        )


class HostedPairingStatusTests(unittest.TestCase):
    def test_missing_or_damaged_downtime_pairing_reaches_each_job_badge(self):
        companion = load_companion()
        for spec in companion.service_specs_for_edition(companion.HOSTED_EDITION):
            for line, expected in (
                ("Hosted addon jobs are waiting for Downtime pairing.", "Pair this computer"),
                ("Hosted addon jobs could not read the saved Downtime pairing.",
                 "Pairing needs attention - open Activity"),
            ):
                with self.subTest(service=spec.key, line=line):
                    process = mock.Mock()
                    status = mock.Mock()
                    badge = mock.Mock()
                    service = companion.Service(
                        spec=spec,
                        window=SimpleNamespace(
                            append=mock.Mock(), hosted_edition=True,
                            update_service_signal=mock.Mock(),
                        ),
                        process=process, status=status, result_badge=badge,
                        job_state="ready",
                    )
                    service.lines.put((process, line + "\n"))
                    service.drain()
                    status.config.assert_called_once_with(text=expected)
                    self.assertEqual("attention", service.job_state)
                    self.assertEqual("Needs attention", badge.config.call_args.kwargs["text"])


class AppServiceStartupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    def test_each_product_starts_only_its_own_selected_services(self):
        for edition, expected in (
            ("member", ["voice"]),
            ("full-local", ["gear", "talents"]),
            ("downtime-hosted", ["gear", "talents"]),
        ):
            with self.subTest(edition=edition):
                services = [
                    SimpleNamespace(
                        spec=spec, start=mock.Mock(), request_external_status=mock.Mock(),
                    )
                    for spec in self.companion.service_specs_for_edition(edition)
                ]
                window = SimpleNamespace(
                    member_edition=edition == "member", services=services,
                )
                self.companion.CompanionWindow.start_launch_services(window)
                self.assertEqual(expected, [service.spec.key for service in services])
                for service in services:
                    service.start.assert_called_once_with()
                    service.request_external_status.assert_not_called()

    def test_downtime_refuses_stale_voice_services_even_when_injected(self):
        for key in ("voice", "voice_discord"):
            with self.subTest(service=key):
                service = SimpleNamespace(
                    spec=SimpleNamespace(key=key, start_on_launch=True, external_control=True),
                    start=mock.Mock(),
                    request_external_status=mock.Mock(),
                )
                window = SimpleNamespace(member_edition=False, services=[service])
                self.companion.CompanionWindow.start_launch_services(window)
                service.start.assert_not_called()
                service.request_external_status.assert_not_called()


class GuildPrivacyTests(unittest.TestCase):
    """The Guild & Privacy card opens the paired guild's own document."""

    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    class Store:
        def __init__(self, url):
            self._url = url

        def load(self):
            if self._url is None:
                return None
            return SimpleNamespace(server_url=self._url, guild_name="Guild")

    def test_the_url_comes_from_the_pairing_not_a_hardcoded_host(self):
        url = self.companion.paired_privacy_url(
            credential_store=self.Store("wss://ears.example.org")
        )
        self.assertEqual("https://ears.example.org/privacy", url)

    def test_a_loopback_or_plain_ws_pairing_offers_no_public_page(self):
        for server in ("ws://127.0.0.1:8770", "", "not a url"):
            with self.subTest(server=server):
                self.assertEqual(
                    "",
                    self.companion.paired_privacy_url(
                        credential_store=self.Store(server)
                    ),
                )

    def test_unpaired_says_so_on_the_card_instead_of_doing_nothing(self):
        """A button that silently does nothing reads as broken."""
        label = mock.Mock()
        window = SimpleNamespace(
            preview=False,
            member_guild_label=label,
        )
        with mock.patch.object(
            self.companion, "paired_privacy_url", return_value=""
        ):
            self.companion.CompanionWindow.open_guild_privacy(window)
        label.config.assert_called_once()
        said = label.config.call_args.kwargs.get("text", "")
        self.assertIn("Pair with your guild first", said)

    def test_the_card_title_is_guild_and_privacy_capital_p(self):
        source = Path(self.companion.__file__).read_text(encoding="utf-8")
        self.assertIn('"Guild & Privacy"', source)
        self.assertNotIn('"Guild & privacy"', source)


class StructuredStatusTests(unittest.TestCase):
    """The @ghstatus channel: the child says what happened, the window
    stops guessing it from prose.

    Both prior misclassification bugs in this window came from the
    prose path being asked to carry the whole load, so the contract
    here is two-sided: structured lines win, and anything malformed
    falls through to being visible prose rather than vanishing.
    """

    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    def gear_service(self):
        window = SimpleNamespace(
            hosted_edition=False,
            result_carrier_available=True,
            append=mock.Mock(),
        )
        service = self.companion.Service(
            spec=next(
                spec for spec in self.companion.SERVICES if spec.key == "gear"
            ),
            window=window,
        )
        process = object()
        service.process = process
        service.set_status = mock.Mock()
        return service, process, window

    def status_line(self, **fields):
        payload = {"schema": 1, "phase": "running", "requestID": "r-1"}
        payload.update(fields)
        return self.companion.GHSTATUS_PREFIX + json.dumps(payload) + "\n"

    def test_each_phase_drives_the_badge_without_marker_guessing(self):
        service, process, _window = self.gear_service()
        cases = (
            ({"phase": "running"},
             ("Simulating - a few minutes", self.companion.WARNING, "checking")),
            ({"phase": "done"},
             ("Results ready - reload WoW", self.companion.SUCCESS, "done")),
            ({"phase": "failed",
              "errorCode": "sim_failed",
              "detail": "SimulationCraft exited 1."},
             ("Failed - SimulationCraft exited 1.",
              self.companion.DANGER, "attention")),
        )
        for fields, expected in cases:
            with self.subTest(phase=fields["phase"]):
                service.set_status.reset_mock()
                service.lines.put((process, self.status_line(**fields)))
                service.drain()
                service.set_status.assert_called_once_with(*expected)

    def test_a_queued_status_counts_down_the_cooldown(self):
        service, process, _window = self.gear_service()
        service.lines.put((process, self.status_line(
            phase="queued", cooldownUntil=time.time() + 1800,
        )))
        service.drain()
        text, color, state = service.set_status.call_args[0]
        import re as _re
        minutes = _re.search(r"(" + chr(92) + "d+) min", text)
        self.assertIsNotNone(minutes, text)
        # Floor division against a live clock: 1800s becomes 29 min
        # the moment a fraction of a second passes. Either is honest.
        self.assertIn(int(minutes.group(1)), (29, 30), text)
        self.assertEqual("queued", state)
        self.assertEqual(self.companion.LITE_ACCENT, color)

    def test_machine_lines_stay_out_of_activity(self):
        """The human sentence for the same event is printed beside the
        payload, so appending both would say everything twice - once as
        prose and once as JSON noise."""
        service, process, window = self.gear_service()
        service.lines.put((process, self.status_line()))
        service.drain()
        window.append.assert_not_called()

    def test_malformed_status_falls_through_to_prose_not_silence(self):
        service, process, window = self.gear_service()
        for bad in (
            self.companion.GHSTATUS_PREFIX + "{truncated\n",
            self.companion.GHSTATUS_PREFIX + '{"schema": 2, "phase": "running"}\n',
            self.companion.GHSTATUS_PREFIX + '{"schema": 1, "phase": "reticulating"}\n',
            self.companion.GHSTATUS_PREFIX + '"not an object"\n',
        ):
            with self.subTest(line=bad):
                window.append.reset_mock()
                service.set_status.reset_mock()
                service.lines.put((process, bad))
                service.drain()
                # Visible in Activity as an ordinary line, and no badge
                # state invented for it.
                window.append.assert_called_once()
                service.set_status.assert_not_called()

    def test_prose_markers_still_work_for_children_without_the_channel(self):
        service, process, _window = self.gear_service()
        service.lines.put(
            (process, "Watching Gear Helper SavedVariables. Press Ctrl+C to stop.\n")
        )
        service.drain()
        service.set_status.assert_called_once_with(
            "Watching for a sim request", self.companion.ACCENT, "ready"
        )


class ConsumedMemberAutostartTests(unittest.TestCase):
    """Shino, 2026-09-02, screen 2: "Open at sign-in" would not stay on.

    His Run key held the MEMBER launcher under the FULL edition's value
    name - the residue of the consolidation that retires a standalone
    Vemal's Ears install. He confirmed that consolidation is intended:
    "Yes the full edition should consume the autostart." Consuming it
    has to mean REPLACING it. Before this the full edition did not
    recognise that command as its own, so migrate_autostart refused to
    touch it and set_autostart - which writes only when the name is
    absent - left the switch stuck off, silently.
    """

    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    def setUp(self):
        self.local = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.local, True)
        patcher = mock.patch.dict(
            os.environ, {"LOCALAPPDATA": str(self.local)}, clear=False
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        launcher = self.local / "Programs" / "VemalsEars"
        launcher.mkdir(parents=True, exist_ok=True)
        (launcher / "VemalsEars.exe").write_bytes(b"launcher")

    def member_command(self):
        return self.companion.installed_member_autostart_command()

    def test_the_full_edition_does_not_own_a_member_entry(self):
        value = self.member_command()
        self.assertIsNotNone(value)
        self.assertFalse(
            self.companion.autostart_value_is_owned(value, member_edition=False),
            "Downtime cannot replace Vemal's Ears startup",
        )

    def test_the_member_edition_does_not_own_the_full_name(self):
        # Only the full edition consumes the sibling. A member install
        # claiming the other direction would let it clobber Downtime.
        self.assertFalse(
            self.companion.autostart_value_is_owned(
                self.member_command(), member_edition=True
            )
        )

    def test_a_genuinely_foreign_value_is_still_not_ours(self):
        # The refusal exists for a reason: an unrelated program parked
        # under the same name must never be overwritten.
        self.assertFalse(
            self.companion.autostart_value_is_owned(
                '"C:/Program Files/Something Else/other.exe"',
                member_edition=False,
            )
        )

    def test_the_sibling_test_and_the_owner_test_now_agree(self):
        # They disagreed before, and that disagreement WAS the bug: the
        # sibling side consumed the value while the owner side called it
        # foreign, so migrate_autostart bailed on a value it had itself
        # decided to take over.
        value = self.member_command()
        self.assertEqual(
            self.companion.sibling_autostart_value_is_owned(value, False),
            self.companion.autostart_value_is_owned(value, member_edition=False),
        )




if __name__ == "__main__":
    unittest.main()
