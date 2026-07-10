from ddgs import DDGS

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
