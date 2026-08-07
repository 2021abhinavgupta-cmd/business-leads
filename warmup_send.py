"""
Reputation warm-up sender — sends a real cold-email-style message through
the live email pipeline to a list of coworker addresses read from
warmup_recipients.txt (gitignored — real personal email addresses shouldn't
live in git history). One address per line.

Runnable two ways: manually (`python -u warmup_send.py`, unbuffered output —
see CLAUDE.md §13 for why -u matters here) or automatically once daily via
scheduler.py when config.WARMUP_ENABLED is set (see that file's job
registration). The daily-firing local Task Scheduler entry this used to rely
on only ran while the machine was on and awake, which is why this exists as
an importable function rather than only a __main__ script.
"""

import os
import time
import random
import datetime

from emailer import get_sender

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Checked in this order: the Railway persistent volume first (survives
# redeploys — same mount CLAUDE.md documents for database.sqlite/screenshots/,
# see §12), falling back to the repo-root copy used for local dev. Neither
# path is ever committed (both gitignored) since these are real personal
# addresses, not test fixtures.
_RECIPIENTS_CANDIDATES = [
    os.path.join(_BASE_DIR, "data", "warmup_recipients.txt"),
    os.path.join(_BASE_DIR, "warmup_recipients.txt"),
]


def _load_recipients() -> list[str]:
    for path in _RECIPIENTS_CANDIDATES:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return [line.strip() for line in f if line.strip()]
    raise FileNotFoundError(
        "No warmup_recipients.txt found — checked "
        + " and ".join(_RECIPIENTS_CANDIDATES)
        + ". Create it with one email address per line (on Railway, place it "
        "in the persistent /app/data volume so it survives redeploys)."
    )

SUBJECTS = [
    "Quick question about your website",
    "Noticed something on your site",
    "Your mobile site speed",
    "A quick audit of your site",
    "Question about your homepage",
]

OPENERS = [
    "Came across your site recently and noticed a few things that might be costing you conversions on mobile.",
    "Was checking out a few sites in your space and yours caught my eye — a couple of quick issues stood out.",
    "Ran a quick technical pass on your homepage and found a couple of things worth flagging.",
    "Took a look at your site on my phone earlier and a few things jumped out.",
]

SCORE_LINES = [
    "Our team ran a quick audit and found your mobile page speed score was {score} out of 100, which is well below what most visitors will tolerate before leaving.",
    "Your site's mobile load score came back at {score}/100 in our scan — most visitors bounce well before that.",
    "A quick scan put your mobile performance at {score}/100, on the lower end for your category.",
]

CLOSERS = [
    "We've been helping brands fix exactly these kinds of issues.\n\nWorth a quick 10 minute call this week?",
    "Happy to walk you through what we found, no pressure either way — 10 minutes this week work?",
    "We fix this exact kind of thing for a living. Open to a short call?",
]

SIGNOFFS = ["Kshitij", "Kshitij Gupta", "Best,\nKshitij"]


def _build_email(seed: int) -> tuple[str, str]:
    # Seed includes today's date so a same-day re-run (or the next scheduled
    # day) doesn't resend byte-identical content to the same address —
    # identical content twice looks bursty/spammy in its own right.
    today = datetime.date.today().isoformat()
    rng = random.Random(f"{today}-{seed}")
    subject = rng.choice(SUBJECTS)
    opener = rng.choice(OPENERS)
    score_line = rng.choice(SCORE_LINES).format(score=rng.randint(28, 54))
    closer = rng.choice(CLOSERS)
    signoff = rng.choice(SIGNOFFS)
    body = f"Hi,\n\n{opener}\n\n{score_line}\n\n{closer}\n\n{signoff}"
    return subject, body


def run_warmup():
    """
    Send one warm-up email to every address in the recipients file, with a
    randomized 20-60s delay between sends (a burst of back-to-back mail
    reads as automated to Gmail regardless of volume — same reasoning as
    main.py's inter-send delay). A missing recipients file is allowed to
    raise here rather than being caught — scheduler.py's caller decides
    whether that should crash the whole scheduler or just skip today's run.
    """
    recipients = _load_recipients()
    ses = get_sender()
    for i, email in enumerate(recipients):
        subject, body = _build_email(seed=i)
        try:
            message_id = ses.send_email(email, subject, body)
            print(f"[{i+1}/{len(recipients)}] Sent to {email} ({subject!r}): {message_id}")
        except Exception as e:
            print(f"[{i+1}/{len(recipients)}] FAILED {email}: {e}")

        if i < len(recipients) - 1:
            delay = random.uniform(20, 60)
            print(f"  waiting {delay:.0f}s...")
            time.sleep(delay)


if __name__ == "__main__":
    run_warmup()
