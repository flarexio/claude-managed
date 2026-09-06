import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_managed import cli
from claude_managed.config import Config
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

    def test_exec_preserves_resume_id_and_does_not_use_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = root / "settings.json"
            settings.write_text("{}")
            config = Config(
                webhook_url="http://127.0.0.1:8644/events",
                secret_file=root / "secret",
                settings_file=settings,
                error_log=root / "errors.log",
                timeout_seconds=1,
                allowed_cwd_roots=(root,),
                allowed_transcript_roots=(root,),
            )
            with (
                mock.patch.object(cli, "load_config", return_value=config),
                mock.patch.object(cli.shutil, "which", return_value="/usr/bin/claude"),
                mock.patch.object(
                    cli.os, "execv", side_effect=RuntimeError("captured")
                ) as execv,
                self.assertRaisesRegex(RuntimeError, "captured"),
            ):
                cli._exec_claude(["--", "--model", "sonnet"], "id-with-punctuation")
            execv.assert_called_once_with(
                "/usr/bin/claude",
                [
                    "/usr/bin/claude",
                    "--resume",
                    "id-with-punctuation",
                    "--settings",
                    str(settings),
                    "--model",
                    "sonnet",
                ],
            )

    def test_rejects_options_that_disable_or_replace_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = root / "settings.json"
            settings.write_text("{}")
            config = Config(
                webhook_url="http://127.0.0.1:8644/events",
                secret_file=root / "secret",
                settings_file=settings,
                error_log=root / "errors.log",
                timeout_seconds=1,
                allowed_cwd_roots=(root,),
                allowed_transcript_roots=(root,),
            )
            for args in (
                ["--settings", "/tmp/other.json"],
                ['--settings={"disableAllHooks":true}'],
                ["--bare"],
                ["--safe-mode"],
            ):
                with (
                    self.subTest(args=args),
                    mock.patch.object(cli, "load_config", return_value=config),
                    self.assertRaisesRegex(ValueError, "not allowed"),
                ):
                    cli._exec_claude(args)

    def test_invalid_timeout_does_not_create_secret(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"CLAUDE_MANAGED_HOME": tmp}, clear=False),
        ):
            self.assertEqual(cli.main(["init", "--timeout", "0"]), 2)
            self.assertFalse(Path(tmp, "secret").exists())


if __name__ == "__main__":
    unittest.main()
