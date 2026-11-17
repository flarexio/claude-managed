"""Shared helpers: hermetic environments, loopback receivers, fake Claude."""

from __future__ import annotations

import hashlib
import hmac
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LAUNCHER = [sys.executable, "-I", "-m", "claude_managed"]


def clean_env(root: Path, **extra: str) -> dict[str, str]:
    """A minimal environment with no inherited HERMES_*/CLAUDE_MANAGED_* state.

    HOME points into the temporary tree so fallback error logs never touch the
    real home directory.
    """
    bin_dirs = [str(root / "bin"), str(Path(sys.executable).parent), "/usr/bin", "/bin"]
    env = {
        "PATH": os.pathsep.join(bin_dirs),
        "HOME": str(root / "home"),
        "LANG": "C.UTF-8",
    }
    env.update(extra)
    return env


def hermes_env(owner: str) -> dict[str, str]:
    return {
        "HERMES_SESSION_ID": f"session-{owner}",
        "HERMES_SESSION_PLATFORM": "discord",
        "HERMES_SESSION_CHAT_ID": f"chat-{owner}",
        "HERMES_SESSION_THREAD_ID": f"thread-{owner}",
        "HERMES_SESSION_PROFILE": "default",
    }


class Receiver:
    """A real loopback HTTP receiver that records every POST."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str], bytes]] = []
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                code = 200 if self.path == "/health" else 404
                self.send_response(code)
                self.end_headers()
                if code == 200:
                    self.wfile.write(b'{"status":"ok"}')

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                with outer.lock:
                    outer.requests.append((self.path, dict(self.headers), body))
                self.send_response(202)
                self.end_headers()

            def log_message(self, format, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def url(self, path: str = "/webhooks/route") -> str:
        return f"http://127.0.0.1:{self.server.server_port}{path}"

    def bodies(self) -> list[bytes]:
        with self.lock:
            return [body for _, _, body in self.requests]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def verify_signature(headers: dict[str, str], body: bytes, secret: str) -> bool:
    lowered = {k.lower(): v for k, v in headers.items()}
    timestamp = lowered["x-webhook-timestamp"]
    expected = hmac.new(
        secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(lowered["x-webhook-signature-v2"], expected)


FAKE_CLAUDE = r"""
import json, os, subprocess, sys, time

if sys.argv[1:] == ["--version"]:
    print("2.1.198 (Claude Code)")
    raise SystemExit(0)
argv = sys.argv[1:]
with open(os.environ["FAKE_CLAUDE_ARGS"], "w") as handle:
    json.dump(argv, handle)
go = os.environ.get("FAKE_CLAUDE_GO")
if go:
    deadline = time.time() + 20
    while not os.path.exists(go):
        if time.time() > deadline:
            raise SystemExit("timed out waiting for go file")
        time.sleep(0.02)
settings = json.load(open(argv[argv.index("--settings") + 1]))
hook_env = dict(os.environ)
hook_env.update(json.loads(os.environ.get("FAKE_HOOK_ENV", "{}")))
for name in os.environ.get("FAKE_HOOK_UNSET", "").split(","):
    hook_env.pop(name, None)
sent = []
for event in filter(None, os.environ.get("FAKE_CLAUDE_FIRE", "").split(",")):
    hook = settings["hooks"][event][0]["hooks"][0]
    body = json.dumps({
        "hook_event_name": event,
        "session_id": os.environ["FAKE_CLAUDE_SESSION"],
        "cwd": os.getcwd(),
        "transcript_path": os.environ["FAKE_CLAUDE_TRANSCRIPT"],
    }).encode()
    result = subprocess.run([hook["command"], *hook["args"]], input=body, env=hook_env)
    sent.append({"event": event, "body": body.decode(), "rc": result.returncode})
with open(os.environ["FAKE_CLAUDE_ARGS"] + ".sent", "w") as handle:
    json.dump(sent, handle)
"""


def install_fake_claude(root: Path) -> Path:
    fakebin = root / "bin"
    fakebin.mkdir(exist_ok=True)
    path = fakebin / "claude"
    path.write_text(f"#!{sys.executable}\n" + FAKE_CLAUDE)
    path.chmod(0o755)
    return path


def write_secret(path: Path, value: str) -> Path:
    path.write_text(value + "\n")
    path.chmod(0o600)
    return path
