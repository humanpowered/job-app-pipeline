"""
Turn a filled resume-development intake document into profile/master.md.

Usage:
  python import_intake.py "path/to/intake.docx"
  python import_intake.py "path/to/intake.docx" --out profile/master.md
  python import_intake.py "path/to/intake.docx" --stdout     # look before writing

Why this exists: a resume is a selection, not a record. It is capped at two
pages, so it holds the accomplishments that suited one application and drops the
rest. master_profile.json inherited that ceiling -- 41 highlights, 898 words, for
a whole career. An intake questionnaire has no such limit: it asks for the
company's size, the problem you were hired to solve, who you reported to, what
you were given authority over, and then as many accomplishments as you can
recall, each as Problem / Actions / Quantifiable Results.

That last structure is worth more than a finished bullet. A bullet has already
been edited for some particular reader; Problem / Actions / Results is the raw
material, so a tailored resume can be composed to fit the posting in front of it
instead of reusing a sentence written for a different one.

Nothing here is rewritten, summarised or inferred. Every line in the output is
text copied from the source, and anything this parser does not recognise is
written to an "Unparsed" section rather than dropped -- a parser that silently
discards half a document looks exactly like one that works.

Reads .docx with the standard library: a .docx is a zip, and word/document.xml
holds the text. No new dependency.

This is additive. It writes a master.md from the document; it does not touch
master_profile.json, and it does not know about roles the document predates.
Merging is a separate, reviewable step.
"""
import argparse
import html
import re
import sys
import zipfile
from datetime import date
from pathlib import Path

SRC = Path(__file__).resolve().parent
ROOT = SRC.parent
DEFAULT_OUT = ROOT / "profile" / "master.md"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# A role block opens with "1. Job Title: X From: a To: b". Title and dates are
# routinely left blank -- in the document this was written for, two of six roles
# had an empty header and the organisation name underneath was the only clue.
ROLE_START = re.compile(r"^\d+\.\s*Job\s*Title:\s*(?P<title>.*?)"
                        r"(?:\s*From:\s*(?P<start>.*?))?"
                        r"(?:\s*To:\s*(?P<end>.*?))?$", re.I)

# label -> the field it fills. Matched on the start of a paragraph; the answer is
# whatever follows on the same line, or the paragraphs after it.
ROLE_FIELDS = [
    ("organization", re.compile(r"^Organization:\s*(.*)$", re.I)),
    ("location", re.compile(r"^Location:\s*(.*)$", re.I)),
    # These two questions run to several sentences. Stopping at the first "?"
    # left the rest of the question in the answer ("What are its
    # products/services? What markets does it service? $1bn annual revenue"),
    # so each pattern consumes the whole question up to its real end.
    ("company", re.compile(r"^What is the size of this company.*?service\?\s*(.*)$", re.I)),
    ("markets", re.compile(r"^Markets:\s*(.*)$", re.I)),
    ("challenge", re.compile(r"^What was your major challenge.*?resolve\.?\s*(.*)$", re.I)),
    ("authority", re.compile(r"^Your level of authority[^?]*\?\s*(.*)$", re.I)),
    ("territory", re.compile(r"^Size of territory\?\s*(.*)$", re.I)),
    ("budget", re.compile(r"^How much was your Budget[^?]*\?\s*(.*)$", re.I)),
    ("reports_to", re.compile(r"^Position you reported to\?\s*(.*)$", re.I)),
    ("responsibilities", re.compile(r"^Major responsibilities[^:]*:\s*(.*)$", re.I)),
    ("awards", re.compile(r"^Did you receive any awards[^?]*\?\s*(.*)$", re.I)),
]
ACCOMPLISHMENTS_HEADER = re.compile(r"^Describe as many relevant accomplishments", re.I)
# "Short Title:Outbound call center fraud" -- the space after the colon is
# optional in real documents, so do not require it.
ACC_FIELDS = [
    ("title", re.compile(r"^Short\s*Title:\s*(.*)$", re.I)),
    ("problem", re.compile(r"^Problem or Situation:\s*(.*)$", re.I)),
    ("actions", re.compile(r"^Actions:\s*(.*)$", re.I)),
    ("results", re.compile(r"^Quantifiable Results:\s*(.*)$", re.I)),
]

