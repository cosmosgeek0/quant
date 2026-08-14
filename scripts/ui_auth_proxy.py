#!/usr/bin/env python3
"""Basic-auth reverse proxy for the read-only dashboard. Stdlib only."""

import argparse
import base64
import binascii
import hashlib
import hmac
import http.client
import io
import json
import re
import socket
import sys
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


DEFAULT_LISTEN = "127.0.0.1:8898"
DEFAULT_UPSTREAM = "http://127.0.0.1:8899"
UPSTREAM_TIMEOUT_SECONDS = 5
STREAM_CHUNK_BYTES = 64 * 1024
AUTH_REALM = "read-only-dashboard"
SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")


class ConfigurationError(ValueError):
    """Raised when a startup value cannot be used safely."""


@dataclass(frozen=True)
class Credentials:
    username: bytes
    password_sha256: bytes


@dataclass(frozen=True)
class Upstream:
    scheme: str
    host: str
    port: int

    def connect(self, timeout):
        connection_type = (http.client.HTTPSConnection
                           if self.scheme == "https"
                           else http.client.HTTPConnection)
        return connection_type(self.host, self.port, timeout=timeout)


def load_credentials(path):
    """Load exactly one ``user:sha256hex`` record from *path*."""
    try:
        raw = Path(path).read_bytes()
    except OSError as error:
        raise ConfigurationError("cannot read credentials file") from error
    if len(raw) > 4096:
        raise ConfigurationError("credentials file is too large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ConfigurationError("credentials file is not UTF-8") from error
    lines = text.splitlines()
    if len(lines) != 1:
        raise ConfigurationError("credentials file must contain one line")
    username, separator, digest = lines[0].partition(":")
    if (not separator or not username or username.strip() != username
            or not SHA256_RE.fullmatch(digest)):
        raise ConfigurationError("credentials file has an invalid record")
    return Credentials(
        username=username.encode("utf-8"),
        password_sha256=digest.lower().encode("ascii"),
    )


def parse_listen(value):
    """Return a validated ``(host, port)`` pair, including bracketed IPv6."""
    if value.startswith("["):
        closing = value.find("]")
        if closing < 0 or value[closing + 1:closing + 2] != ":":
            raise ConfigurationError("listen must be HOST:PORT")
        host = value[1:closing]
        port_text = value[closing + 2:]
    else:
        host, separator, port_text = value.rpartition(":")
        if not separator:
            raise ConfigurationError("listen must be HOST:PORT")
    if not host or not port_text:
        raise ConfigurationError("listen must be HOST:PORT")
    try:
        port = int(port_text, 10)
    except ValueError as error:
        raise ConfigurationError("listen port must be an integer") from error
    if not 0 <= port <= 65535:
        raise ConfigurationError("listen port is out of range")
    return host, port


def parse_upstream(value):
    """Accept only a fixed HTTP(S) origin, without userinfo or a base path."""
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConfigurationError("upstream must be an HTTP(S) origin")
    if (parsed.username is not None or parsed.password is not None
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise ConfigurationError("upstream must not contain userinfo or a path")
    try:
        port = parsed.port
    except ValueError as error:
        raise ConfigurationError("upstream port is invalid") from error
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return Upstream(parsed.scheme, parsed.hostname, port)


def _authorization_is_valid(headers, credentials):
    values = headers.get_all("Authorization", [])
    if len(values) != 1:
        return False
    parts = values[0].split()
    if len(parts) != 2 or parts[0].lower() != "basic":
        return False
    try:
        decoded = base64.b64decode(parts[1].encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError):
        return False
    username, separator, password = decoded.partition(b":")
    if not separator:
        return False
    presented_digest = hashlib.sha256(password).hexdigest().encode("ascii")
    username_matches = hmac.compare_digest(username, credentials.username)
    password_matches = hmac.compare_digest(
        presented_digest, credentials.password_sha256)
    return bool(username_matches & password_matches)


def _target_is_safe(target):
    if (not target.startswith("/") or target.startswith("//")
            or any(ord(character) <= 32 or ord(character) == 127
                   for character in target)):
        return False
    candidate = target
    for _ in range(8):
        if ".." in candidate:
            return False
        try:
            decoded = unquote(candidate, errors="strict")
        except UnicodeError:
            return False
        if decoded == candidate:
            return True
        candidate = decoded
    return False


class ProxyHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, credentials, upstream, log_stream,
                 upstream_timeout):
        self.credentials = credentials
        self.upstream = upstream
        self.log_stream = log_stream
        self.upstream_timeout = upstream_timeout
        self._log_lock = threading.Lock()
        super().__init__(address, handler)

    def write_request_log(self, path, status):
        timestamp = datetime.now(timezone.utc).isoformat(
            timespec="seconds").replace("+00:00", "Z")
        safe_path = json.dumps(path, ensure_ascii=True)
        try:
            with self._log_lock:
                print("ts=%s path=%s status=%d" % (
                    timestamp, safe_path, status),
                      file=self.log_stream, flush=True)
        except (OSError, ValueError):
            pass

    def handle_error(self, request, client_address):
        """Do not expose request-handler tracebacks."""


class IPv6ProxyHTTPServer(ProxyHTTPServer):
    address_family = socket.AF_INET6


class ProxyHandler(BaseHTTPRequestHandler):
    server_version = "DashboardAuthProxy/1"
    sys_version = ""

    def log_message(self, format_string, *args):
        """Suppress BaseHTTPRequestHandler's duplicate request log."""

    def __getattr__(self, name):
        if name.startswith("do_"):
            return self._dispatch
        raise AttributeError(name)

    def do_GET(self):
        self._dispatch()

    def do_HEAD(self):
        self._dispatch()

    def _dispatch(self):
        request_words = self.requestline.split()
        target = request_words[1] if len(request_words) >= 2 else self.path
        status = 500
        self.close_connection = True
        self._response_started = False
        try:
            if not _authorization_is_valid(
                    self.headers, self.server.credentials):
                status = 401
                self._plain_response(
                    status, b"authentication required\n",
                    (("WWW-Authenticate",
                      'Basic realm="%s", charset="UTF-8"' % AUTH_REALM),))
            elif self.command not in {"GET", "HEAD"}:
                status = 405
                self._plain_response(
                    status, b"method not allowed\n", (("Allow", "GET, HEAD"),))
            elif not _target_is_safe(target):
                status = 400
                self._plain_response(status, b"bad request path\n")
            else:
                status = self._proxy_request(target)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        except Exception:
            status = 500
            if not self._response_started:
                try:
                    self._plain_response(status, b"internal proxy error\n")
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
        finally:
            self.server.write_request_log(target, status)

    def _plain_response(self, status, body, extra_headers=()):
        self._response_started = True
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for name, value in extra_headers:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _proxy_request(self, target):
        connection = self.server.upstream.connect(
            self.server.upstream_timeout)
        upstream_status = 502
        try:
            connection.request(
                self.command, target, headers={"Connection": "close"})
            response = connection.getresponse()
            upstream_status = response.status
            content_type = response.getheader("Content-Type")
            content_length = response.getheader("Content-Length")
            transfer_encoding = response.getheader("Transfer-Encoding")

            self._response_started = True
            self.send_response(upstream_status)
            if content_type is not None:
                self.send_header("Content-Type", content_type)
            if transfer_encoding is None and _valid_content_length(
                    content_length):
                self.send_header("Content-Length", content_length)
            self.send_header("Connection", "close")
            self.end_headers()

            if self.command == "GET":
                while True:
                    chunk = response.read(STREAM_CHUNK_BYTES)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            return upstream_status
        except (OSError, http.client.HTTPException, ValueError):
            if not self._response_started:
                self._plain_response(502, b"bad gateway\n")
                return 502
            return upstream_status
        finally:
            connection.close()


def _valid_content_length(value):
    if value is None or not value.isdigit():
        return False
    try:
        return int(value, 10) >= 0
    except ValueError:
        return False


def make_proxy_server(credentials, upstream=DEFAULT_UPSTREAM,
                      listen_host="127.0.0.1", listen_port=8898,
                      log_stream=None,
                      upstream_timeout=UPSTREAM_TIMEOUT_SECONDS):
    parsed_upstream = (parse_upstream(upstream)
                       if isinstance(upstream, str) else upstream)
    server_type = IPv6ProxyHTTPServer if ":" in listen_host else ProxyHTTPServer
    return server_type(
        (listen_host, listen_port), ProxyHandler, credentials,
        parsed_upstream, sys.stdout if log_stream is None else log_stream,
        upstream_timeout)


class _SelftestUpstream(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, body):
        self.body = body
        self.hits = 0
        self.paths = []
        self.authorization_headers = []
        self._hits_lock = threading.Lock()
        super().__init__(address, handler)

    def record(self, handler):
        with self._hits_lock:
            self.hits += 1
            self.paths.append(handler.path)
            self.authorization_headers.append(
                handler.headers.get("Authorization"))

    def handle_error(self, request, client_address):
        pass


class _SelftestUpstreamHandler(BaseHTTPRequestHandler):
    def log_message(self, format_string, *args):
        pass

    def do_GET(self):
        self.server.record(self)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=selftest")
        self.send_header("Content-Length", str(len(self.server.body)))
        self.end_headers()
        self.wfile.write(self.server.body)

    def do_HEAD(self):
        self.server.record(self)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=selftest")
        self.send_header("Content-Length", str(len(self.server.body)))
        self.end_headers()


def make_selftest_upstream(body=b"read-only dashboard\n"):
    return _SelftestUpstream(
        ("127.0.0.1", 0), _SelftestUpstreamHandler, body)


def start_server_thread(server):
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    return thread


def stop_server_thread(server, thread):
    server.shutdown()
    thread.join(5)
    server.server_close()


def basic_authorization(username, password):
    raw = (username + ":" + password).encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def selftest_request(port, method, path, authorization=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {}
    if authorization is not None:
        headers["Authorization"] = authorization
    try:
        connection.request(method, path, headers=headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def run_once_selftest(log_stream=None):
    username = "selftest-reader"
    password = "selftest-password"
    wrong_password = "selftest-wrong"
    output = io.StringIO() if log_stream is None else log_stream
    upstream = proxy = None
    upstream_thread = proxy_thread = None
    upstream_stopped = False
    try:
        with tempfile.TemporaryDirectory() as directory:
            digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
            credential_path = Path(directory) / "credentials"
            credential_path.write_text(
                "%s:%s\n" % (username, digest), encoding="utf-8")
            credentials = load_credentials(credential_path)

            upstream = make_selftest_upstream()
            upstream_thread = start_server_thread(upstream)
            upstream_port = upstream.server_address[1]
            proxy = make_proxy_server(
                credentials,
                upstream="http://127.0.0.1:%d" % upstream_port,
                listen_port=0,
                log_stream=output,
                upstream_timeout=1)
            proxy_thread = start_server_thread(proxy)
            proxy_port = proxy.server_address[1]
            valid = basic_authorization(username, password)
            wrong = basic_authorization(username, wrong_password)

            status, headers, body = selftest_request(
                proxy_port, "GET", "/dashboard?mode=selftest", valid)
            checks = [
                status == 200,
                body == b"read-only dashboard\n",
                headers.get("Content-Type") ==
                "text/plain; charset=selftest",
                upstream.paths == ["/dashboard?mode=selftest"],
                upstream.authorization_headers == [None],
            ]

            hit_count = upstream.hits
            status, headers, _ = selftest_request(
                proxy_port, "GET", "/dashboard", wrong)
            checks.extend((
                status == 401,
                "Basic" in headers.get("WWW-Authenticate", ""),
                upstream.hits == hit_count,
            ))

            status, _, _ = selftest_request(
                proxy_port, "POST", "/dashboard", valid)
            checks.extend((status == 405, upstream.hits == hit_count))

            status, _, _ = selftest_request(
                proxy_port, "GET", "/../private", valid)
            checks.extend((status == 400, upstream.hits == hit_count))

            stop_server_thread(upstream, upstream_thread)
            upstream_stopped = True
            status, headers, body = selftest_request(
                proxy_port, "GET", "/dashboard", valid)
            checks.extend((
                status == 502,
                headers.get("Content-Type", "").startswith("text/plain"),
                body == b"bad gateway\n",
            ))

            logs = output.getvalue() if hasattr(output, "getvalue") else ""
            if logs:
                checks.extend((
                    username not in logs,
                    password not in logs,
                    wrong_password not in logs,
                    digest not in logs,
                ))
            return 0 if all(checks) else 1
    except Exception:
        return 1
    finally:
        if proxy is not None and proxy_thread is not None:
            stop_server_thread(proxy, proxy_thread)
        if (upstream is not None and upstream_thread is not None
                and not upstream_stopped):
            stop_server_thread(upstream, upstream_thread)


def build_argument_parser():
    parser = argparse.ArgumentParser(
        description="Basic-auth proxy for the read-only dashboard")
    parser.add_argument("--listen", default=DEFAULT_LISTEN,
                        metavar="HOST:PORT")
    parser.add_argument("--upstream", default=DEFAULT_UPSTREAM,
                        metavar="URL")
    parser.add_argument("--credentials-file", metavar="PATH")
    parser.add_argument("--once-selftest", action="store_true")
    return parser


def main(argv=None):
    parser = build_argument_parser()
    arguments = parser.parse_args(argv)
    if arguments.once_selftest:
        return run_once_selftest(log_stream=sys.stdout)
    if not arguments.credentials_file:
        parser.error("--credentials-file is required")
    try:
        listen_host, listen_port = parse_listen(arguments.listen)
        upstream = parse_upstream(arguments.upstream)
        credentials = load_credentials(arguments.credentials_file)
        server = make_proxy_server(
            credentials, upstream=upstream,
            listen_host=listen_host, listen_port=listen_port)
    except (ConfigurationError, OSError) as error:
        parser.error(str(error))
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
