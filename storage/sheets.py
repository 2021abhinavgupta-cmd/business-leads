"""
Storage module — manages the CRM data in Google Sheets.
"""

import os
import json
from datetime import datetime, timezone

import gspread

import config

# ---------------------------------------------------------------------------
# Constants & Configuration
# ---------------------------------------------------------------------------
# Expects credentials.json in the project root directory
_CREDENTIALS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "credentials.json")

_HEADERS = [
    "Company",              # A: 1
    "Contact Name",         # B: 2
    "Email",                # C: 3
    "Website",              # D: 4
    "Instagram Handle",     # E: 5
    "IG Followers",         # F: 6
    "IG Engagement Rate",   # G: 7
    "Website Speed Score",  # H: 8
    "SEO Score",            # I: 9
    "Overall Audit Score",  # J: 10
    "Flaw 1",               # K: 11
    "Flaw 2",               # L: 12
    "Email Subject",        # M: 13
    "Email Body",           # N: 14
    "Status",               # O: 15 (pending/drafted/approved/emailed/skipped/failed/replied)
    "Source",               # P: 16 (apollo/google_maps)
    "Sent At",              # Q: 17
    "Reply"                 # R: 18
]


class SheetsStorage:
    """Manage lead CRM data in Google Sheets."""

    def __init__(self):
        if not config.GOOGLE_SHEETS_ID:
            raise ValueError("GOOGLE_SHEETS_ID is not set in config / environment.")

        # Try to load from Environment Variable first (for Railway/Production)
        creds_json_str = os.getenv("GOOGLE_CREDENTIALS_JSON")
        if creds_json_str:
            try:
                creds_dict = json.loads(creds_json_str)
                self.gc = gspread.service_account_from_dict(creds_dict)
            except Exception as e:
                raise ValueError(f"Failed to parse GOOGLE_CREDENTIALS_JSON: {e}")
        # Fallback to local credentials.json file
        else:
            if not os.path.exists(_CREDENTIALS_FILE):
                raise FileNotFoundError(f"Missing Google Sheets credentials at {_CREDENTIALS_FILE} and GOOGLE_CREDENTIALS_JSON is empty.")
            self.gc = gspread.service_account(filename=_CREDENTIALS_FILE)

        self.sheet = self.gc.open_by_key(config.GOOGLE_SHEETS_ID).sheet1

    # ------------------------------------------------------------------
    # Initialization & Insertion
    # ------------------------------------------------------------------

    def init_sheet(self) -> None:
        """Add headers if the sheet is completely empty."""
        try:
            first_row = self.sheet.row_values(1)
            if not first_row:
                self.sheet.append_row(_HEADERS)
        except Exception:
            # If the sheet is empty, row_values might raise an exception
            self.sheet.append_row(_HEADERS)

    def add_lead(self, data: dict) -> None:
        """
        Append a new lead row to the sheet.
        Skips insertion if the exact email already exists (deduplication check).
        """
        email = str(data.get("Email", "")).strip().lower()
        
        # Deduplication check via Column C (index 3)
        if email:
            try:
                # col_values gets all values in that column (1-indexed)
                existing_emails = [e.lower().strip() for e in self.sheet.col_values(3)]
                if email in existing_emails:
                    return  # Skip, email already in CRM
            except Exception:
                pass  # Proceed if we can't read the column yet

        # Build row aligning exactly with _HEADERS order
        row = [
            data.get("Company", ""),
            data.get("Contact Name", ""),
            data.get("Email", ""),
            data.get("Website", ""),
            data.get("Instagram Handle", ""),
            data.get("IG Followers", ""),
            data.get("IG Engagement Rate", ""),
            data.get("Website Speed Score", ""),
            data.get("SEO Score", ""),
            data.get("Overall Audit Score", ""),
            data.get("Flaw 1", ""),
            data.get("Flaw 2", ""),
            data.get("Email Subject", ""),
            data.get("Email Body", ""),
            data.get("Status", "pending"),
            data.get("Source", ""),
            data.get("Sent At", ""),
            data.get("Reply", ""),
        ]
        
        import time
        time.sleep(1.5) # Prevent Google Sheets API Write Rate Limit (60/min)
        self.sheet.append_row(row)

    # ------------------------------------------------------------------
    # Querying
    # ------------------------------------------------------------------

    def get_pending_leads(self) -> list[dict]:
        """
        Return all rows where Status == "pending" and Email is not empty.
        Includes a 'row_number' key in each dict for future targeted updates.
        """
        try:
            records = self.sheet.get_all_records()
        except Exception:
            return []

        pending_leads = []
        
        # get_all_records() returns data starting from row 2 (assuming row 1 is headers).
        for i, row in enumerate(records, start=2):
            status = str(row.get("Status", "")).strip().lower()
            email = str(row.get("Email", "")).strip()
            
            if status == "pending":
                row_data = dict(row)
                row_data["row_number"] = i
                pending_leads.append(row_data)
                
        return pending_leads

    def get_approved_leads(self) -> list[dict]:
        """
        Return all rows where Status == "approved" and Email is not empty.
        Includes a 'row_number' key in each dict for targeted updates.
        """
        try:
            records = self.sheet.get_all_records()
        except Exception:
            return []

        approved_leads = []
        for i, row in enumerate(records, start=2):
            status = str(row.get("Status", "")).strip().lower()
            email = str(row.get("Email", "")).strip()
            
            if status == "approved" and email:
                row_data = dict(row)
                row_data["row_number"] = i
                approved_leads.append(row_data)
                
        return approved_leads

    def get_stats(self) -> dict:
        """
        Return the count of each status for a dashboard overview.
        """
        stats = {
            "pending": 0,
            "emailed": 0,
            "skipped": 0,
            "failed": 0,
            "replied": 0
        }
        
        try:
            # Status is in Column O (index 15)
            statuses = self.sheet.col_values(15)[1:]  # slice off the header
            for s in statuses:
                s_lower = s.strip().lower()
                if s_lower in stats:
                    stats[s_lower] += 1
        except Exception:
            pass
            
        return stats

    # ------------------------------------------------------------------
    # Updating
    # ------------------------------------------------------------------

    def update_status(self, row_number: int, status: str) -> None:
        """
        Update the Status (Col O) and Sent At (Col Q) timestamp for a lead.
        """
        # Column O = 15
        self.sheet.update_cell(row_number, 15, status)
        
        # Column Q = 17
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        self.sheet.update_cell(row_number, 17, now_str)

    def save_draft(self, row_number: int, subject: str, body: str) -> None:
        """
        Update the Email Subject (Col M) and Email Body (Col N) for a lead.
        """
        # Column M = 13
        self.sheet.update_cell(row_number, 13, subject)
        # Column N = 14
        self.sheet.update_cell(row_number, 14, body)

    def mark_replied(self, row_number: int) -> None:
        """
        Update the lead's status to "replied".
        """
        # Column O = 15
        self.sheet.update_cell(row_number, 15, "replied")

    def find_row_by_website(self, website: str) -> int:
        """
        Find the row number for a given website URL.
        Returns the row number (1-indexed) or None if not found.
        """
        try:
            # Website is in Column D (index 4)
            websites = self.sheet.col_values(4)
            target = website.lower().strip().rstrip('/')
            
            for i, w in enumerate(websites):
                if w.lower().strip().rstrip('/') == target:
                    return i + 1  # +1 because gspread is 1-indexed
            return None
        except Exception:
            return None
