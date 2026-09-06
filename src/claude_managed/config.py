from __future__ import annotations

import json
import os
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_URL = "http://127.0.0.1:8644/webhooks/claude-code-events"


def config_dir() -> Path:
    override = os.environ.get("CLAUDE_MANAGED_HOME")
    if override:
        return Path(override).expanduser().resolve()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "claude-managed"


@dataclass(frozen=True)
class Config:
    webhook_url: str
    secret_file: Path
    settings_file: Path
    error_log: Path
    timeout_seconds: float
    allowed_cwd_roots: tuple[Path, ...]
    allowed_transcript_roots: tuple[Path, ...]


def _paths(values: list[str]) -> tuple[Path, ...]:
    return tuple(Path(v).expanduser().resolve() for v in values)


def validate_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("webhook_url must be an http(s) URL")
    if parsed.scheme == "http" and parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ValueError("plain HTTP is allowed only for a loopback webhook")


def validate_timeout(timeout: float) -> None:
    if not 0.1 <= timeout <= 60:
        raise ValueError("timeout_seconds must be between 0.1 and 60")


def load_config(root: Path | None = None) -> Config:
    root = root or config_dir()
    path = root / "config.toml"
    if not path.is_file():
        raise FileNotFoundError(
            f"configuration not found: {path}; run 'claude-managed init'"
        )
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    url = str(data["webhook_url"])
    validate_url(url)
    timeout = float(data.get("timeout_seconds", 8.0))
    validate_timeout(timeout)
    cwd_roots = _paths(list(data.get("allowed_cwd_roots", [])))
    transcript_roots = _paths(list(data.get("allowed_transcript_roots", [])))
    if not cwd_roots or not transcript_roots:
        raise ValueError("allowed cwd and transcript roots must not be empty")
    return Config(
        webhook_url=url,
        secret_file=Path(data["secret_file"]).expanduser().resolve(),
        settings_file=Path(data["settings_file"]).expanduser().resolve(),
        error_log=Path(data["error_log"]).expanduser().resolve(),
        timeout_seconds=timeout,
        allowed_cwd_roots=cwd_roots,
        allowed_transcript_roots=transcript_roots,
    )


def _toml_string(value: str) -> str:
    # TOML basic strings and JSON strings share the escapes we need here.
    # json.dumps also handles control characters that cannot appear literally
    # in a single-line TOML basic string.
    return json.dumps(value, ensure_ascii=False)


def write_config(
    *,
    root: Path,
    webhook_url: str,
    secret_file: Path,
    settings_file: Path,
    error_log: Path,
    timeout_seconds: float,
    cwd_roots: list[Path],
    transcript_roots: list[Path],
) -> Path:
    validate_url(webhook_url)
    validate_timeout(timeout_seconds)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    lines = [
        f"webhook_url = {_toml_string(webhook_url)}",
        f"secret_file = {_toml_string(str(secret_file))}",
        f"settings_file = {_toml_string(str(settings_file))}",
        f"error_log = {_toml_string(str(error_log))}",
        f"timeout_seconds = {timeout_seconds:g}",
        "allowed_cwd_roots = ["
        + ", ".join(_toml_string(str(p)) for p in cwd_roots)
        + "]",
        "allowed_transcript_roots = ["
        + ", ".join(_toml_string(str(p)) for p in transcript_roots)
        + "]",
        "",
    ]
    path = root / "config.toml"
    path.write_text("\n".join(lines), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def secure_file(path: Path) -> tuple[bool, str]:
    try:
        info = path.lstat()
    except OSError as exc:
        return False, str(exc)
    if path.is_symlink():
        return False, "symbolic links are not allowed"
    if not stat.S_ISREG(info.st_mode):
        return False, "not a regular file"
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o077:
        return False, f"permissions are {mode:o}; expected no group/other access"
    return True, f"permissions {mode:o}"
