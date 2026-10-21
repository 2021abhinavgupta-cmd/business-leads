# Designed Flaw Emails Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Import leads get a designed HTML email (option B, warm light) with 2 to 5 boxed screenshots of real, measured flaws on their own website and an automation note at the end; leads without enough real flaws keep the current text email.

**Architecture:** A background audit stage reuses the existing Playwright capture (`analyzer/visuals.py`) to save one boxed screenshot per measured flaw. A new copy generator and renderer turn those flaws into a headline, opening, one sentence per flaw and an HTML plus text email, checked by the existing fact checker. A new sender method builds a multipart message with inline images and no tracking pixel, and `/api/send` uses it for designed leads.

**Tech Stack:** Python 3, FastAPI, SQLite (`PRAGMA user_version`), Playwright, Pillow, Anthropic/Gemini/OpenAI clients already in `analyzer/ai_audit.py`, React/Vite (`frontend/src/ImportView.jsx`), pytest.

**Spec:** `docs/superpowers/specs/2026-10-21-designed-flaw-email-design.md`. Mockup: `.superpowers/brainstorm/1167-1792565534/content/email-design.html` (option B).

## Global Constraints

- Look is option B: cream `#fff8ef` background, text `#2a1f14`, accent `#d9480f`, panel `#f1e3cf`; light background set explicitly; no pure white or pure black.
- HTML under 80 KB; images JPEG, 600 px wide, quality about 70, each under about 90 KB; images inline by Content-ID, never hosted; every `<img>` has alt text and fixed width and height.
- At most 5 images per email, one per section: the full boxed screenshot (it shows the real page around the box). The tight close up crop is used only for the vision check and is never attached.
- A plain text version is always included and carries the same sections.
- No open tracking pixel on designed emails. Keep List-Unsubscribe headers, the suppression check, the postal address and opt-out footer, the daily cap and every existing import gate.
- One call to action: a button "Want the full review? Reply yes" that is a `mailto:` to the reply address. No calendar link, no second ask.
- Sections equal real measured flaws that have a boxed screenshot, at most 5. Fewer than 2 means the lead falls back to the existing text email and audit status `no_flaws`.
- Authored copy contains no hyphens or dashes (existing project rule), is not invented, and each flaw sentence only restates its measured description in plain business terms.
- **Screenshot accuracy (user requirement, 2026-10-21):** a shot is used only if (1) the red box was drawn by the browser on the element the measurement flagged (existing guarantee, keep it), (2) a vision check confirms the cropped box really shows the described defect, (3) it is not a cookie banner or chat widget, (4) the box is still visible after the 600 px resize, (5) the audit is fresh at send time (older than `config.DRAFT_STALE_DAYS` blocks the send and asks for a re-audit), and (6) the preview shows the exact compressed images that will be sent. A shot that fails any check is dropped, never sent; fewer than 2 survivors means the text email.
- Do not change the Dashboard or agriculture audit behavior. Do not fork capture logic.
- Route order: new `/api/import/*` routes go above the `/` static mount in `app.py`.

## Review Focus

1. A site that blocks bots, is down or shows only a login page: audit ends `failed` or `no_flaws`, the lead stays draftable as text, nothing crashes (Task 3, Task 7).
2. The same flaw on several pages or the same element twice: one section only (Task 2).
3. Server restart or Cancel in the middle of an audit leaves leads stuck at `running` forever: they reset and can be re-queued (Task 3, Task 4).
4. Company or person names containing `<`, `&`, quotes or non ASCII break or inject into the HTML (Task 6).
5. A screenshot file deleted before send would produce a broken email: the send is blocked before the lead is claimed (Task 9).
6. A box that is on the wrong thing, on a cookie banner or chat widget, invisible after downscaling, or from an audit weeks old: the shot is dropped or the send blocked, never sent (Task 3, Task 6, Task 9).

---

### Task 1: Schema v15 and store fields

**Files:**
- Modify: `storage/db.py` (add `if schema_version < 15` block after the v14 block), `storage/import_store.py`
- Test: `test_import_store.py`

