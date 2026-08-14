#!/usr/bin/env python3
"""Basic-auth gate for the read-only dashboard: GET/HEAD only, one upstream.

Credentials file: one line "user:sha256hex(password)". The gate never logs
credentials and never follows redirects; upstream host is fixed at start.
"""
import base64
import hashlib
import hmac
import sys
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LISTEN = ("127.0.0.1", 8080)
UPSTREAM = "http://127.0.0.1:8899"
CRED_PATH = "/etc/quant-ui/credentials"


def load_cred():
    with open(CRED_PATH, encoding="utf-8") as handle:
        user, sha = handle.read().strip().split(":", 1)
    return user, sha.lower()


CRED_USER, CRED_SHA = load_cred()


class Gate(BaseHTTPRequestHandler):
    server_version = "quant-ui-gate"
    protocol_version = "HTTP/1.1"

    def _authed(self):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(header[6:], validate=True).decode("utf-8")
            user, password = raw.split(":", 1)
        except Exception:
            return False
        digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return user == CRED_USER and hmac.compare_digest(digest, CRED_SHA)

    def _deny(self):
        body = b"login required"
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="quant"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve(self, head_only):
        if not self._authed():
            self._deny()
            return
        if ".." in self.path:
            self.send_error(400)
            return
        try:
            with urllib.request.urlopen(UPSTREAM + self.path, timeout=10) as up:
                body = up.read()
                ctype = up.headers.get("Content-Type", "text/plain")
        except (urllib.error.URLError, OSError):
            body, ctype = b"upstream unavailable", "text/plain"
            self.send_response(502)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not head_only:
                self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def do_GET(self):
        self._serve(False)

    def do_HEAD(self):
        self._serve(True)

    def do_POST(self):
        self.send_error(405)

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_POST

    def log_message(self, fmt, *args):
        sys.stderr.write("gate | %s | %s\n" % (self.path, args[-1] if args else ""))


def main():
    host, port = LISTEN
    if len(sys.argv) > 1:
        port = int(sys.argv[1])
    ThreadingHTTPServer((host, port), Gate).serve_forever()


if __name__ == "__main__":
    main()
