# CSV Import Outreach Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user upload a lead CSV, have an AI draft one short automation-services email per lead, review and approve every draft, and send them through the existing send path a day's worth at a time.

**Architecture:** New isolated Python package `importer/` (pure CSV rules, homepage fetch, composition and checks, drafting, send policy, service layer) plus one storage module `storage/import_store.py` over two new SQLite tables (schema v14). `app.py` only gains thin routes, one optional field on `SendRequest`, and guarded branches in `/api/send`. The UI is a new self-contained `frontend/src/ImportView.jsx` component mounted as a new `import` view.

**Tech Stack:** Python 3 / FastAPI / SQLite (`PRAGMA user_version` migrations) / pytest (`asyncio_mode = auto`) / React 18 + Vite + axios + lucide-react.

**Spec:** `docs/superpowers/specs/2026-10-15-csv-import-outreach-design.md`

## Global Constraints

- Every new route has `Depends(require_api_key)` and `Depends(rate_limit(N, 60))`. Limits: upload 5, batches 60, leads list 120, restore/skip/edit/approve/unapprove 120, draft 12, status 120.
- No hyphens or dashes in generated email text, in the fixed pitch line, or in the compliance footer's own wording.
- `DAILY_EMAIL_LIMIT` is enforced exactly as today. Never bypass or auto-retry past it.
- Supported countries: `united states`, `india` (code constant `SUPPORTED_COUNTRIES`).
- CSV limits: 5 MB and 20,000 rows, else HTTP 413. The CSV is never written to disk. `*.csv` goes in `.gitignore`.
- `person_phone`, `person_linkedin_url`, `employees` are discarded at import.
- Fixed pitch line is at most 45 words, has no dashes, and never claims no human reviewed the email.
- Import send variant string is exactly `import-automation`.
- Drafting is one lead per request, 45 second overall timeout, `/draft` rate limit 12/60s.
- Existing callers of `/api/send` (no `import_lead_id`) must behave byte for byte as before. With `MAILING_ADDRESS` unset, outgoing bodies are byte for byte unchanged.
- Tests are offline: monkeypatch network, DNS, AI providers, SES. Use a temp DB (`monkeypatch` `db.DB_DIR` and `db.DB_PATH`). Give every endpoint test its own `X-API-Key` (rate-limit buckets are keyed by it). Never edit Python files while a background pytest is running.
- GateGuard hook: state the facts before the first Bash/Write/Edit call, then retry. Never set `ECC_GATEGUARD=off`.
- Windows machine: run tests with `python -m pytest ...` from `D:/office work/projects/leads for business/lead-audit-bot`. Use the Write tool, not heredocs, for files with apostrophes.

## Plan-level decisions that refine the spec (applied in Task 8's spec sync)

