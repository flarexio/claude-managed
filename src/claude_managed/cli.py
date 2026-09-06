from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from . import __version__
from .config import (
    DEFAULT_URL,
    config_dir,
    load_config,
    secure_file,
    validate_timeout,
    validate_url,
    write_config,
)
from .forwarder import hook_main, read_secret
from .settings import build_settings, write_settings

MIN_CLAUDE_VERSION = (2, 1, 198)


def _hook_command() -> tuple[str, tuple[str, ...]]:
    found = shutil.which("claude-managed")
    if found:
        return str(Path(found).resolve()), ()
    return sys.executable, ("-m", "claude_managed")


def _strip_separator(args: list[str]) -> list[str]:
    return args[1:] if args and args[0] == "--" else args


def cmd_init(args: argparse.Namespace) -> int:
    root = config_dir()
    config_path = root / "config.toml"
    existing = None
    if config_path.exists() and not args.force:
        print(f"Configuration already exists: {config_path}", file=sys.stderr)
        print(
            "Use --force to regenerate settings while preserving an existing secret.",
            file=sys.stderr,
        )
        return 2
    if config_path.exists():
        existing = load_config(root)

    secret_file = (
        Path(args.secret_file).expanduser().resolve()
        if args.secret_file
        else existing.secret_file
        if existing
        else root / "secret"
    )
    settings_file = (
        existing.settings_file if existing else root / "managed-settings.json"
    )
    error_log = existing.error_log if existing else root / "errors.log"
    cwd_roots = [
        Path(p).expanduser().resolve()
        for p in (
            args.cwd_root
            or (
                list(existing.allowed_cwd_roots)
                if existing
                else [Path.home() / "projects"]
            )
        )
    ]
    transcript_roots = [
        Path(p).expanduser().resolve()
        for p in (
            args.transcript_root
            or (
                list(existing.allowed_transcript_roots)
                if existing
                else [Path.home() / ".claude" / "projects"]
            )
        )
    ]
    webhook_url = args.webhook_url or (
        existing.webhook_url if existing else DEFAULT_URL
    )
    timeout = (
        args.timeout
        if args.timeout is not None
        else existing.timeout_seconds
        if existing
        else 8.0
    )
    validate_url(webhook_url)
    validate_timeout(timeout)

    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not secret_file.exists():
        secret_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(secrets.token_urlsafe(48) + "\n")
    os.chmod(secret_file, 0o600)
    read_secret(secret_file)

    write_config(
        root=root,
        webhook_url=webhook_url,
        secret_file=secret_file,
        settings_file=settings_file,
        error_log=error_log,
        timeout_seconds=timeout,
        cwd_roots=cwd_roots,
        transcript_roots=transcript_roots,
    )
    command, prefix_args = _hook_command()
    write_settings(settings_file, command, prefix_args)
    os.chmod(settings_file, 0o600)

    print("claude-managed initialized")
    print(f"  config:   {config_path}")
    print(f"  settings: {settings_file}")
    print(f"  secret:   {secret_file} (value not displayed)")
    print("\nConfigure the receiving webhook with the same secret, then run:")
    print("  claude-managed doctor")
    print("  claude-managed run")
    return 0


def _health_url(webhook_url: str) -> str:
    parsed = urlsplit(webhook_url)
    return urlunsplit((parsed.scheme, parsed.netloc, "/health", "", ""))


