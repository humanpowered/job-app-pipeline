"""
The closing verdict of the nightly run.

The lines below are copied from the two runs that ended "[OK] pipeline
completed" with nothing working: 2026-10-14, when every source failed DNS, and
the first run of 2026-10-15, when six credentials held a stray Ctrl+V. The
verdict is a pure function of the run's output and two exit codes, so no step
is run here.
"""
import unittest

import helpers  # noqa: F401  -- puts src/ on the path; must come first

from run_nightly import verdict

FOUND = "  found 38 postings matching your keyword/location filters"
FOUND_NONE = "  found 0 postings matching your keyword/location filters"
SCORED = "[8/10] Director, Marketing Analytics @ Acme"
SCORING_FAILED = ("[skip] scoring failed for Senior Data Scientist, Audio @ Deepgram: "
                  "BadRequestError: Error code: 400")
READY_REPORT = "   [7/10] Acme - Director, Marketing Analytics"   # tracker, indented

JOOBLE_403 = ("[warn] jooble query 'marketing analytics' failed: 403 Client Error: "
              "Forbidden for url: https://jooble.org/api/%16")
ADZUNA_401 = ("[warn] adzuna query 'marketing science' failed: Adzuna returned 401 "
              "for query 'marketing science'")
APIFY_400 = ("[warn] apify_linkedin failed: RuntimeError: Apify returned 400 for "
             "search 'remote marketing analytics / United States': ")
IMAP_REFUSED = ("[warn] email source failed: error: [AUTHENTICATIONFAILED] Invalid "
                "credentials (Failure)")
GREENHOUSE_404 = ("[warn] greenhouse/remotecom failed: 404 Client Error: Not Found "
                  "for url: https://boards-api.greenhouse.io/v1/boards/remotecom/jobs")
DNS = ("[warn] greenhouse/acme failed: HTTPSConnectionPool(host='boards-api."
       "greenhouse.io', port=443): Max retries exceeded")

HEALTHY = [FOUND, SCORED, SCORED, READY_REPORT]


class Verdict(unittest.TestCase):
    def test_a_healthy_run_is_ok(self):
        self.assertEqual(verdict(HEALTHY + [GREENHOUSE_404], 0, 0),
                         ("[OK] pipeline completed", 0))

    def test_pipeline_exit_code_still_decides_first(self):
        line, code = verdict(HEALTHY, 2, 0)
        self.assertTrue(line.startswith("[ERROR]"))
        self.assertEqual(code, 2)

    def test_every_source_failing_dns_is_an_error(self):
        """2026-10-14: no network, nothing found, and the run said OK."""
        line, code = verdict([DNS, DNS, FOUND_NONE], 0, 1)
        self.assertTrue(line.startswith("[ERROR]"), line)
        self.assertIn("no source returned any postings", line)
        self.assertNotEqual(code, 0)

    def test_every_scoring_call_failing_is_an_error(self):
        """2026-10-15: both postings scored got a 400 from a corrupted key."""
        lines = [FOUND, SCORING_FAILED, SCORING_FAILED, READY_REPORT]
        line, code = verdict(lines, 0, 0)
        self.assertTrue(line.startswith("[ERROR]"), line)
        self.assertIn("2 of 2", line)
        self.assertNotEqual(code, 0)

    def test_some_scoring_failures_are_not_an_error(self):
        line, code = verdict([FOUND, SCORED, SCORING_FAILED], 0, 0)
        self.assertEqual((line, code), ("[OK] pipeline completed", 0))

    def test_nothing_new_to_score_is_not_a_scoring_failure(self):
        self.assertEqual(verdict([FOUND, READY_REPORT], 0, 0)[1], 0)

    def test_keyed_sources_refusing_credentials_is_a_warning(self):
        lines = HEALTHY + [JOOBLE_403, JOOBLE_403, ADZUNA_401, APIFY_400, IMAP_REFUSED]
        line, code = verdict(lines, 0, 0)
        self.assertTrue(line.startswith("[OK with WARNING]"), line)
        self.assertIn("jooble, adzuna, apify_linkedin, email refused the credentials", line)
        self.assertEqual(code, 0)

    def test_a_dead_board_token_is_not_a_credential_failure(self):
        line, _ = verdict(HEALTHY + [GREENHOUSE_404], 0, 0)
        self.assertNotIn("WARNING", line)

    def test_reply_check_crashing_is_a_warning(self):
        """2026-10-15: check_replies died on an IMAP login and nothing said so."""
        line, code = verdict(HEALTHY, 0, 1)
        self.assertTrue(line.startswith("[OK with WARNING]"), line)
        self.assertIn("reply check failed", line)
        self.assertEqual(code, 0)

    def test_locked_tracker_is_still_a_warning(self):
        line, _ = verdict(HEALTHY + ["[TRACKER LOCKED] wrote .NEW.csv"], 0, 0)
        self.assertIn("tracker was locked", line)


if __name__ == "__main__":
    unittest.main()
