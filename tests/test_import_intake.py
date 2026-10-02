"""
Parsing a filled intake document into a master record.

Every quirk below came out of a real document, which is the only reason to
believe the parser handles real documents. A .docx is a zip holding
word/document.xml, so the fixtures are built the same way rather than checked in
as binaries.
"""
import re
import tempfile
import unittest
import zipfile
from pathlib import Path

import helpers  # noqa: F401  -- puts src/ on the path; must come first

import import_intake
from import_intake import paragraphs, parse, render

TEMPLATE = (
    '<?xml version="1.0"?><w:document '
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    "<w:body>{body}</w:body></w:document>")


def make_docx(lines, path):
    body = "".join(
        f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in lines)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", TEMPLATE.format(body=body))
    return path


class ReadingTheFile(unittest.TestCase):
    def test_paragraphs_come_back_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_docx(["first", "second", "third"], Path(tmp) / "a.docx")
            self.assertEqual(paragraphs(p), ["first", "second", "third"])

    def test_entities_are_decoded(self):
        """Word writes & as &amp;, which reached a profile as "P&amp;L"."""
        with tempfile.TemporaryDirectory() as tmp:
            p = make_docx(["Budget &amp; P&amp;L"], Path(tmp) / "a.docx")
            self.assertEqual(paragraphs(p), ["Budget & P&L"])

    def test_a_file_that_is_not_a_docx_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "resume.docx"
            bad.write_text("this is a PDF or a .doc, not a .docx", encoding="utf-8")
            with self.assertRaises(SystemExit) as caught:
                paragraphs(bad)
            self.assertIn("not a readable .docx", str(caught.exception))


ROLE = [
    "Employment History",
    "1. Job Title: Head of Analytics From: 11/2020 To: 12/2022",
    "Organization: Northwind Retail",
    "Location: Portland, OR",
    "What is the size of this company ($ of sales)? What are its products/services?"
    " What markets does it service?",
    "$1bn annual revenue",
    "What was your major challenge? Outline the key problem(s) you were hired to resolve.",
    "No analytics function on staff.",
    "Your level of authority - number of people/departments managed?",
    "Senior leader with 2 staff",
    "Size of territory? USA and Canada",
    "How much was your Budget/Financial/P&amp;L responsibility?",
    "NA",
    "Position you reported to? VP, Growth Marketing",
    "Major responsibilities and day-to-day duties:",
    "Lead data strategy.",
    "Conduct administrative tasks.",
    "Did you receive any awards or special recognition?",
    "Win of the week",
    "Describe as many relevant accomplishments as possible in this role:",
    "Short Title: Predictive lifetime value",
    "Problem or Situation: Day 1 spending underestimated returns.",
    "Actions: Built an ML LTV prediction.",
    "Quantifiable Results: 30% increase in marginal profits.",
    "Short Title:No space after the colon",
    "Problem or Situation: It happens in real documents.",
    "Actions: Handle it.",
    "Quantifiable Results: Parsed correctly.",
    "Short Title:",
    "Problem or Situation:",
    "Actions:",
    "Quantifiable Results:",
]


def parsed(lines):
    with tempfile.TemporaryDirectory() as tmp:
        return parse(paragraphs(make_docx(lines, Path(tmp) / "a.docx")))


class RoleFields(unittest.TestCase):
    def setUp(self):
        self.role = parsed(ROLE)["roles"][0]

    def test_header_splits_title_and_dates(self):
        self.assertEqual(self.role["title"], "Head of Analytics")
        self.assertEqual(self.role["start"], "11/2020")
        self.assertEqual(self.role["end"], "12/2022")

    def test_inline_answers(self):
        self.assertEqual(self.role["organization"], "Northwind Retail")
        self.assertEqual(self.role["territory"], "USA and Canada")
        self.assertEqual(self.role["reports_to"], "VP, Growth Marketing")

    def test_answers_on_the_following_line(self):
        self.assertEqual(self.role["authority"], "Senior leader with 2 staff")
        self.assertEqual(self.role["budget"], "NA")

    def test_multi_sentence_questions_do_not_bleed_into_the_answer(self):
        """The company and challenge questions run to several sentences. Stopping
        at the first "?" put the rest of the question into the answer."""
        self.assertEqual(self.role["company"], "$1bn annual revenue")
        self.assertEqual(self.role["challenge"], "No analytics function on staff.")
        for value in (self.role["company"], self.role["challenge"]):
            self.assertNotIn("?", value)
            self.assertNotIn("Outline", value)

    def test_list_answers_accumulate(self):
        self.assertEqual(self.role["responsibilities"],
                         ["Lead data strategy.", "Conduct administrative tasks."])