1. `import_leads` gets a `source_quote TEXT` column (the review drawer shows the homepage evidence quote; the spec's table omitted it).
2. Warning acknowledgement: `POST .../approve` takes `{acknowledge_warnings: bool}` and refuses (409, same shape as audit drafts) a lead with `review_warnings` unless true. The UI forces the checkbox at approve time, so the send loop passes `acknowledge_warnings: true` for approved leads. `/api/send` still enforces the 409 when the flag is false. Editing a lead recomputes warnings and returns it to `drafted`, so it needs a fresh acknowledgement.
3. The UI lives in `frontend/src/ImportView.jsx` (props-free component), not inside `App.jsx`. `App.jsx` only gets the nav button, the view switch case and a help entry.
4. Email body order is: greeting, AI paragraphs, fixed pitch line, sign off (`YOUR_NAME`, default "Kshitij Gupta", as used elsewhere).
5. `discard draft` (drafted to ready) is not a separate endpoint: re-drafting a `drafted`/`failed` lead overwrites the draft.

## File Structure

| File | Responsibility |
|---|---|
| `storage/db.py` (modify) | schema v14 migration |
| `storage/import_store.py` (create) | all SQL for import tables; blockers loader |
| `importer/__init__.py` (create) | package marker |
| `importer/csv_rules.py` (create) | pure CSV parse + skip rules + contact choice |
| `importer/service.py` (create) | upload and status transitions, `FlowError` |
| `importer/homepage.py` (create) | SSRF safe homepage text fetch |
| `importer/compose.py` (create) | fixed line, body assembly, warning checks |
| `importer/drafting.py` (create) | `draft_lead` orchestration |
| `importer/send_policy.py` (create) | send gates + bounce pause |
| `analyzer/ai_audit.py` (modify) | `AIAuditor.generate_import_email` + provider calls |
| `emailer/base_sender.py` (modify) | `_with_footer` |
| `config.py` (modify) | `MAILING_ADDRESS`, `IMPORT_BOUNCE_*` |
| `app.py` (modify) | `SendRequest.import_lead_id`, `/api/send` branches, `/api/import/*` routes |
| `frontend/src/ImportView.jsx` (create) | the Import page |
| `frontend/src/App.jsx` (modify) | nav, view case, help |
| `import_test_support.py` (create) | shared test helpers (not a test file) |
| `test_import_*.py` (create) | tests per task |

---

### Task 1: Schema v14, store, config, gitignore, test helpers

**Files:**
- Modify: `storage/db.py` (after the `schema_version < 13` block, before `conn.commit()` at the end of `init_db`)
- Create: `storage/import_store.py`, `import_test_support.py`, `test_import_store.py`
- Modify: `config.py`, `.gitignore`

**Interfaces:**
- Produces (`storage/import_store.py`):
  - `LEAD_FIELDS: tuple[str, ...]` = `("person_name","person_title","person_seniority","email","organization","domain","industry","city","state","country")`
  - `now_sql() -> str` (UTC `YYYY-MM-DD HH:MM:SS`)
  - `create_batch(filename: str, leads: list[dict]) -> int` (each lead has `LEAD_FIELDS` plus `status`, `skip_reason`)
  - `list_batches() -> list[dict]` (each has `status_counts: dict`)
  - `get_lead(lead_id: int) -> dict | None` (`review_warnings` decoded to a list)
  - `list_leads(batch_id, *, status=None, skip_reason=None, industry=None, seniority=None, state=None, q=None, limit=50, offset=0) -> dict` returning `{"leads": [...], "total": int}`
  - `facets(batch_id) -> dict` with keys `status, skip_reason, industry, seniority, state`
  - `update_lead(lead_id, **fields) -> None` (whitelist: `status, skip_reason, subject, body, review_warnings, source_quote, drafted_at, sent_at, history_id, error`)
  - `load_blockers() -> dict` with set values for keys `imported, suppressed, emailed, company_hosts`
- Produces (`config.py`): `MAILING_ADDRESS: str`, `IMPORT_BOUNCE_PAUSE_PCT: float`, `IMPORT_BOUNCE_MIN_SENT: int`
- Produces (`import_test_support.py`): `use_temp_db(monkeypatch, tmp_path)`, `client_with_key(monkeypatch) -> TestClient`, `make_row(**over) -> dict`, `csv_text(rows) -> str`, `empty_blockers(**sets) -> dict`, `seed_lead(**over) -> int`

- [ ] **Step 1: Write the test helpers**

Create `import_test_support.py`:

```python
"""Shared helpers for the test_import_*.py files. Not itself a test module."""
import csv
import io
import uuid

from fastapi.testclient import TestClient

import app as app_module
from storage import db, import_store

CSV_HEADER = [
    "person_name", "person_title", "person_seniority", "person_email", "person_phone",
    "person_linkedin_url", "organization_name", "domain", "employees", "industries",
    "city", "state", "country",
]


def use_temp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.sqlite"))
    db.init_db()


def client_with_key(monkeypatch):
    """A client whose API key is unique to the test, so rate-limit buckets never collide."""
    key = f"k-{uuid.uuid4().hex}"
    monkeypatch.setattr(app_module.config, "API_KEY", key)
    return TestClient(app_module.app, raise_server_exceptions=False, headers={"X-API-Key": key})


def make_row(**over) -> dict:
    """A parsed-shape lead dict (the keys parse_csv produces)."""
    row = {
        "person_name": "Jane Doe", "person_title": "Chief Compliance Officer",
        "person_seniority": "c_suite", "email": "jane@acme.com",
        "organization": "Acme Bank", "domain": "acme.com", "industry": "banking",
        "city": "Boston", "state": "Massachusetts", "country": "united states",
    }
    row.update(over)
    return row


def csv_text(rows: list[dict]) -> str:
    """Render parsed-shape rows as a CSV shaped like the real export (13 columns)."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_HEADER)
    for r in rows:
        writer.writerow([
            r["person_name"], r["person_title"], r["person_seniority"], r["email"],
            "555 0100", "https://linkedin.example/in/x", r["organization"], r["domain"],
            "120", r["industry"], r["city"], r["state"], r["country"],
        ])
    return buf.getvalue()


def empty_blockers(**sets) -> dict:
    blockers = {"imported": set(), "suppressed": set(), "emailed": set(), "company_hosts": set()}
    blockers.update(sets)
    return blockers


def seed_lead(**over) -> int:
    """Insert one lead (default status ready) in a fresh batch and return its id."""
    row = make_row(**{k: v for k, v in over.items() if k in make_row()})
    row["status"] = over.get("status", "ready")
    row["skip_reason"] = over.get("skip_reason")
    batch_id = import_store.create_batch("seed.csv", [row])
    lead_id = import_store.list_leads(batch_id)["leads"][0]["id"]
    extra = {k: v for k, v in over.items() if k in ("subject", "body", "review_warnings", "source_quote")}
    if extra:
        import_store.update_lead(lead_id, **extra)
    return lead_id
```

- [ ] **Step 2: Write the failing store tests**

Create `test_import_store.py`:

```python
import sqlite3

import pytest

from storage import db, import_store
from import_test_support import make_row, use_temp_db


def _ready(**over):
    row = make_row(**over)
    row.update(status="ready", skip_reason=None)
    return row


def test_init_db_creates_import_tables_and_bumps_to_v14(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    conn = sqlite3.connect(db.DB_PATH)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert {"import_batches", "import_leads"} <= tables
    assert version >= 14


def test_a_v13_database_upgrades_without_losing_history(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    db.log_email("Acme", "acme.com", "a@acme.com", "us@x.com", "S", "B")
    conn = sqlite3.connect(db.DB_PATH)
    conn.execute("DROP TABLE import_leads")
    conn.execute("DROP TABLE import_batches")
    conn.execute("PRAGMA user_version = 13")
    conn.commit()
    conn.close()
    db.init_db()
    assert import_store.list_batches() == []
    assert len(db.get_email_history()) == 1


def test_create_batch_counts_and_list_batches(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    skipped = make_row(email="g@x.gov")
    skipped.update(status="skipped", skip_reason="gov_domain")
    batch_id = import_store.create_batch("f.csv", [_ready(), skipped])
    batches = import_store.list_batches()
    assert batches[0]["id"] == batch_id
    assert (batches[0]["total_rows"], batches[0]["ready_count"], batches[0]["skipped_count"]) == (2, 1, 1)
    assert batches[0]["status_counts"] == {"ready": 1, "skipped": 1}


def test_list_leads_filters_search_and_paging(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    rows = [
        _ready(email="a@a.com", organization="Alpha Bank", industry="banking", state="Ohio"),
        _ready(email="b@b.com", organization="Beta Hospital", industry="hospitals", state="Texas"),
        _ready(email="c@c.com", organization="Gamma Bank", industry="banking", state="Texas"),
    ]
    batch_id = import_store.create_batch("f.csv", rows)
    assert import_store.list_leads(batch_id)["total"] == 3
    assert import_store.list_leads(batch_id, industry="banking")["total"] == 2
    assert import_store.list_leads(batch_id, state="Texas", industry="banking")["total"] == 1
    assert [l["email"] for l in import_store.list_leads(batch_id, q="hospital")["leads"]] == ["b@b.com"]
    page = import_store.list_leads(batch_id, limit=2, offset=2)
    assert page["total"] == 3 and len(page["leads"]) == 1
    assert import_store.facets(batch_id)["industry"] == {"banking": 2, "hospitals": 1}


def test_update_lead_whitelist_and_json_warnings(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    batch_id = import_store.create_batch("f.csv", [_ready()])
    lead_id = import_store.list_leads(batch_id)["leads"][0]["id"]
    import_store.update_lead(lead_id, status="drafted", subject="S", body="B", review_warnings=["w1", "w2"])
    lead = import_store.get_lead(lead_id)
    assert lead["status"] == "drafted" and lead["review_warnings"] == ["w1", "w2"]
    with pytest.raises(ValueError):
        import_store.update_lead(lead_id, email="evil@x.com")
    assert import_store.get_lead(999) is None


def test_load_blockers_reads_suppressions_history_and_sent_hosts(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    db.add_suppression("Out@Out.com", "unsub")
    db.log_email("Sent Co", "https://www.sentco.com/", "Hi@SentCo.com", "us@x.com", "S", "B")
    import_store.create_batch("f.csv", [_ready(email="Prev@Prev.com")])
    blockers = import_store.load_blockers()
    assert "out@out.com" in blockers["suppressed"]
    assert "hi@sentco.com" in blockers["emailed"]
    assert "sentco.com" in blockers["company_hosts"]
    assert "prev@prev.com" in blockers["imported"]
```

- [ ] **Step 3: Run to verify failure**

Run: `python -m pytest test_import_store.py -v`
Expected: FAIL (`ModuleNotFoundError: storage.import_store`).

- [ ] **Step 4: Add the schema migration**

In `storage/db.py`, insert immediately before the final `conn.commit()` / `conn.close()` of `init_db()` (right after the `schema_version < 13` block):

```python
    # Schema v14 (2026-10-15): CSV import outreach queue. A batch is one
    # uploaded file; a lead is one row after validation, carrying its status
    # through ready -> drafted -> approved -> sent. person_phone,
    # person_linkedin_url and employees are deliberately not stored (data
    # minimisation: nothing in this feature uses them). See
    # docs/superpowers/specs/2026-10-15-csv-import-outreach-design.md.
    if schema_version < 14:
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS import_batches (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
            filename      TEXT,
            total_rows    INTEGER NOT NULL,
            ready_count   INTEGER NOT NULL,
            skipped_count INTEGER NOT NULL
        )
        """)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS import_leads (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id         INTEGER NOT NULL,
            person_name      TEXT,
            person_title     TEXT,
            person_seniority TEXT,
            email            TEXT NOT NULL,
            organization     TEXT,
            domain           TEXT,
            industry         TEXT,
            city             TEXT,
            state            TEXT,
            country          TEXT,
            status           TEXT NOT NULL,
            skip_reason      TEXT,
            subject          TEXT,
            body             TEXT,
            review_warnings  TEXT,
            source_quote     TEXT,
            drafted_at       DATETIME,
            sent_at          DATETIME,
            history_id       INTEGER,
            error            TEXT,
            FOREIGN KEY (batch_id) REFERENCES import_batches(id)
        )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_import_leads_batch_status ON import_leads(batch_id, status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_import_leads_email ON import_leads(email)")
        cursor.execute("PRAGMA user_version = 14")

```

- [ ] **Step 5: Add the store module**

Create `storage/import_store.py`:

```python
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


def update_lead(lead_id: int, **fields) -> None:
    bad = set(fields) - _UPDATABLE
    if bad:
        raise ValueError(f"Not updatable: {sorted(bad)}")
    if not fields:
        return
    if "review_warnings" in fields and not isinstance(fields["review_warnings"], str):
        fields["review_warnings"] = json.dumps(fields["review_warnings"] or [])
    # Column names come from the whitelist above, never from the caller's input.
    sets = ", ".join(f"{name} = ?" for name in fields)
    conn = _connect()
    try:
        conn.execute(f"UPDATE import_leads SET {sets} WHERE id = ?", (*fields.values(), lead_id))
        conn.commit()
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
```

- [ ] **Step 6: Add config values and gitignore**

Append to `config.py` (after `DRAFT_STALE_DAYS`, or at the end of the sending-related block):

```python
# === CSV import outreach (2026-10-15) ===
# Physical postal address appended to every outgoing email's footer. CAN-SPAM
# requires one on commercial email to US recipients; nothing in an email body
# carried one before. Unset means emails are byte for byte what they were, and
# imported US leads cannot be sent (the send route returns 409 saying why).
MAILING_ADDRESS = os.getenv("MAILING_ADDRESS", "").strip()
# Pause import sending when the bounce rate across import-automation sends
# exceeds this many percent, once at least IMPORT_BOUNCE_MIN_SENT have gone out.
IMPORT_BOUNCE_PAUSE_PCT = float(os.getenv("IMPORT_BOUNCE_PAUSE_PCT", "5"))
IMPORT_BOUNCE_MIN_SENT = int(os.getenv("IMPORT_BOUNCE_MIN_SENT", "20"))
```

Append to `.gitignore` the two lines: `# lead lists hold personal data, never commit them` and `*.csv`.

- [ ] **Step 7: Run tests**

Run: `python -m pytest test_import_store.py -v`
Expected: PASS (6 tests).

- [ ] **Step 8: Commit**

```bash
git add storage/db.py storage/import_store.py config.py .gitignore import_test_support.py test_import_store.py
git commit -m "feat: schema v14 and store for the CSV import queue"
```

---

### Task 2: CSV parser and skip rules (pure functions)

**Files:**
- Create: `importer/__init__.py` (empty), `importer/csv_rules.py`, `test_import_rules.py`

**Interfaces:**
- Consumes: `storage.import_store.LEAD_FIELDS`, blockers dict shape from `load_blockers()`.
- Produces (`importer/csv_rules.py`):
  - `SUPPORTED_COUNTRIES = ("united states", "india")`, `MAX_CSV_BYTES = 5*1024*1024`, `MAX_ROWS = 20_000`
  - `RESTORABLE_REASONS = frozenset({"gov_domain","second_contact","manual"})`
  - `class CsvImportError(ValueError)` with `.status_code` (400 or 413)
  - `parse_csv(text: str) -> list[dict]` (keys exactly `LEAD_FIELDS`; email lowercased; country lowercased; domain cleaned)
  - `company_key(lead: dict) -> str`
  - `recheck(lead: dict, blockers: dict) -> str | None` (rules 5, 6, 7 only)
  - `apply_rules(rows: list[dict], blockers: dict) -> list[dict]` (adds `status` and `skip_reason`)
  - `LEAD_FIELDS` re-exported

- [ ] **Step 1: Write the failing tests**

Create `test_import_rules.py`:

```python
from collections import Counter

import pytest

from importer import csv_rules
from import_test_support import csv_text, empty_blockers, make_row


def _run(rows, **sets):
    return csv_rules.apply_rules(rows, empty_blockers(**sets))


def test_parse_handles_bom_quoted_commas_and_alias_headers():
    text = (
        "\ufeffEmail,Name,Job Title,Company,Website,Country\n"
        '"jane@acme.com","Jane Doe","Chief Operating Officer, Chief Compliance Officer",'
        "Acme Bank,https://www.acme.com/about,United States\n"
    )
    rows = csv_rules.parse_csv(text)
    assert len(rows) == 1
    r = rows[0]
    assert r["email"] == "jane@acme.com"
    assert r["person_title"] == "Chief Operating Officer, Chief Compliance Officer"
    assert r["organization"] == "Acme Bank"
    assert r["domain"] == "acme.com"
    assert r["country"] == "united states"


def test_parse_discards_phone_linkedin_and_employees():
    rows = csv_rules.parse_csv(csv_text([make_row()]))
    assert set(rows[0]) == set(csv_rules.LEAD_FIELDS)


def test_parse_joins_first_and_last_name_when_there_is_no_name_column():
    rows = csv_rules.parse_csv("first_name,last_name,email\nJane,Doe,j@a.com\n")
    assert rows[0]["person_name"] == "Jane Doe"


def test_parse_rejects_a_file_without_an_email_column():
    with pytest.raises(csv_rules.CsvImportError) as exc:
        csv_rules.parse_csv("name,company\nJane,Acme\n")
    assert "email" in str(exc.value).lower()
    assert exc.value.status_code == 400


def test_parse_rejects_an_empty_file():
    with pytest.raises(csv_rules.CsvImportError):
        csv_rules.parse_csv("")


def test_parse_enforces_row_and_size_limits_with_413(monkeypatch):
    monkeypatch.setattr(csv_rules, "MAX_ROWS", 2)
    with pytest.raises(csv_rules.CsvImportError) as exc:
        csv_rules.parse_csv("email\na@a.com\nb@b.com\nc@c.com\n")
    assert exc.value.status_code == 413
    monkeypatch.setattr(csv_rules, "MAX_ROWS", 20_000)
    monkeypatch.setattr(csv_rules, "MAX_CSV_BYTES", 10)
    with pytest.raises(csv_rules.CsvImportError) as exc:
        csv_rules.parse_csv("email\n" + "a@a.com\n" * 5)
    assert exc.value.status_code == 413


def test_each_skip_reason_fires_in_priority_order():
    rows = [
        make_row(email="not-an-email"),
        make_row(email="a@a.com", country="france"),
        make_row(email="dup@d.com", domain="d.com"),
        make_row(email="dup@d.com", domain="d2.com"),
        make_row(email="old@o.com", domain="o.com"),
        make_row(email="sup@s.com", domain="s.com"),
        make_row(email="sent@t.com", domain="t.com"),
        make_row(email="new@u.com", domain="u.com"),
        make_row(email="g@treasury.gov", domain="treasury.gov"),
        make_row(email="ok@ok.com", domain="ok.com"),
    ]
    out = _run(
        rows,
        imported={"old@o.com"}, suppressed={"sup@s.com"},
        emailed={"sent@t.com"}, company_hosts={"u.com"},
    )
    assert [r["skip_reason"] for r in out] == [
        "invalid_email", "unsupported_country", None, "duplicate_in_file",
        "already_imported", "suppressed", "already_emailed",
        "company_already_emailed", "gov_domain", None,
    ]
    assert [r["status"] for r in out].count("ready") == 2


def test_india_is_a_supported_country():
    assert _run([make_row(country="india")])[0]["status"] == "ready"


def test_one_contact_per_company_prefers_seniority_then_compliance_title():
    rows = [
        make_row(email="m@x.com", domain="x.com", person_seniority="manager", person_title="Compliance Manager"),
        make_row(email="c@x.com", domain="x.com", person_seniority="c_suite", person_title="Chief Compliance Officer"),
        make_row(email="v@x.com", domain="x.com", person_seniority="vp", person_title="VP Compliance"),
    ]
    out = _run(rows)
    assert [(r["email"], r["skip_reason"]) for r in out] == [
        ("m@x.com", "second_contact"), ("c@x.com", None), ("v@x.com", "second_contact"),
    ]


def test_contact_ties_go_to_file_order():
    rows = [
        make_row(email="first@x.com", domain="x.com", person_seniority="director", person_title="Director"),
        make_row(email="second@x.com", domain="x.com", person_seniority="director", person_title="Director"),
    ]
    out = _run(rows)
    assert [r["skip_reason"] for r in out] == [None, "second_contact"]


def test_blank_domain_falls_back_to_the_email_domain():
    rows = [
        make_row(email="a@shared.org", domain=""),
        make_row(email="b@shared.org", domain="", person_seniority="manager"),
    ]
    out = _run(rows)
    assert [r["skip_reason"] for r in out] == [None, "second_contact"]


def test_recheck_only_covers_state_dependent_rules():
    lead = make_row(email="g@treasury.gov", domain="treasury.gov")
    assert csv_rules.recheck(lead, empty_blockers()) is None
    assert csv_rules.recheck(lead, empty_blockers(suppressed={"g@treasury.gov"})) == "suppressed"
    assert csv_rules.recheck(lead, empty_blockers(emailed={"g@treasury.gov"})) == "already_emailed"
    assert csv_rules.recheck(lead, empty_blockers(company_hosts={"treasury.gov"})) == "company_already_emailed"


def test_restorable_reasons():
    assert csv_rules.RESTORABLE_REASONS == {"gov_domain", "second_contact", "manual"}


def test_real_file_shape_yields_890_ready_44_gov_66_second_contacts():
    rows = []
    for i in range(890):
        rows.append(make_row(email=f"cco{i}@bank{i}.com", domain=f"bank{i}.com"))
    for i in range(66):
        rows.append(make_row(
            email=f"mgr{i}@bank{i}.com", domain=f"bank{i}.com",
            person_seniority="manager", person_title="Manager",
        ))
    for i in range(44):
        rows.append(make_row(email=f"o{i}@agency{i}.gov", domain=f"agency{i}.gov"))
    parsed = csv_rules.parse_csv(csv_text(rows))
    assert len(parsed) == 1000
    out = csv_rules.apply_rules(parsed, empty_blockers())
    counts = Counter(r["skip_reason"] or "ready" for r in out)
    assert counts == {"ready": 890, "second_contact": 66, "gov_domain": 44}
    assert all("person_phone" not in r for r in out)
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest test_import_rules.py -v`
Expected: FAIL (`No module named importer`).

- [ ] **Step 3: Implement**

Create empty `importer/__init__.py`, then `importer/csv_rules.py`:

```python
"""
CSV parsing and the skip rules for the import outreach queue. Pure functions:
no database, no network. The caller supplies the outside world as a
`blockers` dict (see storage.import_store.load_blockers).

Rules run in a fixed order and the first failure sets the skip reason; the
design and the reasoning are in
docs/superpowers/specs/2026-10-15-csv-import-outreach-design.md section 7.
"""
import csv
import io
import re

from storage.import_store import LEAD_FIELDS  # noqa: F401  (re-exported for callers and tests)

SUPPORTED_COUNTRIES = ("united states", "india")
MAX_CSV_BYTES = 5 * 1024 * 1024
MAX_ROWS = 20_000
RESTORABLE_REASONS = frozenset({"gov_domain", "second_contact", "manual"})

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# canonical field -> accepted header names (after lowercasing and replacing
# spaces and hyphens with underscores).
_ALIASES = {
    "email": ("person_email", "email", "email_address", "e_mail", "work_email"),
    "person_name": ("person_name", "name", "full_name", "contact_name"),
    "first_name": ("first_name",),
    "last_name": ("last_name",),
    "person_title": ("person_title", "title", "job_title"),
    "person_seniority": ("person_seniority", "seniority"),
    "organization": ("organization_name", "organization", "company", "company_name"),
    "domain": ("domain", "website", "company_domain", "organization_domain"),
    "industry": ("industries", "industry"),
    "city": ("city",),
    "state": ("state", "region"),
    "country": ("country",),
}

_SENIORITY_RANK = {
    "c_suite": 6, "owner": 6, "founder": 6, "vp": 5, "director": 4,
    "partner": 4, "senior": 3, "manager": 2, "entry": 1,
}


class CsvImportError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _norm_header(header: str) -> str:
    return re.sub(r"[\s\-]+", "_", header.strip().lower())


def _clean_domain(value: str) -> str:
    v = (value or "").strip().lower()
    v = re.sub(r"^[a-z][a-z0-9+.\-]*://", "", v)
    v = v.split("/")[0].split("?")[0].split(":")[0]
    return v[4:] if v.startswith("www.") else v


def parse_csv(text: str) -> list[dict]:
    if len(text.encode("utf-8")) > MAX_CSV_BYTES:
        raise CsvImportError(f"File is larger than {MAX_CSV_BYTES // (1024 * 1024)} MB.", 413)
    text = text.lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise CsvImportError("The file is empty.")
    by_norm = {_norm_header(h): h for h in reader.fieldnames if h}
    header_map = {}
    for canonical, names in _ALIASES.items():
        for name in names:
            if name in by_norm:
                header_map[canonical] = by_norm[name]
                break
    if "email" not in header_map:
        raise CsvImportError("No email column found. Expected a header such as person_email or email.")

    rows = []
    for raw in reader:
        got = {c: (raw.get(h) or "").strip() for c, h in header_map.items()}
        if not any(got.values()):
            continue
        if len(rows) >= MAX_ROWS:
            raise CsvImportError(f"Too many rows (limit {MAX_ROWS}).", 413)
        name = got.get("person_name") or " ".join(
            p for p in (got.get("first_name"), got.get("last_name")) if p
        )
        rows.append({
            "person_name": name,
            "person_title": got.get("person_title", ""),
            "person_seniority": got.get("person_seniority", "").lower(),
            "email": got.get("email", "").lower(),
            "organization": got.get("organization", ""),
            "domain": _clean_domain(got.get("domain", "")),
            "industry": got.get("industry", ""),
            "city": got.get("city", ""),
            "state": got.get("state", ""),
            "country": got.get("country", "").lower(),
        })
    if not rows:
        raise CsvImportError("The file has no data rows.")
    return rows


def _valid_email(email: str) -> bool:
    return bool(email) and len(email) <= 254 and bool(_EMAIL_RE.match(email))


def company_key(lead: dict) -> str:
    """The company's domain, or the email's own domain when the column is blank."""
    domain = (lead.get("domain") or "").strip().lower()
    if domain:
        return domain
    email = (lead.get("email") or "").lower()
    return email.rsplit("@", 1)[-1] if "@" in email else ""


def recheck(lead: dict, blockers: dict) -> str | None:
    """Rules 5 to 7: the ones that depend on state that changes over time."""
    email = (lead.get("email") or "").lower()
    if email in blockers["suppressed"]:
        return "suppressed"
    if email in blockers["emailed"]:
        return "already_emailed"
    if company_key(lead) in blockers["company_hosts"]:
        return "company_already_emailed"
    return None


def _is_gov(lead: dict) -> bool:
    email_domain = lead["email"].rsplit("@", 1)[-1]
    return any(d.endswith((".gov", ".mil")) for d in (company_key(lead), email_domain))


def _first_failure(lead: dict, seen: set, blockers: dict) -> str | None:
    email = lead["email"]
    if not _valid_email(email):
        return "invalid_email"
    duplicate = email in seen
    seen.add(email)
    if lead["country"] not in SUPPORTED_COUNTRIES:
        return "unsupported_country"
    if duplicate:
        return "duplicate_in_file"
    if email in blockers["imported"]:
        return "already_imported"
    reason = recheck(lead, blockers)
    if reason:
        return reason
    if _is_gov(lead):
        return "gov_domain"
    return None


def _contact_score(lead: dict) -> int:
    rank = _SENIORITY_RANK.get((lead.get("person_seniority") or "").strip().lower(), 0)
    title = (lead.get("person_title") or "").lower()
    if "chief compliance officer" in title:
        bonus = 2
    elif "compliance" in title:
        bonus = 1
    else:
        bonus = 0
    return rank * 10 + bonus


def apply_rules(rows: list[dict], blockers: dict) -> list[dict]:
    out, seen = [], set()
    for row in rows:
        lead = dict(row)
        reason = _first_failure(lead, seen, blockers)
        lead["status"] = "skipped" if reason else "ready"
        lead["skip_reason"] = reason
        out.append(lead)

    # Rule 9: one contact per company among the rows still standing. Highest
    # score wins; a tie goes to whichever came first in the file.
    best: dict[str, int] = {}
    for index, lead in enumerate(out):
        if lead["status"] != "ready":
            continue
        key = company_key(lead)
        if key not in best or _contact_score(lead) > _contact_score(out[best[key]]):
            best[key] = index
    for index, lead in enumerate(out):
        if lead["status"] == "ready" and best[company_key(lead)] != index:
            lead["status"] = "skipped"
            lead["skip_reason"] = "second_contact"
    return out
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest test_import_rules.py -v`
Expected: PASS (14 tests).

- [ ] **Step 5: Commit**

```bash
git add importer/__init__.py importer/csv_rules.py test_import_rules.py
git commit -m "feat: CSV parser and skip rules for the import queue"
```

---

### Task 3: Service layer and the list/transition routes

**Files:**
- Create: `importer/service.py`, `test_import_service.py`, `test_import_endpoints.py`
- Modify: `app.py` (imports near line 42; Pydantic models and routes inserted directly above the `# Mount screenshots folder` comment)

**Interfaces:**
- Consumes: `csv_rules` (Task 2), `import_store` (Task 1).
- Produces (`importer/service.py`):
  - `class FlowError(Exception)` with `.status_code`, `.detail` (str or dict)
  - `upload(filename: str, csv_text: str) -> dict` returning `{batch_id, total, ready, skipped, skip_breakdown, us_ready, mailing_address_set}`
  - `restore(lead_id) -> dict`, `skip(lead_id) -> dict`, `approve(lead_id, acknowledge_warnings: bool) -> dict`, `unapprove(lead_id) -> dict` (each returns the updated lead)
- Produces (`app.py`): functions `import_upload`, `import_batches`, `import_batch_leads`, `import_restore`, `import_skip`, `import_approve`, `import_unapprove`, helper `_import_call(fn, *args)`, models `ImportUploadIn`, `ImportApproveIn`.

- [ ] **Step 1: Write the failing service tests**

Create `test_import_service.py`:

```python
import pytest

from importer import service
from storage import db, import_store
from import_test_support import csv_text, make_row, seed_lead, use_temp_db


def test_upload_stores_batch_and_reports_breakdown(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    monkeypatch.setattr(service.config, "MAILING_ADDRESS", "")
    rows = [
        make_row(email="a@a.com", domain="a.com"),
        make_row(email="b@b.gov", domain="b.gov"),
        make_row(email="bad"),
    ]
    summary = service.upload("x.csv", csv_text(rows))
    assert (summary["total"], summary["ready"], summary["skipped"]) == (3, 1, 2)
    assert summary["skip_breakdown"] == {"gov_domain": 1, "invalid_email": 1}
    assert summary["us_ready"] == 1
    assert summary["mailing_address_set"] is False
    assert import_store.list_leads(summary["batch_id"])["leads"][0]["status"] == "ready"


def test_uploading_a_bad_file_raises_a_flow_error_and_stores_nothing(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    with pytest.raises(service.FlowError) as exc:
        service.upload("x.csv", "name,company\nJane,Acme\n")
    assert exc.value.status_code == 400
    assert import_store.list_batches() == []


def test_reuploading_the_same_file_skips_everything_as_already_imported(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    text = csv_text([make_row(email="a@a.com", domain="a.com")])
    service.upload("x.csv", text)
    second = service.upload("x.csv", text)
    assert second["ready"] == 0
    assert second["skip_breakdown"] == {"already_imported": 1}


def test_restore_a_government_lead_then_it_is_ready(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    summary = service.upload("x.csv", csv_text([make_row(email="g@b.gov", domain="b.gov")]))
    lead = import_store.list_leads(summary["batch_id"])["leads"][0]
    restored = service.restore(lead["id"])
    assert restored["status"] == "ready" and restored["skip_reason"] is None


def test_restore_rechecks_state_that_changed_after_import(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    summary = service.upload("x.csv", csv_text([make_row(email="g@b.gov", domain="b.gov")]))
    lead_id = import_store.list_leads(summary["batch_id"])["leads"][0]["id"]
    db.add_suppression("g@b.gov", "unsub")
    with pytest.raises(service.FlowError) as exc:
        service.restore(lead_id)
    assert exc.value.status_code == 409
    assert import_store.get_lead(lead_id)["skip_reason"] == "suppressed"


def test_restore_refuses_a_non_restorable_skip(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    lead_id = seed_lead(status="skipped", skip_reason="invalid_email")
    with pytest.raises(service.FlowError) as exc:
        service.restore(lead_id)
    assert exc.value.status_code == 409


def test_manual_skip_is_restorable(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    lead_id = seed_lead()
    assert service.skip(lead_id)["skip_reason"] == "manual"
    assert service.restore(lead_id)["status"] == "ready"


def test_unknown_lead_is_404(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    with pytest.raises(service.FlowError) as exc:
        service.restore(12345)
    assert exc.value.status_code == 404


def test_approve_requires_a_draft_and_unapprove_reverses(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    lead_id = seed_lead()
    with pytest.raises(service.FlowError):
        service.approve(lead_id, acknowledge_warnings=False)
    import_store.update_lead(lead_id, status="drafted", subject="S", body="B")
    assert service.approve(lead_id, acknowledge_warnings=False)["status"] == "approved"
    assert service.unapprove(lead_id)["status"] == "drafted"
    with pytest.raises(service.FlowError):
        service.unapprove(lead_id)


def test_approving_a_warned_draft_needs_the_acknowledgement(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    lead_id = seed_lead(status="drafted", subject="S", body="B", review_warnings=["number not in source"])
    with pytest.raises(service.FlowError) as exc:
        service.approve(lead_id, acknowledge_warnings=False)
    assert exc.value.status_code == 409
    assert exc.value.detail["warnings"] == ["number not in source"]
    assert service.approve(lead_id, acknowledge_warnings=True)["status"] == "approved"


def test_a_failed_send_can_be_re_approved_for_retry(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    lead_id = seed_lead(status="failed", subject="S", body="B")
    assert service.approve(lead_id, acknowledge_warnings=False)["status"] == "approved"
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest test_import_service.py -v`
Expected: FAIL (`No module named importer.service`).

- [ ] **Step 3: Implement the service**

Create `importer/service.py`:

```python
"""Orchestration for the CSV import queue: upload and the status transitions."""
from collections import Counter

import config
from importer import csv_rules
from storage import import_store


class FlowError(Exception):
    """A user-facing failure carrying the HTTP status the route should return."""

    def __init__(self, status_code: int, detail):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


def _load(lead_id: int) -> dict:
    lead = import_store.get_lead(lead_id)
    if lead is None:
        raise FlowError(404, "Import lead not found")
    return lead


def upload(filename: str, csv_text: str) -> dict:
    try:
        rows = csv_rules.parse_csv(csv_text)
    except csv_rules.CsvImportError as e:
        raise FlowError(e.status_code, str(e)) from e
    leads = csv_rules.apply_rules(rows, import_store.load_blockers())
    batch_id = import_store.create_batch(filename or "upload.csv", leads)
    ready = [l for l in leads if l["status"] == "ready"]
    return {
        "batch_id": batch_id,
        "total": len(leads),
        "ready": len(ready),
        "skipped": len(leads) - len(ready),
        "skip_breakdown": dict(Counter(l["skip_reason"] for l in leads if l["status"] == "skipped")),
        "us_ready": sum(1 for l in ready if l["country"] == "united states"),
        "mailing_address_set": bool(config.MAILING_ADDRESS),
    }


def restore(lead_id: int) -> dict:
    lead = _load(lead_id)
    if lead["status"] != "skipped" or lead["skip_reason"] not in csv_rules.RESTORABLE_REASONS:
        raise FlowError(409, "Only government, second contact or manually skipped leads can be restored.")
    reason = csv_rules.recheck(lead, import_store.load_blockers())
    if reason:
        import_store.update_lead(lead_id, skip_reason=reason)
        raise FlowError(409, f"Cannot restore: this lead is now blocked ({reason}).")
    import_store.update_lead(lead_id, status="ready", skip_reason=None)
    return import_store.get_lead(lead_id)


def skip(lead_id: int) -> dict:
    lead = _load(lead_id)
    if lead["status"] not in ("ready", "drafted", "approved", "failed"):
        raise FlowError(409, f"A {lead['status']} lead cannot be skipped.")
    import_store.update_lead(lead_id, status="skipped", skip_reason="manual")
    return import_store.get_lead(lead_id)


def approve(lead_id: int, acknowledge_warnings: bool) -> dict:
    lead = _load(lead_id)
    if lead["status"] not in ("drafted", "failed") or not (lead.get("subject") and lead.get("body")):
        raise FlowError(409, "Only a drafted lead with a subject and body can be approved.")
    if lead["review_warnings"] and not acknowledge_warnings:
        raise FlowError(409, {
            "message": "This draft was flagged during review and hasn't been acknowledged yet.",
            "warnings": lead["review_warnings"],
            "resend_with": "acknowledge_warnings: true",
        })
    import_store.update_lead(lead_id, status="approved", error=None)
    return import_store.get_lead(lead_id)


def unapprove(lead_id: int) -> dict:
    lead = _load(lead_id)
    if lead["status"] != "approved":
        raise FlowError(409, "Only an approved lead can be un-approved.")
    import_store.update_lead(lead_id, status="drafted")
    return import_store.get_lead(lead_id)
```

- [ ] **Step 4: Run service tests**

Run: `python -m pytest test_import_service.py -v`
Expected: PASS (11 tests).

- [ ] **Step 5: Write the failing endpoint tests**

Create `test_import_endpoints.py`:

```python
import inspect

from fastapi.testclient import TestClient

import app as app_module
from storage import import_store
from import_test_support import client_with_key, csv_text, make_row, seed_lead, use_temp_db

ROUTE_FUNCS = [
    "import_upload", "import_batches", "import_batch_leads", "import_restore",
    "import_skip", "import_approve", "import_unapprove",
]


def test_every_import_route_is_key_gated_and_rate_limited():
    for name in ROUTE_FUNCS:
        src = inspect.getsource(getattr(app_module, name))
        assert "require_api_key" in src, name
        assert "rate_limit" in src, name


def test_import_routes_need_the_api_key(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client_with_key(monkeypatch)   # sets a real API_KEY for the app
    bare = TestClient(app_module.app, raise_server_exceptions=False)
    assert bare.get("/api/import/batches").status_code == 401
    assert bare.post("/api/import/upload", json={"csv_text": "email\na@a.com\n"}).status_code == 401


def test_upload_list_and_restore_flow(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)
    rows = [make_row(email="a@a.com", domain="a.com"), make_row(email="g@b.gov", domain="b.gov")]
    r = client.post("/api/import/upload", json={"filename": "f.csv", "csv_text": csv_text(rows)})
    assert r.status_code == 200, r.text
    summary = r.json()
    assert summary["ready"] == 1 and summary["skip_breakdown"] == {"gov_domain": 1}

    r = client.get("/api/import/batches")
    assert r.json()["batches"][0]["id"] == summary["batch_id"]

    r = client.get(f"/api/import/batches/{summary['batch_id']}/leads", params={"status": "skipped"})
    body = r.json()
    assert body["total"] == 1 and "facets" in body
    gov_id = body["leads"][0]["id"]

    r = client.post(f"/api/import/leads/{gov_id}/restore")
    assert r.status_code == 200 and r.json()["lead"]["status"] == "ready"
    assert client.post(f"/api/import/leads/{gov_id}/skip").json()["lead"]["skip_reason"] == "manual"


def test_upload_errors_map_to_http_status(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)
    assert client.post("/api/import/upload", json={"csv_text": "name\nx\n"}).status_code == 400
    monkeypatch.setattr(app_module.import_service.csv_rules, "MAX_ROWS", 1)
    r = client.post("/api/import/upload", json={"csv_text": "email\na@a.com\nb@b.com\n"})
    assert r.status_code == 413


def test_leads_listing_caps_the_page_size(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)
    lead_id = seed_lead()
    batch_id = import_store.get_lead(lead_id)["batch_id"]
    r = client.get(f"/api/import/batches/{batch_id}/leads", params={"limit": 5000})
    assert r.status_code == 422


def test_unknown_lead_is_404_and_approve_flow(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)
    assert client.post("/api/import/leads/999/restore").status_code == 404
    lead_id = seed_lead(status="drafted", subject="S", body="B", review_warnings=["w"])
    r = client.post(f"/api/import/leads/{lead_id}/approve", json={"acknowledge_warnings": False})
    assert r.status_code == 409 and r.json()["detail"]["warnings"] == ["w"]
    r = client.post(f"/api/import/leads/{lead_id}/approve", json={"acknowledge_warnings": True})
    assert r.status_code == 200 and r.json()["lead"]["status"] == "approved"
    r = client.post(f"/api/import/leads/{lead_id}/unapprove")
    assert r.json()["lead"]["status"] == "drafted"
```

- [ ] **Step 6: Add the routes to `app.py`**

Add to the imports near `from storage import db` (line ~42):

```python
from storage import import_store
from importer import service as import_service
```

Change line 9 to include `Query`: `from fastapi import FastAPI, HTTPException, BackgroundTasks, Header, Depends, Request, Query`.

Insert directly above the `# Mount screenshots folder` comment at the bottom (routes registered after the `/` static mount would never be reached):

```python
# ---------------------------------------------------------------------------
# CSV import outreach (2026-10-15). Logic lives in importer/ and
# storage/import_store.py; these are thin routes. Design:
# docs/superpowers/specs/2026-10-15-csv-import-outreach-design.md
# Keep this block above the "/" static mount below: a route registered after
# that mount is never reached.
# ---------------------------------------------------------------------------

class ImportUploadIn(BaseModel):
    filename: str = Field(default="upload.csv", max_length=255)
    csv_text: str


class ImportApproveIn(BaseModel):
    acknowledge_warnings: bool = False


async def _import_call(fn, *args):
    """Run a blocking service call off the event loop and map FlowError to HTTP."""
    try:
        return await asyncio.to_thread(fn, *args)
    except import_service.FlowError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@app.post("/api/import/upload")
async def import_upload(
    req: ImportUploadIn,
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(5, 60)),
):
    return await _import_call(import_service.upload, req.filename, req.csv_text)


@app.get("/api/import/batches")
async def import_batches(
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(60, 60)),
):
    return {"batches": await asyncio.to_thread(import_store.list_batches)}


@app.get("/api/import/batches/{batch_id}/leads")
async def import_batch_leads(
    batch_id: int,
    status: str | None = None,
    skip_reason: str | None = None,
    industry: str | None = None,
    seniority: str | None = None,
    state: str | None = None,
    q: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(120, 60)),
):
    def _load():
        page = import_store.list_leads(
            batch_id, status=status, skip_reason=skip_reason, industry=industry,
            seniority=seniority, state=state, q=q, limit=limit, offset=offset,
        )
        page["facets"] = import_store.facets(batch_id)
        return page
    return await asyncio.to_thread(_load)


@app.post("/api/import/leads/{lead_id}/restore")
async def import_restore(
    lead_id: int,
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(120, 60)),
):
    return {"lead": await _import_call(import_service.restore, lead_id)}


@app.post("/api/import/leads/{lead_id}/skip")
async def import_skip(
    lead_id: int,
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(120, 60)),
):
    return {"lead": await _import_call(import_service.skip, lead_id)}


@app.post("/api/import/leads/{lead_id}/approve")
async def import_approve(
    lead_id: int,
    req: ImportApproveIn,
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(120, 60)),
):
    return {"lead": await _import_call(import_service.approve, lead_id, req.acknowledge_warnings)}


@app.post("/api/import/leads/{lead_id}/unapprove")
async def import_unapprove(
    lead_id: int,
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(120, 60)),
):
    return {"lead": await _import_call(import_service.unapprove, lead_id)}


```

- [ ] **Step 7: Run tests**

Run: `python -m pytest test_import_service.py test_import_endpoints.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add importer/service.py app.py test_import_service.py test_import_endpoints.py
git commit -m "feat: import upload, list and status transition routes"
```

---

### Task 4: Drafting (homepage fetch, composition, checks, AI, draft and edit routes)

**Files:**
- Create: `importer/homepage.py`, `importer/compose.py`, `importer/drafting.py`, `test_import_homepage.py`, `test_import_compose.py`, `test_import_drafting.py`
- Modify: `analyzer/ai_audit.py` (add `_IMPORT_EMAIL_SCHEMA` next to `_FOLLOWUP_RESPONSE_SCHEMA`; append methods to the end of class `AIAuditor`), `importer/service.py` (add `save_edit`), `app.py` (routes `import_draft`, `import_edit`, model `ImportEditIn`, imports), `test_import_endpoints.py` (extend `ROUTE_FUNCS`)

**Interfaces:**
- Consumes: `AIAuditor` static checkers (`_check_spam_trigger_words`, `_check_ai_sounding_phrases`, `_check_forbidden_dashes`, `_check_number_hallucination`, `_verify_source_quotes`, all `(parsed, ..., company) -> str | None`), `security_utils.validate_public_url`, `import_store`.
- Produces:
  - `homepage.fetch_homepage_text(domain: str, get=_http_get) -> str | None`, `homepage.MAX_REDIRECTS`, `homepage.MAX_TEXT_CHARS`
  - `compose.FIXED_LINE: str`, `compose.compose_body(person_name, paragraphs, your_name) -> str`, `compose.facts_text(lead) -> str`, `compose.is_evidence_warning(w: str) -> bool`, `compose.run_checks(subject, body, *, company, facts_text, homepage_text, source_quote, check_evidence=True, carried_warnings=()) -> list[str]`
  - `AIAuditor.generate_import_email(lead: dict, homepage_text: str | None, your_name: str) -> dict | None` returning `{"subject","paragraphs","source_quote","ai_cost"}`; `AIAuditor._build_import_prompt`, `_parse_import_json`
  - `drafting.DraftError`, `drafting.draft_lead(lead, auditor, *, your_name, fetch=homepage.fetch_homepage_text) -> dict` returning `{"subject","body","review_warnings","source_quote","ai_cost"}`
  - `service.save_edit(lead_id, subject, body) -> dict`
  - Routes `POST /api/import/leads/{id}/draft` (12/60), `PUT /api/import/leads/{id}` (120/60); module constant `app.IMPORT_DRAFT_TIMEOUT_SECONDS = 45`; module alias `app.import_drafting`.

- [ ] **Step 1: Write failing homepage tests**

Create `test_import_homepage.py`:

```python
from importer import homepage
from security_utils import UnsafeURLError


def _ok_validate(monkeypatch):
    monkeypatch.setattr(homepage, "validate_public_url", lambda url: None)
    monkeypatch.setattr(homepage.trafilatura, "extract", lambda html: "Acme Bank serves families. " * 400)


def test_returns_truncated_clean_text(monkeypatch):
    _ok_validate(monkeypatch)
    text = homepage.fetch_homepage_text("acme.com", get=lambda url: (200, {}, b"<html>x</html>"))
    assert text.startswith("Acme Bank serves families.")
    assert len(text) <= homepage.MAX_TEXT_CHARS


def test_unsafe_url_returns_none_without_fetching(monkeypatch):
    def refuse(url):
        raise UnsafeURLError("private")
    monkeypatch.setattr(homepage, "validate_public_url", refuse)
    called = []
    assert homepage.fetch_homepage_text("10.0.0.1", get=lambda u: called.append(u)) is None
    assert called == []


def test_a_redirect_to_a_private_address_is_refused(monkeypatch):
    def validate(url):
        if "10.0.0.5" in url:
            raise UnsafeURLError("private")
    monkeypatch.setattr(homepage, "validate_public_url", validate)
    calls = []

    def get(url):
        calls.append(url)
        return 302, {"location": "http://10.0.0.5/admin"}, b""

    assert homepage.fetch_homepage_text("acme.com", get=get) is None
    assert calls == ["https://acme.com"]


def test_redirects_are_followed_then_capped(monkeypatch):
    _ok_validate(monkeypatch)
    hops = []

    def get(url):
        hops.append(url)
        return 302, {"location": "/next"}, b""

    assert homepage.fetch_homepage_text("acme.com", get=get) is None
    assert len(hops) == homepage.MAX_REDIRECTS + 1


def test_error_status_and_exceptions_return_none(monkeypatch):
    _ok_validate(monkeypatch)
    assert homepage.fetch_homepage_text("acme.com", get=lambda u: (404, {}, b"nope")) is None

    def boom(url):
        raise OSError("connection reset")

    assert homepage.fetch_homepage_text("acme.com", get=boom) is None
    assert homepage.fetch_homepage_text("", get=boom) is None
```

- [ ] **Step 2: Implement `importer/homepage.py`**

```python
"""
Fetch a company's homepage text for the import drafter.

The domain comes from an uploaded CSV, so it is untrusted input: every hop is
validated against security_utils.validate_public_url (the same SSRF guard the
audit endpoint uses) and redirects are followed by hand, at most three, so a
public host cannot bounce us onto an internal address. Any failure returns
None; drafting then proceeds from the CSV fields alone and the draft is
flagged "no homepage evidence".
"""
from urllib.parse import urljoin

import httpx
import trafilatura

from security_utils import UnsafeURLError, validate_public_url

MAX_REDIRECTS = 3
FETCH_TIMEOUT_SECONDS = 8.0
MAX_BODY_BYTES = 1_000_000
MAX_TEXT_CHARS = 4000
_USER_AGENT = "Mozilla/5.0 (compatible; MMGAResearch/1.0)"
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)


def _http_get(url: str) -> tuple[int, dict, bytes]:
    """One GET, no redirect following, body capped at MAX_BODY_BYTES."""
    with httpx.Client(
        follow_redirects=False, timeout=FETCH_TIMEOUT_SECONDS,
        headers={"User-Agent": _USER_AGENT},
    ) as client:
        with client.stream("GET", url) as response:
            body = b""
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) >= MAX_BODY_BYTES:
                    body = body[:MAX_BODY_BYTES]
                    break
            return response.status_code, dict(response.headers), body


def fetch_homepage_text(domain: str, get=_http_get) -> str | None:
    if not domain:
        return None
    url = f"https://{domain}"
    try:
        for _ in range(MAX_REDIRECTS + 1):
            validate_public_url(url)
            status, headers, body = get(url)
            if status in _REDIRECT_STATUSES:
                location = headers.get("location") or headers.get("Location")
                if not location:
                    return None
                url = urljoin(url, location)
                continue
            if status != 200 or not body:
                return None
            html = body[:MAX_BODY_BYTES].decode("utf-8", errors="replace")
            text = " ".join((trafilatura.extract(html) or "").split())
            return text[:MAX_TEXT_CHARS] or None
        return None
    except UnsafeURLError as e:
        print(f"[Import] Refused homepage fetch for {domain}: {e}")
        return None
    except Exception as e:
        print(f"[Import] Homepage fetch failed for {domain}: {e}")
        return None
```

Run: `python -m pytest test_import_homepage.py -v` → PASS (5 tests).

- [ ] **Step 3: Write failing compose tests**

Create `test_import_compose.py`:

```python
from analyzer.ai_audit import AIAuditor
from importer import compose

FACTS = "Jane Doe Chief Compliance Officer Acme Bank banking Boston Massachusetts united states acme.com"
HOMEPAGE = "Acme Bank serves families across Boston and offers wealth management."
GOOD_PARAS = ["Acme Bank serves families across Boston. Is vendor onboarding still handled by hand?"]


def _body(paragraphs=GOOD_PARAS, name="Jane Doe"):
    return compose.compose_body(name, paragraphs, "Kshitij Gupta")


def _checks(body, **over):
    kwargs = dict(
        company="Acme Bank", facts_text=FACTS, homepage_text=HOMEPAGE,
        source_quote="serves families across Boston",
    )
    kwargs.update(over)
    return compose.run_checks("Vendor onboarding", body, **kwargs)


def test_fixed_line_is_short_dashless_and_makes_no_false_claim():
    line = compose.FIXED_LINE
    assert len(line.split()) <= 45
    assert not any(d in line for d in ("-", "\u2014", "\u2013"))
    assert "no human" not in line.lower() and "without a human" not in line.lower()
    parsed = {"email_subject": "x", "opening_line": "", "flaws": [{"paragraph": line, "source_quote": ""}]}
    assert AIAuditor._check_spam_trigger_words(parsed, "c") is None
    assert AIAuditor._check_ai_sounding_phrases(parsed, "c") is None
    assert AIAuditor._check_forbidden_dashes(parsed, "c") is None
    assert AIAuditor._check_number_hallucination(parsed, "", "c") is None


def test_compose_body_greeting_order_and_signoff():
    body = _body()
    assert body.startswith("Hi Jane,\n\n")
    assert body.index(GOOD_PARAS[0]) < body.index(compose.FIXED_LINE)
    assert body.endswith("\n\nKshitij Gupta")


def test_greeting_falls_back_when_the_name_is_unusable():
    assert _body(name="").startswith("Hi there,")
    assert _body(name="Dr. Jane Doe").startswith("Hi Jane,")
    assert _body(name="1234").startswith("Hi there,")


def test_a_clean_grounded_draft_has_no_warnings():
    assert _checks(_body()) == []


def test_dashes_spam_words_and_ai_phrases_are_flagged():
    assert any("dash" in w for w in _checks(_body(["A co-op bank. Is it manual?"])))
    assert any("trigger" in w.lower() for w in _checks(_body(["Get a free audit. Is it manual?"])))
    assert any("generic" in w.lower() for w in _checks(_body(["I noticed your bank. Is it manual?"])))


def test_invented_numbers_are_flagged():
    warnings = _checks(_body(["Acme Bank serves families across Boston and saves 40 hours. Manual?"]))
    assert any("40" in w for w in warnings)


def test_missing_homepage_is_flagged_as_no_evidence():
    warnings = _checks(_body(), homepage_text=None, source_quote="")
    assert any(w.startswith("No homepage evidence") for w in warnings)


def test_a_quote_that_is_not_on_the_homepage_is_flagged():
    warnings = _checks(_body(), source_quote="a sentence the page never contained")
    assert any("source quote" in w for w in warnings)


def test_edit_mode_skips_evidence_checks_but_carries_old_evidence_warnings():
    carried = ["No homepage evidence: this draft is based only on the CSV fields."]
    warnings = _checks(
        _body(), homepage_text=None, source_quote="", check_evidence=False, carried_warnings=carried,
    )
    assert warnings == carried
    assert compose.is_evidence_warning(carried[0])
    assert not compose.is_evidence_warning("Contains trigger word(s) ['free']")
```

- [ ] **Step 4: Implement `importer/compose.py`**

```python
"""
Assembly and quality checks for an import email.

The pitch line is appended by code, not written by the model: it must stay
true (the old BaseSender._AUTOMATION_PITCH_LINE says "the issues above were
found", which is false for an import where no audit ran) and it must never
claim that no human reviewed the email, because every draft is reviewed
before it can be sent.
"""
from analyzer.ai_audit import AIAuditor

FIXED_LINE = (
    "Worth mentioning how this reached you: this email was drafted and sent by a system we "
    "built ourselves. That is the kind of automation we build for other teams, so if "
    "repetitive work is eating into your day, it is the same conversation."
)

_HONORIFICS = {"dr", "mr", "mrs", "ms", "prof", "mx"}


def _first_name(person_name: str) -> str:
    for token in (person_name or "").split():
        token = token.strip(".,")
        if token.lower() in _HONORIFICS:
            continue
        return token if token.replace("'", "").isalpha() else ""
    return ""


def compose_body(person_name: str, paragraphs: list[str], your_name: str) -> str:
    first = _first_name(person_name)
    greeting = f"Hi {first}," if first else "Hi there,"
    parts = [greeting, *[p.strip() for p in paragraphs if p.strip()], FIXED_LINE, your_name]
    return "\n\n".join(parts)


def facts_text(lead: dict) -> str:
    """Everything the model was told about the recipient, for the number check."""
    keys = ("person_name", "person_title", "organization", "industry", "city", "state", "country", "domain")
    return " ".join(str(lead.get(k) or "") for k in keys)


def is_evidence_warning(warning: str) -> bool:
    return warning.startswith("No homepage evidence") or "source quote" in warning


def run_checks(
    subject: str, body: str, *, company: str, facts_text: str, homepage_text: str | None,
    source_quote: str, check_evidence: bool = True, carried_warnings=(),
) -> list[str]:
    """
    The project's existing static checkers, adapted to an import email. The
    whole body is treated as one paragraph so greeting, model text, pitch line
    and sign off are all covered. In edit mode (check_evidence False) the
    homepage text is no longer to hand, so the evidence warnings from the
    original draft are carried forward instead of recomputed.
    """
    parsed = {
        "email_subject": subject,
        "opening_line": "",
        "flaws": [{"paragraph": body, "source_quote": source_quote or ""}],
    }
    corpus = f"{facts_text}\n{homepage_text or ''}\n{source_quote or ''}"
    warnings = [w for w in (
        AIAuditor._check_spam_trigger_words(parsed, company),
        AIAuditor._check_ai_sounding_phrases(parsed, company),
        AIAuditor._check_forbidden_dashes(parsed, company),
        AIAuditor._check_number_hallucination(parsed, corpus, company),
    ) if w]
    if check_evidence:
        if not homepage_text or not source_quote:
            warnings.append(
                "No homepage evidence: this draft is based only on the CSV fields, "
                "so it should make no factual claim about the company."
            )
        else:
            quote_warning = AIAuditor._verify_source_quotes(parsed, homepage_text, company)
            if quote_warning:
                warnings.append(quote_warning)
    warnings.extend(carried_warnings)
    return warnings
```

Run: `python -m pytest test_import_compose.py -v` → PASS (9 tests). If `test_a_clean_grounded_draft_has_no_warnings` fails because a checker's substring list matches ordinary words in the sample text, read the warning, and fix the sample sentence in the test (not the checker).

- [ ] **Step 5: Write failing drafting, AI and route tests**

Create `test_import_drafting.py`:

```python
import json
import time

import pytest

import app as app_module
from analyzer.ai_audit import AIAuditor
from importer import compose, drafting
from storage import import_store
from import_test_support import client_with_key, make_row, seed_lead, use_temp_db

LEAD = make_row()
GOOD = {
    "subject": "Vendor onboarding",
    "paragraphs": ["Acme Bank serves families across Boston. Is vendor onboarding still handled by hand?"],
    "source_quote": "serves families across Boston",
}
HOMEPAGE = "Acme Bank serves families across Boston and offers wealth management."


def _auditor():
    return AIAuditor.__new__(AIAuditor)


def test_prompt_contains_no_dashes_and_carries_the_guards():
    prompt = _auditor()._build_import_prompt(LEAD, HOMEPAGE, "Kshitij Gupta")
    assert not any(d in prompt for d in ("-", "\u2014", "\u2013"))
    assert "Acme Bank serves families" in prompt
    assert "hypothetical" in prompt and "50 characters" in prompt and "60 words" in prompt
    assert "question instead of a statement" in prompt


def test_prompt_marks_missing_homepage_text():
    prompt = _auditor()._build_import_prompt(LEAD, None, "Kshitij Gupta")
    assert "(not available)" in prompt


def test_parse_import_json_accepts_fences_and_cleans():
    raw = "```json\n" + json.dumps({"subject": " S ", "paragraphs": ["a", " ", "b"], "source_quote": " q "}) + "\n```"
    assert AIAuditor._parse_import_json(raw) == {"subject": "S", "paragraphs": ["a", "b"], "source_quote": "q"}


@pytest.mark.parametrize("raw", [
    "not json", "{}", '{"subject": "S", "paragraphs": []}', '{"subject": "", "paragraphs": ["a"]}',
    '{"subject": "S", "paragraphs": "a"}',
])
def test_parse_import_json_rejects_bad_shapes(raw):
    assert AIAuditor._parse_import_json(raw) is None


def test_generate_import_email_falls_through_providers(monkeypatch):
    auditor = _auditor()
    monkeypatch.setattr(auditor, "_call_import_anthropic", lambda p: None)
    monkeypatch.setattr(auditor, "_call_import_gemini", lambda p: ("garbage", 0.1))
    monkeypatch.setattr(auditor, "_call_import_openai", lambda p: (json.dumps(GOOD), 0.0004))
    result = auditor.generate_import_email(LEAD, HOMEPAGE, "Kshitij Gupta")
    assert result["subject"] == "Vendor onboarding" and result["ai_cost"] == 0.0004


def test_generate_import_email_returns_none_when_all_fail(monkeypatch):
    auditor = _auditor()
    for name in ("_call_import_anthropic", "_call_import_gemini", "_call_import_openai"):
        monkeypatch.setattr(auditor, name, lambda p: None)
    assert auditor.generate_import_email(LEAD, HOMEPAGE, "K") is None


class _FakeAuditor:
    def __init__(self, result):
        self.result = result

    def generate_import_email(self, lead, homepage_text, your_name):
        return self.result


def test_draft_lead_assembles_body_and_checks():
    out = drafting.draft_lead(
        LEAD, _FakeAuditor({**GOOD, "ai_cost": 0.001}), your_name="Kshitij Gupta",
        fetch=lambda domain: HOMEPAGE,
    )
    assert out["subject"] == "Vendor onboarding"
    assert out["body"].startswith("Hi Jane,") and compose.FIXED_LINE in out["body"]
    assert out["review_warnings"] == []
    assert out["source_quote"] == GOOD["source_quote"] and out["ai_cost"] == 0.001


def test_draft_lead_without_homepage_is_flagged():
    out = drafting.draft_lead(
        LEAD, _FakeAuditor({**GOOD, "source_quote": "", "ai_cost": 0.0}), your_name="K",
        fetch=lambda domain: None,
    )
    assert any(w.startswith("No homepage evidence") for w in out["review_warnings"])


def test_draft_lead_flags_overlong_subject_and_body():
    long_para = " ".join(["word"] * 70)
    out = drafting.draft_lead(
        LEAD, _FakeAuditor({"subject": "s" * 60, "paragraphs": [long_para], "source_quote": "", "ai_cost": 0}),
        your_name="K", fetch=lambda d: None,
    )
    joined = " ".join(out["review_warnings"])
    assert "Subject is 60 characters" in joined and "Body is 70 words" in joined


def test_draft_lead_raises_when_every_provider_failed():
    with pytest.raises(drafting.DraftError):
        drafting.draft_lead(LEAD, _FakeAuditor(None), your_name="K", fetch=lambda d: None)


# ---- routes --------------------------------------------------------------

def _fake_draft(lead, auditor, *, your_name):
    return {
        "subject": "Vendor onboarding", "body": "Hi Jane,\n\nBody", "review_warnings": ["w"],
        "source_quote": "q", "ai_cost": 0.002,
    }


def test_draft_route_saves_the_draft_and_logs_cost(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)
    monkeypatch.setattr(app_module.import_drafting, "draft_lead", _fake_draft)
    lead_id = seed_lead()
    r = client.post(f"/api/import/leads/{lead_id}/draft")
    assert r.status_code == 200, r.text
    lead = r.json()["lead"]
    assert lead["status"] == "drafted" and lead["review_warnings"] == ["w"] and lead["source_quote"] == "q"
    assert any(c["category"] == "AI Import Draft" for c in app_module.db.get_costs())


def test_draft_route_rejects_a_sent_lead_and_unknown_lead(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)
    assert client.post("/api/import/leads/999/draft").status_code == 404
    lead_id = seed_lead(status="sent")
    assert client.post(f"/api/import/leads/{lead_id}/draft").status_code == 409


def test_draft_route_records_an_ai_failure_and_keeps_the_lead_ready(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)

    def fail(lead, auditor, *, your_name):
        raise drafting.DraftError("Every AI provider failed")

    monkeypatch.setattr(app_module.import_drafting, "draft_lead", fail)
    lead_id = seed_lead()
    r = client.post(f"/api/import/leads/{lead_id}/draft")
    assert r.status_code == 502
    lead = import_store.get_lead(lead_id)
    assert lead["status"] == "ready" and "Every AI provider failed" in lead["error"]


def test_draft_route_times_out_cleanly(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)
    monkeypatch.setattr(app_module, "IMPORT_DRAFT_TIMEOUT_SECONDS", 0.05)

    def slow(lead, auditor, *, your_name):
        time.sleep(0.4)
        return _fake_draft(lead, auditor, your_name=your_name)

    monkeypatch.setattr(app_module.import_drafting, "draft_lead", slow)
    lead_id = seed_lead()
    r = client.post(f"/api/import/leads/{lead_id}/draft")
    assert r.status_code == 504
    assert import_store.get_lead(lead_id)["status"] == "ready"


def test_edit_route_rechecks_and_returns_the_lead_to_drafted(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)
    lead_id = seed_lead(
        status="approved", subject="S", body="Hi Jane,\n\nOld", review_warnings=["No homepage evidence: x"],
    )
    r = client.put(f"/api/import/leads/{lead_id}", json={"subject": "New subject", "body": "Hi Jane,\n\nA co-op"})
    assert r.status_code == 200, r.text
    lead = r.json()["lead"]
    assert lead["status"] == "drafted" and lead["subject"] == "New subject"
    assert any("dash" in w for w in lead["review_warnings"])
    assert "No homepage evidence: x" in lead["review_warnings"]


def test_edit_route_rejects_empty_and_unedited_states(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)
    fresh = seed_lead()
    assert client.put(f"/api/import/leads/{fresh}", json={"subject": "S", "body": "B"}).status_code == 409
    drafted = seed_lead(status="drafted", subject="S", body="B")
    assert client.put(f"/api/import/leads/{drafted}", json={"subject": " ", "body": "B"}).status_code == 400
```

- [ ] **Step 6: Add the AI method to `analyzer/ai_audit.py`**

Next to `_FOLLOWUP_RESPONSE_SCHEMA` (top of file) add:

```python
# Schema for the CSV import page's per-lead email (added 2026-10-15). Its own
# schema and parser, like the follow-up one above, so the audit prompt and
# schema are untouched. source_quote must be an exact phrase from the homepage
# text the model was shown, or empty.
_IMPORT_EMAIL_SCHEMA = {
    "type": "object",
    "properties": {
        "subject": {"type": "string"},
        "paragraphs": {"type": "array", "items": {"type": "string"}},
        "source_quote": {"type": "string"},
    },
    "required": ["subject", "paragraphs", "source_quote"],
}
```

Append these methods at the end of class `AIAuditor` (after `_parse_followup_json`; read the last 20 lines of the file first to confirm the class is the last thing in it):

```python
    # ------------------------------------------------------------------
    # Import email generation: CSV import page (2026-10-15)
    # ------------------------------------------------------------------

    def _build_import_prompt(self, lead: dict, homepage_text: str | None, your_name: str) -> str:
        # Written without a single hyphen or dash on purpose: a model mirrors
        # the style of the prompt it is shown (see _check_forbidden_dashes).
        return f"""You write one short outbound email for {your_name}, who runs MMGA Agency. MMGA builds automation for businesses: it removes repetitive manual work such as copying data between systems, chasing approvals, preparing recurring reports and sorting documents.

THE RECIPIENT
Name: {lead.get("person_name") or "unknown"}
Title: {lead.get("person_title") or "unknown"}
Company: {lead.get("organization") or "unknown"}
Industry: {lead.get("industry") or "unknown"}
Location: {lead.get("city") or ""} {lead.get("state") or ""}

WHAT THE COMPANY WEBSITE SAYS (the only evidence you may rely on about the company)
\"\"\"
{homepage_text or "(not available)"}
\"\"\"

RULES
1. Start with one specific problem that someone in the recipient's role at this kind of company plausibly has. Do not introduce MMGA in the first sentence. Write no greeting and no sign off, both are added for you.
2. Use only facts from the recipient block and the website text above. Never state a number, client count or revenue figure about their company. If you need an example number, make it a round and openly hypothetical one, such as "if a team spent 10 hours a week on this".
3. If the website text is not available or gives you nothing useful, make no factual claim about the company beyond its name and the recipient's title, set source_quote to an empty string, and write the problem as a question instead of a statement.
4. If you do rely on the website text, copy ONE short exact phrase from it into source_quote. It must appear in the text word for word.
5. Never use hyphens or dashes of any kind in your response. Write compound words as two words.
6. Do not use phrases such as "I noticed", "I hope this finds you well", "In today's competitive landscape", "would love to connect", "drive growth" or "unlock".
7. The subject has at most 50 characters, is not in capital letters, and avoids the words free, guarantee, urgent, cash and winner.
8. The body is 3 to 5 short sentences and at most 60 words in total, in 1 or 2 paragraphs. The last sentence is one easy question that can be answered with a yes or a no.
9. Plain, human, direct tone.

Respond with JSON only: {{"subject": "...", "paragraphs": ["..."], "source_quote": "..."}}"""

    def generate_import_email(self, lead: dict, homepage_text: str | None, your_name: str) -> dict | None:
        """
        Draft one email for a CSV import lead. Same provider order as
        generate_followup_from_original (Claude Haiku, Gemini, GPT-4o-mini).
        Returns {"subject", "paragraphs", "source_quote", "ai_cost"} or None
        if every provider fails or answers in an unusable shape.
        """
        prompt = self._build_import_prompt(lead, homepage_text, your_name)
        for call_fn in (
            self._call_import_anthropic,
            self._call_import_gemini,
            self._call_import_openai,
        ):
            result = call_fn(prompt)
            if result is None:
                continue
            raw, cost = result
            parsed = self._parse_import_json(raw)
            if parsed is not None:
                parsed["ai_cost"] = cost
                return parsed
        print("[AIAuditor] All AI providers failed for import email generation.")
        return None

    def _call_import_anthropic(self, prompt: str) -> tuple[str, float] | None:
        if not self._anthropic_client:
            return None
        try:
            message = self._anthropic_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=700,
                temperature=_AI_TEMPERATURE,
                tools=[{
                    "name": "submit_import_email",
                    "description": "Submit the drafted email.",
                    "input_schema": _IMPORT_EMAIL_SCHEMA,
                }],
                tool_choice={"type": "tool", "name": "submit_import_email"},
                messages=[{"role": "user", "content": prompt}],
            )
            try:
                cost = (message.usage.input_tokens * 0.25 / 1_000_000) + (message.usage.output_tokens * 1.25 / 1_000_000)
            except Exception:
                cost = 0.0003
            tool_use = next((b for b in message.content if b.type == "tool_use"), None)
            if tool_use is None:
                return None
            return json.dumps(tool_use.input), cost
        except Exception as e:
            print(f"Anthropic error (import email): {e}")
            return None

    def _call_import_gemini(self, prompt: str) -> tuple[str, float] | None:
        if not config.GEMINI_API_KEY:
            return None
        try:
            model = genai.GenerativeModel(
                "gemini-3.5-flash",
                generation_config=genai.types.GenerationConfig(
                    response_mime_type="application/json",
                    response_schema=_IMPORT_EMAIL_SCHEMA,
                    temperature=_AI_TEMPERATURE,
                ),
            )
            response = model.generate_content(prompt)
            try:
                cost = (response.usage_metadata.prompt_token_count * 0.075 / 1_000_000) + (
                    response.usage_metadata.candidates_token_count * 0.30 / 1_000_000)
            except Exception:
                cost = 0.0001
            return response.text, cost
        except Exception as e:
            print(f"Gemini error (import email): {e}")
            return None

    def _call_import_openai(self, prompt: str) -> tuple[str, float] | None:
        if not self._openai_client:
            return None
        try:
            response = self._openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=_AI_TEMPERATURE,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "import_email_result",
                        "strict": True,
                        "schema": {**_IMPORT_EMAIL_SCHEMA, "additionalProperties": False},
                    },
                },
            )
            try:
                cost = (response.usage.prompt_tokens * 0.150 / 1_000_000) + (response.usage.completion_tokens * 0.60 / 1_000_000)
            except Exception:
                cost = 0.0001
            return response.choices[0].message.content, cost
        except Exception as e:
            print(f"OpenAI error (import email): {e}")
            return None

    @staticmethod
    def _parse_import_json(raw: str) -> dict | None:
        cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip()
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end == -1:
            return None
        try:
            data = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            return None
        subject, paragraphs = data.get("subject"), data.get("paragraphs")
        if not isinstance(subject, str) or not subject.strip() or not isinstance(paragraphs, list):
            return None
        paragraphs = [p.strip() for p in paragraphs if isinstance(p, str) and p.strip()]
        if not paragraphs:
            return None
        quote = data.get("source_quote")
        return {
            "subject": subject.strip(),
            "paragraphs": paragraphs,
            "source_quote": quote.strip() if isinstance(quote, str) else "",
        }
```

- [ ] **Step 7: Implement `importer/drafting.py`**

```python
"""Draft one import lead: fetch evidence, ask the model, assemble, check."""
from importer import compose, homepage

MAX_SUBJECT_CHARS = 50
MAX_BODY_WORDS = 60


class DraftError(Exception):
    pass


def draft_lead(lead: dict, auditor, *, your_name: str, fetch=homepage.fetch_homepage_text) -> dict:
    homepage_text = fetch(lead.get("domain") or "")
    result = auditor.generate_import_email(lead, homepage_text, your_name)
    if not result:
        raise DraftError("Every AI provider failed to produce a draft. Try again in a moment.")

    paragraphs = result["paragraphs"]
    subject = result["subject"].strip()
    quote = result.get("source_quote") or ""
    body = compose.compose_body(lead.get("person_name") or "", paragraphs, your_name)
    warnings = compose.run_checks(
        subject, body,
        company=lead.get("organization") or lead.get("domain") or "this lead",
        facts_text=compose.facts_text(lead),
        homepage_text=homepage_text, source_quote=quote,
    )
    if len(subject) > MAX_SUBJECT_CHARS:
        warnings.append(f"Subject is {len(subject)} characters; keep it at {MAX_SUBJECT_CHARS} or fewer.")
    words = sum(len(p.split()) for p in paragraphs)
    if words > MAX_BODY_WORDS:
        warnings.append(f"Body is {words} words; keep it at {MAX_BODY_WORDS} or fewer.")
    return {
        "subject": subject, "body": body, "review_warnings": warnings,
        "source_quote": quote, "ai_cost": result.get("ai_cost", 0.0),
    }
```

- [ ] **Step 8: Add `save_edit` to `importer/service.py`**

Add `from importer import compose` to its imports, then append:

```python
def save_edit(lead_id: int, subject: str, body: str) -> dict:
    lead = _load(lead_id)
    if lead["status"] not in ("drafted", "approved", "failed") or not lead.get("body"):
        raise FlowError(409, "Only a drafted lead can be edited.")
    subject, body = subject.strip(), body.strip()
    if not subject or not body:
        raise FlowError(400, "Subject and body cannot be empty.")
    carried = [w for w in lead["review_warnings"] if compose.is_evidence_warning(w)]
    warnings = compose.run_checks(
        subject, body,
        company=lead.get("organization") or lead.get("domain") or "this lead",
        facts_text=compose.facts_text(lead), homepage_text=None,
        source_quote=lead.get("source_quote") or "",
        check_evidence=False, carried_warnings=carried,
    )
    # An edit invalidates any earlier approval and acknowledgement.
    import_store.update_lead(lead_id, status="drafted", subject=subject, body=body, review_warnings=warnings)
    return import_store.get_lead(lead_id)
```

- [ ] **Step 9: Add the routes to `app.py`**

Imports (next to the Task 3 imports): `from importer import drafting as import_drafting`. Add below the Task 3 routes, still above the static mount:

```python
IMPORT_DRAFT_TIMEOUT_SECONDS = 45


class ImportEditIn(BaseModel):
    subject: str = Field(max_length=200)
    body: str = Field(max_length=5000)


@app.post("/api/import/leads/{lead_id}/draft")
async def import_draft(
    lead_id: int,
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(12, 60)),
):
    # One lead per request, never a batch: Railway's edge cuts requests at
    # roughly 60 to 65 seconds and a draft is a network fetch plus an LLM call.
    lead = await asyncio.to_thread(import_store.get_lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Import lead not found")
    if lead["status"] not in ("ready", "drafted", "failed"):
        raise HTTPException(status_code=409, detail=f"A {lead['status']} lead cannot be drafted.")

    async def _fail(code: int, message: str):
        await asyncio.to_thread(import_store.update_lead, lead_id, error=message)
        raise HTTPException(status_code=code, detail=message)

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                import_drafting.draft_lead, lead, auditor,
                your_name=os.getenv("YOUR_NAME", "Kshitij Gupta"),
            ),
            timeout=IMPORT_DRAFT_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        await _fail(504, "Drafting timed out. The lead was left as it was; try again.")
    except import_drafting.DraftError as e:
        await _fail(502, str(e))

    if result["ai_cost"]:
        await asyncio.to_thread(
            db.log_cost, "AI Import Draft", result["ai_cost"],
            description=f"Import draft for {lead.get('organization') or lead.get('domain')}",
        )
    await asyncio.to_thread(
        import_store.update_lead, lead_id,
        status="drafted", subject=result["subject"], body=result["body"],
        review_warnings=result["review_warnings"], source_quote=result["source_quote"],
        drafted_at=import_store.now_sql(), error=None,
    )
    return {"lead": await asyncio.to_thread(import_store.get_lead, lead_id)}


@app.put("/api/import/leads/{lead_id}")
async def import_edit(
    lead_id: int,
    req: ImportEditIn,
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(120, 60)),
):
    return {"lead": await _import_call(import_service.save_edit, lead_id, req.subject, req.body)}
```

Add `"import_draft", "import_edit"` to `ROUTE_FUNCS` in `test_import_endpoints.py`.

- [ ] **Step 10: Run tests**

Run: `python -m pytest test_import_homepage.py test_import_compose.py test_import_drafting.py test_import_endpoints.py -v`
Expected: PASS.

- [ ] **Step 11: Commit**

```bash
git add importer/homepage.py importer/compose.py importer/drafting.py importer/service.py analyzer/ai_audit.py app.py test_import_homepage.py test_import_compose.py test_import_drafting.py test_import_endpoints.py
git commit -m "feat: AI drafting, evidence checks and edit route for imported leads"
```

---

### Task 5: CAN-SPAM footer

**Files:**
- Modify: `emailer/base_sender.py` (add `_with_footer`; call it first thing in `_build_initial_message` and `_build_followup_message`)
- Create: `test_import_footer.py`

**Interfaces:**
- Consumes: `config.MAILING_ADDRESS`, `config.APP_BASE_URL`.
- Produces: `BaseSender._with_footer(self, body: str, to_email: str) -> str`.

- [ ] **Step 1: Write the failing tests**

Create `test_import_footer.py`:

```python
import config
from emailer.base_sender import BaseSender

ADDRESS = "MMGA Agency, 12 Example Road, Mumbai 400001, India"


def _sender():
    return BaseSender(from_email="us@example.com")


def _parts(msg):
    plain = html = ""
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            plain = part.get_payload(decode=True).decode("utf-8")
        elif part.get_content_type() == "text/html":
            html = part.get_payload(decode=True).decode("utf-8")
    return plain, html


def test_unset_address_leaves_bodies_byte_identical(monkeypatch):
    monkeypatch.setattr(config, "MAILING_ADDRESS", "")
    s = _sender()
    assert s._with_footer("Hello there", "a@b.com") == "Hello there"
    plain, html = _parts(s._build_initial_message("a@b.com", "S", "Hello there", "<m@x>"))
    assert plain == "Hello there"
    assert "MMGA Agency" not in html


def test_initial_email_carries_address_and_optout_link(monkeypatch):
    monkeypatch.setattr(config, "MAILING_ADDRESS", ADDRESS)
    monkeypatch.setattr(config, "APP_BASE_URL", "https://app.example.com")
    plain, html = _parts(_sender()._build_initial_message("a@b.com", "S", "Hello there", "<m@x>"))
    assert plain.startswith("Hello there")
    assert ADDRESS in plain and ADDRESS in html
    assert "https://app.example.com/unsubscribe?email=a%40b.com" in plain


def test_without_a_base_url_the_optout_is_a_reply_instruction(monkeypatch):
    monkeypatch.setattr(config, "MAILING_ADDRESS", ADDRESS)
    monkeypatch.setattr(config, "APP_BASE_URL", "")
    footer = _sender()._with_footer("Hello", "a@b.com")
    assert "reply with the word unsubscribe" in footer


def test_followups_carry_the_footer_too(monkeypatch):
    monkeypatch.setattr(config, "MAILING_ADDRESS", ADDRESS)
    monkeypatch.setattr(config, "APP_BASE_URL", "https://app.example.com")
    plain, _ = _parts(_sender()._build_followup_message("a@b.com", "Re: S", "Nudge", "<o@x>"))
    assert ADDRESS in plain


def test_footer_wording_has_no_dashes(monkeypatch):
    monkeypatch.setattr(config, "MAILING_ADDRESS", "MMGA Agency, 12 Example Road, Mumbai")
    monkeypatch.setattr(config, "APP_BASE_URL", "https://app.example.com")
    footer = _sender()._with_footer("", "a@b.com")
    assert not any(d in footer for d in ("-", "\u2014", "\u2013"))
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest test_import_footer.py -v`
Expected: FAIL (`_with_footer` missing).

- [ ] **Step 3: Implement**

In `emailer/base_sender.py`, add to class `BaseSender` (next to `_unsubscribe_headers`):

```python
    def _with_footer(self, body: str, to_email: str) -> str:
        """
        Append the CAN-SPAM postal address and an opt-out line.

        Commercial email to US recipients needs a physical postal address and a
        clear opt-out; before 2026-10-15 no body carried an address. Opt-in
        through MAILING_ADDRESS so an environment that never sets it sends
        exactly what it always did. The wording itself contains no hyphens (the
        copy rule applies to everything we author); the address and URL are
        whatever the operator configured.
        """
        address = (config.MAILING_ADDRESS or "").strip()
        if not address:
            return body
        if config.APP_BASE_URL:
            optout = (
                "To stop receiving emails from us, unsubscribe here: "
                f"{config.APP_BASE_URL}/unsubscribe?email={quote(to_email, safe='')}"
            )
        else:
            optout = "To stop receiving emails from us, reply with the word unsubscribe."
        return f"{body.rstrip()}\n\n{address}\n{optout}"
```

Add as the first statement of `_build_initial_message` (before `msg = MIMEMultipart("mixed")`) and of `_build_followup_message` (before `message_id = make_msgid(...)`):

```python
        body = self._with_footer(body, to_email)
```

- [ ] **Step 4: Run tests plus the existing email suites**

Run: `python -m pytest test_import_footer.py test_email_deliverability.py test_email_copy.py test_gmail_sender.py -v`
Expected: PASS (existing suites unchanged because `MAILING_ADDRESS` defaults to empty).

- [ ] **Step 5: Commit**

```bash
git add emailer/base_sender.py test_import_footer.py
git commit -m "feat: opt-in CAN-SPAM postal address and opt-out footer"
```

---

### Task 6: Send path, compliance gates, bounce pause, status route

**Files:**
- Create: `importer/send_policy.py`, `test_import_send.py`
- Modify: `app.py` (`SendRequest`, `/api/send`, new helper and `/api/import/status` route, imports), `test_import_endpoints.py` (extend `ROUTE_FUNCS`)

**Interfaces:**
- Consumes: `csv_rules.recheck`, `import_store`, `db.get_variant_performance` (rows have `variant`, `sent`, `bounced`; read the function's return before relying on it), `config.IMPORT_BOUNCE_*`, `config.MAILING_ADDRESS`.
- Produces:
  - `send_policy.IMPORT_VARIANT = "import-automation"`
  - `class SendBlocked(Exception)` with `.status_code`, `.detail`, `.skip_reason`
  - `send_policy.bounce_status(rows, min_sent, pause_pct) -> {"sent","bounced","rate_pct","paused","min_sent","pause_pct"}`
  - `send_policy.check_import_send(lead, *, req_email, acknowledge_warnings, mailing_address, blockers, bounce) -> None` (raises `SendBlocked`)
  - `SendRequest.import_lead_id: int | None = None`
  - Route `GET /api/import/status` (120/60) returning `{sent_today, daily_limit, remaining, bounce, mailing_address_set}`.

- [ ] **Step 1: Read what the tests depend on**

Run: `grep -n "def log_draft" -A6 storage/db.py` and read the tail of `get_variant_performance` to confirm the returned rows keep `variant`, `sent`, `bounced`. Adjust the `db.log_draft(...)` call in the draft-isolation test below to the real signature.

- [ ] **Step 2: Write the failing tests**

Create `test_import_send.py`:

```python
import pytest

import app as app_module
from importer import send_policy
from storage import db, import_store
from import_test_support import client_with_key, empty_blockers, seed_lead, use_temp_db

OK_BOUNCE = {"paused": False}


def _lead(**over):
    lead = {
        "id": 1, "status": "approved", "email": "jane@acme.com", "domain": "acme.com",
        "country": "united states", "review_warnings": [], "subject": "S", "body": "B",
    }
    lead.update(over)
    return lead


def _check(lead, **over):
    kwargs = dict(
        req_email="jane@acme.com", acknowledge_warnings=False, mailing_address="1 Road",
        blockers=empty_blockers(), bounce=OK_BOUNCE,
    )
    kwargs.update(over)
    return send_policy.check_import_send(lead, **kwargs)


# ---- policy ----------------------------------------------------------------

def test_a_valid_approved_lead_passes():
    assert _check(_lead()) is None


@pytest.mark.parametrize("lead,over,code", [
    (None, {}, 404),
    (_lead(status="drafted"), {}, 409),
    (_lead(), {"req_email": "other@acme.com"}, 409),
    (_lead(), {"mailing_address": ""}, 409),
    (_lead(country="india"), {"mailing_address": ""}, None),
    (_lead(), {"bounce": {"paused": True, "sent": 30, "bounced": 4, "rate_pct": 13.33, "pause_pct": 5}}, 409),
    (_lead(review_warnings=["w"]), {}, 409),
    (_lead(review_warnings=["w"]), {"acknowledge_warnings": True}, None),
])
def test_policy_gates(lead, over, code):
    if code is None:
        assert _check(lead, **over) is None
        return
    with pytest.raises(send_policy.SendBlocked) as exc:
        _check(lead, **over)
    assert exc.value.status_code == code


def test_state_that_changed_since_approval_blocks_with_a_skip_reason():
    with pytest.raises(send_policy.SendBlocked) as exc:
        _check(_lead(), blockers=empty_blockers(suppressed={"jane@acme.com"}))
    assert exc.value.skip_reason == "suppressed"


def test_the_us_gate_names_the_setting():
    with pytest.raises(send_policy.SendBlocked) as exc:
        _check(_lead(), mailing_address="")
    assert "MAILING_ADDRESS" in str(exc.value.detail)


def test_bounce_pause_boundaries():
    rows = lambda sent, bounced: [{"variant": "import-automation", "sent": sent, "bounced": bounced}]
    assert send_policy.bounce_status([], 20, 5)["paused"] is False
    assert send_policy.bounce_status(rows(19, 19), 20, 5)["paused"] is False   # below the sample floor
    assert send_policy.bounce_status(rows(20, 1), 20, 5)["paused"] is False    # exactly 5 percent
    assert send_policy.bounce_status(rows(20, 2), 20, 5)["paused"] is True     # 10 percent
    other = [{"variant": "short", "sent": 100, "bounced": 100}]
    assert send_policy.bounce_status(other, 20, 5)["sent"] == 0                # only this feature counts


# ---- /api/send -------------------------------------------------------------

class _Sender:
    def __init__(self, result="<sent@x.com>", error=None):
        self.calls, self.result, self.error = [], result, error

    def send_email(self, to_email, subject, body, **kw):
        self.calls.append({"to": to_email, "subject": subject, "body": body, **kw})
        if self.error:
            raise self.error
        return self.result


def _approved(**over):
    return seed_lead(status="approved", subject="Stored subject", body="Stored body", **over)


def _post(client, lead_id, **over):
    payload = {
        "email": "jane@acme.com", "subject": "CLIENT subject", "body": "CLIENT body",
        "company": "Acme Bank", "website": "acme.com", "import_lead_id": lead_id,
        "acknowledge_warnings": False,
    }
    payload.update(over)
    return client.post("/api/send", json=payload)


@pytest.fixture
def env(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    monkeypatch.setattr(app_module.config, "MAILING_ADDRESS", "1 Example Road, Mumbai")
    monkeypatch.setattr(app_module.config, "DAILY_EMAIL_LIMIT", 50)
    sender = _Sender()
    monkeypatch.setattr(app_module, "ses", sender)
    return client_with_key(monkeypatch), sender


def test_sends_the_stored_copy_not_the_clients(env):
    client, sender = env
    lead_id = _approved()
    r = _post(client, lead_id)
    assert r.status_code == 200, r.text
    assert sender.calls[0]["subject"] == "Stored subject" and sender.calls[0]["body"] == "Stored body"


def test_marks_sent_links_history_and_records_the_variant(env):
    client, _ = env
    lead_id = _approved()
    assert _post(client, lead_id).status_code == 200
    lead = import_store.get_lead(lead_id)
    assert lead["status"] == "sent" and lead["sent_at"] and lead["history_id"]
    row = db.get_email_history_by_id(lead["history_id"])
    assert row["variant"] == "import-automation" and row["website"] == "acme.com"
    assert row["subject"] == "Stored subject"


def test_an_import_send_never_attaches_screenshots_or_touches_audit_drafts(env):
    client, sender = env
    # Adapt this call to db.log_draft's real signature (Step 1): an audit draft
    # for the SAME website must exist before and after the import send.
    db.log_draft("Acme Bank", "acme.com", "jane@acme.com", "Audit subject", "Audit body", "img")
    lead_id = _approved()
    assert _post(client, lead_id).status_code == 200
    call = sender.calls[0]
    assert not call.get("image_path") and not call.get("mobile_image_path") and not call.get("closeup_image_path")
    assert db.get_draft_by_website("acme.com") is not None


def test_unapproved_missing_and_mismatched_leads_are_refused(env):
    client, sender = env
    drafted = seed_lead(status="drafted", subject="S", body="B")
    assert _post(client, drafted).status_code == 409
    assert _post(client, 99999).status_code == 404
    lead_id = _approved()
    assert _post(client, lead_id, email="someone@else.com").status_code == 409
    assert sender.calls == []


def test_us_lead_is_blocked_without_a_mailing_address(env, monkeypatch):
    client, sender = env
    monkeypatch.setattr(app_module.config, "MAILING_ADDRESS", "")
    lead_id = _approved()
    r = _post(client, lead_id)
    assert r.status_code == 409 and "MAILING_ADDRESS" in r.text
    assert sender.calls == []


def test_warnings_need_acknowledgement(env):
    client, _ = env
    lead_id = _approved(review_warnings=["w"])
    assert _post(client, lead_id).status_code == 409
    assert _post(client, lead_id, acknowledge_warnings=True).status_code == 200


def test_the_daily_cap_is_still_enforced(env, monkeypatch):
    client, sender = env
    monkeypatch.setattr(app_module.db, "count_emails_sent_today", lambda: 50)
    r = _post(client, _approved())
    assert r.status_code == 429 and sender.calls == []


def test_a_suppressed_recipient_is_marked_skipped(env):
    client, sender = env
    sender.result = False   # what the sender returns for a suppressed address
    lead_id = _approved()
    r = _post(client, lead_id)
    assert r.status_code == 400
    lead = import_store.get_lead(lead_id)
    assert lead["status"] == "skipped" and lead["skip_reason"] == "suppressed"


def test_a_transport_error_marks_the_lead_failed_with_the_real_error(env, monkeypatch):
    client, _ = env
    monkeypatch.setattr(app_module, "ses", _Sender(error=Exception("SES rejected: bad domain")))
    lead_id = _approved()
    r = _post(client, lead_id)
    assert r.status_code == 500
    lead = import_store.get_lead(lead_id)
    assert lead["status"] == "failed" and "SES rejected" in lead["error"]


def test_state_that_changed_after_approval_is_caught_at_send_time(env):
    client, sender = env
    lead_id = _approved()
    db.add_suppression("jane@acme.com", "unsub")
    r = _post(client, lead_id)
    assert r.status_code == 409 and sender.calls == []
    assert import_store.get_lead(lead_id)["skip_reason"] == "suppressed"


def test_the_bounce_pause_blocks_further_sends(env, monkeypatch):
    client, sender = env
    monkeypatch.setattr(
        app_module.db, "get_variant_performance",
        lambda: [{"variant": "import-automation", "sent": 20, "bounced": 2}],
    )
    r = _post(client, _approved())
    assert r.status_code == 409 and r.json()["detail"]["code"] == "import_paused"
    assert sender.calls == []


def test_existing_callers_without_import_lead_id_are_unchanged(env):
    client, sender = env
    r = client.post("/api/send", json={
        "email": "x@y.com", "subject": "Plain subject", "body": "Plain body",
        "company": "Y", "website": "y.com",
    })
    assert r.status_code == 200
    assert sender.calls[0]["subject"] == "Plain subject"


def test_status_route(env, monkeypatch):
    client, _ = env
    monkeypatch.setattr(app_module.db, "count_emails_sent_today", lambda: 12)
    r = client.get("/api/import/status")
    body = r.json()
    assert body["sent_today"] == 12 and body["daily_limit"] == 50 and body["remaining"] == 38
    assert body["mailing_address_set"] is True and body["bounce"]["paused"] is False
```

- [ ] **Step 3: Run to verify failure**

Run: `python -m pytest test_import_send.py -v`
Expected: FAIL (`No module named importer.send_policy`).

- [ ] **Step 4: Implement `importer/send_policy.py`**

```python
"""
Gates for sending an imported lead through /api/send: status and identity,
state that changed since approval, the CAN-SPAM address requirement, the
bounce pause, and warning acknowledgement. Pure functions; app.py supplies
the facts.
"""
from importer import csv_rules

IMPORT_VARIANT = "import-automation"


class SendBlocked(Exception):
    def __init__(self, status_code: int, detail, skip_reason: str | None = None):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail
        self.skip_reason = skip_reason


def bounce_status(variant_rows: list[dict], min_sent: int, pause_pct: float) -> dict:
    """Bounce rate across this feature's own sends (variant import-automation)."""
    bucket = next((r for r in variant_rows if r.get("variant") == IMPORT_VARIANT), None)
    sent = bucket["sent"] if bucket else 0
    bounced = bucket["bounced"] if bucket else 0
    rate = (bounced / sent * 100) if sent else 0.0
    return {
        "sent": sent, "bounced": bounced, "rate_pct": round(rate, 2),
        "paused": sent >= min_sent and rate > pause_pct,
        "min_sent": min_sent, "pause_pct": pause_pct,
    }


def check_import_send(
    lead: dict | None, *, req_email: str, acknowledge_warnings: bool,
    mailing_address: str, blockers: dict, bounce: dict,
) -> None:
    if lead is None:
        raise SendBlocked(404, "Import lead not found")
    if lead["status"] != "approved":
        raise SendBlocked(409, f"This lead is '{lead['status']}'; only approved leads can be sent.")
    if (req_email or "").strip().lower() != lead["email"].strip().lower():
        raise SendBlocked(409, "The email address does not match the imported lead.")
    reason = csv_rules.recheck(lead, blockers)
    if reason:
        raise SendBlocked(409, f"This lead can no longer be sent ({reason}).", skip_reason=reason)
    if (lead.get("country") or "").strip().lower() == "united states" and not mailing_address:
        raise SendBlocked(
            409,
            "US recipients need a postal address in the email footer (CAN-SPAM). "
            "Set the MAILING_ADDRESS environment variable, then try again.",
        )
    if bounce["paused"]:
        raise SendBlocked(409, {
            "message": (
                f"Import sending is paused: {bounce['bounced']} of {bounce['sent']} sent emails "
                f"bounced ({bounce['rate_pct']}%, limit {bounce['pause_pct']}%)."
            ),
            "code": "import_paused", **bounce,
        })
    if lead["review_warnings"] and not acknowledge_warnings:
        raise SendBlocked(409, {
            "message": "This draft was flagged during review and hasn't been acknowledged yet.",
            "warnings": lead["review_warnings"],
            "resend_with": "acknowledge_warnings: true",
        })
```

- [ ] **Step 5: Wire `app.py`**

1. Imports: `from importer import send_policy as import_send_policy`.
2. `SendRequest`: add after `attach_screenshot`:

```python
    # Set only by the CSV Import page. When present, /api/send sends the
    # STORED, reviewed subject and body for that import lead (the client's
    # copies are ignored), applies the import gates, and skips the audit
    # pipeline's draft lookup, screenshots, Sheets update and draft deletion.
    # Existing callers never send it, so their behaviour is unchanged.
    import_lead_id: int | None = None
```

3. Add this helper directly above `@app.post("/api/send")`:

```python
async def _prepare_import_send(req: SendRequest) -> dict:
    """Load an import lead and apply every import gate; raise HTTPException on a block."""
    lead = await asyncio.to_thread(import_store.get_lead, req.import_lead_id)
    blockers = await asyncio.to_thread(import_store.load_blockers)
    rows = await asyncio.to_thread(db.get_variant_performance)
    bounce = import_send_policy.bounce_status(
        rows, config.IMPORT_BOUNCE_MIN_SENT, config.IMPORT_BOUNCE_PAUSE_PCT
    )
    try:
        import_send_policy.check_import_send(
            lead, req_email=req.email, acknowledge_warnings=req.acknowledge_warnings,
            mailing_address=config.MAILING_ADDRESS, blockers=blockers, bounce=bounce,
        )
    except import_send_policy.SendBlocked as e:
        if e.skip_reason and lead:
            await asyncio.to_thread(
                import_store.update_lead, lead["id"], status="skipped", skip_reason=e.skip_reason
            )
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    return lead
```

4. In `send_email`, immediately before the comment `# The five accuracy checks in analyzer/ai_audit.py already flag a`, insert:

```python
        # CSV Import page sends: load the stored lead and apply the import
        # gates. Everything below that is specific to the audit pipeline
        # (draft lookup, screenshots, Sheets, draft deletion) is skipped for
        # these, because those are keyed by website and an old audit draft or
        # screenshot for the same company would otherwise be attached to, or
        # deleted by, an import send.
        import_lead = None
        send_subject, send_body = req.subject, req.body
        if req.import_lead_id is not None:
            import_lead = await _prepare_import_send(req)
            send_subject, send_body = import_lead["subject"], import_lead["body"]

```

5. Replace the line `draft = await asyncio.to_thread(db.get_draft_by_website, req.website)` with:

```python
        draft = None if import_lead is not None else await asyncio.to_thread(db.get_draft_by_website, req.website)
```

6. Change the screenshot chain so it begins `if import_lead is not None:\n            pass  # import sends never carry audit screenshots\n        elif not req.attach_screenshot:` (keep the existing `print` and the following `elif req.company and req.website:` unchanged).

7. Replace the whole region from `message_id = await asyncio.to_thread(` through `raise HTTPException(status_code=400, detail=f"{req.email} is on the unsubscribe/suppression list")` (Read the lines first and copy them exactly as the Edit `old_string`) with the block below. Every existing comment in that region is kept verbatim in the real file; the changes are marked `# IMPORT`:

```python
        try:
            message_id = await asyncio.to_thread(
                ses.send_email, req.email, send_subject, send_body,   # IMPORT: stored copy for imports
                image_path=image_path, mobile_image_path=mobile_image_path,
                closeup_image_path=closeup_image_path,
            )
        except Exception as send_error:
            if import_lead is not None:   # IMPORT: keep the real transport error on the lead
                await asyncio.to_thread(
                    import_store.update_lead, import_lead["id"],
                    status="failed", error=str(send_error)[:500],
                )
            raise
        success = bool(message_id)

        if success:
            if import_lead is None:   # IMPORT: no Sheets row exists for imported leads
                def save_send_to_sheets():
                    try:
                        row = sheets.find_row_by_website(req.website)
                        if row:
                            sheets.update_status(row, "emailed")
                            sheets.set_message_id(row, message_id)
                    except Exception as e:
                        print(f"Error updating send status in sheets: {e}")
                background_tasks.add_task(save_send_to_sheets)

            # (keep the existing "Log exact costs and email history" comment)
            await asyncio.to_thread(db.log_cost, "AWS SES", 0.0001, description=f"Email to {req.email}")
            # (keep the existing variant comments verbatim)
            variant_sent = (
                f"{config.EMAIL_VARIANT}+auto"
                if draft and draft.get("include_automation_pitch")
                else config.EMAIL_VARIANT
            )
            if import_lead is not None:   # IMPORT: its own bucket in get_variant_performance()
                variant_sent = import_send_policy.IMPORT_VARIANT
            await asyncio.to_thread(
                db.log_email, req.company, req.website, req.email, config.FROM_EMAIL,
                send_subject, send_body, message_id=message_id,   # IMPORT
                variant=variant_sent,
                sector=(draft.get("sector") or "") if draft else "",
                niche=(draft.get("niche") or "") if draft else "",
            )

            if import_lead is not None:   # IMPORT: mark sent and link the history row
                history_row = await asyncio.to_thread(db.get_email_history_by_message_id, message_id)
                await asyncio.to_thread(
                    import_store.update_lead, import_lead["id"],
                    status="sent", sent_at=import_store.now_sql(),
                    history_id=history_row["id"] if history_row else None, error=None,
                )
            else:
                # Remove from drafts since it's sent
                await asyncio.to_thread(db.delete_draft_by_website, req.website)

            return {"status": "success"}
        else:
            if import_lead is not None:   # IMPORT
                await asyncio.to_thread(
                    import_store.update_lead, import_lead["id"],
                    status="skipped", skip_reason="suppressed",
                )
            raise HTTPException(status_code=400, detail=f"{req.email} is on the unsubscribe/suppression list")
```

8. Add the status route in the import section (after `import_edit`, above the static mount):

```python
@app.get("/api/import/status")
async def import_status(
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(120, 60)),
):
    sent_today = await asyncio.to_thread(db.count_emails_sent_today)
    rows = await asyncio.to_thread(db.get_variant_performance)
    return {
        "sent_today": sent_today,
        "daily_limit": config.DAILY_EMAIL_LIMIT,
        "remaining": max(0, config.DAILY_EMAIL_LIMIT - sent_today),
        "bounce": import_send_policy.bounce_status(
            rows, config.IMPORT_BOUNCE_MIN_SENT, config.IMPORT_BOUNCE_PAUSE_PCT
        ),
        "mailing_address_set": bool(config.MAILING_ADDRESS),
    }
```

Add `"import_status"` to `ROUTE_FUNCS` in `test_import_endpoints.py`.

- [ ] **Step 6: Run tests, then the existing send suites**

Run: `python -m pytest test_import_send.py test_import_endpoints.py -v`
Expected: PASS.
Run: `python -m pytest test_draft_controls.py test_followup_sequence.py test_open_tracking.py test_email_copy.py test_sent_websites.py -v`
Expected: PASS (the non-import `/api/send` path is unchanged).

- [ ] **Step 7: Commit**

```bash
git add importer/send_policy.py app.py test_import_send.py test_import_endpoints.py
git commit -m "feat: send imported leads through /api/send with compliance and bounce gates"
```

---

### Task 7: Import page UI

**Files:**
- Create: `frontend/src/ImportView.jsx`
- Modify: `frontend/src/App.jsx` (lucide import line 3, `HELP_CONTENT`, nav button after History, view case in `<main>`)

**Interfaces:**
- Consumes: routes from Tasks 3, 4, 6; the global axios `X-API-Key` header already set at the top of `App.jsx`.
- Produces: default export `ImportView` (no props).

- [ ] **Step 1: Create the component**

Create `frontend/src/ImportView.jsx`:

```jsx
import { useState, useEffect, useCallback, useRef } from 'react';
import axios from 'axios';
import { Upload, Loader2, Check, X, AlertTriangle, Send, RotateCcw, Ban, Sparkles } from 'lucide-react';

const API_BASE = '';
const PAGE = 50;
const SKIP_LABELS = {
  invalid_email: 'Invalid email', unsupported_country: 'Unsupported country',
  duplicate_in_file: 'Duplicate in file', already_imported: 'Already imported',
  suppressed: 'Unsubscribed', already_emailed: 'Already emailed',
  company_already_emailed: 'Company already emailed', gov_domain: 'Government',
  second_contact: 'Second contact', manual: 'Skipped by you',
};
const RESTORABLE = ['gov_domain', 'second_contact', 'manual'];
const STATUS_COLOR = {
  ready: '#38bdf8', drafted: '#a78bfa', approved: '#10b981', sent: '#64748b',
  failed: '#f87171', skipped: '#f59e0b',
};

const errText = (err) => {
  const d = err?.response?.data?.detail;
  if (!d) return err?.message || 'Request failed';
  if (typeof d === 'string') return d;
  return d.message || JSON.stringify(d);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const card = { padding: 16, background: 'rgba(255,255,255,0.04)', border: '1px solid rgba(255,255,255,0.1)', borderRadius: 12 };
const btn = (color = '#38bdf8', disabled = false) => ({
  display: 'inline-flex', alignItems: 'center', gap: 6, padding: '8px 14px', borderRadius: 8,
  border: `1px solid ${color}`, background: 'transparent', color, cursor: disabled ? 'not-allowed' : 'pointer',
  opacity: disabled ? 0.45 : 1, fontSize: 13,
});
const input = { background: 'rgba(15,23,42,0.6)', border: '1px solid rgba(255,255,255,0.15)', borderRadius: 8, color: '#e2e8f0', padding: '8px 10px', fontSize: 13 };

export default function ImportView() {
  const [batchId, setBatchId] = useState(() => {
    try { return Number(localStorage.getItem('importBatchId')) || null; } catch { return null; }
  });
  const [summary, setSummary] = useState(null);
  const [page, setPage] = useState({ leads: [], total: 0, facets: {} });
  const [filters, setFilters] = useState({ status: '', industry: '', seniority: '', state: '', q: '' });
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState(new Set());
  const [status, setStatus] = useState(null);
  const [message, setMessage] = useState('');
  const [uploading, setUploading] = useState(false);
  const [review, setReview] = useState(null);       // lead being reviewed
  const [edit, setEdit] = useState({ subject: '', body: '' });
  const [ack, setAck] = useState(false);
  const [job, setJob] = useState(null);             // { kind, done, total, failed, note, finished }
  const stopRef = useRef(false);

  const loadStatus = useCallback(() => {
    axios.get(`${API_BASE}/api/import/status`).then((r) => setStatus(r.data)).catch(() => {});
  }, []);

  const loadLeads = useCallback(() => {
    if (!batchId) return Promise.resolve();
    const params = { limit: PAGE, offset };
    Object.entries(filters).forEach(([k, v]) => { if (v) params[k] = v; });
    return axios.get(`${API_BASE}/api/import/batches/${batchId}/leads`, { params })
      .then((r) => setPage(r.data))
      .catch((e) => setMessage(errText(e)));
  }, [batchId, filters, offset]);

  useEffect(() => { loadStatus(); }, [loadStatus]);
  useEffect(() => { loadLeads(); }, [loadLeads]);
  useEffect(() => {
    if (!batchId) return;
    try { localStorage.setItem('importBatchId', String(batchId)); } catch { /* storage may be blocked */ }
  }, [batchId]);

  const handleFile = async (e) => {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (!file) return;
    setUploading(true); setMessage('');
    try {
      const csv_text = await file.text();
      const r = await axios.post(`${API_BASE}/api/import/upload`, { filename: file.name, csv_text });
      setSummary(r.data); setBatchId(r.data.batch_id); setOffset(0); setSelected(new Set());
      setFilters({ status: '', industry: '', seniority: '', state: '', q: '' });
      loadStatus();
    } catch (err) { setMessage(errText(err)); }
    setUploading(false);
  };

  const replaceLead = (lead) => setPage((p) => ({ ...p, leads: p.leads.map((l) => (l.id === lead.id ? lead : l)) }));

  const act = async (lead, action) => {
    try {
      const r = await axios.post(`${API_BASE}/api/import/leads/${lead.id}/${action}`);
      replaceLead(r.data.lead); loadLeads();
    } catch (err) { setMessage(errText(err)); loadLeads(); }
  };

  const toggle = (id) => setSelected((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  const readyOnPage = page.leads.filter((l) => ['ready', 'failed'].includes(l.status));

  const draftSelected = async () => {
    const ids = [...selected];
    if (!ids.length) return;
    stopRef.current = false;
    let done = 0; let failed = 0; let note = '';
    setJob({ kind: 'Drafting', done, total: ids.length, failed, note });
    for (const id of ids) {
      if (stopRef.current) { note = 'Stopped.'; break; }
      try { await axios.post(`${API_BASE}/api/import/leads/${id}/draft`); } catch (err) {
        failed += 1; note = errText(err);
        if (err.response?.status === 429) { await sleep(15000); }
      }
      done += 1; setJob({ kind: 'Drafting', done, total: ids.length, failed, note });
    }
    setJob({ kind: 'Drafting', done, total: ids.length, failed, note: note || 'Finished.', finished: true });
    setSelected(new Set()); loadLeads();
  };

  const sendApproved = async () => {
    stopRef.current = false;
    const remaining = status?.remaining ?? 0;
    let queue = [];
    try {
      const list = await axios.get(`${API_BASE}/api/import/batches/${batchId}/leads`, { params: { status: 'approved', limit: 200 } });
      queue = list.data.leads.slice(0, remaining);
    } catch (err) { setMessage(errText(err)); return; }
    let done = 0; let failed = 0; let note = '';
    setJob({ kind: 'Sending', done, total: queue.length, failed, note });
    for (const lead of queue) {
      if (stopRef.current) { note = 'Stopped.'; break; }
      let attempt = 0;
      // The send route allows 10 per minute; the jittered pause below keeps us under it.
      while (attempt < 2) {
        try {
          await axios.post(`${API_BASE}/api/send`, {
            email: lead.email, subject: lead.subject, body: lead.body,
            company: lead.organization || lead.domain, website: lead.domain,
            import_lead_id: lead.id, acknowledge_warnings: true,
          });
          done += 1; break;
        } catch (err) {
          const detail = errText(err);
          if (err.response?.status === 429 && /Daily sending limit/i.test(detail)) { note = detail; stopRef.current = true; break; }
          if (err.response?.status === 429 && attempt === 0) { attempt += 1; await sleep(20000); continue; }
          if (err.response?.data?.detail?.code === 'import_paused') { note = detail; stopRef.current = true; break; }
          failed += 1; note = detail; break;
        }
      }
      setJob({ kind: 'Sending', done, total: queue.length, failed, note });
      if (stopRef.current) break;
      await sleep(6500 + Math.random() * 2500);
    }
    setJob({ kind: 'Sending', done, total: queue.length, failed, note: note || 'Finished.', finished: true });
    loadStatus(); loadLeads();
  };

  const openReview = (lead) => { setReview(lead); setEdit({ subject: lead.subject || '', body: lead.body || '' }); setAck(false); };

  const saveEdit = async () => {
    try {
      const r = await axios.put(`${API_BASE}/api/import/leads/${review.id}`, edit);
      setReview(r.data.lead); replaceLead(r.data.lead); setAck(false); setMessage('');
    } catch (err) { setMessage(errText(err)); }
  };

  const approveReview = async () => {
    try {
      const r = await axios.post(`${API_BASE}/api/import/leads/${review.id}/approve`, { acknowledge_warnings: ack });
      setReview(r.data.lead); replaceLead(r.data.lead); loadLeads(); setMessage('');
    } catch (err) { setMessage(errText(err)); }
  };

  const unapproveReview = async () => {
    try {
      const r = await axios.post(`${API_BASE}/api/import/leads/${review.id}/unapprove`);
      setReview(r.data.lead); replaceLead(r.data.lead); loadLeads();
    } catch (err) { setMessage(errText(err)); }
  };

  const usBlocked = !!status && !status.mailing_address_set && (summary ? summary.us_ready > 0 : true);
  const facets = page.facets || {};
  const busy = !!job && !job.finished;
  const approvedCount = facets.status?.approved || 0;
  const sendDisabled = busy || approvedCount === 0 || usBlocked || !!status?.bounce?.paused;

  return (
    <div className="glass" style={{ padding: 24, display: 'flex', flexDirection: 'column', gap: 20 }}>
      <div>
        <h2 style={{ margin: 0 }}><Upload size={22} style={{ verticalAlign: 'middle', marginRight: 8 }} />Import leads from CSV</h2>
        <p style={{ color: '#94a3b8', margin: '6px 0 0' }}>
          Upload a lead list, let the AI draft one email per lead, read and approve each draft, then send a day's worth at a time.
        </p>
      </div>

      {usBlocked && (
        <div style={{ ...card, borderColor: '#f59e0b', color: '#fbbf24' }}>
          <AlertTriangle size={16} style={{ verticalAlign: 'middle', marginRight: 6 }} />
          US leads cannot be sent yet: set the <b>MAILING_ADDRESS</b> environment variable (a postal address is required in the footer under CAN-SPAM).
        </div>
      )}
      {status?.bounce?.paused && (
        <div style={{ ...card, borderColor: '#f87171', color: '#fca5a5' }}>
          <Ban size={16} style={{ verticalAlign: 'middle', marginRight: 6 }} />
          Import sending is paused: {status.bounce.bounced} of {status.bounce.sent} bounced ({status.bounce.rate_pct}%, limit {status.bounce.pause_pct}%).
        </div>
      )}
      {message && <div style={{ ...card, borderColor: '#f87171', color: '#fca5a5' }}>{message}</div>}

      <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', alignItems: 'center' }}>
        <label style={{ ...btn('#10b981', uploading), cursor: uploading ? 'wait' : 'pointer' }}>
          {uploading ? <Loader2 size={14} /> : <Upload size={14} />} Choose CSV file
          <input type="file" accept=".csv,text/csv" onChange={handleFile} style={{ display: 'none' }} disabled={uploading} />
        </label>
        {status && (
          <span style={{ color: '#cbd5e1', fontSize: 13 }}>
            Sent today {status.sent_today} of {status.daily_limit}, {status.remaining} left
            {status.bounce?.sent > 0 && ` · bounce rate ${status.bounce.rate_pct}% over ${status.bounce.sent} sent`}
          </span>
        )}
      </div>

      {summary && (
        <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
          <div style={card}><div style={{ color: '#94a3b8', fontSize: 12 }}>Total rows</div><b style={{ fontSize: 22 }}>{summary.total}</b></div>
          <div style={card}><div style={{ color: '#94a3b8', fontSize: 12 }}>Ready to draft</div><b style={{ fontSize: 22, color: '#38bdf8' }}>{summary.ready}</b></div>
          {Object.entries(summary.skip_breakdown || {}).map(([reason, n]) => (
            <div key={reason} style={card}><div style={{ color: '#94a3b8', fontSize: 12 }}>{SKIP_LABELS[reason] || reason}</div><b style={{ fontSize: 22, color: '#f59e0b' }}>{n}</b></div>
          ))}
        </div>
      )}

      {batchId && (
        <>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <select style={input} value={filters.status} onChange={(e) => { setFilters({ ...filters, status: e.target.value }); setOffset(0); }}>
              <option value="">All statuses</option>
              {Object.entries(facets.status || {}).map(([k, n]) => <option key={k} value={k}>{k} ({n})</option>)}
            </select>
            <select style={input} value={filters.industry} onChange={(e) => { setFilters({ ...filters, industry: e.target.value }); setOffset(0); }}>
              <option value="">All industries</option>
              {Object.entries(facets.industry || {}).map(([k, n]) => <option key={k} value={k}>{k} ({n})</option>)}
            </select>
            <select style={input} value={filters.seniority} onChange={(e) => { setFilters({ ...filters, seniority: e.target.value }); setOffset(0); }}>
              <option value="">All seniority</option>
              {Object.entries(facets.seniority || {}).map(([k, n]) => <option key={k} value={k}>{k} ({n})</option>)}
            </select>
            <select style={input} value={filters.state} onChange={(e) => { setFilters({ ...filters, state: e.target.value }); setOffset(0); }}>
              <option value="">All states</option>
              {Object.entries(facets.state || {}).map(([k, n]) => <option key={k} value={k}>{k} ({n})</option>)}
            </select>
            <input style={{ ...input, minWidth: 180 }} placeholder="Search name, company, email" value={filters.q}
              onChange={(e) => { setFilters({ ...filters, q: e.target.value }); setOffset(0); }} />
          </div>

          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center' }}>
            <button style={btn('#a78bfa', busy || selected.size === 0)} disabled={busy || selected.size === 0} onClick={draftSelected}>
              <Sparkles size={14} /> Draft {selected.size} selected
            </button>
            <button style={btn('#38bdf8', busy)} disabled={busy} onClick={() => setSelected(new Set(readyOnPage.map((l) => l.id)))}>
              Select all ready on this page
            </button>
            <button style={btn('#10b981', sendDisabled)} disabled={sendDisabled} onClick={sendApproved}>
              <Send size={14} /> Send up to {Math.min(approvedCount, status?.remaining ?? 0)} approved
            </button>
            {busy && <button style={btn('#f87171')} onClick={() => { stopRef.current = true; }}><X size={14} /> Stop</button>}
          </div>

          {job && (
            <div style={card}>
              <b>{job.kind}</b> {job.done} of {job.total}{job.failed > 0 && `, ${job.failed} failed`}
              <div style={{ height: 6, background: 'rgba(255,255,255,0.1)', borderRadius: 3, marginTop: 8 }}>
                <div style={{ height: 6, width: `${job.total ? (job.done / job.total) * 100 : 0}%`, background: '#10b981', borderRadius: 3 }} />
              </div>
              {job.note && <div style={{ color: '#94a3b8', fontSize: 12, marginTop: 6 }}>{job.note}</div>}
            </div>
          )}

          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
              <thead>
                <tr style={{ color: '#94a3b8', textAlign: 'left', borderBottom: '1px solid rgba(255,255,255,0.1)' }}>
                  <th style={{ padding: 8 }} />
                  <th style={{ padding: 8 }}>Name</th><th style={{ padding: 8 }}>Title</th>
                  <th style={{ padding: 8 }}>Company</th><th style={{ padding: 8 }}>Email</th>
                  <th style={{ padding: 8 }}>Status</th><th style={{ padding: 8 }} />
                </tr>
              </thead>
              <tbody>
                {page.leads.map((l) => (
                  <tr key={l.id} style={{ borderBottom: '1px solid rgba(255,255,255,0.05)' }}>
                    <td style={{ padding: 8 }}>
                      {['ready', 'failed'].includes(l.status) && (
                        <input type="checkbox" checked={selected.has(l.id)} onChange={() => toggle(l.id)} disabled={busy} />
                      )}
                    </td>
                    <td style={{ padding: 8 }}>{l.person_name}</td>
                    <td style={{ padding: 8, color: '#94a3b8' }}>{l.person_title}</td>
                    <td style={{ padding: 8 }}>{l.organization}</td>
                    <td style={{ padding: 8, color: '#94a3b8' }}>{l.email}</td>
                    <td style={{ padding: 8, color: STATUS_COLOR[l.status] || '#cbd5e1' }}>
                      {l.status}{l.skip_reason ? `: ${SKIP_LABELS[l.skip_reason] || l.skip_reason}` : ''}
                      {l.error && <div style={{ color: '#f87171', fontSize: 11 }}>{l.error}</div>}
                    </td>
                    <td style={{ padding: 8, whiteSpace: 'nowrap' }}>
                      {['drafted', 'approved', 'failed'].includes(l.status) && l.body && (
                        <button style={btn('#a78bfa')} onClick={() => openReview(l)}>Review</button>
                      )}{' '}
                      {l.status === 'skipped' && RESTORABLE.includes(l.skip_reason) && (
                        <button style={btn('#10b981')} onClick={() => act(l, 'restore')}><RotateCcw size={12} /> Restore</button>
                      )}
                      {['ready', 'drafted', 'approved', 'failed'].includes(l.status) && (
                        <button style={btn('#f59e0b')} onClick={() => act(l, 'skip')}>Skip</button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {page.leads.length === 0 && <p style={{ textAlign: 'center', color: '#64748b', padding: 24 }}>No leads match.</p>}
          </div>

          <div style={{ display: 'flex', gap: 10, alignItems: 'center', color: '#94a3b8', fontSize: 13 }}>
            <button style={btn('#38bdf8', offset === 0)} disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Previous</button>
            <span>{page.total === 0 ? 0 : offset + 1} to {Math.min(offset + PAGE, page.total)} of {page.total}</span>
            <button style={btn('#38bdf8', offset + PAGE >= page.total)} disabled={offset + PAGE >= page.total} onClick={() => setOffset(offset + PAGE)}>Next</button>
          </div>
        </>
      )}

      {review && (
        <div style={{ position: 'fixed', inset: 0, background: 'rgba(2,6,23,0.7)', zIndex: 30, display: 'flex', justifyContent: 'flex-end' }}
          onClick={() => setReview(null)}>
          <div style={{ width: 'min(560px, 96vw)', background: '#0f172a', borderLeft: '1px solid rgba(255,255,255,0.15)', padding: 24, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 12 }}
            onClick={(e) => e.stopPropagation()}>
            <div style={{ display: 'flex', justifyContent: 'space-between' }}>
              <h3 style={{ margin: 0 }}>{review.person_name} · {review.organization}</h3>
              <button style={{ background: 'transparent', border: 'none', color: '#94a3b8', cursor: 'pointer' }} onClick={() => setReview(null)}><X size={18} /></button>
            </div>
            <div style={{ color: '#94a3b8', fontSize: 12 }}>{review.person_title} · {review.email} · status {review.status}</div>

            {review.review_warnings?.length > 0 && (
              <div style={{ ...card, borderColor: '#f87171', color: '#fca5a5' }}>
                <b><AlertTriangle size={14} style={{ verticalAlign: 'middle' }} /> Review before sending</b>
                <ul style={{ margin: '8px 0 0', paddingLeft: 18 }}>{review.review_warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>
                {review.status !== 'approved' && (
                  <label style={{ display: 'flex', gap: 8, marginTop: 10, color: '#e2e8f0' }}>
                    <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} /> I have read these warnings
                  </label>
                )}
              </div>
            )}
            {review.source_quote && (
              <div style={{ ...card, color: '#94a3b8', fontSize: 12 }}>Evidence from their homepage: “{review.source_quote}”</div>
            )}

            <input style={input} value={edit.subject} onChange={(e) => setEdit({ ...edit, subject: e.target.value })} />
            <textarea style={{ ...input, minHeight: 280, fontFamily: 'inherit', lineHeight: 1.5 }} value={edit.body}
              onChange={(e) => setEdit({ ...edit, body: e.target.value })} />
            <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
              <button style={btn('#38bdf8', edit.subject === review.subject && edit.body === review.body)}
                disabled={edit.subject === review.subject && edit.body === review.body} onClick={saveEdit}>Save edit</button>
              {review.status === 'approved' ? (
                <button style={btn('#f59e0b')} onClick={unapproveReview}>Un-approve</button>
              ) : (
                <button style={btn('#10b981', review.review_warnings?.length > 0 && !ack)}
                  disabled={review.review_warnings?.length > 0 && !ack} onClick={approveReview}>
                  <Check size={14} /> Approve
                </button>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Wire it into `App.jsx`**

1. Line 3 lucide import: add `FileSpreadsheet` to the list, and add below the imports: `import ImportView from './ImportView.jsx';`.
2. In `HELP_CONTENT` add after the `history` entry:

```jsx
  import: {
    title: 'Import: emailing a lead list from a CSV',
    tips: [
      'Upload a CSV with an email column. Rows that are unsafe or redundant are skipped automatically and the reason is shown: invalid email, unsupported country, duplicates, unsubscribed, already emailed, government domains and second contacts at the same company.',
      'Government and second contact skips can be restored with one click. A restore is re-checked against your unsubscribe list and send history.',
      'Select leads and click Draft: the AI reads the company homepage and writes one short email per lead. Nothing sends until you have read a draft and approved it.',
      'A red warning box means a check flagged the draft. Read it, tick the box and approve, or edit the text and save, which re-runs the checks.',
      '"Send up to N approved" sends slowly on purpose, and never past the daily limit. If more than 5 percent of the first 20 or more sends bounce, sending pauses by itself.',
      'US leads cannot be sent until the MAILING_ADDRESS environment variable is set on the server.',
    ],
  },
```

3. In the nav, add after the History button's closing `</button>` (line ~3548):

```jsx
          <button
            onClick={() => setCurrentView('import')}
            style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '10px 16px', background: currentView === 'import' ? 'rgba(255,255,255,0.15)' : 'transparent', border: 'none', borderRadius: '8px', color: currentView === 'import' ? '#fff' : '#94a3b8', cursor: 'pointer', fontSize: '15px', fontWeight: currentView === 'import' ? 'bold' : 'normal', transition: 'all 0.2s' }}
          >
            <FileSpreadsheet size={18} /> Import
          </button>
