"""Offline handler checks for scripts/ui_auth_proxy.py."""

import hashlib
import io
import sys
import tempfile
import threading
import unittest
from email.parser import BytesHeaderParser
from pathlib import Path
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import ui_auth_proxy as proxy  # noqa: E402


class FakeSocket:
    def __init__(self, request_bytes):
        self.reader = io.BytesIO(request_bytes)
        self.writer = io.BytesIO()

    def makefile(self, mode, buffering=None):
        if "r" not in mode:
            raise AssertionError("unexpected writable makefile")
        return self.reader

    def sendall(self, data):
        self.writer.write(data)


class FakeResponse:
    def __init__(self, body, content_type):
        self.status = 200
        self.body = io.BytesIO(body)
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
        }

    def getheader(self, name):
        return self.headers.get(name)

    def read(self, size=-1):
        return self.body.read(size)


class FakeConnection:
    def __init__(self, upstream):
        self.upstream = upstream

    def request(self, method, path, headers=None):
        self.upstream.requests.append((method, path, dict(headers or {})))
        if self.upstream.down:
            raise OSError("stub upstream is down")

    def getresponse(self):
        return FakeResponse(self.upstream.body, self.upstream.content_type)

    def close(self):
        pass


class FakeUpstream:
    def __init__(self, body=b"dashboard body\n",
                 content_type="text/plain; charset=selftest"):
        self.body = body
        self.content_type = content_type
        self.down = False
        self.connections = 0
        self.requests = []

    def connect(self, timeout):
        self.connections += 1
        return FakeConnection(self)


class FakeProxyServer:
    def __init__(self, credentials, upstream, log_stream):
        self.credentials = credentials
        self.upstream = upstream
        self.upstream_timeout = 1
        self.log_stream = log_stream
        self._log_lock = threading.Lock()

    write_request_log = proxy.ProxyHTTPServer.write_request_log


def parse_response(raw):
    header_block, separator, body = raw.partition(b"\r\n\r\n")
    if not separator:
        raise AssertionError("handler emitted no HTTP header block")
    status_line, _, header_lines = header_block.partition(b"\r\n")
    status = int(status_line.split(b" ", 2)[1])
    headers = BytesHeaderParser().parsebytes(header_lines + b"\r\n")
    return status, dict(headers.items()), body


class ProxyHarness(unittest.TestCase):
    username = "dashboard-reader"
    password = "correct-horse"
    wrong_password = "wrong-battery"

    def setUp(self):
        digest = hashlib.sha256(self.password.encode("utf-8")).hexdigest()
        self.digest = digest
        self.credentials = proxy.Credentials(
            self.username.encode("utf-8"), digest.encode("ascii"))
        self.logs = io.StringIO()
        self.upstream = FakeUpstream()
        self.server = FakeProxyServer(
            self.credentials, self.upstream, self.logs)

    def authorization(self, password=None):
        return proxy.basic_authorization(
            self.username, self.password if password is None else password)

    def request(self, method="GET", path="/dashboard", authorization=None):
        auth_value = (self.authorization()
                      if authorization is None else authorization)
        request_lines = [
            "%s %s HTTP/1.1" % (method, path),
            "Host: dashboard-proxy",
        ]
        if auth_value is not False:
            request_lines.append("Authorization: %s" % auth_value)
        request_bytes = ("\r\n".join(request_lines) + "\r\n\r\n").encode(
            "ascii")
        fake_socket = FakeSocket(request_bytes)
        proxy.ProxyHandler(fake_socket, ("127.0.0.1", 12345), self.server)
        return parse_response(fake_socket.writer.getvalue())

    def test_valid_login_proxies_content_and_verbatim_path(self):
        path = "/dashboard?view=read%20only&n=2"
        status, headers, body = self.request(path=path)

        self.assertEqual(status, 200)
        self.assertEqual(body, b"dashboard body\n")
        self.assertEqual(
            headers.get("Content-Type"), "text/plain; charset=selftest")
        self.assertEqual(
            self.upstream.requests,
            [("GET", path, {"Connection": "close"})])

    def test_head_proxies_headers_without_a_body(self):
        status, headers, body = self.request(method="HEAD")

        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(headers.get("Content-Length"),
                         str(len(b"dashboard body\n")))
        self.assertEqual(
            self.upstream.requests,
            [("HEAD", "/dashboard", {"Connection": "close"})])

    def test_wrong_password_is_401_without_upstream_contact(self):
        status, headers, body = self.request(
            authorization=self.authorization(self.wrong_password))

        self.assertEqual(status, 401)
        self.assertIn("Basic", headers.get("WWW-Authenticate", ""))
        self.assertEqual(body, b"authentication required\n")
        self.assertEqual(self.upstream.connections, 0)

    def test_post_is_405_without_upstream_contact(self):
        status, headers, body = self.request(method="POST")

        self.assertEqual(status, 405)
        self.assertEqual(headers.get("Allow"), "GET, HEAD")
        self.assertEqual(body, b"method not allowed\n")
        self.assertEqual(self.upstream.connections, 0)

    def test_upstream_down_is_plain_502(self):
        self.upstream.down = True
        status, headers, body = self.request()

        self.assertEqual(status, 502)
        self.assertTrue(headers.get("Content-Type", "").startswith("text/plain"))
        self.assertEqual(body, b"bad gateway\n")

    def test_dotdot_is_400_without_upstream_contact(self):
        for path in ("/../private", "/%2e%2e/private"):
            with self.subTest(path=path):
                status, _, body = self.request(path=path)
                self.assertEqual(status, 400)
                self.assertEqual(body, b"bad request path\n")
        self.assertEqual(self.upstream.connections, 0)

    def test_credentials_never_appear_in_logs(self):
        authorization = self.authorization()
        status, _, _ = self.request(authorization=authorization)
        self.assertEqual(status, 200)

        logs = self.logs.getvalue()
        self.assertNotIn(self.username, logs)
        self.assertNotIn(self.password, logs)
        self.assertNotIn(self.wrong_password, logs)
        self.assertNotIn(self.digest, logs)
        self.assertNotIn(authorization, logs)
        self.assertEqual(len(logs.splitlines()), 1)
        self.assertIn('path="/dashboard" status=200', logs)


class TestOnceSelftest(unittest.TestCase):
    def test_credentials_file_loads_one_sha256_record(self):
        password = "file-password"
        digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials"
            path.write_text("file-user:%s\n" % digest, encoding="utf-8")
            credentials = proxy.load_credentials(path)

        self.assertEqual(credentials.username, b"file-user")
        self.assertEqual(credentials.password_sha256, digest.encode("ascii"))

    def test_once_selftest_cli_selects_ephemeral_selftest(self):
        with mock.patch.object(
                proxy, "run_once_selftest", return_value=0) as selftest:
            self.assertEqual(proxy.main(["--once-selftest"]), 0)
        selftest.assert_called_once_with(log_stream=sys.stdout)


if __name__ == "__main__":
    unittest.main()