SECTION_BASICS = re.compile(r"^The Basics$", re.I)
SECTION_EMPLOYMENT = re.compile(r"^Employment History$", re.I)
COMPETENCIES_Q = re.compile(r"^List your KEY STRENGTHS", re.I)
ACHIEVEMENTS_Q = re.compile(r"^\d*\.?\s*WHAT are your MOST IMPRESSIVE ACHIEVEMENTS", re.I)
APART_Q = re.compile(r"^\d*\.?\s*What Sets You Apart", re.I)
PEERS = re.compile(r"^Comments from Peers", re.I)
TAIL_SECTIONS = re.compile(r"^(Military Background|\d*\.?\s*What FOREIGN LANGUAGES|"
                           r"\d*\.?\s*Technical Abilities|BRANDING STATEMENT|"
                           r"ONE FINAL NOTE)", re.I)

# Boilerplate the form itself prints. Dropping it is safe; it is the form's
# words, not the candidate's.
BOILERPLATE = re.compile(
    r"^(Please return the completed|The \w+ Team appreciates|"
    r"The resume is a living|Such a document will never|"
    r"The following questions will allow|--- All information|"
    r"NOTE: Please complete|One of the most important steps|"
    r"You, like everyone else|Please spend time on this section|"
    r"Please generate as many relevant achievements|"
    r"\* (Problem or situation|Actions|Results):|"
    r"Please include role, dates|Executive Resume Development|"
    r"We look forward to working|Thank you for completing|"
    r"Please attach additional information|\d{6,}-\d{4,})", re.I)

CONTACT = re.compile(r"^(Name|Address|Telephone|Email|LinkedIn web address):\s*(.*)$", re.I)


def paragraphs(path: Path) -> list[str]:
    """Every non-empty paragraph of a .docx, in document order."""
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise SystemExit(f"{path.name} is not a readable .docx ({type(exc).__name__}). "
                         f"If it is a .doc or a PDF, save it as .docx first.")
    out = []
    for para in re.findall(r"<w:p[ >].*?</w:p>", xml, flags=re.S):
        text = html.unescape(re.sub(r"<[^>]+>", "", para))
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            out.append(text)
    return out


def _is_label(text: str) -> bool:
    if ROLE_START.match(text) or ACCOMPLISHMENTS_HEADER.match(text):
        return True
    return any(p.match(text) for _f, p in ROLE_FIELDS + ACC_FIELDS)


