"""
doctor's credential check.

On 2026-10-15 six of seven user-scope credentials held a single Ctrl+V
character (0x16), and doctor reported every one as set. The environment and
the registry are both replaced here, so nothing real is read.
"""
import contextlib
import io
import os
import unittest
from unittest import mock

import helpers  # noqa: F401  -- puts src/ on the path; must come first

import doctor

GOOD = {
    "ANTHROPIC_API_KEY": "sk-ant-api03-abcdefghijklmnop",
    "JOOBLE_API_KEY": "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0",
    "ADZUNA_APP_ID": "abc12345",
    "ADZUNA_APP_KEY": "0123456789abcdef0123456789abcdef",
    "APIFY_TOKEN": "apify_api_ABCDEFGHIJKLMNOP",
    "IMAP_USER": "jordan@example.com",
    "IMAP_APP_PASSWORD": "abcd efgh ijkl mnop",
}
CTRL_V = "\x16"


class Credentials(unittest.TestCase):
    def check(self, shell: dict, user: dict | None = None):
        """Run check_credentials with this shell environment and this user
        (registry) environment."""
        user = user or {}
        doctor._problems.update(fail=0, warn=0)
        buffer = io.StringIO()
        with mock.patch.dict(os.environ, shell, clear=True), \
                mock.patch.object(doctor, "user_env", lambda n: user.get(n, "")), \
                contextlib.redirect_stdout(buffer):
            have = doctor.check_credentials()
        return buffer.getvalue(), dict(doctor._problems), have

    def test_good_credentials_pass(self):
        out, problems, have = self.check(GOOD)
        self.assertEqual(problems, {"fail": 0, "warn": 0}, out)
        self.assertTrue(all(have.values()), have)

    def test_a_pasted_ctrl_v_fails(self):
        """The 2026-10-15 state: every key but one was a lone 0x16."""
        broken = {k: CTRL_V for k in GOOD if k != "IMAP_USER"}
        out, problems, have = self.check({}, {**GOOD, **broken})
        self.assertEqual(problems["fail"], 7, out)    # six keys plus "no usable Anthropic"
        self.assertFalse(have["anthropic"])
        self.assertFalse(any(have[k] for k in ("jooble", "adzuna", "apify", "imap")))
        self.assertNotIn(CTRL_V, out)
        self.assertIn("length 1", out)

    def test_ctrl_v_in_the_registry_fails_even_when_the_shell_is_fine(self):
        """The nightly run reads the user environment, not this shell."""
        out, problems, have = self.check(GOOD, {**GOOD, "APIFY_TOKEN": CTRL_V})
        self.assertEqual(problems["fail"], 1, out)
        self.assertIn("APIFY_TOKEN in your user environment is not usable", out)
        self.assertFalse(have["apify"])

    def test_surrounding_whitespace_fails(self):
        out, problems, have = self.check({**GOOD, "JOOBLE_API_KEY": " secretvalue123 "})
        self.assertEqual(problems["fail"], 1, out)
        self.assertIn("whitespace", out)
        self.assertFalse(have["jooble"])

    def test_no_part_of_a_value_is_ever_printed(self):
        secret = "sk-ant-SECRETPART"
        out, _, _ = self.check({**GOOD, "ANTHROPIC_API_KEY": secret + "\n",
                                "IMAP_USER": "SECRETUSER",
                                "IMAP_APP_PASSWORD": "SECRETPW"})
        for fragment in ("SECRET", "sk-ant-S", "jordan", "abcd"):
            self.assertNotIn(fragment, out)

    def test_implausible_shapes_warn(self):
        out, problems, have = self.check({**GOOD,
                                          "ANTHROPIC_API_KEY": "abc123",
                                          "IMAP_USER": "jordan",
                                          "IMAP_APP_PASSWORD": "my-real-password1"})
        self.assertEqual(problems["fail"], 0, out)
        self.assertEqual(problems["warn"], 3, out)
        self.assertIn("sk-ant-", out)
        self.assertIn("full email address", out)
        self.assertIn("16 letters", out)

    def test_key_only_in_the_registry_is_still_reported_ok(self):
        """Claude Code strips ANTHROPIC_API_KEY from this shell; the registry
        is the truth for the nightly run."""
        shell = {k: v for k, v in GOOD.items() if k != "ANTHROPIC_API_KEY"}
        out, problems, have = self.check(shell, {"ANTHROPIC_API_KEY": GOOD["ANTHROPIC_API_KEY"]})
        self.assertEqual(problems["fail"], 0, out)
        self.assertTrue(have["anthropic"])
        self.assertIn("set in your user environment", out)


if __name__ == "__main__":
    unittest.main()
