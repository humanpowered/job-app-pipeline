"""
Scores each posting for fit against the master profile, and drafts a
tailored resume for postings above a score threshold.

Requires: pip install anthropic
Set ANTHROPIC_API_KEY in your environment before running.
"""
import csv
import html
import json
import os
import re
from datetime import date
from pathlib import Path
import anthropic

PROFILE_DIR = Path(__file__).parent.parent / "profile"
PROFILE_PATH = PROFILE_DIR / "master_profile.json"
SKILLS_CSV_PATH = PROFILE_DIR / "skills_inventory.csv"
OUTPUT_DIR = Path(__file__).parent.parent / "output"

MODEL = "claude-sonnet-5"  # update if you want a different model
SCORE_THRESHOLD = 6  # out of 10, only tailor resumes above this

client = anthropic.Anthropic()  # picks up ANTHROPIC_API_KEY from env


def load_skills_csv(path: Path | None = None) -> tuple[dict, list] | None:
    """
    skills_inventory.csv is the source of truth for skills. Returns
    (skills_by_category, certifications), or None if the file is absent so
    the caller can fall back to whatever is in master_profile.json.

    Only rows with have_it=yes are used. Rows marked no are development
    targets and must never reach a resume. Proficiency, when present, is
    appended so the tailoring step can lead with genuine strengths.
    """
    path = path or SKILLS_CSV_PATH
    if not path.exists():
        return None

    by_category: dict[str, list[str]] = {}
    certifications: list[str] = []
    skipped = 0

    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            skill = (row.get("skill") or "").strip()
            if not skill:
                continue
            if (row.get("have_it") or "").strip().lower() not in ("yes", "y", "true", "1"):
                skipped += 1
                continue

            category = (row.get("category") or "Other").strip() or "Other"
            proficiency = (row.get("proficiency") or "").strip()
            notes = (row.get("notes") or "").strip()

            if category.lower().startswith("certification"):
                certifications.append(skill)
                continue

            entry = skill
            if proficiency:
                entry += f" ({proficiency})"
            if notes:
                entry += f" [{notes}]"
            by_category.setdefault(category, []).append(entry)

    total = sum(len(v) for v in by_category.values()) + len(certifications)
    print(f"  loaded {total} skills from skills_inventory.csv "
          f"({skipped} marked have_it=no, excluded)")
    return by_category, certifications


def load_profile() -> dict:
    with open(PROFILE_PATH, encoding="utf-8") as f:
        profile = json.load(f)

    loaded = load_skills_csv()
    if loaded is None:
        return profile

    by_category, certifications = loaded
    if not by_category and not certifications:
        print("  [warn] skills_inventory.csv had no usable rows -- "
              "falling back to skills in master_profile.json")
        return profile

    # CSV wins: drop the JSON copies so the model never sees two
    # contradictory skill lists.
    profile.pop("skills", None)
    profile.pop("technical_stack", None)
    profile["skills_by_category"] = by_category
    if certifications:
        existing = profile.get("certifications", [])
        merged = list(dict.fromkeys(certifications + existing))
        profile["certifications"] = merged
    return profile


def strip_html(html: str) -> str:
    return re.sub("<[^<]+?>", " ", html or "")


def extract_text(resp) -> str:
    parts = [b.text for b in resp.content if b.type == "text"]
    if not parts:
        raise ValueError(
            f"No text block in response (stop_reason={resp.stop_reason}, "
            f"blocks={[b.type for b in resp.content]})"
        )
    return "\n".join(parts)


def parse_json_response(text: str) -> dict:
    """
    Models occasionally wrap JSON in prose or code fences, or slip in a
    // comment. Pull out the outermost {...} and strip comments before
    parsing so one stray character doesn't kill a whole pipeline run.
    """
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        cleaned = cleaned[start:end + 1]
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # retry without // line comments and trailing commas
        stripped = re.sub(r"(?<!:)//[^\n]*", "", cleaned)
        stripped = re.sub(r",(\s*[}\]])", r"\1", stripped)
        return json.loads(stripped)


SALARY_FLOOR = 180_000   # at or above this, seniority concerns don't apply

# "$150,000 - $185,000" / "$150K-$185K" / "$150,000 to $250,000" and the
# en/em-dash variants pay-transparency boilerplate tends to use
_SALARY_RANGE = re.compile(
    r"\$\s?(\d{2,3}(?:,\d{3})?(?:\.\d+)?)\s?([kK])?\s*(?:-|–|—|to)\s*\$?\s?"
    r"(\d{2,3}(?:,\d{3})?(?:\.\d+)?)\s?([kK])?")


def extract_salary_range(text: str) -> tuple[int, int] | None:
    """
    Pull the largest plausible annual salary range out of a posting.

    Returns (low, high) in dollars, or None. Ranges that look like hourly
    rates or equity percentages are discarded; a posting can mention several
    numbers, so the widest plausible annual pair wins.
    """
    text = text or ""
    best = None
    for m in _SALARY_RANGE.finditer(text):
        # "$45.00 - $65.00 per hour" must not become $45K-$65K
        trailing = text[m.end():m.end() + 30].lower()
        # no \b before "/hr" -- a slash is not a word character, so the
        # boundary never matches and hourly rates slip through
        if re.search(r"per\s+hour|hourly|an\s+hour|/\s?h(r|our)|\bp/?h\b", trailing):
            continue
        if "." in m.group(1) or "." in m.group(3):
            continue                    # cents mean a rate, not a salary

        def val(num, k):
            n = float(num.replace(",", ""))
            if k or n < 1000:           # "150K" or a bare "$150" meaning 150K
                n *= 1000
            return int(n)

        low, high = val(m.group(1), m.group(2)), val(m.group(3), m.group(4))
        if low > high or high < 30_000 or high > 2_000_000:
            continue                    # percentage or noise
        if best is None or high > best[1]:
            best = (low, high)
    return best


