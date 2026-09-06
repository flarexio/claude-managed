import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class Receiver(BaseHTTPRequestHandler):
    body = None
    event = threading.Event()

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        type(self).body = self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(202)
        self.end_headers()
        type(self).event.set()

    def log_message(self, format, *args):
        pass


class EndToEndCliTests(unittest.TestCase):
    def test_init_doctor_hook_and_fake_claude_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            transcripts = root / "transcripts"
            fakebin = root / "bin"
            projects.mkdir()
            transcripts.mkdir()
            fakebin.mkdir()
            args_file = root / "claude-args.json"
            fake_claude = fakebin / "claude"
            fake_claude.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "if sys.argv[1:] == ['--version']:\n"
                "    print('2.1.198 (Claude Code)')\n"
                "    raise SystemExit(0)\n"
                "open(os.environ['FAKE_CLAUDE_ARGS'], 'w').write(json.dumps(sys.argv[1:]))\n"
            )
            fake_claude.chmod(0o755)

            server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            env = os.environ.copy()
            env["CLAUDE_MANAGED_HOME"] = str(root / "config")
            env["FAKE_CLAUDE_ARGS"] = str(args_file)
            env["PATH"] = str(fakebin) + os.pathsep + env.get("PATH", "")
            base = [sys.executable, "-m", "claude_managed"]
            try:
                subprocess.run(
                    base
                    + [
                        "init",
                        "--webhook-url",
                        f"http://127.0.0.1:{server.server_port}/events",
                        "--cwd-root",
                        str(projects),
                        "--transcript-root",
                        str(transcripts),
                    ],
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                doctor = subprocess.run(
                    base + ["doctor"],
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                self.assertIn("OK   webhook", doctor.stdout)

                payload = {
                    "hook_event_name": "Stop",
                    "cwd": str(projects / "repo"),
                    "transcript_path": str(transcripts / "session.jsonl"),
                }
                raw = json.dumps(payload).encode()
                subprocess.run(base + ["hook-forward"], env=env, input=raw, check=True)
                self.assertTrue(Receiver.event.wait(2))
                self.assertEqual(Receiver.body, raw)

                subprocess.run(
                    base + ["resume", "literal-session-id", "--", "--model", "sonnet"],
                    env=env,
                    check=True,
                )
                argv = json.loads(args_file.read_text())
                self.assertEqual(argv[0:2], ["--resume", "literal-session-id"])
                self.assertEqual(argv[-2:], ["--model", "sonnet"])
                self.assertIn("--settings", argv)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
