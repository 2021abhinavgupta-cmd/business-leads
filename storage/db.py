import sqlite3
import os
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DB_DIR, "database.sqlite")

def init_db():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Cost Logs Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS cost_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        category TEXT NOT NULL,
        cost REAL NOT NULL,
        description TEXT
    )
    """)
    
    # Email History Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS email_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        company TEXT,
        website TEXT,
        target_email TEXT,
        sender_email TEXT,
        subject TEXT,
        body TEXT
    )
    """)

    # Email Drafts Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS email_drafts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        company TEXT,
        website TEXT,
        target_email TEXT,
        subject TEXT,
        body TEXT,
        image_url TEXT
    )
    """)

    # Email Suppressions Table — unsubscribed / bounced / complained addresses.
    # Checked before every send (SESSender.send_email/send_followup) so we
    # never re-email someone who opted out, which is both a deliverability
    # risk (spam complaints tank sender reputation) and a compliance one
    # (CAN-SPAM/List-Unsubscribe-Post requires honoring opt-outs).
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS email_suppressions (
        email TEXT PRIMARY KEY,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        reason TEXT
    )
    """)

    # ONE-TIME CLEANUP: Remove historical Google Maps API costs since it is now free.
    # Guarded by PRAGMA user_version so this DELETE runs once ever, not on every
    # init_db() call (init_db() is called from every public function in this module).
    cursor.execute("PRAGMA user_version")
    schema_version = cursor.fetchone()[0]
    if schema_version < 1:
        cursor.execute("DELETE FROM cost_logs WHERE category = 'Google Maps API'")
        cursor.execute("PRAGMA user_version = 1")

    # Add message_id to email_history for follow-up threading (In-Reply-To/
    # References headers) — added after the table already existed in the wild,
    # so it's a migration, not part of the CREATE TABLE above.
    if schema_version < 2:
        cursor.execute("ALTER TABLE email_history ADD COLUMN message_id TEXT")
        cursor.execute("PRAGMA user_version = 2")

    conn.commit()
    conn.close()

def log_cost(category: str, cost: float, description: str = ""):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO cost_logs (category, cost, description) VALUES (?, ?, ?)",
        (category, cost, description)
    )
    conn.commit()
    conn.close()

def log_email(company: str, website: str, target_email: str, sender_email: str, subject: str, body: str, message_id: str = ""):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO email_history (company, website, target_email, sender_email, subject, body, message_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (company, website, target_email, sender_email, subject, body, message_id)
    )
    conn.commit()
    conn.close()

def add_suppression(email: str, reason: str = "unsubscribe"):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO email_suppressions (email, reason) VALUES (?, ?)",
        (email.strip().lower(), reason)
    )
    conn.commit()
    conn.close()

def is_suppressed(email: str) -> bool:
    init_db()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM email_suppressions WHERE email = ?", (email.strip().lower(),))
    row = cursor.fetchone()
    conn.close()
    return row is not None

def get_costs():
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM cost_logs ORDER BY timestamp DESC")
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def get_email_history():
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM email_history ORDER BY timestamp DESC")
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def log_draft(company: str, website: str, target_email: str, subject: str, body: str, image_url: str = ""):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO email_drafts (company, website, target_email, subject, body, image_url) VALUES (?, ?, ?, ?, ?, ?)",
        (company, website, target_email, subject, body, image_url)
    )
    conn.commit()
    conn.close()

def get_drafts():
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM email_drafts ORDER BY timestamp DESC")
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def delete_draft(draft_id: int):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM email_drafts WHERE id = ?", (draft_id,))
    conn.commit()
    conn.close()

def delete_draft_by_website(website: str):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM email_drafts WHERE website = ?", (website,))
    conn.commit()
    conn.close()

