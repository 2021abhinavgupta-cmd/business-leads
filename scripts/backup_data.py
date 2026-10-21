"""Daily backup of the leads app's SQLite database.

Uses sqlite3's online backup API, which gives a consistent snapshot even while
the app is writing (a plain file copy of a live SQLite file can be torn).

Keeps the last KEEP daily snapshots in E:/Backups/leads and, if OneDrive exists,
one extra copy of the newest snapshot there so a laptop disk failure cannot take
every copy at once. Screenshots are re-creatable and ~100 MB, so they are only
mirrored weekly (Sundays).

Run:  python scripts/backup_data.py
"""
import os
import shutil
import sqlite3
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "database.sqlite")
SHOTS = os.path.join(ROOT, "data", "screenshots")
LOCAL_DIR = r"E:\Backups\leads"
ONEDRIVE_DIR = os.path.join(os.path.expanduser("~"), "OneDrive", "Backups", "leads-app")
KEEP = 14


def main() -> int:
    if not os.path.exists(DB):
        print(f"[Backup] database not found at {DB}")
        return 1
    os.makedirs(LOCAL_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    dest = os.path.join(LOCAL_DIR, f"database-{stamp}.sqlite")

    src = sqlite3.connect(DB)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
        ok = dst.execute("pragma integrity_check").fetchone()[0]
    finally:
        dst.close()
        src.close()
    if ok != "ok":
        print(f"[Backup] snapshot failed integrity check: {ok}")
        os.remove(dest)
        return 2
    print(f"[Backup] wrote {dest} ({os.path.getsize(dest) // 1024} KB)")

    snaps = sorted(f for f in os.listdir(LOCAL_DIR) if f.startswith("database-"))
    for old in snaps[:-KEEP]:
        os.remove(os.path.join(LOCAL_DIR, old))

    if os.path.isdir(os.path.expanduser(r"~\OneDrive")):
        try:
            os.makedirs(ONEDRIVE_DIR, exist_ok=True)
            shutil.copy2(dest, os.path.join(ONEDRIVE_DIR, "database-latest.sqlite"))
            print("[Backup] copied newest snapshot to OneDrive")
        except Exception as e:
            print(f"[Backup] OneDrive copy failed (local copy is fine): {e}")

    if datetime.now().weekday() == 6 and os.path.isdir(SHOTS):
        mirror = os.path.join(LOCAL_DIR, "screenshots")
        os.makedirs(mirror, exist_ok=True)
        added = 0
        for f in os.listdir(SHOTS):
            target = os.path.join(mirror, f)
            if not os.path.exists(target):
                shutil.copy2(os.path.join(SHOTS, f), target)
                added += 1
        print(f"[Backup] mirrored {added} new screenshots")
    return 0


if __name__ == "__main__":
    sys.exit(main())
