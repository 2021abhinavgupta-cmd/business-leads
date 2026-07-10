import time
import asyncio
from scrapers.website import WebsiteScraper
from enrichment.decision_maker import DecisionMaker
from analyzer.ai_audit import AIAuditor
from analyzer.visuals import generate_audit_screenshot

async def test_audit():
    start_time = time.time()
    print("Testing WebsiteScraper on TIMEZONE...")
    scraper = WebsiteScraper()
    url = "https://www.timezonegames.com/en-in/locations/timezone-oberoi-mall-goregaon?utm_source=google&utm_medium=organic&utm_campaign=intz_20220420_googlemybusiness&utm_term"
    
    try:
        image_path, html_content = await generate_audit_screenshot(url, "TIMEZONE")
        web_data = await scraper.audit_website(url, html=html_content)
        print(f"Web Data: Reachable={web_data.reachable}, Speed={web_data.page_speed_score}")
    except Exception as e:
        print(f"WebsiteScraper crashed: {e}")
        return
    print(f"WebsiteScraper took {time.time() - start_time:.2f}s")

    dm_start = time.time()
    print("Testing DecisionMaker...")
    dm = DecisionMaker()
    try:
        contact = dm.find_decision_maker("TIMEZONE", url, html_content=html_content)
        print(f"Contact: {contact}")
    except Exception as e:
        print(f"DecisionMaker crashed: {e}")
        return
    print(f"DecisionMaker took {time.time() - dm_start:.2f}s")

    ai_start = time.time()
    print("Testing AIAuditor...")
    auditor = AIAuditor()
    try:
        analysis = auditor.analyze_lead("TIMEZONE", None, web_data, image_path=image_path)
        print(f"Analysis: {analysis.keys() if analysis else 'None'}")
    except Exception as e:
        print(f"AIAuditor crashed: {e}")
    print(f"AIAuditor took {time.time() - ai_start:.2f}s")
    print(f"Total time: {time.time() - start_time:.2f}s")

if __name__ == "__main__":
    asyncio.run(test_audit())
