import hashlib
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import VoiceCaptions.runtime_installer as runtime_installer_module
from VoiceCaptions.runtime_installer import (
    CPU_RUNTIME_SHA256,
    CPU_RUNTIME_URL,
    CUDA_RUNTIME_SHA256,
    CUDA_RUNTIME_URL,
    DLL_ENV,
    FALLBACK_SERVER_ENV,
    MODEL_ENV,
    MODEL_SHA256,
    MODEL_URL,
    RUNTIME_SHA256,
    RUNTIME_URL,
    SERVER_ENV,
    VAD_MODEL_ENV,
    VAD_MODEL_SHA256,
    VAD_MODEL_URL,
    AdapterPreparation,
    DownloadAsset,
    DownloadError,
    IntegrityError,
    RuntimeAssets,
    RuntimeInstallError,
    UnsafeArchiveError,
    cuda_12_4_available,
    default_runtime_root,
    detect_installed_runtime,
    download_asset,
    install_runtime,
    prepare_discord_adapter,
    safe_extract_zip,
    stt_environment,
)


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def runtime_zip(
    *,
    member: str = "Release/whisper-server.exe",
    server_payload: bytes = b"verified whisper server",
    include_vad_dependencies: bool = True,
    extra_files: dict[str, bytes] | None = None,
) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, server_payload)
        archive.writestr("Release/whisper.dll", b"runtime dependency")
        if include_vad_dependencies:
            archive.writestr("Release/ggml.dll", b"ggml dispatcher")
            archive.writestr("Release/ggml-base.dll", b"ggml base")
            archive.writestr("Release/ggml-cpu-test.dll", b"ggml CPU backend")
        # Lets a caller model a file that only one acceleration path ships,
        # which is exactly what the CPU-shaped fixture used to be missing.
        for name, payload in (extra_files or {}).items():
            archive.writestr(name, payload)
    return output.getvalue()


class FixtureDownloader:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def __call__(self, asset, destination, progress):
        self.calls.append((asset.name, asset.url, destination))
        payload = self.payloads[asset.url]
        destination.write_bytes(payload)
        if progress:
            progress(len(payload), len(payload))


def fixture_assets(
    cpu_runtime_payload: bytes,
    model_payload: bytes,
    vad_model_payload: bytes = b"verified vad fixture",
    cuda_runtime_payload: bytes | None = None,
) -> RuntimeAssets:
    cuda_runtime_payload = cuda_runtime_payload or runtime_zip(
        server_payload=b"verified cuda whisper server"
    )
    return RuntimeAssets(
        cpu_runtime=DownloadAsset(
            "test CPU runtime",
            "fixture://cpu-runtime",
            sha256(cpu_runtime_payload),
            1,
            1024 * 1024,
            10,
        ),
        cuda_runtime=DownloadAsset(
            "test CUDA runtime",
            "fixture://cuda-runtime",
            sha256(cuda_runtime_payload),
            1,
            1024 * 1024,
            10,
        ),
        model=DownloadAsset(
            "test model",
            "fixture://model",
            sha256(model_payload),
            1,
            1024 * 1024,
            10,
        ),
        vad_model=DownloadAsset(
            "test VAD model",
            "fixture://vad-model",
            sha256(vad_model_payload),
            1,
            1024 * 1024,
            10,
        ),
    )


def fixture_downloads(
    assets: RuntimeAssets,
    cpu_runtime: bytes,
    model: bytes,
    vad_model: bytes = b"verified vad fixture",
    cuda_runtime: bytes | None = None,
) -> dict[str, bytes]:
    payloads = {
        assets.cpu_runtime.url: cpu_runtime,
        assets.model.url: model,
        assets.vad_model.url: vad_model,
    }
    if cuda_runtime is not None:
        payloads[assets.cuda_runtime.url] = cuda_runtime
    return payloads


