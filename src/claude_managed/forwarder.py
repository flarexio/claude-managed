from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
import urllib.request
import uuid
from pathlib import Path

from .config import Config, load_config, secure_file
from .launch import ContextError, hermes_owner, load_context, secret_fingerprint

MAX_BODY_BYTES = 1_048_576


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _within(path_value: str, roots: tuple[Path, ...]) -> bool:
    candidate = Path(path_value).expanduser().resolve(strict=False)
    return any(candidate == root or candidate.is_relative_to(root) for root in roots)


def validate_payload(payload: object, config: Config) -> dict:
    if not isinstance(payload, dict):
        raise TypeError("hook payload is not a JSON object")
    cwd = payload.get("cwd")
    transcript = payload.get("transcript_path")
    if not isinstance(cwd, str) or not _within(cwd, config.allowed_cwd_roots):
        raise ValueError("payload cwd is outside allowed_cwd_roots")
    if not isinstance(transcript, str) or not _within(
        transcript, config.allowed_transcript_roots
    ):
        raise ValueError("payload transcript_path is outside allowed_transcript_roots")
    return payload


def log_error(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    os.chmod(path, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(f"{timestamp} {message}\n")


def read_secret(path: Path) -> bytes:
    ok, detail = secure_file(path)
    if not ok:
        raise PermissionError(f"unsafe secret file: {detail}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        secret = handle.read(MAX_BODY_BYTES).strip().encode()
    if len(secret) < 32:
        raise ValueError("secret must contain at least 32 characters")
    return secret


def fallback_error_log() -> Path:
    return Path.home() / ".local/state/claude-managed/errors.log"


def forward(
    raw_body: bytes,
    config: Config,
    *,
    launch_id: str | None = None,
    expected_fingerprint: str | None = None,
) -> int:
    if len(raw_body) > MAX_BODY_BYTES:
        raise ValueError(f"hook payload exceeds {MAX_BODY_BYTES} bytes")
    payload = validate_payload(json.loads(raw_body), config)
    secret = read_secret(config.secret_file)
    if expected_fingerprint is not None and not hmac.compare_digest(
        secret_fingerprint(secret), expected_fingerprint
    ):
        raise ContextError("secret changed since launch; refusing to sign")
    timestamp = str(int(time.time()))
    signature = hmac.new(
        secret, timestamp.encode() + b"." + raw_body, hashlib.sha256
    ).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Timestamp": timestamp,
        "X-Webhook-Signature-V2": signature,
        "X-Request-ID": str(uuid.uuid4()),
        "X-Webhook-Event": str(payload.get("hook_event_name", "claude-code")),
    }
    if launch_id is not None:
        # Informational and unsigned: routing isolation comes from the pinned
        # per-launch URL and secret, never from this header.
        headers["X-Claude-Managed-Launch-ID"] = launch_id
    request = urllib.request.Request(
        config.webhook_url, data=raw_body, method="POST", headers=headers
    )
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(request, timeout=config.timeout_seconds) as response:
        if response.status >= 300:
            raise RuntimeError(f"webhook returned HTTP {response.status}")
    return 0


def _drop(log_path: Path | None, message: str) -> int:
    try:
        log_error(log_path or fallback_error_log(), message)
    except Exception:  # noqa: BLE001, S110 - nowhere else is safe to report a log failure
        pass
    return 0  # A notification failure must never block Claude Code.


def hook_main(context_path: str | None = None) -> int:
    """Hook entry point. Always exits zero; failures are logged and dropped."""
    log_path: Path | None = None
    try:
        if context_path is not None:
            # Pinned launch: consult only the explicit context, never ambient
            # CLAUDE_MANAGED_HOME/XDG configuration or a shared route.
            context = load_context(context_path)
            log_path = context.config.error_log
            raw_body = sys.stdin.buffer.read(MAX_BODY_BYTES + 1)
            return forward(
                raw_body,
                context.config,
                launch_id=context.launch_id,
                expected_fingerprint=context.secret_fingerprint,
            )
        # Legacy unbound Hook (settings generated before per-launch contexts).
        if hermes_owner(os.environ) is not None:
            raise ContextError(
                "unbound legacy hook inside a Hermes session; dropped instead of "
                "using the shared route (relaunch with 'claude-managed run "
                "--webhook-url ...')"
            )
        config = load_config()
        log_path = config.error_log
        raw_body = sys.stdin.buffer.read(MAX_BODY_BYTES + 1)
        return forward(raw_body, config)
    except Exception as exc:  # noqa: BLE001 - a Hook must never break Claude Code
        return _drop(log_path, f"{type(exc).__name__}: {exc}")
