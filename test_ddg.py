from ddgs import DDGS
import re

# Guarded so `pytest` can IMPORT this file without running it — pytest
# collects every test_*.py at the repo root, so an unguarded body fired a live
# DuckDuckGo search during collection on every CI push while contributing zero
# tests. Same pattern test_crawl.py and test_ps.py already use. Still runs
# exactly as documented via `python test_ddg.py`.
if __name__ == "__main__":
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
