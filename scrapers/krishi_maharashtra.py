"""
Krishi Maharashtra Scraper — parses the Maharashtra Department of Agriculture's
own public dealer/manufacturer license lists (krishi.maharashtra.gov.in) into
leads.

Unlike every other scraper in this project, this is not search-engine dorking
or a paid API — it's static files the state government itself publishes,
listing every firm licensed to deal in seed/fertilizer for the Maharashtra
market. Confirmed live (2026-08-17) that every list uses the same table
shape and already contains, per row: firm name, district, taluka, a named
responsible person, mobile number and email — the exact contact fields every
other source here has to guess at via OSINT/enrichment. No ToS risk either:
it's the government's own published data, not scraped from a third party.

Two file types, one row schema:
- A PDF (`_PDF_URLS`) — the original cotton-seed dealer list, parsed via
  `pdfplumber.extract_table()`.
- An Excel workbook (`_XLSX_URLS`), found the same day — one file, four
  sheets: Fertilizer Manufacturer (443 rows), Fertilizer Dealer (855),
  Cotton Seed Dealer-State (1087), Cotton Seed-District (24,932). Parsed via
  `pandas`/`openpyxl`. Each sheet name becomes the lead's `Category`, so the
  existing "nearby search" Category badge in the frontend (`lead.Category`,
  App.jsx) renders it for free — no frontend change needed for that part.
Both share `_extract_leads_from_table()`, which finds the same columns by
header-substring match regardless of source, so a differently-ordered sheet
or a new PDF page format doesn't need new parsing code.

Real limitation, stated plainly: none of these lists have a Website column,
so every lead from this source comes through with Website blank — same
"cannot audit, but here's a phone for WhatsApp" path as IndiaMartDorkScraper
leads. The audit pipeline (which needs a site to measure) simply never
fires for these; the phone/email still make them usable outreach targets.

Fertilizer/pesticide sibling *PDFs* mentioned in some portal navigation
weren't located as distinct files — turned out the Excel workbook above
already covers fertilizer manufacturer/dealer data more completely than a
separate PDF would have. Add more URLs to _PDF_URLS/_XLSX_URLS if the portal
publishes further lists, rather than guessing at names that weren't verified.
"""

import re
import time
from io import BytesIO

import httpx
import pandas as pd
import pdfplumber

_PDF_URLS = [
    "https://krishi.maharashtra.gov.in/Site/Upload/Pdf/MahaParwana_Firms_Data_Seed_7_QC.pdf?MenuID=1036",
]

_XLSX_URLS = [
    "https://krishi.maharashtra.gov.in/Site/Upload/Pdf/Fertilizers_Approved_license_list.xlsx?MenuID=1045",
]

# These are static government publications, not a live feed — re-downloading
# and re-parsing a multi-sheet workbook (one sheet alone is ~25,000 rows) on
# every dashboard click would be pure waste. Cached process-lifetime with a
# generous TTL rather than the 10-15 min pattern used elsewhere for API
# results, since this source changes on a government update cycle.
_CACHE_TTL_SECONDS = 6 * 60 * 60
_cache: dict = {"leads": None, "fetched_at": 0.0}


def _clean(cell) -> str:
    if cell is None:
        return ""
    if isinstance(cell, float) and pd.isna(cell):
        return ""
    return str(cell).replace("\n", " ").strip()


