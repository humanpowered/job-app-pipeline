"""
The filters that decide what reaches the scorer at all.

This is the cost control: everything that survives here is paid for. Both
directions matter, so each group tests what must pass as well as what must not.
"""
import contextlib
import csv
import io
import re
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401  -- puts src/ on the path; must come first

import scraper
from scraper import _title_excluded, _title_matches, is_application_subject


class TitleKeywords(unittest.TestCase):
    KEYWORDS = ["marketing analytics", "marketing measurement", "data scien",
                "marketing scien"]

    def match(self, title):
        return _title_matches(title, self.KEYWORDS)

    def test_stem_catches_the_longer_title(self):
        self.assertTrue(self.match("Director, Marketing Analytics"))
        self.assertTrue(self.match("Senior Manager, Marketing Analytics"))

    def test_data_scien_catches_both_words(self):
        """"data science" does not match "Data Scientist", which is why the
        stem is "data scien". Every Staff Data Scientist role was invisible
        until someone noticed."""
        self.assertTrue(self.match("Staff Data Scientist, Ads"))
        self.assertTrue(self.match("Director, Data Science"))

    def test_unrelated_titles_do_not_match(self):
        for title in ("Senior Account Director", "Warehouse Associate",
                      "Creative Director", "Registered Nurse"):
            with self.subTest(title=title):
                self.assertFalse(self.match(title))

    def test_case_and_punctuation(self):
        self.assertTrue(self.match("DIRECTOR - MARKETING ANALYTICS"))


class TitleExclusions(unittest.TestCase):
    EXCLUDES = ["intern", "entry level", "junior ", "account director"]

    def test_junior_roles_are_dropped(self):
        self.assertTrue(_title_excluded("Marketing Analytics Intern", self.EXCLUDES))
        self.assertTrue(_title_excluded("Junior Data Scientist", self.EXCLUDES))

    def test_associate_director_survives(self):
        """Exclusions are phrases on purpose. A bare "associate" would delete
        Associate Director, which is a real target level."""
        self.assertFalse(_title_excluded("Associate Director, Marketing Analytics",
                                         self.EXCLUDES))

    def test_sales_director_is_dropped_but_analytics_director_is_not(self):
        self.assertTrue(_title_excluded("Account Director", self.EXCLUDES))
        self.assertFalse(_title_excluded("Director, Marketing Analytics",
                                         self.EXCLUDES))


class ApplicationMailIsNotAJob(unittest.TestCase):
    """Replies about applications already sent were being scored as openings.
    One interview invitation reached 8/10 and had documents drafted for it."""

    REPLIES = [
        "Your application for Director, Marketing Measurement & Testing",
        "Thank you for your application to Senior Director, Analytics and Insights",
        "Head of Data and Analytics (Remote) - Confirmation of your application",
        "Thank you for applying to Acme!",
        "Thank You for Applying at Beck & Rowe",
        "Thank you for Your Interest in Northwind!",
        "Follow up regarding your Data Scientist application to Acme Corp",
        "Security code for your application to Acme",
        "Re: Acme: Scheduling the interview - October 5th",
        "Track Your Application: Redwood School Lead Head of Growth",
        "Interview confirmation: Director, Analytics",
        # A reply about a conversation that already happened. One of these
        # scored 8/10 and had a resume drafted for it, because it mentions
        # neither an application nor an interview:
        # "RE: [EXTERNAL] Thank you for the call today - <role> - <name>"
        "Thank you for the call today",
        "Thanks for your time yesterday",
        "Thank you for the conversation - next steps",
        "Thank you for our meeting",
    ]

    # Mail that mentions interviews or applications and is still not a reply.
    NOT_REPLIES = [
        "Job Alerts - How to land a job interview!",
        "You have six seconds to get a job interview!",
        "Introducing - AI Interview Buddy",
        "New Radio and Podcast Interview Guest Requests",
        "Stop spending nights on applications",
        "Acme is hiring a Director of Marketing Analytics",
        "Director, Marketing Analytics at Acme: up to $189K/year",
        "94+ Lead/Senior Data Scientist jobs (Remote)",
    ]

    def test_replies_are_recognised(self):
        for subject in self.REPLIES:
            with self.subTest(subject=subject):
                self.assertTrue(is_application_subject(subject))

    def test_postings_and_newsletters_are_left_alone(self):
        for subject in self.NOT_REPLIES:
            with self.subTest(subject=subject):
                self.assertFalse(is_application_subject(subject))

    def test_empty_subject(self):
        self.assertFalse(is_application_subject(""))
        self.assertFalse(is_application_subject(None))


