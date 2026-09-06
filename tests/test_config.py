import tempfile
import unittest
from pathlib import Path

from claude_managed.config import load_config, validate_url, write_config


class ConfigTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret = root / "secret"
            settings = root / "settings.json"
            write_config(
                root=root,
                webhook_url="http://127.0.0.1:8644/webhooks/events",
                secret_file=secret,
                settings_file=settings,
                error_log=root / "errors.log",
                timeout_seconds=3.5,
                cwd_roots=[root / "projects"],
                transcript_roots=[root / "transcripts"],
            )
            config = load_config(root)
            self.assertEqual(config.timeout_seconds, 3.5)
            self.assertEqual(config.secret_file, secret)
            self.assertEqual(config.allowed_cwd_roots, ((root / "projects").resolve(),))

    def test_rejects_plain_http_off_loopback(self):
        with self.assertRaisesRegex(ValueError, "loopback"):
            validate_url("http://example.com/webhook")

    def test_accepts_https_remote(self):
        validate_url("https://example.com/webhook")

    def test_round_trip_escapes_control_characters(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unusual = root / "project\tname\nline"
            write_config(
                root=root,
                webhook_url="http://127.0.0.1:8644/webhooks/events",
                secret_file=root / "secret",
                settings_file=root / "settings.json",
                error_log=root / "errors.log",
                timeout_seconds=3.5,
                cwd_roots=[unusual],
                transcript_roots=[root / "transcripts"],
            )
            config = load_config(root)
            self.assertEqual(config.allowed_cwd_roots, (unusual.resolve(),))


if __name__ == "__main__":
    unittest.main()
