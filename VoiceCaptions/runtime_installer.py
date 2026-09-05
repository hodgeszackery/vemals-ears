"""Verified, opt-in local runtime installer for Voice Captions.

Nothing in this module installs on import or during detection. Downloads and
the npm dependency preparation happen only when ``install_voice_dependencies``
is called by the setup window after a user click.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Protocol

RUNTIME_TAG = "b4938"
CUDA_RUNTIME_URL = (
    "https://github.com/ggml-org/whisper.cpp/releases/download/"
    "b4938/whisper-cublas-12.4.0-bin-x64.zip"
)
CUDA_RUNTIME_SHA256 = (
    "c1b17166e1e31a91cc8e9c1f910d3785e3ce757bb2958bf9dce13fdb4880005f"
)
CPU_RUNTIME_URL = (
    "https://github.com/ggml-org/whisper.cpp/releases/download/"
    "b4938/whisper-bin-x64.zip"
)
CPU_RUNTIME_SHA256 = (
    "c2a4b60edb11f7e11a9191ffb50929535527d4d91c9903dbe3e554583bbbc63d"
)
# Kept as aliases for callers that displayed the former single runtime asset.
RUNTIME_URL = CUDA_RUNTIME_URL
RUNTIME_SHA256 = CUDA_RUNTIME_SHA256
MODEL_URL = (
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/"
    "5359861c739e955e79d9a303bcbc70fb988958b1/"
    "ggml-large-v3-turbo-q5_0.bin"
)
MODEL_SHA256 = "394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2"
VAD_MODEL_URL = (
    "https://huggingface.co/ggml-org/whisper-vad/resolve/"
    "9ffd54a1e1ee413ddf265af9913beaf518d1639b/"
    "ggml-silero-v6.2.0.bin"
)
VAD_MODEL_SHA256 = (
    "2aa269b785eeb53a82983a20501ddf7c1d9c48e33ab63a41391ac6c9f7fb6987"
)

SERVER_ENV = "VOICE_CAPTIONS_WHISPER_SERVER"
FALLBACK_SERVER_ENV = "VOICE_CAPTIONS_WHISPER_FALLBACK_SERVER"
MODEL_ENV = "VOICE_CAPTIONS_WHISPER_MODEL"
DLL_ENV = "VOICE_CAPTIONS_WHISPER_DLL"
VAD_MODEL_ENV = "VOICE_CAPTIONS_WHISPER_VAD_MODEL"
MODEL_FILENAME = "ggml-large-v3-turbo-q5_0.bin"
VAD_MODEL_FILENAME = "ggml-silero-v6.2.0.bin"
ACTIVE_MANIFEST = "active.json"
MANIFEST_SCHEMA = 2
DOWNLOAD_CHUNK_BYTES = 256 * 1024
HTTP_READ_TIMEOUT_SECONDS = 30.0
MAX_ARCHIVE_ENTRIES = 512
MAX_ARCHIVE_FILE_BYTES = 550 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 1_280 * 1024 * 1024
NPM_TIMEOUT_SECONDS = 300.0
MIN_NODE_VERSION = (22, 12, 0)
MIN_CUDA_12_4_WINDOWS_DRIVER = (551, 61)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class RuntimeInstallError(RuntimeError):
    """A user-safe runtime installation failure."""


class DownloadError(RuntimeInstallError):
    pass


class IntegrityError(RuntimeInstallError):
    pass


class UnsafeArchiveError(RuntimeInstallError):
    pass


@dataclass(frozen=True, slots=True)
class DownloadAsset:
    name: str
    url: str
    sha256: str
    minimum_bytes: int
    maximum_bytes: int
    maximum_seconds: float


@dataclass(frozen=True, slots=True)
class RuntimeAssets:
    cpu_runtime: DownloadAsset
    cuda_runtime: DownloadAsset
    model: DownloadAsset
    vad_model: DownloadAsset


OFFICIAL_ASSETS = RuntimeAssets(
    cpu_runtime=DownloadAsset(
        name="compatibility speech engine",
        url=CPU_RUNTIME_URL,
        sha256=CPU_RUNTIME_SHA256,
        minimum_bytes=7 * 1024 * 1024,
        maximum_bytes=10 * 1024 * 1024,
        maximum_seconds=180.0,
    ),
    cuda_runtime=DownloadAsset(
        name="NVIDIA speech engine",
        url=CUDA_RUNTIME_URL,
        sha256=CUDA_RUNTIME_SHA256,
        minimum_bytes=630 * 1024 * 1024,
        maximum_bytes=650 * 1024 * 1024,
        maximum_seconds=1_800.0,
    ),
    model=DownloadAsset(
        name="high-accuracy speech model",
        url=MODEL_URL,
        sha256=MODEL_SHA256,
        minimum_bytes=540 * 1024 * 1024,
        maximum_bytes=560 * 1024 * 1024,
        maximum_seconds=1_800.0,
    ),
    vad_model=DownloadAsset(
        name="speech and noise detector",
        url=VAD_MODEL_URL,
        sha256=VAD_MODEL_SHA256,
        minimum_bytes=800 * 1024,
        maximum_bytes=1 * 1024 * 1024,
        maximum_seconds=180.0,
    ),
)


@dataclass(frozen=True, slots=True)
class InstallProgress:
    phase: str
    message: str
    fraction: float


@dataclass(frozen=True, slots=True)
class InstalledRuntime:
    root: Path
    server_path: Path
    model_path: Path
    fallback_server_path: Path | None = None
    library_path: Path | None = None
    vad_model_path: Path | None = None
    acceleration: str = "cpu"
    version: str = RUNTIME_TAG

    def environment(self) -> dict[str, str]:
        environment = {
            SERVER_ENV: str(self.server_path),
            MODEL_ENV: str(self.model_path),
            # The backend keys decode quality off this: beam search
            # is worth its latency only where the encoder is fast.
            "VOICE_CAPTIONS_WHISPER_ACCELERATION": self.acceleration,
        }
        if self.fallback_server_path is not None:
            environment[FALLBACK_SERVER_ENV] = str(self.fallback_server_path)
        if self.library_path is not None:
            environment[DLL_ENV] = str(self.library_path)
        if self.vad_model_path is not None:
            environment[VAD_MODEL_ENV] = str(self.vad_model_path)
        return environment


@dataclass(frozen=True, slots=True)
class AdapterPreparation:
    ready: bool
    detail: str
    node_version: tuple[int, int, int] | None = None


@dataclass(frozen=True, slots=True)
class InstallOutcome:
    runtime: InstalledRuntime
    adapter: AdapterPreparation


ProgressCallback = Callable[[InstallProgress], None]
ByteProgressCallback = Callable[[int, int | None], None]


class AssetDownloader(Protocol):
    def __call__(
        self,
        asset: DownloadAsset,
        destination: Path,
        progress: ByteProgressCallback | None,
    ) -> None: ...


class ProcessRunner(Protocol):
    def __call__(self, command: list[str], **kwargs): ...


def default_runtime_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return base / "WoWVoiceCaptions" / "runtime"


def cuda_12_4_available(
    *,
    runner: ProcessRunner | None = None,
    nvidia_smi_path: Path | None = None,
) -> bool:
    """Detect a CUDA 12.4-capable Windows driver without changing the system."""
    runner = runner or subprocess.run
    if nvidia_smi_path is None:
        located = shutil.which("nvidia-smi.exe") or shutil.which("nvidia-smi")
        if located:
            nvidia_smi_path = Path(located)
        else:
            system_root = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
            candidate = system_root / "System32" / "nvidia-smi.exe"
            nvidia_smi_path = candidate if candidate.is_file() else None
    if nvidia_smi_path is None or not Path(nvidia_smi_path).is_file():
        return False
    try:
        result = runner(
            [
                str(nvidia_smi_path),
                "--query-gpu=driver_version",
                "--format=csv,noheader",
            ],
            cwd=str(Path(nvidia_smi_path).parent),
            env=_safe_process_environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
            shell=False,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    output = result.stdout if isinstance(result.stdout, str) else ""
    if result.returncode != 0 or len(output) > 4_096:
        return False
    for line in output.splitlines():
        match = re.fullmatch(r"\s*(\d+)\.(\d+)(?:\.\d+)?\s*", line)
        if match and tuple(int(part) for part in match.groups()) >= (
            MIN_CUDA_12_4_WINDOWS_DRIVER
        ):
            return True
    return False


def _emit(
    callback: ProgressCallback | None,
    phase: str,
    message: str,
    fraction: float,
) -> None:
    if callback is not None:
        callback(
            InstallProgress(
                phase=phase,
                message=message,
                fraction=max(0.0, min(float(fraction), 1.0)),
            )
        )


def _content_length(response) -> int | None:
    try:
        raw = response.headers.get("Content-Length")
    except (AttributeError, TypeError):
        return None
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise DownloadError("The download returned an invalid size.") from None
    return value if value >= 0 else None


def download_asset(
    asset: DownloadAsset,
    destination: Path,
    progress: ByteProgressCallback | None = None,
    *,
    opener=urllib.request.urlopen,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Stream one fixed asset with byte and wall-clock bounds."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        asset.url,
        headers={"User-Agent": "WoWVoiceCaptions-runtime-installer/1"},
        method="GET",
    )
    started = clock()
    written = 0
    try:
        with opener(
            request,
            timeout=min(HTTP_READ_TIMEOUT_SECONDS, asset.maximum_seconds),
        ) as response:
            status = getattr(response, "status", None)
            if status is not None and status != 200:
                raise DownloadError("The official download could not be retrieved.")
            expected = _content_length(response)
            if expected is not None and expected > asset.maximum_bytes:
                raise DownloadError("The official download is larger than expected.")
            with destination.open("xb") as output:
                while True:
                    if clock() - started > asset.maximum_seconds:
                        raise DownloadError("The official download took too long.")
                    chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > asset.maximum_bytes:
                        raise DownloadError("The official download is larger than expected.")
                    output.write(chunk)
                    if progress is not None:
                        progress(written, expected)
            if expected is not None and written != expected:
                raise DownloadError("The official download ended early.")
    except RuntimeInstallError:
        destination.unlink(missing_ok=True)
        raise
    except (OSError, TimeoutError) as error:
        destination.unlink(missing_ok=True)
        raise DownloadError("The official download could not be completed.") from error


def _sha256(path: Path, maximum_bytes: int | None = None) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(DOWNLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if maximum_bytes is not None and total > maximum_bytes:
                    raise IntegrityError("A downloaded file is larger than expected.")
                digest.update(chunk)
    except OSError as error:
        raise IntegrityError("A downloaded file could not be verified.") from error
    return digest.hexdigest(), total


def _verify_asset(path: Path, asset: DownloadAsset) -> None:
    digest, size = _sha256(path, asset.maximum_bytes)
    if size < asset.minimum_bytes or size > asset.maximum_bytes:
        raise IntegrityError("A downloaded file has an unexpected size.")
    if digest.lower() != asset.sha256.lower():
        raise IntegrityError("A downloaded file failed its security check.")


def _safe_member_path(name: str) -> PurePosixPath:
    if not name or "\x00" in name or len(name) > 4_096:
        raise UnsafeArchiveError("The speech engine archive contains an unsafe path.")
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise UnsafeArchiveError("The speech engine archive contains an unsafe path.")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(
        part in {"", ".", ".."} or ":" in part for part in path.parts
    ):
        raise UnsafeArchiveError("The speech engine archive contains an unsafe path.")
    return path


def _is_symlink_or_special(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(mode)
    if stat.S_ISLNK(mode):
        return True
    return file_type not in {0, stat.S_IFREG, stat.S_IFDIR}


def safe_extract_zip(
    archive_path: Path,
    destination: Path,
    *,
    maximum_entries: int = MAX_ARCHIVE_ENTRIES,
    maximum_file_bytes: int = MAX_ARCHIVE_FILE_BYTES,
    maximum_total_bytes: int = MAX_ARCHIVE_TOTAL_BYTES,
) -> None:
    """Extract regular files only, never trusting ZIP member paths."""
    destination.mkdir(parents=True, exist_ok=True)
    destination_resolved = destination.resolve()
    total_declared = 0
    seen: set[str] = set()
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = archive.infolist()
            if len(members) > maximum_entries:
                raise UnsafeArchiveError("The speech engine archive contains too many files.")
            for info in members:
                member = _safe_member_path(info.filename)
                collision_key = member.as_posix().rstrip("/").casefold()
                if collision_key in seen:
                    raise UnsafeArchiveError("The speech engine archive contains duplicate paths.")
                seen.add(collision_key)
                if info.flag_bits & 0x1 or _is_symlink_or_special(info):
                    raise UnsafeArchiveError("The speech engine archive contains an unsafe file.")
                if info.file_size < 0 or info.file_size > maximum_file_bytes:
                    raise UnsafeArchiveError("The speech engine archive contains an oversized file.")
                total_declared += info.file_size
                if total_declared > maximum_total_bytes:
                    raise UnsafeArchiveError("The speech engine archive is larger than expected.")

                target = destination.joinpath(*member.parts)
                try:
                    target.resolve().relative_to(destination_resolved)
                except (OSError, ValueError):
                    raise UnsafeArchiveError(
                        "The speech engine archive contains an unsafe path."
                    ) from None
                if info.is_dir() or info.filename.endswith(("/", "\\")):
                    target.mkdir(parents=True, exist_ok=True)
                    continue

                target.parent.mkdir(parents=True, exist_ok=True)
                copied = 0
                with archive.open(info, "r") as source, target.open("xb") as output:
                    while True:
                        chunk = source.read(DOWNLOAD_CHUNK_BYTES)
                        if not chunk:
                            break
                        copied += len(chunk)
                        if copied > info.file_size or copied > maximum_file_bytes:
                            raise UnsafeArchiveError(
                                "The speech engine archive expanded beyond its declared size."
                            )
                        output.write(chunk)
                if copied != info.file_size:
                    raise UnsafeArchiveError("The speech engine archive contains a truncated file.")
    except zipfile.BadZipFile as error:
        raise UnsafeArchiveError("The speech engine download is not a valid archive.") from error


def _relative_inside(root: Path, value: object) -> Path | None:
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        return None
    candidate = root.joinpath(*relative.parts)
    try:
        candidate.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return candidate


def _read_manifest(root: Path) -> Mapping[str, object] | None:
    path = root / ACTIVE_MANIFEST
    try:
        if path.stat().st_size > 16_384:
            return None
        payload = json.loads(path.read_text(encoding="ascii"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def detect_installed_runtime(
    root: Path | None = None,
    *,
    assets: RuntimeAssets = OFFICIAL_ASSETS,
    verify_hashes: bool = True,
) -> InstalledRuntime | None:
    """Read and validate the active local runtime without changing anything."""
    root = Path(root) if root is not None else default_runtime_root()
    manifest = _read_manifest(root)
    if not manifest:
        return None
    return _runtime_from_manifest(
        root,
        manifest,
        assets=assets,
        verify_hashes=verify_hashes,
    )


def _runtime_from_manifest(
    root: Path,
    manifest: Mapping[str, object],
    *,
    assets: RuntimeAssets,
    verify_hashes: bool,
) -> InstalledRuntime | None:
    """Validate one candidate manifest without changing the active pointer."""
    if (
        manifest.get("schema") != MANIFEST_SCHEMA
        or manifest.get("version") != RUNTIME_TAG
        or manifest.get("cpu_runtime_archive_sha256")
        != assets.cpu_runtime.sha256
        or manifest.get("model_sha256") != assets.model.sha256
        or manifest.get("vad_model_sha256") != assets.vad_model.sha256
    ):
        return None
    acceleration = manifest.get("acceleration")
    if acceleration not in {"cpu", "cuda"}:
        return None
    expected_cuda_digest = (
        assets.cuda_runtime.sha256 if acceleration == "cuda" else None
    )
    if manifest.get("cuda_runtime_archive_sha256") != expected_cuda_digest:
        return None
    server = _relative_inside(root, manifest.get("server"))
    fallback_server = _relative_inside(root, manifest.get("fallback_server"))
    library = _relative_inside(root, manifest.get("library"))
    model = _relative_inside(root, manifest.get("model"))
    vad_model = _relative_inside(root, manifest.get("vad_model"))
    server_digest = manifest.get("server_sha256")
    fallback_server_digest = manifest.get("fallback_server_sha256")
    library_digest = manifest.get("library_sha256")
    dependency_digests = manifest.get("vad_dependency_sha256")
    if (
        server is None
        or fallback_server is None
        or library is None
        or model is None
        or vad_model is None
        or not isinstance(server_digest, str)
        or not isinstance(fallback_server_digest, str)
        or not isinstance(library_digest, str)
        or not isinstance(dependency_digests, dict)
        or len(dependency_digests) < 3
        or len(dependency_digests) > 32
    ):
        return None

    dependency_files: list[tuple[Path, str, str]] = []
    dependency_names: set[str] = set()
    dependency_paths: set[str] = set()
    for relative_value, digest_value in dependency_digests.items():
        dependency = _relative_inside(root, relative_value)
        if (
            dependency is None
            or not isinstance(digest_value, str)
            or re.fullmatch(r"[0-9a-fA-F]{64}", digest_value) is None
        ):
            return None
        try:
            if dependency.parent.resolve() != library.parent.resolve():
                return None
        except OSError:
            return None
        path_key = str(dependency).casefold()
        if path_key in dependency_paths:
            return None
        dependency_paths.add(path_key)
        dependency_names.add(dependency.name.casefold())
        dependency_files.append((dependency, digest_value.casefold(), path_key))
    if (
        "ggml.dll" not in dependency_names
        or "ggml-base.dll" not in dependency_names
        or not any(
            name.startswith("ggml-cpu") and name.endswith(".dll")
            for name in dependency_names
        )
    ):
        return None
    # whisper.cpp loads every ggml backend beside the library and then runs on
    # the highest scoring one, so a ggml DLL the manifest never hashed is code
    # that would execute without ever having been verified.
    #
    # Scoped to the library's own directory deliberately. That is the only
    # directory this manifest can speak about: the loop above rejects any
    # dependency whose parent is not library.parent, so "unlisted" is a claim
    # about verification only here. Sweeping the server's directory as well
    # rejected every CUDA install outright, because the CUDA server ships a
    # ggml-cuda.dll that no dependency entry is permitted to name - the check
    # asked a question the manifest's own shape made unanswerable.
    #
    # Those files are not unverified. They arrive inside an archive whose
    # SHA-256 is pinned in CUDA_RUNTIME_SHA256, checked on download, and
    # re-asserted against this manifest above. That is install-time provenance
    # rather than load-time re-verification, which is genuinely weaker; closing
    # the difference needs the manifest to carry per-file digests for every
    # directory it loads from, not a sweep that can only ever fail.
    try:
        for entry in library.parent.iterdir():
            name = entry.name.casefold()
            if (
                name.startswith("ggml")
                and name.endswith(".dll")
                and name not in dependency_names
                and entry.is_file()
            ):
                return None
    except OSError:
        return None
    try:
        if not all(
            path.is_file()
            for path in (
                server,
                fallback_server,
                library,
                model,
                vad_model,
                *(path for path, _digest, _path_key in dependency_files),
            )
        ):
            return None
    except OSError:
        return None
    if verify_hashes:
        try:
            actual_server, _ = _sha256(server, MAX_ARCHIVE_FILE_BYTES)
            actual_fallback_server, _ = _sha256(
                fallback_server, MAX_ARCHIVE_FILE_BYTES
            )
            actual_library, _ = _sha256(library, MAX_ARCHIVE_FILE_BYTES)
            actual_model, _ = _sha256(model, assets.model.maximum_bytes)
            actual_vad_model, _ = _sha256(
                vad_model, assets.vad_model.maximum_bytes
            )
            actual_dependencies = {
                path_key: _sha256(path, MAX_ARCHIVE_FILE_BYTES)[0]
                for path, _digest, path_key in dependency_files
            }
        except IntegrityError:
            return None
        if (
            actual_server != server_digest
            or actual_fallback_server != fallback_server_digest
            or actual_library != library_digest
            or actual_model != assets.model.sha256
            or actual_vad_model != assets.vad_model.sha256
            or any(
                actual_dependencies.get(path_key) != digest
                for _path, digest, path_key in dependency_files
            )
        ):
            return None
    return InstalledRuntime(
        root=root.resolve(),
        server_path=server.resolve(),
        model_path=model.resolve(),
        fallback_server_path=fallback_server.resolve(),
        library_path=library.resolve(),
        vad_model_path=vad_model.resolve(),
        acceleration=acceleration,
    )


def stt_environment(
    root: Path | None = None,
    *,
    assets: RuntimeAssets = OFFICIAL_ASSETS,
) -> dict[str, str]:
    resolved_root = Path(root) if root is not None else default_runtime_root()
    runtime = detect_installed_runtime(resolved_root, assets=assets)
    return runtime.environment() if runtime is not None else {}


def _write_active_manifest(root: Path, payload: Mapping[str, object]) -> None:
    temporary = root / (".active-" + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(
            json.dumps(dict(payload), ensure_ascii=True, separators=(",", ":")) + "\n",
            encoding="ascii",
        )
        os.replace(temporary, root / ACTIVE_MANIFEST)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise RuntimeInstallError("The local speech engine could not be activated.") from error


def _remove_install_candidate(
    path: Path | None,
    installations: Path,
    *,
    expected_prefix: str,
) -> None:
    if path is None:
        return
    try:
        resolved_path = path.resolve()
        resolved_parent = installations.resolve()
        safe = (
            resolved_path.parent == resolved_parent
            and resolved_path.name.startswith(expected_prefix)
        )
    except OSError:
        safe = False
    if safe and path.exists():
        shutil.rmtree(path, ignore_errors=True)


def _remove_staging(path: Path, installations: Path) -> None:
    _remove_install_candidate(
        path,
        installations,
        expected_prefix=".install-",
    )


def _prune_superseded_generations(keep: Path, installations: Path) -> None:
    """Delete published generations the newly written manifest no longer names.

    A generation is only reachable through the active manifest, so once the new
    manifest is durable every other one is an orphaned copy of a runtime that is
    close to 600 MB. Pruning is best effort because a generation still held open
    by a running engine must not turn a finished install into a failure.
    """
    try:
        kept = keep.resolve()
        entries = list(installations.iterdir())
    except OSError:
        return
    for entry in entries:
        if not entry.name.startswith(RUNTIME_TAG + "-"):
            continue
        try:
            if entry.resolve() == kept or not entry.is_dir():
                continue
        except OSError:
            continue
        shutil.rmtree(entry, ignore_errors=True)


def _download_fraction(
    progress: ProgressCallback | None,
    *,
    phase: str,
    message: str,
    start: float,
    span: float,
) -> ByteProgressCallback:
    def report(downloaded: int, expected: int | None) -> None:
        denominator = expected if expected and expected > 0 else None
        ratio = downloaded / denominator if denominator else 0.0
        _emit(progress, phase, message, start + span * min(ratio, 0.99))

    return report


def install_runtime(
    root: Path | None = None,
    *,
    downloader: AssetDownloader | None = None,
    progress: ProgressCallback | None = None,
    assets: RuntimeAssets = OFFICIAL_ASSETS,
    cuda_detector: Callable[[], bool] | None = None,
    retry_cuda_upgrade: bool = False,
) -> InstalledRuntime:
    """Download, verify, stage, and atomically activate whisper.cpp."""
    root = Path(root) if root is not None else default_runtime_root()
    installed = detect_installed_runtime(root, assets=assets)
    downloader = downloader or download_asset
    use_cuda: bool | None = None
    if installed is not None:
        should_probe_upgrade = (
            retry_cuda_upgrade and installed.acceleration == "cpu"
        )
        if should_probe_upgrade:
            try:
                use_cuda = bool((cuda_detector or cuda_12_4_available)())
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
                use_cuda = False
        if not should_probe_upgrade or not use_cuda:
            _emit(progress, "ready", "Local speech engine is already ready.", 1.0)
            return installed
    if use_cuda is None:
        try:
            use_cuda = bool((cuda_detector or cuda_12_4_available)())
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            use_cuda = False
    installations = root / "installs"
    final_folder: Path | None = None
    activated = False
    try:
        installations.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".install-", dir=installations))
    except OSError as error:
        raise RuntimeInstallError("Windows could not prepare the local speech folder.") from error

    cpu_runtime_download = staging / "speech-engine-cpu.zip"
    cuda_runtime_download = staging / "speech-engine-cuda.zip"
    model_download = staging / "speech-model.download"
    vad_model_download = staging / "speech-detector.download"
    try:
        _emit(
            progress,
            "runtime-download",
            "Downloading the compatibility speech engine...",
            0.01,
        )
        downloader(
            assets.cpu_runtime,
            cpu_runtime_download,
            _download_fraction(
                progress,
                phase="runtime-download",
                message="Downloading the compatibility speech engine...",
                start=0.01,
                span=0.03,
            ),
        )
        _emit(
            progress,
            "runtime-verify",
            "Checking the compatibility speech engine...",
            0.04,
        )
        _verify_asset(cpu_runtime_download, assets.cpu_runtime)

        cuda_installed = False
        if use_cuda:
            try:
                _emit(
                    progress,
                    "cuda-download",
                    "Downloading NVIDIA speech acceleration (about 640 MB)...",
                    0.05,
                )
                downloader(
                    assets.cuda_runtime,
                    cuda_runtime_download,
                    _download_fraction(
                        progress,
                        phase="cuda-download",
                        message=(
                            "Downloading NVIDIA speech acceleration "
                            "(about 640 MB)..."
                        ),
                        start=0.05,
                        span=0.48,
                    ),
                )
                _emit(
                    progress,
                    "cuda-verify",
                    "Checking NVIDIA speech acceleration...",
                    0.53,
                )
                _verify_asset(cuda_runtime_download, assets.cuda_runtime)
                cuda_installed = True
            except RuntimeInstallError:
                cuda_runtime_download.unlink(missing_ok=True)
                _emit(
                    progress,
                    "cuda-fallback",
                    "NVIDIA acceleration was unavailable; using compatibility mode.",
                    0.54,
                )

        _emit(
            progress,
            "model-download",
            "Downloading the high-accuracy speech model (about 548 MB)...",
            0.55,
        )
        downloader(
            assets.model,
            model_download,
            _download_fraction(
                progress,
                phase="model-download",
                message=(
                    "Downloading the high-accuracy speech model "
                    "(about 548 MB)..."
                ),
                start=0.55,
                span=0.31,
            ),
        )
        _emit(progress, "model-verify", "Checking the speech model...", 0.87)
        _verify_asset(model_download, assets.model)

        _emit(
            progress,
            "vad-download",
            "Downloading the speech and noise detector...",
            0.88,
        )
        downloader(
            assets.vad_model,
            vad_model_download,
            _download_fraction(
                progress,
                phase="vad-download",
                message="Downloading the speech and noise detector...",
                start=0.88,
                span=0.01,
            ),
        )
        _emit(progress, "vad-verify", "Checking the speech detector...", 0.90)
        _verify_asset(vad_model_download, assets.vad_model)

        files = staging / "files"
        cpu_files = files / "cpu"
        _emit(progress, "extract", "Preparing the local speech engine...", 0.91)
        safe_extract_zip(cpu_runtime_download, cpu_files)
        cpu_servers = sorted(
            path
            for path in cpu_files.rglob("*")
            if path.is_file() and path.name.casefold() == "whisper-server.exe"
        )
        cpu_libraries = sorted(
            path
            for path in cpu_files.rglob("*")
            if path.is_file() and path.name.casefold() == "whisper.dll"
        )
        if len(cpu_servers) != 1 or len(cpu_libraries) != 1:
            raise RuntimeInstallError(
                "The verified speech engine did not contain the expected files."
            )
        cpu_server = cpu_servers[0]
        library = cpu_libraries[0]
        dependency_files = [
            library.with_name("ggml.dll"),
            library.with_name("ggml-base.dll"),
        ]
        dependency_files.extend(
            sorted(
                path
                for path in library.parent.glob("ggml-cpu*.dll")
                if path.is_file()
            )
        )
        if (
            not all(path.is_file() for path in dependency_files[:2])
            or len(dependency_files) < 3
        ):
            raise RuntimeInstallError(
                "The verified speech engine did not contain its required libraries."
            )
        server = cpu_server
        acceleration = "cpu"
        if cuda_installed:
            cuda_files = files / "cuda"
            try:
                safe_extract_zip(cuda_runtime_download, cuda_files)
                cuda_servers = sorted(
                    path
                    for path in cuda_files.rglob("*")
                    if path.is_file()
                    and path.name.casefold() == "whisper-server.exe"
                )
                if len(cuda_servers) != 1:
                    raise RuntimeInstallError(
                        "The NVIDIA speech engine did not contain the expected server."
                    )
                server = cuda_servers[0]
                acceleration = "cuda"
            except RuntimeInstallError:
                shutil.rmtree(cuda_files, ignore_errors=True)
                cuda_runtime_download.unlink(missing_ok=True)
                cuda_installed = False
                _emit(
                    progress,
                    "cuda-fallback",
                    "NVIDIA acceleration was unavailable; using compatibility mode.",
                    0.93,
                )

        server_digest, _ = _sha256(server, MAX_ARCHIVE_FILE_BYTES)
        fallback_server_digest, _ = _sha256(
            cpu_server, MAX_ARCHIVE_FILE_BYTES
        )
        library_digest, _ = _sha256(library, MAX_ARCHIVE_FILE_BYTES)
        dependency_digests = {
            path: _sha256(path, MAX_ARCHIVE_FILE_BYTES)[0]
            for path in dependency_files
        }

        model_folder = staging / "models"
        model_folder.mkdir(parents=True, exist_ok=True)
        model = model_folder / MODEL_FILENAME
        vad_model = model_folder / VAD_MODEL_FILENAME
        os.replace(model_download, model)
        os.replace(vad_model_download, vad_model)
        cpu_runtime_download.unlink(missing_ok=True)
        cuda_runtime_download.unlink(missing_ok=True)

        generation = RUNTIME_TAG + "-" + uuid.uuid4().hex
        final_folder = installations / generation
        os.replace(staging, final_folder)
        server_final = final_folder / server.relative_to(staging)
        fallback_server_final = final_folder / cpu_server.relative_to(staging)
        library_final = final_folder / library.relative_to(staging)
        dependencies_final = {
            final_folder / path.relative_to(staging): digest
            for path, digest in dependency_digests.items()
        }
        model_final = final_folder / model.relative_to(staging)
        vad_model_final = final_folder / vad_model.relative_to(staging)
        payload = {
            "schema": MANIFEST_SCHEMA,
            "version": RUNTIME_TAG,
            "acceleration": acceleration,
            "cpu_runtime_archive_sha256": assets.cpu_runtime.sha256,
            "cuda_runtime_archive_sha256": (
                assets.cuda_runtime.sha256 if cuda_installed else None
            ),
            "model_sha256": assets.model.sha256,
            "vad_model_sha256": assets.vad_model.sha256,
            "server_sha256": server_digest,
            "fallback_server_sha256": fallback_server_digest,
            "library_sha256": library_digest,
            "vad_dependency_sha256": {
                path.relative_to(root).as_posix(): digest
                for path, digest in dependencies_final.items()
            },
            "server": server_final.relative_to(root).as_posix(),
            "fallback_server": fallback_server_final.relative_to(root).as_posix(),
            "library": library_final.relative_to(root).as_posix(),
            "model": model_final.relative_to(root).as_posix(),
            "vad_model": vad_model_final.relative_to(root).as_posix(),
        }
        _emit(progress, "activate", "Finishing local speech setup...", 0.96)
        installed = _runtime_from_manifest(
            root,
            payload,
            assets=assets,
            verify_hashes=True,
        )
        if installed is None:
            raise RuntimeInstallError(
                "The local speech engine did not pass its final check."
            )
        _write_active_manifest(root, payload)
        activated = True
        _prune_superseded_generations(final_folder, installations)
        ready_message = (
            "Local speech engine is ready with NVIDIA acceleration."
            if installed.acceleration == "cuda"
            else "Local speech engine is ready in compatibility mode."
        )
        _emit(progress, "ready", ready_message, 1.0)
        return installed
    except RuntimeInstallError:
        _remove_staging(staging, installations)
        if not activated:
            _remove_install_candidate(
                final_folder,
                installations,
                expected_prefix=RUNTIME_TAG + "-",
            )
        raise
    except OSError as error:
        _remove_staging(staging, installations)
        if not activated:
            _remove_install_candidate(
                final_folder,
                installations,
                expected_prefix=RUNTIME_TAG + "-",
            )
        raise RuntimeInstallError("Windows could not install the local speech engine.") from error
    except BaseException:
        _remove_staging(staging, installations)
        if not activated:
            _remove_install_candidate(
                final_folder,
                installations,
                expected_prefix=RUNTIME_TAG + "-",
            )
        raise


def _safe_process_environment() -> dict[str, str]:
    allowed = {
        "APPDATA",
        "COMMONPROGRAMFILES",
        "COMMONPROGRAMFILES(X86)",
        "LOCALAPPDATA",
        "NUMBER_OF_PROCESSORS",
        "PATH",
        "PROCESSOR_ARCHITECTURE",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "WINDIR",
    }
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


def _node_version(
    node_path: Path,
    runner: ProcessRunner,
) -> tuple[int, int, int] | None:
    try:
        result = runner(
            [str(node_path), "--version"],
            cwd=str(node_path.parent),
            env=_safe_process_environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
            shell=False,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = result.stdout if isinstance(result.stdout, str) else ""
    if result.returncode != 0 or len(output) > 100:
        return None
    match = re.fullmatch(r"\s*v?(\d+)\.(\d+)\.(\d+)\s*", output)
    return tuple(int(part) for part in match.groups()) if match else None


def _find_npm_cli(node_path: Path, explicit: Path | None) -> Path | None:
    candidates = []
    if explicit is not None:
        candidates.append(Path(explicit))
    candidates.extend(
        (
            node_path.parent / "node_modules" / "npm" / "bin" / "npm-cli.js",
            node_path.parent.parent
            / "lib"
            / "node_modules"
            / "npm"
            / "bin"
            / "npm-cli.js",
        )
    )
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def prepare_discord_adapter(
    adapter_dir: Path | None = None,
    *,
    runner: ProcessRunner | None = None,
    node_path: Path | None = None,
    npm_cli_path: Path | None = None,
    timeout_seconds: float = NPM_TIMEOUT_SECONDS,
) -> AdapterPreparation:
    """Run locked npm installation only with a supported local Node runtime."""
    adapter_dir = (
        Path(adapter_dir)
        if adapter_dir is not None
        else Path(__file__).resolve().parent / "discord_adapter"
    )
    runner = runner or subprocess.run
    if node_path is None:
        located = shutil.which("node.exe") or shutil.which("node")
        node_path = Path(located) if located else None
    if node_path is None or not node_path.is_file():
        return AdapterPreparation(
            ready=False,
            detail="Install Node 22.12 or newer to finish the Discord voice helper.",
        )
    version = _node_version(node_path, runner)
    if version is None or version < MIN_NODE_VERSION:
        return AdapterPreparation(
            ready=False,
            detail="Install Node 22.12 or newer to finish the Discord voice helper.",
            node_version=version,
        )
    npm_cli = _find_npm_cli(node_path, npm_cli_path)
    if npm_cli is None:
        return AdapterPreparation(
            ready=False,
            detail="The Node installation is missing its package installer.",
            node_version=version,
        )
    if not (adapter_dir / "package.json").is_file() or not (
        adapter_dir / "package-lock.json"
    ).is_file():
        return AdapterPreparation(
            ready=False,
            detail="The Discord voice helper files are incomplete.",
            node_version=version,
        )

    command = [
        str(node_path),
        str(npm_cli),
        "ci",
        "--omit=dev",
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
    ]
    try:
        result = runner(
            command,
            cwd=str(adapter_dir),
            env=_safe_process_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=max(1.0, min(float(timeout_seconds), NPM_TIMEOUT_SECONDS)),
            check=False,
            shell=False,
            creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return AdapterPreparation(
            ready=False,
            detail="The Discord voice helper setup took too long. Try again.",
            node_version=version,
        )
    except (OSError, subprocess.SubprocessError):
        return AdapterPreparation(
            ready=False,
            detail="The Discord voice helper could not be prepared.",
            node_version=version,
        )
    if result.returncode != 0:
        return AdapterPreparation(
            ready=False,
            detail="The Discord voice helper could not be prepared.",
            node_version=version,
        )
    return AdapterPreparation(
        ready=True,
        detail="Discord voice helper is ready.",
        node_version=version,
    )


def install_voice_dependencies(
    root: Path | None = None,
    *,
    downloader: AssetDownloader | None = None,
    progress: ProgressCallback | None = None,
    assets: RuntimeAssets = OFFICIAL_ASSETS,
    adapter_dir: Path | None = None,
    runner: ProcessRunner | None = None,
    node_path: Path | None = None,
    npm_cli_path: Path | None = None,
    cuda_detector: Callable[[], bool] | None = None,
) -> InstallOutcome:
    """Explicit one-click action for verified STT and locked adapter deps."""
    def runtime_progress(update: InstallProgress) -> None:
        _emit(
            progress,
            update.phase,
            update.message,
            update.fraction * 0.96,
        )

    runtime = install_runtime(
        root,
        downloader=downloader,
        progress=runtime_progress if progress is not None else None,
        assets=assets,
        cuda_detector=cuda_detector,
        retry_cuda_upgrade=True,
    )
    _emit(progress, "adapter", "Preparing the Discord voice helper...", 0.98)
    adapter = prepare_discord_adapter(
        adapter_dir,
        runner=runner,
        node_path=node_path,
        npm_cli_path=npm_cli_path,
    )
    _emit(
        progress,
        "ready",
        "Local voice tools are ready." if adapter.ready else adapter.detail,
        1.0,
    )
    return InstallOutcome(runtime=runtime, adapter=adapter)
