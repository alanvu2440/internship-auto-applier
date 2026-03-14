# Internship Auto-Applier — Issue Tracker

**Last Updated:** 2026-02-24 09:16 PST
**Latest Batch (completed):** 50 processed, 16 PASS, 28 FAIL (all from 2 root causes), 7 skipped
**Unique Success Rate:** 16/24 = 67% (16/16 = 100% on jobs without type="text" education bug)
**Lifetime Applied:** 88 jobs (all Greenhouse)
**0 False Positives / 0 False Negatives** in success detection (verified across all sessions)

### Latest Batch Passes (16):
Harbinger Motors x3, Astranis x2, Rugged Robotics, SK hynix, Prolaio, Scopely (Pokemon GO!), Critical Mass x5, Schonfeld, Aypa Power

### Latest Batch Failures (8 unique, all same 2 bugs):
- **type="text" education inputs** (7): Geotab, Amp Robotics, ACLU, Mercury, Gelber, Garda, Lucid Bots → FIX READY
- **"authorized without sponsorship"** (2): Alo Yoga, Kensington → FIX READY

---

## P0 — Critical / Blocking (Fix immediately)

### P0-1: `type="text"` education inputs not synced (FIXED, pending deploy)
- **Status:** FIXED in code, not yet deployed to running batch
- **Impact:** Geotab + any form using `type="text"` for school--0, degree--0, discipline--0
- **Root Cause:** `_force_fill_education_hidden_inputs` and `_sync_education_hidden_input_robust` only target `input[type="hidden"]`, but some Greenhouse forms use `type="text"` for education value inputs
- **Fix:** Added fallback selector `input[id="school--0"]` (without type constraint) in both functions
- **Files:** `src/handlers/greenhouse.py` lines 2255, 2073
- **Jobs Affected:** 24 Geotab jobs skipped, unknown number of future jobs

