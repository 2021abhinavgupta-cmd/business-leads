import asyncio
import random
import time
from urllib.parse import urlparse
import httpx
from playwright.async_api import async_playwright
from ddgs import DDGS

import config
from analyzer.visuals import _PLAYWRIGHT_SEMAPHORE

TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
NEARBY_SEARCH_URL = "https://places.googleapis.com/v1/places:searchNearby"
_REQUEST_DELAY = 2  # seconds

# Place types that are returned by a no-keyword nearby search but are never
# a sellable lead — infrastructure, transit, civic amenities and the like.
# Without this, a search around a city centre comes back full of train
# stations and corporate parks (live-observed at Mumbai BKC) instead of
# businesses with a website worth auditing.
_NON_BUSINESS_TYPES = {
    "train_station", "subway_station", "bus_station", "transit_station",
    "light_rail_station", "airport", "parking", "bus_stop",
    "city_hall", "courthouse", "embassy", "fire_station", "police",
    "local_government_office", "post_office", "cemetery",
    "park", "national_park", "tourist_attraction", "historical_landmark",
    "place_of_worship", "church", "hindu_temple", "mosque", "synagogue",
    "school", "primary_school", "secondary_school", "university",
    "hospital", "atm", "bank",
}

# Place types queried one-per-request by scrape_nearby. Each gets its own
# 20-result budget from the API, which is the only way to get volume out of
# an endpoint that can't paginate. Ordered by how likely the category is to
# have both a website and an owner who'd act on an audit — service
# businesses first, retail later.
_NEARBY_TYPE_ROTATION = [
    "dentist", "beauty_salon", "hair_salon", "spa", "gym",
    "real_estate_agency", "lawyer", "accounting", "insurance_agency",
    "restaurant", "cafe", "bakery", "hotel",
    "car_repair", "car_dealer", "moving_company", "storage",
    "electrician", "plumber", "painter", "roofing_contractor",
    "general_contractor", "veterinary_care", "physiotherapist",
    "travel_agency", "florist", "furniture_store", "jewelry_store",
    "clothing_store", "shoe_store", "pet_store", "book_store",
    "electronics_store", "home_goods_store", "hardware_store",
    "photographer", "consultant", "advertising_agency",
]