def _extract_leads_from_table(header: list[str], rows: list[list], category: str, source: str) -> list[dict]:
    """
    Shared row->lead mapping for both the PDF's `extract_table()` output and
    an Excel sheet's rows. Finds columns by header substring rather than a
    fixed index, since the PDF and the four Excel sheets don't all order
    their columns identically.
    """
    header_lower = [str(h or "").lower() for h in header]

    def col(*names):
        for name in names:
            for i, h in enumerate(header_lower):
                if name in h:
                    return i
        return None

    i_firm = col("firm name")
    i_dist = col("firm dist")
    i_taluka = col("firm taluka")
    i_person = col("responsible person")
    i_mobile = col("mobile")
    i_mail = col("mail")

    if i_firm is None:
        return []  # not a table in the expected shape

    leads = []
    for row in rows:
        firm = _clean(row[i_firm]) if i_firm < len(row) else ""
        if not firm:
            continue

        district = _clean(row[i_dist]) if i_dist is not None and i_dist < len(row) else ""
        taluka = _clean(row[i_taluka]) if i_taluka is not None and i_taluka < len(row) else ""
        phone_raw = _clean(row[i_mobile]) if i_mobile is not None and i_mobile < len(row) else ""
        phone = re.sub(r"\D", "", phone_raw)

        leads.append({
            "Company": firm,
            "Website": "",
            "Phone": phone,
            "Address": ", ".join(p for p in (taluka, district) if p),
            "Rating": "",
            "Reviews Count": 0,
            "Email": _clean(row[i_mail]) if i_mail is not None and i_mail < len(row) else "",
            "Instagram Handle": "",
            "Decision Maker Name": _clean(row[i_person]) if i_person is not None and i_person < len(row) else "",
            "Source": source,
            "Category": category,
            "District": district,
        })
    return leads


def _parse_pdf(pdf_bytes: bytes) -> list[dict]:
    leads = []
    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            table = page.extract_table()
            if not table or len(table) < 2:
                continue
            leads.extend(_extract_leads_from_table(
                table[0], table[1:],
                category="Cotton Seed Dealer",
                source="Maharashtra Seed Dealer License (krishi.maharashtra.gov.in)",
            ))
    return leads


def _parse_xlsx(xlsx_bytes: bytes) -> list[dict]:
    leads = []
    xl = pd.ExcelFile(BytesIO(xlsx_bytes))
    for sheet_name in xl.sheet_names:
        df = xl.parse(sheet_name)
        if df.empty:
            continue
        leads.extend(_extract_leads_from_table(
            df.columns.tolist(), df.values.tolist(),
            category=sheet_name.strip(),
            source="Maharashtra Fertilizer/Seed License List (krishi.maharashtra.gov.in)",
        ))
    return leads


def _load_all_leads() -> list[dict]:
    now = time.time()
    if _cache["leads"] is not None and (now - _cache["fetched_at"]) < _CACHE_TTL_SECONDS:
        return _cache["leads"]

    all_leads = []
    for url in _PDF_URLS:
        try:
            resp = httpx.get(url, timeout=30.0, follow_redirects=True)
            resp.raise_for_status()
            all_leads.extend(_parse_pdf(resp.content))
        except Exception as e:
            print(f"[KrishiMaharashtra] Failed to fetch/parse PDF {url}: {e}")

    for url in _XLSX_URLS:
        try:
            resp = httpx.get(url, timeout=30.0, follow_redirects=True)
            resp.raise_for_status()
            all_leads.extend(_parse_xlsx(resp.content))
        except Exception as e:
            print(f"[KrishiMaharashtra] Failed to fetch/parse Excel {url}: {e}")

    # The PDF and the xlsx's "Cotton Seed Dealer -State" sheet turned out to
    # be the same underlying license list published in two formats — live-
    # confirmed both return exactly 1087 identical cotton-seed-dealer rows.
    # Dedupe on (name, phone) rather than dropping the PDF outright, since
    # that assumption could stop holding on a future government update.
    seen = set()
    deduped = []
    for lead in all_leads:
        key = (lead["Company"].strip().lower(), lead["Phone"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(lead)

    if deduped:
        _cache["leads"] = deduped
        _cache["fetched_at"] = now
    return deduped


class KrishiMaharashtraScraper:
    def scrape(self, city: str = "", limit: int = 50) -> list[dict]:
        """
        Return licensed seed/fertilizer firms, optionally filtered to a
        district/taluka matching `city` (substring, case-insensitive —
        the source lists' own place names, not GoogleMapsScraper's CITIES
        list, so an exact match isn't guaranteed for every town).
        """
        leads = _load_all_leads()
        if city:
            needle = city.strip().lower()
            leads = [l for l in leads if needle in l["Address"].lower()]
        return leads[:limit]
