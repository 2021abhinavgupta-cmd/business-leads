"""
One-off reputation warm-up sender — sends a real cold-email-style message
through the live SES pipeline to a list of coworker addresses read from
warmup_recipients.txt (gitignored — real personal email addresses shouldn't
live in git history). One address per line, run manually as needed.
"""

import os
import time
import random

from emailer.ses_sender import SESSender

_RECIPIENTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "warmup_recipients.txt")


def _load_recipients() -> list[str]:
    if not os.path.exists(_RECIPIENTS_FILE):
        raise FileNotFoundError(
            f"{_RECIPIENTS_FILE} not found — create it with one email address per line."
        )
    with open(_RECIPIENTS_FILE, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]

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
    rng = random.Random(seed)
    subject = rng.choice(SUBJECTS)
    opener = rng.choice(OPENERS)
    score_line = rng.choice(SCORE_LINES).format(score=rng.randint(28, 54))
    closer = rng.choice(CLOSERS)
    signoff = rng.choice(SIGNOFFS)
    body = f"Hi,\n\n{opener}\n\n{score_line}\n\n{closer}\n\n{signoff}"
    return subject, body


def main():
    recipients = _load_recipients()
    ses = SESSender()
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
    main()
