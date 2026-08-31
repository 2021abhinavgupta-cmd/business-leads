"""
Decision Maker — enriches leads with Instagram handles and decision-maker
contacts, then scores and qualifies them.

Uses free external services:
    - duckduckgo-search (Google alternative for Instagram handle discovery)
    - Custom Website Scraper (Regex on /contact, /about for emails)
    - Fallback generic email patterns
"""

import json
import random
import re
import smtplib
import socket
import time
from urllib.parse import urlparse, urljoin

import anthropic
import httpx
from ddgs import DDGS
from googlesearch import search as google_search
from email_validator import validate_email, EmailNotValidError, EmailUndeliverableError

import config

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_MARKETING_KEYWORDS = {"marketing", "brand", "digital"}
_EMAIL_REGEX = r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"
# Local-parts that are never a real contact address, even if they match a
# company's domain (e.g. transactional "noreply@" addresses picked up by
# regex/OSINT search snippets) — sending outreach to these bounces or gets
# auto-discarded.
_JUNK_LOCAL_PARTS = {
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "postmaster", "mailer-daemon", "webmaster", "abuse",
}


def _is_junk_email(email: str) -> bool:
    local_part = email.split("@", 1)[0].lower()
    return local_part in _JUNK_LOCAL_PARTS


# ---------------------------------------------------------------------------
# Free email extraction — no API, no paid enrichment service
# ---------------------------------------------------------------------------
# A plain regex over visible page text misses two very common cases on the
# small-business sites this tool targets, and picks up noise a mailto: link
# never would. These helpers close that gap without any external call.

# Cloudflare's email obfuscation ("Email Address Protection") replaces the
# real address with <a class="__cf_email__" data-cfemail="HEX"> and also
# leaves /cdn-cgi/l/email-protection#HEX in href attributes. The HEX is the
# address XOR-encoded against its own first byte — trivially reversible, and
# a large share of these leads sit behind Cloudflare, so their contact
# address is sitting in the HTML in a form the regex simply cannot see.
_CF_EMAIL_ATTR = re.compile(r'data-cfemail="([0-9a-fA-F]+)"')
_CF_EMAIL_HREF = re.compile(r'/cdn-cgi/l/email-protection#([0-9a-fA-F]+)')

# mailto: links are a far stronger signal than a string that merely matches
# the email shape — a human put that address there on purpose as the contact.
_MAILTO_HREF = re.compile(r'mailto:([^"\'?>\s]+)', re.IGNORECASE)

# "name [at] company [dot] com" style obfuscation, meant to defeat exactly
# the kind of regex scrape this file does. Normalised back before matching.
#
# ONLY the bracketed forms — [at] (at) [dot] (dot) — are handled. The bare
# word forms ("bob at acme dot com") are deliberately NOT: " at " and " dot "
# occur constantly in ordinary prose ("great at design", "the dot com era"),
# and turning those into an @ or a . manufactures addresses that were never
# on the page. A fabricated recipient is the single worst error this module
# can make (CLAUDE.md §8), so the ambiguous case is left alone.
_OBFUSCATION_SUBS = [
    (re.compile(r'\s*[\[(]\s*at\s*[\])]\s*', re.IGNORECASE), '@'),
    (re.compile(r'\s*[\[(]\s*dot\s*[\])]\s*', re.IGNORECASE), '.'),
]


def _decode_cf_email(encoded_hex: str) -> str:
    """Reverse Cloudflare's data-cfemail encoding. Returns '' on any malformed input."""
    try:
        key = int(encoded_hex[:2], 16)
        return "".join(
            chr(int(encoded_hex[i:i + 2], 16) ^ key)
            for i in range(2, len(encoded_hex), 2)
        )
    except (ValueError, IndexError):
        return ""


def _deobfuscate(text: str) -> str:
    for pattern, replacement in _OBFUSCATION_SUBS:
        text = pattern.sub(replacement, text)
    return text


