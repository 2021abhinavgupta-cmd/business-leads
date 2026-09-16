"""
Apollo Free Scraper — Uses the Apollo.io REST API (Free Tier).
"""

import httpx
import time
import config

class ApolloFreeScraper:
    def __init__(self):
        self.api_key = config.APOLLO_API_KEY
        # Was "https://api.apollo.io/v1/..." (missing the /api segment) until
        # 2026-09-16 — confirmed against Apollo's own docs
        # (docs.apollo.io/reference/people-api-search) that the real path is
        # under /api/v1/, not /v1/. The old URL would 404/fail on every call.
        self.base_url = "https://api.apollo.io/api/v1/mixed_people/api_search"
        # Search never returns email/phone at all — confirmed against Apollo's
        # own docs ("This endpoint doesn't return email addresses or phone
        # numbers. Use the people enrichment ... endpoints."). Getting a real
        # email needs this separate call per person, which costs Apollo
        # credits (1 if an email is found, 0 if not) — added 2026-09-16 after
        # confirming with the user this cost is acceptable, since a lead with
        # no email is dead weight for a tool whose whole job is sending cold
        # emails.
        self.match_url = "https://api.apollo.io/api/v1/people/match"

    def _enrich_email(self, person: dict, org_name: str) -> str:
        """
        One enrichment call for one person. Returns "" on any failure
        (not found, missing name, HTTP error) rather than raising — a lead
        with no email just falls back to "no email, contact via phone/
        LinkedIn instead", same as any other source's no-email lead; it must
        never take down the rest of the batch.
        """
        first_name = person.get("first_name", "")
        last_name = person.get("last_name", "")
        if not (first_name and last_name):
            return ""

        params = {
            "first_name": first_name,
            "last_name": last_name,
            "reveal_personal_emails": True,
        }
        if org_name:
            params["organization_name"] = org_name

        headers = {
            "Cache-Control": "no-cache",
            "Content-Type": "application/json",
            "X-Api-Key": self.api_key,
        }
        try:
            with httpx.Client(timeout=15) as client:
                response = client.post(self.match_url, headers=headers, json=params)
                response.raise_for_status()
                matched = response.json().get("person") or {}
                return matched.get("email") or ""
        except Exception as e:
            print(f"Apollo enrichment failed for {first_name} {last_name}: {e}")
            return ""

    def scrape(self, niche: str, limit: int = 50) -> list[dict]:
        """
        Search Apollo for decision makers in a specific niche.
        """
        if not self.api_key:
            print("APOLLO_API_KEY is not set in .env. Skipping Apollo scraper.")
            return []
            
        print(f"Scraping Apollo (Free API) for: {niche}...")
        leads = []
        
        headers = {
            "Cache-Control": "no-cache",
            "Content-Type": "application/json",
            "X-Api-Key": self.api_key
        }
        
        # Searching for founders/CEOs in the given niche
        payload = {
            "q_keywords": niche,
            "person_titles": ["founder", "ceo", "owner", "cmo", "marketing"],
            "page": 1,
            "per_page": min(limit, 100) # Apollo limit per page
        }

        for attempt in range(3):
            try:
                with httpx.Client(timeout=30) as client:
                    response = client.post(self.base_url, headers=headers, json=payload)
                    response.raise_for_status()
                    data = response.json()
                    break # Success
                    
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429 and attempt < 2:
                    print(f"Apollo API Rate Limit hit (429). Sleeping 60s...")
                    time.sleep(60)
                else:
                    print(f"Apollo API HTTP error: {e}")
                    if hasattr(e, 'response') and e.response:
                        print(e.response.text)
                    return leads
            except Exception as e:
                print(f"Error scraping Apollo: {e}")
                return leads
                
        people = data.get("people", [])
        credits_used = 0
        for person in people:
            org = person.get("organization", {})

            company_name = org.get("name", "")
            website = org.get("website_url", "")
            first_name = person.get("first_name", "")
            last_name = person.get("last_name", "")

            if not company_name or not website:
                # Skip enrichment too — this lead is being discarded either
                # way, no point spending a credit on it.
                continue

            email = self._enrich_email(person, company_name)
            if email:
                credits_used += 1

            leads.append({
                "Company": company_name,
                "Website": website,
                "Phone": org.get("primary_phone", {}).get("number", ""),
                "Address": org.get("city", ""),
                "Rating": "Apollo B2B",
                "Reviews Count": org.get("estimated_num_employees", 0),
                "Email": email,
                "Instagram Handle": "",
                "Decision Maker Name": f"{first_name} {last_name}".strip(),
                "Source": "Apollo Free API"
            })

        print(f"Apollo: {len(leads)} leads, {credits_used} enrichment credit(s) spent finding an email.")
        return leads