class Accomplishments(unittest.TestCase):
    def setUp(self):
        self.accs = parsed(ROLE)["roles"][0]["accomplishments"]

    def test_filled_blocks_are_captured_with_all_three_parts(self):
        self.assertEqual(len(self.accs), 2)
        first = self.accs[0]
        self.assertEqual(first["title"], "Predictive lifetime value")
        self.assertEqual(first["results"], "30% increase in marginal profits.")
        self.assertTrue(first["problem"] and first["actions"])

    def test_a_missing_space_after_the_colon(self):
        self.assertEqual(self.accs[1]["title"], "No space after the colon")

    def test_empty_template_blocks_are_dropped(self):
        """A real document left seven blank blocks at the end of one role."""
        self.assertTrue(all(a.get("title") for a in self.accs))


class ThinAndMalformedRoles(unittest.TestCase):
    def test_a_role_with_no_title_or_dates_still_parses(self):
        """Two of six roles in the source document had an empty header, with the
        organisation underneath as the only identifier."""
        doc = parsed(["Employment History",
                      "2. Job Title: From: To:",
                      "Organization: Redwood Research Group",
                      "Location: Redondo Beach, CA"])
        role = doc["roles"][0]
        self.assertEqual(role["organization"], "Redwood Research Group")
        self.assertEqual(role["title"], "")

    def test_an_entirely_empty_role_block_is_not_emitted(self):
        doc = parsed(["Employment History", "5. Job Title: From: To:",
                      "6. Job Title: From: To:"])
        self.assertEqual(doc["roles"], [])

    def test_the_answer_about_having_no_accomplishments_is_kept(self):
        """One role answered the accomplishments question "All in resume". That
        is the most useful line about that role and it was landing in Unparsed."""
        doc = parsed(["Employment History", "1. Job Title: Analyst From: a To: b",
                      "Organization: Acme",
                      "Describe as many relevant accomplishments as possible in this role:",
                      "All in resume"])
        role = doc["roles"][0]
        self.assertEqual(role["accomplishments"], [])
        self.assertEqual(role["accomplishments_note"], "All in resume")
        self.assertNotIn("All in resume", doc["unparsed"])


class NothingIsSilentlyDropped(unittest.TestCase):
    def test_unrecognised_text_is_kept(self):
        """A pasted job description sat in the middle of the real document. A
        parser that discards what it does not understand looks like one that
        works."""
        doc = parsed(["Employment History", "1. Job Title: Analyst From: a To: b",
                      "Organization: Acme", "Describe as many relevant accomplishments"
                      " as possible in this role:", "Short Title: Did a thing",
                      "Quantifiable Results: It worked.",
                      "About JCPenney: a pasted job advert"])
        joined = " ".join(doc["unparsed"]) + " ".join(
            str(v) for r in doc["roles"] for v in r.values())
        self.assertIn("JCPenney", joined)

    def test_the_rendered_markdown_flags_a_role_with_no_accomplishments(self):
        doc = parsed(["Employment History", "1. Job Title: Analyst From: a To: b",
                      "Organization: Acme"])
        out = render(doc, Path("intake.docx"))
        self.assertIn("No accomplishments were recorded", out)

    def test_the_rendered_markdown_round_trips_its_headings(self):
        out = render(parsed(ROLE), Path("intake.docx"))
        self.assertIn("### Northwind Retail — Head of Analytics", out)
        self.assertIn("#### Predictive lifetime value", out)
        self.assertIn("- **Results:** 30% increase in marginal profits.", out)
        # the file build_profile.py will parse: headings must be findable
        self.assertEqual(len(re.findall(r"^#### ", out, re.M)), 2)


class Boilerplate(unittest.TestCase):
    def test_the_forms_own_instructions_are_not_imported(self):
        doc = parsed([
            "The Acme Team appreciates the time that you spend",
            "NOTE: Please complete the following questions",
            "* Actions: You took, using active verbs",
            "Employment History",
            "1. Job Title: Analyst From: a To: b",
            "Organization: Acme"])
        self.assertEqual(doc["unparsed"], [])
        self.assertEqual(len(doc["roles"]), 1)


if __name__ == "__main__":
    unittest.main()
