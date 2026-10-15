# CSV Import Outreach Page: Design

**Date:** 2026-10-15
**Status:** Draft for review
**Owner:** MMGA Agency (lead-audit-bot)

## 1. Goal

Let the user upload a CSV of leads, review the ones that are safe to mail, have an AI draft one short email per lead about MMGA's automation services, read and approve each draft, and send them through the existing send path, a day's worth at a time, without breaking the SES warm-up cap or sender reputation.

## 2. Background: what the real file contains

The trigger for this work was a 1,000 row export (`revauto-*.csv`, 227,636 bytes). Measured, not assumed:

| Fact | Value |
|---|---|
| Rows | 1,000, every one with an email |
| Columns (13) | `person_name, person_title, person_seniority, person_email, person_phone, person_linkedin_url, organization_name, domain, employees, industries, city, state, country` |
| Audience | US compliance officers (CCO, compliance officer, general counsel) |
| Industries | financial services, investment management, banking, hospitals, insurance, higher education, government administration |
| Country | `united states` on all 1,000 rows |
| Email hygiene | all syntactically valid, 0 duplicates, 0 free-mail, 0 role addresses (`info@`) |
| Companies | 922 distinct company domains; only 2 email domains lack a mail server |
| Not in the file | any email-status column, so verified vs guessed addresses cannot be told apart |
| `.gov` / `.mil` rows | 44 |
| After rules in section 7 | **890 ready** (44 government skipped, 66 second contacts skipped) |

At the current `DAILY_EMAIL_LIMIT` of 50, 890 leads take about 18 sending days. The queue must therefore survive restarts and resume on the next day.

## 3. Decisions already made with the user

1. **Offer:** automation services. The existing website audit pitch is a poor fit for a compliance officer, who does not own the website budget.
2. **Writing method:** AI writes one email per lead from the CSV fields plus a read of the company homepage. The user reviews every draft before it can be sent. No full audit per lead and no generic mail merge template.
3. **Architecture:** a dedicated Import page backed by a server side queue (not rows pushed into the existing in-browser lead cards, and not an unattended auto sender).

## 4. Scope

**In scope:** CSV upload by text, server side validation and dedupe, a persistent queue, per lead AI drafting, review and edit, approve, paced sending through `/api/send`, a bounce based sending pause, the CAN-SPAM footer and gate, a new Import tab.

**Out of scope (explicitly):**

- Unattended or scheduled auto sending.
- A paid email verification service. Unknown deliverability is a stated risk (section 16).
- MX record checks. `dnspython` is not in `requirements.txt`, and only 2 of 923 domains failed the check on the real file, so it is not worth a new dependency.
- Phone and LinkedIn outreach. Those columns are not stored.
- More than one contact per company, and multi touch sequences beyond the existing follow-up tooling.
- Regions other than the supported set in section 7.

## 5. Architecture overview

```
Browser                        Server (FastAPI)                       Existing
-------                        ----------------                       --------
Import tab
  upload CSV text  ---------> POST /api/import/upload
                               parse, validate, dedupe, store -----> email_history,
                               import_batches / import_leads         email_suppressions
  review table     <--------- GET  /api/import/batches/{id}/leads
  draft one lead   ---------> POST /api/import/leads/{id}/draft
                               fetch homepage (SSRF guarded)
                               AIAuditor.generate_import_email ----> provider fallback,
                               checkers, store draft                  db.log_cost
  edit / approve   ---------> PUT / POST .../leads/{id}...
  send approved    ---------> POST /api/send  (+ import_lead_id) ----> daily cap, SES,
                               gates, send, mark sent, log_email      tracking, History
```

New code is isolated in new modules where possible. The only edits to existing behaviour are an optional field on `/api/send`, a footer hook in `BaseSender`, and new config values.

## 6. Data model (schema v14)

Added in `storage/db.py:init_db()` behind `if schema_version < 14:`, following the existing guarded pattern. The current version is 13.