class RuntimeInstallerTests(unittest.TestCase):
    def test_production_assets_are_exactly_pinned(self):
        self.assertEqual(
            "https://github.com/ggml-org/whisper.cpp/releases/download/"
            "b4938/whisper-cublas-12.4.0-bin-x64.zip",
            RUNTIME_URL,
        )
        self.assertEqual(
            "c1b17166e1e31a91cc8e9c1f910d3785e3ce757bb2958bf9dce13fdb4880005f",
            RUNTIME_SHA256,
        )
        self.assertEqual(CUDA_RUNTIME_URL, RUNTIME_URL)
        self.assertEqual(CUDA_RUNTIME_SHA256, RUNTIME_SHA256)
        self.assertEqual(
            "https://github.com/ggml-org/whisper.cpp/releases/download/"
            "b4938/whisper-bin-x64.zip",
            CPU_RUNTIME_URL,
        )
        self.assertEqual(
            "c2a4b60edb11f7e11a9191ffb50929535527d4d91c9903dbe3e554583bbbc63d",
            CPU_RUNTIME_SHA256,
        )
        self.assertEqual(
            "https://huggingface.co/ggerganov/whisper.cpp/resolve/"
            "5359861c739e955e79d9a303bcbc70fb988958b1/"
            "ggml-large-v3-turbo-q5_0.bin",
            MODEL_URL,
        )
        self.assertEqual(
            "394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2",
            MODEL_SHA256,
        )
        self.assertEqual(
            "https://huggingface.co/ggml-org/whisper-vad/resolve/"
            "9ffd54a1e1ee413ddf265af9913beaf518d1639b/"
            "ggml-silero-v6.2.0.bin",
            VAD_MODEL_URL,
        )
        self.assertEqual(
            "2aa269b785eeb53a82983a20501ddf7c1d9c48e33ab63a41391ac6c9f7fb6987",
            VAD_MODEL_SHA256,
        )

    def test_default_location_is_under_local_app_data(self):
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Local"}):
            self.assertEqual(
                Path(r"C:\Local") / "WoWVoiceCaptions" / "runtime",
                default_runtime_root(),
            )

    def test_detection_is_read_only_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "not-created"
            self.assertIsNone(detect_installed_runtime(root))
            self.assertFalse(root.exists())

    def test_offline_install_activates_verified_runtime_and_environment(self):
        archive = runtime_zip()
        model = b"verified model fixture"
        assets = fixture_assets(archive, model)
        downloader = FixtureDownloader(fixture_downloads(assets, archive, model))
        progress = []
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "runtime"
            installed = install_runtime(
                root,
                downloader=downloader,
                assets=assets,
                progress=progress.append,
                cuda_detector=lambda: False,
            )

            self.assertTrue(installed.server_path.is_file())
            self.assertEqual(b"verified whisper server", installed.server_path.read_bytes())
            self.assertEqual(model, installed.model_path.read_bytes())
            self.assertEqual("cpu", installed.acceleration)
            self.assertEqual(installed.server_path, installed.fallback_server_path)
            self.assertTrue(installed.library_path.is_file())
            self.assertTrue(installed.vad_model_path.is_file())
            self.assertEqual(
                {
                    SERVER_ENV: str(installed.server_path),
                    MODEL_ENV: str(installed.model_path),
                    # Tells the STT backend whether beam width above 1
                    # is affordable; cpu here means decode greedy.
                    "VOICE_CAPTIONS_WHISPER_ACCELERATION": "cpu",
                    FALLBACK_SERVER_ENV: str(installed.fallback_server_path),
                    DLL_ENV: str(installed.library_path),
                    VAD_MODEL_ENV: str(installed.vad_model_path),
                },
                stt_environment(root, assets=assets),
            )
            manifest = json.loads((root / "active.json").read_text(encoding="ascii"))
            self.assertNotIn("http", json.dumps(manifest))
            self.assertEqual(3, len(downloader.calls))
            self.assertEqual(2, manifest["schema"])
            self.assertEqual("cpu", manifest["acceleration"])
            self.assertIsNone(manifest["cuda_runtime_archive_sha256"])
            self.assertEqual(
                {"ggml-base.dll", "ggml-cpu-test.dll", "ggml.dll"},
                {
                    Path(path).name
                    for path in manifest["vad_dependency_sha256"]
                },
            )
            self.assertEqual(1.0, progress[-1].fraction)
            self.assertEqual("ready", progress[-1].phase)
            self.assertFalse(any(path.name.startswith(".install-") for path in (root / "installs").iterdir()))

    def test_cuda_runtime_is_primary_and_cpu_runtime_remains_fallback(self):
        cpu_archive = runtime_zip(server_payload=b"CPU server")
        cuda_archive = runtime_zip(server_payload=b"CUDA server")
        model = b"verified model fixture"
        assets = fixture_assets(
            cpu_archive,
            model,
            cuda_runtime_payload=cuda_archive,
        )
        downloader = FixtureDownloader(
            fixture_downloads(
                assets,
                cpu_archive,
                model,
                cuda_runtime=cuda_archive,
            )
        )
        with tempfile.TemporaryDirectory() as folder:
            installed = install_runtime(
                Path(folder) / "runtime",
                downloader=downloader,
                assets=assets,
                cuda_detector=lambda: True,
            )
            self.assertEqual("cuda", installed.acceleration)
            self.assertEqual(b"CUDA server", installed.server_path.read_bytes())
            self.assertEqual(
                b"CPU server", installed.fallback_server_path.read_bytes()
            )
            self.assertNotEqual(
                installed.server_path, installed.fallback_server_path
            )
            self.assertIn("cpu", installed.library_path.parts)
        self.assertEqual(4, len(downloader.calls))

    def test_explicit_repair_can_upgrade_valid_cpu_runtime_to_cuda(self):
        cpu_archive = runtime_zip(server_payload=b"CPU server")
        cuda_archive = runtime_zip(server_payload=b"CUDA server")
        model = b"verified model fixture"
        assets = fixture_assets(
            cpu_archive,
            model,
            cuda_runtime_payload=cuda_archive,
        )
        downloader = FixtureDownloader(
            fixture_downloads(
                assets,
                cpu_archive,
                model,
                cuda_runtime=cuda_archive,
            )
        )
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "runtime"
            cpu = install_runtime(
                root,
                downloader=downloader,
                assets=assets,
                cuda_detector=lambda: False,
            )
            self.assertEqual("cpu", cpu.acceleration)

            cuda = install_runtime(
                root,
                downloader=downloader,
                assets=assets,
                cuda_detector=lambda: True,
                retry_cuda_upgrade=True,
            )

            self.assertEqual("cuda", cuda.acceleration)
            self.assertEqual(b"CUDA server", cuda.server_path.read_bytes())
            self.assertEqual("cuda", detect_installed_runtime(root, assets=assets).acceleration)
        self.assertEqual(7, len(downloader.calls))

    def test_second_install_removes_the_generation_it_replaced(self):
        cpu_archive = runtime_zip(server_payload=b"CPU server")
        cuda_archive = runtime_zip(server_payload=b"CUDA server")
        model = b"verified model fixture"
        assets = fixture_assets(
            cpu_archive,
            model,
            cuda_runtime_payload=cuda_archive,
        )
        downloader = FixtureDownloader(
            fixture_downloads(
                assets,
                cpu_archive,
                model,
                cuda_runtime=cuda_archive,
            )
        )
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "runtime"
            cpu = install_runtime(
                root,
                downloader=downloader,
                assets=assets,
                cuda_detector=lambda: False,
            )
            superseded = {path.name for path in (root / "installs").iterdir()}

            cuda = install_runtime(
                root,
                downloader=downloader,
                assets=assets,
                cuda_detector=lambda: True,
                retry_cuda_upgrade=True,
            )

            remaining = {path.name for path in (root / "installs").iterdir()}
            self.assertEqual(1, len(remaining))
            self.assertEqual(set(), remaining & superseded)
            self.assertFalse(cpu.server_path.exists())
            self.assertTrue(cuda.server_path.is_file())
            self.assertEqual(
                cuda.server_path,
                detect_installed_runtime(root, assets=assets).server_path,
            )

    def test_valid_cpu_runtime_does_not_probe_or_download_without_explicit_retry(self):
        archive = runtime_zip()
        model = b"verified model fixture"
        assets = fixture_assets(archive, model)
        downloader = FixtureDownloader(fixture_downloads(assets, archive, model))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "runtime"
            installed = install_runtime(
                root,
                downloader=downloader,
                assets=assets,
                cuda_detector=lambda: False,
            )
            calls_before = tuple(downloader.calls)
            detector = mock.Mock(side_effect=AssertionError("unexpected CUDA probe"))

            detected = install_runtime(
                root,
                downloader=downloader,
                assets=assets,
                cuda_detector=detector,
            )

            self.assertEqual(installed.server_path, detected.server_path)
            self.assertEqual(calls_before, tuple(downloader.calls))
            detector.assert_not_called()

    def test_one_click_install_enables_cuda_retry_for_repair(self):
        adapter = AdapterPreparation(True, "ready", (22, 12, 0))
        with (
            mock.patch.object(
                runtime_installer_module,
                "install_runtime",
                return_value=mock.sentinel.runtime,
            ) as installer,
            mock.patch.object(
                runtime_installer_module,
                "prepare_discord_adapter",
                return_value=adapter,
            ),
        ):
            outcome = runtime_installer_module.install_voice_dependencies()

        self.assertIs(mock.sentinel.runtime, outcome.runtime)
        self.assertTrue(installer.call_args.kwargs["retry_cuda_upgrade"])

    def test_failed_cuda_verification_falls_back_without_activating_bad_bytes(self):
        cpu_archive = runtime_zip(server_payload=b"CPU server")
        expected_cuda = runtime_zip(server_payload=b"expected CUDA server")
        model = b"verified model fixture"
        assets = fixture_assets(
            cpu_archive,
            model,
            cuda_runtime_payload=expected_cuda,
        )
        downloader = FixtureDownloader(
            fixture_downloads(
                assets,
                cpu_archive,
                model,
                cuda_runtime=b"not the verified CUDA archive",
            )
        )
        progress = []
        with tempfile.TemporaryDirectory() as folder:
            installed = install_runtime(
                Path(folder) / "runtime",
                downloader=downloader,
                assets=assets,
                progress=progress.append,
                cuda_detector=lambda: True,
            )

        self.assertEqual("cpu", installed.acceleration)
        self.assertEqual(installed.server_path, installed.fallback_server_path)
        self.assertIn("cuda-fallback", [update.phase for update in progress])

    def test_hash_failure_never_activates_partial_install(self):
        archive = runtime_zip()
        model = b"expected model"
        assets = fixture_assets(archive, model)
        downloader = FixtureDownloader(
            fixture_downloads(assets, archive, b"wrong model")
        )
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "runtime"
            with self.assertRaises(IntegrityError):
                install_runtime(
                    root,
                    downloader=downloader,
                    assets=assets,
                    cuda_detector=lambda: False,
                )
            self.assertFalse((root / "active.json").exists())
            self.assertEqual([], list((root / "installs").iterdir()))

    def test_runtime_without_vad_dependency_dlls_is_not_activated(self):
        archive = runtime_zip(include_vad_dependencies=False)
        model = b"expected model"
        assets = fixture_assets(archive, model)
        downloader = FixtureDownloader(fixture_downloads(assets, archive, model))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "runtime"
            with self.assertRaises(RuntimeInstallError):
                install_runtime(
                    root,
                    downloader=downloader,
                    assets=assets,
                    cuda_detector=lambda: False,
                )
            self.assertFalse((root / "active.json").exists())
            self.assertEqual([], list((root / "installs").iterdir()))

    def test_failed_repair_preserves_previous_active_pointer(self):
        archive = runtime_zip()
        model = b"expected model"
        assets = fixture_assets(archive, model)
        good = FixtureDownloader(fixture_downloads(assets, archive, model))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "runtime"
            installed = install_runtime(
                root,
                downloader=good,
                assets=assets,
                cuda_detector=lambda: False,
            )
            active_before = (root / "active.json").read_bytes()
            installed.server_path.unlink()
            bad = FixtureDownloader(
                fixture_downloads(assets, archive, b"wrong model")
            )
            with self.assertRaises(IntegrityError):
                install_runtime(
                    root,
                    downloader=bad,
                    assets=assets,
                    cuda_detector=lambda: False,
                )
            self.assertEqual(active_before, (root / "active.json").read_bytes())
            self.assertFalse(any(path.name.startswith(".install-") for path in (root / "installs").iterdir()))

    def test_failed_final_candidate_check_preserves_active_runtime_and_cleans_candidate(self):
        cpu_archive = runtime_zip(server_payload=b"CPU server")
        cuda_archive = runtime_zip(server_payload=b"CUDA server")
        model = b"verified model fixture"
        assets = fixture_assets(
            cpu_archive,
            model,
            cuda_runtime_payload=cuda_archive,
        )
        downloader = FixtureDownloader(
            fixture_downloads(
                assets,
                cpu_archive,
                model,
                cuda_runtime=cuda_archive,
            )
        )
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "runtime"
            installed = install_runtime(
                root,
                downloader=downloader,
                assets=assets,
                cuda_detector=lambda: False,
            )
            active_before = (root / "active.json").read_bytes()
            installs_before = {path.name for path in (root / "installs").iterdir()}
            real_validator = runtime_installer_module._runtime_from_manifest

            def reject_cuda_candidate(candidate_root, manifest, **kwargs):
                if manifest.get("acceleration") == "cuda":
                    return None
                return real_validator(candidate_root, manifest, **kwargs)

            with mock.patch.object(
                runtime_installer_module,
                "_runtime_from_manifest",
                side_effect=reject_cuda_candidate,
            ):
                with self.assertRaises(RuntimeInstallError):
                    install_runtime(
                        root,
                        downloader=downloader,
                        assets=assets,
                        cuda_detector=lambda: True,
                        retry_cuda_upgrade=True,
                    )

            self.assertEqual(active_before, (root / "active.json").read_bytes())
            self.assertEqual(
                installs_before,
                {path.name for path in (root / "installs").iterdir()},
            )
            self.assertEqual(
                installed.server_path,
                detect_installed_runtime(root, assets=assets).server_path,
            )

    def test_schema_one_pointer_is_preserved_but_not_advertised_as_ready(self):
        archive = runtime_zip()
        model = b"expected model"
        assets = fixture_assets(archive, model)
        downloader = FixtureDownloader(
            fixture_downloads(assets, archive, b"wrong model")
        )
        legacy_manifest = (
            b'{"schema":1,"version":"b4938","legacy":"preserve-me"}\n'
        )
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "runtime"
            root.mkdir(parents=True)
            (root / "active.json").write_bytes(legacy_manifest)
            self.assertEqual({}, stt_environment(root, assets=assets))
            with self.assertRaises(IntegrityError):
                install_runtime(
                    root,
                    downloader=downloader,
                    assets=assets,
                    cuda_detector=lambda: False,
                )
            self.assertEqual(legacy_manifest, (root / "active.json").read_bytes())

    def test_modified_model_is_not_returned_for_use(self):
        archive = runtime_zip()
        model = b"expected model"
        assets = fixture_assets(archive, model)
        downloader = FixtureDownloader(fixture_downloads(assets, archive, model))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "runtime"
            installed = install_runtime(
                root,
                downloader=downloader,
                assets=assets,
                cuda_detector=lambda: False,
            )
            installed.model_path.write_bytes(b"modified")
            self.assertIsNone(detect_installed_runtime(root, assets=assets))
            self.assertEqual({}, stt_environment(root, assets=assets))

    def test_modified_vad_model_is_not_returned_for_use(self):
        archive = runtime_zip()
        model = b"expected model"
        assets = fixture_assets(archive, model)
        downloader = FixtureDownloader(fixture_downloads(assets, archive, model))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "runtime"
            installed = install_runtime(
                root,
                downloader=downloader,
                assets=assets,
                cuda_detector=lambda: False,
            )
            installed.vad_model_path.write_bytes(b"modified")
            self.assertIsNone(detect_installed_runtime(root, assets=assets))
            self.assertEqual({}, stt_environment(root, assets=assets))

    def test_modified_vad_dependency_is_not_returned_for_use(self):
        archive = runtime_zip()
        model = b"expected model"
        assets = fixture_assets(archive, model)
        downloader = FixtureDownloader(fixture_downloads(assets, archive, model))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "runtime"
            installed = install_runtime(
                root,
                downloader=downloader,
                assets=assets,
                cuda_detector=lambda: False,
            )
            installed.library_path.with_name("ggml.dll").write_bytes(b"modified")
            self.assertIsNone(detect_installed_runtime(root, assets=assets))
            self.assertEqual({}, stt_environment(root, assets=assets))

    def test_unlisted_ggml_backend_is_not_returned_for_use(self):
        archive = runtime_zip()
        model = b"expected model"
        assets = fixture_assets(archive, model)
        downloader = FixtureDownloader(fixture_downloads(assets, archive, model))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "runtime"
            installed = install_runtime(
                root,
                downloader=downloader,
                assets=assets,
                cuda_detector=lambda: False,
            )
            installed.library_path.with_name("ggml-cpu-rogue.dll").write_bytes(
                b"unlisted backend"
            )
            self.assertIsNone(detect_installed_runtime(root, assets=assets))
            self.assertEqual({}, stt_environment(root, assets=assets))

    def test_cuda_backend_beside_the_server_does_not_fail_the_check(self):
        """A real CUDA install must survive its own ggml-cuda.dll.

        The unlisted-backend sweep once covered the server's directory too.
        It could never pass there: dependency digests are required to live in
        the library's directory, so ggml-cuda.dll - which only ever exists
        beside the CUDA server - can never be named by the manifest, and every
        CUDA install was rejected with "did not pass its final check".

        It shipped because the earlier fixture built the CUDA archive from the
        same zip as the CPU one, so the fixture had no ggml-cuda.dll and the
        remaining names all collided with hashed CPU entries. The file that
        distinguishes a CUDA runtime was the one thing the fixture did not
        model. This test adds it, so the shape of a real install is what gets
        checked.
        """
        cpu_archive = runtime_zip(server_payload=b"CPU server")
        cuda_archive = runtime_zip(
            server_payload=b"CUDA server",
            extra_files={"Release/ggml-cuda.dll": b"cuda backend"},
        )
        model = b"expected model"
        assets = fixture_assets(
            cpu_archive, model, cuda_runtime_payload=cuda_archive
        )
        downloader = FixtureDownloader(
            fixture_downloads(assets, cpu_archive, model, cuda_runtime=cuda_archive)
        )
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "runtime"
            installed = install_runtime(
                root,
                downloader=downloader,
                assets=assets,
                cuda_detector=lambda: True,
            )
            # Without this the assertion below proves nothing.
            self.assertNotEqual(
                installed.server_path.parent, installed.library_path.parent
            )
            self.assertTrue(
                installed.server_path.with_name("ggml-cuda.dll").is_file(),
                "fixture must model the file that only a CUDA runtime ships",
            )
            self.assertIsNotNone(detect_installed_runtime(root, assets=assets))
            self.assertNotEqual({}, stt_environment(root, assets=assets))