class CandidateNameInAnInterviewSubject(unittest.TestCase):
    """An interview subject carries your own full name. Nothing else in an
    inbox does, which is what makes it usable."""

    def setUp(self):
        self.saved = scraper._CANDIDATE_INTERVIEW
        # Build it with the production function rather than restating the
        # pattern here. The first version of this test kept its own copy, so it
        # went on passing when the real rule grew a branch it did not have.
        scraper._CANDIDATE_INTERVIEW = scraper._candidate_name_pattern("Jordan Avery")

    def tearDown(self):
        scraper._CANDIDATE_INTERVIEW = self.saved

    def test_name_beside_interview_is_dropped(self):
        self.assertTrue(is_application_subject(
            "Acme Interview | Jordan Avery | Director Marketing Analytics"))

    def test_name_without_interview_is_kept(self):
        """A recruiter pitching a new role uses your name too."""
        self.assertFalse(is_application_subject(
            "Jordan Avery - Director Marketing Analytics opportunity"))

    def test_name_after_a_reply_prefix_is_dropped(self):
        """The shape that leaked: a thread you are already in, carrying your
        full name, about a call rather than an application."""
        for subject in (
                "RE: [EXTERNAL] Thank you for the call today - Director, "
                "Marketing Analytics - Jordan Avery",
                "Re: Director, Analytics - Jordan Avery",
                "FWD: Jordan Avery resume",
                "fw: next steps for Jordan Avery"):
            with self.subTest(subject=subject):
                self.assertTrue(is_application_subject(subject))

    def test_a_reply_prefix_without_your_name_is_kept(self):
        """Recruiters reply into threads about genuinely new roles."""
        self.assertFalse(is_application_subject(
            "Re: Director, Marketing Analytics opening at Acme"))

    def test_interview_without_the_name_is_kept(self):
        self.assertFalse(is_application_subject("How to land a job interview"))

    def test_no_profile_name_disables_only_this_rule(self):
        scraper._CANDIDATE_INTERVIEW = None
        self.assertFalse(is_application_subject(
            "Acme Interview | Jordan Avery | Director Marketing Analytics"))
        self.assertTrue(is_application_subject("Thank you for applying to Acme!"))