```sql
CREATE TABLE IF NOT EXISTS import_batches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    filename      TEXT,
    total_rows    INTEGER NOT NULL,
    ready_count   INTEGER NOT NULL,
    skipped_count INTEGER NOT NULL
);

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
    status           TEXT NOT NULL,   -- ready | skipped | drafted | approved | sent | failed
    skip_reason      TEXT,
    subject          TEXT,
    body             TEXT,
    review_warnings  TEXT,            -- JSON array
    drafted_at       DATETIME,
    sent_at          DATETIME,
    history_id       INTEGER,         -- email_history.id once sent
    error            TEXT,
    FOREIGN KEY (batch_id) REFERENCES import_batches(id)
);

CREATE INDEX IF NOT EXISTS idx_import_leads_batch_status ON import_leads(batch_id, status);
CREATE INDEX IF NOT EXISTS idx_import_leads_email ON import_leads(email);
```

Data minimisation: `person_phone`, `person_linkedin_url` and `employees` are discarded at import because nothing in this feature uses them.

**Status flow:** `ready -> drafted -> approved -> sent`. `failed` is a draft or send that errored and may be retried. `skipped` rows carry a `skip_reason`. A user can move `drafted` back to `ready` (discard draft) and `approved` back to `drafted` (un-approve).

## 7. Import and validation rules

The server parses the CSV text with the standard library `csv` module (handles quoted commas such as `"Chief Operating Officer, Chief Compliance Officer"` and a UTF-8 BOM). Column headers are matched case insensitively through an alias table (`person_email`, `email`, `Email`, and similar). A file with no recognisable email column is rejected with a clear message.

**Limits:** request text at most 5 MB and at most 20,000 rows, else HTTP 413.

Rules run in this order. The first failing rule sets `skip_reason`:

| # | Rule | `skip_reason` | Restorable? |
|---|---|---|---|
| 1 | Email fails syntax (`local@domain.tld`, at most 254 chars, no spaces) | `invalid_email` | No |
| 2 | Country not in `IMPORT_SUPPORTED_COUNTRIES` (`united states`, `india`) | `unsupported_country` | No |
| 3 | Same email earlier in this file | `duplicate_in_file` | No |
| 4 | Email already in `import_leads` from a previous batch | `already_imported` | No |
| 5 | `db.is_suppressed(email)` | `suppressed` | No |
| 6 | Email already in `email_history` | `already_emailed` | No |
| 7 | Company domain already emailed (normalised through the same key `get_sent_websites_summary` uses) | `company_already_emailed` | No |
| 8 | Domain ends in `.gov` or `.mil` | `gov_domain` | Yes |
| 9 | Not the chosen contact for its company (see below) | `second_contact` | Yes |

**Company domain:** the `domain` column, falling back to the email's own domain when blank (3 rows in the real file).

**Choosing one contact per company:** among a company's remaining rows pick the highest score, ties broken by file order. Score is seniority rank times 10 plus a title bonus.

- Seniority rank: `c_suite`/`owner`/`founder` 6, `vp` 5, `director`/`partner` 4, `senior` 3, `manager` 2, `entry` 1, blank 0.
- Title bonus: +2 if the title contains "chief compliance officer", +1 if it contains "compliance".

One contact per company avoids emailing several people at one bank on the same day, which reads as spray and increases complaints. A "restore" action lets the user override a `second_contact` or `gov_domain` skip. Restoring does not bypass rules 1 to 7: rules 5 to 7 depend on state that changes over time (a new unsubscribe, an email sent since import), so they are re-checked at restore time and again at send time, and a lead that now fails one becomes `skipped` with that reason instead of being sent.

## 8. API

Every route carries `Depends(require_api_key)` and a `rate_limit(...)`, per project rule.

| Route | Purpose | Rate limit |
|---|---|---|
| `POST /api/import/upload` body `{filename, csv_text}` | Parse, validate, store. Returns `{batch_id, total, ready, skipped, skip_breakdown, us_ready, mailing_address_set}` | 5 / 60s |
| `GET /api/import/batches` | List batches with status counts | 60 / 60s |
| `GET /api/import/batches/{id}/leads` | Paged list (`limit` at most 200) with filters `status, industry, seniority, state, q` and facet counts | 120 / 60s |
| `POST /api/import/leads/{id}/restore` | Restorable skip to `ready` | 120 / 60s |
| `POST /api/import/leads/{id}/skip` | User skip, reason `manual` | 120 / 60s |
| `POST /api/import/leads/{id}/draft` | Draft one lead (section 9) | 12 / 60s |
| `PUT /api/import/leads/{id}` body `{subject, body}` | Save a user edit; re-runs the cheap local checks | 120 / 60s |
| `POST /api/import/leads/{id}/approve` and `/unapprove` | Move between `drafted` and `approved` | 120 / 60s |
| `GET /api/import/status` | Sending state: `sent_today, daily_limit, remaining`, bounce stats and pause flag, `mailing_address_set` | 120 / 60s |