class CudaDetectionTests(unittest.TestCase):
    def test_compatible_driver_is_selected_with_bounded_hidden_probe(self):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, "595.97\n", "")

        with tempfile.TemporaryDirectory() as folder:
            nvidia_smi = Path(folder) / "nvidia-smi.exe"
            nvidia_smi.touch()
            with mock.patch.dict(
                os.environ,
                {"PROGRAMDATA": r"C:\ProgramData", "BOT_TOKEN": "secret"},
            ):
                self.assertTrue(
                    cuda_12_4_available(
                        runner=runner,
                        nvidia_smi_path=nvidia_smi,
                    )
                )

        self.assertEqual(1, len(calls))
        command, options = calls[0]
        self.assertIn("--query-gpu=driver_version", command)
        self.assertFalse(options["shell"])
        self.assertLessEqual(options["timeout"], 5.0)
        self.assertEqual(r"C:\ProgramData", options["env"]["PROGRAMDATA"])
        self.assertNotIn("BOT_TOKEN", options["env"])

    def test_old_or_unreadable_driver_uses_cpu(self):
        with tempfile.TemporaryDirectory() as folder:
            nvidia_smi = Path(folder) / "nvidia-smi.exe"
            nvidia_smi.touch()

            def old_driver(command, **kwargs):
                return subprocess.CompletedProcess(command, 0, "551.60\n", "")

            def failed_probe(command, **kwargs):
                return subprocess.CompletedProcess(command, 1, "private", "")

            self.assertFalse(
                cuda_12_4_available(
                    runner=old_driver,
                    nvidia_smi_path=nvidia_smi,
                )
            )
            self.assertFalse(
                cuda_12_4_available(
                    runner=failed_probe,
                    nvidia_smi_path=nvidia_smi,
                )
            )

