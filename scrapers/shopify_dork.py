"""
Shopify Dork Scraper — Uses DuckDuckGo to find e-commerce stores for $0.
"""

import time
import random
from duckduckgo_search import DDGS

class ShopifyDorkScraper:
    def __init__(self):
        self.delay_min = 2
        self.delay_max = 5

    def scrape(self, niche: str, limit: int = 50) -> list[dict]:
        """
        Search DuckDuckGo for Shopify stores in the given niche.
        """
        print(f"Scraping Shopify via DuckDuckGo for: {niche}...")
        leads = []
        
        # We dork the myshopify domain to find hidden backend store links
        query = f'site:myshopify.com "{niche}"'
        
        try:
            for attempt in range(3):
                try:
                    with DDGS() as ddgs:
                        results = list(ddgs.text(query, max_results=limit))
                        break # Success
                except Exception as e:
                    if "Ratelimit" in str(e) or "rate limit" in str(e).lower() or attempt < 2:
                        sleep_time = 15 * (2 ** attempt) # 15, 30, 60s
                        print(f"DDG Rate limit hit or error. Sleeping {sleep_time}s... ({e})")
                        time.sleep(sleep_time)
                    else:
                        raise e
                
            for result in results:
                # E.g., href: https://some-store.myshopify.com
                url = result.get('href', '')
                title = result.get('title', '')
                body = result.get('body', '')
                
                if not url.endswith('myshopify.com') and not url.endswith('myshopify.com/'):
                    # Some results might be deeper links, try to extract base
                    import urllib.parse
                    parsed = urllib.parse.urlparse(url)
                    url = f"https://{parsed.netloc}"

                # Try to extract the real brand name
                company_name = title.replace("-", "|").split("|")[0].strip()
                if "myshopify.com" in company_name.lower():
                    # Fallback to subdomain name
                    parsed = urllib.parse.urlparse(url)
                    company_name = parsed.netloc.replace('.myshopify.com', '').replace('-', ' ').title()

                leads.append({
                    "Company": company_name,
                    "Website": url,
                    "Phone": "",
                    "Address": "",
                    "Rating": "",
                    "Reviews Count": 0,
                    "Email": "", # To be enriched by DecisionMaker
                    "Instagram Handle": "",
                    "Decision Maker Name": "",
                    "Source": "DuckDuckGo Shopify Dork"
                })
                
            time.sleep(random.uniform(self.delay_min, self.delay_max))
                
        except Exception as e:
            print(f"Error scraping Shopify dorks: {e}")
            
        return leads
