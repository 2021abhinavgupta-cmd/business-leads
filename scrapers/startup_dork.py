"""
Startup Dork Scraper — Uses DuckDuckGo to find Y-Combinator startups for $0.
"""

import time
import random
import urllib.parse
from duckduckgo_search import DDGS

class StartupDorkScraper:
    def __init__(self):
        self.delay_min = 2
        self.delay_max = 5

    def scrape(self, niche: str, limit: int = 50) -> list[dict]:
        """
        Search DuckDuckGo for YC startups in the given niche.
        """
        print(f"Scraping YC Startups via DuckDuckGo for: {niche}...")
        leads = []
        
        # Dork YCombinator's company directory
        query = f'site:ycombinator.com/companies "{niche}"'
        
        try:
            for attempt in range(3):
                try:
                    with DDGS() as ddgs:
                        results = list(ddgs.text(query, max_results=limit))
                        break
                except Exception as e:
                    if "Ratelimit" in str(e) or "rate limit" in str(e).lower() or attempt < 2:
                        sleep_time = 15 * (2 ** attempt)
                        print(f"DDG Rate limit hit. Sleeping {sleep_time}s... ({e})")
                        time.sleep(sleep_time)
                    else:
                        raise e
                
            for result in results:
                url = result.get('href', '')
                title = result.get('title', '')
                body = result.get('body', '')
                
                if "/companies/" not in url:
                    continue

                # Title is usually "Company Name | Y Combinator"
                company_name = title.split("|")[0].strip()
                
                # We don't get the direct website from DDG results easily without clicking in,
                # but we can guess the domain or leave it empty for the enrichment step to figure out.
                # Actually, YC company descriptions often have the URL. Let's use DDG to find the real site.
                real_website = self._find_real_website(company_name)

                leads.append({
                    "Company": company_name,
                    "Website": real_website,
                    "Phone": "",
                    "Address": "",
                    "Rating": "YC Funded",
                    "Reviews Count": 0,
                    "Email": "",
                    "Instagram Handle": "",
                    "Decision Maker Name": "",
                    "Source": "DuckDuckGo YC Startup Dork"
                })
                
            time.sleep(random.uniform(self.delay_min, self.delay_max))
                
        except Exception as e:
            print(f"Error scraping Startup dorks: {e}")
            
        return leads

    def _find_real_website(self, company_name: str) -> str:
        """Helper to find the startup's actual website."""
        for attempt in range(3):
            try:
                with DDGS() as ddgs:
                    # Exclude ycombinator from the search
                    results = list(ddgs.text(f'{company_name} startup -site:ycombinator.com', max_results=2))
                    if results:
                        return results[0].get('href', '')
                    break
            except Exception as e:
                if "Ratelimit" in str(e) or "rate limit" in str(e).lower() or attempt < 2:
                    time.sleep(15 * (2 ** attempt))
                else:
                    break
        return ""