**Interfaces:**
- Produces: `import_leads` columns `audit_status TEXT DEFAULT 'none'` (existing rows are backfilled to `'none'`), `audit_error TEXT`, `audited_at DATETIME`, `flaw_shots TEXT` (JSON list), `html_body TEXT`, `design_json TEXT` (JSON object). `import_store.update_lead` accepts those six names. `import_store.get_lead` and `list_leads` return `flaw_shots` as a list and `design_json` as a dict (empty list or dict when null). `import_store.list_leads(..., audit_status=None)` filter, and `facets` gains an `audit_status` key.

- [ ] **Step 1: Failing tests** in `test_import_store.py`: `test_v15_adds_audit_and_design_columns` (fresh db has the six columns and `user_version == 15`), `test_v14_database_migrates_to_v15_keeping_rows`, `test_existing_rows_get_audit_status_none_and_new_rows_default_to_none`, `test_update_lead_round_trips_flaw_shots_and_design_json_as_decoded_values`, `test_list_leads_filters_by_audit_status`.
- [ ] **Step 2:** Run `pytest test_import_store.py -q`. Expected: the four new tests FAIL.
- [ ] **Step 3:** Add the guarded `ALTER TABLE import_leads ADD COLUMN` statements for the six columns and `PRAGMA user_version = 15`. In `import_store`, extend `_UPDATABLE`, JSON-encode `flaw_shots` and `design_json` in `update_lead` when not already strings, decode both in `_decode`, add the `audit_status` filter and facet.
- [ ] **Step 4:** Run `pytest test_import_store.py -q`. Expected: all PASS.
- [ ] **Step 5: Commit** `git add storage test_import_store.py && git commit -m "feat(import): schema v15 audit and design fields"`

### Task 2: Collect one boxed screenshot per flaw

**Files:**
- Modify: `analyzer/visuals.py` (new helper near the filename helpers; call it where the three images are saved, around the `extra_audit_data` assembly; add `"flaw_shots"` to that dict)
- Test: `test_flaw_shots.py` (new)

**Interfaces:**
- Produces: `make_flaw_shot_filename(company_name: str, url: str, index: int, kind: str) -> str`; `collect_flaw_shots(pages: list[dict], mobile_flaw: dict | None, mobile_bytes: bytes | None, company_name: str, url: str, *, max_shots: int = 5, out_dir: str = SCREENSHOTS_DIR) -> list[dict]`. Each item: `{"path": str, "closeup_path": str | None, "page": str, "description": str, "impact": str, "selector": str, "kind": "desktop" | "mobile"}` (`selector` comes from `visual_flaw["selector"]`). `collect_flaw_shots` also takes `mobile_closeup_bytes: bytes | None = None`; in the mobile block of `_generate_audit_screenshot_once` capture it with the existing `_capture_closeup(mobile_page, mobile_visual_flaw)` while the box is still drawn (before `_remove_highlight`), so mobile shots can be verified too. `extra_audit_data["flaw_shots"]` holds that list.

- [ ] **Step 1: Failing tests** with fake page dicts (tiny real PNG bytes) and `tmp_path`: `test_one_shot_per_page_that_has_a_visual_flaw`, `test_pages_without_a_box_are_skipped`, `test_same_description_on_two_pages_gives_one_shot` (keeps the higher impact, review focus 2), `test_mobile_shot_included_only_when_a_mobile_box_was_drawn`, `test_mobile_shot_carries_its_closeup_when_one_was_captured`, `test_every_shot_carries_the_selector`, `test_shots_sorted_most_severe_first_and_capped_at_max_shots`, `test_files_are_jpeg_and_exist`.
- [ ] **Step 2:** Run `pytest test_flaw_shots.py -q`. Expected: FAIL, helper missing.
- [ ] **Step 3:** Implement `collect_flaw_shots` using the existing `_SEVERITY_RANK` and the page dict keys `screenshot_bytes`, `closeup_bytes`, `visual_flaw`, `label`. Convert with Pillow like the existing saves. Wire it into `_generate_audit_screenshot_once` without changing the three existing return values.
- [ ] **Step 4:** Run `pytest test_flaw_shots.py test_screenshot_accuracy.py test_red_box_viewport.py -q`. Expected: PASS (`test_screenshot_accuracy.py` asserts nothing calls `page.screenshot()` directly; do not add such a call).
- [ ] **Step 5: Commit** `git add analyzer/visuals.py test_flaw_shots.py && git commit -m "feat(audit): save one boxed screenshot per measured flaw"`

