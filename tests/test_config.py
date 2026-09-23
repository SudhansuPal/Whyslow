import tempfile
import unittest
from pathlib import Path

from whyslow import config
from whyslow.config import Config, ConfigError


class TestConfig(unittest.TestCase):
    def load_text(self, text: str) -> Config:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.toml"
            p.write_text(text)
            return config.load(p)

    def test_missing_file_gives_defaults(self):
        self.assertEqual(config.load(Path("/nonexistent/whyslow.toml")), Config())

    def test_valid_overrides(self):
        cfg = self.load_text('[sampling]\ninterval_seconds = 2\n[privacy]\ncmdline = "name_only"\n')
        self.assertEqual(cfg.sampling.interval_seconds, 2.0)
        self.assertIsInstance(cfg.sampling.interval_seconds, float)
        self.assertEqual(cfg.privacy.cmdline, "name_only")
        self.assertEqual(cfg.sampling.process_top_n, Config().sampling.process_top_n)

    def test_rejections(self):
        bad = {
            "[nope]\nx = 1": "unknown section",
            "[sampling]\nnope = 1": "unknown key",
            "[sampling]\ninterval_seconds = 0": "outside the allowed range",
            '[sampling]\ninterval_seconds = "1"': "expected a number",
            "[sampling]\ninterval_seconds = true": "expected a number",
            "[sampling]\nprocess_top_n = 2.5": "expected an integer",
            "[sampling]\nsystem_process_visibility = 1": "expected true/false",
            '[privacy]\ncmdline = "full"': "not one of",
            "[dashboard]\nport = 80": "outside the allowed range",
            "sampling = 3": "must be a table",
            "[sampling\n": "invalid TOML",
        }
        for text, message in bad.items():
            with self.subTest(text=text):
                with self.assertRaises(ConfigError) as ctx:
                    self.load_text(text)
                self.assertIn(message, str(ctx.exception))

    def test_effective_config_round_trips(self):
        cfg = self.load_text("[storage]\nretention_days = 3\n")
        self.assertEqual(self.load_text(config.as_toml(cfg)), cfg)


if __name__ == "__main__":
    unittest.main()
