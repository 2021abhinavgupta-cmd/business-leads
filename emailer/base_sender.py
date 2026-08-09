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
        variant = config.EMAIL_VARIANT

        # "Hi Acme Dental Care Team," is a visible mail-merge tell, and the
        # generic name is the COMMON case (contact discovery usually can't
        # find a real person). A bare "Hi there," reads as a human writing
        # quickly; a company name in the salutation reads as a list.
        greeting = f"Hi {contact_name}," if self._is_a_real_person_name(contact_name, company) else "Hi there,"

        body_lines = [f"{greeting}\n", f"{opening_line}\n"]

        # The "short" variant sends ONE flaw instead of all 3-4. The AI is
        # asked for the most severe first, so [0] is the strongest card.
        # Rationale: the full version runs ~220-260 words and reads as four
        # consecutive criticisms of a stranger's business, where cold email
        # that gets replies is typically 50-125. This is a hypothesis, not a
        # fact — which is exactly why it ships as a measurable variant rather
        # than a silent rewrite. See db.get_variant_performance().
        shown_flaws = flaws[:1] if variant == "short" else flaws

        for flaw in shown_flaws:
            if "paragraph" in flaw:
                body_lines.append(f"{flaw.get('paragraph', '')}\n")
            else:
                # Fallback for old AI responses
                body_lines.append(f"{flaw.get('headline', '')}")
                body_lines.append(
                    f"{flaw.get('detail', '')} This means {flaw.get('impact', '')}.\n"
                )

        body_lines.extend(self._closing_lines(variant, your_name))

        return subject, "\n".join(body_lines)

    # Salutation fallbacks produced by enrichment/decision_maker.py when no
    # real person could be found — "{Company} Team" or a bare "Team".
    @staticmethod
    def _is_a_real_person_name(contact_name: str, company: str) -> bool:
        name = (contact_name or "").strip()
        if not name:
            return False
        if name.lower() in {"team", "there"}:
            return False
        # "{Company} Team" — the generic personalised fallback.
        if company and name.lower() == f"{company.strip().lower()} team":
            return False
        return not name.lower().endswith(" team")

    def _closing_lines(self, variant: str, your_name: str) -> list[str]:
        """
        The ask.

        The original was one hardcoded pair of lines on every email:
        "I've been helping brands fix exactly these things." (an unevidenced
        claim of competence from a stranger) followed by a request for a
        10-minute call (a high-commitment ask on first contact). Both are
        kept as the "classic" variant so the change is measurable rather than
        assumed — see db.get_variant_performance().
        """
        if variant == "short":
            # Lower-friction ask: something they receive, not something they
            # have to schedule and show up to.
            return [
                "Want me to send over the short list of what I'd fix first? No call needed.\n",
                f"{your_name}",
            ]

        proof = config.SOCIAL_PROOF_LINE.strip()
        return [
            proof or "I've been helping brands fix exactly these things.",
            "Worth a quick 10 minute call this week?\n",
            f"{your_name}",
        ]

    def generate_followup(self, contact_name: str, stage: int, your_name: str) -> str:
        """
        Generate a short, punchy follow-up email.
        stage 1 = 3 days later, stage 2 = 6 days later.

        Deliberately says nothing specific about WHAT was found. This copy is
        hardcoded and has no access to the original audit — it receives only a
        name and a stage number — so any concrete claim here is a guess that
        will be wrong on a predictable share of sends, to people who have
        already ignored one email.

        Two such claims were live until 2026-08-09 and both were routinely
        false: stage 1 asked whether they'd seen "the mobile website
        screenshot I attached" when the attachment is always the DESKTOP
        screenshot (`_audit.jpg`; the mobile capture is only ever fed to the
        AI, never attached), and stage 2 referred to "your mobile site" and
        "these UI issues" when the original email's flaws are just as often
        performance, SEO, security, certificate expiry, broken links or NAP
        mismatches. If follow-ups ever need to reference the real findings,
        pass the original flaws in rather than reinstating a guess here.
        """
        if stage == 1:
            body_lines = [
                f"Hi {contact_name},\n",
                "Just bumping this up in case it got buried. Did you get a chance to look at the screenshot and notes I sent?",
                "Happy to walk through what I'd fix first, if it's useful.\n",
                f"Best,\n{your_name}",
            ]
        else:
            body_lines = [
                f"Hi {contact_name},\n",
                "Last one from me, promise. If any of what I sent is worth acting on this quarter, I'm glad to show you how I'd approach it.",
                "Either way, wishing you a great week ahead.\n",
                f"Cheers,\n{your_name}",
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
