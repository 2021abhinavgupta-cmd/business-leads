"""
Google Maps scraper — discovers businesses via the Google Places API (New).
"""
import asyncio
from urllib.parse import urlparse
from playwright.async_api import async_playwright
from ddgs import DDGS
from analyzer.visuals import _PLAYWRIGHT_SEMAPHORE # Limit concurrency

class GoogleMapsScraper:
    def __init__(self):
        pass

    async def scrape_google_maps(self, niche: str, city: str, limit: int = 20) -> list[dict]:
        """
        Scrapes Google Maps entirely for free using Playwright.
        Replaces the $0.032/query paid API with a stealthy browser scraper.
        """
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
                    await asyncio.sleep(3) # Wait for initial results
                    
                    # Scroll down the feed a few times to load more leads
                    for _ in range(limit // 5):
                        try:
                            await page.hover('a[href*="/maps/place/"]')
                            await page.mouse.wheel(0, 2000)
                            await asyncio.sleep(1.5)
                        except:
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
                    
        # Limit to requested amount
        names = names[:limit]
        
        # Resolve domains using DDG (100% Free OSINT)
        for name in names:
            website = self._find_website_for_business(name, city)
            
            # If we couldn't resolve a website, skip the lead (we need a website for the AI Audit)
            if not website:
                continue
                
            leads.append({
                "Company": name,
                "Website": website,
                "Phone": "", # Website scraper will find this natively
                "Address": city,
                "Rating": "N/A", # Hard to scrape without clicking, saving memory by skipping
                "Reviews Count": 0,
                "Email": "",
                "Instagram Handle": "",
                "Decision Maker Name": "",
                "Source": "Google Maps (Playwright OSINT)"
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
                    # Skip generic directories
                    if any(x in href for x in ["yelp.com", "facebook.com", "instagram.com", "linkedin.com", "justdial", "yellowpages"]):
                        continue
                    return res.get("href", "")
        except:
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