```

4. In `<main>` after `{currentView === 'cost' && renderCost()}` add: `{currentView === 'import' && <ImportView />}`.

- [ ] **Step 3: Build**

Run (from `frontend/`): `npm run build`
Expected: build succeeds with no errors. If `FileSpreadsheet` is missing from the installed lucide version, use `FileText` (already imported).

- [ ] **Step 4: Manual smoke check (record the outcome in the final report)**

Start the backend (`python -m uvicorn app:app --port 8000`) with a temp DB and open the built app. Upload a small CSV (3 rows, one `.gov`), confirm the summary cards, restore the gov row, select a ready lead, draft it (needs a real AI key; otherwise confirm the 502 message shows in the red box), open Review, edit, approve. Do not click send against real recipients. State plainly in the report which steps were exercised and which were not.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/ImportView.jsx frontend/src/App.jsx
git commit -m "feat: Import page for CSV lead outreach"
```

---

### Task 8: Documentation, spec sync and full verification

**Files:**
- Modify: `CLAUDE.md`, `docs/superpowers/specs/2026-10-15-csv-import-outreach-design.md`

- [ ] **Step 1: Sync the spec with the plan-level refinements**

In the spec: add `source_quote TEXT` to the §6 table; add the approve-time acknowledgement rule to §10 item 3 and §12 item 6; note in §12 that the view is `ImportView.jsx`; state the body order in §9 (greeting, paragraphs, fixed line, sign off). Change `Status: Draft for review` to `Status: Implemented`.