class SafeZipTests(unittest.TestCase):
    def _write_zip(self, path: Path, entries) -> None:
        with zipfile.ZipFile(path, "w") as archive:
            for entry in entries:
                if isinstance(entry, zipfile.ZipInfo):
                    archive.writestr(entry, b"target")
                else:
                    name, payload = entry
                    archive.writestr(name, payload)

    def test_rejects_parent_absolute_drive_and_backslash_traversal(self):
        unsafe_names = (
            "../escape.exe",
            "/absolute.exe",
            "C:/drive.exe",
            "folder\\..\\escape.exe",
        )
        for unsafe_name in unsafe_names:
            with self.subTest(unsafe_name=unsafe_name), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                archive = root / "unsafe.zip"
                self._write_zip(archive, [(unsafe_name, b"bad")])
                with self.assertRaises(UnsafeArchiveError):
                    safe_extract_zip(archive, root / "out")
                self.assertFalse((root / "escape.exe").exists())

    def test_rejects_symbolic_links_and_case_collisions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            symlink_archive = root / "link.zip"
            link = zipfile.ZipInfo("link")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            self._write_zip(symlink_archive, [link])
            with self.assertRaises(UnsafeArchiveError):
                safe_extract_zip(symlink_archive, root / "links")

            collision_archive = root / "collision.zip"
            self._write_zip(
                collision_archive,
                [("Bin/Server.exe", b"a"), ("bin/server.exe", b"b")],
            )
            with self.assertRaises(UnsafeArchiveError):
                safe_extract_zip(collision_archive, root / "collision")


