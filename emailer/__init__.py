"""Emailer package — sends audit reports via AWS SES or a Google Workspace mailbox."""

import config


def get_sender():
    """
    Build the sender for the configured transport (config.EMAIL_PROVIDER).

    Both senders expose the same interface, so callers never need to know
    which one they got. Defaults to SES, so an environment that has never
    heard of EMAIL_PROVIDER keeps its existing behaviour.
    """
    provider = (config.EMAIL_PROVIDER or "ses").strip().lower()

    if provider == "gmail":
        from emailer.gmail_sender import GmailSender
        return GmailSender()

    if provider != "ses":
        print(f"[Emailer] Unknown EMAIL_PROVIDER '{provider}' — falling back to SES.")

    from emailer.ses_sender import SESSender
    return SESSender()
