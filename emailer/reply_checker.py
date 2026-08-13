"""
Reply detection — reads the mailbox replies land in and matches them back to
the emails this tool sent.

Why this and not open tracking: a reply is the only engagement signal that is
both accurate and worth something. It can't be faked by an image pre-fetch,
it can't be missed by someone reading with images off, and it's what actually
moves sender reputation — an open moves nothing.

Works regardless of which transport sent the mail. Every outgoing message
sets Reply-To to FROM_EMAIL, so replies land in that one mailbox no matter
whether SES or Workspace SMTP delivered the original.

Two deliberate limits:

- **Headers only.** Messages are fetched with BODY.PEEK so scanning never
  marks anything as read in a mailbox a human also uses, and only headers are
  pulled — enough to identify and match a reply, not enough to show its text.
  Read the actual reply in your inbox.
- **Bounces are counted, not acted on.** Identifying *which address* bounced
  means parsing the delivery-status part of the message body, which this
  doesn't fetch. So a bounce is reported as "there are N of these, go look"
  rather than silently added to the suppression list — auto-suppressing on a
  guess would permanently block a real lead.
"""

import email
import imaplib
import re
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr

import config
from storage import db

IMAP_PORT = 993
# Enough to identify and match a reply; deliberately not the body.
_HEADER_FIELDS = (
    "MESSAGE-ID IN-REPLY-TO REFERENCES FROM SUBJECT DATE "
    "AUTO-SUBMITTED PRECEDENCE X-AUTOREPLY X-AUTORESPOND CONTENT-TYPE RETURN-PATH"
)

_MSGID_RE = re.compile(r"<[^<>@\s]+@[^<>\s]+>")

_AUTO_SUBJECT_PREFIXES = (
    "auto:", "automatic reply", "autoreply", "out of office", "out-of-office",
    "away from", "on vacation", "on leave", "vacation reply",
)

_BOUNCE_SENDERS = ("mailer-daemon", "postmaster", "no-reply@", "noreply@")
_BOUNCE_SUBJECT_MARKERS = (
    "undeliverable", "delivery status notification", "returned mail",
    "failure notice", "delivery has failed", "mail delivery failed",
    "undelivered mail returned",
)


def _extract_message_ids(value: str) -> list[str]:
    """Pull every RFC message-id out of an In-Reply-To or References header."""
    return _MSGID_RE.findall(value or "")


def is_auto_reply(headers: dict) -> bool:
    """
    True for out-of-office and other machine-generated replies.

    These must not count as engagement: an autoresponder proves the address is
    live, not that a human read anything, and treating one as a reply would
    inflate the only metric here that's supposed to be trustworthy.
    """
    auto_submitted = (headers.get("auto-submitted") or "").strip().lower()
    if auto_submitted and auto_submitted != "no":
        return True

    if headers.get("x-autoreply") or headers.get("x-autorespond"):
        return True

    precedence = (headers.get("precedence") or "").strip().lower()
    if precedence in ("bulk", "auto_reply", "junk", "list"):
        return True

    subject = (headers.get("subject") or "").strip().lower()
    # Strip a leading Re: so "Re: Automatic reply: ..." still matches.
    if subject.startswith("re:"):
        subject = subject[3:].strip()
    return subject.startswith(_AUTO_SUBJECT_PREFIXES)


def is_bounce(headers: dict) -> bool:
    """True for delivery-failure notifications."""
    sender = (headers.get("from") or "").lower()
    if any(marker in sender for marker in _BOUNCE_SENDERS):
        return True

    # An empty Return-Path is the RFC-mandated envelope sender for a DSN,
    # which is exactly what stops bounce notifications bouncing themselves.
    if (headers.get("return-path") or "").strip() in ("<>", ""):
        if "report" in (headers.get("content-type") or "").lower():
            return True

    subject = (headers.get("subject") or "").lower()
    return any(marker in subject for marker in _BOUNCE_SUBJECT_MARKERS)


def _headers_from_raw(raw: bytes) -> dict:
    """Flatten a raw header block into a lowercase-keyed dict."""
    parsed = email.message_from_bytes(raw)
    return {key.lower(): str(value) for key, value in parsed.items()}


def _match_to_sent_email(headers: dict, sent_by_msgid: dict, sent_by_address: dict) -> dict | None:
    """
    Find which sent email a message is replying to.

    Threading headers first — that's an exact match and survives the
    recipient replying from a different address than the one we mailed. The
    sender-address fallback exists because plenty of mail clients (and most
    forwarded/rewritten replies) drop In-Reply-To entirely, and losing a real
    reply is worse than the small chance of crediting the wrong one.
    """
    candidates = _extract_message_ids(headers.get("in-reply-to", "")) + \
        _extract_message_ids(headers.get("references", ""))
    for candidate in candidates:
        if candidate in sent_by_msgid:
            return sent_by_msgid[candidate]

    sender = parseaddr(headers.get("from", ""))[1].lower()
    return sent_by_address.get(sender)


