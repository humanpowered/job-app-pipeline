"""
A coached interview that builds up profile/master.md, one accomplishment at a
time.

Usage:
  python interview.py                  # pick up wherever the record is thinnest
  python interview.py --role Northwind # work on one employer
  python interview.py --coverage       # just show what the record holds
  python interview.py --target 15      # aim higher than the default of 10

Why an interview and not a form. The hard part of a master accomplishment
record is not typing, it is recall: people cannot list fifteen accomplishments
on demand, and a resume has already trained them to name only the two or three
that suited one application. A question they can answer -- "what was wrong
before you got there?" -- surfaces work that "list your achievements" does not.

The coaching is one idea, applied per accomplishment: look for evidence in
descending order of strength, and stop at the first rung that holds.

  metric       a number they already knew
  derived      a number worked out from before-and-after, which most people are
               holding without realising. "Three days a month became half a
               day" is a number; "I automated the reporting" is not.
  scope        the size of the thing -- headcount, budget, markets, volume. Not
               an outcome, but it tells a reader how big the work was.
  qualitative  a contribution with no number attached. A real answer, recorded
               as what it is rather than dressed up as a result.

Which rung an accomplishment landed on is stored, so the build step can prefer
quantified material for postings that reward it, and so the person can see
their own coverage.

Two rules that keep this from becoming tiring, which is the main risk:

  - it never asks about something already in the file. Existing titles go into
    the prompt and near-duplicates are refused locally.
  - the nudge toward more accomplishments is a coverage number, shown once per
    role, not the question "any more?" asked ten times.

Everything is saved as it is confirmed. Stop whenever you like -- Ctrl-C is a
supported way to leave -- and run it again later.
"""
import argparse
import json
import re
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent
ROOT = SRC.parent
MASTER = ROOT / "profile" / "master.md"
STATE = ROOT / "profile" / ".interview_state.json"
sys.path.insert(0, str(SRC))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import master_record as mr
from master_record import Accomplishment, Role
from score_and_tailor import (SCORE_THRESHOLD, require_credentials, request_json,  # noqa: F401
                              spend_summary)

COACH = """You are interviewing someone to build a master record of their career
accomplishments. This record is not a resume: it has no page limit, and later
steps select from it per job application. Your job is to get as much real
material out of them as possible, and to find the numbers they are holding
without realising.

Rules you must follow:

1. ONE question per turn. Short. No preamble, no summarising what they just
   said back to them, no praise.
2. Walk the evidence ladder for each accomplishment, and stop at the first rung
   that holds:
   - metric: a number they already know.
   - derived: a number you work out with them. If they describe something being
     faster, cheaper, cleaner or less manual, ask how long or how much it was
     before, what it is now, and how often it happens. Then state the figure
     you derived and ask if it is right.
   - scope: the size of the work -- people managed, budget, markets, clients,
     volume of data, number of sites.
   - qualitative: what decision it enabled, what problem it ended, who it
     helped. Record this plainly. It is a legitimate answer, not a failure, and
     you must not invent a number to avoid it.
3. Never put a number in a field that the person did not give you or confirm.
4. Work for any profession. Do not assume office work, marketing, or software.
   A nurse, a plant manager and a teacher must all be able to answer you.
5. If they say they cannot remember or want to move on, accept it immediately
   and set status to "role_done". Do not push twice.
6. Do not ask about an accomplishment already recorded. The recorded titles are
   listed for you; if they start describing one of them, say so and ask for a
   different one.

Return ONLY JSON in this exact shape:
{
  "say": "the single thing to show them -- a question, or your derived figure to confirm",
  "draft": {"title": "", "problem": "", "actions": "", "results": "", "evidence": ""},
  "status": "asking" | "complete" | "role_done"
}

- Fill "draft" with everything you have so far; carry forward what you already
  had and add to it. Leave a field "" until you have a real answer for it.
- "title" is a short label, a few words, no metrics in it.
- "evidence" is exactly one of: metric, derived, scope, qualitative.
- Use status "complete" only when title, problem, actions, results and evidence
  are all filled. Then "say" should be one short line confirming what you
  recorded.
- Use status "role_done" when they are finished with this role.
"""