**Drafting is one lead per request, never a batch.** Railway's edge cuts HTTP requests at roughly 60 to 65 seconds (see CLAUDE.md, async audit mode), and a drafting call involves a network fetch plus an LLM call. The frontend loops single lead requests with a visible progress bar, the same pattern `handleGenerateAllFollowups` uses. Each request has a 45 second overall timeout; on timeout the lead stays `ready` with `error` set and can be retried.

## 9. Drafting

New method `AIAuditor.generate_import_email(...)`, following the existing precedent `generate_followup_from_original` (own response schema, own parser, same provider fallback order) so that the audit prompt, schema and behaviour are not touched.

**Inputs:** person name, title, organization, industry, city/state, and homepage text.

**Homepage fetch:**

- URL is `https://<domain>`. It must pass `security_utils.validate_public_url` (the CSV is untrusted input).
- Redirects are followed manually, at most 3, and every hop is re-validated.
- Timeout 8 seconds, body capped at 1 MB, text extracted with `trafilatura` (already a dependency), truncated to 4,000 characters.
- Fetch failure is not fatal: drafting continues from the CSV fields only and the draft gets the warning "no homepage evidence".

**Output schema:** `{subject, paragraphs[], source_quote}`. `source_quote` must be an exact substring of the homepage text, or empty.

**Prompt rules (mirroring the project's existing copy rules and the user's saved cold email guidelines):**

- Lead with a specific, role relevant problem. Do not sell MMGA in the first line. No pleasantries.
- Use only facts present in the inputs. Never state a number, client count or revenue figure about the recipient's company. Any illustrative number must be a round, openly hypothetical worked example.
- If `source_quote` is empty, the email must make no factual statement about the company beyond its name and the recipient's title, and the problem is framed as a question rather than a claim.
- No hyphens or dashes anywhere. No generic AI phrases ("I noticed", "I hope this finds you well", "In today's competitive landscape", and the rest of the existing banned list).
- Subject at most 50 characters, no all caps, no spam trigger words.
- Body at most 60 words, 3 to 5 short sentences, one low pressure question as the only call to action.

**Fixed line, appended by code, not by the AI:**

> Worth mentioning how this reached you: this email was drafted and sent by a system we built ourselves. That is the kind of automation we build for other teams, so if repetitive work is eating into your day, it is the same conversation.

This replaces `BaseSender._AUTOMATION_PITCH_LINE` for imports, because that line says "the issues above were found", which would be **false** here (no audit runs). The replacement is true and must never claim that no human reviewed the email; drafts are reviewed before sending. It must stay at or under 45 words and contain no dashes.

**Checks:** the result is adapted into the dict shape the existing static checkers expect (`email_subject`, `opening_line`, `flaws: [{paragraph, source_quote}]`) and run through `_check_spam_trigger_words`, `_check_ai_sounding_phrases`, `_check_forbidden_dashes`, `_check_number_hallucination` (against the input facts) and `_verify_source_quotes`. Their output becomes `review_warnings`, which must be acknowledged before sending, exactly as on audit drafts. The checkers are reused unchanged.

**Cost:** each call's `ai_cost` is logged with `db.log_cost`, using the category naming the Costs tab already groups by (the plan verifies the exact label).

## 10. Sending

The frontend sends each approved lead through the **existing** `POST /api/send`, so the daily cap, suppression list, SES transport, open tracking, History tab and follow-up tooling all apply unchanged.

`SendRequest` gains one optional field, `import_lead_id: int | None = None`. When it is set, `/api/send` behaves as follows:

