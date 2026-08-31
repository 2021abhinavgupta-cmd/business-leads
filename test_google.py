from googlesearch import search
import re

# Guarded so `pytest` can IMPORT this file without running it — see test_ddg.py
# for the full reasoning. Still runs via `python test_google.py`.
if __name__ == "__main__":
    company_name = "DigiChefs"
    query = f'site:linkedin.com/in "Founder" OR "CEO" "{company_name}"'
    print("Query:", query)

    try:
        results = list(search(query, num_results=3, advanced=True))
        for res in results:
            print(f"Title: {res.title}")
            print(f"URL: {res.url}")
            print(f"Desc: {res.description}")
            print("-" * 20)

            title = res.title
            parts = re.split(r'[-|]', title)
            if parts:
                name = parts[0].strip()
                print("Parsed Name:", name)
    except Exception as e:
        print("Error:", e)