### Task 3: Audit runner and queue

**Files:**
- Create: `importer/audit_runner.py`
- Test: `test_import_audit_runner.py` (new; use `import_test_support`)

**Interfaces:**
- Consumes: `generate_audit_screenshot(url, company_name, on_queued=None, on_started=None) -> (path, html, extra)`; `import_store.update_lead/get_lead`.
- Produces: `MIN_SHOTS = 2`; `async def audit_one(lead: dict, capture=generate_audit_screenshot, judge=visuals._vision_confirms_visible_defect) -> dict` returning `{"audit_status": "done" | "no_flaws" | "failed", "flaw_shots": list, "dropped_shots": list[{"description", "reason"}], "audit_error": str | None}`; `verify_shots(shots: list[dict], judge) -> tuple[list[dict], list[dict]]` returning `(kept, dropped)`, where a shot is kept only if `judge(closeup_bytes, description)` is True (the same narrow yes or no question the existing vision gate asks, never free form critique), the judge is called with the closeup image bytes when the shot has a closeup file, otherwise with the main shot image bytes, and is run with `asyncio.to_thread` because it makes a blocking network call; a shot with no image file at all, or whose `selector` matches `(?i)cookie|consent|gdpr|onetrust|cookiebot|chat|intercom|drift|tawk|crisp|livechat|zendesk|hubspot-messages` (a consent banner or chat widget), is dropped; the runner also writes `audited_at` when a lead finishes; `start_audit(lead_ids: list[int], *, capture=generate_audit_screenshot) -> bool` (False if a run is already active); `progress() -> {"running": bool, "total": int, "done": int, "no_flaws": int, "failed": int, "current": str | None}`; `cancel() -> None`; `reset_stale_running() -> int` (sets leads left at `running` back to `none`, returns count).

- [ ] **Step 1: Failing tests** with a fake async `capture`: `test_done_when_at_least_two_flaw_shots`, `test_no_flaws_when_fewer_than_two_shots`, `test_failed_when_capture_returns_no_html` (error text stored; review focus 1), `test_unsafe_or_empty_domain_is_failed_not_fetched` (uses `security_utils.validate_public_url`), `test_queue_processes_leads_in_order_and_updates_store`, `test_start_while_running_returns_false`, `test_cancel_stops_before_the_next_lead_and_resets_unstarted_leads`, `test_reset_stale_running_resets_leads` (review focus 3), `test_only_ready_or_drafted_leads_are_audited`, `test_shot_dropped_when_the_vision_judge_says_the_box_does_not_show_the_defect` (review focus 6), `test_shot_without_a_closeup_is_judged_on_its_main_image`, `test_shot_with_no_image_file_is_dropped`, `test_shot_dropped_when_the_flagged_element_is_a_cookie_banner_or_chat_widget`, `test_dropping_shots_below_two_makes_the_lead_no_flaws_with_reasons_recorded`, `test_judge_error_drops_the_shot_rather_than_trusting_it`.
- [ ] **Step 2:** Run `pytest test_import_audit_runner.py -q`. Expected: FAIL, module missing.
- [ ] **Step 3:** Implement. One module level asyncio task; leads processed strictly one at a time (the Playwright slot allows one anyway); status written to the store before and after each lead; `cancel` sets a flag checked between leads; an exception from `capture` becomes `failed` and the loop continues.
- [ ] **Step 4:** Run `pytest test_import_audit_runner.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `git add importer/audit_runner.py test_import_audit_runner.py && git commit -m "feat(import): background audit runner"`

### Task 4: Audit endpoints

**Files:**
- Modify: `app.py` (three routes above the `/` static mount; call `audit_runner.reset_stale_running()` in the existing startup hook)
- Test: `test_import_endpoints.py`

**Interfaces:**
- Produces: `POST /api/import/batches/{batch_id}/audit` body `{"lead_ids": [int] | null, "limit": int | null}` (when `lead_ids` is null, take the first `limit` leads of the batch with status `ready` and audit_status `none`, in id order; `limit` capped at 200) returning `{"started": bool, "queued": int}`; `GET /api/import/audit/progress` returning the `progress()` dict; `POST /api/import/audit/cancel`. All use `require_api_key`.

- [ ] **Step 1: Failing tests** (monkeypatch `audit_runner.start_audit`): `test_audit_start_selects_next_ready_leads_without_a_status`, `test_audit_start_rejects_limit_over_200`, `test_audit_start_returns_started_false_when_busy`, `test_audit_endpoints_require_the_api_key`, `test_audit_progress_and_cancel_shapes`.
- [ ] **Step 2:** Run `pytest test_import_endpoints.py -q -k audit`. Expected: FAIL.
- [ ] **Step 3:** Implement the routes as thin wrappers over `audit_runner`.
- [ ] **Step 4:** Run `pytest test_import_endpoints.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `git add app.py test_import_endpoints.py && git commit -m "feat(import): audit start, progress and cancel endpoints"`

### Task 5: Flaw email copy generation

**Files:**
- Modify: `analyzer/ai_audit.py` (new schema near `_IMPORT_EMAIL_SCHEMA`, new methods after `generate_import_email`)
- Test: `test_import_flaw_copy.py` (new)

**Interfaces:**
- Produces: `_FLAW_EMAIL_SCHEMA` with `subject`, `headline`, `opening` (strings) and `sections` (list of `{"index": int, "title": str, "sentence": str}`); `AIAuditor._build_flaw_email_prompt(lead, homepage_text, flaw_shots, your_name) -> str`; `AIAuditor.generate_flaw_email_copy(lead, homepage_text, flaw_shots, your_name, feedback: list[str] | None = None) -> dict | None` returning the parsed copy plus `"ai_cost"`, same provider order and timeouts as `generate_import_email`; `AIAuditor._parse_flaw_copy(raw: str, expected: int) -> dict | None` (None unless there is exactly one section per shot, indexes `0..n-1`).

- [ ] **Step 1: Failing tests** (auditor built with `AIAuditor.__new__`, like `test_import_drafting.py`): `test_prompt_has_no_dashes_and_lists_each_measured_flaw_description`, `test_prompt_forbids_numbers_invented_claims_and_other_flaws`, `test_prompt_requires_hedged_cost_wording`, `test_parse_rejects_wrong_section_count`, `test_parse_rejects_duplicate_or_missing_indexes`, `test_parse_strips_markdown_fences`, `test_generate_uses_provider_fallback_order` (monkeypatch the three call functions).
- [ ] **Step 2:** Run `pytest test_import_flaw_copy.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement. Prompt rules: headline names the company and the count of flaws; subject 2 to 6 words, at most 50 characters, title or sentence case, avoids the spam words already banned; opening is one or two sentences using one fact from the homepage text if available; each section sentence restates only the given measured description in plain terms and says what it can cost the visitor or the business using words like "can" or "may"; no numbers other than the flaw count; no dashes anywhere (write the prompt itself without dashes); untrusted data warning as in `_build_import_prompt`.
- [ ] **Step 4:** Run `pytest test_import_flaw_copy.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `git add analyzer/ai_audit.py test_import_flaw_copy.py && git commit -m "feat(import): flaw email copy generation"`

### Task 6: Image preparation and the option B renderer

**Files:**
- Create: `importer/email_images.py`, `importer/email_html.py`
- Test: `test_import_email_html.py` (new)

**Interfaces:**
- Produces: `email_images.prepare(path: str, *, width: int = 600, max_bytes: int = 90_000) -> bytes` (JPEG, lowers quality from 70 down to a floor of 40 until under `max_bytes`, raises `FileNotFoundError` if the file is missing); `email_images.prepare_all(shots: list[dict]) -> list[tuple[str, bytes]]` returning `(cid, jpeg_bytes)` for each shot's main image in section order using cids `flaw1`, `flaw2` and so on (never the closeup); `email_html.render(copy: dict, shots: list[dict], *, first_name: str, company: str, reply_to: str) -> tuple[str, str]` returning `(html, text)`. The HTML carries the literal marker `<!--FOOTER-->` where the sender inserts the footer. Image `src` values are `cid:` references.