def score_posting(posting: dict, profile: dict) -> dict:
    description = strip_html(posting["description_html"])
    salary = extract_salary_range(description)
    years = profile.get("years_experience") or "many years of"

    if salary and salary[1] >= SALARY_FLOOR:
        seniority_rule = (
            f"- Seniority match. This posting states a pay range of ${salary[0]:,} to "
            f"${salary[1]:,}. The top of that range is at or above ${SALARY_FLOOR:,}, "
            "which means the role is compensated at a level appropriate to the "
            "candidate's experience. DO NOT reduce the score for overqualification, "
            "and set overqualification_risk to false, even if the title reads as IC or "
            "manager-level. Companies paying this much expect deep experience. Judge "
            "seniority on the scope of the work described, not the title."
        )
    elif salary:
        seniority_rule = (
            f"- Seniority match. This posting states a pay range of ${salary[0]:,} to "
            f"${salary[1]:,}, topping out below ${SALARY_FLOOR:,}. The candidate has "
            f"{years} experience and a history of building and leading analytics "
            "functions, so weigh overqualification honestly here and flag it if the "
            "role is an individual-contributor or manager-level position."
        )
    else:
        seniority_rule = (
            "- Seniority match. No pay range is stated. Note that the candidate is often "
            f"OVERqualified for individual-contributor or manager-level roles given "
            f"{years} experience; flag this risk if relevant."
        )

    prompt = f"""You are helping a job seeker evaluate whether a posting is a good fit.

CANDIDATE PROFILE:
{json.dumps(profile, indent=2)}

JOB POSTING:
Title: {posting['title']}
Company: {posting['company']}
Location: {posting['location']}
Description: {description[:4000]}

Score fit from 0-10 considering:
- Skills/experience match
{seniority_rule}
- Location/remote compatibility

Do NOT treat a personal characteristic as a disqualifier unless the profile
states it. Religious affiliation, citizenship, security clearance, veteran
status, language fluency, and willingness to relocate are unknown unless
written down. If a posting requires one and the profile is silent, say the
requirement exists and that it needs confirming; do not assume it is unmet
and do not lower the score for it. Judge on skills, seniority, and the
stated location only.

Respond ONLY with JSON, no other text, in this exact shape:
{{"score": <int 0-10>, "reasoning": "<2-3 sentences>", "overqualification_risk": <true/false>}}
"""
    resp = client.messages.create(
        model=MODEL,
        # 2048, not 500: the seniority rule made this prompt longer and the
        # model reasons before answering, so a small budget truncates the JSON
        # mid-string and surfaces as a confusing parse error.
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    if resp.stop_reason == "max_tokens":
        raise ValueError(
            f"scoring response truncated at max_tokens "
            f"({resp.usage.output_tokens} out); raise max_tokens in score_posting")
    result = parse_json_response(extract_text(resp))
    if salary:
        result["salary_low"], result["salary_high"] = salary
        if salary[1] >= SALARY_FLOOR:
            # belt and braces: the rule above is explicit, but this is a hard
            # constraint Craig asked for, so enforce it rather than trust it
            result["overqualification_risk"] = False
    return result


RESUME_SCHEMA_EXAMPLE = {
    "name": "Jordan Example",
    "contact": "City, ST | email@example.com | 555-555-5555 | linkedin.com/in/example",
    "summary": "2-3 sentence professional summary tailored to this posting",
    "experience": [
        {
            "title": "Job Title",
            "company": "Company Name",
            "dates": "2020 - Present",
            "bullets": ["Achievement bullet 1", "Achievement bullet 2"]
        }
    ],
    "skills": [
        "Category Name: Term, Term, Term, Term, Term, Term",
        "Second Category: Term, Term, Term, Term",
    ],
    "education": ["Degree, School, Year"]
}


# Fitting two pages. Measured against real output: a 977-word resume with 33
# bullets fits; 1,071 words with 37 bullets spilled onto a third page.
MAX_RESUME_WORDS = 900
MAX_SUMMARY_WORDS = 80
# bullets allowed per role by position, newest first. Recent roles carry the
# argument; the oldest are there for continuity, not detail.
BULLETS_BY_POSITION = [6, 6, 3, 3, 5, 3]
BULLETS_TAIL = 2                 # anything beyond the list above


def fit_to_two_pages(resume: dict) -> dict:
    """
    Trim a tailored resume so it renders on two pages.

    The model is told to keep it short and does not reliably comply, the
    same as with cover letter length. Trimming happens from the end of each
    role's bullet list, which is safe because bullets are ordered by
    relevance to the posting within each role.
    """
    def total_words(r):
        parts = [r.get("summary", "")]
        parts += [b for e in r.get("experience", []) for b in e.get("bullets", [])]
        parts += r.get("skills", [])
        parts += r.get("education", [])
        return len(" ".join(parts).split())

    before = total_words(resume)

    summary = resume.get("summary", "").split()
    if len(summary) > MAX_SUMMARY_WORDS:
        # cut at a sentence boundary rather than mid-clause
        text = " ".join(summary)
        sentences = re.split(r"(?<=[.!?])\s+", text)
        kept, n = [], 0
        for s in sentences:
            if n + len(s.split()) > MAX_SUMMARY_WORDS and kept:
                break
            kept.append(s)
            n += len(s.split())
        resume["summary"] = " ".join(kept)

    for i, job in enumerate(resume.get("experience", [])):
        cap = BULLETS_BY_POSITION[i] if i < len(BULLETS_BY_POSITION) else BULLETS_TAIL
        job["bullets"] = (job.get("bullets") or [])[:cap]

    # still over? drop the weakest remaining bullet from the oldest role that
    # has more than one, and repeat
    guard = 0
    while total_words(resume) > MAX_RESUME_WORDS and guard < 40:
        guard += 1
        for job in reversed(resume.get("experience", [])):
            if len(job.get("bullets") or []) > 1:
                job["bullets"].pop()
                break
        else:
            break

    after = total_words(resume)
    if after != before:
        print(f"    trimmed to fit two pages: {before} -> {after} words")
    return resume


MAX_SKILL_CATEGORIES = 5
MAX_TERMS_PER_CATEGORY = 10


def normalize_skills(resume: dict) -> dict:
    """
    Force the skills block into "Category: term, term" lines of atomic terms.

    Two failure modes this fixes, both measured on real output:
      - descriptive phrases instead of terms ("Incrementality experiment
        design: geo-holdout, synthetic control, causal inference"), which
        dilute keyword density and split badly on commas
      - parentheses, which some ATS parsers drop, taking "(MMM)" and
        "(Looker, Tableau)" with them
    """
    skills = resume.get("skills") or []
    if not skills:
        return resume

    # parentheses go regardless of shape; promote the contents to siblings
    skills = [re.sub(r"\s*\(([^)]*)\)", r", \1", s).strip() for s in skills]

    cat_line = re.compile(r"^([^:]{3,40}):\s*(.+)$")
    # Only treat this as categorized when EVERY entry is a category line.
    # A flat list where a few entries happen to contain a colon
    # ("Unit economics: CAC, ...") is a list of skills, not categories, and
    # inferring structure from it produced three bogus headings.
    if not (len(skills) >= 3 and all(cat_line.match(s) for s in skills)):
        seen, flat = set(), []
        for s in skills:
            k = s.lower()
            if k not in seen and s:
                seen.add(k)
                flat.append(s)
        resume["skills"] = flat
        return resume

    categories = []
    for entry in skills:
        m = cat_line.match(entry)
        label = m.group(1).strip()
        terms = [t.strip(" .;") for t in m.group(2).split(",")]
        categories.append((label, [t for t in terms if t]))

    cleaned, seen = [], set()
    for label, terms in categories[:MAX_SKILL_CATEGORIES]:
        keep = []
        for t in terms:
            k = t.lower()
            if k in seen or not t:
                continue
            seen.add(k)
            keep.append(t)
            if len(keep) >= MAX_TERMS_PER_CATEGORY:
                break
        if keep:
            cleaned.append(f"{label}: " + ", ".join(keep))

    resume["skills"] = cleaned
    return resume


def order_experience(resume: dict, profile: dict) -> dict:
    """
    Force reverse-chronological work history.

    Left to itself the model promotes whichever role best matches the
    posting, which put Deutsch LA (2010-2015) above Golden Hippo (2020-2022)
    on agency applications. Sorting against master_profile's order is more
    reliable than parsing free-text date strings, and the profile is already
    newest-first.
    """
    entries = resume.get("experience")
    if not entries:
        return resume

    canonical = [j["company"] for j in profile.get("work_history", [])]

    def match(entry):
        # generated company fields carry descriptors, e.g.
        # "Golden Hippo (Health/Wellness/CPG, ~$1B revenue)"
        name = (entry.get("company") or "").lower()
        for i, c in enumerate(canonical):
            if c.lower() in name or name.split("(")[0].strip() in c.lower():
                return i
        return None

    def rank(entry):
        i = match(entry)
        return len(canonical) if i is None else i   # unrecognized falls to the bottom

    # The descriptor is context for writing bullets, not resume content. Left
    # alone it reaches the page as "Golden Hippo (Health, wellness, beauty &
    # pet care e-commerce/CPG; ~$1B revenue, ~$300M ad spend, 12 brands)",
    # which reads as internal notes and differs resume to resume. Pin the
    # company to the profile's own spelling.
    for entry in entries:
        i = match(entry)
        if i is not None:
            entry["company"] = canonical[i]

    resume["experience"] = sorted(entries, key=rank)
    return resume


def tailor_resume(posting: dict, profile: dict) -> dict:
    prompt = f"""Draft a tailored resume for this candidate applying to this specific posting.

CANDIDATE PROFILE:
{json.dumps(profile, indent=2)}

JOB POSTING:
Title: {posting['title']}
Company: {posting['company']}
Description: {strip_html(posting['description_html'])[:4000]}

Guidelines:
- Mirror the language and priorities of the posting where truthful and accurate.
- {profile.get('positioning_notes', '')}
- Do not fabricate experience, employers, titles, or metrics not present in the profile.
- If the profile has PLACEHOLDER fields, leave a clear "[FILL IN: ...]" marker in that
  field rather than inventing content.
- List work history in reverse-chronological order, most recent role first.
  Never move a role up the page because it is more relevant to this posting;
  relevance is expressed through which bullets you keep and how you word
  them, not through position. The order is enforced after generation, so
  reordering here only creates a mismatch.
- Order/emphasize experience bullets WITHIN each role to match what this
  posting cares about most.

SKILLS SECTION. This is read by both an ATS keyword parser and a human
skimming for anchors. Format for both:
- Group into {MAX_SKILL_CATEGORIES} categories at most, each a single line
  "Category: term, term, term". Fewer, fuller categories beat many thin ones.
- Name categories after what THIS posting asks for. A media role gets
  "Marketing Measurement"; a product analytics role gets "Product & Growth
  Analytics". Do not reuse a fixed set across different postings.
- Every item must be an atomic term a parser can match: "Marketing Mix
  Modeling", "Incrementality Testing", "Synthetic Control", "SQL",
  "Snowflake". NOT descriptive phrases like "Incrementality experiment
  design: geo-holdout, synthetic control, causal inference" -- that is one
  entry pretending to be three, and it splits badly on commas.
- No parentheses anywhere in this section. Write "Marketing Mix Modeling,
  MMM" as two terms rather than "Marketing Mix Modeling (MMM)"; some
  parsers drop the parenthetical and lose the acronym.
- No connective words. "and", "including", "with", "across" have no place
  in a term list.
- At most {MAX_TERMS_PER_CATEGORY} terms per category, strongest first.
- Draw terms from the profile's skills_by_category, preferring the ones
  this posting names. Do not list a skill the candidate does not have.

Respond ONLY with JSON, no other text, matching exactly this shape:
{json.dumps(RESUME_SCHEMA_EXAMPLE, indent=2)}
"""
    resp = client.messages.create(
        model=MODEL,
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )
    resume = order_experience(parse_json_response(extract_text(resp)), profile)
    return fit_to_two_pages(normalize_skills(resume))


def _sanitize_filename_part(text: str) -> str:
    # scraped titles carry HTML entities through to the filename
    # ("Measurement &amp; Insights"); decode before stripping
    text = html.unescape(text or "")
    text = re.sub(r'[<>:"/\\|?*]', "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:80].strip()


# Fixed frame, modelled on the letters Craig has actually sent. Only the
# role title and the grouped achievement block change per posting.
CL_POSITIONING = ("I am a senior data, marketing, and analytics leader, with hands-on "
                  "expertise in building innovative and transformative data and analytics "
                  "systems that deliver actionable insights and drive business performance "
                  "across many verticals.")
CL_LEAD_IN = "Select highlights of my career contributions and achievements thus far include:"
CL_CLOSING_PARA = ("For a more detailed illustration of my skills and experience, please see "
                   "my resume. I would welcome the chance to discuss how my background fits "
                   "what you are building.")

COVER_LETTER_SCHEMA_EXAMPLE = {
    "greeting": "Dear Hiring Committee:",
    "opening": "Please consider my qualifications for the <exact role title> role at "
               "<company>. <One sentence naming the single most relevant thing about this "
               "candidate for THIS posting.>",
    "positioning": "<leave exactly as given in the prompt>",
    "lead_in": "<leave exactly as given in the prompt>",
    "groups": [
        {
            "header": "Theme drawn from what this posting asks for, with scope if it helps "
                      "(e.g. 'Marketing Mix Modeling & Incrementality Measurement')",
            "bullets": [
                "A concrete achievement from the profile with its metric, tied to that theme",
                "A second piece of evidence for the same theme",
            ],
        },
        {"header": "Second theme", "bullets": ["evidence", "evidence"]},
        {"header": "Third theme", "bullets": ["evidence", "evidence"]},
    ],
    "closing_para": "<leave exactly as given in the prompt>",
    "sign_off": "Sincerely,",
    # placeholders only: both are overwritten from the profile after the
    # model returns, so no real contact details belong in this file
    "name": "<your name>",
    "contact": "<your email>\n<your phone>",
}


AI_TELL_PATTERNS = [
    (r"[—–]", "em/en dash"),
    (r"\bnot just\b", '"not just"'),
    (r"\bisn't just\b", '"isn\'t just"'),
    (r"\bit's not about\b", '"it\'s not about"'),
    # the ", not <contrasting noun phrase>" flourish -- same rhetorical move
    # as "not just X but Y", just inverted
    (r",\s+not\s+(?!only\b)\w+", '", not X" contrast flourish'),
    (r"\brather than a stretch\b", '"rather than a stretch"'),
    (r"\bresonate", '"resonate"'),
    (r"\bdrawn to\b", '"drawn to"'),
    (r"\bpassionate about\b", '"passionate about"'),
    (r"\bat the intersection of\b", '"at the intersection of"'),
    (r"\bexcited by the opportunity\b", '"excited by the opportunity"'),
    (r"\bwhat excites me most\b", '"what excites me most"'),
    (r"\bexactly (?:the|that|this) kind of\b", '"exactly that kind of"'),
    (r"\bcaught my attention for a\b", '"caught my attention for a..."'),
    (r"\bstruck a chord\b", '"struck a chord"'),
    (r"\bspearhead", '"spearhead"'),
    # Filler intensifiers. Craig cut "actually" from "so client teams
    # actually use the output" -- these add emphasis, never information.
    (r"\bactually\b", 'filler "actually"'),
    (r"\btruly\b", 'filler "truly"'),
    (r"\breally\b", 'filler "really"'),
    (r"\bincredibly\b", 'filler "incredibly"'),
]


MAX_LETTER_WORDS = 265      # prompt targets 210; this is the hard ceiling,
                            # set with slack so the lint doesn't fail a good
                            # letter over a couple of words
MAX_SENTENCE_WORDS = 38     # length was never the real problem, nesting was


def letter_body(letter: dict) -> str:
    """
    The parts of the letter the model actually wrote.

    Excludes the fixed boilerplate (positioning, lead_in, closing_para) --
    linting text that is required to be verbatim would flag it forever.
    """
    parts = [letter.get("opening", "")]
    for g in letter.get("groups", []):
        parts.append(g.get("header", ""))
        parts.extend(g.get("bullets", []))
    parts.extend(letter.get("paragraphs", []))     # older prose-format letters
    return " ".join(p for p in parts if p)


def find_ai_tells(letter: dict) -> list[str]:
    """Scan the drafted letter body for the patterns the prompt bans."""
    body = letter_body(letter)
    found = []
    for pattern, label in AI_TELL_PATTERNS:
        if re.search(pattern, body, flags=re.IGNORECASE):
            found.append(label)
    return found


MAX_BULLET_WORDS = 42       # bullets are skimmed; past this they read as prose
MIN_GROUPS = 3

# Vague quantifiers the model reaches for when it has no real number.
INVENTED_SCOPE = re.compile(
    r"\b(dozens|scores|countless|numerous|myriad|a wide (range|variety)|"
    r"many (clients|brands|verticals|industries|companies)|"
    r"across (dozens|numerous|countless))\b", re.I)


def find_ungrounded_claims(letter: dict, profile: dict) -> list[str]:
    """
    Catch bullets asserting things the profile does not support.

    Two failure modes seen in practice:
      1. numbers that appear nowhere in the profile (invented or derived)
      2. vague quantifiers standing in for scope ("dozens of verticals")

    A third -- lifting an industry out of company_descriptor and presenting
    it as Craig's own client work -- is handled in the prompt, since
    detecting it reliably needs to know which descriptor a claim came from.
    """
    issues = []
    profile_text = json.dumps(profile).lower()

    # every distinct number in the profile, normalized
    prof_nums = set(re.findall(r"\d+(?:\.\d+)?", profile_text))

    for g in letter.get("groups") or []:
        for b in g.get("bullets") or []:
            if INVENTED_SCOPE.search(b):
                m = INVENTED_SCOPE.search(b)
                issues.append(f'invented scope "{m.group(0)}" in: "{b[:56]}..."')
            for num in re.findall(r"\d+(?:\.\d+)?", b):
                # years and small ordinals show up incidentally; the risk is
                # metrics, so only check numbers that carry weight
                if num in prof_nums or len(num) < 2:
                    continue
                issues.append(f'number "{num}" is not in the profile: "{b[:56]}..."')

    return issues


def specific_pattern(profile: dict | None = None) -> re.Pattern:
    """
    What counts as a concrete bullet: a number, an employer the candidate
    actually worked for, or a named method.

    The employer half comes from the profile rather than a hardcoded list, so
    this travels with whoever is using the pipeline. Named methods stay a
    fixed list; they are domain vocabulary, not biography.
    """
    METHODS = [r"Bayesian", r"geo-holdout", r"synthetic control", r"conjoint",
               r"MMM", r"LTV", r"ROAS", r"attribution", r"incrementality",
               r"A/B test", r"causal inference", r"segmentation", r"forecast"]
    names = []
    for job in (profile or {}).get("work_history", []):
        company = (job.get("company") or "").strip()
        if company:
            names.append(re.escape(company))
            first = company.split()[0]
            if len(first) > 3:                 # "Deutsch" for "Deutsch LA"
                names.append(re.escape(first))
    return re.compile("|".join([r"\d", "%", r"\$"] + names + METHODS), re.I)


def find_style_issues(letter: dict, profile: dict | None = None) -> list[str]:
    """
    Structural checks for the grouped-bullet format. The old prose-shaped
    rules (total word count, sentence nesting, cadence runs) were retired
    with the prose format; the ones that survive are about bullets doing
    their job.
    """
    issues = []

    # legacy prose letters still get the old length check
    if letter.get("paragraphs") and not letter.get("groups"):
        body = " ".join(letter["paragraphs"])
        if len(body.split()) > MAX_LETTER_WORDS:
            issues.append(f"too long ({len(body.split())} words)")
        return issues

    groups = letter.get("groups") or []
    if len(groups) < MIN_GROUPS:
        issues.append(f"only {len(groups)} achievement group(s); want {MIN_GROUPS}")

    seen_headers = set()
    for g in groups:
        header = (g.get("header") or "").strip()
        bullets = [b for b in (g.get("bullets") or []) if b.strip()]

        if not header:
            issues.append("a group is missing its header")
        elif header.lower() in seen_headers:
            issues.append(f'duplicate group header: "{header[:44]}"')
        seen_headers.add(header.lower())

        if not bullets:
            issues.append(f'group "{header[:34]}" has no bullets')
        for b in bullets:
            n = len(b.split())
            if n > MAX_BULLET_WORDS:
                issues.append(f'bullet of {n} words (max {MAX_BULLET_WORDS}): "{b[:64]}..."')

    # Flag only when the block as a whole is vague. An individual bullet
    # without a number is fine ("Built the analytics practice from the ground
    # up"); a letter where most bullets have no number, employer, or named
    # method is the actual failure.
    SPECIFIC = specific_pattern(profile)
    bullets = [b for g in groups for b in (g.get("bullets") or [])]
    vague = [b for b in bullets if not SPECIFIC.search(b)]
    if bullets and len(vague) > len(bullets) / 2:
        issues.append(f"{len(vague)} of {len(bullets)} bullets carry no metric, employer, "
                      f'or named method; e.g. "{vague[0][:58]}..."')

    return issues


def draft_cover_letter(posting: dict, profile: dict) -> dict:
    prompt = f"""Draft a cover letter for this candidate applying to this specific posting.

CANDIDATE PROFILE:
{json.dumps(profile, indent=2)}

JOB POSTING:
Title: {posting['title']}
Company: {posting['company']}
Description: {strip_html(posting['description_html'])[:4000]}

FORMAT. This is a fixed template that has worked in real applications. Most
of it is boilerplate that must be reproduced verbatim. Your job is almost
entirely the grouped achievement block in the middle.

Reproduce these EXACTLY, word for word, in the fields named:
  positioning  = "{CL_POSITIONING}"
  lead_in      = "{CL_LEAD_IN}"
  closing_para = "{CL_CLOSING_PARA}"

Write only two things:

1. OPENING (2 sentences). First sentence: "Please consider my
   qualifications for the <exact role title> role at <company>." Use the
   posting's exact title and the company's own branding of its name.
   Second sentence: the single most relevant fact about the candidate for
   THIS posting, stated concretely. Not "I am a strong fit" but the specific
   thing that makes them one. Keep it to one sentence.

2. THREE GROUPS. Each has a short header naming a capability this posting
   actually asks for, and 2 or 3 bullets of evidence beneath it.
   - Derive the headers from the posting's own requirements. If it asks for
     MMM and incrementality, one header is about that. Do not reuse generic
     headers across different postings.
   - Add scope to a header when it strengthens it, e.g.
     "Survey Design & Research Methodology (12+ years)".
   - Every bullet is a concrete achievement from the profile, with its
     metric where one exists. A bullet with no specific in it is wasted.
   - Order the groups so the one this posting cares about most comes first.
   - Bullets are fragments or single sentences, not paragraphs. Aim for 15
     to 35 words each.
   - Do not repeat the same achievement in two groups.
   - If the posting raises an obvious concern (seniority mismatch, industry
     change), use one bullet to address it with evidence rather than
     ignoring it.

CRITICAL -- this must not read as AI-written. Hiring managers screen for
these patterns and they are an instant credibility hit. Hard rules:
- NEVER use an em dash (--- or the character). Use a period, comma, or
  parenthesis. Do not substitute a spaced hyphen as a workaround.
- NEVER use the "not just X, but Y" / "it's not about X, it's about Y" /
  "X isn't just Y" construction. Not once. This includes the inverted form,
  "<good thing>, not <strawman thing>" (e.g. "frameworks that hold up, not
  dashboards that fall apart") and the "X rather than <strawman>" form
  ("building practices inside agencies rather than bolting them on from
  outside" -- nobody describes their own work as bolting it on). State what
  is true and stop. A contrast is only allowed when the alternative is a
  real, common failure mode someone would admit to ("client teams use the
  output rather than filing it away" is fine, because filing reports away
  genuinely happens).
- Every clause must add information. Cut qualifiers that only add texture:
  "most recently from a standing start" says nothing the next sentence
  doesn't already say better. If removing a phrase loses no fact, remove it.
- Every clause must add information. Cut qualifiers that only add texture.
  If removing a phrase loses no fact, remove it. This matters more in
  bullets than anywhere: a bullet is read in about a second.
- Plain verbs over elevated ones ("built", not "spearheaded"; "ran", not
  "orchestrated"). No "I'm drawn to", "resonates", "excited by the
  opportunity to", "passionate about", "at the intersection of".
- Bullets lead with the achievement, not with throat-clearing. Write
  "Cut A/B test false positives 70%..." rather than "I have experience
  with A/B testing, where I cut...".

Guidelines:
- The company name above may come from an applicant-tracking-system token
  and can be lowercase or run-together ("wpromote", "hims-and-hers"). Write
  it the way the company brands itself ("Wpromote", "Hims & Hers"). Getting
  a prospective employer's own name wrong reads as careless.
GROUNDING -- read this carefully, it is the most important rule here.

Not every field in the profile is a claim the candidate can make about
themselves.

  work_history[].highlights   = things the candidate personally did. THE
                                ONLY source for bullets. Every bullet must
                                trace to one of these.
  work_history[].company_descriptor = what the EMPLOYER's business was, its
                                revenue, and the markets THE COMPANY served.
                                This is background so you understand the
                                setting. It is NOT a list of the candidate's
                                clients or their personal scope. If an
                                employer served travel and fashion clients,
                                that does not mean the candidate worked on
                                them. Their accounts are whatever the
                                highlights actually name.
  work_history[].scope        = team size and reporting line. Usable, but
                                state it as written, not inflated.
  skills_by_category          = capabilities, not accomplishments. A skill
                                does not become an achievement bullet.

Hard rules that follow from this:
- Never move an industry, client, vertical, or market from a
  company_descriptor into a bullet as something Craig did.
- Never invent a quantifier. No "dozens of verticals", "numerous clients",
  "many brands" unless that exact scope appears in a highlight. If the
  profile says 12 brands, write 12 brands. If it gives no number, give no
  number.
- Every metric (percentage, dollar figure, count, time span) must appear in
  the profile. Do not derive, round, combine, or estimate one.
- If a posting asks for something Craig has not done, leave it out. A
  shorter letter is better than an inaccurate one.
- Draw only on achievements and metrics present in the profile. Do not
  fabricate experience, employers, titles, or numbers.
- {profile.get('positioning_notes', '')}
- Craig has published writing listed in the profile. It can support a
  bullet in one clause; do not summarize its contents.
- If the profile has PLACEHOLDER fields, leave a clear "[FILL IN: ...]"
  marker rather than inventing content.

Respond ONLY with JSON, no other text, matching exactly this shape:
{json.dumps(COVER_LETTER_SCHEMA_EXAMPLE, indent=2)}
"""
    messages = [{"role": "user", "content": prompt}]
    letter = None

    # The style rules above are hard constraints, and prompt adherence alone
    # isn't reliable for them -- so check the draft and give one corrective
    # pass naming the specific violations before accepting it.
    for attempt in range(2):
        # 16384: the style and structure constraints make the model think at
        # length before writing. At 4096 the thinking block consumed the whole
        # budget and returned no text; at 8192 a letter was still truncated
        # mid-JSON on the second (corrective) pass, which carries the extra
        # context of the rejected draft.
        resp = client.messages.create(
            model=MODEL,
            max_tokens=16384,
            messages=messages,
        )
        text = extract_text(resp).strip()
        if resp.stop_reason == "max_tokens":
            # Truncated mid-JSON. Surfaces as a confusing "Unterminated string"
            # from the parser otherwise, which sent me hunting the wrong bug.
            raise ValueError(
                f"response truncated at max_tokens ({resp.usage.output_tokens} out); "
                "raise max_tokens in draft_cover_letter")
        letter = parse_json_response(text)
        # The boilerplate is fixed. Overwrite rather than trusting the model
        # to reproduce it verbatim; it paraphrases otherwise.
        letter["positioning"] = CL_POSITIONING
        letter["lead_in"] = CL_LEAD_IN
        letter["closing_para"] = CL_CLOSING_PARA
        letter.setdefault("greeting", "Dear Hiring Committee:")
        letter.setdefault("sign_off", "Sincerely,")
        letter["name"] = profile.get("name", "")
        letter["contact"] = f"{profile.get('email','')}\n{profile.get('phone','')}"

        problems = (find_ungrounded_claims(letter, profile)
                    + find_ai_tells(letter) + find_style_issues(letter, profile))
        if not problems:
            return letter
        if attempt == 0:
            print(f"    [style] rewriting cover letter, found: {'; '.join(problems)}")
            messages += [
                {"role": "assistant", "content": text},
                {"role": "user", "content":
                    "This draft has these problems: " + "; ".join(problems) + ". "
                    "Rewrite fixing them, but do not overcorrect into choppy, "
                    "uniform sentences -- that is worse than the original "
                    "problem. Untangle nested clauses rather than simply "
                    "shortening everything. Cut whole sentences to hit the word "
                    "count instead of trimming words from every sentence. Keep "
                    "the strongest concrete achievements, drop the weakest, and "
                    "let the writing breathe. Respond ONLY with the same JSON "
                    "shape."},
            ]

    remaining = (find_ungrounded_claims(letter, profile)
                 + find_ai_tells(letter) + find_style_issues(letter, profile))
    print(f"    [style] warning: cover letter still has: {'; '.join(remaining)} "
          f"-- review before sending")
    return letter


def resume_filename_base(company: str, title: str, output_dir: Path = OUTPUT_DIR) -> str:
    """
    company_title_YYYY-MM-DD, with a -2/-3/... suffix if that exact name is
    already taken (e.g. the same company posts the identical title twice)
    so results never silently overwrite each other.
    """
    base = f"{_sanitize_filename_part(company)}_{_sanitize_filename_part(title)}_{date.today().isoformat()}"
    candidate = base
    n = 2
    while (output_dir / f"{candidate}.json").exists() or (output_dir / f"{candidate}.docx").exists():
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def run(postings: list[dict]):
    """
    Scores/tailors only postings not already in scored_postings.json (matched
    by url, same as score_batch.py) so re-running the pipeline after new
    postings show up doesn't re-spend API calls -- or generate a fresh
    resume -- for ones already processed. Returns only the newly-processed
    entries; scored_postings.json on disk holds the full merged history.
    """
    profile = load_profile()
    OUTPUT_DIR.mkdir(exist_ok=True)
    scored_path = OUTPUT_DIR / "scored_postings.json"

    previous = json.loads(scored_path.read_text(encoding="utf-8")) if scored_path.exists() else []
    seen_urls = {r["url"] for r in previous if "url" in r}
    merged = {r["posting_id"]: r for r in previous}

    new_postings = [p for p in postings if p["url"] not in seen_urls]
    skipped = len(postings) - len(new_postings)
    if skipped:
        print(f"Skipping {skipped} already-scored posting(s)")

    def save():
        scored_path.write_text(json.dumps(list(merged.values()), indent=2), encoding="utf-8")

    new_results, failures = [], []
    for posting in new_postings:
        # One bad API response used to abort the whole run and discard every
        # posting scored before it, because the save happened only at the end.
        # Isolate each posting and checkpoint after each one instead.
        try:
            eval_result = score_posting(posting, profile)
        except Exception as e:
            print(f"[skip] scoring failed for {posting['title'][:44]} @ {posting['company']}: "
                  f"{type(e).__name__}: {str(e)[:90]}")
            failures.append((posting, "scoring", str(e)[:120]))
            continue

        entry = {**posting, **eval_result}
        print(f"[{entry['score']}/10] {posting['title']} @ {posting['company']}"
              f"{' (overqualification risk)' if entry.get('overqualification_risk') else ''}")

        if eval_result["score"] >= SCORE_THRESHOLD:
            base = resume_filename_base(posting["company"], posting["title"])
            try:
                resume_data = tailor_resume(posting, profile)
                json_path = OUTPUT_DIR / f"{base}.json"
                json_path.write_text(json.dumps(resume_data, indent=2), encoding="utf-8")
                entry["tailored_resume_json"] = str(json_path)
                entry["tailored_resume_docx"] = str(OUTPUT_DIR / f"{base}.docx")
                print(f"    -> resume drafted: {json_path.name} "
                      f"(run render_resume.js on it to produce the .docx)")
            except Exception as e:
                print(f"    [warn] resume failed: {type(e).__name__}: {str(e)[:90]}")
                failures.append((posting, "resume", str(e)[:120]))

            try:
                cover_data = draft_cover_letter(posting, profile)
                cover_json_path = OUTPUT_DIR / f"{base}_cover.json"
                cover_json_path.write_text(json.dumps(cover_data, indent=2), encoding="utf-8")
                entry["cover_letter_json"] = str(cover_json_path)
                entry["cover_letter_docx"] = str(OUTPUT_DIR / f"{base}_cover.docx")
                print(f"    -> cover letter drafted: {cover_json_path.name}")
            except Exception as e:
                print(f"    [warn] cover letter failed: {type(e).__name__}: {str(e)[:90]}")
                failures.append((posting, "cover letter", str(e)[:120]))

        merged[entry["posting_id"]] = entry
        new_results.append(entry)
        save()          # checkpoint, so a later crash can't undo this posting

    save()
    if failures:
        print(f"\n  {len(failures)} step(s) failed and were skipped:")
        for posting, stage, err in failures:
            print(f"    {posting['company']} - {posting['title'][:40]} [{stage}]: {err}")
        print("  Run `python score_and_tailor.py --repair` to retry the missing "
              "documents without re-scoring.")
    return new_results


def repair():
    """
    Fill in documents missing from already-scored postings.

    A posting whose resume succeeded but whose cover letter failed is still
    written to scored_postings.json, so the normal run skips it on the next
    pass as already-scored. This retries just the missing pieces.
    """
    profile = load_profile()
    scored_path = OUTPUT_DIR / "scored_postings.json"
    data = json.loads(scored_path.read_text(encoding="utf-8"))

    todo = [e for e in data
            if e.get("score", 0) >= SCORE_THRESHOLD
            and (not e.get("tailored_resume_json") or not e.get("cover_letter_json"))]
    if not todo:
        print("Nothing to repair: every qualifying posting has both documents.")
        return []

    print(f"Repairing {len(todo)} posting(s) with missing documents\n")
    fixed = []
    for entry in todo:
        label = f"{entry['company']} - {entry['title'][:44]}"
        base = (Path(entry["tailored_resume_json"]).stem if entry.get("tailored_resume_json")
                else resume_filename_base(entry["company"], entry["title"]))
        print(f"[{entry['score']}/10] {label}")

        if not entry.get("tailored_resume_json"):
            try:
                d = tailor_resume(entry, profile)
                p = OUTPUT_DIR / f"{base}.json"
                p.write_text(json.dumps(d, indent=2), encoding="utf-8")
                entry["tailored_resume_json"] = str(p)
                entry["tailored_resume_docx"] = str(OUTPUT_DIR / f"{base}.docx")
                print(f"    -> resume drafted: {p.name}")
            except Exception as e:
                print(f"    [warn] resume still failing: {type(e).__name__}: {str(e)[:90]}")

        if not entry.get("cover_letter_json"):
            try:
                d = draft_cover_letter(entry, profile)
                p = OUTPUT_DIR / f"{base}_cover.json"
                p.write_text(json.dumps(d, indent=2), encoding="utf-8")
                entry["cover_letter_json"] = str(p)
                entry["cover_letter_docx"] = str(OUTPUT_DIR / f"{base}_cover.docx")
                print(f"    -> cover letter drafted: {p.name}")
            except Exception as e:
                print(f"    [warn] cover letter still failing: {type(e).__name__}: {str(e)[:90]}")

        scored_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        fixed.append(entry)
    return fixed


if __name__ == "__main__":
    import sys as _sys
    if "--repair" in _sys.argv:
        repair()
    else:
        from scraper import collect_all_postings
        run(collect_all_postings())
