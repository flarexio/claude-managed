import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_managed import cli
from claude_managed.settings import EVENTS, build_settings


class CliTests(unittest.TestCase):
    def test_parses_and_compares_supported_claude_version(self):
        version = cli._parse_claude_version("2.1.198 (Claude Code)")
        self.assertGreaterEqual(version, cli.MIN_CLAUDE_VERSION)

    def test_rejects_unparseable_claude_version(self):
        with self.assertRaisesRegex(ValueError, "could not parse"):
            cli._parse_claude_version("Claude Code development build")

    def test_settings_include_expected_events_and_async_hooks(self):
        settings = build_settings("/usr/local/bin/claude-managed")
        self.assertEqual(set(settings["hooks"]), set(EVENTS))
        for groups in settings["hooks"].values():
            hook = groups[0]["hooks"][0]
            self.assertTrue(hook["async"])
            self.assertEqual(hook["args"], ["hook-forward"])

    def test_init_creates_private_secret_and_valid_json(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"CLAUDE_MANAGED_HOME": tmp}, clear=False),
        ):
            result = cli.main(
                [
                    "init",
                    "--cwd-root",
                    str(Path(tmp) / "projects"),
                    "--transcript-root",
                    str(Path(tmp) / "transcripts"),
                ]
            )
            self.assertEqual(result, 0)
            root = Path(tmp)
            self.assertEqual(root.joinpath("secret").stat().st_mode & 0o777, 0o600)
            settings = json.loads(root.joinpath("managed-settings.json").read_text())
            self.assertIn("Stop", settings["hooks"])

    def test_force_init_preserves_existing_secret_and_configuration(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"CLAUDE_MANAGED_HOME": tmp}, clear=False),
        ):
            custom_secret = Path(tmp) / "outside-secret"
            custom_secret.write_text("x" * 48)
            custom_secret.chmod(0o600)
            first = [
                "init",
                "--webhook-url",
                "https://receiver.example/events",
                "--secret-file",
                str(custom_secret),
                "--cwd-root",
                str(Path(tmp) / "projects"),
                "--transcript-root",
                str(Path(tmp) / "transcripts"),
                "--timeout",
                "4",
            ]
            self.assertEqual(cli.main(first), 0)
            self.assertEqual(cli.main(["init", "--force"]), 0)
            config = cli.load_config(Path(tmp))
            self.assertEqual(config.secret_file, custom_secret)
            self.assertEqual(config.webhook_url, "https://receiver.example/events")
            self.assertEqual(config.timeout_seconds, 4)

    def _init(self, tmp: str) -> Path:
        root = Path(tmp)
        secret = root / "secret"
        secret.write_text("s" * 48)
        secret.chmod(0o600)
        self.assertEqual(
            cli.main(
                [
                    "init",
                    "--webhook-url",
                    "http://127.0.0.1:8644/shared",
                    "--secret-file",
                    str(secret),
                    "--cwd-root",
                    str(root / "projects"),
                    "--transcript-root",
                    str(root / "transcripts"),
                ]
            ),
            0,
        )
        return root

    def _captured_exec(self, argv, env_extra=None):
        """Run cli.main with execv captured; return (rc, execv mock)."""
        with (
            mock.patch.object(
                cli.shutil,
                "which",
                side_effect=lambda name: (
                    "/usr/bin/claude" if name == "claude" else None
                ),
            ),
            mock.patch.object(
                cli.os, "execv", side_effect=RuntimeError("captured")
            ) as execv,
        ):
            if env_extra:
                os.environ.update(env_extra)
            try:
                rc = cli.main(argv)
            except RuntimeError:
                rc = None
        return rc, execv

    def test_exec_preserves_resume_id_and_does_not_use_shell(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"CLAUDE_MANAGED_HOME": tmp}, clear=True),
        ):
            self._init(tmp)
            rc, execv = self._captured_exec(
                ["resume", "id-with-punctuation;$(x)", "--", "--model", "sonnet"]
            )
            self.assertIsNone(rc)
            exe, argv = execv.call_args.args
            self.assertEqual(exe, "/usr/bin/claude")
            self.assertEqual(
                argv[:3], ["/usr/bin/claude", "--resume", "id-with-punctuation;$(x)"]
            )
            self.assertEqual(argv[3], "--settings")
            self.assertEqual(argv[5:], ["--model", "sonnet"])
            settings = Path(argv[4])
            self.assertEqual(settings.parent.parent, Path(tmp).resolve() / "launches")
            context = json.loads((settings.parent / "context.json").read_text())
            self.assertEqual(context["resume_session_id"], "id-with-punctuation;$(x)")
            self.assertEqual(context["route"], "shared")
            self.assertIsNone(context["owner"])
            hook = json.loads(settings.read_text())["hooks"]["Stop"][0]["hooks"][0]
            self.assertEqual(
                hook["args"][-3:],
                ["hook-forward", "--context", str(settings.parent / "context.json")],
            )
            for path in (settings, settings.parent / "context.json"):
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(settings.parent.stat().st_mode & 0o777, 0o700)

    def test_rejects_options_that_disable_or_replace_hooks(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"CLAUDE_MANAGED_HOME": tmp}, clear=True),
        ):
            self._init(tmp)
            for args in (
                ["--settings", "/tmp/other.json"],
                ['--settings={"disableAllHooks":true}'],
                ["--bare"],
                ["--safe-mode"],
            ):
                with self.subTest(args=args):
                    rc, execv = self._captured_exec(["run", "--", *args])
                    self.assertEqual(rc, 2)
                    execv.assert_not_called()
            self.assertFalse(Path(tmp, "launches").exists())

    def test_route_option_parsing_errors(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"CLAUDE_MANAGED_HOME": tmp}, clear=True),
        ):
            self._init(tmp)
            for argv in (
                ["run", "--secret-file", str(Path(tmp, "secret"))],
                ["run", "--shared-route", "--webhook-url", "http://127.0.0.1:1/x"],
                ["run", "--webhook-url", "http://example.com/plain-http"],
                [
                    "resume",
                    "--shared-route",
                    "--webhook-url",
                    "http://127.0.0.1:1/x",
                    "id",
                ],
            ):
                with self.subTest(argv=argv):
                    rc, execv = self._captured_exec(argv)
                    self.assertEqual(rc, 2)
                    execv.assert_not_called()
            with self.assertRaises(SystemExit):
                cli.build_parser().parse_args(["run", "--model", "sonnet"])

    def test_route_options_parse_before_separator(self):
        with mock.patch.object(cli, "_exec_claude", return_value=0) as exec_claude:
            cli.main(
                [
                    "resume",
                    "--webhook-url",
                    "http://127.0.0.1:1/a",
                    "--secret-file",
                    "/s",
                    "the-id",
                    "--",
                    "--webhook-url",
                    "claude-arg",
                    "--",
                    "x",
                ]
            )
        args, session_id = exec_claude.call_args.args
        self.assertEqual(session_id, "the-id")
        self.assertEqual(args.webhook_url, "http://127.0.0.1:1/a")
        self.assertEqual(args.secret_file, "/s")
        self.assertEqual(args.session_id, "the-id")
        self.assertEqual(args.claude_args, ["--webhook-url", "claude-arg", "--", "x"])

    def test_context_error_at_launch_is_clean_cli_failure(self):
        from claude_managed.launch import ContextError

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"CLAUDE_MANAGED_HOME": tmp}, clear=True),
        ):
            self._init(tmp)
            with mock.patch.object(
                cli, "create_launch", side_effect=ContextError("bad context")
            ):
                rc, execv = self._captured_exec(["run"])
            self.assertEqual(rc, 2)
            execv.assert_not_called()

    def test_hook_forward_bad_arguments_exit_zero(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"HOME": tmp}, clear=True),
        ):
            for argv in (
                ["hook-forward", "--bogus"],
                ["hook-forward", "--context"],
                ["hook-forward", "--context", "a", "b"],
            ):
                with self.subTest(argv=argv):
                    self.assertEqual(cli.main(argv), 0)
            log = Path(tmp, ".local/state/claude-managed/errors.log")
            self.assertIn("unexpected hook-forward arguments", log.read_text())

    def test_invalid_timeout_does_not_create_secret(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"CLAUDE_MANAGED_HOME": tmp}, clear=False),
        ):
            self.assertEqual(cli.main(["init", "--timeout", "0"]), 2)
            self.assertFalse(Path(tmp, "secret").exists())


if __name__ == "__main__":
    unittest.main()
