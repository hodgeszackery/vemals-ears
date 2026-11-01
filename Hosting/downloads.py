"""Stream the publisher's fixed public host downloads over loopback HTTP.

This sidecar is publisher infrastructure, not part of the installed host.
Publish complete files into the public-only directory before starting it;
replace files atomically when publishing updates. No directory listing,
request-to-filename translation, or operator-state access is provided.
"""

from __future__ import annotations

import argparse
from contextlib import suppress
from dataclasses import dataclass
from email.utils import formatdate, parsedate_to_datetime
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import logging
import os
from pathlib import Path
import re
import socket
import stat
import threading


CHUNK_BYTES = 256 * 1024
MAX_CONNECTIONS = 16
REQUEST_TIMEOUT = 10.0
TRANSFER_TIMEOUT = 120.0
HEALTH_BODY = b'{"status":"ok","service":"vemals-ears-downloads"}\n'


@dataclass(frozen=True)
class Download:
    filename: str
    content_type: str
    disposition: str = "attachment"


DOWNLOADS = {
    "/download/vemals-ears-host": Download("Vemals-Ears-Host-Setup.exe", "application/octet-stream"),
    "/download/vemals-ears-host/source": Download("VemalsEars-Host.zip", "application/zip"),
    "/download/vemals-ears-host/sha256": Download(
        "Vemals-Ears-Host-Setup.sha256", "text/plain; charset=us-ascii", "inline"),
}


class UnsatisfiableRange(ValueError):
    """A requested byte range cannot be represented by this file."""


def byte_range(value: str | None, size: int) -> tuple[int, int] | None:
    """Return one inclusive byte range; ignore unsupported units/range sets.

RFC 9110 section 14.2 allows a server to ignore Range. Multipart ranges are
deliberately served as a complete 200 response instead of allocating buffers.
"""
    if value is None:
        return None
    unit, separator, positions = value.strip().partition("=")
    if not separator or unit.lower() != "bytes":
        return None
    if "," in positions:
        return None
    match = re.fullmatch(r"([0-9]{0,20})-([0-9]{0,20})", positions.strip())
    if not match or not any(match.groups()) or size == 0:
        raise UnsatisfiableRange
    first, last = match.groups()
    if not first:
        suffix = int(last)
        if suffix == 0:
            raise UnsatisfiableRange
        return max(0, size - suffix), size - 1
    start = int(first)
    end = int(last) if last else size - 1
    if start >= size or end < start:
        raise UnsatisfiableRange
    return start, min(end, size - 1)


def fingerprint(metadata: os.stat_result) -> tuple[int, ...]:
    return (metadata.st_dev, metadata.st_ino, metadata.st_size,
            metadata.st_mtime_ns, metadata.st_ctime_ns)


def if_range_matches(value: str, etag: str, modified: float) -> bool:
    value = value.strip()
    if value.startswith('"'):
        return value == etag
    if value.startswith("W/"):
        return False
    try:
        date = parsedate_to_datetime(value)
        return date.tzinfo is not None and int(date.timestamp()) == int(modified)
    except (ValueError, TypeError, OverflowError):
        return False


class DownloadServer(ThreadingHTTPServer):
    """Bound concurrent handlers so stalled downloads cannot grow threads."""

    daemon_threads = True
    block_on_close = False
    request_queue_size = 64

    def __init__(self, address, directory: Path, *, max_connections: int = MAX_CONNECTIONS,
                 transfer_timeout: float = TRANSFER_TIMEOUT, chunk_bytes: int = CHUNK_BYTES):
        self.directory = Path(directory).resolve(strict=True)
        if not self.directory.is_dir():
            raise ValueError("The public download directory is not a directory.")
        if not 1 <= max_connections <= 256 or not 0 < chunk_bytes <= CHUNK_BYTES:
            raise ValueError("Invalid download concurrency or chunk size.")
        if not 0 < transfer_timeout <= 600:
            raise ValueError("Invalid transfer timeout.")
        self.transfer_timeout = transfer_timeout
        self.chunk_bytes = chunk_bytes
        self._slots = threading.BoundedSemaphore(max_connections)
        self._connections: set[socket.socket] = set()
        self._connection_lock = threading.Lock()
        self._etag_lock = threading.Lock()
        self._etags: dict[str, tuple[tuple[int, ...], str]] = {}
        super().__init__(address, DownloadHandler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            with suppress(OSError):
                # Consume a bounded initial header block before closing. On
                # Windows, closing with the ordinary request still unread can
                # reset TCP and hide the 503 from a well-behaved HTTP client.
                request.settimeout(0.1)
                with suppress(OSError):
                    request.recv(8192)
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n"
                    b"Retry-After: 5\r\nCache-Control: no-store\r\n"
                    b"X-Content-Type-Options: nosniff\r\nConnection: close\r\n\r\n")
            self.shutdown_request(request)
            return
        with self._connection_lock:
            self._connections.add(request)
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._release(request)
            raise

    def _release(self, request):
        with self._connection_lock:
            self._connections.discard(request)
        self._slots.release()

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release(request)

    def server_close(self):
        with self._connection_lock:
            connections = tuple(self._connections)
        for connection in connections:
            with suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
            connection.close()
        super().server_close()

    def open_download(self, download: Download):
        path = self.directory / download.filename
        before = path.lstat()
        if (path.is_symlink() or not stat.S_ISREG(before.st_mode)
                or path.resolve(strict=True).parent != self.directory):
            raise FileNotFoundError
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        stream = os.fdopen(descriptor, "rb")
        try:
            metadata = os.fstat(stream.fileno())
            # On Windows, lstat may report creation time in st_ctime while
            # fstat reports change time. Compare their common identity/size/
            # modification fields; cache validators use fstat consistently.
            if not stat.S_ISREG(metadata.st_mode) or fingerprint(before)[:4] != fingerprint(metadata)[:4]:
                raise FileNotFoundError
            return stream, metadata
        except BaseException:
            stream.close()
            raise

    def etag(self, download: Download, stream, metadata: os.stat_result) -> str:
        key = fingerprint(metadata)
        # At most one bounded read-through per publication. Subsequent HEAD,
        # GET and resumed requests reuse its strong content hash.
        with self._etag_lock:
            cached = self._etags.get(download.filename)
            if cached is not None and cached[0] == key:
                return cached[1]
            digest = hashlib.sha256()
            while payload := stream.read(self.chunk_bytes):
                digest.update(payload)
            if fingerprint(os.fstat(stream.fileno())) != key:
                raise OSError("Published file changed while computing its validator.")
            value = '"sha256-' + digest.hexdigest() + '"'
            self._etags[download.filename] = (key, value)
            stream.seek(0)
            return value

    def handle_error(self, request, client_address):
        # No request target, user-supplied header, or local path is logged.
        logging.error("A download request failed unexpectedly.")


class DownloadHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "VemalsEarsDownloads/1"
    sys_version = ""
    timeout = REQUEST_TIMEOUT

    def log_request(self, code="-", size="-"):
        route = self.path
        if route not in DOWNLOADS and route != "/healthz":
            route = "unknown"
        method = self.command if self.command in {"GET", "HEAD"} else "unsupported"
        logging.info("%s %s %s", method, route, code)

    def log_message(self, format, *args):
        pass

    def _headers(self, status: int, content_type: str, length: int | None,
                 headers: dict[str, str] | None = None):
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()

    def _error(self, status: int, message: bytes, headers: dict[str, str] | None = None):
        self._headers(status, "text/plain; charset=us-ascii", len(message),
                      {"Cache-Control": "no-store", **(headers or {})})
        if self.command != "HEAD":
            self.wfile.write(message)

    def _serve(self, *, head: bool):
        self.connection.settimeout(self.server.transfer_timeout)
        route = self.path
        if route == "/healthz":
            self._headers(200, "application/json", len(HEALTH_BODY), {"Cache-Control": "no-store"})
            if not head:
                self.wfile.write(HEALTH_BODY)
            return
        download = DOWNLOADS.get(route)
        if download is None:
            self._error(404, b"Not found.\n")
            return
        try:
            stream, metadata = self.server.open_download(download)
        except OSError:
            self._error(404, b"Download unavailable.\n")
            return
        with stream:
            try:
                etag = self.server.etag(download, stream, metadata)
            except OSError:
                self._error(503, b"Download is being published; retry shortly.\n", {"Retry-After": "5"})
                return
            headers = {
                "Accept-Ranges": "bytes",
                "ETag": etag,
                "Last-Modified": formatdate(metadata.st_mtime, usegmt=True),
                "Cache-Control": "public, max-age=0, must-revalidate",
                "Content-Disposition": download.disposition + '; filename="' + download.filename + '"',
            }
            condition = self.headers.get("If-None-Match")
            if condition and any(value.strip().removeprefix("W/") in {etag, "*"}
                                 for value in condition.split(",")):
                self._headers(304, download.content_type, None, headers)
                return
            requested = None if head else self.headers.get("Range")
            if requested and self.headers.get("If-Range"):
                if not if_range_matches(self.headers["If-Range"], etag, metadata.st_mtime):
                    requested = None
            try:
                selected = byte_range(requested, metadata.st_size)
            except UnsatisfiableRange:
                self._error(416, b"Requested byte range is not satisfiable.\n",
                            {"Content-Range": "bytes */" + str(metadata.st_size), "Accept-Ranges": "bytes"})
                return
            if selected is None:
                start, length, status = 0, metadata.st_size, 200
            else:
                start, end = selected
                length, status = end - start + 1, 206
                headers["Content-Range"] = "bytes " + str(start) + "-" + str(end) + "/" + str(metadata.st_size)
            self._headers(status, download.content_type, length, headers)
            if head:
                return
            stream.seek(start)
            remaining = length
            while remaining:
                payload = stream.read(min(self.server.chunk_bytes, remaining))
                if not payload:
                    raise OSError("Published file ended before its advertised length.")
                self.wfile.write(payload)
                remaining -= len(payload)

    def _request(self, *, head=False):
        try:
            self._serve(head=head)
        except (OSError, TimeoutError):
            # Clients can pause, cancel or reconnect with Range at any point.
            # A partial transfer closes; it never emits a second HTTP response.
            self.close_connection = True

    def do_GET(self):
        self._request()

    def do_HEAD(self):
        self._request(head=True)

    def _unsupported(self):
        with suppress(OSError):
            self._error(405, b"Only GET and HEAD are supported.\n", {"Allow": "GET, HEAD"})

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _unsupported


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8773)
    args = parser.parse_args(argv)
    try:
        address = ipaddress.ip_address(args.host)
        if address.version != 4 or not address.is_loopback or not 1 <= args.port <= 65535:
            raise ValueError("Use an IPv4 loopback address and a port from 1 to 65535.")
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
        with DownloadServer((args.host, args.port), args.directory) as server:
            logging.info("Host download sidecar listening on %s:%s", args.host, args.port)
            try:
                server.serve_forever(poll_interval=0.2)
            except KeyboardInterrupt:
                pass
        return 0
    except (OSError, ValueError) as error:
        parser.exit(2, "Download sidecar: " + str(error) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
