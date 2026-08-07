"""
Shared sending logic for every email transport.

Everything that decides how a message *looks* to a spam filter lives here —
the multipart text+HTML structure, the List-Unsubscribe headers, the
suppression-list check, the RFC threading headers on follow-ups. Only the
final "hand these bytes to a mail server" step is left to a subclass.

That split is deliberate: the deliverability work in this file was tuned over
several rounds against real Gmail placement (see CLAUDE.md §8), and a second
transport that rebuilt its own MIME would inevitably drift away from it — one
copy would get a fix and the other wouldn't.
"""

import os
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.utils import make_msgid, formatdate
from urllib.parse import quote

import config
from storage import db
from emailer.tracking import pixel_html, tracking_id_for


class TransientSendError(Exception):
    """
    A failure worth one retry — throttling, a dropped connection, a 4xx SMTP
    response. Distinct from a permanent rejection, which should surface
    immediately rather than being retried into a delay.
    """


class BaseSender:
    """Transport-agnostic cold-email sender."""

    # One retry on a transient failure, then give up and raise.
    _RETRIES = 1
    _RETRY_DELAY_SECONDS = 5

    # Human-readable name of the transport, used in log lines.
    provider_name = "base"

    def __init__(self, from_email: str | None = None):
        self.from_email = from_email or config.FROM_EMAIL
        # Falls back to the sending address, so an environment that has never
        # heard of REPLY_TO_EMAIL behaves exactly as before.
        self.reply_to = config.REPLY_TO_EMAIL or self.from_email

    # --- transport hook -------------------------------------------------

    def _transport_send(self, msg: MIMEMultipart, to_email: str) -> None:
        """
        Hand a fully-built MIME message to the mail server.

        Raises:
            TransientSendError: if retrying might succeed.
            Exception: on a permanent rejection, with the server's real
            error text preserved (a bare False gives the caller no way to
            tell "suppressed" apart from "the server rejected this").
        """
        raise NotImplementedError

    def check_quota(self) -> dict:
        """
        Report remaining daily sending capacity.

        Returns:
            A dict with 'Max24HourSend', 'SentLast24Hours' and 'Remaining'.
        """
        raise NotImplementedError

    # --- shared helpers -------------------------------------------------

    def _msgid_domain(self) -> str:
        return (self.from_email or "").split("@")[-1] or "localhost"

    def _unsubscribe_headers(self, to_email: str) -> dict:
        """
        Build List-Unsubscribe (+ List-Unsubscribe-Post) headers per RFC 2369 /
        RFC 8058. Gmail/Yahoo bulk-sender rules require this header; without
        it, mail is far more likely to land in spam regardless of content.
        Always includes a mailto: fallback; adds a one-click HTTPS link (and
        the List-Unsubscribe-Post flag that unlocks Gmail's one-click button)
        only if APP_BASE_URL is configured, since that URL must be a live,
        unauthenticated endpoint (see app.py's /unsubscribe route).
        """
        # The mailto goes to the reply address, not the sending one — an
        # unsubscribe request nobody reads is the same as no unsubscribe
        # mechanism, and honouring opt-outs is a compliance obligation.
        targets = [f"<mailto:{self.reply_to}?subject=Unsubscribe>"]
        headers = {}
        if config.APP_BASE_URL:
            url = f"{config.APP_BASE_URL}/unsubscribe?email={quote(to_email, safe='')}"
            targets.append(f"<{url}>")
            headers["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        headers["List-Unsubscribe"] = ", ".join(targets)
        return headers

    # --- copy generation ------------------------------------------------

    def generate_email(
        self, company: str, contact_name: str, analysis: dict, your_name: str
    ) -> tuple[str, str]:
        """
        Generate the subject and body for the cold email.

        Args:
            company: The target company's name.
            contact_name: The first name of the decision maker.
            analysis: The AI audit dict containing flaws and email copy.
            your_name: The sender's name to sign off with.

        Returns:
            (subject, body) as plain text strings.
        """
        subject = analysis.get("email_subject", f"Quick question about {company}")
        opening_line = analysis.get(
            "opening_line", "Came across your brand and wanted to reach out."
        )

        flaws = analysis.get("flaws", [])

        body_lines = [
            f"Hi {contact_name},\n",
            f"{opening_line}\n",
        ]

        if flaws:
            for flaw in flaws:
                if "paragraph" in flaw:
                    body_lines.append(f"{flaw.get('paragraph', '')}\n")
                else:
                    # Fallback for old AI responses
                    body_lines.append(f"{flaw.get('headline', '')}")
                    body_lines.append(
                        f"{flaw.get('detail', '')} This means {flaw.get('impact', '')}.\n"
                    )

        body_lines.extend([
            "I've been helping brands fix exactly these things.",
            "Worth a quick 10 minute call this week?\n",
            f"{your_name}",
        ])

        return subject, "\n".join(body_lines)

    def generate_followup(self, contact_name: str, stage: int, your_name: str) -> str:
        """
        Generate a short, punchy follow-up email.
        stage 1 = 3 days later, stage 2 = 6 days later.
        """
        if stage == 1:
            body_lines = [
                f"Hi {contact_name},\n",
                "Just bumping this up to the top of your inbox. Did you get a chance to see the mobile website screenshot I attached?",
                "I know you're busy, but this is causing a direct loss in conversions.\n",
                "Let me know if you have 10 minutes this week.",
                f"\nBest,\n{your_name}",
            ]
        else:
            body_lines = [
                f"Hi {contact_name},\n",
                "I'll stop bugging you after this! Just wanted to follow up one last time regarding your mobile site.",
                "If fixing these UI issues is a priority for this quarter, I'd love to show you how we'd tackle it.",
                "Either way, wishing you a great week ahead.\n",
                f"\nCheers,\n{your_name}",
            ]

        return "\n".join(body_lines)

    # --- message construction -------------------------------------------

    def _build_initial_message(
        self, to_email: str, subject: str, body: str, message_id: str, image_path: str = None
    ) -> MIMEMultipart:
        """
        Build the first-touch message as multipart/mixed raw MIME carrying
        both a text/plain and a text/html part (spam filters weight a missing
        text/plain alternative heavily) plus List-Unsubscribe headers. If
        image_path is given, the screenshot is embedded inline as a related
        part rather than attached as a file.
        """
        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"] = self.from_email
        msg["Reply-To"] = self.reply_to
        msg["To"] = to_email
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = message_id
        for header, value in self._unsubscribe_headers(to_email).items():
            msg[header] = value

        html_body = body.replace("\n", "<br>")
        # Only the HTML alternative carries the pixel — a text/plain part
        # can't load an image, and putting a bare URL there would just show
        # the recipient a tracking link.
        pixel = pixel_html(tracking_id_for(message_id))

        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(body, "plain", "utf-8"))

        if image_path and os.path.exists(image_path):
            html_with_img = f"""
                    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; max-width: 600px; margin: 0 auto; color: #1a1a1a; line-height: 1.6;">
                        <div style="padding: 20px;">
                            {html_body}
                        </div>
                        <div style="background-color: #f8fafc; padding: 24px; border-radius: 12px; margin: 20px 0; border: 1px solid #e2e8f0;">
                            <h3 style="margin-top: 0; color: #0f172a; font-size: 16px;">Visual Audit Evidence</h3>
                            <p style="color: #475569; font-size: 14px; margin-bottom: 16px;">Here is the screenshot my team took of your website on mobile:</p>
                            <img src='cid:audit_img' alt='Website Audit' style='max-width: 100%; height: auto; border-radius: 8px; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06); display: block; margin: 0 auto;'>
                        </div>
                        {pixel}
                    </div>
                    """
            alt.attach(MIMEText(html_with_img, "html", "utf-8"))

            related = MIMEMultipart("related")
            related.attach(alt)

            with open(image_path, "rb") as f:
                img_data = f.read()

            img = MIMEImage(img_data)
            img.add_header("Content-ID", "<audit_img>")
            img.add_header("Content-Disposition", "inline")
            related.attach(img)

            msg.attach(related)
        else:
            html_plain = f"""
                    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; max-width: 600px; margin: 0 auto; color: #1a1a1a; line-height: 1.6;">
                        {html_body}
                        {pixel}
                    </div>
                    """
            alt.attach(MIMEText(html_plain, "html", "utf-8"))
            msg.attach(alt)

        return msg

    def _build_followup_message(
        self, to_email: str, subject: str, body: str, in_reply_to: str = ""
    ) -> MIMEMultipart:
        message_id = make_msgid(domain=self._msgid_domain())
        html_body = body.replace("\n", "<br>")
        pixel = pixel_html(tracking_id_for(message_id))
        html_template = f"""
                <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; max-width: 600px; color: #1a1a1a; line-height: 1.6;">
                    {html_body}
                    {pixel}
                </div>
                """

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.from_email
        msg["Reply-To"] = self.reply_to
        msg["To"] = to_email
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = message_id
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        for header, value in self._unsubscribe_headers(to_email).items():
            msg[header] = value

        msg.attach(MIMEText(body, "plain", "utf-8"))
        msg.attach(MIMEText(html_template, "html", "utf-8"))
        return msg

    # --- sending --------------------------------------------------------

    def send_email(self, to_email: str, subject: str, body: str, image_path: str = None):
        """
        Send the first-touch cold email.

        Returns:
            The RFC Message-ID (str, truthy) if the transport accepted the
            email, False if the recipient is on the suppression list.

        Raises:
            Exception: if the transport rejected or failed the send, with the
            server's real error message preserved.
        """
        if db.is_suppressed(to_email):
            print(f"Skipping {to_email}: on the unsubscribe/suppression list")
            return False

        for attempt in range(self._RETRIES + 1):
            message_id = make_msgid(domain=self._msgid_domain())
            msg = self._build_initial_message(
                to_email, subject, body, message_id, image_path=image_path
            )
            try:
                self._transport_send(msg, to_email)
                return message_id
            except TransientSendError as e:
                if attempt < self._RETRIES:
                    time.sleep(self._RETRY_DELAY_SECONDS)
                    continue
                print(f"[{self.provider_name}] Transient failure persisted after retry: {e}")
                raise Exception(str(e)) from e
            except Exception as e:
                print(f"[{self.provider_name}] Error sending email: {e}")
                raise

    def send_followup(
        self, to_email: str, original_subject: str, body: str, in_reply_to: str = ""
    ) -> bool:
        """
        Send a follow-up, threaded to the original via real In-Reply-To /
        References headers (not just a matching "Re:" subject — that alone
        doesn't make Gmail/Outlook group it as one thread; it just makes an
        unrelated new message look like a spoofed reply, which reads worse to
        spam filters than an honest new email).

        Unlike send_email this swallows every failure to False, matching the
        original SES behaviour — its only caller is main.py's batch runner,
        which has no error path to surface a raise into.
        """
        if db.is_suppressed(to_email):
            print(f"Skipping follow-up to {to_email}: on the unsubscribe/suppression list")
            return False

        subject = original_subject
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"

        for attempt in range(self._RETRIES + 1):
            try:
                msg = self._build_followup_message(to_email, subject, body, in_reply_to)
                self._transport_send(msg, to_email)
                return True
            except TransientSendError as e:
                if attempt < self._RETRIES:
                    time.sleep(self._RETRY_DELAY_SECONDS)
                    continue
                print(f"[{self.provider_name}] Error sending follow-up: {e}")
                return False
            except Exception as e:
                print(f"[{self.provider_name}] Unexpected error in follow-up: {e}")
                return False
