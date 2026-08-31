from ddgs import DDGS

# Guarded so `pytest` can IMPORT this file without running it — see test_ddg.py
# for the full reasoning. Still runs via `python test_maps.py`.
if __name__ == "__main__":
    try:
        with DDGS() as ddgs:
            results = list(ddgs.maps("dental clinic in london", max_results=10))
            for res in results:
                print("Title:", res.get("title"))
                print("Address:", res.get("address"))
                print("Phone:", res.get("phone"))
                print("Website:", res.get("url"))
                print("Rating:", res.get("rating"))
                print("-" * 20)
    except Exception as e:
        print("Error:", e)