def _extract_emails_from_html(html: str) -> set[str]:
    """
    Every address discoverable in *html* without a network call: decoded
    Cloudflare-protected ones, mailto: targets, de-obfuscated "x at y dot z"
    ones, and finally the plain regex. Lower-cased; not yet filtered for junk
    or validity — the caller does that.
    """
    if not html:
        return set()

    found: set[str] = set()

    for hex_blob in _CF_EMAIL_ATTR.findall(html) + _CF_EMAIL_HREF.findall(html):
        decoded = _decode_cf_email(hex_blob)
        if "@" in decoded:
            found.add(decoded.strip().lower())

    for target in _MAILTO_HREF.findall(html):
        # A mailto: can carry ?subject=... etc — already stripped by the
        # pattern, but it can also be a comma-separated list.
        for addr in target.split(","):
            addr = addr.strip().lower()
            if "@" in addr:
                found.add(addr)

    for match in re.findall(_EMAIL_REGEX, _deobfuscate(html)):
        found.add(match.strip().lower())

    return found


# ---------------------------------------------------------------------------
# Free mailbox verification — an SMTP conversation, no paid service
# ---------------------------------------------------------------------------
# Off unless config.VERIFY_EMAIL_SMTP is set. See that setting's comment for
# why the default is off (RCPT probes from a datacenter IP are a
# sender-reputation risk). Everything here is designed to only ever *raise*
# confidence in an address: an inconclusive or blocked probe leaves a guess
# flagged exactly as it was.

_SMTP_PROBE_TIMEOUT = 8
# After this many probes in a row come back blocked/greylisted/refused, stop
# probing for the rest of the process. One mail host refusing us is a strong
# hint the source IP is being throttled, and hammering past that is exactly
# what gets an IP listed. Same circuit-breaker shape as scrapers/instagram.py.
_SMTP_MAX_CONSECUTIVE_FAILURES = 3
_smtp_consecutive_failures = 0
_smtp_breaker_tripped = False

# Local-part patterns for a person, most-likely first. Only used when SMTP
# verification is on — without it there is nothing to choose between them and
# the caller just takes the first.
def _person_email_patterns(name: str, domain: str) -> list[str]:
    parts = [re.sub(r'[^a-z]', '', p) for p in name.lower().split()]
    parts = [p for p in parts if p]
    if not parts:
        return []
    first = parts[0]
    last = parts[-1] if len(parts) > 1 else ""
    if not last:
        return [f"{first}@{domain}"]
    locals_ = [
        f"{first}.{last}", f"{first}{last}", f"{first}",
        f"{first[0]}{last}", f"{first}_{last}", f"{first[0]}.{last}",
    ]
    seen, ordered = set(), []
    for lp in locals_:
        if lp not in seen:
            seen.add(lp)
            ordered.append(f"{lp}@{domain}")
    return ordered

# Generic role addresses to try for a business when no person is known, most
# likely to be read first.
_ROLE_LOCAL_PARTS = ("hello", "info", "contact", "enquiries", "hi", "team", "sales")


def _mx_hosts(domain: str) -> list[str]:
    """MX hostnames for *domain*, via email-validator's resolver (already a dep)."""
    try:
        result = validate_email(f"probe@{domain}", check_deliverability=True)
        mx = getattr(result, "mx", None) or []
        # email-validator returns [(preference, host), ...] sorted by preference.
        return [host for _pref, host in mx if host]
    except Exception:
        return []


def _smtp_probe(mx_host: str, recipient: str) -> int | None:
    """
    One RCPT TO probe. Returns the SMTP status code, or None if the
    conversation could not be completed (connection refused, timeout, the
    server dropped us). Uses an empty MAIL FROM, the standard "just checking"
    envelope that does not imply an actual send.
    """
    try:
        with smtplib.SMTP(mx_host, 25, timeout=_SMTP_PROBE_TIMEOUT) as smtp:
            smtp.ehlo_or_helo_if_needed()
            smtp.mail("")
            code, _msg = smtp.rcpt(recipient)
            return code
    except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError,
            socket.timeout, socket.gaierror, ConnectionError, OSError):
        return None
    except smtplib.SMTPException:
        return None


