"""
Apollo Free Scraper — Uses the Apollo.io REST API (Free Tier).
"""

import httpx
import time
import config

class ApolloFreeScraper:
    def __init__(self):
        self.api_key = config.APOLLO_API_KEY
        self.base_url = "https://api.apollo.io/v1/mixed_people/api_search"

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
        for person in people:
            org = person.get("organization", {})
            
            company_name = org.get("name", "")
            website = org.get("website_url", "")
            first_name = person.get("first_name", "")
            last_name = person.get("last_name", "")
            email = person.get("email", "")
            
            if not company_name or not website:
                continue

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
            
        return leads
