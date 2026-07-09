import asyncio
import sys

from emailer.ses_sender import SESSender
from storage.sheets import SheetsStorage
from analyzer.visuals import generate_audit_screenshot

async def send_approved_emails():
    """
    Finds all leads in Google Sheets with Status="approved",
    sends the drafted email via SES, and marks them as "emailed".
    """
    try:
        ses = SESSender()
        sheets = SheetsStorage()
    except Exception as e:
        print(f"Failed to initialize components: {e}")
        sys.exit(1)

    approved_leads = sheets.get_approved_leads()
    print(f"Found {len(approved_leads)} approved leads ready to send.")

    for lead in approved_leads:
        company = lead.get("Company", "Unknown Company")
        email = lead.get("Email", "")
        subject = lead.get("Email Subject", "")
        body = lead.get("Email Body", "")
        website = lead.get("Website", "")
        row_number = lead.get("row_number")

        if not email or not subject or not body:
            print(f"  Skipping {company} - missing email, subject, or body.")
            continue

        print(f"  Sending email to {email} ({company})...")

        # Generate fresh screenshot for the email
        image_path = None
        if website:
            image_path = await generate_audit_screenshot(website, company)

        success = ses.send_email(email, subject, body, image_path=image_path)

        if image_path:
            import os
            try:
                os.remove(image_path)
            except:
                pass

        if success:
            sheets.update_status(row_number, "emailed")
            print(f"  [SUCCESS] Emailed {company}")
        else:
            sheets.update_status(row_number, "failed_error")
            print(f"  [FAILED] Could not send to {company}")

if __name__ == "__main__":
    asyncio.run(send_approved_emails())