- [ ] **Step 1: Failing tests**: `test_prepare_returns_jpeg_at_most_600_wide_and_under_90kb`, `test_prepare_missing_file_raises`, `test_red_box_is_still_visible_after_resize_and_compression` (build a test image with a red outline, prepare it, assert red pixels remain along the outline; review focus 6), `test_prepare_never_crops_the_box_out_of_the_frame`, `test_html_under_80kb_with_five_sections`, `test_every_img_has_alt_width_and_height`, `test_html_escapes_markup_in_company_and_names` (review focus 4: `<script>`, `&`, quotes, non ASCII), `test_no_tracking_pixel_and_no_remote_urls` (no `http` in `src`, no 1x1 image), `test_button_is_mailto_with_prefilled_subject`, `test_automation_note_is_last_block_before_footer_marker`, `test_text_version_contains_every_section_title_and_sentence`, `test_copy_has_no_hyphens_or_dashes_in_template_text`.
- [ ] **Step 2:** Run `pytest test_import_email_html.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement `render` table based (600 px wide wrapper, inline styles only, explicit `bgcolor` plus `background-color` for Outlook). Automation note text: that this email, its screenshots and the review were produced by automation MMGA built, and MMGA builds the same for compliance, finance and operations teams. Use `html.escape` on every interpolated value.
- [ ] **Step 4:** Run `pytest test_import_email_html.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `git add importer/email_images.py importer/email_html.py test_import_email_html.py && git commit -m "feat(import): option B email renderer and image prep"`

### Task 7: Designed drafting, preview and design edit

**Files:**
- Modify: `importer/drafting.py`, `app.py` (the existing import draft route near the `import_drafting.draft_lead` call, plus two new routes)
- Test: `test_import_drafting.py`, `test_import_endpoints.py`

**Interfaces:**
- Consumes: Tasks 1, 5, 6; `AIAuditor.verify_import_email(lead, homepage_text, subject, paragraphs) -> {"issues", "ai_cost"} | None`.
- Produces: `drafting.draft_designed(lead: dict, auditor, *, your_name: str, reply_to: str, fetch=homepage.fetch_homepage_text) -> dict` returning `{"subject", "body", "html_body", "design", "review_warnings", "source_quote", "ai_cost"}` where `design` is `{"opening", "headline", "sections": [{"index", "title", "sentence"}]}` and `body` is the text version. The existing `/api/import/batches/{id}/leads` response replaces `html_body` with `is_designed: bool` (the HTML can be tens of KB per lead) and the lead detail used by the drawer keeps `design_json` but not `html_body`; the preview route is the only way to read the HTML. The existing draft route calls `draft_designed` when the lead's `audit_status == "done"` and otherwise `draft_lead` unchanged. `GET /api/import/leads/{lead_id}/preview` returns `{"html": str}` with each `cid:` replaced by a `data:image/jpeg;base64` URI of the exact bytes `email_images.prepare_all` will send (so the user previews what the recipient gets) and the footer marker removed. `PUT /api/import/leads/{lead_id}/design` body `{"subject", "opening", "sections": [{"index", "sentence"}]}` re-renders, reruns the static checks and keeps the existing edit semantics for status (mirror the existing text edit route).