def _verify_mailbox(email: str) -> str:
    """
    'valid' | 'invalid' | 'catch_all' | 'inconclusive'

    Only 'valid' is safe to treat as a confirmed address. 'catch_all' means
    the server accepts everything so the probe proved nothing; 'inconclusive'
    means we were greylisted, blocked or could not connect. Both of the
    latter must leave the address exactly as trusted as it was before.
    """
    global _smtp_consecutive_failures, _smtp_breaker_tripped

    if _smtp_breaker_tripped:
        return "inconclusive"

    domain = email.split("@", 1)[1]
    hosts = _mx_hosts(domain)
    if not hosts:
        return "inconclusive"

    mx = hosts[0]
    real_code = _smtp_probe(mx, email)

    if real_code is None:
        _smtp_consecutive_failures += 1
        if _smtp_consecutive_failures >= _SMTP_MAX_CONSECUTIVE_FAILURES:
            _smtp_breaker_tripped = True
            print("[DecisionMaker] SMTP verification circuit breaker tripped "
                  "(mail hosts keep refusing us) — skipping further probes this run.")
        return "inconclusive"

    _smtp_consecutive_failures = 0

    # Greylisting / temporary refusal — a retry might clear it, but we do not
    # block a batch waiting, and a 4xx is never a confirmed rejection.
    if 400 <= real_code < 500:
        return "inconclusive"

    if real_code >= 500:
        return "invalid"

    # 2xx: the server accepted the recipient. Now check it is not just
    # accepting everything — probe an address that cannot exist.
    sentinel = f"nx-{random.randint(10_000_000, 99_999_999)}-verify@{domain}"
    sentinel_code = _smtp_probe(mx, sentinel)
    if sentinel_code is not None and 200 <= sentinel_code < 300:
        return "catch_all"

    return "valid"


