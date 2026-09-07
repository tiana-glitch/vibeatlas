import json
import socket
import threading
import time
import unittest
from email.message import Message
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

from career_copilot import Orchestrator
from server import RequestError, build_server, make_handler


def _initialise_preview_vault(root: Path) -> None:
    (root / "01-知识库目录.md").write_text(
        "---\ntype: index\n---\n\n# 知识库目录\n", encoding="utf-8"
    )
    (root / "02-更新流水账.md").write_text(
        "---\ntype: log\n---\n\n# 更新流水账\n", encoding="utf-8"
    )


def _read_response(connection: socket.socket) -> bytes:
    connection.settimeout(5)
    chunks = []
    while True:
        try:
            chunk = connection.recv(65536)
        except socket.timeout:
            break
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


class RequestBodyFramingTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.root = Path(self.folder.name)
        _initialise_preview_vault(self.root)
        self.http_server = build_server(
            host="127.0.0.1",
            port=0,
            directory=self.root,
            vault_root=self.root,
        )
        self.thread = threading.Thread(target=self.http_server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.http_server.server_address

    def tearDown(self):
        self.http_server.shutdown()
        self.http_server.server_close()
        self.thread.join(timeout=3)
        self.folder.cleanup()

    def _post_raw(self, headers: bytes, body: bytes, close_write: bool = False) -> bytes:
        connection = socket.create_connection((self.host, self.port), timeout=3)
        try:
            request = (
                b"POST /api/knowledge/preview HTTP/1.1\r\n"
                + b"Host: localhost\r\n"
                + headers
                + b"\r\n"
                + body
            )
            connection.sendall(request)
            if close_write:
                connection.shutdown(socket.SHUT_WR)
            return _read_response(connection)
        finally:
            connection.close()

    @staticmethod
    def _status_and_json(response: bytes):
        head, body = response.split(b"\r\n\r\n", 1)
        status = int(head.split(b"\r\n", 1)[0].split()[1])
        return status, json.loads(body.decode("utf-8"))

    def test_close_delimited_body_without_content_length(self):
        payload = b'{"filename":"unframed.md","text":"close delimited body"}'
        started = time.monotonic()
        response = self._post_raw(
            b"Content-Type: application/json\r\nConnection: keep-alive\r\n",
            payload,
        )
        elapsed = time.monotonic() - started
        status, result = self._status_and_json(response)
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "awaiting_review")
        # The Vercel fallback waits only for a bounded idle period when the
        # proxy keeps the upstream connection open.
        self.assertLess(elapsed, 4)

    def test_chunked_body_without_content_length(self):
        payload = b'{"filename":"chunked.md","text":"chunked body"}'
        pieces = (payload[:9], payload[9:])
        wire = b"".join(
            ("%X\r\n" % len(piece)).encode("ascii") + piece + b"\r\n"
            for piece in pieces
        ) + b"0\r\n\r\n"
        response = self._post_raw(
            b"Content-Type: application/json\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"Connection: close\r\n",
            wire,
        )
        status, result = self._status_and_json(response)
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "awaiting_review")

    def test_missing_body_keeps_length_required_error(self):
        response = self._post_raw(
            b"Content-Type: application/json\r\nConnection: close\r\n",
            b"",
            close_write=True,
        )
        status, result = self._status_and_json(response)
        self.assertEqual(status, 411)
        self.assertEqual(result["error"]["code"], "length_required")

    def test_unknown_length_reader_enforces_limit(self):
        handler_type = make_handler(Orchestrator(), directory=self.root, vault_root=self.root)
        handler = handler_type.__new__(handler_type)
        handler.headers = Message()
        handler.rfile = BytesIO(b"12345")
        handler.connection = _FakeConnection()
        handler.close_connection = False
        with self.assertRaises(RequestError) as raised:
            handler._read_request_body(allow_empty=False, max_bytes=4)
        self.assertEqual(getattr(raised.exception, "code", None), "body_too_large")


class _FakeConnection:
    def __init__(self):
        self.timeout = None

    def gettimeout(self):
        return self.timeout

    def settimeout(self, value):
        self.timeout = value


if __name__ == "__main__":
    unittest.main()
