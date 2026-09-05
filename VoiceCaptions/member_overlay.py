"""Launch and feed the member-facing Vemal's Ears caption overlay."""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
from pathlib import Path

from .events import ProtocolEvent, event_to_json


ROOT = Path(__file__).resolve().parent
OVERLAY = ROOT / "overlay.py"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Where the overlay child's stderr lands. It went to DEVNULL before,
# which made one real outage undiagnosable by design: the child can die
# at import and nothing anywhere says why. Appended across launches,
# truncated at spawn once it grows past the cap.
CHILD_LOG_CAP_BYTES = 256 * 1024


def overlay_child_log_path() -> Path:
    import os
    root = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    return root / "WoWVoiceCaptions" / "overlay-child.log"


def _child_stderr_handle():
    """An append handle for the child's stderr, or DEVNULL as last resort.

    Never let logging be the reason the overlay cannot start.
    """
    try:
        path = overlay_child_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > CHILD_LOG_CAP_BYTES:
            path.unlink()
        return open(path, "ab")
    except OSError:
        return subprocess.DEVNULL
OVERLAY_QUEUE_SIZE = 32
_OVERLAY_STOP = object()


def child_python() -> str:
    executable = Path(sys.executable)
    if executable.name.lower() == "python.exe":
        windowed = executable.with_name("pythonw.exe")
        if windowed.is_file():
            return str(windowed)
    return str(executable)


class OverlaySink:
    """Bounded asynchronous pipe writer for sanitized overlay events."""

    def __init__(
        self,
        process: subprocess.Popen[str],
        *,
        max_pending: int = OVERLAY_QUEUE_SIZE,
    ) -> None:
        if max_pending < 1 or max_pending > 128:
            raise ValueError("max_pending must be between 1 and 128")
        self.process = process
        self._pending: queue.Queue[object] = queue.Queue(maxsize=max_pending)
        self._failed = threading.Event()
        self._closing = threading.Event()
        self._active_payload: str | None = None
        self._writer = threading.Thread(
            target=self._write_events,
            name="VoiceCaptionsOverlayWriter",
            daemon=True,
        )
        self._writer.start()

    @classmethod
    def start(cls) -> "OverlaySink":
        stderr_handle = _child_stderr_handle()
        process = subprocess.Popen(
            [child_python(), "-E", "-s", "-B", "-u", str(OVERLAY), "--exit-on-eof"],
            cwd=str(ROOT.parent),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=stderr_handle,
            text=True,
            encoding="utf-8",
            errors="strict",
            bufsize=1,
            creationflags=NO_WINDOW,
        )
        if stderr_handle is not subprocess.DEVNULL:
            # The child holds its own copy of the handle now.
            stderr_handle.close()
        return cls(process)

    def alive(self) -> bool:
        return (
            not self._closing.is_set()
            and not self._failed.is_set()
            and self._writer.is_alive()
            and self.process.poll() is None
        )

    # How long a full queue may hold a caption before the child is judged
    # wedged and the supervisor's restart path takes over.
    SEND_PATIENCE_SECONDS = 0.25

    def send(self, event: ProtocolEvent) -> bool:
        if not self.alive():
            return False
        try:
            # Bounded blocking, not put_nowait: the relay replays up to 256
            # envelopes back-to-back on reconnect, and judging the 33rd
            # caption a fatal failure threw the client into a reconnect
            # loop that met the same replay every time. A healthy overlay
            # drains continuously so a burst waits microseconds per slot;
            # a child that stopped reading does not free one in a quarter
            # second either, so the wedged case still fails closed below.
            self._pending.put(
                event_to_json(event) + "\n",
                timeout=self.SEND_PATIENCE_SECONDS,
            )
            return True
        except queue.Full:
            self._failed.set()
            return False

    def _write_events(self) -> None:
        stream = self.process.stdin
        try:
            if stream is None:
                self._failed.set()
                return
            while True:
                payload = self._pending.get()
                try:
                    if payload is _OVERLAY_STOP:
                        return
                    if not isinstance(payload, str) or self.process.poll() is not None:
                        self._failed.set()
                        return
                    self._active_payload = payload
                    stream.write(payload)
                    stream.flush()
                except (BrokenPipeError, OSError, UnicodeError):
                    self._failed.set()
                    return
                finally:
                    self._active_payload = None
                    payload = None
                    self._pending.task_done()
        finally:
            if stream is not None:
                try:
                    stream.close()
                except (BrokenPipeError, OSError):
                    pass

    def _terminate_process(self) -> None:
        if self.process.poll() is not None:
            return
        try:
            self.process.terminate()
        except ProcessLookupError:
            return
        except OSError:
            pass
        try:
            self.process.wait(timeout=2)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            self.process.kill()
        except ProcessLookupError:
            return
        except OSError:
            pass
        try:
            self.process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def close(self) -> None:
        first_close = not self._closing.is_set()
        self._closing.set()
        if first_close:
            # The sentinel is the only thing that releases the writer from its
            # untimed get(), so a full queue must not be allowed to drop it or
            # the thread parks forever. Nothing can enqueue once _closing is
            # set, so evicting captions to make room terminates, and a caption
            # lost during shutdown is never displayed anyway.
            while True:
                try:
                    self._pending.put_nowait(_OVERLAY_STOP)
                    break
                except queue.Full:
                    try:
                        self._pending.get_nowait()
                    except queue.Empty:
                        pass
                    else:
                        self._pending.task_done()

        self._writer.join(timeout=0.25)
        if self._writer.is_alive():
            self._terminate_process()
        elif self.process.poll() is None:
            try:
                self.process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                self._terminate_process()
        self._writer.join(timeout=1)
        if not self._writer.is_alive():
            while True:
                try:
                    self._pending.get_nowait()
                except queue.Empty:
                    break
                else:
                    self._pending.task_done()