1. Loads the import lead. 404 if missing, 409 if its status is not `approved`, 409 if `req.email` does not match the lead's email.
2. Uses the **stored** subject and body, not the client's copy, so what was reviewed and checked is exactly what is sent.
3. If the lead has `review_warnings` and `acknowledge_warnings` is false, returns the same 409 shape as audit drafts.
4. Applies the compliance and bounce gates (section 11).
5. Skips the main pipeline's draft lookup, screenshot attachment, Sheets update and draft deletion. Those are keyed by website and would otherwise cross contaminate: an old audit draft or screenshot for the same company could be attached to, or deleted by, an import send.
6. Records `variant = "import-automation"` so `get_variant_performance()` reports these sends as their own bucket.
7. On success marks the lead `sent`, sets `sent_at`, and links `history_id` (looked up through `get_email_history_by_message_id`).

Existing callers do not send `import_lead_id`, so their behaviour is unchanged.

**Pacing:** `/api/send` is limited to 10 per 60 seconds. The frontend waits 6.5 to 9 seconds (jittered) between sends so it stays under that limit instead of hitting it and backing off. A day's 50 sends therefore take about 6 minutes. On HTTP 429 with the daily limit message the loop stops and reports how many remain; the cap is never bypassed or retried automatically.

## 11. Compliance and safety gates

**CAN-SPAM (US recipients).** Every commercial email to a US address needs a physical postal address and a clear opt-out. Today the sender has one click unsubscribe headers, an `/unsubscribe` route and a suppression list, but **no postal address in any email body**.

- New config `MAILING_ADDRESS`. When set, `BaseSender` appends a footer to the plain and HTML bodies of initial emails and follow-ups: the address, plus an opt-out line linking to the existing `{APP_BASE_URL}/unsubscribe?email=...` route. The footer contains no hyphens.
- When `MAILING_ADDRESS` is unset, emails are byte for byte what they are today.
- An import lead whose `country` is `united states` cannot be sent while `MAILING_ADDRESS` is unset. `/api/send` returns 409 with a message saying what to set. The Import page shows this as a blocking banner from the moment of upload.

**Bounce pause.** Before each send, `/api/send` (for import leads only) computes the bounce rate across this feature's sent emails (`email_history` rows with `variant = "import-automation"`), using the `bounced` flag `get_email_history()` already derives. If at least `IMPORT_BOUNCE_MIN_SENT` (default 20) have been sent and the rate exceeds `IMPORT_BOUNCE_PAUSE_PCT` (default 5), it returns 409 `import_paused` and the page shows the figures. There is no override button in v1; the thresholds are env configurable. Bounces are only known once the reply checker has seen them, so this is a best effort brake that lags real bounces. The first 20 sends are therefore the exposure window, which is why the daily cap matters.

**Existing guards retained:** `DAILY_EMAIL_LIMIT` is enforced exactly as today and never silently bypassed or auto retried. Suppressed addresses are refused by the sender itself.

**Data handling:** the CSV is parsed from request text and never written to disk or committed. `*.csv` is added to `.gitignore` (no CSV is currently tracked, confirmed) so a lead list cannot be committed by accident. Imported PII lives only in the SQLite database on the Railway volume.

## 12. Frontend

New view `import` in `App.jsx` (nav button "Import", `FileSpreadsheet` icon), rendered by `renderImport()`, following the existing `currentView` switch. Server is the source of truth; lead rows are **not** kept in `localStorage` (size and PII). Only the active `batch_id` is remembered there.

Sections, top to bottom:

1. **Upload.** File picker. The file is read in the browser and sent as `csv_text`.
2. **Summary.** Cards for total, ready, and each skip reason with counts, plus a blocking banner if US leads are ready and `MAILING_ADDRESS` is unset.
3. **Sending status.** "Sent today 12 of 50, 38 left", bounce rate with pause state.
4. **Table.** Paged, filterable by status, industry, seniority and state, with search. Row actions: draft, restore, skip.
5. **Draft selected.** Loops single lead draft requests with a progress bar and a stop button.
6. **Review.** A drawer showing subject, body, review warnings (red, must be acknowledged) and the homepage evidence quote. Editable. Approve or un-approve.
7. **Send approved.** "Send up to 38 approved" with jittered pacing and a stop button. Final summary lists sent, failed and what stopped the run.