class DecisionMaker:
    """Enrich, score, and qualify leads."""

    def __init__(self, min_score: int = 50):
        self.min_score = min_score
        # Relaxed httpx client for scraping random websites (fast timeout to prevent 502)
        self.client = httpx.Client(timeout=5)
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        # Last-resort strategy only — costs real tokens, so only wired up if
        # a key is present and only ever called after every free strategy
        # below has already failed.
        if config.ANTHROPIC_API_KEY:
            self._anthropic_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        else:
            self._anthropic_client = None

    # ------------------------------------------------------------------
    # 1. Instagram handle discovery
    # ------------------------------------------------------------------

    def find_instagram_handle(self, company_name: str, website: str) -> str:
        """
        Search DuckDuckGo for the company's Instagram profile.
        """
        for attempt in range(2):
            try:
                with DDGS() as ddgs:
                    results = list(ddgs.text(f"{company_name} instagram", max_results=5, backend="lite"))
                    if results:
                        break
            except Exception as e:
                print(f"DDG Error for {company_name}: {e}")
                results = []
                
        if not results:
            try:
                results = [{"href": res.url} for res in google_search(f"{company_name} instagram", num_results=5, advanced=True)]
            except Exception as e:
                print(f"Google fallback error: {e}")
                return ""

        for item in results:
            link = item.get("href", "")
            if "instagram.com/" not in link:
                continue

            match = re.search(r"instagram\.com/([a-zA-Z0-9_.]+)", link)
            if match:
                handle = match.group(1)
                # Skip generic Instagram pages
                if handle.lower() not in {"p", "explore", "accounts", "reel", "stories"}:
                    return handle

        return ""

    # ------------------------------------------------------------------
    # 2. Decision-maker contact discovery
    # ------------------------------------------------------------------

    def find_decision_maker(self, company_name: str, website: str, html_content: str | None = None) -> dict:
        """
        Find the best marketing contact for a company.

        Strategy order:
            1. Custom website scraper looking for public emails (now also
               decodes Cloudflare-protected addresses and mailto: links).
            2. LinkedIn OSINT (CEO/founder name). LinkedIn has no email, so
               the address is constructed from the name — a guess, unless
               config.VERIFY_EMAIL_SMTP is on and the mail server confirms
               one of the likely patterns exists.
            3. General OSINT email dork.
            4. Claude web_fetch (last resort, real API cost — only tried
               once every free strategy above has failed).
            5. Fallback to generic role patterns — an unverified marketing@
               guess, or an SMTP-confirmed role address when VERIFY_EMAIL_SMTP
               is on.

        Args:
            company_name: Business name.
            website:      Business website URL.
            html_content: Pre-rendered Playwright HTML of the homepage (optional).

        Returns:
            ``{"name", "email", "title", "is_guess", "domain_accepts_mail"}``

            ``is_guess`` is the important one: strategies 2 and 5 CONSTRUCT
            an address from a name or a common pattern and cannot confirm the
            mailbox exists, while 1, 3 and 4 read a real published address.
            Both used to return the identical dict shape, so nothing
            downstream could tell a scraped address from a fabricated one —
            and a fabricated one is a hard bounce, which damages sender
            reputation for every future send. See CLAUDE.md §8.
        """
        domain = self._extract_domain(website)
        if not domain:
            return {"name": "", "email": "", "title": "", "is_guess": False, "domain_accepts_mail": None}

        if not website.startswith(("http://", "https://")):
            website = f"https://{website}"

        # Generic salutation, personalized to the actual business instead of
        # a fixed "Marketing Team" string, since we usually can't infer a
        # real person's name from a scraped/guessed inbox address.
        generic_name = f"{company_name} Team" if company_name else "Team"

        # Strategy 1 — Internal Scraper (Now powered by Playwright HTML)
        scraped_email = self._scrape_website_for_email(website, html_content)
        if scraped_email:
            # Read off the business's own site — a real published address.
            return self._finalise(generic_name, scraped_email, "", is_guess=False)

        # Strategy 2 — LinkedIn OSINT (CEO Discovery)
        #
        # LinkedIn does not publish email addresses, so this can only get a
        # NAME. From the name we construct "first.last@domain" — a guess,
        # flagged is_guess=True, that a human has to check before sending.
        #
        # When config.VERIFY_EMAIL_SMTP is on, we first try each likely
        # pattern against the domain's own mail server; if one is confirmed to
        # exist (and the domain is not catch-all), that comes back as a real
        # verified address. If verification is off, inconclusive, or blocked,
        # we fall through to the same single guess as before — it never makes
        # the result worse.
        ceo_name = self._find_ceo_name(company_name)
        if ceo_name:
            if config.VERIFY_EMAIL_SMTP:
                verified = self._verified_email_from_name(ceo_name, domain)
                if verified:
                    return self._finalise(ceo_name, verified, "Founder / CEO", is_guess=False)
            guessed = self._guess_email_from_name(ceo_name, domain)
            if guessed:
                return self._finalise(ceo_name, guessed, "Founder / CEO", is_guess=True)

        # Strategy 3 — OSINT Email Dork
        osint_email = self._find_email_via_osint(company_name, domain)
        if osint_email:
            # Appeared verbatim in a real search result, not constructed.
            return self._finalise(generic_name, osint_email, "", is_guess=False)

        # Strategy 4 — Claude web_fetch (last resort, real API cost — only
        # reached when the three free strategies above all came up empty,
        # e.g. a JS-obfuscated email or contact info buried off the homepage)
        claude_result = self._find_via_claude_web_fetch(company_name, website)
        claude_cost = claude_result.get("cost", 0.0)

        if claude_result.get("email"):
            result = self._finalise(
                claude_result.get("name") or generic_name,
                claude_result["email"],
                "Founder / CEO" if claude_result.get("name") else "",
                is_guess=False,
            )
            result["cost"] = claude_cost
            return result

        # Strategy 5 — generic email patterns. When SMTP verification is on,
        # try the common role addresses (hello@, info@, contact@, ...) against
        # the mail server and return the first one that actually exists as a
        # verified address instead of an unverified marketing@ guess.
        if config.VERIFY_EMAIL_SMTP:
            verified_role = self._verified_role_email(domain)
            if verified_role:
                result = self._finalise(generic_name, verified_role, "", is_guess=False)
                if claude_cost:
                    result["cost"] = claude_cost
                return result

        result = self._fallback_patterns(domain, generic_name)
        if claude_cost:
            result["cost"] = claude_cost
        return result

    # ------------------------------------------------------------------
    # SMTP-verified address construction (only when VERIFY_EMAIL_SMTP)
    # ------------------------------------------------------------------

    def _verified_email_from_name(self, name: str, domain: str) -> str:
        """
        First "first.last@domain"-style pattern for *name* that the mail
        server confirms exists on a non-catch-all domain, or "" if none is
        confirmed. A catch-all or blocked probe returns "" so the caller
        falls back to its normal unverified guess.
        """
        for candidate in _person_email_patterns(name, domain):
            if _verify_mailbox(candidate) == "valid":
                return candidate
            if _smtp_breaker_tripped:
                break
        return ""

    def _verified_role_email(self, domain: str) -> str:
        """As above, for generic role addresses when no person is known."""
        for local_part in _ROLE_LOCAL_PARTS:
            candidate = f"{local_part}@{domain}"
            if _verify_mailbox(candidate) == "valid":
                return candidate
            if _smtp_breaker_tripped:
                break
        return ""

    # ------------------------------------------------------------------
    # Custom Website Scraper for Emails
    # ------------------------------------------------------------------

    def _scrape_website_for_email(self, base_url: str, html_content: str | None = None) -> str:
        """
        Crawl homepage, /contact, and /about pages looking for emails.
        If html_content is provided, it scans that first without making a network request.
        """
        found_emails = set()

        # Check provided Playwright HTML first (bypasses Cloudflare). Uses the
        # full extractor: decoded Cloudflare-protected addresses, mailto:
        # targets and de-obfuscated "x at y dot z" forms, not just the raw
        # regex — all three are common on these sites and invisible to a plain
        # regex over text.
        if html_content:
            found_emails.update(_extract_emails_from_html(html_content))

        # If we didn't find anything, try standard HTTP ping on contact pages.
        # More paths than before ("/contact" alone missed a lot): small
        # business sites put the address on an About or Team page as often as
        # a Contact one, and the slug varies.
        if not found_emails:
            paths_to_check = [
                "", "/contact", "/contact-us", "/contact-us/", "/about",
                "/about-us", "/team", "/get-in-touch", "/support",
            ]
            for path in paths_to_check:
                url = urljoin(base_url, path)
                try:
                    response = self.client.get(url, headers=self.headers, follow_redirects=True)
                    if response.status_code == 200:
                        found_emails.update(_extract_emails_from_html(response.text))
                        if found_emails:
                            break
                except Exception:
                    continue
                
        # Filter out common false positives (e.g. image files matching regex)
        valid_emails = []
        junk_extensions = (".png", ".jpg", ".jpeg", ".gif", ".css", ".js", ".svg", ".webp", ".mp4", "sentry.io", "example.com")
        
        for email in found_emails:
            email_lower = email.lower()
            if not email_lower.endswith(junk_extensions) and not _is_junk_email(email_lower):
                valid_emails.append(email_lower)

        if not valid_emails:
            return ""
            
        # Score emails to prioritize high-value contacts
        def score_email(e: str) -> int:
            if any(kw in e for kw in ["founder", "ceo", "director", "owner"]):
                return 100
            if any(kw in e for kw in ["marketing", "growth", "sales"]):
                return 80
            if any(kw in e for kw in ["hello", "hi", "contact", "info"]):
                return 50
            return 10  # obscure/personal emails
            
        valid_emails.sort(key=score_email, reverse=True)
        
        # Verify deliverability (DNS/MX lookup) before accepting
        for candidate in valid_emails:
            try:
                valid = validate_email(candidate, check_deliverability=True)
                return valid.normalized
            except EmailNotValidError:
                continue

        # Deliverability checks failed for every candidate — this is often a
        # false negative (DNS/MX lookups from a cloud host getting blocked or
        # timing out), not proof the address is fake. These emails were
        # scraped directly off the business's own site, so trust format
        # validity alone rather than cascading down to weaker OSINT-guessed
        # fallback strategies that produce worse results.
        try:
            return validate_email(valid_emails[0], check_deliverability=False).normalized
        except EmailNotValidError:
            return ""

    # ------------------------------------------------------------------
    # LinkedIn OSINT
    # ------------------------------------------------------------------

    def _find_ceo_name(self, company_name: str) -> str:
        """Search LinkedIn via DDG (with Google fallback) for the CEO/Founder's name."""
        query = f'site:linkedin.com/in "Founder" OR "CEO" "{company_name}"'
        results = []
        
        for attempt in range(2):
            try:
                with DDGS() as ddgs:
                    results = list(ddgs.text(query, max_results=3, backend="lite"))
                    if results:
                        break
            except Exception as e:
                print(f"DDG Error finding CEO for {company_name}: {e}")
                
        if not results:
            try:
                raw_results = list(google_search(query, num_results=3, advanced=True))
                results = [{"title": res.title} for res in raw_results]
            except Exception as e:
                pass
                
        if not results:
            return ""
            
        title = results[0].get("title", "")
        parts = re.split(r'[-|]', title)
        if parts:
            name = parts[0].strip()
            if len(name.split()) <= 3 and "LinkedIn" not in name:
                return name
        return ""

    def _guess_email_from_name(self, name: str, domain: str) -> str:
        """
        Construct the most likely address for a person at *domain*.

        Was `_guess_and_verify_email`, and the "verify" half was dead code:
        it tried to detect a catch-all domain by checking whether an obviously
        fake address validated, then only tried the real patterns if it
        didn't. But `email-validator`'s `check_deliverability` resolves DNS/MX
        records ONLY — it never opens an SMTP connection and cannot tell
        whether a specific mailbox exists, contrary to the old docstring. So
        the fake address validated on every domain that has mail at all,
        `is_catch_all` was always True, the pattern loop never ran once, and
        the function always returned patterns[0] anyway. Verified empirically
        against gmail.com and mmga.agency before removing it.

        Nothing available here can confirm a mailbox exists, so this is
        honestly named and its result is always marked is_guess=True by the
        caller. `check_deliverability` is still genuinely useful, just for a
        different question — see `domain_accepts_mail`.
        """
        parts = name.lower().split()
        if not parts:
            return ""

        first = re.sub(r'[^a-z]', '', parts[0])
        last = re.sub(r'[^a-z]', '', parts[-1]) if len(parts) > 1 else ""
        if not first:
            return ""

        return f"{first}.{last}@{domain}" if last else f"{first}@{domain}"

    @staticmethod
    def domain_accepts_mail(email: str) -> bool | None:
        """
        Can this address's domain receive mail at all?

        True  — the domain publishes MX (or A) records; a mailbox may or may
                not exist, but mail will at least be accepted for delivery.
        False — the domain resolves nowhere. Sending here is a GUARANTEED
                hard bounce, and hard bounces are the single most damaging
                thing for sender reputation (AWS reviews accounts at a ~5%
                bounce rate — one bad address in a 20-send batch clears that
                bar on its own, which has already nearly happened here once).
        None  — the lookup itself failed. Deliberately NOT treated as False:
                DNS from a cloud host is unreliable enough that a timeout
                would otherwise block real, valid leads (see CLAUDE.md §8 on
                exactly this, which is why scraped addresses fall back to
                format-only validation).
        """
        if not email or "@" not in email:
            return None
        try:
            validate_email(email, check_deliverability=True)
            return True
        except EmailUndeliverableError:
            return False
        except EmailNotValidError:
            # Malformed rather than undeliverable — a different problem, and
            # not one this function is meant to answer.
            return None
        except Exception:
            return None

    def _find_email_via_osint(self, company_name: str, domain: str) -> str:
        """Fallback OSINT search to scrape emails directly off Google/DDG."""
        query = f'"{company_name}" "@gmail.com" OR "@{domain}" email'
        results = []
        
        for attempt in range(2):
            try:
                with DDGS() as ddgs:
                    results = list(ddgs.text(query, max_results=5, backend="lite"))
                    if results:
                        break
            except Exception as e:
                print(f"DDG Error OSINT email for {company_name}: {e}")
                
        if not results:
            try:
                raw_results = list(google_search(query, num_results=5, advanced=True))
                results = [{"body": res.description, "title": res.title} for res in raw_results]
            except Exception:
                pass
                
        if not results:
            return ""
                    
        for res in results:
            text = res.get("body", "") + " " + res.get("title", "")
            emails = re.findall(_EMAIL_REGEX, text)
            for email in emails:
                email_lower = email.lower()
                if _is_junk_email(email_lower):
                    continue
                if email_lower.endswith(domain) or email_lower.endswith("@gmail.com"):
                    try:
                        valid = validate_email(email, check_deliverability=False)
                        return valid.normalized
                    except Exception:
                        pass
        return ""

    # ------------------------------------------------------------------
    # Claude web_fetch (last resort — real API cost)
    # ------------------------------------------------------------------

    def _find_via_claude_web_fetch(self, company_name: str, website: str) -> dict:
        """
        Last-resort contact discovery: has Claude itself fetch and read the
        site via Anthropic's web_fetch server tool, and pull out a contact
        email and (if named anywhere) a decision maker's name.

        Only ever called after the free scraper + LinkedIn OSINT + email-dork
        strategies above have all failed — e.g. a JS-obfuscated email, or
        contact info buried on a page our regex-based scraper didn't check.
        This costs real Anthropic API tokens (~$0.002-0.01/call), so it's
        deliberately gated to the failure path rather than run on every lead.

        web_fetch cannot render JavaScript (static HTML/text/PDF only), so
        this still won't help on a fully client-rendered contact page — it's
        a targeted patch for the "static HTML but regex-unfriendly" gap, not
        a general scraping upgrade.
        """
        if not self._anthropic_client:
            return {}

        try:
            response = self._anthropic_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                tools=[{"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 2}],
                messages=[{
                    "role": "user",
                    "content": (
                        f"Fetch {website} and, if there's an obvious contact/about page linked from it, "
                        f"fetch that too. Find the best real contact email address for the business "
                        f"\"{company_name}\", and the name of the founder/CEO/owner if one is mentioned "
                        "anywhere on the page. Reply with ONLY valid JSON, no markdown, no explanation, "
                        'in exactly this shape: {"email": "...", "name": "..."}. Use an empty string for '
                        "whichever you cannot find. Never invent or guess an email or name that isn't "
                        "literally written on the page."
                    ),
                }],
            )
        except Exception as e:
            print(f"[DecisionMaker] Claude web_fetch failed for {company_name}: {e}")
            return {}

        # Pricing: Claude Haiku 4.5, $0.25/1M input tokens, $1.25/1M output
        # tokens — web_fetch's fetched page content counts as input tokens,
        # so this can run noticeably higher than a plain text-only call.
        try:
            cost = (response.usage.input_tokens * 0.25 / 1_000_000) + (response.usage.output_tokens * 1.25 / 1_000_000)
        except Exception:
            cost = 0.005  # fallback estimate — a couple of fetched pages' worth of tokens

        # The final text reply (after any web_fetch tool-use turns) is the
        # last text block in the response content array.
        raw_text = ""
        for block in response.content:
            if block.type == "text":
                raw_text = block.text

        parsed = self._parse_claude_json(raw_text)
        if not parsed:
            return {"cost": cost}

        email = (parsed.get("email") or "").strip().lower()
        name = (parsed.get("name") or "").strip()

        if email:
            if _is_junk_email(email):
                email = ""
            else:
                try:
                    email = validate_email(email, check_deliverability=False).normalized
                except EmailNotValidError:
                    email = ""

        return {"email": email, "name": name, "cost": cost}

    @staticmethod
    def _parse_claude_json(raw: str) -> dict | None:
        """Strip markdown fences and parse the first {...} block, same pattern as analyzer/ai_audit.py."""
        cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip()
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1:
            return None
        try:
            return json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError:
            return None

    # ------------------------------------------------------------------
    # Fallback generic patterns
    # ------------------------------------------------------------------

    def _finalise(self, name: str, email: str, title: str, *, is_guess: bool) -> dict:
        """
        Build the contact result, recording how much to trust the address.

        Every strategy funnels through here so `is_guess` can never be
        forgotten on a new one, and so the domain deliverability check runs
        exactly once per lead regardless of which strategy won.
        """
        return {
            "name": name,
            "email": email,
            "title": title,
            "is_guess": is_guess,
            "domain_accepts_mail": self.domain_accepts_mail(email),
        }

    def _fallback_patterns(self, domain: str, generic_name: str = "Marketing Team") -> dict:
        """
        Return a generic marketing email for *domain* when all
        external lookups fail.

        This address is INVENTED — nothing has confirmed it exists — so it is
        always flagged is_guess=True. It used to be returned in the same
        shape as a genuinely scraped address, which meant the drafts inbox
        showed no difference between "we found their real email" and "we made
        one up", and a made-up address is a hard bounce.
        """
        return self._finalise(generic_name, f"marketing@{domain}", "", is_guess=True)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_domain(website: str) -> str:
        """
        Pull the bare domain from a URL.
        """
        if not website:
            return ""

        if not website.startswith(("http://", "https://")):
            website = f"https://{website}"

        netloc = urlparse(website).netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc
