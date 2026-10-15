"""
The nightly run: the pipeline, the reply check, and the morning brief, written
to logs/pipeline_YYYY-MM-DD.log as well as to the screen.

Usage:
  python run_nightly.py              # run everything, append to today's log
  python run_nightly.py --keep 60    # keep 60 days of logs instead of 30

This exists because the logging used to live in a Windows .cmd file that
redirected stdout. Three things followed from that, none of them obvious:
`python run_pipeline.py` -- which is what the README tells you to run --
produced no log at all, the morning brief had nothing to read, and doctor's
dead-source and retry-rate checks sat permanently idle reporting "not enough
history". The diagnostics were only ever alive on one machine.

So the orchestration is here, in Python, on any OS. run_nightly.cmd and
run_nightly.sh are one-line wrappers for Task Scheduler and cron.

Two details worth keeping if you edit this:

  - PYTHONIOENCODING=utf-8 for the children. Writing to a pipe makes Python's
    stdout cp1252 on Windows, and a posting title carrying a macron or an
    accent then kills the run mid-scoring. One did, on U+014C.
  - the verdict lines at the end ([OK], [ERROR], [OK with WARNING]) are read
    by doctor and by build_digest. They are an interface, not decoration.
"""
import argparse
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

SRC = Path(__file__).resolve().parent
ROOT = SRC.parent
LOGS = ROOT / "logs"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError, OSError):
    pass            # no usable stdout; the log is what matters, see say()


def say(text: str, log=None) -> None:
    """
    Write a line to the log, and to the screen when there is one.

    A scheduler can start this with no console at all, and on Windows a write
    to that stdout raises rather than going nowhere quietly. The old wrapper
    never met this because it redirected stdout into the log file, so a handle
    always existed. The log is the record; the screen is a convenience.
    """
    if log is not None:
        log.write(text + "\n")
        log.flush()                 # a crash should not take the log with it
    try:
        print(text, flush=True)
    except (OSError, ValueError):
        pass

# (script, what to say before it, does its exit code decide the run's verdict)
STEPS = [
    ("run_pipeline.py", "", True),
    # Runs even when the pipeline failed: a rejection that arrived is worth
    # recording either way. Its own failure is a warning in the verdict and
    # must not mask the pipeline's code.
    ("check_replies.py", "Checking for replies from employers...", False),
    # Reads the log written above, so it goes last.
    ("build_digest.py", "", False),
]


def run_step(script: str, log, seen: list) -> int:
    """Run one step, sending its output to the screen and the log together,
    and keeping it in `seen` for the verdict."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen([sys.executable, script], cwd=SRC, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for raw in proc.stdout:
        text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        seen.append(text)
        say(text, log)
    return proc.wait()


# Sources that need a key or a login. A 400/401/403 from one of these is
# almost always the credential, not the query.
KEYED = ("jooble", "adzuna", "company_watchlist", "apify_linkedin",
         "apify_indeed", "email")
SOURCE_FAILED = re.compile(r"^\[warn\] (%s)\b.*? failed: (.*)" % "|".join(KEYED))
AUTH_REFUSED = re.compile(r"returned 40[013]\b|\b40[013] Client Error|"
                          r"AUTHENTICATIONFAILED|Invalid credentials", re.I)
FOUND = re.compile(r"^\s*found (\d+) postings matching")
SCORED = re.compile(r"^\[\d+/10\]")     # column zero; the tracker report indents
SCORING_FAILED = "[skip] scoring failed for"


def verdict(lines: list[str], pipeline_code: int, replies_code: int) -> tuple[str, int]:
    """
    The run's closing line and exit code, judged from this run's output.

    Exit codes alone said "[OK] pipeline completed" on 2026-10-14, when every
    source failed DNS and nothing was found, and again on 2026-10-15, when
    every keyed source was refused, both scoring calls failed and the reply
    check crashed on a bad IMAP login. Each source and each posting is wrapped
    so one failure cannot stop the run, which is right, and it also means the
    run cannot tell from the inside that everything failed. This can.
    """
    if pipeline_code != 0:
        return f"[ERROR] pipeline exited with code {pipeline_code}", pipeline_code

    found = [int(m.group(1)) for m in map(FOUND.match, lines) if m]
    if found and found[-1] == 0:
        return ("[ERROR] no source returned any postings -- every source "
                "failed or found nothing; see the [warn] lines above"), 1

    scored = sum(1 for ln in lines if SCORED.match(ln))
    failed = sum(1 for ln in lines if ln.startswith(SCORING_FAILED))
    if failed and not scored:
        return (f"[ERROR] every scoring call failed ({failed} of {failed}) -- "
                "see the [skip] lines above"), 1

    problems = []
    refused = []
    for ln in lines:
        m = SOURCE_FAILED.match(ln)
        if m and AUTH_REFUSED.search(m.group(2)) and m.group(1) not in refused:
            refused.append(m.group(1))
    if refused:
        problems.append(f"{', '.join(refused)} refused the credentials")
    if replies_code != 0:
        problems.append(f"the reply check failed (exit code {replies_code})")
    if any("[TRACKER LOCKED]" in ln for ln in lines):
        problems.append("the tracker was locked")
    if problems:
        return ("[OK with WARNING] pipeline completed but "
                + "; ".join(problems) + " -- see above"), 0
    return "[OK] pipeline completed", 0


def prune(keep_days: int) -> None:
    cutoff = time.time() - keep_days * 86400
    for old in LOGS.glob("pipeline_*.log"):
        if old.stat().st_mtime < cutoff:
            try:
                old.unlink()
            except OSError:
                pass                # locked or already gone; not worth failing


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--keep", type=int, default=30, metavar="DAYS",
                    help="how many days of logs to keep (default 30)")
    args = ap.parse_args()

    LOGS.mkdir(exist_ok=True)
    log_path = LOGS / f"pipeline_{datetime.now():%Y-%m-%d}.log"

    codes = {}
    seen: list[str] = []        # this run's output only; the log holds the whole day
    with open(log_path, "a", encoding="utf-8") as log:
        bar = "=" * 60
        for text in (bar, f"{datetime.now():%Y-%m-%d %H:%M:%S}", bar):
            say(text, log)

        for script, announce, _decides in STEPS:
            if announce:
                say(f"\n{announce}", log)
            if not (SRC / script).exists():
                say(f"[warn] {script} is missing; skipping that step", log)
                continue
            codes[script] = run_step(script, log, seen)

        # A locked tracker, a refused key or a crashed reply check still exit
        # 0 from the pipeline, so read the output rather than the codes alone.
        closing, verdict_code = verdict(seen, codes.get("run_pipeline.py", 0),
                                        codes.get("check_replies.py", 0))
        say(closing, log)

    prune(args.keep)
    return verdict_code


if __name__ == "__main__":
    sys.exit(main())