## 13. Configuration

| Name | Default | Purpose |
|---|---|---|
| `MAILING_ADDRESS` | unset | Footer postal address; required to send to US leads |
| `IMPORT_BOUNCE_PAUSE_PCT` | `5` | Bounce rate that pauses import sending |
| `IMPORT_BOUNCE_MIN_SENT` | `20` | Minimum sends before the pause can trigger |

`IMPORT_SUPPORTED_COUNTRIES` is a code constant (`united states`, `india`), not an env var. Documented in CLAUDE.md section 4 and the changelog.

## 14. Error handling

- Malformed CSV, no email column, or over the limits: 4xx with a specific message; nothing is stored.
- Homepage fetch failure or an unsafe URL: draft proceeds without evidence and is flagged.
- Every AI provider failing: the lead stays `ready` with `error` set; the loop continues with the next lead.
- Send failure: lead becomes `failed` with the transport's real error; it can be retried.
- Suppressed at send time: the lead is marked `skipped` with `suppressed`.

## 15. Testing

All offline: network, DNS, the AI providers and SES are monkeypatched, and a temp database fixture is used (the existing `temp_db` pattern).

- **Parser and rules:** BOM, quoted commas, alias headers, each skip reason in order, restorable vs not, company dedupe choosing the intended contact, the 3 blank domain rows, size limits, an end to end run asserting the real file's shape (890 ready from 1,000) using a synthetic fixture with the same structure.
- **Endpoints:** auth and rate limit present on every route, paging and filters, status transitions, illegal transitions rejected.
- **Drafting:** SSRF rejection of private and redirected URLs, fetch failure fallback, schema parse, checkers wired and `review_warnings` populated, fixed line appended and free of dashes and "no human" claims, a draft with no evidence is framed as a question.
- **Sending:** stored copy used instead of client copy, email mismatch rejected, status gate, warnings acknowledgement, no main pipeline draft or attachment cross contamination, `import-automation` variant recorded, `history_id` linked, daily cap still enforced.
- **Compliance:** US lead blocked without `MAILING_ADDRESS`, footer present when set and absent (byte identical bodies) when unset, footer free of dashes.
- **Bounce pause:** below minimum sample no pause, above threshold pauses, boundary values.
- **Regression:** the existing suite stays green; `/api/send` behaviour without `import_lead_id` is unchanged.

## 16. Risks and known limits

1. **Unverified list.** No email status column exists, and mailbox existence is not checked. Expect some bounces. Mitigated by small daily batches and the bounce pause, not eliminated.
2. **Hostile mail filters.** Banks, hospitals and government run aggressive gateways, and the sending domain is still warming up. Spam trap and complaint risk is real and affects every send from the domain, not only these.
3. **Bounce visibility lags.** The pause depends on the reply checker having processed bounce notices.
4. **Audience fit is unproven.** Whether compliance officers respond to an automation pitch is unknown. The `import-automation` variant exists so the dashboard can answer that with data after the first sends.
5. **SSRF hardening is TOCTOU.** `validate_public_url` resolves DNS once; a hostile DNS rebinding record is the same accepted limit as the existing audit endpoint.
6. **Legal.** The footer and gate cover CAN-SPAM's address and opt-out requirements; this is not legal advice and does not cover other regimes (hence the supported country set).

## 17. Acceptance criteria

1. Uploading a file shaped like the real CSV yields 890 `ready`, 44 `gov_domain`, 66 `second_contact` and no stored phone or LinkedIn data.
2. A US lead cannot be sent until `MAILING_ADDRESS` is set; once set, every email carries the address and opt-out line.
3. Sending 50 leads in a day never exceeds `DAILY_EMAIL_LIMIT`, and the next day resumes where it stopped after a server restart.
4. What is sent is exactly the stored, reviewed copy; a mismatched email or an unapproved lead is refused.
5. An import send never attaches or deletes an unrelated audit draft or screenshot.
6. Crossing the bounce threshold blocks further import sends with the figures shown.
7. The full existing suite passes, and the frontend builds clean.
