"""
B2B Directory Dork Scrapers — use DuckDuckGo to find supplier listings on
India's major B2B directories for $0, the same site: dork pattern as
StartupDorkScraper.

Built for sectors (agriculture especially) where a lot of dealers/suppliers
list on a directory instead of running their own site. Leads without a real
external website still come through with a blank Website field (same
convention StartupDorkScraper uses) rather than pointing "Website" at the
directory listing page itself — auditing IndiaMART's/TradeIndia's own site
would be measuring the wrong business entirely.

`B2BDirectoryDorkScraper` holds the shared dork/parse/retry logic;
`IndiaMartDorkScraper` (the original, kept as its own name since app.py and
scheduler.py already import it directly), `TradeIndiaDorkScraper` and
`ExportersIndiaDorkScraper` (added 2026-08-17, both confirmed live to carry
real agri-equipment listings) are one-line subclasses naming their domain.
"""

import time
import random
from ddgs import DDGS


class B2BDirectoryDorkScraper:
    domain = ""       # e.g. "indiamart.com" — set by subclasses
    source_label = ""  # e.g. "IndiaMART Dork" — set by subclasses

    def __init__(self):
        self.delay_min = 2
        self.delay_max = 5

    def scrape(self, niche: str, city: str = "", limit: int = 20) -> list[dict]:
        """
        Search DuckDuckGo for this directory's listings in the given niche
        (optionally narrowed to a city).
        """
        query = f'site:{self.domain} "{niche}"' + (f' "{city}"' if city else "")
        print(f"Scraping {self.domain} via DuckDuckGo for: {niche}{f' in {city}' if city else ''}...")
        leads = []

        try:
            results = []
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

            seen_companies = set()
            for result in results:
                url = result.get('href', '')
                title = result.get('title', '')

                if self.domain not in url:
                    continue

                # Directory titles are typically "Company Name - Product/Service"
                # or "Product | Company Name". Either way the company name is
                # the longer, more distinctive side — take the first segment
                # since that's the seller name far more often than not.
                company_name = title.split("|")[0].split(" - ")[0].strip()
                if not company_name or company_name.lower() in seen_companies:
                    continue
                seen_companies.add(company_name.lower())

                real_website = self._find_real_website(company_name)

                leads.append({
                    "Company": company_name,
                    "Website": real_website,
                    "Phone": "",
                    "Address": city,
                    "Rating": "",
                    "Reviews Count": 0,
                    "Email": "",
                    "Instagram Handle": "",
                    "Decision Maker Name": "",
                    "Source": self.source_label,
                })

            time.sleep(random.uniform(self.delay_min, self.delay_max))

        except Exception as e:
            print(f"Error scraping {self.domain} dorks: {e}")

        return leads

    def _find_real_website(self, company_name: str) -> str:
        """Helper to find the supplier's actual website, off the directory."""
        for attempt in range(3):
            try:
                with DDGS() as ddgs:
                    results = list(ddgs.text(f'{company_name} official website -site:{self.domain}', max_results=2))
                    if results:
                        return results[0].get('href', '')
                    break
            except Exception as e:
                if "Ratelimit" in str(e) or "rate limit" in str(e).lower() or attempt < 2:
                    time.sleep(15 * (2 ** attempt))
                else:
                    break
        return ""


class IndiaMartDorkScraper(B2BDirectoryDorkScraper):
    domain = "indiamart.com"
    source_label = "IndiaMART Dork"


class TradeIndiaDorkScraper(B2BDirectoryDorkScraper):
    domain = "tradeindia.com"
    source_label = "TradeIndia Dork"


class ExportersIndiaDorkScraper(B2BDirectoryDorkScraper):
    domain = "exportersindia.com"
    source_label = "ExportersIndia Dork"