def parse(paras: list[str]) -> dict:
    """
    Walk the document once, in order, collecting whatever is recognised.

    The shape is positional rather than nested: a label either carries its answer
    on the same line or owns the paragraphs until the next label.
    """
    doc = {"contact": {}, "competencies": [], "achievements": [], "sets_apart": [],
           "roles": [], "peers": [], "tail": [], "unparsed": []}
    where = "head"
    role = None
    acc = None
    field = None            # (bucket, key) currently collecting loose paragraphs

    def flush_role():
        nonlocal role, acc
        if acc and any(acc.get(k) for k in ("title", "problem", "actions", "results")):
            role["accomplishments"].append(acc)
        acc = None
        if role and (role.get("organization") or role.get("title")
                     or role["accomplishments"]):
            doc["roles"].append(role)
        role = None

    for text in paras:
        if BOILERPLATE.match(text):
            continue

        m = CONTACT.match(text)
        if m and where == "head":
            doc["contact"][m.group(1).lower().replace(" web address", "")] = m.group(2)
            continue

        if SECTION_BASICS.match(text):
            where, field = "basics", None
            continue
        if SECTION_EMPLOYMENT.match(text):
            where, field = "employment", None
            continue
        if PEERS.match(text):
            flush_role()
            where, field = "peers", None
            continue
        if TAIL_SECTIONS.match(text):
            flush_role()
            where, field = "tail", None
            doc["tail"].append(text)
            continue

        if where == "basics":
            if COMPETENCIES_Q.match(text):
                field = ("competencies", None)
                continue
            if ACHIEVEMENTS_Q.match(text):
                field = ("achievements", None)
                continue
            if APART_Q.match(text):
                field = ("sets_apart", None)
                continue
            if field:
                doc[field[0]].append(text)
            else:
                doc["unparsed"].append(text)
            continue

        if where == "employment":
            m = ROLE_START.match(text)
            if m:
                flush_role()
                role = {"title": (m.group("title") or "").strip(),
                        "start": (m.group("start") or "").strip(),
                        "end": (m.group("end") or "").strip(),
                        "accomplishments": []}
                field = None
                continue
            if role is None:
                doc["unparsed"].append(text)
                continue

            if ACCOMPLISHMENTS_HEADER.match(text):
                # Whatever answers this question is worth keeping even when it
                # is not an accomplishment. One role answered it "All in
                # resume", which is the single most useful line about that role:
                # it says the record is thin and where to go instead.
                field = ("role", "accomplishments_note")
                continue

            matched = False
            for key, pattern in ACC_FIELDS:
                m = pattern.match(text)
                if not m:
                    continue
                if key == "title":
                    if acc and any(acc.get(k) for k in
                                   ("title", "problem", "actions", "results")):
                        role["accomplishments"].append(acc)
                    acc = {}
                if acc is None:
                    acc = {}
                acc[key] = m.group(1).strip()
                field = ("acc", key)
                matched = True
                break
            if matched:
                continue

            for key, pattern in ROLE_FIELDS:
                m = pattern.match(text)
                if not m:
                    continue
                value = m.group(1).strip()
                if key in ("responsibilities", "awards", "markets"):
                    role.setdefault(key, [])
                    if value:
                        role[key].append(value)
                else:
                    role[key] = value
                field = ("role", key)
                matched = True
                break
            if matched:
                continue

            # a loose paragraph: it belongs to whatever label came last
            if field and field[0] == "acc" and acc is not None:
                acc[field[1]] = (acc.get(field[1], "") + " " + text).strip()
            elif field and field[0] == "role":
                key = field[1]
                if key in ("responsibilities", "awards", "markets"):
                    role.setdefault(key, []).append(text)
                else:
                    role[key] = (role.get(key, "") + " " + text).strip()
            else:
                doc["unparsed"].append(text)
            continue

        if where == "peers":
            doc["peers"].append(text)
            continue
        if where == "tail":
            doc["tail"].append(text)
            continue
        doc["unparsed"].append(text)

    flush_role()
    return doc


