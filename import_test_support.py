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
