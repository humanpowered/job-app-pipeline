"""
Checks the setup and reports what works, what is missing, and what that costs
you. Run it after cloning, after changing config, or when a nightly run did
something surprising.

Makes no API calls and writes nothing. The optional dry run scrapes the free
sources so you can see how many postings survive your filters before spending
anything on scoring.

  python doctor.py                 # configuration and credentials only
  python doctor.py --dry-run       # also scrape the free sources and count
  python doctor.py --dry-run --include-paid   # also run the paid LinkedIn actor

Exit code is 1 when something is actually broken, so a scheduled wrapper can
tell "misconfigured" from "nothing to do".
"""
import csv
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SRC = Path(__file__).parent
ROOT = SRC.parent
CONFIG = ROOT / "config"
PROFILE = ROOT / "profile"
OUTPUT = ROOT / "output"
LOGS = ROOT / "logs"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OK, WARN, FAIL = "  ok  ", " warn ", " FAIL "
_problems = {"fail": 0, "warn": 0}


def line(status: str, what: str, detail: str = "") -> None:
    if status is FAIL:
        _problems["fail"] += 1
    elif status is WARN:
        _problems["warn"] += 1
    print(f"[{status}] {what}" + (f"  —  {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


# --- runtime ----------------------------------------------------------------

def check_runtime() -> None:
    section("Runtime")
    v = sys.version_info
    if v >= (3, 10):
        line(OK, f"Python {v.major}.{v.minor}.{v.micro}")
    else:
        line(FAIL, f"Python {v.major}.{v.minor}", "3.10 or newer is required")

    for module, why in (("anthropic", "scoring and drafting"),
                        ("requests", "every job source"),
                        ("yaml", "reading config")):
        try:
            __import__(module)
            line(OK, f"{module} installed")
        except ImportError:
            line(FAIL, f"{module} missing", f"needed for {why}: pip install -r requirements.txt")

    try:
        out = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=20)
        line(OK, f"Node {out.stdout.strip()}", "renders the .docx files")
    except Exception:
        line(WARN, "Node not found", "scoring still works; .docx rendering does not")
    if (SRC / "node_modules" / "docx").is_dir():
        line(OK, "docx package installed")
    else:
        line(WARN, "docx package missing", "run: cd src && npm install")


# --- credentials ------------------------------------------------------------

def check_credentials() -> dict:
    section("Credentials")
    have = {}

    anthropic_ok = bool(os.environ.get("ANTHROPIC_API_KEY")
                        or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    if not anthropic_ok:
        cfg_dir = os.environ.get("ANTHROPIC_CONFIG_DIR")
        base = (Path(cfg_dir) if cfg_dir
                else Path(os.environ.get("APPDATA", "")) / "Anthropic" if os.name == "nt"
                else Path.home() / ".config" / "anthropic")
        creds = base / "credentials"
        anthropic_ok = creds.is_dir() and any(creds.glob("*.json"))
        if anthropic_ok:
            line(OK, "Anthropic sign-in", "OAuth profile from `ant auth login`")
    if anthropic_ok and os.environ.get("ANTHROPIC_API_KEY"):
        line(OK, "ANTHROPIC_API_KEY set")
    elif not anthropic_ok:
        line(FAIL, "No Anthropic credential", "nothing can be scored or drafted")
    have["anthropic"] = anthropic_ok

    optional = {
        "jooble": (["JOOBLE_API_KEY"], "Jooble and the company watchlist"),
        "adzuna": (["ADZUNA_APP_ID", "ADZUNA_APP_KEY"], "the Adzuna source"),
        "apify": (["APIFY_TOKEN"], "LinkedIn (paid, billed per result)"),
        "imap": (["IMAP_USER", "IMAP_APP_PASSWORD"], "the mailbox source and reply checking"),
    }
    for name, (vars_, what) in optional.items():
        missing = [v for v in vars_ if not os.environ.get(v)]
        have[name] = not missing
        if missing:
            line(WARN, f"{', '.join(missing)} not set", f"{what} will be skipped")
        else:
            line(OK, f"{name} credentials set", what)
    return have


# --- configuration ----------------------------------------------------------

def check_config() -> dict:
    section("Configuration")
    info = {"titles": 0, "excludes": 0, "sources": [], "threshold": None}
    sys.path.insert(0, str(SRC))

    try:
        import yaml
        cfg = yaml.safe_load((CONFIG / "boards.yaml").read_text(encoding="utf-8")) or {}
        line(OK, "boards.yaml parses")
    except FileNotFoundError:
        line(FAIL, "boards.yaml missing", "copy boards.example.yaml to boards.yaml")
        return info
    except Exception as exc:
        line(FAIL, "boards.yaml is not valid YAML", str(exc)[:90])
        return info

    try:
        import settings
        tuning = settings.load_tuning()
        info["threshold"] = tuning["score_threshold"]
        line(OK, "tuning.yaml" if settings.TUNING_YAML.exists() else "tuning defaults",
             f"model {tuning['model']}, draft at {tuning['score_threshold']}+, "
             f"floor ${tuning['salary_floor']:,}")

        frame, tells = settings.load_letter()
        line(OK, "letter.yaml" if settings.LETTER_YAML.exists() else "letter defaults",
             f"{len(tells)} phrases the lint rejects")
        if frame["positioning"] == settings.PLACEHOLDER_POSITIONING:
            line(FAIL, "Cover letter positioning is still the placeholder",
                 "write frame.positioning in config/letter.yaml in your own voice")

        titles = settings.load_titles()
        if titles:
            info["titles"], info["excludes"] = len(titles[0]), len(titles[1])
        else:
            info["titles"] = len(cfg.get("title_keywords") or [])
            info["excludes"] = len(cfg.get("exclude_title_keywords") or [])
            line(WARN, "No titles.csv", "falling back to the lists in boards.yaml")
        if not info["titles"]:
            line(FAIL, "No search terms", "nothing would ever match")
        else:
            line(OK, f"{info['titles']} search term(s), {info['excludes']} exclusion(s)")
    except SystemExit as exc:
        line(FAIL, "A config file is malformed", str(exc).splitlines()[0].replace("[FATAL] ", ""))

    counts = {
        "greenhouse": len(cfg.get("greenhouse") or []),
        "lever": len(cfg.get("lever") or []),
        "ashby": len(cfg.get("ashby") or []),
        "workday": len(cfg.get("workday") or []),
        "smartrecruiters": len(cfg.get("smartrecruiters") or []),
    }
    for name, n in counts.items():
        if n:
            info["sources"].append(f"{name} ({n})")
    for name in ("jooble", "adzuna", "remotive", "jobicy", "careerjet",
                 "company_watchlist", "email", "apify_linkedin"):
        block = cfg.get(name) or {}
        if isinstance(block, dict) and block.get("enabled"):
            info["sources"].append(name)
    line(OK, f"{len(info['sources'])} source(s) enabled", ", ".join(info["sources"]))
    if not cfg.get("locations"):
        line(WARN, "No locations set", "every location will be accepted")
    return info


# --- profile ----------------------------------------------------------------

def check_profile() -> None:
    section("Profile")
    path = PROFILE / "master_profile.json"
    if not path.exists():
        line(FAIL, "master_profile.json missing",
             "copy profile/master_profile.example.json and fill it in")
        return
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        line(FAIL, "master_profile.json is not valid JSON", str(exc)[:90])
        return

    for field in ("name", "email", "location"):
        if (profile.get(field) or "").strip():
            line(OK, f"{field} set")
        else:
            line(FAIL, f"{field} missing", "it appears on every document")

    if (profile.get("name") or "") == "Jordan Avery":
        line(WARN, "Profile is still the shipped example", "documents would go out as Jordan Avery")

    jobs = profile.get("work_history") or []
    if not jobs:
        line(FAIL, "work_history is empty", "there is nothing to build a resume from")
    else:
        thin = [j.get("company", "?") for j in jobs if len(j.get("highlights") or []) < 2]
        line(OK, f"{len(jobs)} role(s), "
                 f"{sum(len(j.get('highlights') or []) for j in jobs)} highlight(s)")
        if thin:
            line(WARN, f"{len(thin)} role(s) with fewer than 2 highlights",
                 ", ".join(thin[:4]))

    skills = PROFILE / "skills_inventory.csv"
    if not skills.exists():
        line(WARN, "No skills_inventory.csv", "skills come from master_profile.json instead")
    else:
        with open(skills, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        yes = [r for r in rows if (r.get("have_it") or "").strip().lower() == "yes"]
        gaps = len(rows) - len(yes)
        if yes:
            line(OK, f"{len(yes)} claimed skill(s)", f"{gaps} tracked as gaps")
        else:
            line(FAIL, "skills_inventory.csv has no have_it=yes rows",
                 "every document would ship without skills")


# --- workspace --------------------------------------------------------------

def check_workspace() -> None:
    section("Workspace")
    try:
        OUTPUT.mkdir(exist_ok=True)
        probe = OUTPUT / ".doctor_write_test"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        line(OK, "output/ is writable")
    except Exception as exc:
        line(FAIL, "output/ is not writable", str(exc)[:80])

    tracker = OUTPUT / "application_tracker.csv"
    if tracker.exists():
        try:
            with open(tracker, "a", encoding="utf-8"):
                pass
            with open(tracker, newline="", encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))
            applied = sum(1 for r in rows if (r.get("date_submitted") or "").strip())
            line(OK, f"tracker: {len(rows)} row(s), {applied} applied")
        except PermissionError:
            line(WARN, "application_tracker.csv is locked",
                 "close it in Excel or the run writes a .NEW.csv instead")
    else:
        line(OK, "no tracker yet", "it is created on the first run")

    logs = sorted(LOGS.glob("pipeline_*.log")) if LOGS.exists() else []
    if not logs:
        line(WARN, "No run logs yet", "the pipeline has not run here")
        return
    newest = logs[-1]
    text = newest.read_text(encoding="utf-8", errors="replace")
    age = (datetime.now() - datetime.fromtimestamp(newest.stat().st_mtime)).days
    tail = "\n".join(text.splitlines()[-6:])
    if "[ERROR]" in tail:
        line(FAIL, f"last run ended in ERROR ({newest.name})", "see the end of that log")
    elif "[OK" in tail:
        line(OK, f"last run completed ({newest.name})", f"{age} day(s) ago")
    else:
        line(WARN, f"last run has no verdict ({newest.name})", "it may have been interrupted")


# --- optional dry run -------------------------------------------------------

def dry_run(info: dict, include_paid: bool) -> None:
    section("Dry run (no scoring, nothing written)")
    sys.path.insert(0, str(SRC))
    import scraper
    import yaml

    if not include_paid:
        # the LinkedIn actor bills per result, so it stays out of a check
        cfg_path = CONFIG / "boards.yaml"
        original = cfg_path.read_text(encoding="utf-8")
        cfg = yaml.safe_load(original) or {}
        if (cfg.get("apify_linkedin") or {}).get("enabled"):
            line(WARN, "Skipping the paid LinkedIn source",
                 "it bills per result; add --include-paid to exercise it")
            scraper.load_config = lambda: {**cfg, "apify_linkedin": {"enabled": False}}

    from collections import Counter
    postings = scraper.collect_all_postings()
    by_source = Counter(p["source"] for p in postings)
    print()
    line(OK, f"{len(postings)} posting(s) survive your filters")
    for source, n in by_source.most_common():
        print(f"         {source:12} {n}")

    scored_path = OUTPUT / "scored_postings.json"
    if scored_path.exists():
        seen = {e.get("url") for e in json.loads(scored_path.read_text(encoding="utf-8"))}
        fresh = [p for p in postings if p["url"] not in seen]
        print()
        line(OK, f"{len(fresh)} of them have never been scored",
             "the rest cost nothing on the next run")
        if fresh and info.get("threshold") is not None:
            print(f"         a real run would score those {len(fresh)}, then draft "
                  f"documents for any that reach {info['threshold']}/10")
    else:
        print()
        line(OK, f"all {len(postings)} would be scored on the first run")


def main() -> int:
    print("job-app-pipeline doctor")
    check_runtime()
    creds = check_credentials()
    info = check_config()
    check_profile()
    check_workspace()

    if "--dry-run" in sys.argv:
        if not creds.get("anthropic"):
            print()
            line(WARN, "Running the dry run anyway", "it makes no API calls")
        try:
            dry_run(info, include_paid="--include-paid" in sys.argv)
        except Exception as exc:
            line(FAIL, "Dry run failed", f"{type(exc).__name__}: {str(exc)[:90]}")

    section("Summary")
    if _problems["fail"]:
        print(f"{_problems['fail']} problem(s) to fix, {_problems['warn']} warning(s).")
        return 1
    if _problems["warn"]:
        print(f"Ready to run. {_problems['warn']} warning(s) worth reading above.")
        return 0
    print("Everything checks out.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
