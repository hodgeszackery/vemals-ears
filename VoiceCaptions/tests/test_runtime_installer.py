import dataclasses
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
    OFFICIAL_ASSETS,
    RUNTIME_SHA256,
    RUNTIME_URL,
    SERVER_ENV,
    VAD_MODEL_ENV,
    VAD_MODEL_SHA256,
    VAD_MODEL_URL,
    VULKAN_RUNTIME_SHA256,
    VULKAN_RUNTIME_URL,
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
    select_acceleration,
    stt_environment,
    vulkan_gpu_available,
    vulkan_runtime_pinned,
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
    vulkan_runtime_payload: bytes | None = None,
) -> RuntimeAssets:
    """Vulkan stays unpinned (None) unless a payload is given."""
    cuda_runtime_payload = cuda_runtime_payload or runtime_zip(
        server_payload=b"verified cuda whisper server"
    )
    vulkan_runtime = None
    if vulkan_runtime_payload is not None:
        vulkan_runtime = DownloadAsset(
            "test Vulkan runtime",
            "fixture://vulkan-runtime",
            sha256(vulkan_runtime_payload),
            1,
            1024 * 1024,
            10,
        )
    return RuntimeAssets(
        vulkan_runtime=vulkan_runtime,
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
    vulkan_runtime: bytes | None = None,
) -> dict[str, bytes]:
    payloads = {
        assets.cpu_runtime.url: cpu_runtime,
        assets.model.url: model,
        assets.vad_model.url: vad_model,
    }
    if cuda_runtime is not None:
        payloads[assets.cuda_runtime.url] = cuda_runtime
    if vulkan_runtime is not None:
        payloads["fixture://vulkan-runtime"] = vulkan_runtime
    return payloads


def vulkan_zip(
    server_payload: bytes = b"Vulkan server",
    *,
    include_backend: bool = True,
) -> bytes:
    """Shaped like the upstream CUDA zip, ggml-vulkan.dll for ggml-cuda.dll."""
    extra = {"Release/ggml-vulkan.dll": b"vulkan backend"} if include_backend else {}
    return runtime_zip(server_payload=server_payload, extra_files=extra)


def unpinned_vulkan(assets: RuntimeAssets) -> RuntimeAssets:
    """The production sentinel shape: an asset whose url and hash are empty."""
    return dataclasses.replace(
        assets,
        vulkan_runtime=DownloadAsset("test Vulkan runtime", "", "", 1, 1024, 10),
    )


def never(name: str):
    return mock.Mock(side_effect=AssertionError("unexpected " + name + " probe"))


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
        # Covers both GPU engines now; retry_cuda_upgrade is its old name.
        self.assertTrue(installer.call_args.kwargs["retry_gpu_upgrade"])

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


