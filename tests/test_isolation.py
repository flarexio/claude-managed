"""Owner isolation: concurrent Hermes launches must never cross-route hooks."""

import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from support import (
    LAUNCHER,
    Receiver,
    clean_env,
    hermes_env,
    install_fake_claude,
    verify_signature,
    write_secret,
)

SHARED_SECRET = "shared-" + "s" * 40
SECRET_A = "owner-a-" + "a" * 40
SECRET_B = "owner-b-" + "b" * 40


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / "home").mkdir()
        self.projects = self.root / "projects"
        self.repo = self.projects / "repo"  # same cwd for every launch
        self.transcripts = self.root / "transcripts"
        self.repo.mkdir(parents=True)
        self.transcripts.mkdir()
        self.home = self.root / "config"  # same managed home for every launch
        install_fake_claude(self.root)
        self.shared = Receiver()
        self.recv_a = Receiver()
        self.recv_b = Receiver()
        self.secret_shared = write_secret(self.root / "shared-secret", SHARED_SECRET)
        self.secret_a = write_secret(self.root / "secret-a", SECRET_A)
        self.secret_b = write_secret(self.root / "secret-b", SECRET_B)
        self.cli(
            "init",
            "--webhook-url",
            self.shared.url("/webhooks/claude-code-events"),
            "--secret-file",
            str(self.secret_shared),
            "--cwd-root",
            str(self.projects),
            "--transcript-root",
            str(self.transcripts),
        )

    def tearDown(self):
        for receiver in (self.shared, self.recv_a, self.recv_b):
            receiver.close()
        self.tmp.cleanup()

    # -- helpers ---------------------------------------------------------

    def env(self, **extra):
        return clean_env(self.root, CLAUDE_MANAGED_HOME=str(self.home), **extra)

    def cli(self, *argv, env=None, check=True):
        return subprocess.run(
            LAUNCHER + list(argv),
            env=env or self.env(),
            cwd=self.repo,
            input="",  # never inherit the test runner's stdin
            capture_output=True,
            text=True,
            check=check,
            timeout=60,
        )

    def launch_env(self, name, owner=None, fire="SessionStart,Stop", **extra):
        env = self.env(
            FAKE_CLAUDE_ARGS=str(self.root / f"{name}.args.json"),
            FAKE_CLAUDE_SESSION=f"claude-session-{name}",
            FAKE_CLAUDE_TRANSCRIPT=str(self.transcripts / f"{name}.jsonl"),
            FAKE_CLAUDE_FIRE=fire,
            **extra,
        )
        if owner:
            env.update(hermes_env(owner))
        return env

    def sent(self, name):
        return json.loads((self.root / f"{name}.args.json.sent").read_text())

    def argv(self, name):
        return json.loads((self.root / f"{name}.args.json").read_text())

    def launch_dir(self, name) -> Path:
        argv = self.argv(name)
        return Path(argv[argv.index("--settings") + 1]).parent

    def assert_delivered(self, receiver, name, secret):
        sent = self.sent(name)
        self.assertTrue(sent)
        self.assertTrue(all(item["rc"] == 0 for item in sent))
        expected = [item["body"].encode() for item in sent]
        self.assertEqual(receiver.bodies(), expected)  # byte-for-byte
        launch_id = self.launch_dir(name).name
        for _, headers, body in receiver.requests:
            self.assertTrue(verify_signature(headers, body, secret))
            lowered = {k.lower(): v for k, v in headers.items()}
            self.assertEqual(lowered["x-claude-managed-launch-id"], launch_id)

    def fallback_log(self) -> Path:
        return self.root / "home/.local/state/claude-managed/errors.log"

    def payload(self, name="manual") -> bytes:
        return json.dumps(
            {
                "hook_event_name": "Stop",
                "session_id": name,
                "cwd": str(self.repo),
                "transcript_path": str(self.transcripts / f"{name}.jsonl"),
            }
        ).encode()

    # -- tests -----------------------------------------------------------

    def test_concurrent_owners_same_home_and_cwd_never_cross_route(self):
        go = self.root / "go"
        a = subprocess.Popen(
            LAUNCHER
            + [
                "run",
                "--webhook-url",
                self.recv_a.url("/webhooks/claude-owner-a"),
                "--secret-file",
                str(self.secret_a),
                "--",
                "--model",
                "sonnet",
            ],
            env=self.launch_env(
                "a",
                owner="a",
                FAKE_CLAUDE_GO=str(go),
                # Hooks run with ambient state pointing at B and a hostile home.
                FAKE_HOOK_ENV=json.dumps(
                    {
                        **hermes_env("b"),
                        "CLAUDE_MANAGED_HOME": str(self.root / "elsewhere"),
                        "XDG_CONFIG_HOME": str(self.root / "elsewhere-xdg"),
                    }
                ),
            ),
            cwd=self.repo,
            stdin=subprocess.DEVNULL,
        )
        b = subprocess.Popen(
            LAUNCHER
            + [
                "resume",
                "--webhook-url",
                self.recv_b.url("/webhooks/claude-owner-b"),
                "--secret-file",
                str(self.secret_b),
                "claude-session-b",
            ],
            env=self.launch_env(
                "b",
                owner="b",
                FAKE_CLAUDE_GO=str(go),
                FAKE_HOOK_ENV=json.dumps(hermes_env("a")),
                FAKE_HOOK_UNSET="CLAUDE_MANAGED_HOME",
            ),
            cwd=self.repo,
            stdin=subprocess.DEVNULL,
        )
        try:
            deadline = time.time() + 20
            while not all((self.root / f"{n}.args.json").exists() for n in ("a", "b")):
                self.assertLess(time.time(), deadline, "launches did not start")
                time.sleep(0.02)
            # Both sessions are live. Retarget every shared, mutable input.
            self.cli(
                "init",
                "--force",
                "--webhook-url",
                self.recv_b.url("/webhooks/hijack"),
                "--secret-file",
                str(self.secret_b),
            )
            self.cli("settings")
            write_secret(self.secret_shared, "rotated-" + "r" * 40)
            go.touch()
            self.assertEqual(a.wait(20), 0)
            self.assertEqual(b.wait(20), 0)
        finally:
            for proc in (a, b):
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()

        self.assert_delivered(self.recv_a, "a", SECRET_A)
        self.assert_delivered(self.recv_b, "b", SECRET_B)
        self.assertEqual(self.shared.requests, [])
        self.assertEqual(
            {path for path, _, _ in self.recv_a.requests}, {"/webhooks/claude-owner-a"}
        )
        self.assertEqual(
            {path for path, _, _ in self.recv_b.requests}, {"/webhooks/claude-owner-b"}
        )
        self.assertEqual(self.argv("a")[-2:], ["--model", "sonnet"])
        self.assertEqual(self.argv("b")[:2], ["--resume", "claude-session-b"])
        ctx_a = json.loads((self.launch_dir("a") / "context.json").read_text())
        ctx_b = json.loads((self.launch_dir("b") / "context.json").read_text())
        self.assertEqual(ctx_a["owner"]["session_id"], "session-a")
        self.assertEqual(ctx_b["owner"]["session_id"], "session-b")
        self.assertEqual(ctx_b["resume_session_id"], "claude-session-b")
        self.assertNotEqual(self.launch_dir("a"), self.launch_dir("b"))
        self.assertNotIn(SECRET_A, (self.launch_dir("a") / "context.json").read_text())

    def test_resume_binds_to_current_owner_not_prior_owner(self):
        self.cli(
            "run",
            "--webhook-url",
            self.recv_a.url(),
            "--secret-file",
            str(self.secret_a),
            env=self.launch_env("first", owner="a"),
        )
        self.assert_delivered(self.recv_a, "first", SECRET_A)
        before = len(self.recv_a.requests)
        env = self.launch_env("again", owner="b")
        env["FAKE_CLAUDE_SESSION"] = "claude-session-first"
        self.cli(
            "resume",
            "--webhook-url",
            self.recv_b.url(),
            "--secret-file",
            str(self.secret_b),
            "claude-session-first",
            env=env,
        )
        self.assertEqual(len(self.recv_a.requests), before)
        self.assert_delivered(self.recv_b, "again", SECRET_B)
        self.assertNotEqual(self.launch_dir("first"), self.launch_dir("again"))

        # Resuming inside a Hermes session without a route never reuses A or
        # the shared route; it refuses before launching Claude.
        launches = sorted((self.home / "launches").iterdir())
        result = self.cli(
            "resume",
            "claude-session-first",
            env=self.launch_env("third", owner="c"),
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("no owner-specific route", result.stderr)
        self.assertFalse((self.root / "third.args.json").exists())
        self.assertEqual(sorted((self.home / "launches").iterdir()), launches)

    def test_hermes_context_fails_closed_without_owner_route(self):
        for argv in (
            ["run"],
            ["run", "--webhook-url", self.shared.url("/webhooks/claude-code-events")],
        ):
            with self.subTest(argv=argv):
                result = self.cli(
                    *argv, env=self.launch_env("x", owner="a"), check=False
                )
                self.assertEqual(result.returncode, 2)
                self.assertFalse((self.root / "x.args.json").exists())
        self.assertFalse((self.home / "launches").exists())
        self.assertEqual(self.shared.requests, [])

        # Deliberate opt-in is explicit and recorded.
        self.cli("run", "--shared-route", env=self.launch_env("opt", owner="a"))
        self.assert_delivered(self.shared, "opt", SHARED_SECRET)
        context = json.loads((self.launch_dir("opt") / "context.json").read_text())
        self.assertEqual(context["route"], "shared")
        self.assertEqual(context["owner"]["chat_id"], "chat-a")

    def test_non_hermes_launch_pins_shared_route_against_later_mutation(self):
        go = self.root / "go"
        proc = subprocess.Popen(
            LAUNCHER + ["run"],
            env=self.launch_env("plain", FAKE_CLAUDE_GO=str(go)),
            cwd=self.repo,
            stdin=subprocess.DEVNULL,
        )
        try:
            deadline = time.time() + 20
            while not (self.root / "plain.args.json").exists():
                self.assertLess(time.time(), deadline)
                time.sleep(0.02)
            self.cli("init", "--force", "--webhook-url", self.recv_b.url("/moved"))
            go.touch()
            self.assertEqual(proc.wait(20), 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        self.assert_delivered(self.shared, "plain", SHARED_SECRET)
        self.assertEqual(self.recv_b.requests, [])

    def test_legacy_unbound_hook(self):
        raw = self.payload("legacy")
        result = self.cli("hook-forward", env=self.env(), check=False)
        # Empty stdin: invalid JSON is logged, never raised.
        self.assertEqual(result.returncode, 0)
        subprocess.run(
            LAUNCHER + ["hook-forward"], env=self.env(), input=raw, check=True
        )
        self.assertEqual(self.shared.bodies(), [raw])

        hermes = self.env(**hermes_env("a"))
        done = subprocess.run(
            LAUNCHER + ["hook-forward"], env=hermes, input=raw, check=False
        )
        self.assertEqual(done.returncode, 0)
        self.assertEqual(self.shared.bodies(), [raw])  # nothing new
        self.assertIn("unbound legacy hook", self.fallback_log().read_text())

    def test_invalid_contexts_drop_and_exit_zero(self):
        self.cli(
            "run",
            "--webhook-url",
            self.recv_a.url(),
            "--secret-file",
            str(self.secret_a),
            env=self.launch_env("valid", owner="a", fire=""),
        )
        good = self.launch_dir("valid")
        launches = good.parent

        def clone(name, mutate_text=None):
            target = launches / name
            shutil.copytree(good, target)
            target.chmod(0o700)
            if mutate_text:
                ctx = target / "context.json"
                ctx.write_text(mutate_text(ctx.read_text()))
                ctx.chmod(0o600)
            return target / "context.json"

        corrupt = clone("c" * 32, lambda _: "{not json")
        mismatch = clone("d" * 32)
        bad_url = clone(
            "e" * 32,
            lambda t: t.replace(self.recv_a.url(), "http://example.com/x"),
        )
        loose = clone("f" * 32)
        loose.chmod(0o644)
        link_dir = launches / ("1" * 32)
        link_dir.mkdir(mode=0o700)
        (link_dir / "context.json").symlink_to(good / "context.json")
        wrong_version = clone(
            "2" * 32, lambda t: t.replace('"version": 1', '"version": 9')
        )
        cases = {
            "missing": str(launches / ("0" * 32) / "context.json"),
            "relative": "launches/x/context.json",
            "corrupt": str(corrupt),
            "launch-id mismatch": str(mismatch),
            "plain http remote": str(bad_url),
            "group readable": str(loose),
            "symlink": str(link_dir / "context.json"),
            "wrong version": str(wrong_version),
            "wrong name": str(good / "settings.json"),
        }
        hostile_env = self.env(**hermes_env("a"))  # managed home already set
        for label, path in cases.items():
            with self.subTest(label):
                done = subprocess.run(
                    LAUNCHER + ["hook-forward", "--context", path],
                    env=hostile_env,
                    input=self.payload(label),
                    check=False,
                )
                self.assertEqual(done.returncode, 0)
        for receiver in (self.shared, self.recv_a, self.recv_b):
            self.assertEqual(receiver.requests, [])
        log = self.fallback_log().read_text()
        self.assertEqual(log.count("ContextError"), len(cases))
        self.assertNotIn(SECRET_A, log)

        # Secret rotated after launch: pinned fingerprint refuses to sign.
        write_secret(self.secret_a, "rotated-" + "z" * 40)
        done = subprocess.run(
            LAUNCHER + ["hook-forward", "--context", str(good / "context.json")],
            env=hostile_env,
            input=self.payload("rotated"),
            check=False,
        )
        self.assertEqual(done.returncode, 0)
        self.assertEqual(self.recv_a.requests, [])
        error_log = json.loads((good / "context.json").read_text())["error_log"]
        self.assertIn("secret changed since launch", Path(error_log).read_text())

        # And the untouched context still works once the secret is restored.
        write_secret(self.secret_a, SECRET_A)
        subprocess.run(
            LAUNCHER + ["hook-forward", "--context", str(good / "context.json")],
            env=hostile_env,
            input=self.payload("restored"),
            check=True,
        )
        self.assertEqual(self.recv_a.bodies(), [self.payload("restored")])

    def test_path_policy_still_enforced_for_pinned_launch(self):
        self.cli(
            "run",
            "--webhook-url",
            self.recv_a.url(),
            "--secret-file",
            str(self.secret_a),
            env=self.launch_env("pol", owner="a", fire=""),
        )
        ctx = self.launch_dir("pol") / "context.json"
        outside = json.dumps(
            {"cwd": "/tmp/elsewhere", "transcript_path": "/tmp/t.jsonl"}
        ).encode()
        done = subprocess.run(
            LAUNCHER + ["hook-forward", "--context", str(ctx)],
            env=self.env(),
            input=outside,
            check=False,
        )
        self.assertEqual(done.returncode, 0)
        self.assertEqual(self.recv_a.requests, [])


if __name__ == "__main__":
    unittest.main()