class FakeResponse:
    def __init__(self, chunks, content_length=None, status=200):
        self._chunks = iter(chunks)
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, _size):
        return next(self._chunks, b"")


class BoundedDownloadTests(unittest.TestCase):
    def test_stream_rejects_declared_and_actual_oversize(self):
        asset = DownloadAsset("fixture", "https://example.invalid/file", "0" * 64, 1, 4, 10)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "asset"
            with self.assertRaises(DownloadError):
                download_asset(
                    asset,
                    path,
                    opener=lambda request, timeout: FakeResponse([], content_length=5),
                )
            self.assertFalse(path.exists())
            with self.assertRaises(DownloadError):
                download_asset(
                    asset,
                    path,
                    opener=lambda request, timeout: FakeResponse([b"123", b"45"]),
                )
            self.assertFalse(path.exists())

    def test_stream_enforces_total_elapsed_time(self):
        asset = DownloadAsset("fixture", "https://example.invalid/file", "0" * 64, 1, 10, 1)
        clock_values = iter((0.0, 0.5, 1.5))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "asset"
            with self.assertRaises(DownloadError):
                download_asset(
                    asset,
                    path,
                    opener=lambda request, timeout: FakeResponse([b"123", b"456"]),
                    clock=lambda: next(clock_values),
                )
            self.assertFalse(path.exists())


