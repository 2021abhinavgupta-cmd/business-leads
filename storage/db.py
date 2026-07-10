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
    
    # ONE-TIME CLEANUP: Remove historical Google Maps API costs since it is now free.
    # Guarded by PRAGMA user_version so this DELETE runs once ever, not on every
    # init_db() call (init_db() is called from every public function in this module).
    cursor.execute("PRAGMA user_version")
    schema_version = cursor.fetchone()[0]
    if schema_version < 1:
        cursor.execute("DELETE FROM cost_logs WHERE category = 'Google Maps API'")
        cursor.execute("PRAGMA user_version = 1")

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

def log_email(company: str, website: str, target_email: str, sender_email: str, subject: str, body: str):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO email_history (company, website, target_email, sender_email, subject, body) VALUES (?, ?, ?, ?, ?, ?)",
        (company, website, target_email, sender_email, subject, body)
    )
    conn.commit()
    conn.close()

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

