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
- a private, immutable per-launch routing context, so concurrent launches never share a mutable route;
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
pipx install git+https://github.com/flarexio/claude-managed.git
```

Or with `uv`:

```bash
uv tool install git+https://github.com/flarexio/claude-managed.git
```

For development:

```bash
git clone https://github.com/flarexio/claude-managed.git
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
- `managed-settings.json` — legacy, unbound Hook configuration (`0600`); `run`/`resume` no longer use it
- `launches/<launch-id>/` — one private (`0700`) directory per `run`/`resume` (see [Per-launch routing](#per-launch-routing-and-owner-isolation))
- `errors.log` — only created when delivery/validation fails

Override that directory with `CLAUDE_MANAGED_HOME` when testing or isolating profiles.

Configure the receiving webhook with the **same secret** using the receiver's secure secret-management method. Do not commit it, paste it into issue text, or embed it in Claude settings.

### 2. Verify prerequisites

```bash
claude-managed doctor
```

It checks:

- configuration validity;
- Claude executable availability and minimum supported version;
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

Everything after the first `--` goes to Claude verbatim. `claude-managed` options must come before it.

Launched from inside a Hermes conversation, `run` and `resume` require an owner-specific route (see below):

```bash
claude-managed run \
  --webhook-url http://127.0.0.1:8644/webhooks/claude-owner-<id> \
  --secret-file ~/.config/claude-managed/owners/<id>.secret \
  -- --model sonnet
```

### 4. Resume an existing session

```bash
claude-managed resume 01234567-89ab-cdef-0123-456789abcdef
```

The session ID is passed as one exact argument without shell interpolation. Existing transcript and context are retained; the resumed process gains managed Hooks.

`resume` always creates a **new** launch context bound to the *current* launcher's environment and route options. It never reuses the route or owner of the launch that originally created the session. Inside Hermes it needs the same `--webhook-url`/`--secret-file` as `run`:

```bash
claude-managed resume --webhook-url http://127.0.0.1:8644/webhooks/claude-owner-<id> \
  --secret-file ~/.config/claude-managed/owners/<id>.secret \
  01234567-89ab-cdef-0123-456789abcdef
```

The Hook command path is pinned at launch. After moving or upgrading the executable, restart managed sessions. `claude-managed settings` only refreshes the legacy shared settings file.

## Commands

```text
claude-managed init [options]       Create config, secret, and Hook settings
claude-managed doctor               Check the complete local setup
claude-managed settings             Regenerate Hook settings
claude-managed run [ROUTE] [-- ARGS...]     Start interactive Claude with managed Hooks
claude-managed resume [ROUTE] ID [-- ...]   Resume one exact Claude session with Hooks
claude-managed hook-forward --context PATH  Internal Hook entry point (pinned launch)
claude-managed hook-forward                 Legacy unbound Hook entry point

ROUTE (per launch; shared config is never modified):
  --webhook-url URL     owner-specific receiver for this launch only
  --secret-file PATH    private HMAC secret for that receiver (default: configured secret)
  --shared-route        inside Hermes, deliberately use the configured shared route
```

`run` and `resume` intentionally reject caller-provided `--settings` (including `--settings=...`), `--bare`, and `--safe-mode`; each can silently replace or disable the managed Hooks.

## Per-launch routing and owner isolation

### Problem this solves

Previously every generated Hook ran a bare `claude-managed hook-forward`. That command loaded the shared `config.toml` at **event time**, located through whatever `CLAUDE_MANAGED_HOME`/`XDG_CONFIG_HOME` the Hook inherited. All managed sessions posted to one static route, so events from unrelated sessions, including ones launched by a different Hermes conversation, went to that route's single configured recipient. Nothing was broadcast to both conversations; the events reached whichever recipient the shared route was configured for, which was not necessarily the owner. Re-running `init` or editing config also silently retargeted sessions that were already running.

### How launches are pinned now

Every `run`/`resume`:

1. resolves the route once, at launch, from the shared config plus any `--webhook-url`/`--secret-file`;
2. creates `launches/<launch-id>/` (`0700`) under the managed home, containing:
   - `context.json` (`0600`): webhook URL, secret **path** plus a SHA-256 fingerprint (never the secret itself), timeout, path allowlists, error log, route kind (`owner`/`shared`), the resume ID, and the launching Hermes owner identifiers (`HERMES_SESSION_ID`, `…_PLATFORM`, `…_CHAT_ID`, `…_THREAD_ID`, `…_PROFILE`, `HERMES_HOME`) when present;
   - `settings.json` (`0600`): Hooks that call `claude-managed hook-forward --context <absolute path to context.json>`;
3. execs Claude with `--settings <that settings.json>`.

At event time `hook-forward --context` reads **only** that file. It ignores `CLAUDE_MANAGED_HOME`, `XDG_CONFIG_HOME`, `HERMES_*`, the shared `config.toml` and `managed-settings.json`, so changing any of them after launch does not affect a running session. The context must be an absolute path to a regular file named `context.json`. It must not be a symlink, it and its directory must be owned by you with no group/other access, and its `launch_id` must match the directory name. If the secret file's content no longer matches the pinned fingerprint, the event is dropped rather than signed with a different secret.

On any failure (missing, corrupt, unsafe or mismatched context, rotated secret, path-policy violation, transport error) the Hook logs the reason, drops the event and **exits 0**. It never falls back to the shared route. Errors that occur before a context could be trusted go to `~/.local/state/claude-managed/errors.log`. Later errors go to the context's `error_log`.

Launch directories are not removed automatically, because `claude-managed` execs Claude and does not outlive it. Delete `launches/<id>/` once that Claude process has exited; its Hooks would then be dropped.

### Hermes: one route per owner, fail closed

When the launcher's environment contains a non-empty `HERMES_SESSION_ID`, `HERMES_SESSION_PLATFORM` or `HERMES_SESSION_CHAT_ID`, `run`/`resume` **refuse to start** (exit 2, before Claude is launched) unless:

- `--webhook-url` names a route that is **different** from the shared configured `webhook_url`, or
- `--shared-route` is passed explicitly. In that case events go to the shared receiver exactly as before, and the context records `"route": "shared"`.

Owner metadata alone does not isolate anything. Hermes dispatches by route name (`/webhooks/<name>`), and each route has its own prompt, delivery target and secret. Isolation therefore comes only from **giving each owner its own route and secret**, for example:

```bash
# Operator, once per owner (outside claude-managed). Prefer omitting --secret so
# Hermes generates one; then store it in a private 0600 file for --secret-file.
hermes webhook subscribe claude-owner-<id> \
  --prompt "$(cat examples/hermes-supervisor-prompt.md)" \
  --deliver discord --deliver-chat-id <that conversation's chat id>
```

`claude-managed` does **not** create, verify or modify Hermes subscriptions. It cannot prove that a URL belongs to the launching conversation. It only guarantees that each launch posts to the route it was given at launch and to nothing else. Making sure `claude-owner-<id>` really delivers to that owner, and that no two owners share a route, remains the operator's and receiver's responsibility. Note that `hermes webhook subscribe --secret VALUE` puts the secret on a command line.

A webhook-triggered Hermes agent is a **separate run**. It does not resume or join the conversation that launched Claude. Use the route's delivery target (and optionally `--mirror-to-session`) to put results in front of the right conversation.

### Legacy and non-Hermes use

- Outside Hermes, plain `claude-managed run` still uses the configured shared route, now pinned at launch.
- A bare `hook-forward` (no `--context`), as found in `managed-settings.json` or in sessions started before this change, keeps the old ambient-config behaviour **only outside a Hermes context**. Inside one it drops the event and logs `unbound legacy hook inside a Hermes session`.

### Migrating existing live sessions

Claude sessions started with an earlier version still run bare `hook-forward` from the shared `managed-settings.json`. After upgrading, sessions launched from Hermes will **stop delivering events** (dropped and logged, never misrouted). Sessions launched outside Hermes keep reading the current shared config. To restore delivery, exit each affected Claude session and resume it with an owner route:

```bash
claude-managed resume --webhook-url <owner route> --secret-file <owner secret> <session-id>
```

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

The Hook JSON from Claude is forwarded byte-for-byte as the POST body (unchanged).

Headers:

```text
Content-Type: application/json
X-Webhook-Timestamp: <unix seconds>
X-Webhook-Signature-V2: <hex HMAC-SHA256>
X-Request-ID: <UUID v4>
X-Webhook-Event: <hook_event_name>
X-Claude-Managed-Launch-ID: <32 hex chars>   # pinned launches only; new
```

`X-Claude-Managed-Launch-ID` is **unsigned and informational**: use it for logging and correlation, never for authorization or routing. Python's urllib may send it as `X-claude-managed-launch-id`, and HTTP header names are case-insensitive.

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
- Each launch's route, secret reference and path policy are pinned in a private context named explicitly by the Hook command. There is no event-time fallback to shared config.
- Inside Hermes, launches without an owner-specific route fail closed.
- The fallback Hook command uses `python -I`, so a project's working directory cannot shadow the package.
- Hook errors do not alter Claude's behavior.

### Limitations

- Same-user trust: anything running as your user can edit a `0600` launch context or settings file. Pinning protects against accidental retargeting and ambient-variable drift, not against a malicious process under your own UID.
- Project or user Claude settings can still add their own Hooks or set `disableAllHooks`. `run`/`resume` reject only `--settings`, `--bare` and `--safe-mode` on the command line.
- Endpoint ownership is not verified (see above).

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
- Restart the session after moving or reinstalling the executable (the Hook path is pinned per launch).
- Check `${XDG_CONFIG_HOME:-~/.config}/claude-managed/errors.log` and `~/.local/state/claude-managed/errors.log`.
- `unbound legacy hook inside a Hermes session`: the session predates per-launch contexts. Resume it with `--webhook-url`.
- `secret changed since launch`: the secret file was rotated. Restart the session.
- Confirm sender and receiver use the same HMAC secret.
- Confirm both path allowlists include the real resolved paths.

### Why was a routine start not posted to chat?

The event may have been received correctly and classified as `[SILENT]`. Check receiver logs or webhook-source session history rather than treating silence as delivery failure.

## Testing

```bash
python -m unittest discover -s tests -v
python -m compileall -q src
```

Tests cover config round-tripping, URL policy, private secret creation, generated events, exact argv construction, CLI route parsing, path rejection, original-body forwarding, and signature verification against real loopback HTTP receivers. Owner-isolation tests run a fake `claude` executable that executes the generated Hook commands. They cover concurrent Hermes owners A and B sharing one managed home and cwd while the shared config, settings and secret are mutated mid-session and the Hook environment is altered, resume rebinding, fail-closed launches, legacy behaviour, and invalid or tampered contexts. Tests do not execute a real Claude Code binary.

## License

MIT