# The contract as a constraint the API enforces, not a request the prompt makes.
# Without it the model answered the person and left the JSON out, because the
# rest of this prompt is entirely about holding a conversation.
INTERVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "say": {"type": "string"},
        "draft": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "problem": {"type": "string"},
                "actions": {"type": "string"},
                "results": {"type": "string"},
                "evidence": {"type": "string",
                             "enum": list(mr.EVIDENCE_TIERS) + [""]},
            },
            "required": ["title", "problem", "actions", "results", "evidence"],
            "additionalProperties": False,
        },
        "status": {"type": "string",
                   "enum": ["asking", "complete", "role_done"]},
    },
    "required": ["say", "draft", "status"],
    "additionalProperties": False,
}


def ask(question: str) -> str:
    """Read a line, treating Ctrl-C and Ctrl-D as "stop", not as a crash."""
    try:
        return input(f"\n{question}\n> ").strip()
    except (KeyboardInterrupt, EOFError):
        raise Stop()


class Stop(Exception):
    """The person chose to leave. Not an error."""


# Typed answers that mean "I am finished with this role".
GIVE_UP = {"done", "stop", "next", "no more", "skip", "nothing", "that's all",
           "thats all", "no", "n", "none"}


def load_record() -> mr.Record:
    if MASTER.exists():
        return mr.parse(MASTER.read_text(encoding="utf-8"))
    return mr.Record(header=["# Master accomplishment record", "",
                             "Built by `interview.py`. Edit it by hand any time;",
                             "the interview reads whatever is here and asks only",
                             "about what is missing."])


def save(rec: mr.Record) -> None:
    """Write via a temporary file, so an interruption mid-write cannot leave a
    half-written record where a complete one used to be."""
    MASTER.parent.mkdir(parents=True, exist_ok=True)
    tmp = MASTER.with_suffix(".md.tmp")
    tmp.write_text(mr.render(rec), encoding="utf-8")
    tmp.replace(MASTER)


def show_coverage(rec: mr.Record, target: int) -> None:
    if not rec.roles:
        print("  no roles recorded yet")
        return
    width = max(len(r.employer) for r in rec.roles) + 2
    for role in by_need(rec):
        print(f"  {role.employer[:40]:{width}} {mr.coverage_note(role, target)}")
    total = sum(r.coverage()["total"] for r in rec.roles)
    quant = sum(r.coverage()["quantified"] for r in rec.roles)
    print(f"\n  {total} accomplishment(s) across {len(rec.roles)} role(s), "
          f"{quant} with a number")


def by_need(rec: mr.Record) -> list:
    """Thinnest first: that is where an hour of someone's time is worth most."""
    return sorted(rec.roles, key=lambda r: (r.coverage()["total"],
                                            r.coverage()["quantified"]))


def add_roles(rec: mr.Record) -> None:
    """Collect the employment skeleton when there is no record yet."""
    print("\nNo roles on file yet. List them newest first; press Enter on an "
          "empty employer when you are done.")
    while True:
        employer = ask("Employer?")
        if not employer:
            break
        title = ask(f"Your job title at {employer}?")
        dates = ask("Dates? (anything readable, e.g. 2019 - 2022)")
        role = Role(employer=employer, title=title)
        if dates:
            role.fields["Dates"] = dates
        rec.roles.append(role)
        save(rec)
        print(f"  saved {employer}")


