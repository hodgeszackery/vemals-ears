"""Supervisor for the Voice Captions companion service.

The unified companion starts this file with --watch. It owns the overlay and,
later, the Discord and speech-to-text children. Child JSON never reaches the
companion log: that log is visible and intentionally carries status only.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from VoiceCaptions.coordinator import (  # type: ignore
        CoordinatorError,
        NodeAdapterCapability,
        detect_node_adapter,
        run_node_capture,
    )
    from VoiceCaptions.credentials import (  # type: ignore
        CredentialStore,
        CredentialStoreError,
        DiscordCredentials,
        WindowsCredentialStore,
    )
    from VoiceCaptions.events import (  # type: ignore
        CaptionEvent,
        CaptionProtocolError,
        ProtocolEvent,
        SessionEvent,
        parse_event_line,
    )
    from VoiceCaptions.member_overlay import (  # type: ignore
        NO_WINDOW,
        OVERLAY,
        ROOT,
        OverlaySink,
        child_python,
    )
    from VoiceCaptions.settings import (  # type: ignore
        default_settings_path,
        load_settings,
    )
    from VoiceCaptions.stt import (  # type: ignore
        BackendCapability,
        TranscriptionService,
        WhisperCppBackend,
        WhisperCppConfig,
        WhisperCppVadGate,
        detect_whisper_cpp,
    )
else:
    from .coordinator import (
        CoordinatorError,
        NodeAdapterCapability,
        detect_node_adapter,
        run_node_capture,
    )
    from .credentials import (
        CredentialStore,
        CredentialStoreError,
        DiscordCredentials,
        WindowsCredentialStore,
    )
    from .events import (
        CaptionEvent,
        CaptionProtocolError,
        ProtocolEvent,
        SessionEvent,
        parse_event_line,
    )
    from .member_overlay import (
        NO_WINDOW,
        OVERLAY,
        ROOT,
        OverlaySink,
        child_python,
    )
    from .settings import default_settings_path, load_settings
    from .stt import (
        BackendCapability,
        TranscriptionService,
        WhisperCppBackend,
        WhisperCppConfig,
        WhisperCppVadGate,
        detect_whisper_cpp,
    )

STT_CACHE_MAX_AGE_SECONDS = 30.0
_EXPLICIT_STT_ENVIRONMENT = (
    "VOICE_CAPTIONS_WHISPER_SERVER",
    "VOICE_CAPTIONS_WHISPER_FALLBACK_SERVER",
    "VOICE_CAPTIONS_WHISPER_MODEL",
    "VOICE_CAPTIONS_WHISPER_DLL",
    "VOICE_CAPTIONS_WHISPER_VAD_MODEL",
)


def status(text: str) -> None:
    """The only channel inherited by the visible companion log.

    Stamped with the local time. Shino, 2026-09-04: "it keeps logging in
    and out of channels and causing disruptions" - and the log could not
    answer how often, because every line was bare text. A whole evening
    of joins and drops read as one undated column. The stamp is a
    PREFIX, so the companion window's substring matching is unaffected.
    """
    print(time.strftime("%H:%M:%S") + "  " + text, flush=True)


# The roles focus mode lets through. STRICT by request: UNKNOWN is dropped
# too, so a speaker nobody mapped with /vem map goes silent while focus
# is on. That is the requested behaviour, not an accident - and it means a
# group that never maps hears nobody in focus, which the companion's toggle
# label states outright.
FOCUS_ROLES = frozenset({"TANK", "HEALER"})


def settings_focus_provider(path=None):
    """Read focus mode from the overlay settings file, cheaply, live.

    The companion toggles focus by rewriting the settings file, and this
    process must notice without a restart. One os.stat per caption is the
    whole cost; the file is re-read only when its mtime changes. Anything
    unreadable reads as focus off, because a broken settings file silently
    muting people would be the wrong failure.
    """
    target = path or default_settings_path()
    cache = {"stamp": None, "value": False}

    def focused() -> bool:
        try:
            stamp = os.stat(target).st_mtime_ns
        except OSError:
            cache["stamp"] = None
            cache["value"] = False
            return False
        if stamp != cache["stamp"]:
            cache["stamp"] = stamp
            cache["value"] = load_settings(target).focus_mode
        return cache["value"]

    return focused


class CaptionRouter:
    """Validate component JSON and forward it without logging its content."""

    PUBLIC_SESSION_STATUS = {
        "active": "Captions active",
        "paused": "Captions paused - waiting for consent",
        "ready": "Vemal's Ears host ready",
        "stopped": "Captions stopped",
        "error": "Error: Vemal's Ears source is unavailable",
    }

    def __init__(self, sink: OverlaySink, focus_provider=None) -> None:
        self.sink = sink
        self._focused = focus_provider or settings_focus_provider()

    def route_event(self, event: ProtocolEvent) -> str | None:
        # Focus drops only captions. Session state, clears, and everything
        # else still flow: focus narrows who is heard, never what the
        # pipeline knows about itself.
        if (
            isinstance(event, CaptionEvent)
            and event.role not in FOCUS_ROLES
            and self._focused()
        ):
            return None
        if not self.sink.send(event):
            return "Error: Vemal's Ears overlay is unavailable"
        if isinstance(event, SessionEvent):
            return self.PUBLIC_SESSION_STATUS[event.state]
        return None


    def route_line(self, line: str) -> str | None:
        try:
            event = parse_event_line(line)
        except CaptionProtocolError:
            return None
        return self.route_event(event)


@dataclass(frozen=True)
class RuntimeReadiness:
    ready: bool
    public_status: str
    node: NodeAdapterCapability | None = None
    stt_config: WhisperCppConfig | None = None
    credentials: DiscordCredentials | None = None


@dataclass(frozen=True)
class _SttConfigCache:
    manifest_key: tuple[str, int, int] | None
    asset_key: tuple[tuple[str, int, int], ...] | None
    config: WhisperCppConfig
    verified_at: float


_STT_CONFIG_CACHE: _SttConfigCache | None = None


def _file_key(path: Path) -> tuple[str, int, int] | None:
    try:
        stat = path.stat()
        resolved = path.resolve()
    except OSError:
        return None
    return (str(resolved), stat.st_size, stat.st_mtime_ns)


def _stt_asset_key(
    config: WhisperCppConfig,
) -> tuple[tuple[str, int, int], ...] | None:
    required = (config.server_path, config.model_path)
    if any(path is None for path in required):
        return None
    paths = [Path(path) for path in required if path is not None]
    paths.extend(
        Path(path)
        for path in (
            config.fallback_server_path,
            config.vad_library_path,
            config.vad_model_path,
        )
        if path is not None
    )
    keys = tuple(_file_key(path) for path in paths)
    if any(key is None for key in keys):
        return None
    return tuple(key for key in keys if key is not None)


def installed_stt_config() -> WhisperCppConfig:
    """Use the managed runtime when present, otherwise check explicit env."""
    global _STT_CONFIG_CACHE
    checked_at = time.monotonic()
    try:
        from VoiceCaptions.runtime_installer import (
            ACTIVE_MANIFEST,
            default_runtime_root,
            stt_environment,
        )

        manifest_key = _file_key(default_runtime_root() / ACTIVE_MANIFEST)
        cached = _STT_CONFIG_CACHE
        if (
            cached is not None
            and cached.manifest_key == manifest_key
            and checked_at - cached.verified_at < STT_CACHE_MAX_AGE_SECONDS
        ):
            if cached.asset_key is None:
                return cached.config
            current_assets = _stt_asset_key(cached.config)
            if current_assets == cached.asset_key:
                return cached.config
        environment = stt_environment()
    except (ImportError, OSError, RuntimeError, ValueError):
        environment = {}
        manifest_key = None
    if not environment:
        environment = {
            name: value.strip()
            for name in _EXPLICIT_STT_ENVIRONMENT
            if isinstance((value := os.environ.get(name)), str) and value.strip()
        }
    server = environment.get("VOICE_CAPTIONS_WHISPER_SERVER")
    fallback_server = environment.get("VOICE_CAPTIONS_WHISPER_FALLBACK_SERVER")
    model = environment.get("VOICE_CAPTIONS_WHISPER_MODEL")
    vad_library = environment.get("VOICE_CAPTIONS_WHISPER_DLL")
    vad_model = environment.get("VOICE_CAPTIONS_WHISPER_VAD_MODEL")
    config = WhisperCppConfig(
        # Absent or unknown reads as not accelerated, which decodes
        # greedy - the conservative direction for a machine we know
        # nothing about.
        accelerated=environment.get("VOICE_CAPTIONS_WHISPER_ACCELERATION")
        == "cuda",
        server_path=Path(server) if server else None,
        model_path=Path(model) if model else None,
        fallback_server_path=(Path(fallback_server) if fallback_server else None),
        vad_library_path=Path(vad_library) if vad_library else None,
        vad_model_path=Path(vad_model) if vad_model else None,
    )
    asset_key = _stt_asset_key(config)
    _STT_CONFIG_CACHE = _SttConfigCache(
        manifest_key, asset_key, config, checked_at
    )
    return config


def detect_speech_gate(config: WhisperCppConfig) -> BackendCapability:
    """Check the required pre-transcription VAD without loading native code."""
    return WhisperCppVadGate.from_whisper_config(config).capability


def check_runtime_readiness(
    *,
    node_detector: Callable[[], NodeAdapterCapability] = detect_node_adapter,
    stt_config_loader: Callable[[], WhisperCppConfig] = installed_stt_config,
    stt_detector: Callable[[WhisperCppConfig], BackendCapability] = detect_whisper_cpp,
    speech_gate_detector: Callable[
        [WhisperCppConfig], BackendCapability
    ] = detect_speech_gate,
    credential_store: CredentialStore | None = None,
    credential_store_factory: Callable[[], CredentialStore] = WindowsCredentialStore,
) -> RuntimeReadiness:
    """Check public prerequisites without starting capture or exposing values."""
    try:
        node = node_detector()
    except (OSError, RuntimeError, ValueError):
        return RuntimeReadiness(
            False, "Setup required: install the Discord voice runtime"
        )
    if not node.available:
        return RuntimeReadiness(
            False, "Setup required: install the Discord voice runtime"
        )
    try:
        store = credential_store or credential_store_factory()
        credentials = store.load()
    except (CredentialStoreError, OSError):
        return RuntimeReadiness(
            False,
            "Error: Vemal's Ears host setup is unavailable",
            node=node,
        )
    if credentials is None:
        return RuntimeReadiness(
            False,
            "Setup required: connect Discord",
            node=node,
        )
    try:
        stt_config = stt_config_loader()
        capability = stt_detector(stt_config)
    except (OSError, RuntimeError, ValueError):
        return RuntimeReadiness(
            False,
            "Setup required: update local speech tools",
            node=node,
            credentials=credentials,
        )
    if not capability.available:
        return RuntimeReadiness(
            False,
            "Setup required: update local speech tools",
            node=node,
            stt_config=stt_config,
            credentials=credentials,
        )
    try:
        gate_capability = speech_gate_detector(stt_config)
    except (OSError, RuntimeError, ValueError):
        gate_capability = BackendCapability(
            "speech gate", False, "local speech detector is unavailable"
        )
    if not gate_capability.available:
        return RuntimeReadiness(
            False,
            "Setup required: update local speech tools",
            node=node,
            stt_config=stt_config,
            credentials=credentials,
        )
    return RuntimeReadiness(
        True,
        "Vemal's Ears host ready",
        node=node,
        stt_config=stt_config,
        credentials=credentials,
    )


def _caption_forwarder(router: CaptionRouter) -> Callable[[ProtocolEvent], bool]:
    def forward(event: ProtocolEvent) -> bool:
        public_message = router.route_event(event)
        if public_message == "Error: Vemal's Ears overlay is unavailable":
            return False
        if public_message:
            status(public_message)
        return True

    forward.alive = router.sink.alive  # type: ignore[attr-defined]

    def segment_dropped(reason: str) -> None:
        # The coordinator throttles this to one line per reason per 30 s
        # and the reason is an enum word - never a member's audio or text.
        status("Captions skipped one segment (" + str(reason)[:64] + ")")

    forward.segment_dropped = segment_dropped  # type: ignore[attr-defined]

    # The bot leaving and rejoining the voice channel. Its own line, so
    # it never overwrites the session state a reconnect does not change,
    # and so an evening of churn is countable afterwards.
    VOICE_STATE_WORDS = {
        "voice.disconnected": "Vemal's Ears left the voice channel",
        "voice.reconnecting": "Vemal's Ears is rejoining the voice channel",
        "voice.reconnected": "Vemal's Ears rejoined the voice channel",
    }

    def voice_state(event_type: str) -> None:
        message = VOICE_STATE_WORDS.get(event_type)
        if message:
            status(message)

    forward.voice_state = voice_state  # type: ignore[attr-defined]
    # Hosted guild sinks also carry the private relay-to-Discord quick-chat
    # control channel. Keep it attached to the callable handed to the
    # coordinator without making the local overlay depend on GuildHub.
    if hasattr(router.sink, "next_quick_chat"):
        forward.next_quick_chat = router.sink.next_quick_chat  # type: ignore[attr-defined]
    if hasattr(router.sink, "complete_quick_chat"):
        forward.complete_quick_chat = router.sink.complete_quick_chat  # type: ignore[attr-defined]
    if hasattr(router.sink, "update_audience"):
        forward.update_audience = router.sink.update_audience  # type: ignore[attr-defined]
    if hasattr(router.sink, "link_viewer"):
        forward.link_viewer = router.sink.link_viewer  # type: ignore[attr-defined]
    if hasattr(router.sink, "enroll_viewer"):
        forward.enroll_viewer = router.sink.enroll_viewer  # type: ignore[attr-defined]
    return forward


# A rebuild of the capture stack is expensive - an overlay process,
# two speech model loads and a Node start - so a permanent fault
# (no microphone, no pairing, a missing runtime) must not pay that
# price every two seconds for the rest of the session.
RETRY_MAX_SECONDS = 60.0
# A capture session that stayed up this long counts as healthy, so
# the next failure starts the backoff over rather than inheriting
# the ceiling from an unrelated fault hours earlier.
RETRY_HEALTHY_SECONDS = 120.0


def run_watch(
    stop_event: threading.Event | None = None,
    *,
    readiness_check: Callable[[], RuntimeReadiness] = check_runtime_readiness,
    overlay_factory: Callable[[], OverlaySink] = OverlaySink.start,
    capture_runner=run_node_capture,
    retry_seconds: float = 2.0,
) -> int:
    stop_event = stop_event or threading.Event()
    last_public_status = ""
    backoff = retry_seconds
    session_started = time.monotonic()
    try:
        while not stop_event.is_set():
            readiness = readiness_check()
            if not readiness.ready:
                if readiness.public_status != last_public_status:
                    status(readiness.public_status)
                    last_public_status = readiness.public_status
                stop_event.wait(backoff)
                backoff = min(backoff * 2, RETRY_MAX_SECONDS)
                continue

            assert readiness.node is not None
            assert readiness.stt_config is not None
            assert readiness.credentials is not None
            try:
                sink = overlay_factory()
            except OSError:
                status("Error: Vemal's Ears overlay could not start")
                return 1

            router = CaptionRouter(sink)
            backend = WhisperCppBackend(readiness.stt_config)
            speech_gate = WhisperCppVadGate.from_whisper_config(
                readiness.stt_config
            )
            service = TranscriptionService(
                backend,
                speech_gate=speech_gate,
                max_concurrent_speakers=readiness.stt_config.worker_count,
            )
            if readiness.public_status != last_public_status:
                status(readiness.public_status)
                last_public_status = readiness.public_status
            session_started = time.monotonic()
            try:
                asyncio.run(
                    capture_runner(
                        readiness.credentials,
                        readiness.node,
                        service,
                        _caption_forwarder(router),
                        stop_event,
                    )
                )
            except (CoordinatorError, OSError, RuntimeError) as error:
                if not stop_event.is_set():
                    # Say which failure, not just that there was one. The
                    # codes are enum words - adapter_start_failed,
                    # transcription_failed - and carry no audio, no
                    # caption text and no credentials. Without this the
                    # operator watches the bot join and leave in a loop
                    # with four words to go on.
                    # CoordinatorError keeps its str() generic on purpose -
                    # that text is what a member could end up reading - and
                    # carries the real reason in .code. Reading str() here
                    # printed the same sentence twice and told nobody
                    # anything.
                    reason = getattr(error, "code", "") or str(error) \
                        or type(error).__name__
                    status(
                        "Error: Vemal's Ears capture pipeline stopped ("
                        + reason[:120] + ")"
                    )
                    last_public_status = ""
            finally:
                sink.close()
            if not stop_event.is_set():
                if time.monotonic() - session_started >= RETRY_HEALTHY_SECONDS:
                    backoff = retry_seconds
                stop_event.wait(backoff)
                backoff = min(backoff * 2, RETRY_MAX_SECONDS)
    except KeyboardInterrupt:
        pass
    status("Stopped.")
    return 0


def run_demo() -> int:
    return subprocess.call(
        [child_python(), "-E", "-s", "-B", "-u", str(OVERLAY), "--demo"],
        cwd=str(ROOT.parent),
        creationflags=NO_WINDOW,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--watch", action="store_true", help="Run under Downtime.")
    mode.add_argument("--demo", action="store_true", help="Show an eight-second preview.")
    args = parser.parse_args(argv)
    return run_watch() if args.watch else run_demo()


if __name__ == "__main__":
    raise SystemExit(main())
