from ddgs import DDGS
import re

company_name = "DigiChefs"
query = f'site:linkedin.com/in "Founder" OR "CEO" "{company_name}"'

try:
    with DDGS() as ddgs:
        results = list(ddgs.text(query, max_results=3, backend="lite"))
        for res in results:
            print("Title:", res.get("title"))
            print("Href:", res.get("href"))
            print("-" * 20)
except Exception as e:
    print("Error:", e)
