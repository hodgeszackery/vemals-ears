"""Offline tests for the hash-pinned workflow linter download boundary."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
import stat
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile

from ci import lint_workflows as lint


BINARY = b"not executed - offline test binary"


def tar_payload(*, name="actionlint", data=BINARY, kind=tarfile.REGTYPE, extras=()):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for entry_name, entry_data, entry_kind in ((name, data, kind), *extras):
            info = tarfile.TarInfo(entry_name)
            info.type = entry_kind
            if entry_kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                info.linkname = "../../outside"
            info.size = len(entry_data) if entry_kind == tarfile.REGTYPE else 0
            archive.addfile(info, io.BytesIO(entry_data) if info.size else None)
    return buffer.getvalue()


def zip_payload(*, name="actionlint.exe", data=BINARY, mode=stat.S_IFREG | 0o700, extras=()):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for entry_name, entry_data in ((name, data), *extras):
            info = zipfile.ZipInfo(entry_name)
            info.create_system = 3
            info.external_attr = mode << 16
            archive.writestr(info, entry_data, compress_type=zipfile.ZIP_DEFLATED)
    return buffer.getvalue()


def verified(payload, system):
    asset = lint.ASSETS[system][0]
    with mock.patch.dict(lint.ASSETS, {system: (asset, hashlib.sha256(payload).hexdigest())}):
        return lint.verified_binary(payload, system=system)


class ArchiveBoundaryTests(unittest.TestCase):
    def test_hash_mismatch_rejects_before_reading_archive(self):
        with mock.patch.object(lint.tarfile, "open") as tar_open:
            with mock.patch.object(lint.zipfile, "ZipFile") as zip_open:
                for system in ("Linux", "Windows"):
                    with self.subTest(system=system):
                        with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
                            lint.verified_binary(b"not an archive", system=system)
        tar_open.assert_not_called()
        zip_open.assert_not_called()

    def test_tar_regular_named_binary_is_returned(self):
        self.assertEqual(verified(tar_payload(), "Linux"), BINARY)

    def test_zip_regular_named_binary_is_returned(self):
        self.assertEqual(verified(zip_payload(), "Windows"), BINARY)

    def test_zip_without_unix_filetype_metadata_is_allowed(self):
        self.assertEqual(verified(zip_payload(mode=0o600), "Windows"), BINARY)

    def test_tar_symlink_hardlink_directory_and_device_are_rejected(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.DIRTYPE, tarfile.CHRTYPE, tarfile.FIFOTYPE):
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(ValueError, "Unexpected"):
                    verified(tar_payload(kind=kind), "Linux")

    def test_zip_symlink_directory_and_special_file_modes_are_rejected(self):
        for mode in (stat.S_IFLNK, stat.S_IFDIR, stat.S_IFCHR, stat.S_IFIFO):
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(ValueError, "Unexpected"):
                    verified(zip_payload(mode=mode | 0o700), "Windows")

    def test_tar_traversal_name_cannot_substitute_for_exact_executable(self):
        for name in ("../actionlint", "/actionlint", "nested/actionlint"):
            with self.subTest(name=name), self.assertRaises(KeyError):
                verified(tar_payload(name=name), "Linux")

    def test_zip_traversal_name_cannot_substitute_for_exact_executable(self):
        for name in ("../actionlint.exe", "/actionlint.exe", "nested/actionlint.exe"):
            with self.subTest(name=name), self.assertRaises(KeyError):
                verified(zip_payload(name=name), "Windows")

    def test_other_tar_entries_are_never_extracted(self):
        payload = tar_payload(extras=(("../../outside", b"untrusted", tarfile.REGTYPE),))
        with mock.patch.object(tarfile.TarFile, "extract", side_effect=AssertionError("extract forbidden")):
            with mock.patch.object(tarfile.TarFile, "extractall", side_effect=AssertionError("extractall forbidden")):
                self.assertEqual(verified(payload, "Linux"), BINARY)

    def test_other_zip_entries_are_never_extracted(self):
        payload = zip_payload(extras=(("../../outside.exe", b"untrusted"),))
        with mock.patch.object(zipfile.ZipFile, "extract", side_effect=AssertionError("extract forbidden")):
            with mock.patch.object(zipfile.ZipFile, "extractall", side_effect=AssertionError("extractall forbidden")):
                self.assertEqual(verified(payload, "Windows"), BINARY)

    def test_oversized_compressed_tar_executable_is_rejected(self):
        payload = tar_payload(data=b"0" * 4096)
        self.assertLess(len(payload), 1024)
        with mock.patch.object(lint, "MAX_DOWNLOAD", 1024):
            with self.assertRaisesRegex(ValueError, "Unexpected actionlint executable"):
                verified(payload, "Linux")

    def test_oversized_compressed_zip_executable_is_rejected(self):
        payload = zip_payload(data=b"0" * 4096)
        self.assertLess(len(payload), 1024)
        with mock.patch.object(lint, "MAX_DOWNLOAD", 1024):
            with self.assertRaisesRegex(ValueError, "Oversized"):
                verified(payload, "Windows")

    def test_empty_executable_is_rejected(self):
        for factory, system in ((tar_payload, "Linux"), (zip_payload, "Windows")):
            with self.subTest(system=system), self.assertRaises(ValueError):
                verified(factory(data=b""), system)

    def test_archive_byte_limit_is_checked_before_hash_or_decode(self):
        with mock.patch.object(lint, "MAX_DOWNLOAD", 3):
            with self.assertRaisesRegex(ValueError, "Oversized actionlint archive"):
                lint.verified_binary(b"1234", system="Linux")


class LintCommandTests(unittest.TestCase):
    def test_unsupported_platform_or_architecture_never_downloads(self):
        for system, machine in (("Darwin", "x86_64"), ("Linux", "aarch64")):
            with self.subTest(system=system, machine=machine):
                with mock.patch.object(lint.platform, "system", return_value=system):
                    with mock.patch.object(lint.platform, "machine", return_value=machine):
                        with mock.patch.object(lint.urllib.request, "urlopen") as download:
                            with self.assertRaisesRegex(SystemExit, "x64 Linux and Windows"):
                                lint.main()
                        download.assert_not_called()

    def run_main(self, root, payload, system="Linux", exit_code=0):
        asset = lint.ASSETS[system][0]
        patches = (
            mock.patch.object(lint, "ROOT", root),
            mock.patch.object(lint.platform, "system", return_value=system),
            mock.patch.object(lint.platform, "machine", return_value="AMD64"),
            mock.patch.dict(lint.ASSETS, {system: (asset, hashlib.sha256(payload).hexdigest())}),
            mock.patch.object(lint.urllib.request, "urlopen", return_value=io.BytesIO(payload)),
            mock.patch.object(lint.subprocess, "run", return_value=subprocess.CompletedProcess([], exit_code)),
        )
        import contextlib
        with contextlib.ExitStack() as stack:
            entered = [stack.enter_context(patch) for patch in patches]
            code = lint.main()
            return code, entered[-2], entered[-1]

    def test_only_validated_bytes_run_and_both_workflow_extensions_are_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflows = root / ".github/workflows"
            workflows.mkdir(parents=True)
            for name in ("a.yml", "b.yaml", "not-workflow.txt"):
                (workflows / name).write_text("name: fixture\n", encoding="ascii")
            (workflows / "directory.yml").mkdir()
            code, download, run = self.run_main(root, tar_payload(), exit_code=7)
            self.assertEqual(code, 7)
            self.assertIn("https://github.com/rhysd/actionlint/releases/download/", download.call_args.args[0])
            self.assertEqual(download.call_args.kwargs["timeout"], 60)
            command = run.call_args.args[0]
            self.assertEqual(command[1:4], ["-color", "-shellcheck=", "-pyflakes="])
            self.assertEqual(command[4:], [str(workflows / "a.yml"), str(workflows / "b.yaml")])
            self.assertEqual(run.call_args.kwargs["cwd"], root)
            self.assertEqual(run.call_args.kwargs["timeout"], 60)
            self.assertFalse(run.call_args.kwargs["check"])
            self.assertNotIn("shell", run.call_args.kwargs)
            self.assertFalse(Path(command[0]).exists(), "temporary executable must be cleaned up")

    def test_windows_uses_only_named_temporary_exe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".github/workflows").mkdir(parents=True)
            (root / ".github/workflows/tests.yml").write_text("name: fixture\n", encoding="ascii")
            code, _download, run = self.run_main(root, zip_payload(), system="Windows")
            self.assertEqual(code, 0)
            self.assertEqual(Path(run.call_args.args[0][0]).name, "actionlint.exe")

    def test_no_workflows_fails_without_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".github/workflows").mkdir(parents=True)
            with self.assertRaisesRegex(SystemExit, "No workflows found"):
                self.run_main(root, tar_payload())

    def test_download_size_bound_rejects_before_archive_verification(self):
        with mock.patch.object(lint.platform, "system", return_value="Linux"):
            with mock.patch.object(lint.platform, "machine", return_value="x86_64"):
                with mock.patch.object(lint, "MAX_DOWNLOAD", 3):
                    with mock.patch.object(lint.urllib.request, "urlopen", return_value=io.BytesIO(b"123456")):
                        with mock.patch.object(lint, "verified_binary") as verify:
                            with self.assertRaisesRegex(SystemExit, "Oversized actionlint download"):
                                lint.main()
                        verify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