class FakeCompletedProcess:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


class AdapterPreparationTests(unittest.TestCase):
    def _layout(self, root: Path):
        node = root / "node.exe"
        node.write_bytes(b"node")
        npm_cli = root / "node_modules" / "npm" / "bin" / "npm-cli.js"
        npm_cli.parent.mkdir(parents=True)
        npm_cli.write_text("npm", encoding="ascii")
        adapter = root / "adapter"
        adapter.mkdir()
        (adapter / "package.json").write_text("{}", encoding="ascii")
        (adapter / "package-lock.json").write_text("{}", encoding="ascii")
        return node, npm_cli, adapter

    def test_supported_node_runs_locked_hidden_bounded_npm_without_shell(self):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            if command[1] == "--version":
                return FakeCompletedProcess(stdout="v22.12.0\n")
            return FakeCompletedProcess(returncode=0)

        with tempfile.TemporaryDirectory() as folder:
            node, npm_cli, adapter = self._layout(Path(folder))
            result = prepare_discord_adapter(
                adapter,
                runner=runner,
                node_path=node,
                npm_cli_path=npm_cli,
            )
        self.assertEqual(AdapterPreparation(True, "Discord voice helper is ready.", (22, 12, 0)), result)
        self.assertEqual(2, len(calls))
        command, options = calls[1]
        self.assertEqual(
            [
                str(node),
                str(npm_cli),
                "ci",
                "--omit=dev",
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
            ],
            command,
        )
        self.assertFalse(options["shell"])
        self.assertLessEqual(options["timeout"], 300)
        self.assertEqual(subprocess.DEVNULL, options["stdout"])
        self.assertEqual(subprocess.DEVNULL, options["stderr"])
        self.assertNotIn("BOT_TOKEN", options["env"])

    def test_old_node_never_runs_npm(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return FakeCompletedProcess(stdout="v22.11.0")

        with tempfile.TemporaryDirectory() as folder:
            node, npm_cli, adapter = self._layout(Path(folder))
            result = prepare_discord_adapter(
                adapter,
                runner=runner,
                node_path=node,
                npm_cli_path=npm_cli,
            )
        self.assertFalse(result.ready)
        self.assertIn("Node 22.12", result.detail)
        self.assertEqual(1, len(calls))

    def test_npm_timeout_returns_only_safe_status(self):
        def runner(command, **kwargs):
            if command[1] == "--version":
                return FakeCompletedProcess(stdout="v23.0.0")
            raise subprocess.TimeoutExpired(command, kwargs["timeout"], output="secret")

        with tempfile.TemporaryDirectory() as folder:
            node, npm_cli, adapter = self._layout(Path(folder))
            result = prepare_discord_adapter(
                adapter,
                runner=runner,
                node_path=node,
                npm_cli_path=npm_cli,
            )
        self.assertFalse(result.ready)
        self.assertEqual("The Discord voice helper setup took too long. Try again.", result.detail)
        self.assertNotIn("secret", result.detail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