- [ ] **Step 1: Failing tests**: `test_designed_draft_runs_verify_loop_and_retries_up_to_three_times` (fake auditor like `_CheckedAuditor`), `test_unresolved_issues_become_fact_check_warnings`, `test_measured_flaws_count_as_evidence_for_the_checker` (the evidence string passed to `verify_import_email` contains each description), `test_draft_route_falls_back_to_text_when_audit_status_is_not_done` (review focus 1), `test_designed_draft_stores_html_body_and_design_json_and_status_drafted`, `test_preview_embeds_the_exact_prepared_image_bytes`, `test_design_edit_rerenders_and_rejects_html_in_sentences`, `test_design_edit_requires_a_designed_lead`.
- [ ] **Step 2:** Run the new tests. Expected: FAIL.
- [ ] **Step 3:** Implement by extracting the verify loop from `draft_lead` into a shared helper both functions use (behavior of `draft_lead` must not change; its existing tests stay green). Run `compose.run_checks` over the combined text body. **Deterministic output validation (ruling from the Task 5 review; Task 5's parser does not check content):** before rendering, `draft_designed` replaces every hyphen or dash in the copy strings (subject, headline, opening, titles, sentences) with a space, collapses whitespace, and treats any of these as a verify-loop issue that triggers a rewrite and is a warning if still present after the last attempt: a digit anywhere other than the flaw count, a URL or email address or phone number, the word MMGA, a subject over 50 characters or outside 2 to 6 words, a banned spam word (free, guarantee, urgent, cash, winner). Tests: `test_dashes_in_model_copy_are_replaced_with_spaces`, `test_digits_urls_mmga_and_long_subject_trigger_a_rewrite`.
- [ ] **Step 4:** Run `pytest test_import_drafting.py test_import_endpoints.py test_import_compose.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `git add importer/drafting.py app.py test_import_drafting.py test_import_endpoints.py && git commit -m "feat(import): designed drafts, preview and design edit"`

### Task 8: Sender for designed emails

**Files:**
- Modify: `emailer/base_sender.py`
- Test: `test_import_designed_send.py` (new)

**Interfaces:**
- Produces: `BaseSender._footer_lines(to_email: str) -> list[str]` (empty when `MAILING_ADDRESS` is unset; `_with_footer` output must be byte identical to today); `BaseSender._build_designed_message(to_email, subject, text_body, html_body, images: list[tuple[str, bytes]], message_id) -> MIMEMultipart` (mixed, containing related, containing alternative with text part first then HTML, then each image with `Content-ID` and inline disposition; unsubscribe headers and Reply-To as in `_build_initial_message`; footer applied to both parts; no tracking pixel); `BaseSender.send_designed_email(to_email, subject, text_body, html_body, images) -> str | False` with the same suppression check, retry and error behavior as `send_email`.

- [ ] **Step 1: Failing tests**: `test_with_footer_unchanged_after_refactor` (reuse the cases in `test_import_footer.py`), `test_designed_message_structure_text_before_html_and_images_inline`, `test_designed_message_has_unsubscribe_and_reply_to_headers`, `test_designed_message_has_no_tracking_pixel`, `test_footer_appears_in_text_and_replaces_the_marker_in_html` (and is HTML escaped), `test_send_designed_returns_false_for_suppressed_recipient`, `test_send_designed_retries_transient_errors_like_send_email`.
- [ ] **Step 2:** Run `pytest test_import_designed_send.py test_import_footer.py -q`. Expected: new tests FAIL.
- [ ] **Step 3:** Implement by refactoring `_with_footer` onto `_footer_lines`, then the new methods modeled on `_build_initial_message` and `send_email`.
- [ ] **Step 4:** Run `pytest test_import_designed_send.py test_import_footer.py test_email_deliverability.py test_gmail_sender.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `git add emailer/base_sender.py test_import_designed_send.py && git commit -m "feat(email): designed multipart send with inline images"`

### Task 9: `/api/send` for designed leads

**Files:**
- Modify: `app.py` (`_prepare_import_send` and the import branch of `send_email`), `importer/send_policy.py`
- Test: `test_import_send.py`

**Interfaces:**
- Consumes: Task 8, `email_images.prepare_all`.
- **Stale design guard (ruling from the Task 7 review):** a re-audit replaces `flaw_shots` but leaves `html_body` in place, so sentences could sit under the wrong screenshots. Task 7 saves `design_json.shot_paths` and exposes `drafting.shots_match_design(lead) -> bool`; `_prepare_import_send` must call it for any lead with `html_body` and block with 409 ("The screenshots changed after this email was drafted; draft it again") BEFORE claiming the lead. Test `test_send_blocked_when_shots_no_longer_match_the_design` (status stays `approved`, nothing sent).
- Produces: a lead with a stored `html_body` is sent with `ses.send_designed_email(send_to, subject, body, html_body, images)`; all other leads use `ses.send_email` unchanged. History logging, claim before send, failure handling and the response shape are unchanged. Designed leads log the variant `import-designed` instead of `import-automation`. **`importer/send_policy.py`: add `IMPORT_DESIGNED_VARIANT = "import-designed"` and `IMPORT_VARIANTS = (IMPORT_VARIANT, IMPORT_DESIGNED_VARIANT)`, and make `bounce_status` sum `sent` and `bounced` across every variant in `IMPORT_VARIANTS`** (today it reads only `import-automation`, so designed sends would bypass the bounce pause).

- [ ] **Step 1: Failing tests** (monkeypatch the sender): `test_designed_lead_uses_send_designed_email_with_prepared_images`, `test_text_lead_still_uses_send_email`, `test_missing_screenshot_blocks_before_claiming_the_lead` (409, status still `approved`, nothing sent; review focus 5), `test_stale_audit_blocks_the_send_with_a_re_audit_message` (audited_at older than `config.DRAFT_STALE_DAYS`, same 409 shape and no claim; review focus 6), `test_fresh_audit_is_allowed`, `test_designed_send_failure_marks_lead_failed_with_the_error`, `test_designed_send_logs_history_with_designed_variant_and_links_history_id`, `test_bounce_pause_counts_designed_and_text_sends_together` (in `test_import_send.py` or the send policy tests; review focus 6 safety), `test_all_existing_import_gates_still_apply_to_designed_leads` (US without `MAILING_ADDRESS`, bounce pause, daily cap, warnings acknowledgement).
- [ ] **Step 2:** Run `pytest test_import_send.py -q`. Expected: new tests FAIL.
- [ ] **Step 3:** Implement. Prepare images inside `_prepare_import_send` (before any status change) and pass them through.
- [ ] **Step 4:** Run `pytest test_import_send.py test_import_final_fixes.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `git add app.py test_import_send.py && git commit -m "feat(import): send designed emails"`

### Task 10: Import page UI

**Files:**
- Modify: `frontend/src/ImportView.jsx`

**Interfaces:**
- Consumes: Tasks 4 and 7 endpoints.
- Produces: a batch toolbar control "Audit next N sites" (N default 25, max 200) with Start and Cancel, polling `GET /api/import/audit/progress` every 3 seconds while running and stopping on unmount; an audit status badge per lead (`none`, `running`, `done`, `no flaws`, `failed` with the error on hover) and an audit status filter; in the review drawer, when the lead has `html_body`, a sandboxed `<iframe srcDoc>` preview fetched from the preview route, the editable fields from `PUT .../design` (subject, opening, one sentence per section) and the existing warnings, approve and send flow; text leads show the current drawer unchanged.

- [ ] **Step 1:** Implement the controls and drawer branch following the existing patterns (runningRef and unmount guards, axios with the API key header already used in the file). The iframe uses `sandbox=""` (no scripts).
- [ ] **Step 2:** Run `cd frontend && npm run build`. Expected: build succeeds with no errors.
- [ ] **Step 3:** Start the app locally and verify by hand with a 3 row CSV: audit 3 leads, open a designed draft, edit a sentence, approve. Expected: preview shows the option B layout and the edited sentence; a lead with no flaws shows the text drawer.
- [ ] **Step 4: Commit** `git add frontend/src/ImportView.jsx && git commit -m "feat(import): audit controls and designed email preview"`

### Task 11: Docs and the real trial gate

**Files:**
- Modify: `CLAUDE.md` (section 5.15, env and gotchas, changelog), the spec status line, this plan's checkboxes

- [ ] **Step 1:** Document the audit stage, the single browser slot (one audit at a time), the 50 per day cap meaning audits only need to stay ahead of it, the designed email rules, and the `import-designed` variant in History.
- [ ] **Step 2:** Run the full suite: `pytest -q`. Expected: all pass, 0 failed.
- [ ] **Step 3: Accuracy check (manual, needs the user):** for each of the first 10 audited leads, open the lead's real site next to the email preview and confirm that every shot's red box is on the thing its sentence describes. Record every miss. The target is 0 misses; any miss means stop, find the cause (box position, overlay, vision gate) and fix it before continuing.
- [ ] **Step 4: Trial gate (manual, needs the user):** audit 10 real leads, record the wall time and `cost_logs` total for the audit stage and the AI copy, and send the 10 drafts to a seed inbox the user controls. Check rendering in Gmail and Outlook web. Report the measured time and cost per lead to the user before any larger run. Then the pilot of 50 to 100 from the spec, watching bounces and replies in History.
- [ ] **Step 5: Commit** `git add CLAUDE.md docs && git commit -m "docs: designed flaw emails"`
