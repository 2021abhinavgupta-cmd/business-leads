# Agriculture Lead Targeting — What Was Built

Added 2026-08-17. Everything below lives inside the same dashboard you already use — nothing new to sign up for, nothing new to deploy separately. This file is a plain-language summary for you; `CLAUDE.md` has the full technical version for future dev work.

## 1. New "Agriculture" tab

A new tab in the top nav, next to Dashboard/Costs/History. Instead of typing a niche and city like the normal search, it shows a grid of 18 pre-set agriculture niches you just click:

Agricultural Equipment Dealer, Tractor Dealership, Fertilizer & Pesticide Shop, Seed Store, Dairy Farm, Poultry Farm, Cold Storage Facility, Irrigation Equipment Supplier, Agri-Tech Startup, Food Processing Unit, Grain Warehouse, Plant Nursery, Agricultural Export Company, Organic Farming Consultant, Farm Equipment Rental, Agricultural Cooperative Society, Veterinary Clinic (Livestock), Sugar Mill.

Set a city at the top, click a niche, results land in the same leads grid/audit/send flow you already know.

## 2. Four ways to find agriculture leads

Each niche card gives you two buttons, plus one standalone source:

| Source | What it is | Button |
|---|---|---|
| **Google Maps** | Same Maps search you already use elsewhere, just pre-filled with the niche | "Maps" button on each card |
| **B2B directories** | Searches IndiaMART, TradeIndia, or ExportersIndia (pick one from the dropdown above the grid) for supplier listings | "Directory" button on each card |
| **Maharashtra government dealer list** | Real government-published data — see below | "Search licensed dealers" button (standalone) |

**Why more than one source:** Google Maps skips any business with no website — and a lot of agriculture dealers/farms genuinely don't have one. The other two sources catch those.

## 3. The best one: Maharashtra's own government dealer list

This is the standout. The Maharashtra Department of Agriculture publishes its own licensed dealer/manufacturer lists publicly (`krishi.maharashtra.gov.in`) — real government data, not scraped from anywhere shady, no terms-of-service risk.

**~21,000 real leads**, already including:
- Firm name
- District & taluka (area)
- A named contact person
- **Mobile number**
- **Email address**

No guessing, no enrichment step needed — this is rarer than it sounds; every other lead source in this tool has to work to find contact info, this one hands it to you.

Covers: fertilizer manufacturers, fertilizer dealers, cotton seed dealers (state + district level).

**One real limitation:** none of these listings include a website, so this source can't feed the AI website audit. See the WhatsApp point below for what to do with them instead.

## 4. WhatsApp buttons (works for every lead, not just agriculture)

Any lead with a phone number but no website used to be a dead end — "Cannot audit, no website found," nothing else you could do. Now there's a **"Message on WhatsApp"** button that opens a pre-filled WhatsApp chat to that number. Once an audit *is* done, there's also a **"Send via WhatsApp"** button that sends the same drafted email content over WhatsApp instead of (or alongside) email.

This is what makes the Maharashtra government list actually usable — no website, but you can still reach every one of those ~21,000 contacts.

## 5. Smarter emails for agriculture leads

When an agriculture lead does get an audit + email drafted, the email includes one extra line tailored to the sector:

- **Irrigation Equipment Supplier** niche → mentions **PM-KUSUM** (the real government solar irrigation subsidy scheme) — because buyers in that category are actively searching for suppliers because of it.
- **Tractor Dealership / Farm Equipment** niches → mentions **SMAM** (the real government farm-machinery subsidy scheme), same reasoning.
- Every other agriculture niche → a general line about how agriculture buyers/co-ops increasingly search online first.

Important: the email never claims *your specific lead* is registered under either scheme — that's not something we can verify — it just states a true, general fact about buyer behavior in that category. Kept deliberately honest so nothing in the email could be called out as false.

## 6. What I looked into but did NOT build

Being upfront about this rather than quietly skipping it:

- **MCA company registry, browsed by industry code** — could have found registered agriculture companies directly. Didn't build it because I have no way to test it without a live `DATA_GOV_IN_API_KEY` (you'd need to get one from data.gov.in first).
- **Udyam/MSME small-business registry, browsed the same way** — would be even better than MCA since it covers small proprietorships (most small dealers), but the government's page blocked my automated check, so I couldn't even confirm it has the data I'd need.
- **Real IndiaMART page-scraping** (vs. the search-engine-based approach actually used) — technically possible but riskier (more likely to get blocked), only worth it if the current approach's results turn out too thin.
- **FPO (Farmer Producer Organization) directories** — real, but only ~40 organizations listed publicly, too small to be worth building a dedicated source for.

If you get a data.gov.in API key at some point, tell me and I can revisit the first two.

## Where to find this in the code (for reference)

- `frontend/src/agriNiches.js` — the 18 niche list
- `scrapers/krishi_maharashtra.py` — the government dealer-list source
- `scrapers/indiamart_dork.py` — the 3 B2B directory sources
- `emailer/base_sender.py` — the PM-KUSUM/SMAM email logic
- `CLAUDE.md` §5.12 — full technical writeup
