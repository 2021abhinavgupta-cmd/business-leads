"""
Decision Maker — enriches leads with Instagram handles and decision-maker
contacts, then scores and qualifies them.

Uses free external services:
    - duckduckgo-search (Google alternative for Instagram handle discovery)
    - Custom Website Scraper (Regex on /contact, /about for emails)
    - Fallback generic email patterns
"""

import re
import time
from urllib.parse import urlparse, urljoin

import httpx
from ddgs import DDGS
from googlesearch import search as google_search
from email_validator import validate_email, EmailNotValidError

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


class DecisionMaker:
    """Enrich, score, and qualify leads."""

    def __init__(self, min_score: int = 50):
        self.min_score = min_score
        # Relaxed httpx client for scraping random websites (fast timeout to prevent 502)
        self.client = httpx.Client(timeout=5)
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }

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
            1. Custom website scraper looking for public emails.
            2. Fallback to generic email patterns.

        Args:
            company_name: Business name.
            website:      Business website URL.
            html_content: Pre-rendered Playwright HTML of the homepage (optional).

        Returns:
            ``{"name": str, "email": str, "title": str}``
        """
        domain = self._extract_domain(website)
        if not domain:
            return {"name": "", "email": "", "title": ""}

        if not website.startswith(("http://", "https://")):
            website = f"https://{website}"

        # Generic salutation, personalized to the actual business instead of
        # a fixed "Marketing Team" string, since we usually can't infer a
        # real person's name from a scraped/guessed inbox address.
        generic_name = f"{company_name} Team" if company_name else "Team"

        # Strategy 1 — Internal Scraper (Now powered by Playwright HTML)
        scraped_email = self._scrape_website_for_email(website, html_content)
        if scraped_email:
            return {
                "name": generic_name,
                "email": scraped_email,
                "title": ""
            }

        # Strategy 2 — LinkedIn OSINT (CEO Discovery)
        ceo_name = self._find_ceo_name(company_name)
        if ceo_name:
            verified_email = self._guess_and_verify_email(ceo_name, domain)
            return {
                "name": ceo_name,
                "email": verified_email,
                "title": "Founder / CEO"
            }

        # Strategy 3 — OSINT Email Dork
        osint_email = self._find_email_via_osint(company_name, domain)
        if osint_email:
            return {
                "name": generic_name,
                "email": osint_email,
                "title": ""
            }

        # Strategy 4 — generic email patterns
        return self._fallback_patterns(domain, generic_name)

    # ------------------------------------------------------------------
    # Custom Website Scraper for Emails
    # ------------------------------------------------------------------

    def _scrape_website_for_email(self, base_url: str, html_content: str | None = None) -> str:
        """
        Crawl homepage, /contact, and /about pages looking for emails.
        If html_content is provided, it scans that first without making a network request.
        """
        found_emails = set()
        
        # Check provided Playwright HTML first (bypasses Cloudflare)
        if html_content:
            emails = set(re.findall(_EMAIL_REGEX, html_content))
            found_emails.update(emails)
            
        # If we didn't find anything, try standard HTTP ping on contact pages
        if not found_emails:
            paths_to_check = ["", "/contact"]
            for path in paths_to_check:
                url = urljoin(base_url, path)
                try:
                    response = self.client.get(url, headers=self.headers, follow_redirects=True)
                    if response.status_code == 200:
                        emails = set(re.findall(_EMAIL_REGEX, response.text))
                        found_emails.update(emails)
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

    def _guess_and_verify_email(self, name: str, domain: str) -> str:
        """Generate common email patterns for a name and verify via SMTP."""
        parts = name.lower().split()
        if not parts:
            return ""
            
        first = re.sub(r'[^a-z]', '', parts[0])
        last = re.sub(r'[^a-z]', '', parts[-1]) if len(parts) > 1 else ""
        
        patterns = [f"{first}@{domain}"]
        if last:
            patterns.extend([
                f"{first}.{last}@{domain}",
                f"{first}{last}@{domain}",
                f"{first[0]}{last}@{domain}",
                f"{first[0]}.{last}@{domain}"
            ])
            
        # Catch-all detection: if a completely fake email validates, the domain is a catch-all
        # and SMTP guessing is useless.
        is_catch_all = False
        try:
            validate_email(f"bounce-test-992384@{domain}", check_deliverability=True)
            is_catch_all = True
        except Exception:
            pass
            
        if not is_catch_all:
            for candidate in patterns:
                try:
                    # check_deliverability=True performs DNS MX and SMTP checks
                    valid = validate_email(candidate, check_deliverability=True)
                    return valid.normalized
                except EmailNotValidError:
                    continue
                
        # Fallback to firstname if all verification fails (or if it's a catch-all)
        return patterns[0]

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
    # Fallback generic patterns
    # ------------------------------------------------------------------

    @staticmethod
    def _fallback_patterns(domain: str, generic_name: str = "Marketing Team") -> dict:
        """
        Return a generic marketing email for *domain* when all
        external lookups fail.
        """
        patterns = [
            f"marketing@{domain}",
            f"info@{domain}",
            f"hello@{domain}",
        ]
        return {
            "name": generic_name,
            "email": patterns[0],
            "title": "",
        }

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