class GoogleMapsScraper:
    def __init__(self):
        self.api_key = config.GOOGLE_MAPS_API_KEY
        self.client = httpx.Client(timeout=30)

    async def scrape_google_maps(self, niche: str, city: str, limit: int = 20) -> list[dict]:
        """
        Hybrid Scraper Architecture:
        1. Attempts to use the ultra-fast, reliable Google Places API (which provides a $200 free tier).
        2. If the API fails (e.g. limit exceeded, no billing account), gracefully falls back 
           to the 100% free Playwright + OSINT scraper.
        """
        print(f"[Maps] Attempting official API scrape for {niche} in {city}...")
        try:
            leads = self._scrape_via_api(niche, city, limit)
            if leads:
                print(f"[Maps] API successful! Found {len(leads)} leads.")
                return leads
        except Exception as e:
            print(f"[Maps] API failed or blocked: {e}")
            
        print("[Maps] Falling back to free Playwright OSINT scraper...")
        return await self._scrape_via_playwright(niche, city, limit)

    # ---------------------------------------------------------
    # STRATEGY 1: Official Google Places API (Fast, Reliable)
    # ---------------------------------------------------------
    def _scrape_via_api(self, niche: str, city: str, limit: int) -> list[dict]:
        if not self.api_key:
            raise ValueError("No API Key provided")
            
        leads = []
        headers = {
            "X-Goog-Api-Key": self.api_key,
            # `nextPageToken` MUST be listed here. In the Places API (New),
            # the field mask controls the whole response body, not just the
            # per-place fields — omitting it means the API never returns a
            # token, so the pagination loop below always broke after page 1.
            # Live-verified 2026-07-31: without it the response's only
            # top-level key is "places"; with it, "nextPageToken" appears
            # and page 2+ fetch correctly. This capped every search at one
            # 20-result page, which after the has-a-website filter and
            # domain dedupe below is why asking for 10 leads returned ~5.
            "X-Goog-FieldMask": "places.displayName,places.websiteUri,places.nationalPhoneNumber,places.formattedAddress,places.rating,places.userRatingCount,nextPageToken",
            "Content-Type": "application/json"
        }
        payload = {
            "textQuery": f"{niche} in {city}",
            "pageSize": 20
        }
        
        # Now that pagination actually works (see the field-mask note above),
        # this loop needs a hard page bound: `len(leads) < limit` alone can
        # keep requesting pages when few results per page have a website,
        # and every page is a billable API call. Google's Text Search caps
        # at ~60 results (3 pages of 20) anyway, so this mostly just
        # guarantees termination rather than restricting real results.
        _MAX_PAGES = 3
        pages_fetched = 0

        while len(leads) < limit and pages_fetched < _MAX_PAGES:
            time.sleep(_REQUEST_DELAY)
            response = self.client.post(TEXT_SEARCH_URL, headers=headers, json=payload)
            response.raise_for_status() # Will raise Exception if 403 (Billing issues)
            data = response.json()
            pages_fetched += 1

            places = data.get("places", [])
            if not places:
                break
                
            for place in places:
                name = place.get("displayName", {}).get("text", "")
                website = place.get("websiteUri", "")
                
                if not name or not website:
                    continue
                    
                leads.append({
                    "Company": name,
                    "Website": website,
                    "Phone": place.get("nationalPhoneNumber", ""),
                    "Address": place.get("formattedAddress", ""),
                    "Rating": str(place.get("rating", "")),
                    "Reviews Count": place.get("userRatingCount", 0),
                    "Email": "",
                    "Instagram Handle": "",
                    "Decision Maker Name": "",
                    "Source": "Google Maps (API)"
                })
                
            next_token = data.get("nextPageToken")
            if not next_token:
                break
            payload["pageToken"] = next_token

        deduped = self._deduplicate(leads)[:limit]
        # Two filters legitimately shrink the result set below `limit`:
        # businesses with no website at all (skipped above — there's nothing
        # to audit), and multiple branches of one chain sharing a domain
        # (collapsed by _deduplicate). Asking for 10 and getting 5 is
        # usually this, not a failure — but silently returning fewer than
        # requested with no explanation looks like a bug from the UI.
        if len(deduped) < limit:
            print(f"[Maps API] Returning {len(deduped)} of {limit} requested leads for '{niche} in {city}' — {pages_fetched} page(s) fetched, {len(leads)} listing(s) had a website, {len(leads) - len(deduped)} dropped as duplicate domains (chain branches). Listings with no website are skipped since there's nothing to audit.")
        return deduped

    # ---------------------------------------------------------
    # Nearby search — "everything around this point", no niche
    # ---------------------------------------------------------
    async def scrape_nearby(
        self,
        latitude: float,
        longitude: float,
        radius_m: int = 3000,
        limit: int = 20,
    ) -> list[dict]:
        """
        Find businesses of every type within *radius_m* of a coordinate.

        Different endpoint from scrape_google_maps: searchText needs a
        keyword ("dentist in Mumbai"), while searchNearby takes a circle and
        returns whatever is inside it. That's what makes an unfiltered
        "everything near me" search possible at all — there's no keyword
        that means "any business".

        No Playwright fallback here, deliberately: the OSINT path is built
        around scraping a text query off a Maps search page and has no
        equivalent for a radius search, so this degrades to an empty list
        rather than silently returning unrelated results.

        Searches one business type at a time rather than once for
        everything. A single unfiltered call returns at most 20 places and
        cannot paginate (unlike searchText), and the places physically
        nearest a point skew heavily to small shops with no website —
        live-observed 20 places around Mumbai BKC yielding only 4 auditable
        leads. Widening the radius does not help, because DISTANCE ranking
        returns the same nearest 20 no matter how big the circle is
        (verified: 2km, 6km and 18km all returned an identical four).
        Giving each type its own request is the only way to get real volume,
        and it also spreads results across industries instead of returning
        whichever category happens to cluster around the caller.
        """
        if not self.api_key:
            print("[Maps Nearby] No GOOGLE_MAPS_API_KEY set — cannot run a nearby search.")
            return []

        radius = min(radius_m, 50000)
        collected: dict[str, dict] = {}

        for place_type in _NEARBY_TYPE_ROTATION:
            batch = await self._nearby_once(latitude, longitude, radius, place_type)
            for lead in batch:
                # Keyed by domain so a business listed under two types, or
                # re-found by a later query, is only counted once.
                collected.setdefault(self._extract_domain(lead["Website"]), lead)

            if len(collected) >= limit:
                break
            # Same jittered pacing as the rest of this scraper.
            await asyncio.sleep(random.uniform(0.3, 0.9))

        leads = list(collected.values())[:limit]
        print(f"[Maps Nearby] Returning {len(leads)} lead(s) within {radius}m of ({latitude:.4f}, {longitude:.4f}).")
        return leads

    async def _nearby_once(
        self, latitude: float, longitude: float, radius_m: int, place_type: str | None = None
    ) -> list[dict]:
        """One searchNearby call, optionally restricted to a single place type."""
        headers = {
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": (
                "places.displayName,places.websiteUri,places.nationalPhoneNumber,"
                "places.formattedAddress,places.rating,places.userRatingCount,"
                "places.primaryTypeDisplayName,places.types"
            ),
            "Content-Type": "application/json",
        }
        payload = {
            "locationRestriction": {
                "circle": {
                    "center": {"latitude": latitude, "longitude": longitude},
                    # Google rejects anything above 50km.
                    "radius": float(min(radius_m, 50000)),
                }
            },
            # 20 is the API's per-request maximum, and searchNearby has no
            # pagination at all (unlike searchText) — so this endpoint can
            # never return more than 20 raw results per call, before the
            # has-a-website and non-business filters below cut it further.
            "maxResultCount": 20,
            "rankPreference": "DISTANCE",
        }
        if place_type:
            payload["includedTypes"] = [place_type]

        try:
            response = await asyncio.to_thread(
                self.client.post, NEARBY_SEARCH_URL, headers=headers, json=payload
            )
            response.raise_for_status()
            places = response.json().get("places", [])
        except Exception as e:
            print(f"[Maps Nearby] Search failed: {e}")
            return []

        leads = []
        skipped_non_business = 0
        for place in places:
            name = place.get("displayName", {}).get("text", "")
            website = place.get("websiteUri", "")
            if not name or not website:
                continue

            if set(place.get("types", [])) & _NON_BUSINESS_TYPES:
                skipped_non_business += 1
                continue

            leads.append({
                "Company": name,
                "Website": website,
                "Phone": place.get("nationalPhoneNumber", ""),
                "Address": place.get("formattedAddress", ""),
                "Rating": str(place.get("rating", "")),
                "Reviews Count": place.get("userRatingCount", 0),
                "Category": (place.get("primaryTypeDisplayName") or {}).get("text", ""),
                "Email": "",
                "Instagram Handle": "",
                "Decision Maker Name": "",
                "Source": "Google Maps (Nearby)",
            })

        deduped = self._deduplicate(leads)
        print(
            f"[Maps Nearby] {len(places)} place(s) within {radius_m}m -> {len(deduped)} lead(s) "
            f"({skipped_non_business} skipped as non-business, rest had no website or a duplicate domain)."
        )
        return deduped

    # ---------------------------------------------------------
    # STRATEGY 2: Playwright + DDGS OSINT (100% Free, Slower)
    # ---------------------------------------------------------
    async def _scrape_via_playwright(self, niche: str, city: str, limit: int) -> list[dict]:
        leads = []
        query = f"{niche} in {city}"
        url = f"https://www.google.com/maps/search/{query.replace(' ', '+')}"
        
        names = []
        async with _PLAYWRIGHT_SEMAPHORE:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=['--no-sandbox', '--disable-dev-shm-usage']
                )
                page = await browser.new_page()
                
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(random.uniform(2.5, 4.5)) # Wait for initial results (jittered)

                    # Scroll down the feed a few times to load more leads
                    for _ in range(limit // 5):
                        try:
                            await page.hover('a[href*="/maps/place/"]')
                            await page.mouse.wheel(0, 2000)
                            await asyncio.sleep(random.uniform(1.2, 2.5)) # jittered, anti-detection
                        except Exception:
                            break
                            
                    # Extract unique names from aria-labels
                    names = await page.evaluate("""() => {
                        const items = Array.from(document.querySelectorAll('a[href*="/maps/place/"]'));
                        return [...new Set(items.map(a => a.getAttribute('aria-label')).filter(Boolean))];
                    }""")
                    
                except Exception as e:
                    print(f"Playwright Maps Error: {e}")
                finally:
                    await browser.close()
                    
        names = names[:limit]
        
        # Resolve domains using DDG (100% Free OSINT). Jittered delay between
        # lookups so a big batch doesn't hammer DDG in a tight loop.
        for i, name in enumerate(names):
            if i > 0:
                await asyncio.sleep(random.uniform(1.0, 2.0))
            # _find_website_for_business is sync (blocking DDGS network call);
            # thread it so it doesn't stall the event loop for other requests.
            website = await asyncio.to_thread(self._find_website_for_business, name, city)
            if not website:
                continue
                
            leads.append({
                "Company": name,
                "Website": website,
                "Phone": "", # Website scraper will find this natively
                "Address": city,
                "Rating": "N/A", 
                "Reviews Count": 0,
                "Email": "",
                "Instagram Handle": "",
                "Decision Maker Name": "",
                "Source": "Google Maps (Playwright OSINT Fallback)"
            })
            
        return self._deduplicate(leads)

    def _find_website_for_business(self, company_name: str, city: str) -> str:
        """Use DDG lite to find the official website."""
        query = f'"{company_name}" "{city}" official website'
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=3, backend="lite"))
                for res in results:
                    href = res.get("href", "").lower()
                    if any(x in href for x in ["yelp.com", "facebook.com", "instagram.com", "linkedin.com", "justdial", "yellowpages"]):
                        continue
                    return res.get("href", "")
        except Exception:
            pass
        return ""

    def _deduplicate(self, leads: list[dict]) -> list[dict]:
        seen_domains = set()
        unique = []
        for lead in leads:
            domain = self._extract_domain(lead.get("Website", ""))
            if domain and domain not in seen_domains:
                seen_domains.add(domain)
                unique.append(lead)
        return unique

    def _extract_domain(self, url: str) -> str:
        if not url: return ""
        if not url.startswith(("http://", "https://")):
            url = "http://" + url
        try:
            netloc = urlparse(url).netloc.lower()
            return netloc.replace("www.", "")
        except Exception:
            return ""
