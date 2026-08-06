"""
Gmail Sender — delivers cold emails through a Google Workspace mailbox over
authenticated SMTP submission.

Why this exists alongside SES: Gmail weighs mail originating from Google's
own infrastructure differently from third-party ESP traffic, and this tool's
leads are overwhelmingly Gmail/Workspace inboxes. SES is the better transport
for volume and for anything transactional; Workspace is the better transport
for a small number of genuinely 1:1 cold emails to Gmail recipients.

Two constraints worth knowing before switching to this:

1. Google's Gmail Program Policies prohibit bulk unsolicited mail, and
   accounts that send it can be suspended. Keep the daily volume low
   (GMAIL_DAILY_CAP) and use a throwaway domain, never the domain that
   carries real client correspondence.

2. Gmail's submission server may replace the Message-ID header we set with
   one of its own. If it does, the ID returned by send_email() — which
   storage/db.py persists and send_followup() threads against — won't match
   what the recipient actually received, and follow-ups will arrive as new
   messages instead of joining the thread. This is unverified against a live
   Workspace mailbox; check a received message's raw headers against the
   logged ID before relying on follow-up threading. The fix, if it turns out
   to be needed, is to send via the Gmail API instead, whose response returns
   the real message and thread IDs.
"""

import smtplib
import socket

import config
from storage import db
from emailer.base_sender import BaseSender, TransientSendError

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_TIMEOUT_SECONDS = 30


class GmailSender(BaseSender):
    """Send cold emails through a Google Workspace mailbox via SMTP."""

    provider_name = "Gmail"

    def __init__(self):
        # Gmail rewrites the From header to the authenticated mailbox unless
        # the address is a verified "Send mail as" alias, so defaulting the
        # sender to GMAIL_USER keeps what we build and what the recipient
        # sees identical.
        super().__init__(from_email=config.GMAIL_USER or config.FROM_EMAIL)
        self.user = config.GMAIL_USER
        self.password = config.GMAIL_APP_PASSWORD

        if not self.user or not self.password:
            print(
                "[Gmail] WARNING: GMAIL_USER / GMAIL_APP_PASSWORD are not both set — "
                "every send will fail. Create an App Password at "
                "https://myaccount.google.com/apppasswords (requires 2-Step Verification)."
            )

    def _transport_send(self, msg, to_email: str) -> None:
        if not self.user or not self.password:
            raise Exception(
                "Gmail transport is not configured: set GMAIL_USER and GMAIL_APP_PASSWORD"
            )

        # A fresh connection per send. Gmail drops idle SMTP sessions, and
        # sends here are minutes apart by design, so a pooled connection
        # would spend most of its life stale for no measurable gain.
        try:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT_SECONDS) as server:
                server.starttls()
                server.login(self.user, self.password)
                server.send_message(msg, from_addr=self.from_email, to_addrs=[to_email])

        except smtplib.SMTPAuthenticationError as e:
            # Retrying a bad credential just delays the same failure.
            raise Exception(
                f"Gmail rejected the login: {e}. Check GMAIL_APP_PASSWORD is a current "
                "App Password (not the account password) and that 2-Step Verification is on."
            ) from e

        except smtplib.SMTPSenderRefused as e:
            raise Exception(
                f"Gmail refused the sender address {self.from_email}: {e}. It must be the "
                "authenticated mailbox or a verified 'Send mail as' alias."
            ) from e

        except smtplib.SMTPRecipientsRefused as e:
            raise Exception(f"Gmail refused the recipient {to_email}: {e}") from e

        except smtplib.SMTPResponseException as e:
            # 4xx is a soft failure worth one retry; 5xx is permanent.
            if 400 <= e.smtp_code < 500:
                raise TransientSendError(f"Gmail temporary error {e.smtp_code}: {e.smtp_error}") from e
            raise Exception(f"Gmail error {e.smtp_code}: {e.smtp_error}") from e

        except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, socket.timeout, OSError) as e:
            raise TransientSendError(f"Gmail connection problem: {e}") from e

    def check_quota(self) -> dict:
        """
        Report remaining daily capacity.

        Gmail exposes no quota API, so this is our own configured cap
        (GMAIL_DAILY_CAP) measured against the rolling 24h send count already
        tracked in SQLite. That cap should stay far below Google's own limit
        — the risk being managed here is account suspension for bulk sending,
        not hitting a hard ceiling.
        """
        cap = float(config.GMAIL_DAILY_CAP)
        try:
            sent = float(db.count_emails_sent_today())
        except Exception as e:
            print(f"[Gmail] Could not read today's send count: {e}")
            sent = 0.0

        return {
            "Max24HourSend": cap,
            "SentLast24Hours": sent,
            "Remaining": max(0.0, cap - sent),
        }
