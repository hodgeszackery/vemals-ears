"""The independent member package and refresh boundary."""
import ast
import contextlib
import io
import json
import re
import sys
import subprocess
import shutil
import tempfile
import unittest
from unittest import mock
import zipfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'packaging'))
import build_release
import build_runtime
import refresh_local_copy
import publish_release
import source_distribution
import runtime_licenses
import runtime_sources
import verify_bundle
from tests.test_runtime_sources import write_source_fixture


class CorrespondingSourceTests(unittest.TestCase):
    def seed_source(self, root):
        files = source_distribution.snapshot(ROOT, build_release.WINDOWS_INSTALLER_APP_FILES)
        source_distribution.write_source_tree(files, root)
        return files

    def test_real_member_archive_carries_license_native_sources_and_matching_application(self):
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.object(build_release, 'DIST', Path(folder)):
                member, missing = build_release.build_member_companion()
            self.assertEqual([], missing)
            source_bytes = (Path(folder) / 'vemals-ears-source.zip').read_bytes()
            with zipfile.ZipFile(member) as package, zipfile.ZipFile(io.BytesIO(source_bytes)) as source:
                self.assertEqual(source_bytes, package.read('VemalsEars/Source/vemals-ears-source.zip'))
                for relative in ('LICENSE', 'docs/SOURCE.txt', 'Installer/THIRD-PARTY-NOTICES.txt',
                                 'Installer/INNO-SETUP-LICENSE.txt'):
                    self.assertEqual((ROOT / relative).read_bytes(), package.read('VemalsEars/' + relative))
                required = ('LICENSE', 'SOURCE-IMPORT.json', 'docs/SOURCE.txt',
                            'build_release.py', 'source_distribution.py',
                            'Installer/launcher.c', 'Installer/launcher.rc', 'Windows/DowntimeLauncher.c',
                            'Installer/VemalsEars.iss', 'Installer/build_installer.ps1',
                            'Installer/stage_installer.py', 'Installer/runtime-lock.json',
                            'Installer/runtime-sources.lock.json', 'packaging/runtime_sources.py',
                            'packaging/build_runtime.py', 'packaging/runtime_licenses.py')
                for relative in required:
                    self.assertEqual((ROOT / relative).read_bytes(), source.read('VemalsEars-source/' + relative))
                for relative in build_release.VEMALS_EARS_MEMBER_APP_FILES:
                    self.assertEqual(package.read('VemalsEars/' + relative), source.read('VemalsEars-source/' + relative))

    def test_member_payload_imports_speech_style_dependencies_without_the_checkout(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with mock.patch.object(build_release, 'DIST', root):
                member, missing = build_release.build_member_companion()
            self.assertEqual([], missing)
            payload = root / 'payload'
            with zipfile.ZipFile(member) as package:
                for relative in build_release.VEMALS_EARS_MEMBER_APP_FILES:
                    destination = payload / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(package.read('VemalsEars/' + relative))
            probe = "\n".join((
                "import pathlib, sys",
                "root = pathlib.Path(sys.argv[1]).resolve()",
                "sys.path.insert(0, str(root))",
                "from VoiceCaptions.events import CaptionEvent",
                "from VoiceCaptions.prosody import SpeechStyleDetector",
                "assert CaptionEvent.__dataclass_fields__['speech_style']",
                "assert SpeechStyleDetector().classify_and_observe('speaker', -20.0) == 'normal'",
                "for name, module in tuple(sys.modules.items()):",
                "    if name.split('.')[0] == 'VoiceCaptions':",
                "        assert pathlib.Path(module.__file__).resolve().is_relative_to(root), name",
                "print('MEMBER-PROSODY-IMPORT-OK')",
            ))
            result = subprocess.run(
                [sys.executable, '-I', '-S', '-B', '-c', probe, str(payload)],
                cwd=payload, capture_output=True, text=True, timeout=30,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn('MEMBER-PROSODY-IMPORT-OK', result.stdout)

    def test_source_archive_excludes_unlisted_and_operator_files_even_inside_build_roots(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.seed_source(root)
            sentinels = ('GuildHub/.env', 'GuildHub/private-notes.txt', 'Installer/local-signing.json',
                         'Windows/private-key.txt', 'secrets/token.txt', 'dist/old.zip',
                         'node_modules/secret.js', '.git/config', 'GuildHub/live.sqlite3')
            for relative in sentinels:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'CONFIDENTIAL-UNTRACKED-SENTINEL')
            payload = source_distribution.archive_bytes(source_distribution.snapshot(root, build_release.WINDOWS_INSTALLER_APP_FILES))
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                names = archive.namelist()
                for relative in sentinels:
                    self.assertNotIn('VemalsEars-source/' + relative, names)
                self.assertNotIn(b'CONFIDENTIAL-UNTRACKED-SENTINEL', b''.join(archive.read(name) for name in names))

    def test_missing_license_native_source_or_build_script_refuses_an_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            files = self.seed_source(root)
            for relative in ('LICENSE', 'Windows/DowntimeLauncher.c', 'Installer/build_installer.ps1',
                             'packaging/runtime_licenses.py', 'packaging/runtime_sources.py',
                             'Installer/runtime-sources.lock.json'):
                with self.subTest(missing=relative):
                    path = root / relative
                    path.unlink()
                    with self.assertRaisesRegex(source_distribution.SourceError, 'missing'):
                        source_distribution.snapshot(root, build_release.WINDOWS_INSTALLER_APP_FILES)
                    path.write_bytes(files[relative])

    def test_identical_source_has_identical_archive_bytes(self):
        files = source_distribution.snapshot(ROOT, build_release.WINDOWS_INSTALLER_APP_FILES)
        self.assertEqual(source_distribution.archive_bytes(files), source_distribution.archive_bytes(dict(reversed(list(files.items())))))

    def test_modified_extracted_source_rebuilds_member_and_installer_payload_without_git(self):
        files = source_distribution.snapshot(ROOT, build_release.WINDOWS_INSTALLER_APP_FILES)
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder)
            with zipfile.ZipFile(io.BytesIO(source_distribution.archive_bytes(files))) as archive:
                archive.extractall(target)
            root = target / 'VemalsEars-source'
            self.assertFalse((root / '.git').exists())
            app = root / 'WoW-Companion.pyw'
            app.write_bytes(app.read_bytes() + b'\n# Recipient modification for corresponding-source rebuild test.\n')
            native = root / 'Installer/launcher.c'
            native.write_bytes(native.read_bytes() + b'\n/* Recipient native launcher modification. */\n')
            version = (root / 'GuildHub/version.py').read_text(encoding='ascii')
            version = re.search(r'^CLIENT_VERSION = "([^"]+)"', version, re.MULTILINE).group(1)
            commands = (
                [sys.executable, '-E', '-s', '-B', str(root / 'source_distribution.py')],
                [sys.executable, '-E', '-s', '-B', str(root / 'build_release.py'), '--member'],
                [sys.executable, '-E', '-s', '-B', str(root / 'Installer/stage_installer.py'),
                 '--source', str(root), '--stage', str(target / 'installer-stage'), '--version', version],
            )
            for command in commands:
                result = subprocess.run(command, cwd=target, capture_output=True, text=True, timeout=45)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            with zipfile.ZipFile(root / 'dist/vemals-ears-member.zip') as package:
                self.assertEqual(app.read_bytes(), package.read('VemalsEars/WoW-Companion.pyw'))
                with zipfile.ZipFile(io.BytesIO(package.read('VemalsEars/Source/vemals-ears-source.zip'))) as source:
                    self.assertEqual(app.read_bytes(), source.read('VemalsEars-source/WoW-Companion.pyw'))
            stage = target / 'installer-stage'
            self.assertEqual(app.read_bytes(), (stage / 'app/WoW-Companion.pyw').read_bytes())
            self.assertEqual(native.read_bytes(), (stage / 'source/Installer/launcher.c').read_bytes())
            with zipfile.ZipFile(stage / 'app/Source/vemals-ears-source.zip') as source:
                for relative in ('Installer/launcher.c', 'Installer/launcher.rc', 'Windows/DowntimeLauncher.c', 'Installer/VemalsEars.iss'):
                    self.assertEqual((stage / 'source' / relative).read_bytes(), source.read('VemalsEars-source/' + relative))
            self.assertIsNone(json.loads((stage / 'payload-manifest.json').read_text())['source_commit'])

    def test_member_builder_refuses_payload_bytes_that_differ_from_embedded_source(self):
        original = zipfile.ZipFile.writestr
        def corrupt(archive, name, data, *args, **kwargs):
            if name == 'VemalsEars/WoW-Companion.pyw':
                data = data + b'\n# mismatched payload\n'
            return original(archive, name, data, *args, **kwargs)
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(build_release, 'DIST', Path(folder)), \
                mock.patch.object(zipfile.ZipFile, 'writestr', corrupt):
            with self.assertRaisesRegex(source_distribution.SourceError, 'payload differs'):
                build_release.build_member_companion()


class IndependentReleaseTests(unittest.TestCase):
    def test_member_refresh_uses_exactly_the_member_application_allowlist(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder)
            (target / 'vemals-ears-member.json').write_text('{}', encoding='ascii')
            files = refresh_local_copy.shipped_files(target)
            self.assertEqual(set(build_release.VEMALS_EARS_MEMBER_APP_FILES), set(files))
            self.assertEqual(len(files), len(set(files)))
            self.assertTrue(refresh_local_copy.is_member_copy(target))

    def test_member_refresh_never_copies_host_or_addon_tools(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder)
            (target / 'vemals-ears-member.json').write_text('{}', encoding='ascii')
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, refresh_local_copy.refresh(target))
            files = {path.relative_to(target).as_posix() for path in target.rglob('*') if path.is_file()}
            self.assertIn('GuildHub/client.py', files)
            self.assertIn('VoiceCaptions/member_overlay.py', files)
            for relative in files:
                for forbidden in ('discord_adapter/', 'VoiceCaptions/stt/', 'GuildHub/host.py',
                                  'GuildHub/server.py', 'GuildHub/worker_launcher.py',
                                  'VoiceCaptions/coordinator.py', 'VoiceCaptions/voice_captions.py',
                                  'GearHelper/', 'BossTalents/', 'Warpsim/'):
                    self.assertNotIn(forbidden, relative)

    def test_a_damaged_member_marker_still_selects_only_member_files(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder)
            marker = target / 'vemals-ears-member.json'
            marker.write_text('{ truncated', encoding='ascii')
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, refresh_local_copy.refresh(target))
            files = {path.relative_to(target).as_posix() for path in target.rglob('*') if path.is_file()}
            self.assertEqual(set(build_release.VEMALS_EARS_MEMBER_APP_FILES) |
                             {marker.name, 'Source/vemals-ears-source.zip'}, files)
            self.assertEqual('{ truncated', marker.read_text(encoding='ascii'))

    def test_conflicting_product_markers_refuse_refresh_before_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder)
            for name in ('vemals-ears-member.json', 'downtime-hosted.json'):
                (target / name).write_text('{}', encoding='ascii')
            before = {path.name: path.read_bytes() for path in target.iterdir()}
            with mock.patch.object(refresh_local_copy.shutil, 'copy2') as copy:
                with self.assertRaises(SystemExit):
                    refresh_local_copy.refresh(target)
            copy.assert_not_called()
            self.assertEqual(before, {path.name: path.read_bytes() for path in target.iterdir()})

    def test_foreign_extraction_is_refused_before_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder)
            with mock.patch.object(refresh_local_copy.shutil, "copy2") as copy:
                with self.assertRaisesRegex(SystemExit, "only a Vemals Ears"):
                    refresh_local_copy.refresh(target)
            copy.assert_not_called()
            self.assertEqual([], list(target.iterdir()))

    def test_publication_owns_only_ve_artifacts_and_never_invokes_downtime(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            dist = root / "dist"
            downloads = root / "downloads"
            dist.mkdir()
            downloads.mkdir()
            installer = dist / "Vemals-Ears-Setup.exe"
            installer.write_bytes(b"MZ isolated installer")
            (dist / "vemals-ears-member.zip").write_bytes(b"member archive")
            existing = downloads / "downtime.zip"
            existing.write_bytes(b"independent downtime release")
            with mock.patch.object(publish_release, "DIST", dist), \
                    mock.patch.object(publish_release, "INSTALLER", installer), \
                    mock.patch.object(publish_release, "client_version", return_value="0.5.8"), \
                    mock.patch.object(publish_release, "stamped_version", return_value="0.5.8"), \
                    mock.patch.object(publish_release, "run") as run, \
                    mock.patch.object(publish_release, "console") as console, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, publish_release.publish("0.5.8", False, advertise=False, downloads=downloads))
            commands = [call.args[1] for call in run.call_args_list]
            self.assertEqual([sys.executable, "build_release.py", "--member", "--require-runtime"], commands[1])
            self.assertFalse(any("--companion" in command or "--hosted" in command for command in commands))
            console.assert_not_called()
            self.assertEqual(b"independent downtime release", existing.read_bytes())
            self.assertEqual(installer.read_bytes(), (downloads / installer.name).read_bytes())
            self.assertEqual(b"member archive", (downloads / "vemals-ears-member.zip").read_bytes())

    def test_release_version_must_already_be_reviewed(self):
        with mock.patch.object(publish_release, "client_version", return_value="0.5.8"), \
                mock.patch.object(publish_release, "run") as run:
            with self.assertRaisesRegex(SystemExit, "already match"):
                publish_release.publish("0.5.9", True)
        run.assert_not_called()


