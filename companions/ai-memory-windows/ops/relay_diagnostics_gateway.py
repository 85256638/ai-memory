"""Fixed-route loopback diagnostics for the user's existing Chat relay.

Forward request/response bytes without rewriting, retries, or moderation bypass.
Only task identities and bounded sanitized failure reasons are persisted.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import msvcrt
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request

import llm_task_diagnostics as diagnostic

OPS = Path(__file__).resolve().parent
PORT = 49376
IDENTITY = "ai-memory-local-relay-diagnostics-v1"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Gateway(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def handle_error(self, request, client_address):
        # Default HTTPServer tracebacks can contain upstream data.
        pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def reply(self, status, value):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            self.reply(200, {"service": IDENTITY, "pid": os.getpid(),
                            "route_digest": hashlib.sha256(
                                self.server.upstream.encode()).hexdigest()})
        else:
            self.reply(404, {"error": "Fixed Chat relay route only"})

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.reply(404, {"error": "Fixed Chat relay route only"})
            return
        try:
            if self.server.upstream != diagnostic.upstream_base():
                raise ValueError("route_changed")
        except (OSError, ValueError, KeyError):
            self.reply(503, {"error": "Configured relay changed; restart the local gateway first"})
            return
        try:
            key = (self.server.ops / "relay-api-key.txt").read_text("utf-8-sig").strip()
            valid = bool(key) and hmac.compare_digest(
                self.headers.get("Authorization", "").encode(),
                ("Bearer " + key).encode())
        except OSError:
            valid = False
        if not valid:
            self.reply(401, {"error": "Local relay authentication failed"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > 32 * 1024 * 1024 or self.headers.get("Transfer-Encoding"):
            self.reply(413, {"error": "A bounded Content-Length request is required"})
            return
        self.connection.settimeout(360)
        body = self.rfile.read(length)
        if len(body) != length:
            self.reply(400, {"error": "Incomplete request"})
            return
        task = diagnostic.task_from_request(body, self.server.data)
        headers = {"Authorization": "Bearer " + key,
                   "Content-Type": self.headers.get("Content-Type", "application/json")}
        for name in ("User-Agent", "x-request-id", "x-opencode-session"):
            if self.headers.get(name):
                headers[name] = self.headers[name]
        request = urllib.request.Request(
            self.server.upstream + "/chat/completions", data=body, headers=headers)
        # This is a single fixed upstream. Never follow redirects with credentials.
        opener = urllib.request.build_opener(NoRedirect)
        try:
            try:
                response = opener.open(request, timeout=360)
            except urllib.error.HTTPError as error:
                response = error
        except (OSError, urllib.error.URLError):
            failure_body = json.dumps({"error": {
                "code": "local_transport_error",
                "message": "Local diagnostic relay could not reach the configured upstream",
            }}).encode()
            if task:
                diagnostic.store_failure(task, diagnostic.classify(502, failure_body), self.server.ops)
            self.reply(502, json.loads(failure_body))
            return
        sample = bytearray()
        client_open = True
        with response:
            status = response.code
            content_type = response.headers.get("Content-Type", "")
            self.send_response(status)
            for name in ("Content-Type", "Content-Length", "Content-Encoding",
                         "Retry-After", "x-request-id"):
                if response.headers.get(name):
                    self.send_header(name, response.headers[name])
            self.end_headers()
            while chunk := response.read1(65536):
                if len(sample) < 1024 * 1024:
                    sample.extend(chunk[:1024 * 1024 - len(sample)])
                if client_open:
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except (ConnectionError, OSError):
                        # Keep the provider's final error even if the caller left.
                        client_open = False
        failure = diagnostic.classify(status, bytes(sample), key, content_type)
        if task and failure:
            diagnostic.store_failure(task, failure, self.server.ops)


def health() -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{PORT}/healthz", timeout=1) as response:
            value = json.load(response)
            expected = hashlib.sha256(diagnostic.upstream_base().encode()).hexdigest()
            return value.get("service") == IDENTITY and value.get("route_digest") == expected
    except (OSError, ValueError, KeyError):
        return False


def ensure() -> None:
    if health():
        return
    # Native process creation escapes a Task Scheduler job's lifetime.
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    ps = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    commandline = f'"{pythonw}" "{Path(__file__).resolve()}" --serve'
    literal = commandline.replace("'", "''")
    script = (
        "$s=New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly "
        "-Property @{ShowWindow=[uint16]0}; "
        "$r=Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
        "-Arguments @{CommandLine='" + literal + "';ProcessStartupInformation=$s}; "
        "if ($r.ReturnValue -ne 0) { exit 1 }"
    )
    result = subprocess.run([str(ps), "-NoProfile", "-NonInteractive",
                             "-WindowStyle", "Hidden", "-Command", script],
                            capture_output=True, timeout=15,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError("diagnostic_relay_native_start_failed")
    until = time.monotonic() + 8
    while time.monotonic() < until:
        if health():
            return
        time.sleep(0.2)
    raise RuntimeError("diagnostic_relay_not_healthy_or_port_occupied")


def serve() -> None:
    with (OPS / "relay-diagnostics.lock").open("a+b") as lock:
        lock.seek(0)
        if not lock.read(1):
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return
        with Gateway(("127.0.0.1", PORT), Handler) as server:
            server.ops, server.data = OPS, OPS.parent
            server.upstream = diagnostic.upstream_base()
            server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--ensure", action="store_true")
    mode.add_argument("--serve", action="store_true")
    arguments = parser.parse_args()
    try:
        ensure() if arguments.ensure else serve()
    except Exception:
        # No raw exception output containing URLs, headers, keys, or content.
        sys.exit(1)