def render(doc: dict, source: Path) -> str:
    """Markdown with stable headings, so build_profile.py can read it back."""
    L = [f"# Master accomplishment record",
         "",
         f"Imported from `{source.name}` on {date.today().isoformat()}.",
         "",
         "Every line below is copied from that document. Nothing was rewritten,",
         "summarised or inferred. This file is the input; master_profile.json is",
         "built from it.",
         "",
         "Add as much as you can recall, role by role. There is no page limit here --",
         "that constraint belongs to a resume, and the tailoring step applies it per",
         "posting by selecting from what is in this file.",
         ""]

    if doc["contact"]:
        L += ["## Contact", "",
              "Check these against the present day: an intake document is a snapshot,",
              "and an address or a target title from a past search is often stale.", ""]
        for key, value in doc["contact"].items():
            L.append(f"- **{key.title()}:** {value}")
        L.append("")

    if doc["competencies"] or doc["sets_apart"]:
        L += ["## Positioning", ""]
        if doc["competencies"]:
            L += ["### Core competencies", ""] + [f"- {c}" for c in doc["competencies"]] + [""]
        if doc["sets_apart"]:
            L += ["### What sets me apart", ""] + doc["sets_apart"] + [""]

    L += ["## Roles", ""]
    for role in doc["roles"]:
        name = role.get("organization") or "[FILL IN: employer]"
        title = role.get("title") or "[FILL IN: title]"
        L.append(f"### {name} — {title}")
        L.append("")
        dates = " – ".join(x for x in (role.get("start"), role.get("end")) if x)
        pairs = [("Dates", dates), ("Location", role.get("location")),
                 ("Company", role.get("company")), ("Challenge", role.get("challenge")),
                 ("Authority", role.get("authority")), ("Territory", role.get("territory")),
                 ("Budget", role.get("budget")), ("Reported to", role.get("reports_to"))]
        for label, value in pairs:
            if value:
                L.append(f"- **{label}:** {value}")
        for label, key in (("Markets", "markets"), ("Responsibilities", "responsibilities"),
                           ("Recognition", "awards")):
            items = [x for x in (role.get(key) or []) if x]
            if items:
                L.append(f"- **{label}:** " + "; ".join(items))
        if role.get("accomplishments_note"):
            L.append(f"- **Note on accomplishments:** {role['accomplishments_note']}")
        if not role["accomplishments"]:
            L.append("")
            L.append("> No accomplishments were recorded for this role in the source "
                     "document. This is the gap worth filling first: add a "
                     "`#### Short title` block below with Problem, Actions and Results.")
        L.append("")
        for acc in role["accomplishments"]:
            L.append(f"#### {acc.get('title') or '[FILL IN: short title]'}")
            L.append("")
            for label, key in (("Problem", "problem"), ("Actions", "actions"),
                               ("Results", "results")):
                value = acc.get(key)
                L.append(f"- **{label}:** {value}" if value
                         else f"- **{label}:** [FILL IN]")
            L.append("")

    if doc["achievements"]:
        L += ["## Achievements not attached to a role", "",
              "From the document's \"most impressive achievements\" question. These",
              "restate some of the above in different words, and the indented lines",
              "under one of them are its results. Move each into the right role and",
              "delete this section -- it is left separate rather than merged because",
              "deciding which are duplicates takes judgement.", ""]
        L += [f"- {a}" for a in doc["achievements"]] + [""]

    if doc["peers"]:
        L += ["## Peer comments", ""] + [f"- {p}" for p in doc["peers"]] + [""]

    if doc["tail"]:
        L += ["## Other sections from the document", ""] + [f"- {t}" for t in doc["tail"]] + [""]

    if doc["unparsed"]:
        L += ["## Unparsed", "",
              "Text this importer did not recognise. It is here so nothing is lost;",
              "most of it is usually form boilerplate or something pasted into the",
              "document. Delete what you do not want.", ""]
        L += [f"- {u}" for u in doc["unparsed"]] + [""]

    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("docx", help="a filled resume-development intake document")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help=f"default {DEFAULT_OUT}")
    ap.add_argument("--stdout", action="store_true", help="print instead of writing")
    args = ap.parse_args()

    source = Path(args.docx)
    if not source.exists():
        raise SystemExit(f"No such file: {source}")

    paras = paragraphs(source)
    doc = parse(paras)
    text = render(doc, source)

    accomplishments = sum(len(r["accomplishments"]) for r in doc["roles"])
    print(f"  {source.name}")
    print(f"  {len(paras)} paragraph(s) read")
    print(f"  {len(doc['roles'])} role(s), {accomplishments} accomplishment(s) "
          f"with a problem/actions/results breakdown")
    for role in doc["roles"]:
        n = len(role["accomplishments"])
        flag = "" if n else "   <- nothing recorded for this role"
        print(f"    {(role.get('organization') or '?')[:28]:30} "
              f"{n} accomplishment(s){flag}")
    if doc["achievements"]:
        print(f"  {len(doc['achievements'])} achievement line(s) not attached to a role")
    if doc["unparsed"]:
        print(f"  {len(doc['unparsed'])} paragraph(s) not recognised -- kept under "
              f'"Unparsed" for you to triage')
    if not doc["roles"]:
        print("  [warn] no role blocks found. Is this a filled intake document, "
              "or an ordinary resume?")

    if args.stdout:
        print("\n" + "-" * 70 + "\n")
        print(text)
        return 0

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        raise SystemExit(f"\n{out} already exists. Delete or rename it first, or "
                         f"pass --out somewhere else; this would overwrite work.")
    out.write_text(text, encoding="utf-8")
    print(f"\n  wrote {out}")
    print("  Read it before anything else uses it. Roles the document predates are "
          "not in it -- it knows only what was written down at the time.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
