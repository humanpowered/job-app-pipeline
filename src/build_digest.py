"""
Writes MORNING_BRIEF.md: what the nightly run found and what needs attention.

Called at the end of run_nightly.cmd. Also runnable by hand any time.

Covers things the raw log does not: applications that have gone quiet,
postings that disappeared after you applied, and anything drafted but not
yet sent.
"""
import csv
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

SRC = Path(__file__).parent
OUTPUT_DIR = SRC.parent / "output"
PROJECT = SRC.parent.parent
BRIEF = PROJECT / "MORNING_BRIEF.md"
LOGS = SRC.parent / "logs"

FOLLOW_UP_DAYS = 10          # applied this long ago with no status movement
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def parse_date(s: str):
    s = (s or "").strip()
    if not s or s.upper() == "NA":
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def last_log() -> tuple[str, list[str]]:
    files = sorted(LOGS.glob("pipeline_*.log")) if LOGS.exists() else []
    if not files:
        return "", []
    text = files[-1].read_text(encoding="utf-8", errors="replace")
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    # A run starts at its timestamp line. Splitting on the "===" banner drops
    # that line into the preceding block, so anchor on the timestamp instead.
    starts = [i for i, ln in enumerate(lines)
              if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", ln.strip())]
    if starts:
        lines = lines[starts[-1]:]
    return files[-1].name, lines


def main():
    rows = []
    tracker = OUTPUT_DIR / "application_tracker.csv"
    if tracker.exists():
        with open(tracker, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))

    today = date.today()
    log_name, log_lines = last_log()

    ran_at = next((ln for ln in log_lines if re.match(r"\d{4}-\d{2}-\d{2} \d{2}:", ln)), "")
    found = next((ln.strip() for ln in log_lines if "postings matching" in ln), "")
    errors = [ln for ln in log_lines if "[ERROR]" in ln or "[warn]" in ln or "FAILED" in ln]
    ok = any("[OK]" in ln for ln in log_lines)

    # newly scored this run: lines like "[8/10] Title @ company"
    scored_now = [ln.strip() for ln in log_lines if re.match(r"\[\d+/10\]", ln.strip())]

    def has_docs(r):
        return bool(r.get("resume"))

    ready = [r for r in rows if has_docs(r) and not (r.get("date_submitted") or "").strip()]

    quiet = []
    for r in rows:
        d = parse_date(r.get("date_submitted", ""))
        status = (r.get("status") or "").strip().lower()
        if d and status in ("", "applied") and (today - d).days >= FOLLOW_UP_DAYS:
            quiet.append(((today - d).days, r))
    quiet.sort(key=lambda x: -x[0])

    vanished = [r for r in rows
                if (r.get("notes") or "").startswith("[no longer listed")
                and (r.get("status") or "").strip().lower() != "closed"]

    L = [f"# Morning brief — {today.strftime('%A, %B %d, %Y')}", ""]

    L.append("## Last run")
    if ran_at:
        status_word = "completed" if ok else "DID NOT COMPLETE CLEANLY"
        L.append(f"- {ran_at} — {status_word}")
    else:
        L.append("- No run log found.")
    if found:
        L.append(f"- {found}")
    if scored_now:
        L.append(f"- {len(scored_now)} new posting(s) scored:")
        for s in scored_now:
            L.append(f"    - {s}")
    else:
        L.append("- No new postings; everything found was already scored.")
    if errors:
        L.append(f"- **{len(errors)} warning(s)/error(s):**")
        for e in errors[:5]:
            L.append(f"    - `{e.strip()[:110]}`")
    L.append("")

    L.append(f"## Ready to apply ({len(ready)})")
    if ready:
        for r in sorted(ready, key=lambda x: -int(x.get("score") or 0)):
            flag = "  ⚠ overqualification risk" if r.get("overqualified") else ""
            sal = f"  {r['salary']}" if r.get("salary") else ""
            L.append(f"- **[{r.get('score')}/10]** {r.get('company')} — {r.get('title')}{sal}{flag}")
            L.append(f"    - {r.get('url','')}")
    else:
        L.append("- Nothing waiting. Every drafted document has a disposition.")
    L.append("")

    L.append(f"## Gone quiet ({len(quiet)})")
    if quiet:
        L.append(f"Applied {FOLLOW_UP_DAYS}+ days ago, still marked applied or blank:")
        for days, r in quiet:
            L.append(f"- **{days} days** — {r.get('company')} — {r.get('title')}"
                     f" (sent {r.get('date_submitted')})")
        L.append("")
        L.append("Worth a follow-up, or update `status` in the tracker.")
    else:
        L.append("- Nothing overdue.")
    L.append("")

    if vanished:
        L.append(f"## Postings that disappeared ({len(vanished)})")
        for r in vanished:
            L.append(f"- {r.get('company')} — {r.get('title')}")
        L.append("")
        L.append("Set `status` to `closed` in the tracker to stop these appearing.")
        L.append("")

    applied = sum(1 for r in rows if parse_date(r.get("date_submitted", "")))
    L.append("## Totals")
    L.append(f"- {len(rows)} postings tracked, {applied} applied, {len(ready)} ready")
    if log_name:
        L.append(f"- Full log: `job-app-assistant/logs/{log_name}`")

    BRIEF.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"  wrote {BRIEF.name}: {len(scored_now)} new, {len(ready)} ready, "
          f"{len(quiet)} gone quiet")


if __name__ == "__main__":
    main()