class DroppedPaidPostings(unittest.TestCase):
    """
    The record of what the filters threw away after it was paid for.

    This exists because "commercial analytics" was missing from titles.csv
    while boards.yaml paid Indeed to search for it, and nothing anywhere said
    so. The source just looked quiet.
    """
    KEYWORDS = ["marketing analytics", "commercial analytics"]
    EXCLUDE = ["intern", "account director"]

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.log = Path(self.tmp) / "dropped_paid_titles.csv"
        self._real = scraper.DROPPED_LOG
        scraper.DROPPED_LOG = self.log

    def tearDown(self):
        scraper.DROPPED_LOG = self._real
        shutil.rmtree(self.tmp, ignore_errors=True)

    def job(self, title, location="Remote", company="Acme"):
        return {"title": title, "location": location, "company": company,
                "url": f"https://example.com/{title}", "source": "linkedin"}

    def reason(self, title):
        return scraper.drop_reason(self.job(title), self.KEYWORDS, self.EXCLUDE)

    def test_an_unmatched_title_is_named_as_such(self):
        self.assertEqual(self.reason("Director, Digital Analytics"),
                         "no title term matched")

    def test_an_excluded_title_names_the_term_that_did_it(self):
        self.assertEqual(self.reason("Account Director, Marketing Analytics"),
                         "title excluded: account director")

    def test_exclusion_is_checked_before_the_match(self):
        """A title can fail both tests: this one matches no search term and is
        also excluded. The exclusion is the more specific answer -- reporting
        'no title term matched' would send someone off to add a term for a
        posting that an exclusion was always going to reject anyway.

        The title has to fail both for this to test anything. "Marketing
        Analytics Intern" matches a term and is excluded, so it returns the
        same answer whichever test runs first."""
        self.assertEqual(self.reason("Account Director, Digital Analytics"),
                         "title excluded: account director")

    def test_anything_the_title_tests_allow_is_blamed_on_location(self):
        self.assertEqual(self.reason("Director, Marketing Analytics"),
                         "location")

    def test_only_the_dropped_postings_come_back(self):
        kept = self.job("Director, Marketing Analytics")
        billed = [kept, self.job("Director, Digital Analytics")]
        dropped = scraper.log_dropped("linkedin", billed, [kept],
                                      self.KEYWORDS, self.EXCLUDE)
        self.assertEqual([j["title"] for j in dropped],
                         ["Director, Digital Analytics"])

    def test_two_postings_with_the_same_title_are_told_apart(self):
        """LinkedIn returns one posting per city, so identical titles are
        normal. Matching on the title would mark the kept copy as dropped."""
        first = self.job("Director, Marketing Analytics", location="Remote")
        second = self.job("Director, Marketing Analytics", location="Tokyo")
        dropped = scraper.log_dropped("linkedin", [first, second], [first],
                                      self.KEYWORDS, self.EXCLUDE)
        self.assertEqual(len(dropped), 1)
        self.assertEqual(dropped[0]["location"], "Tokyo")

    def test_nothing_dropped_writes_no_file(self):
        kept = self.job("Director, Marketing Analytics")
        self.assertEqual(scraper.log_dropped("linkedin", [kept], [kept],
                                             self.KEYWORDS, self.EXCLUDE), [])
        self.assertFalse(self.log.exists())

    def test_the_log_gets_a_header_and_a_row_per_drop(self):
        scraper.log_dropped("indeed", [self.job("Director, Digital Analytics")],
                            [], self.KEYWORDS, self.EXCLUDE)
        rows = list(csv.reader(self.log.read_text(encoding="utf-8").splitlines()))
        self.assertEqual(rows[0][:3], ["date", "source", "reason"])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][1], "indeed")
        self.assertEqual(rows[1][3], "Director, Digital Analytics")

    def test_a_second_run_appends_rather_than_replacing(self):
        for title in ("Director, Digital Analytics", "VP, Growth Analytics"):
            scraper.log_dropped("linkedin", [self.job(title)], [],
                                self.KEYWORDS, self.EXCLUDE)
        rows = list(csv.reader(self.log.read_text(encoding="utf-8").splitlines()))
        self.assertEqual(len(rows), 3)

    def test_rows_past_the_keep_window_are_pruned(self):
        stale = (datetime.now() - timedelta(days=scraper.DROPPED_KEEP_DAYS + 5))
        with open(self.log, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["date", "source", "reason", "title", "company",
                        "location", "url"])
            w.writerow([stale.strftime("%Y-%m-%d"), "linkedin", "location",
                        "Old Role", "Acme", "Remote", ""])
        scraper.log_dropped("linkedin", [self.job("Director, Digital Analytics")],
                            [], self.KEYWORDS, self.EXCLUDE)
        titles = [r[3] for r in
                  csv.reader(self.log.read_text(encoding="utf-8").splitlines())][1:]
        self.assertEqual(titles, ["Director, Digital Analytics"])

    def test_a_log_that_cannot_be_written_does_not_lose_the_run(self):
        """The postings were already paid for. A logging failure must not be
        the thing that discards them."""
        scraper.DROPPED_LOG = Path(self.tmp) / "nope" / "x.csv"
        with mock.patch("builtins.open", side_effect=OSError("read-only")):
            dropped = scraper.log_dropped(
                "linkedin", [self.job("Director, Digital Analytics")], [],
                self.KEYWORDS, self.EXCLUDE)
        self.assertEqual(len(dropped), 1)

    def report(self, dropped):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            scraper.report_dropped("linkedin", dropped)
        return out.getvalue()

    def test_the_report_counts_each_reason_separately(self):
        billed = [self.job("Director, Digital Analytics"),
                  self.job("Account Director, Marketing Analytics"),
                  self.job("Director, Marketing Analytics", location="Tokyo")]
        text = self.report(scraper.log_dropped("linkedin", billed, [],
                                               self.KEYWORDS, self.EXCLUDE))
        self.assertIn("3 billed posting(s) dropped", text)
        self.assertIn("1 no title term", text)
        self.assertIn("1 excluded title", text)
        self.assertIn("1 location", text)

    def test_unmatched_titles_are_named_and_location_drops_are_not(self):
        """An unmatched title is a candidate for titles.csv, so it belongs in
        front of someone. A location drop is nothing to act on."""
        billed = [self.job("Director, Digital Analytics"),
                  self.job("Director, Marketing Analytics", location="Tokyo")]
        text = self.report(scraper.log_dropped("linkedin", billed, [],
                                               self.KEYWORDS, self.EXCLUDE))
        self.assertIn("no term matched: Director, Digital Analytics", text)
        self.assertNotIn("no term matched: Director, Marketing Analytics", text)

    def test_the_same_unmatched_title_is_named_once(self):
        billed = [self.job("Director, Digital Analytics", company="Acme"),
                  self.job("Director, Digital Analytics", company="Globex")]
        text = self.report(scraper.log_dropped("linkedin", billed, [],
                                               self.KEYWORDS, self.EXCLUDE))
        self.assertEqual(text.count("no term matched:"), 1)

    def test_a_long_list_is_capped_and_says_so(self):
        billed = [self.job(f"Director, Thing {i} Analysis") for i in range(9)]
        text = self.report(scraper.log_dropped("linkedin", billed, [],
                                               self.KEYWORDS, self.EXCLUDE))
        self.assertEqual(text.count("no term matched:"), 6)
        self.assertIn("and 3 more distinct title(s)", text)

    def test_nothing_dropped_prints_nothing(self):
        self.assertEqual(self.report([]), "")


if __name__ == "__main__":
    unittest.main()
