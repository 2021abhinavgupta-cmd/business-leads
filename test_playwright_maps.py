import asyncio
from playwright.async_api import async_playwright
from googlesearch import search
import time

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        query = "dental clinic in london"
        url = f"https://www.google.com/maps/search/{query.replace(' ', '+')}"
        
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3) # Wait for results
        
        # Scroll the feed a bit
        try:
            await page.hover('a[href*="/maps/place/"]')
            await page.mouse.wheel(0, 1000)
            await asyncio.sleep(2)
        except Exception:
            pass
            
        names = await page.evaluate("""() => {
            const items = Array.from(document.querySelectorAll('a[href*="/maps/place/"]'));
            return [...new Set(items.map(a => a.getAttribute('aria-label')).filter(Boolean))];
        }""")
        
        await browser.close()
        
        print(f"Extracted {len(names)} names from Maps.")
        
        # Resolve domains
        for name in names[:3]: # test 3
            try:
                res = list(search(f"{name} official website", num_results=1))
                website = res[0] if res else ""
                print(f"Name: {name} -> Website: {website}")
            except Exception as e:
                print(f"Error for {name}: {e}")

# Guarded so `pytest` can IMPORT this file without running it. pytest collects
# every test_*.py at the repo root, and an unguarded call here meant that on
# each CI push this launched Chromium and live-scraped Google Maps from
# GitHub's IP ranges — the exact ToS/ban risk CLAUDE.md §8 documents, fired
# automatically, while collecting zero tests (this file defines no test_*
# function). It was also the one legacy script with no top-level try/except,
# so a Playwright or Google failure raised during collection and failed the
# whole run. Still runs exactly as documented via `python test_playwright_maps.py`.
if __name__ == "__main__":
    asyncio.run(run())
