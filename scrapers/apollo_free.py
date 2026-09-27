"""
Apollo Free Scraper — Uses the Apollo.io REST API (Free Tier).
"""

import httpx
import time
import config


class ApolloError(RuntimeError):
    """
    Apollo refused the request.

    Raised instead of returning an empty list, added 2026-09-27 after a live
    search reported "Added 0 leads from Apollo" and neither the user nor the
    logs could distinguish three completely different situations: an invalid
    or expired API key, a plan that does not permit the endpoint, and a
    search that genuinely matched nobody. All three produced the identical
    empty result, which turned a one-line answer into a guessing session.

    Carries a message written for the person running the search, not for the
    log, because it is surfaced in the UI.
    """


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
        # Bulk enrichment: same data and same credit cost per record as
        # match_url, but 10 people per request instead of 1. Added
        # 2026-09-28 — a 25 lead search was making 25 separate calls, which
        # is pure rate-limit exposure for no benefit.
        self.bulk_match_url = "https://api.apollo.io/api/v1/people/bulk_match"

    # Apollo's own label for how much it trusts an address. Only "verified"
    # is safe to actually send to: Apollo returns pattern-based GUESSES when
    # it cannot confirm an inbox, and published bounce rates on Apollo lists
    # run 3-9%. That matters more here than for most tools, because this
    # project sends through SES under a warm-up cap (config.DAILY_EMAIL_LIMIT)
    # where bounces damage sender reputation for EVERY send, not just the
    # Apollo ones. A guessed address is therefore kept and shown, but never
    # placed in the field the send path reads.
    _SENDABLE_EMAIL_STATUS = "verified"

    # Apollo's bulk endpoint takes at most 10 people per request.
    _BULK_ENRICH_BATCH = 10

    def _enrich_emails(self, people: list[dict]) -> list[dict]:
        """
        Emails for *people*, in the same order, via Apollo's bulk endpoint.

        Returns one dict per input person: {"email": str, "status": str}.
        A person Apollo could not match, or a whole batch that failed, comes
        back as {"email": "", "status": ""} — never an exception. A lead
        with no email just falls back to "contact via phone instead", same
        as any other source's no-email lead, and one bad batch must never
        take down the rest of the search.

        Uses /people/bulk_match (10 per call) rather than /people/match (1
        per call), added 2026-09-28. Same credit cost per record either way,
        so this is purely about call volume: a 25 lead search was 25 requests
        and is now 3, which matters because Apollo rate limits per minute.
        The bulk response also reports credits_consumed directly, so the
        credit figure logged below is Apollo's own number instead of this
        code inferring it from how many emails came back.
        """
        results = [{"email": "", "status": ""} for _ in people]
        if not people or not self.api_key:
            return results

        headers = {
            "Cache-Control": "no-cache",
            "Content-Type": "application/json",
            "X-Api-Key": self.api_key,
        }

        # Apollo cannot match a nameless person, so those are dropped from
        # the batches entirely rather than occupying one of the ten slots.
        # Their result stays blank, which is what the caller expects.
        enrichable = [
            (index, person)
            for index, person in enumerate(people)
            if person.get("first_name") and person.get("last_name")
        ]

        for start in range(0, len(enrichable), self._BULK_ENRICH_BATCH):
            batch = enrichable[start:start + self._BULK_ENRICH_BATCH]
            details = []
            for _, person in batch:
                detail = {
                    "first_name": person.get("first_name", ""),
                    "last_name": person.get("last_name", ""),
                }
                org_name = (person.get("organization") or {}).get("name", "")
                if org_name:
                    detail["organization_name"] = org_name
                details.append(detail)

            try:
                with httpx.Client(timeout=30) as client:
                    response = client.post(
                        self.bulk_match_url,
                        headers=headers,
                        json={"details": details, "reveal_personal_emails": True},
                    )
                    response.raise_for_status()
                    data = response.json()
            except Exception as e:
                print(f"Apollo bulk enrichment failed for a batch of {len(batch)}: {e}")
                continue

            # Apollo returns `matches` positionally against `details`, with a
            # null entry where it could not match someone.
            matches = data.get("matches") or []
            consumed = data.get("credits_consumed")
            if consumed is not None:
                print(f"Apollo bulk enrichment: {consumed} credit(s) consumed for {len(batch)} person(s).")

            for (original_index, _), matched in zip(batch, matches):
                if not matched:
                    continue
                # match_confidence is "high" or "none"; a "none" match is
                # Apollo saying it guessed at WHO this is, so the address
                # cannot be trusted no matter what status it carries.
                if (matched.get("match_confidence") or "").lower() == "none":
                    continue
                results[original_index] = {
                    "email": matched.get("email") or "",
                    "status": (matched.get("email_status") or "").lower(),
                }

        return results

    @staticmethod
    def _explain_http_error(status: int, body: str) -> str:
        """
        Turn an Apollo HTTP status into something the person running the
        search can act on. The raw status alone ("502") tells them nothing
        about whether to fix a key, upgrade a plan, or just try later.
        """
        if status == 401:
            return (
                "Apollo rejected the API key. Generate a new one in Apollo "
                "(Settings > Integrations > API) and update APOLLO_API_KEY in "
                "Railway's Variables tab."
            )
        if status == 403:
            return (
                "Apollo accepted the key but refused this endpoint. People "
                "Search is usually not available on the free plan, so this "
                "normally means the plan needs upgrading rather than the key "
                "being wrong."
            )
        if status == 422:
            return f"Apollo rejected the search filters as invalid: {body[:200]}"
        if status == 429:
            return "Apollo rate limited the request. Wait a few minutes and try again."
        return f"Apollo returned HTTP {status}: {body[:200]}"

    def count_people_at_domains(self, domains: list[str]) -> int | None:
        """
        How many people Apollo holds at *domains*, with no role filter at all.

        A diagnostic, not a lead source. When a district search matches zero
        people there are two completely different causes and the user cannot
        tell them apart: Apollo may hold no record of those companies, or it
        may hold plenty of people none of whom clear the seniority filter.
        The first is a coverage limit with nothing to fix; the second is our
        filter being too tight. One unfiltered call settles it.

        Returns None if the question could not be answered (no key, Apollo
        refused). It must never turn a merely-empty result into a failed
        request, so nothing here raises.
        """
        domains = [d for d in (domains or []) if d]
        if not self.api_key or not domains:
            return None

        headers = {
            "Cache-Control": "no-cache",
            "Content-Type": "application/json",
            "X-Api-Key": self.api_key,
        }
        payload = {
            "q_organization_domains_list": domains[:1000],
            "page": 1,
            # Only the count is wanted, so ask for the smallest page Apollo
            # will return and read the total off the pagination block.
            "per_page": 1,
        }
        try:
            with httpx.Client(timeout=30) as client:
                response = client.post(self.base_url, headers=headers, json=payload)
                response.raise_for_status()
                data = response.json()
        except Exception as e:
            print(f"Apollo coverage probe failed: {e}")
            return None

        pagination = data.get("pagination") or {}
        total = pagination.get("total_entries")
        if isinstance(total, int):
            return total
        return len(data.get("people") or [])

    def scrape(self, niche: str, limit: int = 50, city: str = "", domains: list[str] | None = None) -> list[dict]:
        """
        Search Apollo for decision makers in a specific niche.

        *city*, when given, narrows the search to companies headquartered
        there instead of anywhere in India. Apollo's organization_locations
        filter accepts cities, US states and countries but NOT
        neighbourhoods or postal codes (confirmed against
        docs.apollo.io/reference/people-api-search), so this is as tight as
        Apollo can be aimed — "mumbai", never "bandra kurla complex". An
        area-level search belongs to the Google Places path
        (GoogleMapsScraper.scrape_area), which is boxed by real coordinates.

        *domains*, when given, is how an area-level search is actually
        served. Apollo is asked for decision makers at those exact companies
        via q_organization_domains_list (up to 1000 per request, confirmed
        against the docs) instead of being asked to guess a geography it
        cannot express. The caller gets the domains from
        GoogleMapsScraper.scrape_area, which boxes a real district on the
        map — so every match is genuinely inside that district rather than
        merely somewhere in the same city. Added 2026-09-27 on request
        ("i just want that apollo searches gives me leads if i type bkc").

        Raises ApolloError if Apollo refuses the request. An empty list from
        this method now means exactly one thing: Apollo answered, and nobody
        matched.
        """
        if not self.api_key:
            print("APOLLO_API_KEY is not set in .env. Skipping Apollo scraper.")
            return []
            
        domains = [d for d in (domains or []) if d]
        if domains:
            print(f"Scraping Apollo (Free API) for decision makers at {len(domains)} specific companies...")
        else:
            print(f"Scraping Apollo (Free API) for: {niche} in {city or 'india (country-wide)'}...")
        leads = []
        
        headers = {
            "Cache-Control": "no-cache",
            "Content-Type": "application/json",
            "X-Api-Key": self.api_key
        }
        
        # Searching for founders/CEOs in the given niche.
        #
        # Narrowed 2026-09-16 ("i want good leads i can convert into
        # clients") — an unfiltered keyword search returns any company size
        # anywhere, which meant a keyword like "Digital Marketing Agency"
        # could just as easily surface a 2000-person multinational's CMO as
        # a two-person local shop's owner. This whole pipeline's pitch is
        # "here are specific problems with YOUR website" backed by a real
        # audit — that lands with a small business owner who can act on it
        # personally, not with an enterprise that already has a marketing
        # department and a professionally built site. Three real Apollo
        # filters (confirmed against docs.apollo.io/reference/people-api-search,
        # not guessed) now bias results toward that profile:
        #   - person_seniorities: actual decision-makers, not e.g. a junior
        #     marketing coordinator who happens to carry a matched title.
        #   - organization_num_employees_ranges: small businesses only —
        #     this is who can approve a small agency's pitch on their own
        #     say-so, and who plausibly still has real website problems.
        #   - organization_locations: India by default — matches this tool's
        #     actual market everywhere else (MCA lookup, IndiaMART/
        #     TradeIndia, Maharashtra government data, IST-scheduled sends).
        # The seniority and company-size filters are deliberately hardcoded,
        # not exposed as search options — if a genuinely different
        # market/company-size is ever needed, widen this rather than
        # silently drift back to the unfiltered version.
        #
        # Location is the one that IS caller-controlled, added 2026-09-25.
        # Until then it was always ["india"], which meant every Apollo
        # search swept the entire country: asking for "Digital Marketing
        # Agency" returned owners in Kochi and Guwahati with equal weight to
        # the city actually being prospected. A city narrows it; blank keeps
        # the old country-wide behaviour so every existing caller
        # (scheduler.py's b2b job, the tests) is unaffected.
        payload = {
            "page": 1,
            "per_page": min(limit, 100) # Apollo limit per page
        }

        # contact_email_status is applied on BOTH branches below (added
        # 2026-09-28). Apollo hands back people it holds no confirmed
        # address for unless told otherwise, and each of those costs a
        # wasted enrichment attempt and yields a lead this pipeline cannot
        # use, since its entire job is sending cold email. Asking for
        # verified only at search time deliberately narrows the result count
        # in exchange for results that can actually be mailed.
        if domains:
            # Targeting named companies, so the geography and keyword filters
            # are not just unnecessary, they are actively harmful: the caller
            # already decided which companies count by picking them off a map.
            # Apollo capped at 1000 domains per request.
            payload["q_organization_domains_list"] = domains[:1000]
            # The employee-count filter is dropped here on purpose. It exists
            # to stop a blind keyword search dragging in enterprises, but
            # these companies were hand-picked from one district, and
            # silently discarding a match because Apollo thinks it has 60
            # staff would throw away a lead the user explicitly asked for.
            #
            # person_titles is dropped too, and person_seniorities widened,
            # after a live BKC search found 60 companies and matched 0 people
            # (2026-09-27). Apollo combines titles and seniorities with AND,
            # not OR (confirmed in the docs), so the old pairing demanded a
            # literal title from {founder, ceo, owner, cmo, marketing} AND a
            # seniority from {owner, founder, c_suite}. An Indian SME is
            # typically run by a "Director", "Partner", "Proprietor" or
            # "Managing Director" — none of those words are in the title
            # list, so every one of them was discarded. Seniority alone
            # already expresses "decision maker", which is all this needs,
            # and director/partner are real Apollo enum values.
            payload["person_seniorities"] = [
                "owner", "founder", "c_suite", "partner", "director",
            ]
            payload["contact_email_status"] = ["verified"]
            if niche and niche.strip():
                payload["q_keywords"] = niche.strip()
        else:
            payload["q_keywords"] = niche
            payload["person_titles"] = ["founder", "ceo", "owner", "cmo", "marketing"]
            payload["person_seniorities"] = ["owner", "founder", "c_suite"]
            payload["organization_num_employees_ranges"] = ["1,10", "11,50"]
            payload["organization_locations"] = (
                [city.strip().lower()] if city and city.strip() else ["india"]
            )
            payload["contact_email_status"] = ["verified"]

        for attempt in range(3):
            try:
                with httpx.Client(timeout=30) as client:
                    response = client.post(self.base_url, headers=headers, json=payload)
                    response.raise_for_status()
                    data = response.json()
                    break # Success
                    
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status == 429 and attempt < 2:
                    print(f"Apollo API Rate Limit hit (429). Sleeping 60s...")
                    time.sleep(60)
                    continue
                body = ""
                try:
                    body = e.response.text
                except Exception:
                    pass
                print(f"Apollo API HTTP error {status}: {body[:500]}")
                raise ApolloError(self._explain_http_error(status, body)) from e
            except Exception as e:
                print(f"Error scraping Apollo: {e}")
                raise ApolloError(f"Could not reach Apollo: {e}") from e
        else:
            # Every attempt was a 429 that we slept through.
            raise ApolloError(
                "Apollo rate limited every attempt. Wait a few minutes and try again."
            )


        # Two passes rather than enriching inside the loop: the bulk
        # endpoint takes 10 people at a time, so the keepers have to be
        # collected before any enrichment can happen.
        keepers = []
        skipped_no_company = 0
        skipped_no_email_on_file = 0
        for person in data.get("people", []):
            org = person.get("organization") or {}
            if not org.get("name") or not org.get("website_url"):
                # Discarded either way, so it must not reach enrichment.
                skipped_no_company += 1
                continue
            # has_email is Apollo telling us, for free in the search
            # response, whether it holds a verified address for this person.
            # Explicitly False means enrichment cannot return one, so the
            # call is pure latency and rate-limit budget. Absent/None is
            # "unknown" and still gets tried, the same way a missing review
            # count is treated as unknown rather than zero elsewhere.
            if person.get("has_email") is False:
                skipped_no_email_on_file += 1
                continue
            keepers.append(person)

        enrichments = self._enrich_emails(keepers)

        unverified = 0
        for person, enriched in zip(keepers, enrichments):
            org = person.get("organization") or {}
            email = enriched["email"]
            status = enriched["status"]
            # Only a verified address goes in "Email", because that is the
            # field the send path reads. A guessed one is still surfaced so
            # the lead is not silently poorer than it is, but it sits in a
            # field nothing can auto-send to.
            sendable = email if status == self._SENDABLE_EMAIL_STATUS else ""
            if email and not sendable:
                unverified += 1

            leads.append({
                "Company": org.get("name", ""),
                "Website": org.get("website_url", ""),
                "Phone": (org.get("primary_phone") or {}).get("number", ""),
                "Address": org.get("city", ""),
                "Rating": "Apollo B2B",
                "Reviews Count": org.get("estimated_num_employees", 0),
                "Email": sendable,
                "Unverified Email": "" if sendable else email,
                "Email Status": status,
                "Instagram Handle": "",
                "Decision Maker Name": f"{person.get('first_name', '')} {person.get('last_name', '')}".strip(),
                "Source": "Apollo Free API"
            })

        print(
            f"Apollo: {len(leads)} lead(s), "
            f"{sum(1 for lead in leads if lead['Email'])} with a verified email, "
            f"{unverified} with an unverified one held back from sending "
            f"({skipped_no_company} skipped with no company/website, "
            f"{skipped_no_email_on_file} skipped because Apollo holds no email for them)."
        )
        return leads
