"""Per-launch, immutable hook routing context.

Every ``run``/``resume`` pins its own route, secret reference, path policy, and
Claude settings into a private directory. The generated Hook command names
that directory's ``context.json`` explicitly, so a running Claude session never
re-reads shared, mutable configuration or ambient environment variables.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import Config, validate_timeout, validate_url

CONTEXT_VERSION = 1
CONTEXT_NAME = "context.json"
SETTINGS_NAME = "settings.json"
MAX_CONTEXT_BYTES = 65_536
FINGERPRINT_PREFIX = b"claude-managed/secret-fingerprint/v1:"

# Environment variables Hermes sets for processes launched from a conversation.
HERMES_OWNER_VARS = {
    "session_id": "HERMES_SESSION_ID",
    "platform": "HERMES_SESSION_PLATFORM",
    "chat_id": "HERMES_SESSION_CHAT_ID",
    "thread_id": "HERMES_SESSION_THREAD_ID",
    "profile": "HERMES_SESSION_PROFILE",
    "hermes_home": "HERMES_HOME",
}
# Any of these (non-empty) means the launcher runs inside a Hermes session.
HERMES_DETECT_VARS = (
    "HERMES_SESSION_ID",
    "HERMES_SESSION_PLATFORM",
    "HERMES_SESSION_CHAT_ID",
)


class ContextError(Exception):
    """A launch context is missing, unsafe, or inconsistent."""


def hermes_owner(env: dict[str, str] | os._Environ) -> dict[str, str] | None:
    """Return non-secret Hermes owner identifiers, or None outside Hermes."""
    if not any(env.get(name) for name in HERMES_DETECT_VARS):
        return None
    return {key: env[var] for key, var in HERMES_OWNER_VARS.items() if env.get(var)}


def secret_fingerprint(secret: bytes) -> str:
    return hashlib.sha256(FINGERPRINT_PREFIX + secret).hexdigest()


@dataclass(frozen=True)
class LaunchContext:
    launch_id: str
    directory: Path
    config: Config
    secret_fingerprint: str
    route: str
    owner: dict[str, str] | None

    @property
    def context_file(self) -> Path:
        return self.directory / CONTEXT_NAME

    @property
    def settings_file(self) -> Path:
        return self.directory / SETTINGS_NAME


def _write_private(path: Path, text: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def create_launch(
    *,
    home: Path,
    config: Config,
    secret: bytes,
    route: str,
    owner: dict[str, str] | None,
    command: str,
    resume_session_id: str | None,
    hook_executable: str,
    hook_prefix_args: tuple[str, ...],
) -> LaunchContext:
    """Write a new private launch directory and return its pinned context."""
    from .settings import build_settings

    launch_id = uuid.uuid4().hex
    launches = home / "launches"
    launches.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(launches, 0o700)
    directory = launches / launch_id
    directory.mkdir(mode=0o700)  # fails if it already exists
    os.chmod(directory, 0o700)
    context_file = directory / CONTEXT_NAME
    document = {
        "version": CONTEXT_VERSION,
        "launch_id": launch_id,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "command": command,
        "resume_session_id": resume_session_id,
        "route": route,
        "owner": owner,
        "webhook_url": config.webhook_url,
        "secret_file": str(config.secret_file),
        "secret_fingerprint": secret_fingerprint(secret),
        "timeout_seconds": config.timeout_seconds,
        "allowed_cwd_roots": [str(p) for p in config.allowed_cwd_roots],
        "allowed_transcript_roots": [str(p) for p in config.allowed_transcript_roots],
        "error_log": str(config.error_log),
    }
    _write_private(context_file, json.dumps(document, indent=2) + "\n")
    settings = build_settings(
        hook_executable,
        hook_prefix_args,
        context_file=context_file,
    )
    _write_private(directory / SETTINGS_NAME, json.dumps(settings, indent=2) + "\n")
    return load_context(context_file)


def _check_private(path: Path, *, directory: bool) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ContextError(f"cannot stat {path}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise ContextError(f"{path} is a symbolic link")
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode):
        raise ContextError(f"{path} has an unexpected file type")
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise ContextError(f"{path} is not owned by the current user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ContextError(f"{path} grants group/other access")


def _require(data: dict, key: str, kind: type | tuple[type, ...]):
    value = data.get(key)
    if not isinstance(value, kind) or isinstance(value, bool) and kind is not bool:
        raise ContextError(f"context field {key!r} is missing or invalid")
    return value


def _absolute_paths(values: object, key: str) -> tuple[Path, ...]:
    if not isinstance(values, list) or not values:
        raise ContextError(f"context field {key!r} must be a non-empty list")
    paths = []
    for value in values:
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ContextError(f"context field {key!r} must hold absolute paths")
        paths.append(Path(value))
    return tuple(paths)


def load_context(context_file: str | Path) -> LaunchContext:
    """Load and strictly validate a pinned launch context.

    Only the explicit file is consulted: no environment variables, no shared
    config, and no fallback route.
    """
    path = Path(context_file)
    if not path.is_absolute():
        raise ContextError("context path must be absolute")
    if path.name != CONTEXT_NAME:
        raise ContextError(f"context file must be named {CONTEXT_NAME}")
    _check_private(path.parent, directory=True)
    _check_private(path, directory=False)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read(MAX_CONTEXT_BYTES + 1)
    except OSError as exc:
        raise ContextError(f"cannot read context: {exc}") from exc
    if len(raw) > MAX_CONTEXT_BYTES:
        raise ContextError("context file is too large")
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContextError(f"context is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ContextError("context is not a JSON object")
    if data.get("version") != CONTEXT_VERSION:
        raise ContextError("unsupported context version")
    launch_id = _require(data, "launch_id", str)
    if launch_id != path.parent.name:
        raise ContextError("launch_id does not match its directory")
    route = _require(data, "route", str)
    if route not in {"owner", "shared"}:
        raise ContextError("context route must be 'owner' or 'shared'")
    owner = data.get("owner")
    if owner is not None and not (
        isinstance(owner, dict)
        and all(isinstance(k, str) and isinstance(v, str) for k, v in owner.items())
    ):
        raise ContextError("context owner is invalid")
    url = _require(data, "webhook_url", str)
    timeout = _require(data, "timeout_seconds", (int, float))
    secret_file = _require(data, "secret_file", str)
    error_log = _require(data, "error_log", str)
    fingerprint = _require(data, "secret_fingerprint", str)
    try:
        validate_url(url)
        validate_timeout(float(timeout))
    except ValueError as exc:
        raise ContextError(str(exc)) from exc
    for key, value in (("secret_file", secret_file), ("error_log", error_log)):
        if not Path(value).is_absolute():
            raise ContextError(f"context field {key!r} must be absolute")
    if len(fingerprint) != 64:
        raise ContextError("context secret_fingerprint is invalid")
    config = Config(
        webhook_url=url,
        secret_file=Path(secret_file),
        settings_file=path.parent / SETTINGS_NAME,
        error_log=Path(error_log),
        timeout_seconds=float(timeout),
        allowed_cwd_roots=_absolute_paths(
            data.get("allowed_cwd_roots"), "allowed_cwd_roots"
        ),
        allowed_transcript_roots=_absolute_paths(
            data.get("allowed_transcript_roots"), "allowed_transcript_roots"
        ),
    )
    return LaunchContext(
        launch_id=launch_id,
        directory=path.parent,
        config=config,
        secret_fingerprint=fingerprint,
        route=route,
        owner=owner,
    )