# When someone says they are out of material, they usually mean they are out of
# material of the kind they have been thinking about. One prompt from a
# different direction is worth asking; a second one is nagging.
#
# The lens offered is the one least represented in what the role already holds,
# so it is a genuinely different question rather than a rephrasing of the last
# one. Picked in code rather than by the model, because "ask exactly once, from
# an angle not already covered" is a rule worth enforcing rather than hoping for.
LENSES = [
    ("people", ("hire", "hired", "train", "coach", "mentor", "team", "staff",
                "promot", "recruit"),
     "Who did you hire, train or promote there, and what changed because of it?"),
    ("fixing", ("broken", "fixed", "mess", "cleaned", "rescued", "failing",
                "backlog", "inherit"),
     "What was broken or neglected when you arrived that you ended up fixing?"),
    ("money", ("cost", "saved", "revenue", "budget", "margin", "profit",
               "spend", "price"),
     "Did anything you did make or save the organisation money, even indirectly?"),
    ("speed", ("faster", "hours", "days", "automat", "manual", "time",
               "turnaround", "delay"),
     "What used to take a long time, or take a lot of hands, that stopped doing so?"),
    ("risk", ("audit", "complian", "outage", "safety", "error", "fraud",
              "risk", "incident", "quality"),
     "Did you prevent something going wrong, or catch something that had?"),
    ("legacy", ("standard", "adopted", "documented", "still", "rolled out",
                "template", "process", "handbook"),
     "Is anything you built or wrote there still in use after you left?"),
    ("stopping", ("stopped", "retired", "simplif", "consolidat", "removed",
                  "cancelled", "declined"),
     "Did you stop, simplify or retire something that was not worth doing?"),
]


def pick_lens(role: Role) -> tuple[str, str]:
    """The angle least covered by what this role already records."""
    recorded = " ".join(
        [a.title + " " + a.problem + " " + a.actions + " " + a.results
         for a in role.accomplishments] + list(role.recorded_bullets)).lower()
    scored = [(sum(recorded.count(k) for k in keys), name, question)
              for name, keys, question in LENSES]
    scored.sort(key=lambda s: s[0])
    return scored[0][1], scored[0][2]


def near_duplicate(role: Role, title: str) -> str | None:
    """Has this already been recorded? Compared on content words, because the
    same accomplishment gets described twice with different phrasing."""
    def words(text):
        return {w for w in re.findall(r"[a-z]{4,}", (text or "").lower())}

    new = words(title)
    if not new:
        return None
    for acc in role.accomplishments:
        existing = words(acc.title)
        if existing and len(new & existing) / len(new | existing) > 0.5:
            return acc.title
    for bullet in role.recorded_bullets:
        existing = words(bullet)
        if existing and len(new & existing) >= max(3, len(new) * 0.6):
            return bullet[:60] + "..."
    return None


