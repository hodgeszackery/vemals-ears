"""The host package is complete outside the checkout and cannot collect state."""

from contextlib import closing
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import urllib.error
import urllib.request
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("build_host", ROOT / "packaging/build_host.py")
build_host = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_host)


class HostArchiveTests(unittest.TestCase):
    def fixture(self, root):
        for relative in build_host.HOST_FILES:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("source fixture: " + relative + "\n", encoding="ascii")

    def test_only_allowlisted_sources_are_read_even_beside_live_state(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            self.fixture(root)
            private_files = (
                "Hosting/relay.env", "Hosting/state/relay.env",
                "Hosting/secrets/discord-bot-token", "GuildHub/guild-hub.sqlite3",
                "VoiceCaptions/discord_adapter/.env",
                "VoiceCaptions/discord_adapter/src/credentials.json",
                "VoiceCaptions/discord_adapter/node_modules/private.txt",
                "VoiceCaptions/consent-state.json", "VoiceCaptions/mapping-state.json",
                "VoiceCaptions/runtime/model.bin", "WoW-Companion.pyw",
            )
            for relative in private_files:
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("XENO_PRIVATE_ARCHIVE_SENTINEL", encoding="ascii")
            original_read = Path.read_bytes

            def permitted_read(path):
                self.assertIn(path.relative_to(root).as_posix(), build_host.HOST_FILES)
                return original_read(path)

            with mock.patch.object(Path, "read_bytes", permitted_read):
                files = build_host.snapshot(root)
            self.assertEqual(set(build_host.HOST_FILES), set(files))
            self.assertNotIn(b"XENO_PRIVATE_ARCHIVE_SENTINEL", b"".join(files.values()))
            data = build_host.archive_bytes(files)
            self.assertEqual(files, build_host.verify_archive(data))
            self.assertEqual(data, build_host.archive_bytes(dict(reversed(list(files.items())))))

    def test_missing_source_fails_before_replacing_an_existing_output(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            (root / "GuildHub/host.py").unlink()
            target = root / "previous.zip"
            target.write_bytes(b"previous package")
            with self.assertRaises(build_host.HostArchiveError):
                build_host.build(root, target)
            self.assertEqual(b"previous package", target.read_bytes())

    def test_unlisted_and_linked_paths_are_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            self.fixture(root)
            for relative in ("../secret", "Hosting/relay.env", "C:/secret", "Hosting\\setup.py"):
                with self.subTest(relative=relative), self.assertRaises(build_host.HostArchiveError):
                    build_host.source_path(root, relative)
            original_is_symlink = Path.is_symlink

            def linked_host(path):
                return path == root / "Hosting" or original_is_symlink(path)

            with mock.patch.object(Path, "is_symlink", linked_host):
                with self.assertRaises(build_host.HostArchiveError):
                    build_host.snapshot(root)

    def test_archive_verifier_rejects_extra_files_and_changed_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            pristine = build_host.archive_bytes(build_host.snapshot(root))
            for added, changed in ((True, False), (False, True)):
                with self.subTest(added=added, changed=changed):
                    altered = io.BytesIO()
                    with zipfile.ZipFile(io.BytesIO(pristine)) as source:
                        with zipfile.ZipFile(altered, "w") as output:
                            for name in source.namelist():
                                payload = source.read(name)
                                if changed and name.endswith("/GuildHub/host.py"):
                                    payload = b"changed source\n"
                                output.writestr(name, payload)
                            if added:
                                output.writestr(build_host.ARCHIVE_ROOT + "Hosting/relay.env", "private")
                    with self.assertRaises(build_host.HostArchiveError):
                        build_host.verify_archive(altered.getvalue())

    def test_real_package_imports_and_rebuilds_without_checkout_or_git(self):
        data = build_host.archive_bytes(build_host.snapshot(ROOT))
        with tempfile.TemporaryDirectory() as folder:
            temporary = Path(folder)
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                archive.extractall(temporary)
            extracted = temporary / build_host.ARCHIVE_ROOT.rstrip("/")
            self.assertFalse((extracted / ".git").exists())
            probe = "\n".join((
                "import pathlib, sys",
                "root = pathlib.Path(sys.argv[1]).resolve()",
                "sys.path.insert(0, str(root))",
                "from GuildHub import host, server",
                "from Hosting import setup, service",
                "assert callable(host.main) and callable(server.main)",
                "assert callable(setup.main) and callable(service.main)",
                "assert 'tkinter' not in sys.modules",
                "for name, module in tuple(sys.modules.items()):",
                "    if name.split('.')[0] in {'GuildHub', 'VoiceCaptions', 'Hosting'}:",
                "        assert pathlib.Path(module.__file__).resolve().is_relative_to(root), name",
                "print('ISOLATED-HOST-IMPORT-OK')",
            ))
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-c", probe, str(extracted)],
                cwd=temporary, capture_output=True, text=True, timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("ISOLATED-HOST-IMPORT-OK", result.stdout)
            rebuilt = temporary / "rebuilt.zip"
            result = subprocess.run(
                [sys.executable, "-I", "-B", str(extracted / "packaging/build_host.py"),
                 "--output", str(rebuilt)],
                cwd=temporary, capture_output=True, text=True, timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual(data, rebuilt.read_bytes())
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                manifest = json.loads(archive.read(build_host.ARCHIVE_ROOT + build_host.MANIFEST_NAME))
                for record in manifest["files"]:
                    self.assertEqual(record["sha256"], hashlib.sha256(
                        archive.read(build_host.ARCHIVE_ROOT + record["path"])).hexdigest())

    def test_extracted_host_configures_and_serves_with_local_admin(self):
        # Exercise the shipped files as a new operator, with no checkout on
        # sys.path and no Discord connection, download, or installed service.
        data = build_host.archive_bytes(build_host.snapshot(ROOT))
        with tempfile.TemporaryDirectory() as folder:
            temporary = Path(folder).resolve()
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                archive.extractall(temporary)
            extracted = temporary / build_host.ARCHIVE_ROOT.rstrip("/")
            state = temporary / "host-state"
            environment = {
                key: value for key, value in os.environ.items()
                if not key.startswith(("VEMALS_EARS_", "VOICE_CAPTIONS_", "CAPTIONS_"))
            }
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            configure = "\n".join((
                "import pathlib, sys",
                "sys.path.insert(0, sys.argv[1])",
                "from Hosting.setup import create_configuration",
                "create_configuration(pathlib.Path(sys.argv[2]),",
                "    public_hostname='xeno-host-test.example.org',",
                "    guild_id='123456789012345678', application_id='234567890123456789',",
                "    bot_token='XENO_SYNTHETIC_DISCORD_TOKEN_NEVER_USED', port=int(sys.argv[3]))",
                "print('NEW-HOST-CONFIG-OK')",
            ))
            configured = subprocess.run(
                [sys.executable, "-I", "-B", "-c", configure, str(extracted), str(state), str(port)],
                cwd=temporary, env=environment, capture_output=True, text=True,
                timeout=30, creationflags=flags,
            )
            self.assertEqual(0, configured.returncode, configured.stdout + configured.stderr)
            self.assertIn("NEW-HOST-CONFIG-OK", configured.stdout)
            self.assertNotIn("XENO_SYNTHETIC_DISCORD_TOKEN", configured.stdout + configured.stderr)
            self.assertNotIn("XENO_SYNTHETIC_DISCORD_TOKEN", (state / "relay.env").read_text())
            if os.name == "nt":
                # Verify the real setup ACL, including the credentials'
                # inheritance, without requiring administrator privileges.
                acl_probe = "\n".join((
                    "$ErrorActionPreference = 'Stop'",
                    "$stateAcl = [System.IO.Directory]::GetAccessControl($env:XENO_HOST_TEST_STATE)",
                    "$secretPath = Join-Path $env:XENO_HOST_TEST_STATE 'secrets/discord-bot-token'",
                    "$secretAcl = [System.IO.File]::GetAccessControl($secretPath)",
                    "$sidType = [System.Security.Principal.SecurityIdentifier]",
                    "[ordered]@{",
                    "  protected = $stateAcl.AreAccessRulesProtected",
                    "  currentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value",
                    "  stateSids = @($stateAcl.Access | ForEach-Object { $_.IdentityReference.Translate($sidType).Value })",
                    "  secretSids = @($secretAcl.Access | ForEach-Object { $_.IdentityReference.Translate($sidType).Value })",
                    "} | ConvertTo-Json -Compress",
                ))
                inspected = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", acl_probe],
                    cwd=temporary, env={**environment, "XENO_HOST_TEST_STATE": str(state)},
                    capture_output=True, text=True, timeout=30, creationflags=flags,
                )
                self.assertEqual(0, inspected.returncode, inspected.stdout + inspected.stderr)
                acl = json.loads(inspected.stdout)
                self.assertTrue(acl["protected"], inspected.stdout + inspected.stderr)
                expected_sids = {acl["currentSid"], "S-1-5-18", "S-1-5-32-544"}
                # Windows may retain an OWNER RIGHTS ACE on Python-created
                # temporary directories. It denotes the same owning account.
                for actual_sids in (set(acl["stateSids"]), set(acl["secretSids"])):
                    self.assertTrue(expected_sids.issubset(actual_sids))
                    self.assertTrue(actual_sids.issubset(expected_sids | {"S-1-3-4"}))
            else:
                self.assertEqual(0o700, stat.S_IMODE(state.stat().st_mode))
                self.assertEqual(0o600, stat.S_IMODE((state / "secrets/discord-bot-token").stat().st_mode))

            # -I omits cwd and PYTHONPATH; only the extracted package is added.
            launcher = (
                "import runpy, sys; sys.path.insert(0, sys.argv.pop(1)); "
                "runpy.run_module('Hosting.service', run_name='__main__')"
            )
            command = [sys.executable, "-I", "-B", "-u", "-c", launcher,
                       str(extracted), "--state-directory", str(state)]
            relay = subprocess.Popen(
                [*command, "relay"], cwd=temporary, env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                creationflags=flags,
            )
            # Bypass proxy environment variables: every HTTP request stays on
            # the dynamically allocated loopback listener created by this test.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            origin = "http://127.0.0.1:" + str(port)
            try:
                deadline = time.monotonic() + 15
                while True:
                    if relay.poll() is not None:
                        output, _ = relay.communicate(timeout=5)
                        self.fail("Fresh host relay exited before health check: " + output)
                    try:
                        with opener.open(origin + "/healthz", timeout=0.5) as response:
                            health = json.load(response)
                        break
                    except (urllib.error.URLError, TimeoutError):
                        if time.monotonic() >= deadline:
                            self.fail("Fresh host relay did not become healthy within 15 seconds.")
                        time.sleep(0.05)
                self.assertEqual({"status": "ok", "service": "vemals-ears"}, health)
                with opener.open(origin + "/join", timeout=3) as response:
                    self.assertEqual(200, response.status)
                    page = response.read().decode("ascii")
                self.assertIn("Vemal's Ears", page)
                self.assertIn('data-server="wss://xeno-host-test.example.org"', page)
                self.assertIn('href="https://xeno-host-test.example.org/download"', page)
                for arguments, expected in (
                    (["list"], "No devices enrolled."),
                    (["enroll", "--minutes", "1"], "Join page: https://xeno-host-test.example.org/join#"),
                ):
                    administered = subprocess.run(
                        [*command, "admin", *arguments], cwd=temporary, env=environment,
                        capture_output=True, text=True, timeout=15, creationflags=flags,
                    )
                    self.assertEqual(0, administered.returncode,
                                     administered.stdout + administered.stderr)
                    self.assertIn(expected, administered.stdout)
                with closing(sqlite3.connect(state / "relay/guild-hub.sqlite3")) as database:
                    count = database.execute("SELECT COUNT(*) FROM enrollments").fetchone()[0]
                self.assertEqual(1, count)
                self.assertFalse((temporary / "guild-hub.sqlite3").exists())
                self.assertIsNone(relay.poll())
            finally:
                # Only this test's Popen child is stopped. No SCM or process
                # name lookup is involved, and the temporary state stays local.
                if relay.poll() is None:
                    relay.terminate()
                try:
                    relay.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    relay.kill()
                    relay.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
