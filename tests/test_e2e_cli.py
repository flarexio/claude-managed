import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from support import LAUNCHER, Receiver, clean_env, install_fake_claude


class EndToEndCliTests(unittest.TestCase):
    def test_init_doctor_hook_and_fake_claude_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            transcripts = root / "transcripts"
            projects.mkdir()
            transcripts.mkdir()
            install_fake_claude(root)
            args_file = root / "claude-args.json"
            receiver = Receiver()
            env = clean_env(
                root,
                CLAUDE_MANAGED_HOME=str(root / "config"),
                FAKE_CLAUDE_ARGS=str(args_file),
            )
            try:
                subprocess.run(
                    LAUNCHER
                    + [
                        "init",
                        "--webhook-url",
                        receiver.url("/events"),
                        "--cwd-root",
                        str(projects),
                        "--transcript-root",
                        str(transcripts),
                    ],
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                doctor = subprocess.run(
                    LAUNCHER + ["doctor"],
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                self.assertIn("OK   webhook", doctor.stdout)

                payload = {
                    "hook_event_name": "Stop",
                    "cwd": str(projects / "repo"),
                    "transcript_path": str(transcripts / "session.jsonl"),
                }
                raw = json.dumps(payload).encode()
                # Legacy unbound hook outside Hermes keeps working.
                subprocess.run(
                    LAUNCHER + ["hook-forward"], env=env, input=raw, check=True
                )
                self.assertEqual(receiver.bodies(), [raw])

                subprocess.run(
                    LAUNCHER
                    + ["resume", "literal-session-id", "--", "--model", "sonnet"],
                    env=env,
                    check=True,
                )
                argv = json.loads(args_file.read_text())
                self.assertEqual(argv[0:2], ["--resume", "literal-session-id"])
                self.assertEqual(argv[-2:], ["--model", "sonnet"])
                settings_path = Path(argv[argv.index("--settings") + 1])
                self.assertEqual(settings_path.parent.parent, root / "config/launches")
            finally:
                receiver.close()


if __name__ == "__main__":
    unittest.main()
