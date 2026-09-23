import unittest

from whyslow.daemon import is_sampler_cmdline


class TestSamplerIdentity(unittest.TestCase):
    def test_accepts_the_sampler(self):
        self.assertTrue(is_sampler_cmdline(["/usr/bin/python3", "-m", "whyslow", "run", "--background"]))
        self.assertTrue(is_sampler_cmdline(["/opt/x/python", "-m", "whyslow", "--config", "c.toml", "run"]))
        self.assertTrue(is_sampler_cmdline(["/usr/bin/python3", "/Users/me/whyslow/.venv/bin/whyslow", "run"]))

    def test_rejects_lookalikes(self):
        for argv in (
            ["Cursor Helper (Plugin): extension-host (user) whyslow [1-5]"],
            ["/usr/bin/vim", "/Users/me/whyslow/README.md"],
            ["/usr/bin/python3", "-m", "whyslow", "top"],
            ["/usr/bin/python3", "-m", "notwhyslow", "run"],
            ["bash", "-c", "whyslow run"],
            [],
        ):
            with self.subTest(argv=argv):
                self.assertFalse(is_sampler_cmdline(argv))


if __name__ == "__main__":
    unittest.main()
