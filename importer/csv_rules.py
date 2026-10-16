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

_EMAIL_BAD = '@\\s,;<>"()\\[\\]\\\\'
_EMAIL_RE = re.compile(rf"^[^{_EMAIL_BAD}]+@[^{_EMAIL_BAD}]+\.[^{_EMAIL_BAD}]+$")
_GOV_SUFFIXES = (".gov", ".mil", ".gov.in", ".nic.in")
_COUNTRY_ALIASES = {
    "usa": "united states", "us": "united states", "u.s.": "united states",
    "u.s.a.": "united states", "united states of america": "united states",
}

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
    v = v.rstrip(".")
    return v[4:] if v.startswith("www.") else v


def _norm_country(value: str) -> str:
    v = re.sub(r"\s+", " ", (value or "").strip().lower())
    return _COUNTRY_ALIASES.get(v, v)


def _norm_seniority(value: str) -> str:
    return re.sub(r"[\s\-]+", "_", (value or "").strip().lower())


def parse_csv(text: str) -> list[dict]:
    if len(text.encode("utf-8")) > MAX_CSV_BYTES:
        raise CsvImportError(f"File is larger than {MAX_CSV_BYTES // (1024 * 1024)} MB.", 413)
    text = text.lstrip("﻿")
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
            "person_seniority": _norm_seniority(got.get("person_seniority", "")),
            "email": got.get("email", "").lower(),
            "organization": got.get("organization", ""),
            "domain": _clean_domain(got.get("domain", "")),
            "industry": got.get("industry", ""),
            "city": got.get("city", ""),
            "state": got.get("state", ""),
            "country": _norm_country(got.get("country", "")),
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
    return any(d.endswith(_GOV_SUFFIXES) for d in (company_key(lead), email_domain))


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
    rank = _SENIORITY_RANK.get(_norm_seniority(lead.get("person_seniority") or ""), 0)
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
