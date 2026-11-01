"""Real HTTP witnesses for bounded public downloads and resume behavior."""

from contextlib import contextmanager
import hashlib
import http.client
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from Hosting import downloads


EXE_PATH = "/download/vemals-ears-host"


class SmallSendBufferServer(downloads.DownloadServer):
    def get_request(self):
        connection, address = super().get_request()
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8192)
        return connection, address


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.payload = bytes(range(256)) * 128
        self.exe = self.root / "Vemals-Ears-Host-Setup.exe"
        self.exe.write_bytes(self.payload)
        (self.root / "VemalsEars-Host.zip").write_bytes(b"source archive fixture\n")
        (self.root / "Vemals-Ears-Host-Setup.sha256").write_text(
            hashlib.sha256(self.payload).hexdigest() + "  Vemals-Ears-Host-Setup.exe\n", encoding="ascii")
        (self.root / "private.txt").write_text("XENO_PRIVATE_NOT_A_DOWNLOAD", encoding="ascii")

    @contextmanager
    def running(self, cls=downloads.DownloadServer, **options):
        server = cls(("127.0.0.1", 0), self.root, **options)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.wait_for(lambda: not server._connections)

    def wait_for(self, condition, timeout=3):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() >= deadline:
                self.fail("Download handler did not complete within the cleanup deadline.")
            time.sleep(0.01)

    def request(self, server, path=EXE_PATH, method="GET", headers=None):
        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, {key.lower(): value for key, value in response.getheaders()}, response.read()
        finally:
            connection.close()

    def test_exact_public_routes_and_head_have_correct_headers(self):
        with self.running() as server:
            for route, download in downloads.DOWNLOADS.items():
                with self.subTest(route=route):
                    status, headers, body = self.request(server, route)
                    self.assertEqual(200, status)
                    self.assertEqual((self.root / download.filename).read_bytes(), body)
                    self.assertEqual(len(body), int(headers["content-length"]))
                    self.assertEqual(download.content_type, headers["content-type"])
                    self.assertEqual("nosniff", headers["x-content-type-options"])
                    self.assertEqual("bytes", headers["accept-ranges"])
                    self.assertIn(download.filename, headers["content-disposition"])
                    self.assertIn("must-revalidate", headers["cache-control"])
                    head_status, head_headers, head_body = self.request(server, route, "HEAD")
                    self.assertEqual(200, head_status)
                    self.assertEqual(b"", head_body)
                    for name in ("content-length", "etag", "last-modified", "content-type"):
                        self.assertEqual(headers[name], head_headers[name])
            status, _, body = self.request(server, "/healthz")
            self.assertEqual(200, status)
            self.assertEqual({"status": "ok", "service": "vemals-ears-downloads"}, json.loads(body))
            status, _, body = self.request(server, "/healthz", "HEAD")
            self.assertEqual((200, b""), (status, body))

    def test_closed_open_suffix_and_unsatisfiable_ranges(self):
        with self.running() as server:
            for value, start, end in (
                ("bytes=0-9", 0, 9), ("bytes=17-", 17, len(self.payload) - 1),
                ("bytes=-7", len(self.payload) - 7, len(self.payload) - 1),
                ("bytes=-999999", 0, len(self.payload) - 1),
                ("bytes=9-999999", 9, len(self.payload) - 1),
            ):
                with self.subTest(value=value):
                    status, headers, body = self.request(server, headers={"Range": value})
                    self.assertEqual(206, status)
                    self.assertEqual(self.payload[start:end + 1], body)
                    self.assertEqual("bytes " + str(start) + "-" + str(end) + "/" + str(len(self.payload)),
                                     headers["content-range"])
                    self.assertEqual(len(body), int(headers["content-length"]))
            for value in ("bytes=999999-", "bytes=9-3", "bytes=-0", "bytes=-", "bytes=a-4"):
                with self.subTest(value=value):
                    status, headers, body = self.request(server, headers={"Range": value})
                    self.assertEqual(416, status)
                    self.assertEqual("bytes */" + str(len(self.payload)), headers["content-range"])
                    self.assertNotEqual(self.payload, body)
            for value in ("items=0-2", "bytes=0-0,2-2"):
                status, _, body = self.request(server, headers={"Range": value})
                self.assertEqual((200, self.payload), (status, body))
            status, headers, body = self.request(server, method="HEAD", headers={"Range": "bytes=0-9"})
            self.assertEqual((200, b""), (status, body))
            self.assertEqual(str(len(self.payload)), headers["content-length"])
            self.assertNotIn("content-range", headers)

    def test_strong_validators_make_resumes_safe_across_publications(self):
        with self.running() as server:
            _, headers, _ = self.request(server, method="HEAD")
            etag = headers["etag"]
            self.assertEqual('"sha256-' + hashlib.sha256(self.payload).hexdigest() + '"', etag)
            for validator in (etag, headers["last-modified"]):
                status, _, body = self.request(server, headers={"Range": "bytes=8-11", "If-Range": validator})
                self.assertEqual((206, self.payload[8:12]), (status, body))
            for validator in ('"different"', "W/" + etag, "not-a-date"):
                status, _, body = self.request(server, headers={"Range": "bytes=8-11", "If-Range": validator})
                self.assertEqual((200, self.payload), (status, body))
            for condition in (etag, "W/" + etag, "*"):
                status, _, body = self.request(server, headers={"If-None-Match": condition})
                self.assertEqual((304, b""), (status, body))
            replacement = b"a later publication with different bytes"
            self.exe.write_bytes(replacement)
            status, changed_headers, body = self.request(server, headers={"Range": "bytes=8-11", "If-Range": etag})
            self.assertEqual((200, replacement), (status, body))
            self.assertNotEqual(etag, changed_headers["etag"])

    def test_arbitrary_files_and_traversal_are_never_served(self):
        with self.running() as server:
            for route in (
                "/", "/private.txt", "/download/private.txt", EXE_PATH + "/private.txt",
                EXE_PATH + "/../private.txt", EXE_PATH + "/%2e%2e/private.txt",
                EXE_PATH + "/source/", "/download%2fvemals-ears-host", "/healthz/../private.txt",
            ):
                with self.subTest(route=route):
                    status, headers, body = self.request(server, route)
                    self.assertEqual(404, status)
                    self.assertNotIn(b"XENO_PRIVATE", body)
                    self.assertEqual("no-store", headers["cache-control"])
            status, _, body = self.request(server, EXE_PATH + "?version=fixture")
            self.assertEqual(404, status)
            self.assertNotEqual(self.payload, body)
            status, headers, _ = self.request(server, method="POST")
            self.assertEqual(405, status)
            self.assertEqual("GET, HEAD", headers["allow"])
            self.exe.unlink()
            self.exe.mkdir()
            status, _, _ = self.request(server)
            self.assertEqual(404, status)

    def test_file_reads_are_bounded_and_head_reuses_the_hash(self):
        reads = []
        with self.running(chunk_bytes=4096) as server:
            original = server.open_download

            class GuardedStream:
                def __init__(inner, stream):
                    inner.stream = stream

                def read(inner, size=-1):
                    self.assertGreater(size, 0)
                    self.assertLessEqual(size, 4096)
                    reads.append(size)
                    return inner.stream.read(size)

                def __getattr__(inner, name):
                    return getattr(inner.stream, name)

                def __enter__(inner):
                    return inner

                def __exit__(inner, *args):
                    inner.stream.close()

            def open_guarded(download):
                stream, metadata = original(download)
                return GuardedStream(stream), metadata

            with mock.patch.object(server, "open_download", side_effect=open_guarded):
                status, _, body = self.request(server)
                self.assertEqual((200, self.payload), (status, body))
                self.assertGreater(len(reads), 2)
                reads.clear()
                self.assertEqual(200, self.request(server, method="HEAD")[0])
                self.assertEqual([], reads)

    def test_slow_transfer_outlives_socket_timeout_and_health_stays_responsive(self):
        payload = bytes(range(256)) * 4096
        self.exe.write_bytes(payload)
        with self.running(SmallSendBufferServer, transfer_timeout=0.25, chunk_bytes=16384) as server:
            connection = http.client.HTTPConnection(*server.server_address, timeout=5)
            connection.connect()
            connection.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8192)
            try:
                connection.request("GET", EXE_PATH)
                response = connection.getresponse()
                self.assertEqual(200, response.status)
                started = time.monotonic()
                chunks = [response.read(4096)]
                self.assertEqual(200, self.request(server, "/healthz")[0])
                while chunk := response.read(4096):
                    chunks.append(chunk)
                    time.sleep(0.003)
                self.assertGreater(time.monotonic() - started, server.transfer_timeout)
                self.assertEqual(payload, b"".join(chunks))
            finally:
                connection.close()

    def test_concurrency_is_bounded_and_cancelled_download_releases_capacity(self):
        self.exe.write_bytes(self.payload * 128)
        with self.running(SmallSendBufferServer, max_connections=1) as server:
            held = socket.create_connection(server.server_address, timeout=3)
            try:
                self.wait_for(lambda: len(server._connections) == 1)
                status, headers, _ = self.request(server, "/healthz")
                self.assertEqual(503, status)
                self.assertEqual("5", headers["retry-after"])
            finally:
                held.close()
            self.wait_for(lambda: not server._connections)
            with mock.patch.object(server, "handle_error") as unexpected:
                with socket.create_connection(server.server_address, timeout=3) as interrupted:
                    interrupted.sendall(b"GET /download/vemals-ears-host HTTP/1.1\r\nHost: localhost\r\n\r\n")
                    self.assertIn(b"200 OK", interrupted.recv(512))
                    interrupted.shutdown(socket.SHUT_RDWR)
                self.wait_for(lambda: not server._connections)
                unexpected.assert_not_called()
            self.assertEqual(200, self.request(server, "/healthz")[0])

    def test_standalone_script_starts_without_the_repository_on_python_path(self):
        script = self.root / "publisher-runtime/downloads.py"
        script.parent.mkdir()
        shutil.copyfile(Path(downloads.__file__), script)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", str(script), "--directory", str(self.root),
             "--host", "127.0.0.1", "--port", str(port)],
            cwd=script.parent, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            deadline = time.monotonic() + 5
            while True:
                if process.poll() is not None:
                    output, _ = process.communicate(timeout=2)
                    self.fail("Standalone download server exited: " + output)
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                try:
                    connection.request("GET", "/healthz")
                    response = connection.getresponse()
                    self.assertEqual(200, response.status)
                    self.assertEqual("vemals-ears-downloads", json.loads(response.read())["service"])
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        self.fail("Standalone download server did not become ready.")
                    time.sleep(0.01)
                finally:
                    connection.close()
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)

    def test_cli_refuses_accidental_non_loopback_exposure(self):
        with mock.patch.object(downloads, "DownloadServer") as server:
            with self.assertRaises(SystemExit) as stopped, mock.patch.object(sys, "stderr"):
                downloads.main(["--directory", str(self.root), "--host", "0.0.0.0"])
            self.assertEqual(2, stopped.exception.code)
            server.assert_not_called()


if __name__ == "__main__":
    unittest.main()