def interview_role(rec: mr.Record, role: Role, target: int) -> None:
    print(f"\n{'=' * 68}\n{role.label()}")
    print(f"  {mr.coverage_note(role, target, prompt=True)}")
    recorded = [a.title for a in role.accomplishments] + list(role.recorded_bullets)
    if recorded:
        print("\n  already recorded, so I will not ask about these:")
        for item in recorded:
            print(f"    - {item[:88]}")

    lens_offered = False
    seed = None             # (question, their answer) to open the next round with

    def one_more_angle():
        """
        Ask once, from a direction the record does not already cover, then let
        go for good.

        Someone saying they are out of material usually means they are out of
        the kind they have been thinking about. The rule is exactly one more
        question: the counter lives here rather than in the prompt, because
        "once, and then stop" is the sort of thing a model will drift on.
        """
        nonlocal lens_offered, seed
        if lens_offered:
            return False
        lens_offered = True
        name, question = pick_lens(role)
        print(f"\n  One more angle before we move on, then I'll leave it ({name}).")
        answer = ask(f"{question}\n  (press Enter to skip)")
        if not answer or answer.lower() in GIVE_UP:
            return False
        seed = (question, answer)
        return True

    while True:
        draft = Accomplishment()
        # Rebuilt each time round, not once per role: the list has to include
        # the accomplishment recorded a minute ago, or the next question can
        # ask about the same thing again.
        context = {
            "employer": role.employer,
            "job_title": role.title,
            "dates": role.fields.get("Dates", ""),
            "already_recorded": ([a.title for a in role.accomplishments]
                                 + list(role.recorded_bullets)),
        }
        messages = [{"role": "user", "content":
                     f"{COACH}\n\nROLE CONTEXT:\n{json.dumps(context, indent=2)}\n\n"
                     f"Begin. Ask your first question about an accomplishment "
                     f"that is not already recorded."}]
        if seed:
            # carry the answer they just gave into a fresh conversation, so the
            # lens question is not asked twice
            question, answer = seed
            seed = None
            messages += [
                {"role": "assistant", "content": json.dumps(
                    {"say": question, "draft": {}, "status": "asking"})},
                {"role": "user", "content": answer},
            ]
        while True:
            reply, _raw = request_json(messages, 4096, "interview",
                                       schema=INTERVIEW_SCHEMA)
            say = (reply.get("say") or "").strip()
            status = reply.get("status", "asking")
            got = reply.get("draft") or {}
            for key in ("title", "problem", "actions", "results", "evidence"):
                if got.get(key):
                    setattr(draft, key, str(got[key]).strip())

            if status == "role_done":
                if say:
                    print(f"\n{say}")
                if one_more_angle():
                    break           # start a fresh round seeded with their answer
                return

            if status == "complete":
                dupe = near_duplicate(role, draft.title)
                if dupe:
                    print(f"\n  that looks like one already on file: {dupe}")
                    messages.append({"role": "assistant", "content": json.dumps(reply)})
                    messages.append({"role": "user", "content":
                                     f"That duplicates a recorded accomplishment "
                                     f"({dupe!r}). Ask for a different one."})
                    continue
                if draft.evidence not in mr.EVIDENCE_TIERS:
                    draft.evidence = "qualitative"
                role.accomplishments.append(draft)
                save(rec)
                print(f"\n  recorded: {draft.title}")
                print(f"  evidence: {draft.evidence} "
                      f"({mr.EVIDENCE_HELP[draft.evidence]})")
                print(f"  {mr.coverage_note(role, target)}")
                break

            answer = ask(say or "Go on?")
            if not answer or answer.lower() in GIVE_UP:
                if not answer:
                    print("\n  nothing recorded for that one.")
                if one_more_angle():
                    break
                return
            messages.append({"role": "assistant", "content": json.dumps(reply)})
            messages.append({"role": "user", "content": answer})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--role", help="work on this employer (substring match)")
    ap.add_argument("--coverage", action="store_true", help="show the record and exit")
    ap.add_argument("--target", type=int, default=10,
                    help="accomplishments per role to aim for (default 10)")
    args = ap.parse_args()

    rec = load_record()

    if args.coverage:
        print(f"\n{MASTER}")
        show_coverage(rec, args.target)
        return 0

    require_credentials()

    if not rec.roles:
        try:
            add_roles(rec)
        except Stop:
            print("\n  stopped. Run again to carry on.")
            return 0
        if not rec.roles:
            print("  nothing to interview about yet.")
            return 0

    print(f"\n{MASTER}")
    show_coverage(rec, args.target)

    if args.role:
        wanted = [r for r in rec.roles
                  if args.role.lower() in r.employer.lower()]
        if not wanted:
            raise SystemExit(f"\nNo role matching {args.role!r}. "
                             f"Known: {', '.join(r.employer for r in rec.roles)}")
        queue = wanted
    else:
        queue = by_need(rec)

    try:
        for role in queue:
            interview_role(rec, role, args.target)
            if role is not queue[-1]:
                more = ask("Move on to the next role? (Enter for yes, 'stop' to finish)")
                if more.lower() in ("stop", "n", "no", "quit"):
                    break
    except Stop:
        print("\n\n  stopped, and everything confirmed so far is saved.")
    finally:
        save(rec)
        print()
        show_coverage(rec, args.target)
        summary = spend_summary()
        if summary:
            print(f"\n{summary}")
        print(f"\n  {MASTER}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