def _parse_claude_version(output: str) -> tuple[int, int, int]:
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", output)
    if not match:
        raise ValueError(
            f"could not parse Claude Code version from: {output.strip()!r}"
        )
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def cmd_doctor(_: argparse.Namespace) -> int:
    checks: list[tuple[str, bool, str]] = []
    try:
        config = load_config()
        checks.append(("config", True, str(config_dir() / "config.toml")))
    except Exception as exc:  # noqa: BLE001 - doctor reports every configuration failure
        checks.append(("config", False, str(exc)))
        config = None

    claude = shutil.which("claude")
    if claude:
        try:
            version_result = subprocess.run(
                [claude, "--version"],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
            version_text = (version_result.stdout or version_result.stderr).strip()
            version = _parse_claude_version(version_text)
            version_ok = version >= MIN_CLAUDE_VERSION
            detail = f"{claude}: {version_text}"
            if not version_ok:
                detail += "; requires Claude Code >=2.1.198"
            checks.append(("claude", version_ok, detail))
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            checks.append(("claude", False, str(exc)))
    else:
        checks.append(("claude", False, "not found on PATH"))

    if config:
        secret_ok, secret_detail = secure_file(config.secret_file)
        if secret_ok:
            try:
                read_secret(config.secret_file)
            except (OSError, ValueError) as exc:
                secret_ok, secret_detail = False, str(exc)
        checks.append(("secret", secret_ok, f"{config.secret_file}: {secret_detail}"))
        try:
            settings = json.loads(config.settings_file.read_text(encoding="utf-8"))
            command, prefix_args = _hook_command()
            expected = build_settings(command, prefix_args)
            ok = settings == expected
            detail = (
                str(config.settings_file)
                if ok
                else "stale; run 'claude-managed settings'"
            )
            checks.append(("settings", ok, detail))
        except Exception as exc:  # noqa: BLE001 - doctor reports malformed settings
            checks.append(("settings", False, str(exc)))
        try:
            with urllib.request.urlopen(
                _health_url(config.webhook_url), timeout=config.timeout_seconds
            ) as response:
                body = response.read(512).decode("utf-8", errors="replace")
                checks.append(("webhook", response.status < 300, body))
        except Exception as exc:  # noqa: BLE001 - doctor reports all transport failures
            checks.append(("webhook", False, str(exc)))

    for name, ok, detail in checks:
        print(f"{'OK' if ok else 'FAIL':4} {name:10} {detail}")
    return 0 if all(ok for _, ok, _ in checks) else 1


def cmd_settings(_: argparse.Namespace) -> int:
    config = load_config()
    command, prefix_args = _hook_command()
    write_settings(config.settings_file, command, prefix_args)
    os.chmod(config.settings_file, 0o600)
    print(config.settings_file)
    return 0


def _exec_claude(extra: list[str], resume_id: str | None = None) -> int:
    config = load_config()
    if not config.settings_file.is_file():
        raise FileNotFoundError(
            f"settings not found: {config.settings_file}; run 'claude-managed settings'"
        )
    extra = _strip_separator(extra)
    blocked = next(
        (
            arg
            for arg in extra
            if arg in {"--settings", "--bare", "--safe-mode"}
            or arg.startswith("--settings=")
        ),
        None,
    )
    if blocked:
        raise ValueError(
            f"{blocked} disables or replaces managed hooks and is not allowed"
        )
    claude = shutil.which("claude")
    if not claude:
        raise FileNotFoundError("claude executable not found on PATH")
    argv = [claude]
    if resume_id is not None:
        argv.extend(["--resume", resume_id])
    argv.extend(["--settings", str(config.settings_file), *extra])
    os.execv(claude, argv)
    return 127


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-managed",
        description="Run opt-in Claude Code sessions supervised through authenticated lifecycle hooks.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser(
        "init", help="create config, secret, and Claude hook settings"
    )
    init.add_argument("--webhook-url")
    init.add_argument("--secret-file")
    init.add_argument(
        "--cwd-root", action="append", help="allowed project root; repeatable"
    )
    init.add_argument(
        "--transcript-root",
        action="append",
        help="allowed Claude transcript root; repeatable",
    )
    init.add_argument("--timeout", type=float)
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)

    doctor = sub.add_parser(
        "doctor",
        help="verify config, permissions, Claude, settings, and webhook health",
    )
    doctor.set_defaults(func=cmd_doctor)

    settings = sub.add_parser(
        "settings",
        help="regenerate managed Claude settings after moving/updating the CLI",
    )
    settings.set_defaults(func=cmd_settings)

    run = sub.add_parser("run", help="start a managed interactive Claude Code session")
    run.add_argument(
        "claude_args",
        nargs=argparse.REMAINDER,
        help="arguments passed to Claude after --",
    )
    run.set_defaults(func=lambda a: _exec_claude(a.claude_args))

    resume = sub.add_parser(
        "resume", help="resume a Claude Code session with managed hooks"
    )
    resume.add_argument("session_id", help="Claude Code session ID, preserved exactly")
    resume.add_argument(
        "claude_args",
        nargs=argparse.REMAINDER,
        help="arguments passed to Claude after --",
    )
    resume.set_defaults(func=lambda a: _exec_claude(a.claude_args, a.session_id))

    hook = sub.add_parser("hook-forward", help=argparse.SUPPRESS)
    hook.set_defaults(func=lambda _: hook_main())
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (FileNotFoundError, ValueError, KeyError, OSError) as exc:
        print(f"claude-managed: {exc}", file=sys.stderr)
        return 2