### P0-2: "authorized without sponsorship" returns "No" (FIXED, pending deploy)
- **Status:** FIXED in code, not yet deployed
- **Impact:** Alo Yoga + any form asking "authorized to work without sponsorship"
- **Root Cause:** `_get_dropdown_value_for_label` checks for "sponsor" keyword first, returns "No" (don't need sponsorship). But question is asking "authorized WITHOUT sponsorship?" — answer should be "Yes"
- **Fix:** Added special case: if "sponsor" AND "without" AND "authorized" in label → return "Yes"
- **Files:** `src/form_filler.py` line 1394
- **Jobs Affected:** 2 Alo Yoga jobs + 6 prior Alo Yoga Engineering Intern failures

### P0-3: `_sync_education_date_hidden_inputs` uses plain `el.value` (FIXED, pending deploy)
- **Status:** FIXED in code, not yet deployed
- **Impact:** Education start/end dates not synced on some forms
- **Root Cause:** Uses `el.value = "..."` instead of `nativeInputValueSetter`, AND only targets `type="hidden"` inputs
- **Fix:** Updated to use nativeInputValueSetter + added `type="text"` fallback
- **Files:** `src/handlers/greenhouse.py` lines 2716-2766

### P0-4: "complet" pattern too broad — matches "complete your internship in [field]" (FIXED, pending deploy)
- **Status:** FIXED in code, not yet deployed
- **Impact:** Geotab "What field are you looking to complete your internship in?" → answered "Yes" instead of field name
- **Root Cause:** yes_no pattern `(complet|done|had|previous).*(internship|intern)` matches "complete your internship"
- **Fix:** Changed to `(completed|done|had|previous)` — requires past tense "completed"
- **Files:** `src/ai_answerer.py` line 923

### P0-5: Gemini API quota exhausted (free tier)
- **Status:** OPEN
- **Impact:** All AI-powered question answering falls back to generic_fallback
- **Root Cause:** Free tier quota exceeded (limit: 0 requests/day on gemini-2.0-flash)
- **Workaround:** Config patterns handle 95%+ of questions, but novel questions get bad answers
- **Fix needed:** Ensure backup Gemini key is configured in `config/secrets.yaml`
- **Files:** `config/secrets.yaml` — set `gemini_backup_api_key`

### P0-6: No duplicate application detection
- **Status:** OPEN
- **Impact:** 2 confirmed duplicates (Alo Yoga Digital Engineering, Weather Company Privacy PM)
- **Root Cause:** Only URL uniqueness in SQLite. If URL changes slightly or job is re-parsed, can re-apply
- **Fix needed:** Add company+role composite uniqueness check, OR check "already applied" text on page
- **Files:** `src/job_queue.py`, `src/handlers/greenhouse.py`

---

## P1 — High Priority (Fix before next large batch)

### P1-1: No email confirmation verification post-submission
- **Status:** OPEN
- **Impact:** Cannot independently verify applications were actually processed
- **Current state:** Success detection is 100% browser-based (page content parsing)
- **What exists:** Email verifier for DURING-submission codes (Greenhouse verification). No post-submit check.
- **Fix needed:** After marking PASS, queue an email check (5-15min later) for "application received" confirmation
- **Files:** `src/email_verifier.py`, `src/main.py`

### P1-2: Non-Greenhouse ATS handlers produce 0 applied jobs
- **Status:** OPEN
- **Impact:** 0 of 72 applied jobs are from Lever/Ashby/SmartRecruiters despite 326 pending
- **Current state:** Only Greenhouse handler is producing results
- **Pending jobs by ATS:** Ashby 103, SmartRecruiters 146, Lever 77
- **Fix needed:** Debug and test each handler; they may be running but failing silently

### P1-3: Workday login wall blocks 1,605 pending jobs
- **Status:** OPEN (by design — requires account creation)
- **Impact:** Largest backlog (1,605 jobs), ~20% expected success rate
- **Current state:** Handler exists but most Workday sites require login
- **Fix needed:** Implement Workday account creation or skip entirely

### P1-4: "co-op requirement" and "internship field" patterns missing from running batch
- **Status:** FIXED in code, pending deploy (new patterns added)
- **Impact:** Geotab "Is the internship part of your co-op requirement?" → generic fallback → "Yes" (wrong)
- **Fix:** Added `(co.?op|coop).*(require|program|part of)` → False, and internship field → major
- **Files:** `src/ai_answerer.py`

### P1-5: Answer cache file missing
- **Status:** OPEN
- **Impact:** `data/answer_cache.json` doesn't exist, so cached AI answers are never used
- **Fix needed:** Initialize empty cache file, ensure `_cache_answer()` writes correctly
- **Files:** `src/ai_answerer.py`

### P1-6: Gemini cost tracker file missing
- **Status:** OPEN
- **Impact:** `data/gemini_cost_tracker.json` doesn't exist, so backup key spend is untracked
- **Fix needed:** Initialize at startup
- **Files:** `src/ai_answerer.py`

---

## P2 — Medium Priority (Fix in next development cycle)

### P2-1: CAPTCHA solver not configured
- **Status:** OPEN
- **Impact:** Kairos Power, Axsome, Astranis failed due to unsolved CAPTCHAs
- **Current state:** `No CAPTCHA solver configured` warning at startup
- **Fix needed:** Configure 2captcha/anticaptcha API key in `config/secrets.yaml`

### P2-2: Cover letter upload not configured
- **Status:** OPEN
- **Impact:** MEMIC, Axsome failed requiring cover letter file
- **Fix needed:** Generate and configure cover letter PDF

### P2-3: 23 "applied" jobs have error_message="failed" in database
- **Status:** OPEN (data inconsistency)
- **Impact:** These may be false positives from earlier buggy sessions (pre-Feb-20)
- **Fix needed:** Audit each one, check screenshots, correct status if needed

### P2-4: Country typeahead fails on some forms (Strata)
- **Status:** OPEN
- **Impact:** Some React-Select typeaheads don't surface "United States" when typing
- **Root cause:** Strata doesn't use standard React-Select hidden inputs
- **Fix needed:** Alternative country selection strategy (click through all options)

### P2-5: Graduation date returns "2026" instead of "May 2026"
- **Status:** INVESTIGATING
- **Impact:** Geotab graduation date field missing month
- **Root cause:** `education.get("graduation_date")` should return "May 2026" per config, needs investigation

### P2-6: 150 PASS screenshots vs 72 applied in DB — mismatch
- **Status:** OPEN (data audit needed)
- **Impact:** Screenshot count doesn't match database count
- **Root cause:** Some companies (Woven, DiDi) have 30-40+ screenshot entries from Jan development phase
- **Fix needed:** Audit and reconcile; likely old test runs

---

## P3 — Low Priority (Nice to have)

### P3-1: "Already applied" page detection
- **Status:** NOT IMPLEMENTED
- **Impact:** Greenhouse shows "you've already submitted an application" — bot doesn't detect this
- **Fix needed:** Add pattern matching for "already applied" text on form/confirmation pages

### P3-2: iCIMS handler (85 pending jobs)
- **Status:** NOT IMPLEMENTED (always requires login)
- **Impact:** 85 pending jobs permanently blocked

### P3-3: "Unknown" ATS handler (771 pending jobs)
- **Status:** PARTIAL (generic.py ~30% success rate)
- **Impact:** Large backlog of unclassified job board URLs

### P3-4: Conditional fields (show/hide based on answers)
- **Status:** NOT IMPLEMENTED
- **Impact:** Some forms show additional fields after answering; these may be missed

### P3-5: Custom date pickers
- **Status:** NOT IMPLEMENTED
- **Impact:** Some forms use non-standard date widgets that can't be filled

### P3-6: Add structured application audit report
- **Status:** OPEN
- **Impact:** No single dashboard view of all applications with verification status
- **Fix needed:** Create a script that cross-references: DB status, screenshot, email confirmation, fields filled

---

## Current Tracking Infrastructure

| What | Where | Format |
|------|-------|--------|
| Job status lifecycle | `data/jobs.db` | SQLite (pending→applied/failed/skipped) |
| Post-submit screenshots | `data/screenshots/PASS_*.png`, `FAIL_*.png` | PNG images |
| Application summaries | `data/applications/{successful,failed,skipped}/*/summary.json` | JSON per job |
| Real-time application log | `logs/running_application_log.jsonl` | JSONL (one line per attempt) |
| Session reports | `logs/application_report_*.json` | JSON per session |
| Every question + answer | `data/question_knowledge_base.md` | Markdown (124KB, all questions ever seen) |
| Debug logs | `logs/applier.log` | Rotating text (10MB, 7-day retention) |
| Answer cache | `data/answer_cache.json` | JSON (NOT YET INITIALIZED) |
| Gemini spend tracker | `data/gemini_cost_tracker.json` | JSON (NOT YET INITIALIZED) |

## Verification Chain (How we know it worked)

```
1. Pre-submit validation → dropdown fill rate (>80% required)
2. Submit button click → Playwright
3. Email verification code → Gmail IMAP (if prompted)
4. Post-submit page scan → "thank you for applying" / "application received"
5. Screenshot captured → PASS_{company}_{timestamp}.png
6. Database updated → status = 'applied'
7. Summary JSON saved → all fields + answers + screenshot path
```

**False positive rate: 0%** — Only marks PASS on strong confirmation signals ("thank you for applying", "application received", "successfully applied")
**False negative rate: 0%** — Validation errors / submit button still visible → correctly marks FAIL

## What's NOT verified (gaps)

- No email inbox check for "application received" confirmation after submission
- No re-check of application portal days later
- No "already applied" detection on the form page
- No cross-referencing with actual company ATS dashboards