class VulkanRuntimeTests(unittest.TestCase):
    """Mirrors the CUDA install cases for the AMD/Vulkan engine."""

    def setUp(self):
        self.cpu_archive = runtime_zip(server_payload=b"CPU server")
        self.cuda_archive = runtime_zip(
            server_payload=b"CUDA server",
            extra_files={"Release/ggml-cuda.dll": b"cuda backend"},
        )
        self.vulkan_archive = vulkan_zip()
        self.model = b"verified model fixture"
        self.assets = fixture_assets(
            self.cpu_archive,
            self.model,
            cuda_runtime_payload=self.cuda_archive,
            vulkan_runtime_payload=self.vulkan_archive,
        )

    def downloader(self, *, vulkan=None, cuda=None):
        return FixtureDownloader(
            fixture_downloads(
                self.assets,
                self.cpu_archive,
                self.model,
                cuda_runtime=self.cuda_archive if cuda is None else cuda,
                vulkan_runtime=self.vulkan_archive if vulkan is None else vulkan,
            )
        )

    def install(self, root, downloader, **kwargs):
        kwargs.setdefault("acceleration_preference", "auto")
        return install_runtime(
            root, downloader=downloader, assets=self.assets, **kwargs
        )

    def test_production_vulkan_asset_is_an_explicit_unset_sentinel(self):
        # Pinning the CI-built zip must update this test with the exact
        # values, the same way test_production_assets_are_exactly_pinned
        # holds the upstream ones.
        self.assertEqual("", VULKAN_RUNTIME_URL)
        self.assertEqual("", VULKAN_RUNTIME_SHA256)
        self.assertEqual("AMD/Vulkan speech engine", OFFICIAL_ASSETS.vulkan_runtime.name)
        self.assertEqual(VULKAN_RUNTIME_URL, OFFICIAL_ASSETS.vulkan_runtime.url)
        self.assertFalse(vulkan_runtime_pinned())
        self.assertFalse(vulkan_runtime_pinned(OFFICIAL_ASSETS))
        self.assertEqual("NVIDIA", runtime_installer_module.gpu_support_label())

    def test_pinned_requires_both_a_url_and_a_full_sha256(self):
        self.assertTrue(vulkan_runtime_pinned(self.assets))
        for url, digest in (("", "a" * 64), ("https://x/y.zip", ""), ("https://x/y.zip", "a" * 63)):
            with self.subTest(url=url, digest=digest):
                assets = dataclasses.replace(
                    self.assets,
                    vulkan_runtime=DownloadAsset("v", url, digest, 1, 2, 3),
                )
                self.assertFalse(vulkan_runtime_pinned(assets))
        self.assertFalse(vulkan_runtime_pinned(dataclasses.replace(self.assets, vulkan_runtime=None)))
        self.assertEqual("NVIDIA or AMD", runtime_installer_module.gpu_support_label(self.assets))

    def test_unpinned_vulkan_is_never_probed_and_falls_back_like_before(self):
        assets = unpinned_vulkan(self.assets)
        downloader = FixtureDownloader(
            fixture_downloads(assets, self.cpu_archive, self.model)
        )
        detector = never("Vulkan")
        with tempfile.TemporaryDirectory() as folder:
            installed = install_runtime(
                Path(folder) / "runtime",
                downloader=downloader,
                assets=assets,
                cuda_detector=lambda: False,
                vulkan_detector=detector,
                acceleration_preference="vulkan",
            )
        self.assertEqual("cpu", installed.acceleration)
        self.assertEqual(3, len(downloader.calls))
        detector.assert_not_called()

    def test_unpinned_vulkan_leaves_cuda_installs_unchanged(self):
        assets = unpinned_vulkan(self.assets)
        downloader = FixtureDownloader(
            fixture_downloads(assets, self.cpu_archive, self.model, cuda_runtime=self.cuda_archive)
        )
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "runtime"
            installed = install_runtime(
                root,
                downloader=downloader,
                assets=assets,
                cuda_detector=lambda: True,
                vulkan_detector=never("Vulkan"),
            )
            self.assertEqual("cuda", installed.acceleration)
            manifest = json.loads((root / "active.json").read_text(encoding="ascii"))
            self.assertIsNone(manifest["vulkan_runtime_archive_sha256"])
            self.assertEqual("cuda", detect_installed_runtime(root, assets=assets).acceleration)

    def test_vulkan_runtime_is_primary_when_cuda_is_unusable(self):
        downloader = self.downloader()
        progress = []
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "runtime"
            installed = self.install(
                root,
                downloader,
                progress=progress.append,
                cuda_detector=lambda: False,
                vulkan_detector=lambda: True,
            )
            self.assertEqual("vulkan", installed.acceleration)
            self.assertEqual(b"Vulkan server", installed.server_path.read_bytes())
            self.assertEqual(b"CPU server", installed.fallback_server_path.read_bytes())
            self.assertIn("cpu", installed.library_path.parts)
            self.assertIn("vulkan", installed.server_path.parts)
            manifest = json.loads((root / "active.json").read_text(encoding="ascii"))
            self.assertEqual("vulkan", manifest["acceleration"])
            self.assertEqual(self.assets.vulkan_runtime.sha256, manifest["vulkan_runtime_archive_sha256"])
            self.assertIsNone(manifest["cuda_runtime_archive_sha256"])
            self.assertEqual(
                "vulkan",
                stt_environment(root, assets=self.assets)["VOICE_CAPTIONS_WHISPER_ACCELERATION"],
            )
        self.assertEqual(4, len(downloader.calls))
        self.assertEqual("fixture://vulkan-runtime", downloader.calls[1][1])
        phases = [update.phase for update in progress]
        self.assertIn("vulkan-download", phases)
        self.assertIn("vulkan-verify", phases)
        self.assertNotIn("cuda-download", phases)
        self.assertEqual("Local speech engine is ready with AMD/Vulkan acceleration.", progress[-1].message)

    def test_cuda_is_selected_before_vulkan(self):
        downloader = self.downloader()
        detector = never("Vulkan")
        with tempfile.TemporaryDirectory() as folder:
            installed = self.install(
                Path(folder) / "runtime",
                downloader,
                cuda_detector=lambda: True,
                vulkan_detector=detector,
            )
        self.assertEqual("cuda", installed.acceleration)
        detector.assert_not_called()

    def test_no_gpu_uses_cpu(self):
        downloader = self.downloader()
        with tempfile.TemporaryDirectory() as folder:
            installed = self.install(
                Path(folder) / "runtime",
                downloader,
                cuda_detector=lambda: False,
                vulkan_detector=lambda: False,
            )
        self.assertEqual("cpu", installed.acceleration)
        self.assertEqual(3, len(downloader.calls))

    def test_explicit_repair_can_upgrade_valid_cpu_runtime_to_vulkan(self):
        downloader = self.downloader()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "runtime"
            cpu = self.install(root, downloader, cuda_detector=lambda: False, vulkan_detector=lambda: False)
            self.assertEqual("cpu", cpu.acceleration)
            vulkan = self.install(
                root,
                downloader,
                cuda_detector=lambda: False,
                vulkan_detector=lambda: True,
                retry_gpu_upgrade=True,
            )
            self.assertEqual("vulkan", vulkan.acceleration)
            self.assertEqual("vulkan", detect_installed_runtime(root, assets=self.assets).acceleration)
            self.assertEqual(1, len(list((root / "installs").iterdir())))
        self.assertEqual(7, len(downloader.calls))

    def test_legacy_retry_cuda_upgrade_flag_also_upgrades_to_vulkan(self):
        downloader = self.downloader()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "runtime"
            self.install(root, downloader, cuda_detector=lambda: False, vulkan_detector=lambda: False)
            upgraded = self.install(
                root,
                downloader,
                cuda_detector=lambda: False,
                vulkan_detector=lambda: True,
                retry_cuda_upgrade=True,
            )
        self.assertEqual("vulkan", upgraded.acceleration)

    def test_explicit_repair_upgrades_vulkan_to_cuda_but_never_reinstalls_vulkan(self):
        downloader = self.downloader()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "runtime"
            vulkan = self.install(root, downloader, cuda_detector=lambda: False, vulkan_detector=lambda: True)
            calls = len(downloader.calls)
            same = self.install(
                root,
                downloader,
                cuda_detector=lambda: False,
                vulkan_detector=lambda: True,
                retry_gpu_upgrade=True,
            )
            self.assertEqual(vulkan.server_path, same.server_path)
            self.assertEqual(calls, len(downloader.calls))
            cuda = self.install(
                root,
                downloader,
                cuda_detector=lambda: True,
                vulkan_detector=never("Vulkan"),
                retry_gpu_upgrade=True,
            )
            self.assertEqual("cuda", cuda.acceleration)
            self.assertFalse(vulkan.server_path.exists())

    def test_valid_cuda_runtime_is_never_probed_for_vulkan(self):
        downloader = self.downloader()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "runtime"
            installed = self.install(root, downloader, cuda_detector=lambda: True)
            calls = len(downloader.calls)
            again = self.install(
                root,
                downloader,
                cuda_detector=never("CUDA"),
                vulkan_detector=never("Vulkan"),
                retry_gpu_upgrade=True,
            )
        self.assertEqual(installed.server_path, again.server_path)
        self.assertEqual(calls, len(downloader.calls))

    def test_valid_vulkan_runtime_does_not_probe_or_download_without_explicit_retry(self):
        downloader = self.downloader()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "runtime"
            installed = self.install(root, downloader, cuda_detector=lambda: False, vulkan_detector=lambda: True)
            calls = len(downloader.calls)
            again = self.install(
                root,
                downloader,
                cuda_detector=never("CUDA"),
                vulkan_detector=never("Vulkan"),
            )
        self.assertEqual(installed.server_path, again.server_path)
        self.assertEqual(calls, len(downloader.calls))

    def test_failed_vulkan_verification_falls_back_without_activating_bad_bytes(self):
        downloader = self.downloader(vulkan=b"not the verified Vulkan archive")
        progress = []
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "runtime"
            installed = self.install(
                root,
                downloader,
                progress=progress.append,
                cuda_detector=lambda: False,
                vulkan_detector=lambda: True,
            )
            manifest = json.loads((root / "active.json").read_text(encoding="ascii"))
        self.assertEqual("cpu", installed.acceleration)
        self.assertEqual(installed.server_path, installed.fallback_server_path)
        self.assertIsNone(manifest["vulkan_runtime_archive_sha256"])
        fallback = [update for update in progress if update.phase == "vulkan-fallback"]
        self.assertEqual(
            "AMD/Vulkan acceleration was unavailable; using compatibility mode.",
            fallback[0].message,
        )

    def test_vulkan_archive_without_its_backend_falls_back_to_cpu(self):
        archive = vulkan_zip(include_backend=False)
        self.assets = fixture_assets(
            self.cpu_archive, self.model, vulkan_runtime_payload=archive
        )
        downloader = self.downloader(vulkan=archive)
        progress = []
        with tempfile.TemporaryDirectory() as folder:
            installed = self.install(
                Path(folder) / "runtime",
                downloader,
                progress=progress.append,
                cuda_detector=lambda: False,
                vulkan_detector=lambda: True,
            )
        self.assertEqual("cpu", installed.acceleration)
        self.assertIn("vulkan-fallback", [update.phase for update in progress])

    def test_vulkan_backend_beside_the_server_does_not_fail_the_check(self):
        downloader = self.downloader()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "runtime"
            installed = self.install(root, downloader, cuda_detector=lambda: False, vulkan_detector=lambda: True)
            self.assertNotEqual(installed.server_path.parent, installed.library_path.parent)
            self.assertTrue(installed.server_path.with_name("ggml-vulkan.dll").is_file())
            self.assertIsNotNone(detect_installed_runtime(root, assets=self.assets))
            self.assertNotEqual({}, stt_environment(root, assets=self.assets))

    def test_ggml_vulkan_dropped_beside_the_library_is_rejected(self):
        downloader = self.downloader()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "runtime"
            installed = self.install(root, downloader, cuda_detector=lambda: False, vulkan_detector=lambda: False)
            installed.library_path.with_name("ggml-vulkan.dll").write_bytes(b"unlisted")
            self.assertIsNone(detect_installed_runtime(root, assets=self.assets))
            self.assertEqual({}, stt_environment(root, assets=self.assets))

    def test_vulkan_manifest_round_trips_and_fails_closed(self):
        downloader = self.downloader()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "runtime"
            installed = self.install(root, downloader, cuda_detector=lambda: False, vulkan_detector=lambda: True)
            detected = detect_installed_runtime(root, assets=self.assets)
            self.assertEqual(installed, detected)
            # The constant being unset again (or never set) must not trust it.
            self.assertIsNone(detect_installed_runtime(root, assets=unpinned_vulkan(self.assets)))
            self.assertIsNone(
                detect_installed_runtime(
                    root, assets=dataclasses.replace(self.assets, vulkan_runtime=None)
                )
            )
            manifest_path = root / "active.json"
            original = json.loads(manifest_path.read_text(encoding="ascii"))
            for change in (
                {"vulkan_runtime_archive_sha256": "0" * 64},
                {"vulkan_runtime_archive_sha256": None},
                {"cuda_runtime_archive_sha256": self.assets.cuda_runtime.sha256},
                {"acceleration": "metal"},
            ):
                with self.subTest(change=change):
                    manifest_path.write_text(json.dumps({**original, **change}), encoding="ascii")
                    self.assertIsNone(detect_installed_runtime(root, assets=self.assets))
            manifest_path.write_text(json.dumps(original), encoding="ascii")
            self.assertIsNotNone(detect_installed_runtime(root, assets=self.assets))

    def test_manifest_written_before_vulkan_existed_still_validates(self):
        downloader = self.downloader()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "runtime"
            self.install(root, downloader, cuda_detector=lambda: True)
            manifest_path = root / "active.json"
            manifest = json.loads(manifest_path.read_text(encoding="ascii"))
            del manifest["vulkan_runtime_archive_sha256"]
            manifest_path.write_text(json.dumps(manifest), encoding="ascii")
            self.assertEqual("cuda", detect_installed_runtime(root, assets=self.assets).acceleration)

    def test_failed_final_vulkan_candidate_preserves_active_runtime(self):
        downloader = self.downloader()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "runtime"
            self.install(root, downloader, cuda_detector=lambda: False, vulkan_detector=lambda: False)
            active_before = (root / "active.json").read_bytes()
            installs_before = {path.name for path in (root / "installs").iterdir()}
            real_validator = runtime_installer_module._runtime_from_manifest

            def reject_vulkan(candidate_root, manifest, **kwargs):
                if manifest.get("acceleration") == "vulkan":
                    return None
                return real_validator(candidate_root, manifest, **kwargs)

            with mock.patch.object(
                runtime_installer_module, "_runtime_from_manifest", side_effect=reject_vulkan
            ):
                with self.assertRaises(RuntimeInstallError):
                    self.install(
                        root,
                        downloader,
                        cuda_detector=lambda: False,
                        vulkan_detector=lambda: True,
                        retry_gpu_upgrade=True,
                    )
            self.assertEqual(active_before, (root / "active.json").read_bytes())
            self.assertEqual(installs_before, {path.name for path in (root / "installs").iterdir()})


