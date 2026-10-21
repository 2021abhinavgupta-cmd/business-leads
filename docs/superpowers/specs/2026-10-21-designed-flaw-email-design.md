# Designed flaw emails for the Import page: design

Status: Proposed, waiting for approval. Written 2026-10-21.

## Intent and why

The Import page today sends a short text email (problem, MMGA line, question). The user wants each of the 1,000 imported leads to receive something closer to the Google AI Studio reference email: a designed, easy to keep reading email that (1) tells them their website has specific flaws, shows 4 to 5 screenshots of the real site with the problem boxed in red, the same way the agriculture audits already do, and (2) ends by saying the email itself was produced by automation MMGA built, so the reader links "automation" with MMGA.

The audience is a compliance, finance or operations person at a US or Indian company who got a cold email. The goal is that they open it, see it was made about their own site, and reply.

## Decided by the user

- Look: option B, "warm light" (cream background, orange accent, reads like a personal letter). Mockup: `.superpowers/brainstorm/1167-1792565534/content/email-design.html`.
- Content: a bold headline naming the company, then numbered flaw sections with screenshots, then the automation note at the end.
- Flaws are the same kind used in the agriculture audits (measured, with a red box on a real screenshot).
- Applies to all imported leads (about 1,000).
- Every email is still reviewed and approved by the user before sending.

## Decided by me (my calls, change any of them)

1. **One soft call to action**, a button "Want the full review? Reply yes" that opens a reply to the sender. No calendar link, no second ask. Reason: vendor reports (Smartlead, Clevery, Hunter) agree a single low commitment ask beats a booking ask. The data is vendor sourced, so treat it as direction, not proof.
2. **Deliverability guard rails**, because the same sources say image heavy HTML in cold mail can land in spam more often: keep the HTML under 80 KB; JPEG screenshots at 600 px wide, quality about 70, each under about 90 KB; images attached inline by Content-ID (the existing method) and not hosted; every image has alt text and fixed width and height; a plain text version is always included; **no open tracking pixel on these emails**; keep the existing unsubscribe headers and postal address footer.
3. **Pilot before the full run.** Send the first 50 to 100 designed emails, watch bounces and replies, then continue. The existing bounce pause (over 5% bounced after 20 sent) stays on.
4. **Never invent a flaw.** The number of sections equals the number of real, measured flaws that have a screenshot, up to 5. Fewer than 2 real flaws with a screenshot means the lead falls back to the existing text email and is marked "no audit flaws found".
5. **Text and claims** are written by the model and checked by the existing fact check (website claims need an exact quote on the page), now with the audit's flaw list as extra evidence. Each flaw sentence may only restate its flaw description.
6. **Dark mode:** the email sets a light background explicitly and avoids pure white and pure black so clients that auto invert still look acceptable.

## Goals

- A designed email that matches option B for every lead that has enough real flaws.
- 4 to 5 images per email: desktop homepage with red box, a phone view with red box, up to 2 more pages with red boxes, and close ups of the boxed element.
- A background audit queue that processes the imported leads while the user does other work, with visible progress and a cancel.
- The existing review drawer shows a faithful preview of the designed email, with the flaws, and Approve and Send work as today.

## Non goals

- Not changing the agriculture or main Dashboard audit flows.
- Not hosting images on a server or CDN.
- Not an editor for the design. Wording can be edited in the drawer as today; layout is fixed.
- Not guaranteeing inbox placement. Sources are vendor blogs; the pilot is the real test.

## Anti goals (a failure even if it technically works)

- Any flaw, screenshot or box that does not match the live site (a false claim to a stranger).
- A boxed screenshot that shows a cookie banner or chat widget as the "flaw".
- Emails that look like marketing blasts and land in spam, hurting the sending domain for the user's other outreach.
- Spending a large sum or running for days unattended with no way to stop.

## Constraints

- Reuse the existing audit: `scrapers/website.py audit_website`, `analyzer/visuals.py` (Playwright capture, axe-core, `_highlight_element`, close ups), `analyzer/flaws.py` ranking, and `analyzer/ai_audit.py` fact checks. Do not fork the capture logic.
- The audit takes a couple of minutes per site (10 second settle delay, Lighthouse, axe, crawl), so 1,000 leads is hours. It must run as a background queue with limited concurrency (the capture slot semaphore already exists in `visuals.py`).
- Today the audit yields at most a desktop screenshot, a mobile screenshot and one close up. Reaching 4 to 5 images means capturing boxed screenshots for up to two extra crawled pages and an extra close up. That is new work in `visuals.py`, behind the same guards.
- The sender must build a multipart message: plain text, HTML, and related inline images (`_build_initial_message` is the pattern). The import send path in `/api/send` currently sends a plain body; it needs to accept the designed HTML and its images.
- Sites that block bots, are down or render nothing give no audit. Those leads fall back to the text email.
- Opt in CAN-SPAM footer, suppression, daily cap and the existing import gates apply unchanged.

## How it fits the existing code (starting point for the plan)

- New table or columns for the per lead audit result: status (queued, running, done, failed, skipped), flaw list, image file paths, error, cost.
- A new `/api/import/batches/{id}/audit` start, progress and cancel, using a background worker that calls the existing audit with a small concurrency limit.
- `importer/drafting.py` gets a second mode: if the audit has at least 2 flaws with screenshots, ask the model for the headline, opening and one sentence per flaw (grounded), then render the option B template; otherwise use today's text draft.
- A new template module renders the HTML and the plain text from the same data, so both always agree.
- The review drawer shows the HTML preview with the images; send builds the multipart message with inline images.

## Left to the builder

- Exact HTML and CSS (table based for Outlook), image sizing and compression.
- Concurrency number, retry rules, storage location for images and clean up after send.
- The exact flaw wording rules and the fallback rules, inside the constraints above.

## Open points for the user

- Cost and time: a full run of 1,000 audits plus AI wording is estimated at hours of runtime and a few dollars to low tens of dollars of API cost. A measured estimate comes from a 10 lead trial before the full run.
- Sender identity in the email (name, short line about MMGA): the draft uses the existing sign off name and the fixed MMGA line.
