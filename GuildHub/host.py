"""Run the consent-gated Discord/STT worker for a Vemal's Ears guild host."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import signal
import re
import stat
import threading
import time
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from VoiceCaptions.credentials import (
    CredentialValidationError,
    DiscordCredentials,
    validate_credentials,
)
from VoiceCaptions.events import CaptionEvent, ProtocolEvent, SessionEvent
from VoiceCaptions.coordinator import VoiceCaptionCoordinator, run_node_capture
from VoiceCaptions.voice_captions import check_runtime_readiness, run_watch

from .publisher import PublisherError, RemoteCaptionSink, publisher_url
from .quick_chat_worker import RemoteQuickChatWorker
from .worker_health import (
    DEFAULT_HEALTH_FILE,
    HEALTHY_STATES,
    MAX_HEALTH_FILE_BYTES,
    WorkerHealthError,
    WorkerHealthMarker,
    resolve_worker_health_file,
)


MAX_SETTING_FILE_BYTES = 4096

# The give-up note is rendered in a console pill, so it is bounded to
# something a person can read at a glance rather than to a file size.
MAX_GIVE_UP_NOTE_CHARS = 200
MAX_SETTING_PATH_CHARS = 4096

_DISCORD_SNOWFLAKE = re.compile(r"^[0-9]{17,20}$")
_PUBLISHER_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,192}$")

_APPLICATION_ID_SETTING = "VEMALS_EARS_DISCORD_APPLICATION_ID"
_BOT_TOKEN_SETTING = "VEMALS_EARS_DISCORD_BOT_TOKEN"
_GUILD_ID_SETTING = "VEMALS_EARS_GUILD_ID"
_SERVER_URL_SETTING = "VEMALS_EARS_SERVER_URL"
_PUBLISHER_TOKEN_SETTING = "VEMALS_EARS_PUBLISHER_TOKEN"
_VIEWER_IDENTITY_CONTEXT = b"vemals-ears-viewer-v1"


class HostConfigurationError(ValueError):
    """Headless host configuration is missing or unsafe."""


@dataclass(frozen=True, repr=False)
class HeadlessHostConfig:
    credentials: DiscordCredentials
    guild_id: str
    server_url: str
    publisher_token: str
    health_file: Path

    def __repr__(self) -> str:
        return "HeadlessHostConfig(<redacted>)"

    __str__ = __repr__


class _StaticCredentialStore:
    def __init__(self, credentials: DiscordCredentials) -> None:
        self._credentials = credentials

    def load(self) -> DiscordCredentials:
        return self._credentials

    def __repr__(self) -> str:
        return "_StaticCredentialStore(<redacted>)"


def _configuration_error() -> HostConfigurationError:
    return HostConfigurationError("Vemal's Ears host configuration is invalid.")


def derive_viewer_identity_key(publisher_token: str) -> bytes:
    """Derive the stable guild-scoped key used to pseudonymize Discord IDs."""
    if not isinstance(publisher_token, str) or not _PUBLISHER_TOKEN.fullmatch(
        publisher_token
    ):
        raise _configuration_error()
    return hmac.new(
        publisher_token.encode("ascii"),
        _VIEWER_IDENTITY_CONTEXT,
        hashlib.sha256,
    ).digest()


def _environment_value(
    environment: Mapping[str, str], name: str
) -> str | None:
    value = environment.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _configuration_error()
    value = value.strip()
    return value or None


def _read_setting_file(path_text: str) -> str:
    if len(path_text) > MAX_SETTING_PATH_CHARS or "\x00" in path_text:
        raise _configuration_error()
    try:
        path = Path(path_text)
        if not path.is_absolute():
            raise _configuration_error()
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise _configuration_error()
        with path.open("rb") as handle:
            payload = handle.read(MAX_SETTING_FILE_BYTES + 1)
    except HostConfigurationError:
        raise
    except (OSError, ValueError):
        raise _configuration_error() from None
    if len(payload) > MAX_SETTING_FILE_BYTES:
        raise _configuration_error()
    try:
        value = payload.decode("utf-8").strip()
    except UnicodeError:
        raise _configuration_error() from None
    if not value:
        raise _configuration_error()
    return value


def _load_setting(environment: Mapping[str, str], name: str) -> str:
    direct = _environment_value(environment, name)
    file_path = _environment_value(environment, name + "_FILE")
    if direct is not None and file_path is not None:
        raise _configuration_error()
    if direct is not None:
        value = direct
    elif file_path is not None:
        value = _read_setting_file(file_path)
    else:
        raise _configuration_error()
    try:
        value_size = len(value.encode("utf-8"))
    except UnicodeError:
        raise _configuration_error() from None
    if value_size > MAX_SETTING_FILE_BYTES or any(
        ord(character) < 0x21 or ord(character) > 0x7E for character in value
    ):
        raise _configuration_error()
    return value


# How many consecutive quick failures before the worker stops asking to
# be restarted. Measured on a real machine: without a limit this reached
# 363 restarts across 23 hours, every one of them logging the same line,
# with nothing escalating and nothing surfacing the count.
CONSECUTIVE_FAILURE_LIMIT = 5

# A run that stayed up this long counts as recovery and clears the tally.
# Without it a network blip every few hours would accumulate into a
# give-up that nobody could explain.
HEALTHY_RUN_SECONDS = 120.0

# Failures that all happened inside this window are a restart storm, not
# a broken worker, and must never reach the limit above.
#
# Measured on 2026-09-03: the relay restarted four times in three minutes
# and this worker spent all five of its failures in 66 SECONDS - at
# 00:22:25, :32, :38, then 00:23:24 and :31 - then stopped for good.
# Captions stayed dead seven and a half hours. A deploy bounces the relay
# for minutes, so a count with no clock attached cannot survive one.
GIVE_UP_MIN_SPAN_SECONDS = 300.0

# How long the service manager should wait before restarting a worker
# that failed, in milliseconds. Install-Downtime-Services.ps1 sets this
# as AppRestartDelay, and the installer test pins the two together.
#
# It has to be big enough that CONSECUTIVE_FAILURE_LIMIT restarts cannot
# fit inside GIVE_UP_MIN_SPAN_SECONDS - otherwise the count is spent
# before the clock ever matters, which is precisely how a deploy took
# captions off the air on 2026-09-03.
WORKER_RESTART_DELAY_MS = 30_000

# The service manager is configured to treat this code as "stop", where a
# 1 means "restart me". Anything that cannot be fixed by trying again
# should use it.
GIVE_UP_EXIT_CODE = 3


def _failure_tally_file(health_file: Path) -> Path:
    """Beside the health marker, so it shares its lifetime and location."""
    return health_file.with_name(health_file.name + ".failures")


def _read_failure_tally(path: Path, now: float | None = None) -> tuple[int, float]:
    """The consecutive failure count and when the first of them happened.

    A count on its own cannot tell a restart storm from a broken worker,
    so the file carries the first failure's wall-clock time beside it.
    Wall clock rather than monotonic because this outlives the process
    that wrote it.

    Anything unreadable, corrupt or absurd reads as no failures. A tally
    that cannot be trusted must never be the reason a worker gives up.
    """
    moment = time.time() if now is None else now
    try:
        raw = path.read_text(encoding="ascii").strip()
    except (OSError, ValueError):
        return 0, moment
    parts = raw.split()
    try:
        value = int(parts[0])
    except (IndexError, ValueError):
        return 0, moment
    if not 0 <= value < 10_000:
        return 0, moment
    # A single number is a tally written before this file carried a time.
    # Treat its first failure as now: the worst that costs is one extra
    # span's patience, which is the safe direction.
    first = moment
    if len(parts) > 1:
        try:
            candidate = float(parts[1])
        except ValueError:
            candidate = moment
        # A first-failure time in the future, or absurdly far past, is a
        # clock that moved. Fall back to now rather than trusting it.
        if 0.0 < candidate <= moment:
            first = candidate
    return value, first


def _record_failure(path: Path, now: float | None = None) -> tuple[int, float]:
    moment = time.time() if now is None else now
    count, first = _read_failure_tally(path, moment)
    count += 1
    if count == 1:
        first = moment
    try:
        path.write_text("%d %.3f" % (count, first), encoding="ascii")
    except OSError:
        # Losing the tally means retrying forever, which is the old
        # behaviour rather than a new failure. Never let it stop a start.
        pass
    return count, first


def _clear_failure_tally(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
    _clear_give_up_note(_give_up_note_file(path))


def _give_up_note_file(tally_file: Path) -> Path:
    """Beside the tally, and durable across the stopped service.

    The health marker cannot carry this: the console only reads it while
    the service is RUNNING, so a worker that gave up looks exactly like
    one somebody stopped on purpose. That ambiguity is what made a
    seven-and-a-half-hour outage look like a quiet evening.
    """
    return tally_file.with_name(tally_file.name + ".gaveup")


def _write_give_up_note(path: Path, reason: str) -> None:
    text = " ".join(reason.split())[:MAX_GIVE_UP_NOTE_CHARS]
    try:
        path.write_text(text + "\n", encoding="ascii", errors="replace")
    except OSError:
        # Failing to explain a give-up must not change the give-up.
        pass


def _clear_give_up_note(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def read_give_up_note(health_file: Path) -> str | None:
    """Why the worker stopped asking to be restarted, if it did.

    Public because the console renders it: a stopped worker and a worker
    that gave up are different facts and must not share one pill.
    """
    path = _give_up_note_file(_failure_tally_file(health_file))
    try:
        raw = path.read_text(encoding="ascii", errors="replace")
    except (OSError, ValueError):
        return None
    reason = " ".join(raw.split())[:MAX_GIVE_UP_NOTE_CHARS]
    return reason or None


def _load_health_file(environment: Mapping[str, str]) -> Path:
    try:
        return resolve_worker_health_file(environment)
    except WorkerHealthError:
        raise _configuration_error() from None


def load_host_config(
    environment: Mapping[str, str] | None = None,
) -> HeadlessHostConfig:
    """Load and validate the headless host without using Windows storage."""
    source = os.environ if environment is None else environment
    try:
        credentials = validate_credentials(
            _load_setting(source, _APPLICATION_ID_SETTING),
            _load_setting(source, _BOT_TOKEN_SETTING),
        )
        guild_id = _load_setting(source, _GUILD_ID_SETTING)
        if not _DISCORD_SNOWFLAKE.fullmatch(guild_id):
            raise _configuration_error()
        server_url = _load_setting(source, _SERVER_URL_SETTING)
        parsed_server = urlsplit(server_url)
        if parsed_server.path not in {"", "/"}:
            raise _configuration_error()
        publisher_url(server_url)
        publisher_token = _load_setting(source, _PUBLISHER_TOKEN_SETTING)
        if not _PUBLISHER_TOKEN.fullmatch(publisher_token):
            raise _configuration_error()
        health_file = _load_health_file(source)
    except HostConfigurationError:
        raise
    except (CredentialValidationError, PublisherError, TypeError, ValueError):
        raise _configuration_error() from None
    return HeadlessHostConfig(
        credentials=credentials,
        guild_id=guild_id,
        server_url=server_url,
        publisher_token=publisher_token,
        health_file=health_file,
    )


class HealthReportingSink:
    """Publish captions while exposing only accepted session state."""

    def __init__(
        self,
        sink: RemoteCaptionSink,
        marker: WorkerHealthMarker,
        quick_chat: RemoteQuickChatWorker | None = None,
    ) -> None:
        self._sink = sink
        self._marker = marker
        self._quick_chat = quick_chat

    def _fail(self) -> bool:
        try:
            self._marker.clear()
        except WorkerHealthError:
            pass
        try:
            self._sink.close()
        except Exception:
            pass
        if self._quick_chat is not None:
            try:
                self._quick_chat.close()
            except Exception:
                pass
        return False

    def alive(self) -> bool:
        try:
            alive = self._sink.alive()
        except Exception:
            return self._fail()
        if not alive:
            return self._fail()
        if self._quick_chat is not None:
            try:
                if not self._quick_chat.alive():
                    return self._fail()
            except Exception:
                return self._fail()
        return True

    def next_quick_chat(self):
        if self._quick_chat is None:
            return None
        return self._quick_chat.next_quick_chat()

    def complete_quick_chat(self, request_id: str, status: str) -> bool:
        if self._quick_chat is None:
            return False
        return self._quick_chat.complete_quick_chat(request_id, status)

    def update_audience(
        self, generation: int, revision: int, tokens: tuple[str, ...]
    ) -> bool:
        try:
            accepted = self._sink.update_audience(generation, revision, tokens)
        except Exception:
            return self._fail()
        return True if accepted is True else self._fail()

    def link_viewer(self, code: str, token: str) -> bool:
        try:
            accepted = self._sink.link_viewer(code, token)
        except Exception:
            return self._fail()
        return True if accepted is True else self._fail()

    def enroll_viewer(self, code: str, token: str, ttl_seconds: int) -> bool:
        # 0.4.5 to 0.4.8 shipped without this: the coordinator looks the
        # method up dynamically and, finding nothing here, dropped every
        # pairing invitation without a frame, an error or a log line.
        try:
            accepted = self._sink.enroll_viewer(code, token, ttl_seconds)
        except Exception:
            return self._fail()
        return True if accepted is True else self._fail()

    def send(self, event: ProtocolEvent) -> bool:
        try:
            if isinstance(event, CaptionEvent):
                accepted = self._sink.send(
                    event,
                    audience_generation=event.audience_generation,
                    audience_revision=event.audience_revision,
                )
            else:
                accepted = self._sink.send(event)
        except Exception:
            return self._fail()
        if not accepted:
            return self._fail()
        if not isinstance(event, SessionEvent):
            return True
        try:
            if event.state in HEALTHY_STATES:
                self._marker.set_state(event.state)
            elif event.state == "stopped":
                # A stopped caption session leaves the Discord/STT worker
                # available for the next explicit start command.
                self._marker.set_state("ready")
            else:
                self._marker.clear()
            return True
        except (OSError, RuntimeError, WorkerHealthError):
            return self._fail()

    def close(self) -> None:
        try:
            self._sink.close()
        finally:
            if self._quick_chat is not None:
                try:
                    self._quick_chat.close()
                except Exception:
                    pass
            try:
                self._marker.clear()
            except WorkerHealthError:
                pass


@contextmanager
def _discord_guild_scope(guild_id: str):
    previous = os.environ.get("DISCORD_GUILD_ID")
    os.environ["DISCORD_GUILD_ID"] = guild_id
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("DISCORD_GUILD_ID", None)
        else:
            os.environ["DISCORD_GUILD_ID"] = previous


def _clear_marker(marker: WorkerHealthMarker) -> bool:
    try:
        marker.clear()
        return True
    except WorkerHealthError:
        return False


def _install_stop_handlers(stop_event: threading.Event) -> None:
    """SIGTERM must run the shutdown chain, or consent persistence is fiction.

    The container stops this process with SIGTERM, and Python's default
    disposition terminates without unwinding. run_watch's finally never
    runs, so the node adapter is never asked to stop, so the consent
    ledger's close() - the only remover of its dirty marker - never
    happens, and the next run distrusts the file and re-asks everyone.
    Every docker compose restart therefore discarded every remembered
    choice, declines included. Setting the stop event instead lets the
    ordinary stop path run end to end.
    """

    def request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(signum, request_stop)
        except (ValueError, OSError):
            # Not the main thread, or a platform without this signal.
            # Best effort: the worker still runs, it just cannot stop
            # gracefully, which is no worse than before.
            pass


def main(
    argv: list[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--watch",
        action="store_true",
        help="Run the guild caption worker until it is stopped.",
    )
    mode.add_argument(
        "--healthcheck",
        action="store_true",
        help="Check the worker's non-secret runtime state.",
    )
    args = parser.parse_args(argv)
    source = os.environ if environment is None else environment

    try:
        marker = WorkerHealthMarker(_load_health_file(source))
    except (HostConfigurationError, WorkerHealthError):
        return 1 if args.healthcheck else 2
    if args.healthcheck:
        return 0 if marker.is_healthy() else 1
    if not _clear_marker(marker):
        print("Error: Vemal's Ears worker health is unavailable", flush=True)
        return 1

    try:
        config = load_host_config(source)
    except HostConfigurationError:
        print("Setup required: connect this host to Vemal's Ears", flush=True)
        return 2

    credential_store = _StaticCredentialStore(config.credentials)

    def readiness_check():
        return check_runtime_readiness(credential_store=credential_store)

    def remote_sink() -> HealthReportingSink:
        sink = RemoteCaptionSink.start(config.server_url, config.publisher_token)
        try:
            quick_chat = RemoteQuickChatWorker.start(
                config.server_url, config.publisher_token
            )
        except Exception:
            sink.close()
            raise
        return HealthReportingSink(sink, marker, quick_chat)

    stop_event = threading.Event()
    _install_stop_handlers(stop_event)

    tally_file = _failure_tally_file(marker.path)
    started_at = time.monotonic()
    identity_key = derive_viewer_identity_key(config.publisher_token)

    async def guild_capture_runner(
        credentials, node, stt_service, event_sink, capture_stop_event
    ):
        await run_node_capture(
            credentials,
            node,
            stt_service,
            event_sink,
            capture_stop_event,
            coordinator_factory=lambda service, sink: VoiceCaptionCoordinator(
                service,
                sink,
                identity_key=identity_key,
            ),
        )

    result = 1
    detail = ""
    try:
        with _discord_guild_scope(config.guild_id):
            result = run_watch(
                stop_event=stop_event,
                readiness_check=readiness_check,
                overlay_factory=remote_sink,
                capture_runner=guild_capture_runner,
            )
    except PublisherError as error:
        # Say which thing failed. PublisherError is raised in a dozen
        # places - a bad address, an unencrypted remote, a missing
        # credential, an uninstalled network runtime, the quick-chat
        # relay - and naming one of them confidently sends whoever reads
        # the log at the wrong component. This cost 357 crash-loops and
        # three investigations of a caption server that was working.
        detail = str(error).strip() or "no detail"
        print("Error: " + detail, flush=True)
        result = 1
    finally:
        if not _clear_marker(marker):
            result = 1

    ran_for = time.monotonic() - started_at
    if result == 0 or ran_for >= HEALTHY_RUN_SECONDS:
        # It worked, or it worked for a while. Either way the next
        # failure is a fresh problem and starts its own count.
        _clear_failure_tally(tally_file)
        return result

    failures, first_failure = _record_failure(tally_file)
    spanned = max(0.0, time.time() - first_failure)
    return handle_failure(tally_file, failures, spanned, detail, result)


def should_give_up(failures: int, spanned: float) -> bool:
    """Whether the worker stops asking to be restarted.

    BOTH conditions, and the second is the one that was missing. Five
    failures inside a minute is a relay that is not up yet; five spread
    over the span below is a worker that is broken. On 2026-09-03 the
    count alone took captions off the air for seven and a half hours
    over a deploy.
    """
    return (
        failures >= CONSECUTIVE_FAILURE_LIMIT
        and spanned >= GIVE_UP_MIN_SPAN_SECONDS
    )


def handle_failure(
    tally_file: Path,
    failures: int,
    spanned: float,
    detail: str,
    result: int,
) -> int:
    """Say what is happening, leave a note if we are done, return the code.

    Lives out here rather than inline in main() because it is the part
    that has to be right: a sabotage run proved that deleting the note
    or dropping the clock check left a green suite behind.
    """
    if should_give_up(failures, spanned):
        print(
            "Vemal's Ears worker failed %d times over %d minutes and is "
            "giving up. The last error is above. Fix it, then start the "
            "service again."
            % (failures, round(spanned / 60.0)),
            flush=True,
        )
        _write_give_up_note(
            _give_up_note_file(tally_file), detail or "no detail was reported"
        )
        return GIVE_UP_EXIT_CODE
    if failures >= CONSECUTIVE_FAILURE_LIMIT:
        # Past the count but not the clock. This is the shape of a relay
        # that is restarting, and the worker's job is to wait it out
        # rather than take itself off the air over a deploy.
        print(
            "Vemal's Ears worker has failed %d times, but all within %d "
            "seconds - that reads as a server restarting, so it keeps "
            "trying."
            % (failures, round(spanned)),
            flush=True,
        )
    else:
        # Say the count on every failure, not only the last one. The
        # measured bug was not a missing final message - it was 363
        # identical lines with no number anywhere, so nothing ever read
        # as escalating.
        print(
            "Vemal's Ears worker has failed %d time%s in a row. "
            "It will stop trying after %d spread over %d minutes."
            % (
                failures,
                "" if failures == 1 else "s",
                CONSECUTIVE_FAILURE_LIMIT,
                round(GIVE_UP_MIN_SPAN_SECONDS / 60.0),
            ),
            flush=True,
        )
    # Deliberately no sleep here. Spacing restarts is the service
    # manager's job through AppRestartDelay, and doing it in-process both
    # delays a clean exit and made the host suite take five minutes.
    return result


if __name__ == "__main__":
    raise SystemExit(main())
