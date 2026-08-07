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

    # Email Opens Table — one row per tracking-pixel fetch, not per email.
    # A single message can be fetched many times (reopened, forwarded,
    # re-rendered), and the raw hits are worth keeping: collapsing to a
    # boolean at write time would throw away the repeat-open signal, which is
    # the more interesting one. Aggregation happens at read time instead.
    # No FK to email_history: the pixel is matched by a digest of the
    # Message-ID (see emailer/tracking.py), and a hit can legitimately arrive
    # for a message that was sent outside this database.
    if schema_version < 3:
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS email_opens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tracking_id TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            user_agent TEXT,
            ip_hash TEXT,
            is_automated INTEGER DEFAULT 0
        )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_email_opens_tracking ON email_opens (tracking_id)")
        cursor.execute("PRAGMA user_version = 3")

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

def count_emails_sent_today() -> int:
    """
    Number of emails actually sent in the last 24h, read from the real send
    log rather than an in-process counter.

    DAILY_EMAIL_LIMIT used to be enforced only in main.py's batch loop,
    which never sends (it drafts to Sheets — see main.py:process_single_lead,
    whose "emailed" branch is unreachable), so the cap didn't constrain the
    path emails actually go out on: the Drafts inbox -> /api/send. On a
    brand-new sending domain the daily volume ceiling is the whole point of
    the warm-up, so it has to be enforced where sending really happens, and
    it has to survive restarts — hence counting rows, not a module global.

    CURRENT_TIMESTAMP is UTC, so this is a rolling 24h window rather than a
    calendar day; close enough for a volume guard and immune to timezone
    edge cases.
    """
    init_db()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM email_history WHERE timestamp >= datetime('now', '-1 day')")
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 0


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

def log_email_open(tracking_id: str, user_agent: str = "", ip_hash: str = "", is_automated: bool = False):
    """Record one tracking-pixel fetch. See emailer/tracking.py on what this does and doesn't mean."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO email_opens (tracking_id, user_agent, ip_hash, is_automated) VALUES (?, ?, ?, ?)",
        (tracking_id, user_agent[:300], ip_hash, 1 if is_automated else 0)
    )
    conn.commit()
    conn.close()


def get_open_summary() -> dict:
    """
    Per-message open aggregates, keyed by tracking_id.

    Automated fetches are counted separately rather than dropped — a message
    whose only hits came from a scanner genuinely hasn't been read, and that
    reads very differently from one with no hits at all.
    """
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT tracking_id,
               SUM(CASE WHEN is_automated = 0 THEN 1 ELSE 0 END) AS open_count,
               SUM(CASE WHEN is_automated = 1 THEN 1 ELSE 0 END) AS automated_count,
               MIN(CASE WHEN is_automated = 0 THEN timestamp END) AS first_opened_at,
               MAX(CASE WHEN is_automated = 0 THEN timestamp END) AS last_opened_at
        FROM email_opens
        GROUP BY tracking_id
    """)
    rows = cursor.fetchall()
    conn.close()
    return {row["tracking_id"]: dict(row) for row in rows}


def get_email_history():
    """
    Send log, newest first, with open data merged in.

    Rows sent before open tracking existed (no message_id, or sent with
    tracking off) come back with open_count 0 — which means "never
    measured", not "never read". The frontend distinguishes the two using
    tracking_enabled from /api/history.
    """
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM email_history ORDER BY timestamp DESC")
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()

    from emailer.tracking import tracking_id_for

    summary = get_open_summary()
    for row in rows:
        stats = summary.get(tracking_id_for(row.get("message_id") or ""), {})
        row["open_count"] = stats.get("open_count") or 0
        row["automated_count"] = stats.get("automated_count") or 0
        row["first_opened_at"] = stats.get("first_opened_at")
        row["last_opened_at"] = stats.get("last_opened_at")

    return rows

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

