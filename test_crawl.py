import asyncio
from crawl4ai import AsyncWebCrawler

async def main():
    async with AsyncWebCrawler() as crawler:
        result = await crawler.arun(url='https://example.com', screenshot=True)
        print("Markdown len:", len(result.markdown))
        print("Screenshot len:", len(result.screenshot) if result.screenshot else "No screenshot")

if __name__ == "__main__":
    asyncio.run(main())
