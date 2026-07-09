"""
Google Maps scraper — discovers businesses via the Google Places API (New).
"""
import time
from urllib.parse import urlparse
import httpx
import config

TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
_REQUEST_DELAY = 2  # seconds

class GoogleMapsScraper:
    def __init__(self):
        self.api_key = config.GOOGLE_MAPS_API_KEY
        self.client = httpx.Client(timeout=30)

    def scrape_google_maps(self, niche: str, city: str, limit: int = 100) -> list[dict]:
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
            try:
                response = self.client.post(TEXT_SEARCH_URL, headers=headers, json=payload)
                response.raise_for_status()
                data = response.json()
            except Exception as e:
                print(f"Maps API Error: {e}")
                break
                
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
                    "Source": "Google Maps"
                })
                
            next_token = data.get("nextPageToken")
            if not next_token:
                break
            payload["pageToken"] = next_token
            
        return self._deduplicate(leads)[:limit]

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
