from __future__ import annotations

import json
from pathlib import Path

EVENTS: dict[str, str | None] = {
    "SessionStart": "startup|resume|compact",
    "Stop": None,
    "StopFailure": None,
    "Notification": "permission_prompt|agent_needs_input|agent_completed",
}


def build_settings(
    executable: str,
    prefix_args: tuple[str, ...] = (),
    *,
    context_file: Path | None = None,
) -> dict:
    """Build Claude Hook settings.

    With ``context_file`` every Hook explicitly names one pinned launch
    context; without it the legacy, ambient-config ``hook-forward`` is used.
    """
    hooks: dict[str, list[dict]] = {}
    args = [*prefix_args, "hook-forward"]
    if context_file is not None:
        args.extend(["--context", str(context_file)])
    command = {
        "type": "command",
        "command": executable,
        "args": args,
        "async": True,
    }
    for event, matcher in EVENTS.items():
        group: dict = {"hooks": [{**command, "args": list(args)}]}
        if matcher is not None:
            group["matcher"] = matcher
        hooks[event] = [group]
    return {"hooks": hooks}


def write_settings(
    path: Path, executable: str, prefix_args: tuple[str, ...] = ()
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(build_settings(executable, prefix_args), indent=2) + "\n",
        encoding="utf-8",
    )
