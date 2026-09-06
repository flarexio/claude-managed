import hashlib
import hmac
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from claude_managed.config import Config
from claude_managed.forwarder import MAX_BODY_BYTES, forward, validate_payload


class Handler(BaseHTTPRequestHandler):
    body = b""
    headers_seen = None

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        type(self).body = self.rfile.read(length)
        type(self).headers_seen = self.headers
        self.send_response(202)
        self.end_headers()
        self.wfile.write(b"accepted")

    def log_message(self, format, *args):
        pass


class ForwarderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.projects = self.root / "projects"
        self.transcripts = self.root / "transcripts"
        self.projects.mkdir()
        self.transcripts.mkdir()
        self.secret_value = "s" * 48
        self.secret = self.root / "secret"
        self.secret.write_text(self.secret_value)
        self.secret.chmod(0o600)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.config = Config(
            webhook_url=f"http://127.0.0.1:{self.server.server_port}/events",
            secret_file=self.secret,
            settings_file=self.root / "settings.json",
            error_log=self.root / "errors.log",
            timeout_seconds=2,
            allowed_cwd_roots=(self.projects.resolve(),),
            allowed_transcript_roots=(self.transcripts.resolve(),),
        )

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def test_forwards_original_body_with_valid_v2_signature(self):
        payload = {
            "hook_event_name": "Stop",
            "cwd": str(self.projects / "repo"),
            "transcript_path": str(self.transcripts / "session.jsonl"),
        }
        raw = json.dumps(payload, separators=(",", ":")).encode()
        self.assertEqual(forward(raw, self.config), 0)
        self.assertEqual(Handler.body, raw)
        headers = Handler.headers_seen
        timestamp = headers["X-Webhook-Timestamp"]
        expected = hmac.new(
            self.secret_value.encode(), timestamp.encode() + b"." + raw, hashlib.sha256
        ).hexdigest()
        self.assertTrue(
            hmac.compare_digest(headers["X-Webhook-Signature-V2"], expected)
        )
        self.assertEqual(headers["X-Webhook-Event"], "Stop")
        self.assertTrue(headers["X-Request-ID"])

    def test_rejects_cwd_outside_allowlist(self):
        payload = {
            "cwd": "/tmp/untrusted",
            "transcript_path": str(self.transcripts / "session.jsonl"),
        }
        with self.assertRaisesRegex(ValueError, "cwd"):
            validate_payload(payload, self.config)

    def test_rejects_transcript_outside_allowlist(self):
        payload = {
            "cwd": str(self.projects / "repo"),
            "transcript_path": "/tmp/untrusted.jsonl",
        }
        with self.assertRaisesRegex(ValueError, "transcript"):
            validate_payload(payload, self.config)

    def test_rejects_oversized_body_before_parsing(self):
        with self.assertRaisesRegex(ValueError, "exceeds"):
            forward(b"x" * (MAX_BODY_BYTES + 1), self.config)

    def test_rejects_secret_with_group_access(self):
        self.secret.chmod(0o640)
        payload = {
            "cwd": str(self.projects / "repo"),
            "transcript_path": str(self.transcripts / "session.jsonl"),
        }
        with self.assertRaisesRegex(PermissionError, "unsafe secret"):
            forward(json.dumps(payload).encode(), self.config)


if __name__ == "__main__":
    unittest.main()
