"""
Persistence for the CSV import outreach queue (schema v14).

Every function opens its own short-lived connection against db.DB_PATH read at
call time, the same way storage/db.py does, so the temp-DB monkeypatching the
test suite uses works unchanged.
"""
import json
import sqlite3
from datetime import datetime, timezone

from storage import db

LEAD_FIELDS = (
    "person_name", "person_title", "person_seniority", "email",
    "organization", "domain", "industry", "city", "state", "country",
)

_UPDATABLE = frozenset({
    "status", "skip_reason", "subject", "body", "review_warnings",
    "source_quote", "drafted_at", "sent_at", "history_id", "error",
})


def now_sql() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _connect() -> sqlite3.Connection:
    db.init_db()
    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _decode(row: sqlite3.Row) -> dict:
    lead = dict(row)
    raw = lead.get("review_warnings")
    try:
        lead["review_warnings"] = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        lead["review_warnings"] = []
    return lead


def create_batch(filename: str, leads: list[dict]) -> int:
    ready = sum(1 for lead in leads if lead["status"] == "ready")
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO import_batches (filename, total_rows, ready_count, skipped_count) VALUES (?, ?, ?, ?)",
            (filename, len(leads), ready, len(leads) - ready),
        )
        batch_id = cur.lastrowid
        cur.executemany(
            "INSERT INTO import_leads (batch_id, person_name, person_title, person_seniority, email, "
            "organization, domain, industry, city, state, country, status, skip_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (batch_id, *(lead[f] for f in LEAD_FIELDS), lead["status"], lead.get("skip_reason"))
                for lead in leads
            ],
        )
        conn.commit()
        return batch_id
    finally:
        conn.close()


def list_batches() -> list[dict]:
    conn = _connect()
    try:
        batches = [dict(r) for r in conn.execute("SELECT * FROM import_batches ORDER BY id DESC")]
        for batch in batches:
            batch["status_counts"] = {
                r["status"]: r["n"]
                for r in conn.execute(
                    "SELECT status, COUNT(*) AS n FROM import_leads WHERE batch_id = ? GROUP BY status",
                    (batch["id"],),
                )
            }
        return batches
    finally:
        conn.close()


def get_lead(lead_id: int) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM import_leads WHERE id = ?", (lead_id,)).fetchone()
        return _decode(row) if row else None
    finally:
        conn.close()


def list_leads(
    batch_id: int, *, status=None, skip_reason=None, industry=None, seniority=None,
    state=None, q=None, limit: int = 50, offset: int = 0,
) -> dict:
    where, params = ["batch_id = ?"], [batch_id]
    for column, value in (
        ("status", status), ("skip_reason", skip_reason), ("industry", industry),
        ("person_seniority", seniority), ("state", state),
    ):
        if value:
            where.append(f"{column} = ?")
            params.append(value)
    if q:
        like = f"%{q.strip().lower()}%"
        where.append(
            "(lower(person_name) LIKE ? OR lower(organization) LIKE ? "
            "OR lower(email) LIKE ? OR lower(person_title) LIKE ?)"
        )
        params += [like] * 4
    clause = " AND ".join(where)
    conn = _connect()
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM import_leads WHERE {clause}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM import_leads WHERE {clause} ORDER BY id LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return {"leads": [_decode(r) for r in rows], "total": total}
    finally:
        conn.close()


def facets(batch_id: int) -> dict:
    out = {}
    conn = _connect()
    try:
        for key, column in (
            ("status", "status"), ("skip_reason", "skip_reason"), ("industry", "industry"),
            ("seniority", "person_seniority"), ("state", "state"),
        ):
            rows = conn.execute(
                f"SELECT {column} AS v, COUNT(*) AS n FROM import_leads "
                f"WHERE batch_id = ? AND {column} IS NOT NULL AND {column} != '' "
                f"GROUP BY {column} ORDER BY n DESC, v",
                (batch_id,),
            )
            out[key] = {r["v"]: r["n"] for r in rows}
        return out
    finally:
        conn.close()


def update_lead(lead_id: int, *, only_if_status: tuple | None = None, **fields) -> bool:
    """Update a lead. With only_if_status the write is a compare and set: it applies only while
    the lead is still in one of those statuses. Returns True if a row changed."""
    bad = set(fields) - _UPDATABLE
    if bad:
        raise ValueError(f"Not updatable: {sorted(bad)}")
    if not fields:
        return False
    if "review_warnings" in fields and not isinstance(fields["review_warnings"], str):
        fields["review_warnings"] = json.dumps(fields["review_warnings"] or [])
    # Column names come from the whitelist above, never from the caller's input.
    sets = ", ".join(f"{name} = ?" for name in fields)
    conn = _connect()
    try:
        sql, params = f"UPDATE import_leads SET {sets} WHERE id = ?", [*fields.values(), lead_id]
        if only_if_status:
            sql += f" AND status IN ({', '.join('?' * len(only_if_status))})"
            params.extend(only_if_status)
        changed = conn.execute(sql, params).rowcount > 0
        conn.commit()
        return changed
    finally:
        conn.close()


def load_blockers() -> dict:
    """
    Everything the skip rules need to know about the outside world, loaded in
    a few bulk reads rather than a query per row (a file can be 20,000 rows).
    """
    conn = _connect()
    try:
        imported = {r[0] for r in conn.execute("SELECT lower(email) FROM import_leads")}
        suppressed = {r[0] for r in conn.execute("SELECT lower(email) FROM email_suppressions")}
        emailed = {
            r[0] for r in conn.execute(
                "SELECT lower(target_email) FROM email_history WHERE target_email IS NOT NULL"
            )
        }
    finally:
        conn.close()
    hosts = set()
    for key in db.get_sent_websites_summary():
        host = key.split("/")[0]
        if host:
            hosts.add(host)
    return {"imported": imported, "suppressed": suppressed, "emailed": emailed, "company_hosts": hosts}
