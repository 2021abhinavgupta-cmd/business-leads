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


FLAW_DESCRIPTIONS = (
    "The main call to action button has low contrast against its background",
    "Body text on the contact page is too small to read comfortably",
    "Images on the services page have no alt text",
    "The navigation menu overlaps the logo on a phone",
    "Footer links sit too close together to tap",
)


def make_flaw_shots(tmp_path, n: int = 2) -> list[dict]:
    """n flaw shot dicts backed by real small PNG files under tmp_path."""
    from PIL import Image
    shots = []
    for i in range(n):
        path = tmp_path / f"flaw_shot_{i}.png"
        Image.new("RGB", (900, 600), (240, 200 - 30 * i, 220)).save(path)
        shots.append({
            "path": str(path), "closeup_path": None, "page": f"https://acme.com/page{i}",
            "description": FLAW_DESCRIPTIONS[i], "impact": "high", "selector": f"#el{i}",
            "kind": "desktop",
        })
    return shots


def flaw_copy(n: int = 2, *, subject="Website problems at Acme Bank", headline=None,
              opening="Here is what a visitor sees on your website today.", titles=None,
              sentences=None, ai_cost=0.001) -> dict:
    """A well formed generate_flaw_email_copy result for n shots."""
    letters = "ABCDE"
    return {
        "subject": subject,
        "headline": headline if headline is not None else f"Acme Bank has {n} website problems",
        "opening": opening,
        "sections": [
            {
                "index": i,
                "title": titles[i] if titles else f"Problem {letters[i]} on the site",
                "sentence": sentences[i] if sentences else f"Visitors can miss part {letters[i]} of the page.",
            }
            for i in range(n)
        ],
        "ai_cost": ai_cost,
    }


def seed_designed_lead(tmp_path, n: int = 2, *, status="drafted", **over) -> int:
    """A lead drafted as a designed email: audit done, n real shot files, rendered html."""
    from importer import email_html
    lead_id = seed_lead(status=status, **over)
    shots = make_flaw_shots(tmp_path, n)
    copy = flaw_copy(n)
    html, text = email_html.render(copy, shots, first_name="Jane", company="Acme Bank",
                                   reply_to="reply@mmga.example")
    import_store.update_lead(
        lead_id, audit_status="done", flaw_shots=shots, audited_at=import_store.now_sql(),
        subject=copy["subject"], body=text, html_body=html,
        review_warnings=over.get("review_warnings", []),
        design_json={
            "opening": copy["opening"], "headline": copy["headline"], "sections": copy["sections"],
            "shot_paths": [s["path"] for s in shots],
        },
    )
    return lead_id


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