class ReleaseSplit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        with mock.patch.object(build_release, 'DIST', Path(cls._tmp.name)):
            cls.member = build_release.build_member_companion()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_member_bundle_is_exactly_the_thin_member_allowlist(self):
        path, missing = self.member
        self.assertFalse(missing, "member bundle is missing: " + str(missing))
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
        expected = {
            "VemalsEars/" + relative
            for relative in build_release.VEMALS_EARS_MEMBER_APP_FILES
        }
        expected.update(
            {
                "VemalsEars/vemals-ears-member.json",
                "VemalsEars/HOW-TO-RUN.txt",
                "VemalsEars/Source/vemals-ears-source.zip",
            }
        )
        # Every allowlisted file is present, and the only thing permitted
        # beyond the allowlist is the embedded interpreter and its
        # launcher. Strict set equality was the original rule here; it
        # cannot survive a bundled runtime, so the rule is now stated as
        # what it always meant - no APPLICATION file arrives that the
        # allowlist did not name.
        self.assertFalse(expected - names, "member allowlist files missing")
        extra = {
            name for name in names - expected
            if not name.startswith("VemalsEars/runtime/")
            and name != "VemalsEars/Start-Vemals-Ears.cmd"
        }
        self.assertFalse(extra, "member bundle carries unlisted files: "
                         + str(sorted(extra)))
        self.assertNotIn("VemalsEars/GearHelper/Tools/gearhelper_auto.py", names)
        self.assertNotIn("VemalsEars/GearHelper/Tools/raidbots_import.py", names)
        self.assertNotIn("VemalsEars/BossTalents/Tools/boss_talents_auto.py", names)
        self.assertNotIn("VemalsEars/GuildHub/console.py", names)
        self.assertNotIn("VemalsEars/VoiceCaptions/README.txt", names)


    def test_member_bundle_has_marker_and_complete_source_setup_instructions(self):
        path, missing = self.member
        self.assertFalse(missing, "member bundle is missing: " + str(missing))
        with zipfile.ZipFile(path) as archive:
            marker = json.loads(
                archive.read("VemalsEars/vemals-ears-member.json").decode("ascii")
            )
            instructions = archive.read("VemalsEars/HOW-TO-RUN.txt").decode("ascii")
        self.assertEqual({"edition": "member", "schema": 1}, marker)
        self.assertIn("--require-hashes", instructions)
        self.assertIn("GuildHub\\requirements-client.txt", instructions)


    @unittest.skipUnless(sys.platform == "win32", "the desktop UI uses Windows APIs")
    def test_extracted_products_construct_their_window_and_preferences(self):
        probe = """import os
import runpy
import sys
from pathlib import Path
from unittest import mock

root_path = Path(sys.argv[1])
sys.path.insert(0, str(root_path))
os.environ['LOCALAPPDATA'] = str(root_path / '_isolated_state')
application = runpy.run_path(str(root_path / 'WoW-Companion.pyw'))
edition = application['resolve_edition']()
application['configure_application'](edition)
tk = application['tk']
root = tk.Tk()
root.withdraw()
original_toplevel_init = tk.Toplevel.__init__

def hidden_toplevel(self, *args, **kwargs):
    original_toplevel_init(self, *args, **kwargs)
    self.withdraw()

try:
    with mock.patch.object(root, 'deiconify'), \\
         mock.patch.object(tk.Toplevel, '__init__', hidden_toplevel), \\
         mock.patch.object(tk.Toplevel, 'lift'), \\
         mock.patch.object(tk.Toplevel, 'focus_force'):
        window = application['CompanionWindow'](root, edition=edition, preview=True)
        preferences = application['SettingsMenu'](window, root)
        root.update_idletasks()
        keys = [service.spec.key for service in window.services]
        if edition == application['MEMBER_EDITION']:
            assert keys == ['voice'], keys
            assert window.caption_suite
            assert (root_path / 'Vemals-Ears.pyw').is_file()
        else:
            assert keys == ['gear', 'talents'], keys
            assert not window.caption_suite
            assert 'GuildHub.client' not in sys.modules
            assert 'VoiceCaptions.overlay' not in sys.modules
            assert 'VoiceCaptions.member_overlay' not in sys.modules
            assert 'VoiceCaptions.settings' not in sys.modules
        preferences.destroy()
        print('PRODUCT-PREVIEW-OK', edition)
finally:
    root.destroy()
"""
        for bundle, product in (
            (self.member, "VemalsEars"),
        ):
            with self.subTest(product=product), tempfile.TemporaryDirectory() as folder:
                with zipfile.ZipFile(bundle[0]) as archive:
                    archive.extractall(folder)
                completed = subprocess.run(
                    [sys.executable, "-I", "-B", "-c", probe, str(Path(folder) / product)],
                    cwd=folder,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
                self.assertIn("PRODUCT-PREVIEW-OK", completed.stdout)


class RuntimeEmbedding(unittest.TestCase):
    """The bundle is self-contained when a runtime is staged, and honest
    when it is not: the thin fallback must say it needs Python, and a
    release build must refuse to produce it at all."""

    def build_with(self, dist, require_runtime=False):
        source_root = dist / 'source-checkout'
        if not source_root.is_dir():
            source_root = ROOT
        with mock.patch.object(build_release, 'DIST', dist), mock.patch.object(build_release, 'ROOT', source_root):
            return build_release.build_member_companion(require_runtime=require_runtime)


    def stage_fake_runtime(self, dist):
        """A runtime-shaped directory, without downloading a real one.

        These tests are about what the packager does with a staged
        runtime, not about the runtime itself - build_runtime.py runs the
        real interpreter, and packaging/verify_bundle.py runs the built
        archive, so nothing here needs 58 MB and a network.
        """
        runtime = dist / "runtime"
        site = runtime / "Lib" / "site-packages" / "websockets"
        site.mkdir(parents=True)
        (runtime / "Lib" / "tkinter").mkdir(parents=True)
        (runtime / "pythonw.exe").write_bytes(b"MZ fake interpreter")
        (runtime / "python.exe").write_bytes(b"MZ fake interpreter")
        (site / "__init__.py").write_bytes(b"")
        (runtime / "Lib" / "tkinter" / "__init__.py").write_bytes(b"")
        source_root = dist / 'source-checkout'
        source_distribution.write_source_tree(
            source_distribution.snapshot(ROOT, build_release.WINDOWS_INSTALLER_APP_FILES), source_root)
        lock_path = source_root / 'Installer/runtime-lock.json'
        lock = json.loads(lock_path.read_text(encoding='utf-8'))
        for relative in runtime_licenses.required_license_paths(lock):
            path = runtime / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'complete upstream notice fixture\r\n')
        fixture = write_source_fixture(dist / 'upstream-fixture', runtime_lock=lock_path)
        source_lock = source_root / 'Installer/runtime-sources.lock.json'
        source_lock.write_bytes(fixture['source_lock'].read_bytes())
        runtime_sources.stage_runtime_sources(runtime, fixture['cache'], source_lock=source_lock,
                                              runtime_lock=lock_path, offline=True)
        return runtime

    def test_old_cached_runtime_without_licenses_or_sources_cannot_be_packaged(self):
        with tempfile.TemporaryDirectory() as folder:
            dist = Path(folder)
            runtime = self.stage_fake_runtime(dist)
            for relative, message in (('LICENSE.txt', 'missing or empty'),
                                      ('Source/README.txt', 'inventory mismatch')):
                path = runtime / relative
                original = path.read_bytes()
                path.unlink()
                for require_runtime in (False, True):
                    with self.subTest(missing=relative, require_runtime=require_runtime):
                        with self.assertRaisesRegex(SystemExit, message):
                            self.build_with(dist, require_runtime=require_runtime)
                        self.assertFalse((dist / 'vemals-ears-member.zip').exists())
                path.write_bytes(original)

    def test_bundle_verifier_rejects_missing_sources_before_running_its_interpreter(self):
        with tempfile.TemporaryDirectory() as folder:
            dist = Path(folder)
            self.stage_fake_runtime(dist)
            path, _ = self.build_with(dist)
            with zipfile.ZipFile(path) as archive:
                files = {name: archive.read(name) for name in archive.namelist()
                         if name != 'VemalsEars/runtime/Source/README.txt'}
            with zipfile.ZipFile(path, 'w') as archive:
                for name, data in files.items():
                    archive.writestr(name, data)
            with mock.patch.object(verify_bundle, 'ROOT', dist / 'source-checkout'), \
                    mock.patch.object(verify_bundle, 'DIST', dist), \
                    mock.patch.object(verify_bundle.subprocess, 'run') as run:
                problems = verify_bundle.verify(verify_bundle.BUNDLES[0])
            run.assert_not_called()
            self.assertTrue(any('source inventory mismatch' in problem for problem in problems), problems)

    def test_zip_refresh_refuses_changed_runtime_source_lock_before_any_write(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder)
            self.stage_fake_runtime(target)
            (target / 'vemals-ears-member.json').write_bytes(b'{}')
            (target / 'GuildHub').mkdir()
            (target / 'GuildHub/client.py').write_bytes(b'# old client preserved on failed preflight\n')
            source_root = target / 'source-checkout'
            lock_path = source_root / 'Installer/runtime-lock.json'
            lock = json.loads(lock_path.read_text())
            lock['python']['version'] = '3.14.8'
            lock_path.write_bytes(runtime_sources.json_bytes(lock))
            updated = write_source_fixture(target / 'updated-upstream-fixture', runtime_lock=lock_path)
            (source_root / 'Installer/runtime-sources.lock.json').write_bytes(updated['source_lock'].read_bytes())
            before = {path.relative_to(target): path.read_bytes()
                      for path in target.rglob('*') if path.is_file()}
            with mock.patch.object(refresh_local_copy, 'ROOT', source_root), \
                    contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(1, refresh_local_copy.refresh(target))
            self.assertIn('retained runtime does not match', errors.getvalue())
            self.assertIn('rebuild the full ZIP', errors.getvalue())
            self.assertEqual(before, {path.relative_to(target): path.read_bytes()
                                     for path in target.rglob('*') if path.is_file()})

    def test_thin_bundle_is_honest_about_needing_python(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path, _missing = self.build_with(Path(tmp))
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
                readme = archive.read("VemalsEars/HOW-TO-RUN.txt").decode()
        self.assertNotIn("VemalsEars/Start-Vemals-Ears.cmd", names)
        self.assertFalse(any(n.startswith("VemalsEars/runtime/") for n in names))
        self.assertIn("needs Python 3", readme)
        self.assertNotIn("Start-Vemals-Ears.cmd", readme)

    def test_staged_runtime_rides_along_with_a_launcher(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self.stage_fake_runtime(Path(tmp))
            path, missing = self.build_with(Path(tmp))
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
                launcher = archive.read("VemalsEars/Start-Vemals-Ears.cmd")
                readme = archive.read("VemalsEars/HOW-TO-RUN.txt").decode()
        self.assertFalse(missing)
        self.assertIn("VemalsEars/runtime/pythonw.exe", names)
        self.assertIn("VemalsEars/runtime/python.exe", names)
        self.assertIn(
            "VemalsEars/runtime/Lib/site-packages/websockets/__init__.py", names)
        # cmd.exe needs CRLF; a bare-LF batch file is misparsed silently.
        self.assertIn(b"\r\n", launcher)
        self.assertNotIn(b"\n", launcher.replace(b"\r\n", b""))
        text = launcher.decode("ascii")
        self.assertIn("runtime\\pythonw.exe", text)
        self.assertIn("Vemals-Ears.pyw", text)
        # Double-clicking this inside Explorer's zip preview extracts only
        # the .cmd, so the runtime is not beside it. Without the guard the
        # member sees a console flash and nothing else, and reports "I
        # double-clicked it and nothing happened".
        self.assertIn("if not exist", text)
        self.assertIn("pause", text)
        # And a stale TCL_LIBRARY from another Python points the bundled
        # interpreter at a foreign Tcl, killing the caption window.
        for variable in ("TCL_LIBRARY", "TK_LIBRARY", "PYTHONHOME"):
            self.assertIn(variable, text)
        self.assertIn("Start-Vemals-Ears.cmd", readme)
        self.assertNotIn("needs Python 3", readme)


    def test_release_builds_refuse_a_thin_bundle(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                self.build_with(Path(tmp), require_runtime=True)


    def test_the_runtime_and_the_installer_stage_from_one_lock(self):
        """One pinned set of versions, or the two member paths drift.

        The ZIP a member downloads today and the signed installer they get
        once it is signed both stage from Installer/runtime-lock.json. If
        this file ever stops being the single source, the two artifacts
        can ship different Pythons and only one of them gets tested.
        """
        lock = build_runtime.load_lock()
        self.assertEqual(1, lock["schema"])
        self.assertEqual("x64", lock["architecture"])
        self.assertRegex(lock["python"]["version"], r"^3\.\d+\.\d+$")
        self.assertRegex(lock["python"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(lock["python"]["url"].startswith(
            "https://www.python.org/ftp/python/"))
        # PowerShell reads the same file for the installer.
        installer = (ROOT / "Installer" / "build_installer.ps1").read_text(
            encoding="utf-8", errors="replace")
        self.assertIn("runtime-lock.json", installer)

    def test_every_pinned_wheel_is_hash_locked(self):
        lock = build_runtime.load_lock()
        names = set()
        for wheel in lock["wheels"]:
            self.assertRegex(wheel["sha256"], r"^[0-9a-f]{64}$")
            self.assertTrue(wheel["url"].endswith(wheel["filename"]))
            names.add(wheel["name"])
        # websockets is the member client's one hard dependency: without
        # it GuildHub/client.py cannot open a caption connection at all.
        self.assertIn("websockets", names)

    def test_the_lock_agrees_with_the_client_requirements_pin(self):
        """Two files name websockets. They must name the same bytes."""
        lock = build_runtime.load_lock()
        wheel = next(w for w in lock["wheels"] if w["name"] == "websockets")
        requirements = (ROOT / "GuildHub" / "requirements-client.txt").read_text(
            encoding="utf-8")
        self.assertIn(wheel["version"], requirements)
        self.assertIn(wheel["sha256"], requirements.lower())

    def test_a_bad_lock_is_refused_rather_than_half_staged(self):
        import tempfile
        original = build_runtime.LOCK
        try:
            with tempfile.TemporaryDirectory() as tmp:
                bad = Path(tmp) / "runtime-lock.json"
                build_runtime.LOCK = bad
                bad.write_text('{"schema": 99}', encoding="ascii")
                with self.assertRaises(SystemExit):
                    build_runtime.load_lock()
                bad.write_text('{"schema": 1, "architecture": "arm64"}',
                               encoding="ascii")
                with self.assertRaises(SystemExit):
                    build_runtime.load_lock()
                bad.write_text("not json", encoding="ascii")
                with self.assertRaises(SystemExit):
                    build_runtime.load_lock()
        finally:
            build_runtime.LOCK = original

    def test_fetch_deletes_and_refuses_a_corrupt_download(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            cached = Path(tmp) / "artifact.zip"
            cached.write_bytes(b"tampered")
            with self.assertRaises(SystemExit):
                build_runtime.fetch("https://unreachable.invalid/x", cached,
                                    "0" * 64)
            self.assertFalse(cached.exists(), "a corrupt file must not "
                             "survive to satisfy the next cache check")


class UiKitShipsWholeTests(unittest.TestCase):
    """Every module of the UI kit is named in the release manifest.

    DOWNTIME_UI_FILES lists files one by one, so a new kit module is
    invisible to the packager until someone remembers it - and the app
    then fails at import inside the shipped bundle, where nobody is
    watching. This is the same shape as the adapter's session_ledger.js
    miss on 2026-09-01; the fix is a test, not a habit.
    """

    def test_every_kit_module_is_in_the_manifest(self):
        listed = set(build_release.DOWNTIME_UI_FILES)
        on_disk = {
            f"downtime_ui/{path.name}"
            for path in (ROOT / "downtime_ui").glob("*.py")
        }
        missing = sorted(on_disk - listed)
        self.assertEqual(
            [],
            missing,
            "these kit modules would not ship: " + ", ".join(missing)
            + " - add them to DOWNTIME_UI_FILES in build_release.py.",
        )
        stale = sorted(listed - on_disk)
        self.assertEqual([], stale, "manifest names files that do not exist: " + ", ".join(stale))


class RefreshSourcePreflightTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "checkout"
        self.target = Path(directory.name) / "extracted"
        self.root.mkdir()
        for relative in source_distribution.BUILD_FILES:
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / relative).read_bytes())
        self.target.mkdir()
        (self.target / "vemals-ears-member.json").write_text("{}", encoding="ascii")
        patch = mock.patch.object(refresh_local_copy, "ROOT", self.root)
        patch.start()
        self.addCleanup(patch.stop)

    def test_later_missing_sources_leave_existing_and_new_destinations_untouched(self):
        (self.root / "client.py").write_bytes(b"new client")
        (self.root / "new.py").write_bytes(b"new file")
        # A directory named by the manifest is not a source file either.
        (self.root / "not-a-file.py").mkdir()
        (self.target / "client.py").write_bytes(b"old client")
        before = {path.relative_to(self.target): path.read_bytes() for path in self.target.iterdir()}
        files = ("client.py", "new.py", "protocol.py", "not-a-file.py")
        errors = io.StringIO()
        with mock.patch.object(build_release, "VEMALS_EARS_MEMBER_APP_FILES", files), \
                mock.patch.object(refresh_local_copy.shutil, "copy2", wraps=shutil.copy2) as copy, \
                contextlib.redirect_stderr(errors):
            code = refresh_local_copy.refresh(self.target)
        self.assertEqual(code, 1)
        self.assertEqual({path.relative_to(self.target): path.read_bytes() for path in self.target.iterdir()}, before)
        copy.assert_not_called()
        self.assertIn("protocol.py", errors.getvalue())
        self.assertIn("not-a-file.py", errors.getvalue())
        self.assertIn("was not changed", errors.getvalue())


    def test_installer_version_is_exempt_from_required_source_preflight(self):
        (self.target / "payload-manifest.json").write_text('{"version": "1.2.3"}', encoding="ascii")
        (self.target / "GuildHub").mkdir()
        version = self.target / "GuildHub/version.py"
        version.write_bytes(b'CLIENT_VERSION = "1.2.3"\n')
        (self.root / "client.py").write_bytes(b"new client")
        files = ("GuildHub/version.py", "client.py")
        with mock.patch.object(build_release, "VEMALS_EARS_MEMBER_APP_FILES", files), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(refresh_local_copy.refresh(self.target), 0)
        self.assertEqual(version.read_bytes(), b'CLIENT_VERSION = "1.2.3"\n')
        self.assertEqual((self.target / "client.py").read_bytes(), b"new client")

    def test_plain_extraction_still_requires_its_version_source(self):
        with mock.patch.object(build_release, "VEMALS_EARS_MEMBER_APP_FILES", ("GuildHub/version.py",)), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(refresh_local_copy.refresh(self.target), 1)
        self.assertIn("GuildHub/version.py", errors.getvalue())
        self.assertEqual([p.name for p in self.target.iterdir()], ["vemals-ears-member.json"])


class RefreshPreservesInstalledVersionTests(unittest.TestCase):
    """A refresh updates the code, never the installed identity.

    The install at %LOCALAPPDATA%/Programs/VemalsEars reported three
    versions at once on 2026-09-02 - registry 0.3.10, payload manifest
    0.5.0, GuildHub/version.py 0.5.1 - because promote.py refreshes it
    every deploy and version.py is in the member manifest. An install
    that always claims to be newest is never offered the update it
    needs, so the one copy that exists to test the member experience was
    the one copy that could not.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.source_root = self.root / 'build-source'
        source_distribution.write_source_tree(
            source_distribution.snapshot(ROOT, build_release.WINDOWS_INSTALLER_APP_FILES), self.source_root)
        self.upstream_fixture = write_source_fixture(
            self.root / 'upstream-fixture', runtime_lock=self.source_root / 'Installer/runtime-lock.json')
        (self.source_root / 'Installer/runtime-sources.lock.json').write_bytes(
            self.upstream_fixture['source_lock'].read_bytes())
        self.enterContext(mock.patch.object(refresh_local_copy, 'ROOT', self.source_root))

    def make_install(self, *, installer_managed, recorded="0.3.10"):
        """An extracted member copy, optionally installer-managed."""
        install = self.root / "VemalsEars"
        app = install / "app"
        app.mkdir(parents=True)
        (app / "vemals-ears-member.json").write_text("{}", encoding="utf-8")
        version = app / "GuildHub" / "version.py"
        version.parent.mkdir(parents=True, exist_ok=True)
        version.write_text(
            'CLIENT_VERSION = "%s"\n' % recorded, encoding="utf-8"
        )
        if installer_managed:
            (install / "payload-manifest.json").write_text(
                json.dumps({"version": recorded, "product": "Vemal's Ears"}),
                encoding="utf-8",
            )
        return app, version

    def source_complete_native_install(self):
        app, version = self.make_install(installer_managed=True)
        (app.parent / 'VemalsEars.exe').write_bytes(b'MZ retained installed launcher')
        runtime = app.parent / 'runtime'
        runtime.mkdir()
        (runtime / 'retained-runtime.dll').write_bytes(b'retained runtime')
        lock_path = self.source_root / 'Installer/runtime-lock.json'
        for relative in runtime_licenses.required_license_paths(json.loads(lock_path.read_text())):
            path = runtime / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'upstream runtime license fixture\r\n')
        runtime_sources.stage_runtime_sources(runtime, self.upstream_fixture['cache'],
                                              source_lock=self.source_root / 'Installer/runtime-sources.lock.json',
                                              runtime_lock=lock_path, offline=True)
        files = source_distribution.snapshot(self.source_root, build_release.WINDOWS_INSTALLER_APP_FILES)
        files['GuildHub/version.py'] = version.read_bytes()
        (app / 'Installer').mkdir()
        (app / 'Installer/uninstall_cleanup.py').write_bytes(files['Installer/uninstall_cleanup.py'])
        capsule = app / source_distribution.EMBEDDED_PATH
        capsule.parent.mkdir()
        capsule.write_bytes(source_distribution.archive_bytes(files))
        return app, version, files

    def test_native_refresh_updates_matching_source_and_retains_runtime_and_version(self):
        app, version, files = self.source_complete_native_install()
        version_before = version.read_bytes()
        runtime_before = (app.parent / 'runtime/retained-runtime.dll').read_bytes()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, refresh_local_copy.refresh(app))
        self.assertEqual(version_before, version.read_bytes())
        self.assertEqual(runtime_before, (app.parent / 'runtime/retained-runtime.dll').read_bytes())
        with zipfile.ZipFile(app / source_distribution.EMBEDDED_PATH) as source:
            for relative in build_release.WINDOWS_INSTALLER_APP_FILES:
                self.assertEqual((app / relative).read_bytes(), source.read('VemalsEars-source/' + relative))
            for relative in refresh_local_copy.RETAINED_BINARY_INPUTS:
                self.assertEqual(files[relative], source.read('VemalsEars-source/' + relative))

    def test_native_refresh_without_prior_source_refuses_before_any_write(self):
        app, _, _ = self.source_complete_native_install()
        (app / source_distribution.EMBEDDED_PATH).unlink()
        before = {path.relative_to(app.parent): path.read_bytes()
                  for path in app.parent.rglob('*') if path.is_file()}
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(1, refresh_local_copy.refresh(app))
        self.assertIn('install a source-complete', errors.getvalue())
        self.assertEqual(before, {path.relative_to(app.parent): path.read_bytes()
                                 for path in app.parent.rglob('*') if path.is_file()})

    def test_native_refresh_accepts_new_app_modules_and_keeps_the_source_rebuildable(self):
        app, _, files = self.source_complete_native_install()
        checkout = self.root / 'checkout'
        source_distribution.write_source_tree(files, checkout)
        added = 'VoiceCaptions/development_extra.py'
        (checkout / added).write_bytes(b'EXTRA = "recipient app addition"\n')
        builder = checkout / 'build_release.py'
        builder.write_text(builder.read_text(encoding='ascii').replace(
            'VEMALS_EARS_MEMBER_APP_FILES = (', 'VEMALS_EARS_MEMBER_APP_FILES = (\n    "' + added + '",'), encoding='ascii')
        with mock.patch.object(refresh_local_copy, 'ROOT', checkout), \
                mock.patch.object(build_release, 'VEMALS_EARS_MEMBER_APP_FILES',
                                  build_release.VEMALS_EARS_MEMBER_APP_FILES + (added,)), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, refresh_local_copy.refresh(app))
        extracted = self.root / 'rebuilt'
        with zipfile.ZipFile(app / source_distribution.EMBEDDED_PATH) as archive:
            archive.extractall(extracted)
        result = subprocess.run([sys.executable, '-E', '-s', '-B',
                                 str(extracted / 'VemalsEars-source/build_release.py'), '--member'],
                                capture_output=True, text=True, timeout=45)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        with zipfile.ZipFile(extracted / 'VemalsEars-source/dist/vemals-ears-member.zip') as archive:
            self.assertEqual((app / added).read_bytes(), archive.read('VemalsEars/' + added))

    def test_native_refresh_with_changed_native_input_refuses_before_any_write(self):
        app, _, files = self.source_complete_native_install()
        files['Installer/launcher.c'] += b'\n/* original installed native variant */\n'
        (app / source_distribution.EMBEDDED_PATH).write_bytes(source_distribution.archive_bytes(files))
        before = {path.relative_to(app.parent): path.read_bytes()
                  for path in app.parent.rglob('*') if path.is_file()}
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(1, refresh_local_copy.refresh(app))
        self.assertIn('retained native/runtime build inputs changed', errors.getvalue())
        self.assertEqual(before, {path.relative_to(app.parent): path.read_bytes()
                                 for path in app.parent.rglob('*') if path.is_file()})

    def test_refresh_reports_payload_corruption_instead_of_success(self):
        app, _ = self.make_install(installer_managed=False)
        original = Path.write_bytes
        def corrupt(path, payload):
            if path == app / 'GuildHub/client.py':
                payload += b'\n# damaged write\n'
            return original(path, payload)
        with mock.patch.object(Path, 'write_bytes', corrupt), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(1, refresh_local_copy.refresh(app))
        self.assertIn('payload differs', errors.getvalue())

    def test_an_installer_managed_copy_keeps_its_version(self):
        app, version = self.make_install(installer_managed=True)
        before = version.read_text(encoding="utf-8")

        with contextlib.redirect_stdout(io.StringIO()):
            refresh_local_copy.refresh(app)

        self.assertEqual(before, version.read_text(encoding="utf-8"))
        self.assertIn("0.3.10", version.read_text(encoding="utf-8"))

    def test_a_plain_extraction_is_still_fully_refreshed(self):
        # Only installer-managed copies have an identity to protect. An
        # extracted zip has no manifest and no registry entry, and must
        # keep being brought fully up to date.
        app, version = self.make_install(installer_managed=False)

        with contextlib.redirect_stdout(io.StringIO()):
            refresh_local_copy.refresh(app)

        self.assertEqual(
            (Path(refresh_local_copy.ROOT) / "GuildHub/version.py").read_text(
                encoding="utf-8"
            ),
            version.read_text(encoding="utf-8"),
        )

    def test_the_manifest_is_found_beside_the_app_directory(self):
        # promote.py refreshes the app/ subdirectory while the installer
        # writes the manifest to the install root above it. Looking only
        # in the target would find nothing and protect nothing.
        app, _ = self.make_install(installer_managed=True)
        found = refresh_local_copy.installer_manifest(app)
        self.assertIsNotNone(found)
        self.assertEqual(app.parent / "payload-manifest.json", found)

    def test_the_code_is_still_refreshed_on_a_managed_copy(self):
        # The whole point of the script survives: only identity is spared.
        app, _ = self.make_install(installer_managed=True)
        client = app / "GuildHub" / "client.py"
        client.write_text("# stale\n", encoding="utf-8")

        with contextlib.redirect_stdout(io.StringIO()):
            refresh_local_copy.refresh(app)

        self.assertEqual(
            (Path(refresh_local_copy.ROOT) / "GuildHub/client.py").read_text(
                encoding="utf-8"
            ),
            client.read_text(encoding="utf-8"),
        )

    def test_it_says_what_it_left_alone(self):
        app, _ = self.make_install(installer_managed=True)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            refresh_local_copy.refresh(app)
        printed = output.getvalue()
        self.assertIn("Installer-managed", printed)
        self.assertIn("0.3.10", printed)


if __name__ == '__main__':
    unittest.main(verbosity=2)