- [ ] **Step 2: Update `CLAUDE.md`**

Read `CLAUDE.md` first. Then: (a) add `MAILING_ADDRESS`, `IMPORT_BOUNCE_PAUSE_PCT`, `IMPORT_BOUNCE_MIN_SENT` to the environment variable table (section 4); (b) add a changelog entry dated 2026-10-15 describing the Import tab, schema v14, `import_lead_id` on `/api/send`, the `import-automation` variant, the footer and the bounce pause; (c) add one gotcha line: import routes must stay above the `/` static mount at the bottom of `app.py`.

- [ ] **Step 3: Full suite and build**

Run: `python -m pytest -q` (no background runs; do not edit Python meanwhile).
Expected: all pass (previous baseline 942 passed, 1 skipped, plus the new tests). Report the exact counts.
Run: `npm run build` in `frontend/`. Expected: clean.

- [ ] **Step 4: Verify the acceptance criteria**

Tick each spec §17 item against a named test:
1. `test_real_file_shape_yields_890_ready_44_gov_66_second_contacts`
2. `test_us_lead_is_blocked_without_a_mailing_address`, `test_initial_email_carries_address_and_optout_link`
3. `test_the_daily_cap_is_still_enforced` (the queue is in SQLite, so it survives restarts; `test_import_store.py` covers reload)
4. `test_sends_the_stored_copy_not_the_clients`, `test_unapproved_missing_and_mismatched_leads_are_refused`
5. `test_an_import_send_never_attaches_screenshots_or_touches_audit_drafts`
6. `test_the_bounce_pause_blocks_further_sends`
7. full suite + build

