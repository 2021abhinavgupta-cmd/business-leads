"""
Storage module — manages the CRM data in Google Sheets.
"""

import os
import json
import threading
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
    "Status",               # O: 15 (pending/drafted/approved/emailed/skipped/failed/replied/unsubscribed)
    "Source",               # P: 16 (apollo/google_maps)
    "Sent At",              # Q: 17
    "Reply",                # R: 18
    "Follow-up Stage",      # S: 19
    "Message ID"            # T: 20 (RFC Message-ID of the initial send, for follow-up threading)
]


class SheetsStorage:
    """Manage lead CRM data in Google Sheets."""

    def __init__(self):
        """
        Prepare the CRM handle WITHOUT touching the network.

        This used to authenticate against Google and open the spreadsheet
        right here. Because `app.py` constructs `SheetsStorage()` at module
        scope, that made `import app` perform a live, credentialed Google API
        call — so merely importing the application required working Sheets
        credentials, a correct system clock, and network access.

        The consequences were real and already documented in CLAUDE.md §8:
        an unrelated environment problem surfaced as scattered failures across
        several test files rather than one clearly-labelled Sheets error (a
        dev machine whose clock had drifted 11 days took a while to diagnose
        for exactly this reason). Worse, and previously unnoticed: **GitHub
        Actions configures no secrets at all**, so `GOOGLE_SHEETS_ID` is unset
        there and every `import app` test has been erroring on every push,
        while §6 claimed they "run for real" in CI.

        Connecting lazily fixes all of that at once and changes nothing about
        how the CRM behaves — the first method that actually needs the
        spreadsheet still connects, still raises the same errors, and still
        self-heals the header row. What moves is only WHEN, from import time
        to first real use.
        """
        self._gc = None
        self._sheet = None
        # Guard against two threads connecting at once: app.py dispatches
        # every sheets call through asyncio.to_thread, so concurrent requests
        # can reach the property simultaneously on a cold process.
        self._connect_lock = threading.Lock()

        # A misconfigured deployment must still be obvious. It just gets a
        # startup warning here instead of a crash, matching how app.py already
        # announces a missing API_KEY / ALLOWED_ORIGINS rather than refusing
        # to boot. The audit and drafting paths do not need Sheets at all
        # (every write to it is a background task wrapped in try/except), so
        # taking the whole web service down over the legacy CRM was a heavier
        # failure than the problem warranted.
        if not config.GOOGLE_SHEETS_ID:
            print(
                "[Sheets] WARNING: GOOGLE_SHEETS_ID is not set — the CRM is unavailable. "
                "Audits and drafts still work; anything that reads or writes the lead sheet "
                "will fail when it is first used."
            )

    @property
    def sheet(self):
        """The worksheet, connecting on first access."""
        if self._sheet is None:
            with self._connect_lock:
                # Re-check inside the lock: another thread may have connected
                # while this one waited.
                if self._sheet is None:
                    self._connect()
        return self._sheet

    def _connect(self) -> None:
        """
        Authenticate and open the spreadsheet.

        Raises exactly what __init__ used to raise, so a genuinely
        misconfigured environment still gets the same clear message — only
        later, and only if something actually needed the CRM.
        """
        if not config.GOOGLE_SHEETS_ID:
            raise ValueError("GOOGLE_SHEETS_ID is not set in config / environment.")

        # Try to load from Environment Variable first (for Railway/Production)
        creds_json_str = os.getenv("GOOGLE_CREDENTIALS_JSON")
        if creds_json_str:
            try:
                creds_dict = json.loads(creds_json_str)
                self._gc = gspread.service_account_from_dict(creds_dict)
            except Exception as e:
                raise ValueError(f"Failed to parse GOOGLE_CREDENTIALS_JSON: {e}")
        # Fallback to local credentials.json file
        else:
            if not os.path.exists(_CREDENTIALS_FILE):
                raise FileNotFoundError(f"Missing Google Sheets credentials at {_CREDENTIALS_FILE} and GOOGLE_CREDENTIALS_JSON is empty.")
            self._gc = gspread.service_account(filename=_CREDENTIALS_FILE)

        # Assigned BEFORE init_sheet() so the header backfill can read
        # self.sheet through the property without re-entering _connect().
        self._sheet = self._gc.open_by_key(config.GOOGLE_SHEETS_ID).sheet1
        self.init_sheet()

    # ------------------------------------------------------------------
    # Initialization & Insertion
    # ------------------------------------------------------------------

    def init_sheet(self) -> None:
        """
        Add headers if the sheet is completely empty, or backfill any header
        columns appended to _HEADERS after this sheet was first created (e.g.
        "Message ID") so older sheets self-heal instead of silently writing
        to an unlabeled column.
        """
        try:
            first_row = self.sheet.row_values(1)
        except Exception:
            first_row = []

        if not first_row:
            self.sheet.append_row(_HEADERS)
            return

        if len(first_row) < len(_HEADERS):
            missing = _HEADERS[len(first_row):]
            start_col = len(first_row) + 1
            cell_range = f"{gspread.utils.rowcol_to_a1(1, start_col)}:{gspread.utils.rowcol_to_a1(1, len(_HEADERS))}"
            self.sheet.update(cell_range, [missing])

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
            data.get("Follow-up Stage", 0),
            data.get("Message ID", ""),
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

    def get_leads_for_followup(self, max_stage: int = 2) -> list[dict]:
        """
        Return leads that were 'emailed', haven't 'replied', 
        whose Follow-up Stage is < max_stage, and were last 
        emailed > 3 days ago.
        """
        try:
            records = self.sheet.get_all_records()
        except Exception:
            return []

        followup_leads = []
        now = datetime.now(timezone.utc)
        
        for i, row in enumerate(records, start=2):
            status = str(row.get("Status", "")).strip().lower()
            stage = row.get("Follow-up Stage", "")
            stage = int(stage) if str(stage).isdigit() else 0
            sent_at_str = str(row.get("Sent At", "")).strip()
            
            if status == "emailed" and stage < max_stage and sent_at_str:
                try:
                    # Parse "2024-05-10 14:30:00 UTC"
                    sent_at = datetime.strptime(sent_at_str.replace(" UTC", ""), "%Y-%m-%d %H:%M:%S")
                    sent_at = sent_at.replace(tzinfo=timezone.utc)
                    
                    if (now - sent_at).days >= 3:
                        row_data = dict(row)
                        row_data["row_number"] = i
                        followup_leads.append(row_data)
                except Exception as e:
                    pass
                    
        return followup_leads

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

    def increment_followup(self, row_number: int, new_stage: int) -> None:
        """
        Update the Follow-up Stage (Col S), set Sent At to now.
        """
        # Column S = 19
        self.sheet.update_cell(row_number, 19, new_stage)
        # Update Sent At
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

    def mark_replied(self, row_number: int, sentiment: str = "") -> None:
        """
        Update the lead's status to "replied", or "replied-{sentiment}" when
        a VADER sentiment classification is available (emailer/reply_checker.py)
        — "positive"/"negative"/"neutral" — so the operator can tell a likely
        yes from a likely no at a glance instead of opening every thread.
        Any of these values is equally "not emailed" to run_followups()'s
        `status == "emailed"` filter, so this doesn't change which leads stop
        getting follow-ups, only what the stopped status says.
        """
        # Column O = 15
        self.sheet.update_cell(row_number, 15, f"replied-{sentiment}" if sentiment else "replied")

    def mark_unsubscribed(self, row_number: int) -> None:
        """
        Update the lead's status to "unsubscribed" so run_batch/run_followups
        skip it going forward (both only pick up "pending"/"emailed" rows).
        """
        # Column O = 15
        self.sheet.update_cell(row_number, 15, "unsubscribed")

    def set_message_id(self, row_number: int, message_id: str) -> None:
        """
        Store the RFC Message-ID of the email just sent (Col T) so a later
        follow-up can thread against it via In-Reply-To/References.
        """
        # Column T = 20
        self.sheet.update_cell(row_number, 20, message_id)

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

    def find_row_by_email(self, email: str) -> int:
        """
        Find the row number for a given email address.
        Returns the row number (1-indexed) or None if not found.
        """
        try:
            # Email is in Column C (index 3)
            emails = self.sheet.col_values(3)
            target = email.lower().strip()

            for i, e in enumerate(emails):
                if e.lower().strip() == target:
                    return i + 1
            return None
        except Exception:
            return None
