"""
SES Sender — delivers plain-text cold emails through Amazon SES.
"""

import time
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.application import MIMEApplication
from email.utils import make_msgid, formatdate
from urllib.parse import quote

import boto3
from botocore.exceptions import ClientError

import config
from storage import db


class SESSender:
    """Send plain-text cold emails via AWS Simple Email Service."""

    def __init__(self):
        self.client = boto3.client(
            "ses",
            region_name=config.AWS_REGION,
            aws_access_key_id=config.AWS_ACCESS_KEY,
            aws_secret_access_key=config.AWS_SECRET_KEY,
        )
        self.from_email = config.FROM_EMAIL

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
        targets = [f"<mailto:{self.from_email}?subject=Unsubscribe>"]
        headers = {}
        if config.APP_BASE_URL:
            url = f"{config.APP_BASE_URL}/unsubscribe?email={quote(to_email, safe='')}"
            targets.append(f"<{url}>")
            headers["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        headers["List-Unsubscribe"] = ", ".join(targets)
        return headers

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
        opening_line = analysis.get("opening_line", "Came across your brand and wanted to reach out.")
        
        flaws = analysis.get("flaws", [])
        
        body_lines = [
            f"Hi {contact_name},\n",
            f"{opening_line}\n"
        ]
        
        if flaws:
            for flaw in flaws:
                if 'paragraph' in flaw:
                    body_lines.append(f"{flaw.get('paragraph', '')}\n")
                else:
                    # Fallback for old AI responses
                    body_lines.append(f"{flaw.get('headline', '')}")
                    body_lines.append(f"{flaw.get('detail', '')} This means {flaw.get('impact', '')}.\n")
            
        body_lines.extend([
            "I've been helping brands fix exactly these things.",
            "Worth a quick 10 minute call this week?\n",
            f"{your_name}"
        ])
        
        body = "\n".join(body_lines)
        body = "\n".join(body_lines)
        return subject, body

    def send_email(self, to_email: str, subject: str, body: str, image_path: str = None):
        """
        Send an email using AWS SES, always as a multipart/mixed raw MIME
        message so it carries both a plain-text part (spam filters weight a
        missing text/plain alternative heavily) and List-Unsubscribe headers
        (required by Gmail/Yahoo bulk-sender rules). If image_path is given,
        the screenshot is embedded inline as a related part.

        Args:
            to_email: The recipient's email address.
            subject: The email subject.
            body: The plain-text email body (also used to derive the HTML part).
            image_path: Optional path to an image to embed.

        Returns:
            The RFC Message-ID (str, truthy) if SES accepted the email,
            False otherwise (send failed or the recipient is suppressed).
        """
        if db.is_suppressed(to_email):
            print(f"Skipping {to_email}: on the unsubscribe/suppression list")
            return False

        retries = 1
        unsub_headers = self._unsubscribe_headers(to_email)

        for attempt in range(retries + 1):
            try:
                message_id = make_msgid(domain=self._msgid_domain())
                msg = MIMEMultipart('mixed')
                msg['Subject'] = subject
                msg['From'] = self.from_email
                msg['To'] = to_email
                msg['Date'] = formatdate(localtime=True)
                msg['Message-ID'] = message_id
                for header, value in unsub_headers.items():
                    msg[header] = value

                # Convert plain text body to HTML for the email layout
                html_body = body.replace('\n', '<br>')

                alt = MIMEMultipart('alternative')
                alt.attach(MIMEText(body, 'plain', 'utf-8'))

                if image_path and os.path.exists(image_path):
                    # Professional HTML Layout for the Email
                    html_with_img = f"""
                    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; max-width: 600px; margin: 0 auto; color: #1a1a1a; line-height: 1.6;">
                        <div style="padding: 20px;">
                            {html_body}
                        </div>
                        <div style="background-color: #f8fafc; padding: 24px; border-radius: 12px; margin: 20px 0; border: 1px solid #e2e8f0;">
                            <h3 style="margin-top: 0; color: #0f172a; font-size: 16px;">📸 Visual Audit Evidence</h3>
                            <p style="color: #475569; font-size: 14px; margin-bottom: 16px;">Here is the screenshot my team took of your website on mobile:</p>
                            <img src='cid:audit_img' alt='Website Audit' style='max-width: 100%; height: auto; border-radius: 8px; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06); display: block; margin: 0 auto;'>
                        </div>
                    </div>
                    """
                    alt.attach(MIMEText(html_with_img, 'html', 'utf-8'))

                    related = MIMEMultipart('related')
                    related.attach(alt)

                    with open(image_path, 'rb') as f:
                        img_data = f.read()

                    img = MIMEImage(img_data)
                    img.add_header('Content-ID', '<audit_img>')
                    img.add_header('Content-Disposition', 'inline')
                    related.attach(img)

                    msg.attach(related)
                else:
                    html_plain = f"""
                    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; max-width: 600px; margin: 0 auto; color: #1a1a1a; line-height: 1.6;">
                        {html_body}
                    </div>
                    """
                    alt.attach(MIMEText(html_plain, 'html', 'utf-8'))
                    msg.attach(alt)

                self.client.send_raw_email(
                    Source=self.from_email,
                    Destinations=[to_email],
                    RawMessage={'Data': msg.as_string()}
                )
                return message_id

            except ClientError as e:
                error_code = e.response.get("Error", {}).get("Code", "")
                error_message = e.response.get("Error", {}).get("Message", "")

                if "Daily message quota exceeded" in error_message or "LimitExceeded" in error_code:
                    raise Exception(f"SES Daily sending quota exceeded: {error_message}") from e

                if error_code == "MessageRejected":
                    print(f"Message rejected by SES: {error_message}")
                    return False

                if error_code == "Throttling":
                    if attempt < retries:
                        time.sleep(5)
                        continue
                    else:
                        print(f"Throttling error after retry: {error_message}")
                        return False

                print(f"SES ClientError ({error_code}): {error_message}")
                return False

            except Exception as e:
                print(f"Unexpected error sending email: {e}")
                return False

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
                f"\nBest,\n{your_name}"
            ]
        else:
            body_lines = [
                f"Hi {contact_name},\n",
                "I'll stop bugging you after this! Just wanted to follow up one last time regarding your mobile site.",
                "If fixing these UI issues is a priority for this quarter, I'd love to show you how we'd tackle it.",
                "Either way, wishing you a great week ahead.\n",
                f"\nCheers,\n{your_name}"
            ]
            
        return "\n".join(body_lines)

    def send_followup(self, to_email: str, original_subject: str, body: str, in_reply_to: str = "") -> bool:
        """
        Send a follow-up email, threaded to the original via real
        In-Reply-To/References headers (not just a matching "Re:" subject —
        that alone doesn't make Gmail/Outlook group it as one thread; it
        just makes an unrelated new message look like a spoofed reply,
        which reads worse to spam filters than an honest new email).
        """
        if db.is_suppressed(to_email):
            print(f"Skipping follow-up to {to_email}: on the unsubscribe/suppression list")
            return False

        subject = original_subject
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"

        unsub_headers = self._unsubscribe_headers(to_email)
        retries = 1
        for attempt in range(retries + 1):
            try:
                html_body = body.replace('\n', '<br>')
                html_template = f"""
                <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; max-width: 600px; color: #1a1a1a; line-height: 1.6;">
                    {html_body}
                </div>
                """

                msg = MIMEMultipart('alternative')
                msg['Subject'] = subject
                msg['From'] = self.from_email
                msg['To'] = to_email
                msg['Date'] = formatdate(localtime=True)
                msg['Message-ID'] = make_msgid(domain=self._msgid_domain())
                if in_reply_to:
                    msg['In-Reply-To'] = in_reply_to
                    msg['References'] = in_reply_to
                for header, value in unsub_headers.items():
                    msg[header] = value

                msg.attach(MIMEText(body, 'plain', 'utf-8'))
                msg.attach(MIMEText(html_template, 'html', 'utf-8'))

                self.client.send_raw_email(
                    Source=self.from_email,
                    Destinations=[to_email],
                    RawMessage={'Data': msg.as_string()}
                )
                return True
            except ClientError as e:
                if attempt < retries:
                    time.sleep(5)
                    continue
                print(f"SES Error sending follow-up: {e}")
                return False
            except Exception as e:
                print(f"Unexpected error in follow-up: {e}")
                return False

    def check_ses_quota(self) -> dict:
        """
        Get the remaining SES daily sending quota.

        Returns:
            A dict containing 'Max24HourSend', 'SentLast24Hours', and 'Remaining'.
        """
        try:
            response = self.client.get_send_quota()
            max_send = response.get("Max24HourSend", 0.0)
            sent = response.get("SentLast24Hours", 0.0)
            remaining = max(0.0, max_send - sent)
            
            return {
                "Max24HourSend": max_send,
                "SentLast24Hours": sent,
                "Remaining": remaining
            }
        except Exception as e:
            print(f"Error getting SES quota: {e}")
            return {
                "Max24HourSend": 0.0,
                "SentLast24Hours": 0.0,
                "Remaining": 0.0
            }
