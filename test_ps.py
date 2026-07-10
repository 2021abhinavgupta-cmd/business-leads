import asyncio
from httpx import AsyncClient
import config
from scrapers.website import WebsiteScraper

async def main():
    scraper = WebsiteScraper()
    scores = await scraper._pagespeed("https://www.hitchki.co/")
    print("Scores:", scores)
    await scraper.client.aclose()

if __name__ == "__main__":
    asyncio.run(main())