class AccelerationSelectionTests(unittest.TestCase):
    def setUp(self):
        self.assets = fixture_assets(
            runtime_zip(), b"model", vulkan_runtime_payload=vulkan_zip()
        )

    def select(self, cuda, vulkan, preference="auto", assets=None):
        return select_acceleration(
            assets=assets or self.assets,
            cuda_detector=cuda,
            vulkan_detector=vulkan,
            preference=preference,
        )

    def test_order_is_cuda_then_vulkan_then_cpu(self):
        yes, no = (lambda: True), (lambda: False)
        self.assertEqual("cuda", self.select(yes, never("Vulkan")))
        self.assertEqual("vulkan", self.select(no, yes))
        self.assertEqual("cpu", self.select(no, no))

    def test_preferences_limit_the_candidates_but_never_skip_detection(self):
        yes, no = (lambda: True), (lambda: False)
        self.assertEqual("cpu", self.select(never("CUDA"), never("Vulkan"), "cpu"))
        self.assertEqual("vulkan", self.select(never("CUDA"), yes, "vulkan"))
        self.assertEqual("cpu", self.select(never("CUDA"), no, "vulkan"))
        self.assertEqual("cuda", self.select(yes, never("Vulkan"), "cuda"))
        self.assertEqual("cpu", self.select(no, never("Vulkan"), "cuda"))
        self.assertEqual("vulkan", self.select(no, yes, "nonsense"))

    def test_preference_is_read_from_the_environment(self):
        yes = lambda: True  # noqa: E731
        for value, expected in (("VULKAN", "vulkan"), (" cpu ", "cpu"), ("auto", "cuda"), ("bogus", "cuda")):
            with self.subTest(value=value), mock.patch.dict(
                os.environ,
                {runtime_installer_module.ACCELERATION_PREFERENCE_ENV: value},
            ):
                self.assertEqual(
                    expected,
                    select_acceleration(assets=self.assets, cuda_detector=yes, vulkan_detector=yes),
                )

    def test_unpinned_vulkan_is_not_probed_even_when_preferred(self):
        assets = unpinned_vulkan(self.assets)
        self.assertEqual("cpu", self.select(lambda: False, never("Vulkan"), "auto", assets))
        self.assertEqual("cpu", self.select(never("CUDA"), never("Vulkan"), "vulkan", assets))

    def test_failing_detectors_read_as_unavailable(self):
        def broken():
            raise OSError("probe failed")

        self.assertEqual("vulkan", self.select(broken, lambda: True))
        self.assertEqual("cpu", self.select(broken, broken))


class VulkanDetectionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        self.loader = root / "vulkan-1.dll"
        self.loader.touch()
        self.powershell = root / "powershell.exe"
        self.powershell.touch()
        self.wmic = root / "WMIC.exe"
        self.wmic.touch()
        self.calls = []

    def detect(self, responses, **overrides):
        """responses maps 'powershell'/'wmic' to (returncode, stdout) or an exception."""

        def runner(command, **kwargs):
            self.calls.append((command, kwargs))
            key = "powershell" if Path(command[0]).name == "powershell.exe" else "wmic"
            response = responses[key]
            if isinstance(response, BaseException):
                raise response
            return subprocess.CompletedProcess(command, response[0], response[1], "")

        options = {
            "runner": runner,
            "powershell_path": self.powershell,
            "wmic_path": self.wmic,
            "vulkan_loader_path": self.loader,
            "windows": True,
        }
        options.update(overrides)
        return vulkan_gpu_available(**options)

    def test_amd_adapter_is_detected_with_a_bounded_hidden_probe(self):
        with mock.patch.dict(os.environ, {"BOT_TOKEN": "secret"}):
            self.assertTrue(
                self.detect(
                    {
                        "powershell": (
                            0,
                            "AMD Radeon RX 7800 XT|Advanced Micro Devices, Inc.|31.0.24027.1012\r\n",
                        ),
                        "wmic": AssertionError("wmic must not run"),
                    }
                )
            )
        self.assertEqual(1, len(self.calls))
        command, options = self.calls[0]
        self.assertIn("Win32_VideoController", command[-1])
        self.assertIn("Get-CimInstance", command[-1])
        self.assertFalse(options["shell"])
        self.assertLessEqual(options["timeout"], 15.0)
        self.assertEqual(subprocess.DEVNULL, options["stdin"])
        self.assertNotIn("BOT_TOKEN", options["env"])

    def test_adapter_classification(self):
        cases = (
            ("ATI Radeon HD 5450|ATI Technologies Inc.|15.200.1062.1004", True),
            ("Intel(R) Arc(TM) A770 Graphics|Intel Corporation|32.0.101.5762", True),
            ("NVIDIA GeForce GTX 1060|NVIDIA|30.0.14.7141", True),
            ("Intel(R) UHD Graphics 770|Intel Corporation|31.0.101.4502", False),
            ("Microsoft Basic Display Adapter|(Standard display types)|10.0.22621.1", False),
            ("AMD Radeon RX 6600|Advanced Micro Devices, Inc.|", False),
            ("Parsec Virtual Display Adapter|Parsec Cloud, Inc.|0.45.0.0", False),
        )
        for line, expected in cases:
            with self.subTest(line=line):
                self.assertEqual(
                    expected,
                    self.detect({"powershell": (0, line + "\n"), "wmic": (1, "")}),
                )

    def test_any_capable_adapter_among_several_is_enough(self):
        output = (
            "Intel(R) UHD Graphics 630|Intel Corporation|31.0.101.2111\n"
            "AMD Radeon RX 580|Advanced Micro Devices, Inc.|31.0.21921.1000\n"
        )
        self.assertTrue(self.detect({"powershell": (0, output), "wmic": (1, "")}))

    def test_wmic_fallback_parses_value_records(self):
        output = (
            "\r\r\n\r\r\nAdapterCompatibility=Intel Corporation\r\r\n"
            "DriverVersion=31.0.101.2111\r\r\nName=Intel(R) UHD Graphics 630\r\r\n"
            "\r\r\n\r\r\nAdapterCompatibility=Advanced Micro Devices, Inc.\r\r\n"
            "DriverVersion=31.0.21921.1000\r\r\nName=AMD Radeon RX 580\r\r\n\r\r\n"
        )
        for failure in ((1, ""), subprocess.TimeoutExpired("powershell", 15), OSError("blocked")):
            with self.subTest(failure=failure):
                self.calls.clear()
                self.assertTrue(self.detect({"powershell": failure, "wmic": (0, output)}))
                self.assertEqual(2, len(self.calls))
                command, options = self.calls[1]
                self.assertEqual(
                    ["path", "win32_VideoController", "get", "Name,AdapterCompatibility,DriverVersion", "/value"],
                    command[1:],
                )
                self.assertFalse(options["shell"])

    def test_every_failure_reads_as_unavailable_without_raising(self):
        self.assertFalse(
            self.detect({"powershell": (1, ""), "wmic": subprocess.TimeoutExpired("wmic", 15)})
        )
        self.assertFalse(self.detect({"powershell": (0, "x" * 20_000), "wmic": (1, "")}))
        self.assertFalse(self.detect({"powershell": ValueError("bad"), "wmic": RuntimeError("odd")}))

    def test_missing_loader_or_non_windows_never_runs_a_probe(self):
        responses = {"powershell": AssertionError("no probe"), "wmic": AssertionError("no probe")}
        self.assertFalse(self.detect(responses, vulkan_loader_path=Path(self.folder.name) / "absent.dll"))
        self.assertFalse(self.detect(responses, windows=False))
        self.assertEqual([], self.calls)

    def test_missing_probe_tools_read_as_unavailable(self):
        absent = Path(self.folder.name) / "absent.exe"
        self.assertFalse(
            self.detect(
                {"powershell": AssertionError("no probe"), "wmic": AssertionError("no probe")},
                powershell_path=absent,
                wmic_path=absent,
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
