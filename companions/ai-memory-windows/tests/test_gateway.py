"""Attempt unauthorized forwarding and credential-bearing redirects locally."""
from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

OPS = Path(__file__).resolve().parent.parent / "ops"
sys.path.insert(0, str(OPS))
import relay_diagnostics_gateway as gateway


@contextmanager
def running(server):
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.server.received.append({
            "path": self.path, "auth": self.headers.get("Authorization"),
            "body": self.rfile.read(int(self.headers["Content-Length"])),
        })
        self.send_response(self.server.reply_status)
        if self.server.redirect:
            self.send_header("Location", self.server.redirect)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"answer":"synthetic"}')


class GatewayBoundaryTests(unittest.TestCase):
    def test_auth_fixed_path_size_and_valid_control(self):
        self.exercise_boundary(200)

    def test_redirect_never_forwards_credentials_to_another_destination(self):
        self.exercise_boundary(302)

    def exercise_boundary(self, upstream_status):
        with tempfile.TemporaryDirectory() as temporary:
            ops = Path(temporary)
            key = "synthetic-fixture-only"
            (ops / "relay-api-key.txt").write_text(key)
            upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
            upstream.received, upstream.reply_status, upstream.redirect = [], upstream_status, None
            destination = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
            destination.received, destination.reply_status, destination.redirect = [], 200, None
            if upstream_status == 302:
                upstream.redirect = f"http://127.0.0.1:{destination.server_port}/stolen"
            server = gateway.Gateway(("127.0.0.1", 0), gateway.Handler)
            server.ops, server.data = ops, ops
            server.upstream = f"http://127.0.0.1:{upstream.server_port}"
            def send(path="/v1/chat/completions", auth="Bearer " + key, length=None):
                body = b'{"messages":[{"role":"user","content":"synthetic fixture"}]}'
                connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                headers = {"Authorization": auth, "Content-Type": "application/json"}
                if length is not None:
                    headers["Content-Length"] = str(length)
                connection.request("POST", path, body, headers)
                response = connection.getresponse()
                status, payload = response.status, response.read()
                connection.close()
                return status, payload
            with running(upstream), running(destination), running(server), patch.object(gateway.diagnostic, "upstream_base", return_value=server.upstream):
                self.assertEqual(send(auth="Bearer incorrect")[0], 401)
                self.assertEqual(send(path="/arbitrary")[0], 404)
                self.assertEqual(send(length=33 * 1024 * 1024)[0], 413)
                self.assertEqual(upstream.received, [])
                status, payload = send()
                self.assertEqual(status, upstream_status)
                self.assertEqual(len(upstream.received), 1)
                self.assertEqual(upstream.received[0]["path"], "/chat/completions")
                self.assertEqual(upstream.received[0]["auth"], "Bearer " + key)
                self.assertEqual(destination.received, [])
                self.assertNotIn(key.encode(), payload)
                with patch.object(gateway.diagnostic, "upstream_base", return_value="https://changed.invalid/v1"):
                    self.assertEqual(send()[0], 503)
                self.assertEqual(len(upstream.received), 1)


if __name__ == "__main__":
    unittest.main()
