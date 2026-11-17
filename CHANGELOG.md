# Changelog

All notable changes to this project are documented here.

## Unreleased

Owner isolation for managed Hooks (fixes cross-conversation delivery):

- `run`/`resume` pin a private, immutable per-launch context (`launches/<id>/context.json` + `settings.json`); Hooks call `hook-forward --context <path>` and never re-read shared config or ambient `CLAUDE_MANAGED_HOME`/`XDG_CONFIG_HOME`/`HERMES_*` at event time.
- New per-launch options `--webhook-url`, `--secret-file`, `--shared-route` on `run` and `resume`; shared config is never modified.
- Inside a Hermes session, launches without an owner-specific route (or with the shared URL) fail closed unless `--shared-route` is explicit.
- `resume` always binds to the current launcher's owner and route.
- Missing/corrupt/unsafe contexts, rotated secrets, and malformed `hook-forward` arguments are logged and dropped with exit 0; there is no fallback route.
- Legacy bare `hook-forward` is dropped inside a Hermes context; unchanged outside it.
- Wire: body unchanged; new unsigned informational `X-Claude-Managed-Launch-ID` header on pinned launches.
- Fallback Hook command uses `python -I -m claude_managed`.
- Claude arguments are taken verbatim after the first `--`.
- Migration: live sessions started under 0.1.0 inside Hermes stop delivering until resumed with `--webhook-url`.

## 0.1.0 — 2026-09-06

Initial alpha release:

- opt-in `run` and exact-session `resume` commands;
- generated asynchronous Claude Code lifecycle Hooks;
- HMAC-SHA256 V2 webhook forwarding with timestamps and request IDs;
- project/transcript path allowlists and private secret enforcement;
- local setup and health diagnostics;
- Hermes supervisor prompt and end-to-end test coverage;
- enforced managed-Hook flags and configuration validation;
- safe TOML escaping and enforced Claude Code 2.1.198+ compatibility.
