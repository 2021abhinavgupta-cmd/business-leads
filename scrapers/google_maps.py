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
_REQUEST_DELAY = 2  # seconds

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
            "X-Goog-FieldMask": "places.displayName,places.websiteUri,places.nationalPhoneNumber,places.formattedAddress,places.rating,places.userRatingCount",
            "Content-Type": "application/json"
        }
        payload = {
            "textQuery": f"{niche} in {city}",
            "pageSize": 20
        }
        
        while len(leads) < limit:
            time.sleep(_REQUEST_DELAY)
            response = self.client.post(TEXT_SEARCH_URL, headers=headers, json=payload)
            response.raise_for_status() # Will raise Exception if 403 (Billing issues)
            data = response.json()
                
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
            
        return self._deduplicate(leads)[:limit]

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
