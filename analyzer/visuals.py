import os
from io import BytesIO
from playwright.async_api import async_playwright
from PIL import Image, ImageDraw

# Create temporary directory for screenshots
os.makedirs("screenshots", exist_ok=True)

async def generate_audit_screenshot(url: str, company_name: str) -> tuple[str | None, str | None]:
    """
    Takes a mobile screenshot of the URL, draws an analysis box on it,
    and returns a tuple of (filepath, html_content). Returns (None, None) on failure.
    """
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
        
    try:
        # Step 1: Capture Screenshot via Playwright
        async with async_playwright() as p:
            # Launch chromium headless
            browser = await p.chromium.launch(headless=True)
            
            # Simulate a standard mobile device (iPhone 13 dimensions)
            context = await browser.new_context(
                viewport={'width': 390, 'height': 844},
                user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1",
                is_mobile=True,
                has_touch=True
            )
            
            page = await context.new_page()
            
            # Go to URL with a timeout and wait until network is mostly idle
            await page.goto(url, timeout=20000, wait_until="networkidle")
            
            # Take screenshot directly to memory
            screenshot_bytes = await page.screenshot(full_page=False)
            
            # Grab fully rendered HTML
            html_content = await page.content()
            
            await browser.close()
            
        # Step 2: Draw on the image via Pillow
        img = Image.open(BytesIO(screenshot_bytes)).convert("RGB")
        draw = ImageDraw.Draw(img)
        
        # We will draw a sleek red box on the top half to simulate "scanning" an issue
        width, height = img.size
        # Draw a rectangle slightly indented from the edges
        box = [20, 100, width - 20, 300]
        draw.rectangle(box, outline="red", width=5)
        
        # Save the image
        safe_name = "".join([c if c.isalnum() else "_" for c in company_name.lower()])
        filepath = f"screenshots/{safe_name}_audit.jpg"
        img.save(filepath, format="JPEG", quality=85)
        
        return filepath, html_content
        
    except Exception as e:
        print(f"Failed to generate visual evidence for {url}: {e}")
        return None, None
