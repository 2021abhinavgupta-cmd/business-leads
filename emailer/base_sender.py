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

    @property
    def reply_to(self) -> str | None:
        """
        Where replies should land. Falls back to the sending address, so an
        environment that has never heard of REPLY_TO_EMAIL behaves exactly as
        before.

        Derived on every read rather than frozen in __init__. As a stored
        attribute it silently desynchronised the moment anything reassigned
        `from_email` after construction: the sender would keep using the OLD
        address for `Reply-To` and for the `mailto:` in `List-Unsubscribe`,
        while the `From` header showed the new one. A Reply-To that disagrees
        with From is a phishing pattern to a spam filter, which is the exact
        class of deliverability damage this file exists to avoid — and it
        would have been invisible, since nothing compares the two.
        """
        return config.REPLY_TO_EMAIL or self.from_email

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
        #
        # Skipped entirely when there is no address to point it at. With
        # FROM_EMAIL and REPLY_TO_EMAIL both unset this used to interpolate
        # the string "None", shipping every message a literal
        # `<mailto:None?subject=Unsubscribe>` — a header that parses, looks
        # present to a filter, and goes nowhere. A malformed opt-out
        # mechanism is worse than an absent one: Gmail and Yahoo's bulk
        # sender rules check that it works, and a broken one is the kind of
        # thing that only shows up as unexplained spam placement.
        targets = []
        if self.reply_to:
            targets.append(f"<mailto:{self.reply_to}?subject=Unsubscribe>")

        headers = {}
        if config.APP_BASE_URL:
            url = f"{config.APP_BASE_URL}/unsubscribe?email={quote(to_email, safe='')}"
            targets.append(f"<{url}>")
            headers["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

        if targets:
            headers["List-Unsubscribe"] = ", ".join(targets)
        return headers

    # --- copy generation ------------------------------------------------

    # Deliberately generic — no claim about THIS business qualifying for a
    # scheme, since that's unverifiable from an audit and a false claim there
    # would be worse than no line at all. Just the true, general point that
    # ag buyers now search online first. Used when sector_detail doesn't
    # match a more specific scheme below.
    _GENERIC_AGRI_LINE = (
        "Worth noting for your sector specifically: buyers and "
        "cooperatives increasingly search online first, including for "
        "suppliers linked to government schemes and subsidies. A weak "
        "web presence costs you discoverability, not just polish.\n"
    )

    # (keyword substrings matched against sector_detail.lower(), the line to
    # use). Both PM-KUSUM and SMAM are real, active 2026 central schemes
    # (live-verified) — naming them is a true general fact about buyer
    # search behavior in that category, never a claim that this specific
    # lead is empanelled/approved under either one.
    _AGRI_SCHEME_LINES = [
        (("irrigation",), (
            "Worth noting for your category specifically: PM-KUSUM's solar "
            "irrigation subsidy has buyers actively searching for pump and "
            "irrigation suppliers online right now. A weak web presence "
            "costs you exactly that traffic, not just polish.\n"
        )),
        (("tractor", "farm equipment", "equipment rental", "equipment dealer"), (
            "Worth noting for your category specifically: SMAM's farm "
            "machinery subsidy has buyers actively searching for equipment "
            "dealers online right now. A weak web presence costs you "
            "exactly that traffic, not just polish.\n"
        )),
    ]

    # Real credential, agriculture leads only (added 2026-09-07, on request;
    # narrowed same day to its own explicit flag rather than firing on
    # every sector=="agriculture" lead automatically; corrected same day
    # again after the user clarified the actual claim). Metazyne (metazyne.in)
    # and Agrius (@agriusindia) are two of MMGA's own agriculture clients:
    # MMGA built the Metazyne website and runs the Agrius social pages.
    # NOT "MMGA's own brand" — the first version of this line said that,
    # which was factually wrong, and it was corrected on direct user
    # feedback. A true, checkable fact about MMGA's own work in this
    # sector, not a claim about the lead. Only appears when the frontend's
    # dedicated "Generate for Agriculture" button set include_agri_credibility,
    # not on the ordinary "Generate AI Audit & Draft" button even for an
    # agriculture-tagged lead.
    _AGRI_CREDIBILITY_LINE = (
        "Worth mentioning: we work in agriculture too. We built the "
        "Metazyne website (metazyne.in) and manage the Agrius social "
        "pages (instagram.com/agriusindia).\n"
    )

    def generate_email(
        self, company: str, contact_name: str, analysis: dict, your_name: str,
        sector: str = "", sector_detail: str = "", include_agri_credibility: bool = False,
    ) -> tuple[str, str]:
        """
        Generate the subject and body for the cold email.

        Args:
            company: The target company's name.
            contact_name: The first name of the decision maker.
            analysis: The AI audit dict containing flaws and email copy.
            your_name: The sender's name to sign off with.
            sector: Optional lead category tag ("agriculture" today) used to
                add one sector-flavored sentence to the pitch. Empty for every
                other lead, so this is additive and never changes existing
                copy.
            sector_detail: Optional niche/category within the sector (e.g.
                "Irrigation Equipment Supplier"), used to name a real,
                specific government scheme instead of a generic sentence —
                see _AGRI_SCHEME_LINES. Falls back to the generic line when
                empty or unmatched.
            include_agri_credibility: Adds _AGRI_CREDIBILITY_LINE (the
                Metazyne/@agriusindia mention) when True AND sector ==
                "agriculture" — both, not either. The frontend only sets
                this flag from a dedicated "Generate for Agriculture" button
                (opt-in per draft, not automatic for every agri lead), and
                the sector check is a server-side backstop so the flag alone
                can never put an agriculture-specific claim on a lead from
                any other sector.

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

        # Deliberately generic — no claim about THIS business qualifying for
        # any specific scheme, since that's unverifiable from an audit and a
        # false claim there would be worse than no line at all. Just the
        # true, general point that ag buyers now search online first.
        if sector == "agriculture":
            line = self._GENERIC_AGRI_LINE
            detail_lower = (sector_detail or "").lower()
            for keywords, scheme_line in self._AGRI_SCHEME_LINES:
                if any(k in detail_lower for k in keywords):
                    line = scheme_line
                    break
            body_lines.append(line)

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

        if sector == "agriculture" and include_agri_credibility:
            body_lines.append(self._AGRI_CREDIBILITY_LINE)

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

        Three-email sequence by design (2026-08-10, on the founder's own
        framing): email 1 states the problem (generate_email), stage 1 here
        re-offers help, stage 2 asks for an explicit yes/no so the thread
        actually closes instead of trailing off unanswered. Whether this
        ever REACHES anyone depends on scheduler.py running as its own
        always-on service and on run_followups() skipping anyone who already
        replied (see main.py) — a follow-up sequence that mails someone who
        already said yes or no is worse than no sequence at all.

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
                "Wanted to follow up in case my earlier note got buried — still happy to help if it's useful.",
                "I'm glad to walk through what I'd fix first, no pressure either way.\n",
                f"Best,\n{your_name}",
            ]
        else:
            body_lines = [
                f"Hi {contact_name},\n",
                "Last note from me on this — just reply YES if it's worth a quick call, or NO and I'll leave it there.",
                "Either way, thanks for reading, and wishing you a great week ahead.\n",
                f"Cheers,\n{your_name}",
            ]

        return "\n".join(body_lines)

    # --- message construction -------------------------------------------

    # Caption for each capture, keyed by the Content-ID it is embedded under.
    #
    # These say what the image ACTUALLY is. The single hardcoded caption this
    # replaced read "Here is the screenshot my team took of your website on
    # mobile:" while /api/send attached `<company>_<hash>_audit.jpg` — the
    # DESKTOP capture (analyzer/visuals.py saves the desktop bytes under that
    # name; the mobile one is `_mobile.jpg` and was never attached to
    # anything). So every screenshot email opened its evidence section with a
    # false statement about the evidence.
    #
    # This is the same bug CLAUDE.md §8 records as fixed in generate_followup
    # on 2026-08-09 ("Did you get a chance to see the mobile website
    # screenshot I attached?"). That fix corrected the follow-up copy and
    # never reached this template, so the falsehood simply moved to the first
    # touch and stayed there. Captions are derived from which file is actually
    # attached now, rather than written once and assumed.
    _IMAGE_CAPTIONS = {
        "audit_img": "How your site renders on a desktop browser:",
        "audit_img_mobile": "And how the same page renders on a phone:",
        # Only ever present alongside audit_img, when a box was actually
        # drawn on that capture — see analyzer/visuals.py's
        # _capture_closeup. Added after a live report that a reader could
        # mistake the marker on the full-page image for the site's own
        # design; a tight crop of the same element removes that doubt
        # outright instead of relying on the marker alone.
        "audit_img_closeup": "A close up crop of the exact spot the magenta box above is pointing at:",
    }

    def _build_initial_message(
        self, to_email: str, subject: str, body: str, message_id: str,
        image_path: str = None, mobile_image_path: str = None,
        closeup_image_path: str = None,
    ) -> MIMEMultipart:
        """
        Build the first-touch message as multipart/mixed raw MIME carrying
        both a text/plain and a text/html part (spam filters weight a missing
        text/plain alternative heavily) plus List-Unsubscribe headers.

        Any screenshot given is embedded inline as a related part rather than
        attached as a file. All three captures are included when they exist,
        each under its own caption — the desktop view is where the marked
        evidence is drawn, the close up is a tight crop of the same marked
        element for anyone who can't easily spot it at full-page scale, and
        the mobile view is what most of these prospects' customers actually
        see.
        """
        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"] = self.from_email
        # Same reasoning as the unsubscribe mailto: omit the header rather
        # than emit a literal "None" for it.
        if self.reply_to:
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

        # (Content-ID, file path) for each capture that really exists on disk.
        # A path that was passed but never written (a failed mobile pass, a
        # screenshot cleaned up early) is dropped here rather than producing a
        # caption for an image the recipient will see as a broken placeholder.
        attachments = [
            (cid, path)
            for cid, path in (
                ("audit_img", image_path),
                ("audit_img_closeup", closeup_image_path),
                ("audit_img_mobile", mobile_image_path),
            )
            if path and os.path.exists(path)
        ]

        if attachments:
            figures = "".join(
                f"""
                            <p style="color: #475569; font-size: 14px; margin: 16px 0 8px;">{self._IMAGE_CAPTIONS[cid]}</p>
                            <img src='cid:{cid}' alt='Website audit screenshot' style='max-width: 100%; height: auto; border-radius: 8px; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06); display: block; margin: 0 auto;'>"""
                for cid, _ in attachments
            )
            html_with_img = f"""
                    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; max-width: 600px; margin: 0 auto; color: #1a1a1a; line-height: 1.6;">
                        <div style="padding: 20px;">
                            {html_body}
                        </div>
                        <div style="background-color: #f8fafc; padding: 24px; border-radius: 12px; margin: 20px 0; border: 1px solid #e2e8f0;">
                            <h3 style="margin-top: 0; color: #0f172a; font-size: 16px;">Visual Audit Evidence</h3>{figures}
                        </div>
                        {pixel}
                    </div>
                    """
            alt.attach(MIMEText(html_with_img, "html", "utf-8"))

            related = MIMEMultipart("related")
            related.attach(alt)

            for cid, path in attachments:
                with open(path, "rb") as f:
                    img_data = f.read()

                img = MIMEImage(img_data)
                img.add_header("Content-ID", f"<{cid}>")
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
        # Same reasoning as the unsubscribe mailto: omit the header rather
        # than emit a literal "None" for it.
        if self.reply_to:
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

    def send_email(self, to_email: str, subject: str, body: str, image_path: str = None,
                   mobile_image_path: str = None, closeup_image_path: str = None):
        """
        Send the first-touch cold email.

        Args:
            image_path:         Desktop screenshot, the one carrying the marked box.
            mobile_image_path:  Mobile screenshot. Optional and independent —
                any of the three may be given, and the captions in the
                message are built from whichever actually exist on disk.
            closeup_image_path: Tight crop of just the marked element on the
                desktop capture. Only ever set when a box was actually drawn.

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
                to_email, subject, body, message_id,
                image_path=image_path, mobile_image_path=mobile_image_path,
                closeup_image_path=closeup_image_path,
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