def _mark_replied_in_sheet(target_email: str) -> None:
    """
    Best-effort: find this lead's row and set Status to "replied" so
    run_followups stops sending it further follow-ups.

    A module-level function (not inlined in check_replies) so tests can
    monkeypatch it wholesale without needing real Sheets credentials — the
    real SheetsStorage() is only constructed here, at call time, never at
    import time. A Sheets API failure must not blow up the whole reply scan
    over one row it couldn't update; the reply itself is still logged to
    the local DB by the caller regardless of whether this succeeds.
    """
    if not target_email:
        return
    try:
        from storage.sheets import SheetsStorage

        sheets = SheetsStorage()
        row = sheets.find_row_by_email(target_email)
        if row:
            sheets.mark_replied(row)
        else:
            print(f"[Replies] {target_email} replied but has no row in the sheet — nothing to mark.")
    except Exception as e:
        print(f"[Replies] Could not mark {target_email} as replied in the sheet: {e}")


def check_replies(lookback_days: int | None = None) -> dict:
    """
    Scan the reply mailbox and record anything matching a sent email.

    Returns a summary dict; safe to call repeatedly, since replies are stored
    keyed on their own Message-ID and re-recording one is a no-op.
    """
    host = config.IMAP_HOST
    user = config.IMAP_USER
    password = config.IMAP_PASSWORD

    if not (host and user and password):
        raise Exception(
            "Reply checking is not configured: set IMAP_HOST, IMAP_USER and "
            "IMAP_PASSWORD (the password must be an App Password, not the "
            "account password)."
        )

    days = lookback_days if lookback_days is not None else config.REPLY_LOOKBACK_DAYS
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")

    sent = db.get_email_history()
    sent_by_msgid = {row["message_id"]: row for row in sent if row.get("message_id")}
    sent_by_address = {}
    for row in sent:
        address = (row.get("target_email") or "").strip().lower()
        # Newest-first ordering means the first row per address is the most
        # recent send, which is the one a reply almost certainly answers.
        if address and address not in sent_by_address:
            sent_by_address[address] = row

    summary = {"scanned": 0, "replies": 0, "auto_replies": 0, "bounces": 0, "unmatched": 0}

    connection = imaplib.IMAP4_SSL(host, IMAP_PORT)
    try:
        connection.login(user, password)
        connection.select("INBOX", readonly=True)

        status, data = connection.search(None, f'(SINCE "{since}")')
        if status != "OK":
            raise Exception(f"IMAP search failed: {status}")

        message_numbers = data[0].split()
        summary["scanned"] = len(message_numbers)

        for number in message_numbers:
            try:
                status, fetched = connection.fetch(number, f"(BODY.PEEK[HEADER.FIELDS ({_HEADER_FIELDS})])")
                if status != "OK" or not fetched or not isinstance(fetched[0], tuple):
                    continue

                headers = _headers_from_raw(fetched[0][1])
                reply_message_id = (headers.get("message-id") or "").strip()
                sender = parseaddr(headers.get("from", ""))[1].lower()

                bounce = is_bounce(headers)
                auto = False if bounce else is_auto_reply(headers)

                matched = _match_to_sent_email(headers, sent_by_msgid, sent_by_address)
                if not matched:
                    summary["unmatched"] += 1
                    continue

                if bounce:
                    summary["bounces"] += 1
                elif auto:
                    summary["auto_replies"] += 1
                else:
                    summary["replies"] += 1
                    # A genuine reply, not a bounce or an auto-responder, is
                    # a decision — mark it in the Sheet, not just this local
                    # table, because run_followups() (main.py) reads its
                    # skip list from the Sheet's own Status column and has
                    # no visibility into email_replies at all. Without this,
                    # nothing stopped a follow-up going out to someone who
                    # already said yes or no.
                    _mark_replied_in_sheet(matched.get("target_email") or sender)

                db.log_reply(
                    reply_message_id=reply_message_id,
                    in_reply_to=matched.get("message_id") or "",
                    from_email=sender,
                    target_email=matched.get("target_email") or "",
                    subject=(headers.get("subject") or "")[:300],
                    is_auto=auto,
                    is_bounce=bounce,
                )
            except Exception as e:
                print(f"[Replies] Skipped a message that could not be parsed: {e}")

    finally:
        try:
            connection.logout()
        except Exception:
            pass

    print(
        f"[Replies] Scanned {summary['scanned']} messages since {since}: "
        f"{summary['replies']} real replies, {summary['auto_replies']} auto-replies, "
        f"{summary['bounces']} bounces, {summary['unmatched']} unrelated."
    )
    return summary
