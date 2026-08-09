import json
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

    # Email Replies Table — inbound mail matched back to something we sent.
    # reply_message_id is the PRIMARY KEY so re-scanning the same mailbox is
    # idempotent: the checker re-reads a rolling window of days and will see
    # the same reply many times, and INSERT OR REPLACE turns that into a
    # no-op rather than a pile of duplicates inflating the one metric here
    # that's meant to be trustworthy.
    if schema_version < 4:
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS email_replies (
            reply_message_id TEXT PRIMARY KEY,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            in_reply_to TEXT,
            from_email TEXT,
            target_email TEXT,
            subject TEXT,
            is_auto INTEGER DEFAULT 0,
            is_bounce INTEGER DEFAULT 0
        )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_email_replies_thread ON email_replies (in_reply_to)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_email_replies_target ON email_replies (target_email)")
        cursor.execute("PRAGMA user_version = 4")

    # review_warnings on email_drafts — the AI-audit accuracy checks
    # (_check_number_hallucination, _verify_grounding, etc. in
    # analyzer/ai_audit.py) used to only print() their findings, which
    # meant real signal ("this cites a number that isn't in the source
    # data") only ever reached a Railway log line nobody was watching while
    # actually reviewing a draft. Stored as a JSON-encoded list of warning
    # strings (possibly empty); parsed back to a list in get_drafts().
    if schema_version < 5:
        cursor.execute("ALTER TABLE email_drafts ADD COLUMN review_warnings TEXT")
        cursor.execute("PRAGMA user_version = 5")

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


def log_reply(reply_message_id: str, in_reply_to: str, from_email: str, target_email: str,
              subject: str = "", is_auto: bool = False, is_bounce: bool = False):
    """
    Record one inbound message matched to something we sent.

    INSERT OR REPLACE, keyed on the reply's own Message-ID, because the
    checker re-scans a rolling window and will see the same reply on every
    run. A reply that carries no Message-ID at all gets a synthetic key
    derived from its own content, so those stay idempotent too instead of
    all colliding on the empty string.
    """
    init_db()
    key = (reply_message_id or "").strip()
    if not key:
        import hashlib
        seed = f"{from_email}|{subject}|{in_reply_to}".encode("utf-8")
        key = f"<synthetic-{hashlib.blake2s(seed, digest_size=12).hexdigest()}>"

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """INSERT OR REPLACE INTO email_replies
           (reply_message_id, in_reply_to, from_email, target_email, subject, is_auto, is_bounce)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (key, in_reply_to, (from_email or "").strip().lower(), (target_email or "").strip().lower(),
         subject[:300], 1 if is_auto else 0, 1 if is_bounce else 0)
    )
    conn.commit()
    conn.close()


def get_replies():
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM email_replies ORDER BY timestamp DESC")
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_reply_summary() -> tuple[dict, dict]:
    """
    Reply aggregates indexed two ways: by the Message-ID replied to, and by
    the address we originally mailed.

    Both exist because matching happens both ways — threading headers are
    exact but plenty of clients drop them, in which case the sender's address
    is all there is to go on.
    """
    by_thread: dict = {}
    by_address: dict = {}

    for row in get_replies():
        entry = {
            "replied": not row["is_auto"] and not row["is_bounce"],
            "is_auto": bool(row["is_auto"]),
            "is_bounce": bool(row["is_bounce"]),
            "from_email": row["from_email"],
            "subject": row["subject"],
            "timestamp": row["timestamp"],
        }
        # A real reply outranks an auto-reply or bounce for the same thread:
        # an out-of-office followed by a genuine answer is a genuine answer.
        for index, key in ((by_thread, row["in_reply_to"]), (by_address, row["target_email"])):
            if not key:
                continue
            existing = index.get(key)
            if existing is None or (entry["replied"] and not existing["replied"]):
                index[key] = entry

    return by_thread, by_address


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
    replies_by_thread, replies_by_address = get_reply_summary()

    for row in rows:
        stats = summary.get(tracking_id_for(row.get("message_id") or ""), {})
        row["open_count"] = stats.get("open_count") or 0
        row["automated_count"] = stats.get("automated_count") or 0
        row["first_opened_at"] = stats.get("first_opened_at")
        row["last_opened_at"] = stats.get("last_opened_at")

        reply = replies_by_thread.get(row.get("message_id") or "") or \
            replies_by_address.get((row.get("target_email") or "").strip().lower())
        row["replied"] = bool(reply and reply["replied"])
        row["auto_replied"] = bool(reply and reply["is_auto"])
        row["bounced"] = bool(reply and reply["is_bounce"])
        row["reply_subject"] = reply["subject"] if reply else None
        row["reply_at"] = reply["timestamp"] if reply else None

    return rows

def log_draft(company: str, website: str, target_email: str, subject: str, body: str, image_url: str = "", review_warnings: list[str] | None = None):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO email_drafts (company, website, target_email, subject, body, image_url, review_warnings) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (company, website, target_email, subject, body, image_url, json.dumps(review_warnings) if review_warnings else None)
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
    drafts = [dict(row) for row in rows]
    for draft in drafts:
        raw = draft.get("review_warnings")
        try:
            draft["review_warnings"] = json.loads(raw) if raw else []
        except (TypeError, json.JSONDecodeError):
            draft["review_warnings"] = []
    return drafts

def get_draft_by_website(website: str):
    """
    Fetch the single draft for *website*, or None.

    Exists so the send path can check a draft's own review_warnings and age
    before letting it go out — /api/send receives the subject/body in the
    request, not the draft row, so without this lookup it has no way to know
    the copy it's about to send was flagged during generation.
    """
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM email_drafts WHERE website = ? ORDER BY timestamp DESC LIMIT 1", (website,))
    row = cursor.fetchone()
    conn.close()
    if row is None:
        return None
    draft = dict(row)
    raw = draft.get("review_warnings")
    try:
        draft["review_warnings"] = json.loads(raw) if raw else []
    except (TypeError, json.JSONDecodeError):
        draft["review_warnings"] = []
    return draft

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

