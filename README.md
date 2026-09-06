# claude-managed

`claude-managed` launches **opt-in interactive Claude Code sessions** with authenticated lifecycle hooks. A local supervisor such as [Hermes Agent](https://github.com/NousResearch/hermes-agent) can react when Claude starts, stops, fails, needs input, or completes—without polling the TUI and without Discord bot-to-bot messaging.

```text
Claude Code TUI
  → async lifecycle Hook
  → HMAC-authenticated webhook
  → Hermes/Jarvis verifies transcript + repository
  → silent, notify, or safely continue the same TUI
```

> Status: alpha. The CLI and wire contract are tested; review generated settings before using it on sensitive projects.

## Why

A Claude `Stop` event is a signal, not proof of completion. A supervisor can use the event to inspect the referenced transcript, check `git diff` and test evidence, distinguish routine stops from real decisions, and send a narrow continuation prompt when appropriate.

`claude-managed` handles the Claude side of that boundary:

- per-session opt-in instead of global hooks;
- `run` and exact-ID `resume` wrappers;
- generated Claude Hook settings;
- asynchronous, non-blocking delivery;
- HMAC-SHA256 V2 signatures, timestamps, and request IDs;
- allowlists for project and transcript roots;
- private config/secret files;
- a `doctor` command for local verification;
- no runtime dependencies beyond Python 3.11+.

## Requirements

- Python 3.11+
- Claude Code 2.1.198 or newer, installed and authenticated (`claude` on `PATH`)
- An HTTP webhook receiver that implements the [wire contract](#wire-contract)
- For the documented workflow: Hermes Agent with its webhook platform enabled

## Install

With `pipx`:

```bash
pipx install git+https://github.com/jarvis-flarex/claude-managed.git
```

Or with `uv`:

```bash
uv tool install git+https://github.com/jarvis-flarex/claude-managed.git
```

For development:

```bash
git clone https://github.com/jarvis-flarex/claude-managed.git
cd claude-managed
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
```

## Quick start

### 1. Initialize the sender

```bash
claude-managed init \
  --webhook-url http://127.0.0.1:8644/webhooks/claude-code-events \
  --cwd-root "$HOME/projects" \
  --transcript-root "$HOME/.claude/projects"
```

This creates, under `${XDG_CONFIG_HOME:-~/.config}/claude-managed/`:

- `config.toml` — endpoint and path allowlists (`0600`)
- `secret` — generated HMAC secret (`0600`, never displayed)
- `managed-settings.json` — generated Claude Hook configuration (`0600`)
- `errors.log` — only created when delivery/validation fails

Override that directory with `CLAUDE_MANAGED_HOME` when testing or isolating profiles.

Configure the receiving webhook with the **same secret** using the receiver's secure secret-management method. Do not commit it, paste it into issue text, or embed it in Claude settings.

### 2. Verify prerequisites

```bash
claude-managed doctor
```

It checks:

- configuration validity;
- Claude executable availability;
- secret permissions;
- generated settings freshness;
- receiver `/health` response.

### 3. Start a managed session

```bash
cd ~/projects/my-repo
claude-managed run
```

Pass Claude arguments after `--`:

```bash
claude-managed run -- --model sonnet
```

### 4. Resume an existing session

```bash
claude-managed resume 01234567-89ab-cdef-0123-456789abcdef
```

The session ID is passed as one exact argument without shell interpolation. Existing transcript and context are retained; the resumed process gains managed Hooks.

If the installed executable moves after an upgrade, regenerate settings:

```bash
claude-managed settings
```

## Commands

```text
claude-managed init [options]       Create config, secret, and Hook settings
claude-managed doctor               Check the complete local setup
claude-managed settings             Regenerate Hook settings
claude-managed run [-- ARGS...]     Start interactive Claude with managed Hooks
claude-managed resume ID [-- ...]   Resume one exact Claude session with Hooks
claude-managed hook-forward         Internal Hook entry point
```

`run` and `resume` intentionally reject caller-provided `--settings` (including `--settings=...`), `--bare`, and `--safe-mode`; each can silently replace or disable the managed Hooks.

## Events

Generated settings subscribe to:

- `SessionStart`: `startup`, `resume`, and `compact`
- `Stop`
- `StopFailure`
- `Notification`: `permission_prompt`, `agent_needs_input`, and `agent_completed`

Every command Hook is asynchronous. Delivery failure is logged locally and exits zero so monitoring can never block Claude Code.

## Hermes receiver

Enable the adapter on loopback when Claude and Hermes share a host:

```yaml
platforms:
  webhook:
    enabled: true
    extra:
      host: 127.0.0.1
      port: 8644
```

Restart Hermes after enabling or changing the adapter; dynamic subscriptions themselves hot-reload.

Create an authenticated subscription at `/webhooks/claude-code-events`, configure it with the generated sender secret, and use a prompt based on [`examples/hermes-supervisor-prompt.md`](examples/hermes-supervisor-prompt.md).

The receiver—not this sender—owns agent policy. A safe supervisor should:

1. Treat the payload as a signal rather than a completion claim.
2. Resolve `transcript_path` only below an approved Claude projects root.
3. Resolve `cwd` only below approved project roots.
4. Inspect recent transcript context, repository diff, and actual test output.
5. Classify `routine`, `completed`, `needs-human`, `recoverable`, or `failed`.
6. Return `[SILENT]` for routine events.
7. Never auto-approve permissions, merge, deploy, delete data, or expose secrets.

## Wire contract

The Hook JSON from Claude is forwarded byte-for-byte as the POST body.

Headers:

```text
Content-Type: application/json
X-Webhook-Timestamp: <unix seconds>
X-Webhook-Signature-V2: <hex HMAC-SHA256>
X-Request-ID: <UUID v4>
X-Webhook-Event: <hook_event_name>
```

Signature input:

```text
HMAC-SHA256(secret, timestamp + "." + raw_request_body)
```

A receiver should reject stale timestamps, compare signatures in constant time, and deduplicate request IDs.

Plain HTTP endpoints are accepted only for `localhost`, `127.0.0.1`, or `::1`; remote receivers must use HTTPS.

## Security model

### Sender controls

- Config directory is `0700`; secret, config, and settings are `0600`.
- `cwd` must resolve below an allowed project root.
- `transcript_path` must resolve below an allowed transcript root.
- The secret is read from a private regular file—not a symlink, command-line argument, or settings JSON.
- Hook bodies are capped at 1 MiB and webhook redirects are refused.
- Claude arguments are executed as an argv list, never through a shell.
- Hook errors do not alter Claude's behavior.

### Receiver controls still required

Sender path validation limits accidental leakage but does not make Hook payloads trusted. The receiver must independently enforce authentication, timestamp/replay checks, path policy, action policy, and completion verification.

Do not let a webhook-triggered agent automatically approve dangerous permissions or perform irreversible actions.

## Process lifetime

A Claude process launched from inside another service can remain in that service's systemd cgroup. Restarting the parent service may terminate Claude even if it is inside tmux. Its transcript remains resumable:

```bash
claude-managed resume <session-id>
```

If Claude must survive supervisor restarts, launch it from an independently managed user service/scope and verify its actual cgroup. Do not assume tmux alone changes cgroup ownership.

## Troubleshooting

### `doctor` reports webhook failure

Check the receiver directly:

```bash
curl -fsS http://127.0.0.1:8644/health
```

For Hermes, verify the gateway and subscription:

```bash
hermes gateway status
hermes webhook list
```

### Claude works but no event arrives

- Confirm the session was started through `claude-managed`.
- Run `claude-managed settings` after moving or reinstalling the executable.
- Check `${XDG_CONFIG_HOME:-~/.config}/claude-managed/errors.log`.
- Confirm sender and receiver use the same HMAC secret.
- Confirm both path allowlists include the real resolved paths.

### Why was a routine start not posted to chat?

The event may have been received correctly and classified as `[SILENT]`. Check receiver logs or webhook-source session history rather than treating silence as delivery failure.

## Testing

```bash
python -m unittest discover -s tests -v
python -m compileall -q src
```

Tests cover config round-tripping, URL policy, private secret creation, generated events, exact argv construction, path rejection, original-body forwarding, and signature verification against a real local HTTP server.

## License

MIT
