import unittest

from whyslow.redact import MASK, MAX_CMDLINE_CHARS, redact_argv, redact_cmdline, redact_text

# Fake credentials, built by concatenation so secret scanners don't flag this file.
GH = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
AWS = "AKIA" + "IOSFODNN7EXAMPLE"
SLACK = "xoxb-" + "123456789012-abcdefABCDEF"
ANTHROPIC = "sk-ant-" + "api03-Zx9Yw8Vu7Ts6Rq5Po4Nm3Lk2"
STRIPE = "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc"
GOOGLE = "AIza" + "SyA-1234567890abcdefghijklmnopqrstu"
JWT = "eyJhbGciOiJIUzI1NiJ9" + ".eyJzdWIiOiIxMjM0NTY3ODkwIn0" + ".dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
HEX = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
B64 = "dGhpc0lzQVZlcnlTZWNyZXRWYWx1ZTEyMzQ1Njc4OTA="


class LeakCase(unittest.TestCase):
    def assertNoLeak(self, secret: str, output: str) -> None:
        self.assertNotIn(secret, output, f"secret leaked: {output!r}")


class TestFlags(LeakCase):
    def test_flag_equals(self):
        for flag in ("--password", "--db-password", "--api-key", "--apikey", "--token",
                     "--client-secret", "--auth", "--access_key", "-password", "--PASSWORD"):
            with self.subTest(flag=flag):
                out = redact_argv(["tool", f"{flag}=hunter2"])
                self.assertEqual(out, ["tool", f"{flag}={MASK}"])

    def test_flag_separate_value(self):
        out = redact_argv(["mysqldump", "--password", "hunter2", "--user", "root", "db"])
        self.assertEqual(out, ["mysqldump", "--password", MASK, "--user", "root", "db"])

    def test_flag_followed_by_another_flag_is_not_swallowed(self):
        out = redact_argv(["tool", "--auth", "--verbose"])
        self.assertEqual(out, ["tool", "--auth", "--verbose"])

    def test_harmless_flags_untouched(self):
        argv = ["/usr/bin/python3", "-m", "http.server", "--port=8000", "--bind", "127.0.0.1", "-v"]
        self.assertEqual(redact_argv(argv), argv)


class TestText(LeakCase):
    def test_key_value_pairs(self):
        for text in ("token=hunter2", "API_KEY=hunter2", "DB_PASSWORD=hunter2", "secret: hunter2",
                     "https://api.example.com/v1?user=bob&access_token=hunter2&x=1",
                     'password="hunter2 with spaces"', "sessionid=hunter2;path=/"):
            with self.subTest(text=text):
                self.assertNoLeak("hunter2", redact_text(text))

    def test_kv_keeps_surrounding_query(self):
        out = redact_text("https://x.test/cb?user=bob&token=hunter2&page=3")
        self.assertEqual(out, f"https://x.test/cb?user=bob&token={MASK}&page=3")

    def test_connection_strings(self):
        for dsn in ("postgres://app:s3cr3tPw@db.internal:5432/prod",
                    "mongodb+srv://admin:s3cr3tPw@cluster0.mongodb.net/test",
                    "redis://:s3cr3tPw@localhost:6379/0",
                    "amqp://guest:s3cr3tPw@rabbit/"):
            with self.subTest(dsn=dsn):
                out = redact_text(dsn)
                self.assertNoLeak("s3cr3tPw", out)
                self.assertIn("@", out)  # host is kept for debugging

    def test_ado_style_connection_string(self):
        out = redact_text("Server=db;Database=prod;User Id=sa;Password=s3cr3tPw;")
        self.assertNoLeak("s3cr3tPw", out)

    def test_token_as_url_username(self):
        out = redact_text(f"https://{GH}@github.com/me/repo.git")
        self.assertNoLeak(GH, out)
        self.assertIn("github.com/me/repo.git", out)

    def test_bearer_and_basic(self):
        for text in ("Authorization: Bearer abc.def-ghi_jkl", "Bearer abc.def-ghi_jkl",
                     "Authorization: Basic dXNlcjpwYXNzd29yZA==", "Authorization%3A%20Bearer%20abc.def-ghi_jkl"):
            with self.subTest(text=text):
                out = redact_text(text)
                self.assertNoLeak("abc.def-ghi_jkl", out)
                self.assertNoLeak("dXNlcjpwYXNzd29yZA", out)

    def test_known_token_formats(self):
        for token in (GH, AWS, SLACK, ANTHROPIC, STRIPE, GOOGLE, JWT, "github_pat_" + "11ABCDEFG0123456789_abcdefghij"):
            with self.subTest(token=token[:8]):
                self.assertNoLeak(token, redact_text(f"--header=X {token} trailing"))
                self.assertNoLeak(token, " ".join(redact_argv(["tool", token])))

    def test_long_hex_and_base64_blobs(self):
        self.assertNoLeak(HEX, redact_text(f"run {HEX}"))
        self.assertNoLeak(B64, redact_text(f"--data {B64}"))

    def test_pem_private_key(self):
        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow...secretstuff\n-----END RSA PRIVATE KEY-----"
        self.assertNoLeak("secretstuff", redact_text(pem))

    def test_paths_and_ordinary_args_are_not_mangled(self):
        for text in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                     "/System/Library/PrivateFrameworks/SkyLight.framework/Resources/WindowServer",
                     "/Users/me/Library/Application Support/Code/User/workspaceStorage/abc",
                     "com.apple.WebKit.WebContent", "--type=renderer", "550e8400-e29b-41d4-a716-446655440000",
                     "--lang=en-US", "-psn_0_12345"):
            with self.subTest(text=text):
                self.assertEqual(redact_text(text), text)


class TestCmdline(LeakCase):
    def test_name_only_mode_returns_nothing(self):
        self.assertIsNone(redact_cmdline(["curl", "-H", "Authorization: Bearer abcdefgh123"], "name_only"))

    def test_redact_mode_joins_and_masks(self):
        out = redact_cmdline(["psql", "postgres://u:s3cr3tPw@h/db", "--password", "hunter2"], "redact")
        self.assertNoLeak("s3cr3tPw", out)
        self.assertNoLeak("hunter2", out)
        self.assertTrue(out.startswith("psql "))

    def test_args_with_spaces_are_quoted(self):
        self.assertEqual(redact_cmdline(["/Applications/My App.app/x", "-v"], "redact"), '"/Applications/My App.app/x" -v')

    def test_empty(self):
        self.assertIsNone(redact_cmdline([], "redact"))
        self.assertIsNone(redact_cmdline(None, "redact"))

    def test_truncation_happens_after_redaction(self):
        argv = ["tool"] + ["x" * 50] * 40 + [f"--token={GH}"]
        out = redact_cmdline(argv, "redact")
        self.assertLessEqual(len(out), MAX_CMDLINE_CHARS)
        self.assertNoLeak(GH, out)
        # A secret straddling the cut must not survive partially either.
        argv = ["tool", "a" * (MAX_CMDLINE_CHARS - 20), GH]
        self.assertNotIn(GH[:12], redact_cmdline(argv, "redact"))


if __name__ == "__main__":
    unittest.main()