- [ ] **Step 5: Commit and hand back**

```bash
git add CLAUDE.md docs/superpowers/specs/2026-10-15-csv-import-outreach-design.md
git commit -m "docs: CSV import outreach notes and spec sync"
```

Then report to the user: what shipped, the test counts, what was not exercised (a real AI draft, a real SES send, the real CSV), and ask whether to merge `feat/csv-import-outreach` into `main` and push. Do not push without being told to.

---

## Self-review notes

- **Spec coverage:** §6 schema (T1), §7 rules (T2), §8 routes (T3 list/restore/skip/approve, T4 draft/edit, T6 status), §9 drafting (T4), §10 sending (T6), §11 gates (T5 footer, T6 US gate and bounce pause), §12 UI (T7), §13 config (T1), §14 errors (T3, T4, T6 tests), §15 tests (every task), §17 acceptance (T8 step 4).
- **Type consistency:** blockers keys `imported, suppressed, emailed, company_hosts` are identical in `load_blockers`, `empty_blockers`, `csv_rules`, `send_policy`. Lead field names are `LEAD_FIELDS` everywhere. `FlowError(status_code, detail)` and `SendBlocked(status_code, detail, skip_reason)` are used consistently. Route function names in `ROUTE_FUNCS` match the definitions: `import_upload, import_batches, import_batch_leads, import_restore, import_skip, import_approve, import_unapprove, import_draft, import_edit, import_status`.
- **Known limits carried from the spec:** bounce visibility lags the reply checker; the list is unverified; audience fit unproven (the `import-automation` variant exists to measure it).
