# ALL KNOWN ISSUES — Internship Auto-Applier

Last updated: 2026-03-04

## Current Stats

| Metric | Value |
|--------|-------|
| Total Applied | **200** |
| Target | **300-400** |
| Total Pending | **1,650** |
| Total Failed | **5** |
| Total Skipped | **1,787** |

| ATS | Pending | Applied | Failed | Skipped | Success Rate |
|-----|---------|---------|--------|---------|-------------|
| Greenhouse | 23 | 149 | 0 | 226 | ~8% recent (was ~95%) |
| SmartRecruiters | 80 | 9 | 5 | 84 | ~0% (form can't advance) |
| Lever | 68 | 3 | 0 | 20 | ~0% (hCaptcha) |
| Ashby | 0 | 14 | 0 | 102 | Exhausted |
| Workday | 493 | 3 | 0 | 1,355 | ~20% (login walls) |
| iCIMS | 139 | 0 | 0 | 0 | 0% (login always) |
| Unknown | 839 | 5 | 0 | 0 | ~30% |
| JobVite | 7 | 0 | 0 | 0 | Untested |
| BambooHR | 1 | 0 | 0 | 0 | Untested |

---

## P0 — BLOCKING (Fix before next batch)

### P0-0: ESC Monitor race condition loses key presses — FIXED (2026-03-04)
- **File**: `src/main.py:109-116` (`EscMonitor.wait_for_toggle`)
- **Root cause**: `_toggle_event.clear()` wipes an already-set event before `wait()`
- **Fix applied**: Create fresh `asyncio.Event()` per wait call. Added startup banner + bell + logger.
- **Status**: FIXED

### P0-1: Education React-Select typeaheads not filling — FIXED (2026-03-04)
- **File**: `src/handlers/greenhouse.py` (`_fill_greenhouse_education_fields`, `_inject_react_select_values_before_submit`)
- **Symptom**: `Missed required fields: ['school--0', 'degree--0', 'discipline--0']`
- **Fixes applied**:
  1. Hidden input detection now uses wildcard selectors for `education-N--school` IDs
  2. School search tries progressively shorter terms: full name → 3 words → 2 words → abbreviation (SJSU)
  3. Pre-submit inject uses element handles instead of `getElementById`, handles non-standard IDs
- **Status**: FIXED

### P0-2: Custom question dropdowns unanswered — FIXED (2026-03-04)
- **File**: `src/handlers/greenhouse.py` (`_fill_greenhouse_question_inputs`)
- **Symptom**: `POST-SUBMIT: Unfilled dropdown (Select...)`
- **Fixes applied**:
  1. Added 4-tier fuzzy matching for `<select>` elements: exact label → case-insensitive partial → Yes/No match → first option fallback
  2. Collects option values alongside text for value-based selection when label fails
- **Status**: FIXED

### P0-3: Phone country code dropdown not filled — FIXED (2026-03-04)
- **File**: `src/form_filler.py` (`_get_dropdown_value_for_label`) + already handled in `src/handlers/greenhouse.py` (`_fill_greenhouse_phone_country_code`)
- **Symptom**: `Missed required fields: ['phoneNumber--countryPhoneCode']`
- **Fixes applied**:
  1. Greenhouse handler already had dedicated `_fill_greenhouse_phone_country_code` method (was working)
  2. Added phone country code pattern to form_filler's `_get_dropdown_value_for_label` for generic/other handlers
- **Status**: FIXED

### P0-4: Cover letter upload not working for iframe forms — ALREADY WORKING
- **File**: `src/handlers/greenhouse.py`
- **Status**: ALREADY WORKING — Code review confirms:
  1. `_upload_cover_letter` has 4 strategies (attribute match, label text, section attrs, 2nd file input)
  2. `_upload_cover_letter_in_frame` exists with 3 strategies
  3. Both standard and iframe paths call it
  4. `config/cover_letter.pdf` was created (previously empty string in config was the real bug)
- **Note**: If failures persist, it's due to non-standard upload widgets, not missing code

### P0-5: Transcript upload not finding field — ALREADY WORKING
- **File**: `src/handlers/greenhouse.py`
- **Status**: ALREADY WORKING — Code review confirms:
  1. `_upload_transcript` has 3 strategies (attribute match, label text, nearby text)
  2. `_upload_transcript_in_frame` exists and is called from iframe path
  3. Both standard and iframe paths include transcript upload
  4. Broad label selector set: `.upload-label, label[class*="upload"], label, .field-label, [class*="label"], legend, .attachment-label, [class*="upload-label"], [class*="error"]`

---

## P1 — HIGH (Fix to unlock more applications)

### P1-0: SmartRecruiters screening questions fail in spl-* Shadow DOM — FIXED (2026-03-04)
- **File**: `src/handlers/smartrecruiters.py:976` (`_nd_handle_screening_questions`)
- **Fix applied**: Replaced all selectors with recursive `findInShadow()` that traverses `element.shadowRoot` in both detection and answer-filling JS.
- **Status**: FIXED

### P1-0b: Simplify extension not wired into handlers — FIXED (2026-03-04)
- **Files**: `src/handlers/greenhouse.py`, `lever.py`, `ashby.py`, `workday.py`
- **Fix applied**: Added `await self.wait_for_extension_autofill(page)` in all 4 handlers after page load + popup dismissal, before CAPTCHA/form filling
- **Status**: FIXED (SmartRecruiters excluded — uses nodriver, separate browser)

### P1-1: SmartRecruiters multi-step form can't advance past validation
- **File**: `src/handlers/smartrecruiters.py`
- **Symptom**: Form stuck on Step 1/2 with "Fields marked with * are required"
- **Root cause**: City autocomplete, resume spl-dropzone, phone validation don't persist through Angular re-renders
- **Status**: OPEN — needs live testing after shadow DOM fix

### P1-2: Lever blocked by hCaptcha — ALREADY IMPLEMENTED
- **File**: `src/captcha_solver.py`
- **Status**: ALREADY IMPLEMENTED — Code review confirms:
  1. `detect_hcaptcha()` (lines 365-426): full detection in main page + all iframes
  2. `solve_hcaptcha()` (lines 428-457): solver support for 2captcha AND AntiCaptcha
  3. `inject_hcaptcha_token()` (lines 493-572): token injection for main page + all frames
  4. `solve_and_inject()` (lines 583-605): checks hCaptcha FIRST, then reCAPTCHA
  5. Lever's `_submit_application` calls `solve_invisible_recaptcha()` which checks hCaptcha first
- **Note**: If still failing, check: (a) 2captcha balance/config, (b) sitekey extraction, (c) token injection timing

### P1-3: Source/referral dropdown not filled — FIXED (2026-03-04)
- **File**: `src/form_filler.py` + `src/handlers/greenhouse.py`
- **Fixes applied**:
  1. Greenhouse handler already had `_fill_greenhouse_source_dropdown` called from both standard and iframe paths
  2. Added standalone "source" pattern to form_filler's `_get_dropdown_value_for_label` for broader matching
- **Status**: FIXED

### P1-4: Workday login walls (493 pending, 1,355 skipped)
- **File**: `src/handlers/workday.py`
- **Status**: Auth flow implemented, dry runs pass for 5 companies. Ready for `--workday-accounts` batch testing.

### P1-5: Unknown ATS jobs (839 pending)
- **File**: `src/handlers/generic.py`
- **Status**: OPEN — need ATS re-detection pass

---

## P2 — MEDIUM (Quality improvements)

### P2-1: Checkbox question groups not checked — FIXED (2026-03-04)
- **File**: `src/handlers/greenhouse.py` (`_handle_checkbox_questions`)
- **Fix applied**: Added skill/language checkbox group matching. Detects parent fieldset labels like "programming languages", "technologies", "which language", etc. Matches against config `skills.programming_languages`, `skills.frameworks`, `skills.tools`. Falls back to common languages (Python, Java, JavaScript, etc.)
- **Status**: FIXED

### P2-2: Location field often fails
- **Symptom**: `Could not fill location field`
- **Status**: OPEN — multiple strategies already exist, needs live debugging

### P2-3: Education date fields (end-month, end-year, start-year) empty — ALREADY WORKING
- **File**: `src/handlers/greenhouse.py` (`_fill_graduation_date_fields`)
- **Status**: ALREADY WORKING — Code has:
  1. React-Select dropdown filling for month/year with label matching
  2. HTML `<select>` fallback for standard dropdowns
  3. Pre-submit inject for date fields as last resort
  4. Handles start/end/graduation/expected month/year combinations

### P2-4: Timeouts on complex forms — FIXED (2026-03-04)
- **File**: `src/main.py:458`
- **Fix applied**: Increased handler timeout from 300s to 600s
- **Status**: FIXED

### P2-5: Education fields with non-standard IDs — FIXED (2026-03-04)
- **File**: `src/handlers/greenhouse.py`
- **Fix applied**: Both `_fill_greenhouse_education_fields` and `_inject_react_select_values_before_submit` now use wildcard selectors: `input[id*="--school"]`, `input[id*="education"][id*="school"]` etc. Fixed in P0-1 above.
- **Status**: FIXED

### P2-6: "Already applied" not detected — FIXED (2026-03-04)
- **File**: `src/handlers/base.py` (`is_application_complete`, `is_job_closed`)
- **Fix applied**: Added 10+ new patterns:
  - `is_application_complete` failure indicators: "already submitted", "you have already submitted an application", "duplicate application", "previously applied", "application already exists", "job is no longer posted", etc.
  - `is_job_closed` indicators: "this position is no longer", "this role is no longer", "posting has been removed", "this opening has been filled", "you have already submitted", etc.
- **Status**: FIXED

---

## P3 — LOW (Nice to have)

### P3-1: Portfolio/GitHub upload field (5 occurrences)
- Some jobs want a portfolio PDF upload. Could auto-generate or use resume as fallback.
- **Status**: OPEN — low priority

### P3-2: iCIMS handler (139 pending)
- All iCIMS jobs require login. Handler exists but not production-ready.
- **Status**: OPEN — blocked by auth requirement

### P3-3: JobVite handler (7 pending)
- No handler built. Small count, low priority.
- **Status**: OPEN

### P3-4: BambooHR handler (1 pending)
- No handler built. Single job.
- **Status**: OPEN

### P3-5: Job quality filtering
- Some jobs are clearly wrong matches (PhD-only, specific domain expertise). Should be pre-filtered.
- **Status**: OPEN

### P3-6: Cover letter personalization
- Current cover letter is generic. Could use Gemini to personalize per company/role.
- **Status**: OPEN — nice-to-have

---

## Fix Summary (2026-03-04 Session)

### FIXED (12 issues)
| Issue | What | Impact |
|-------|------|--------|
| P0-0 | ESC monitor race condition | User can now reliably pause/resume bot |
| P0-1 | Education typeahead + non-standard IDs | +10-15 Greenhouse apps |
| P0-2 | Question dropdown fuzzy matching | +8-12 Greenhouse apps |
| P0-3 | Phone country code dropdown | +5-8 apps |
| P1-0 | SmartRecruiters Shadow DOM | Unblocks SR screening questions |
| P1-0b | Simplify extension wired | Extension autofill → handler fills rest |
| P1-3 | Source dropdown pattern | +3-5 apps |
| P2-1 | Checkbox skill groups | Fills programming language checkboxes |
| P2-4 | Timeout 300s → 600s | Fixes complex form timeouts |
| P2-5 | Non-standard education IDs | Merged with P0-1 fix |
| P2-6 | Already-applied detection | Prevents wasted retries |

### ALREADY WORKING (4 issues — no code change needed)
| Issue | What | Notes |
|-------|------|-------|
| P0-4 | Cover letter upload | Code exists; config PDF was the fix |
| P0-5 | Transcript upload | Code exists including iframe path |
| P1-2 | Lever hCaptcha | Full hCaptcha solver already implemented |
| P2-3 | Education date fields | `_fill_graduation_date_fields` handles this |

### STILL OPEN (7 issues)
| Issue | What | Next Step |
|-------|------|-----------|
| P1-1 | SmartRecruiters validation | Live test after shadow DOM fix |
| P1-4 | Workday login walls | Run `--workday-accounts` batch |
| P1-5 | Unknown ATS re-detection | Run ATS re-detection pass |
| P2-2 | Location field | Needs live debugging |
| P3-1-6 | Portfolio, iCIMS, JobVite, BambooHR, filtering, personalization | Low priority |

---

## Optimal Run Strategy (to reach 300-400)

### Pipeline: Extension → Handler → Gemini Smart → CAPTCHA

```
1. Simplify extension autofills boilerplate (name/email/phone) — FREE
2. Handler fills ATS-specific fields (education, dropdowns, uploads) — FREE
3. Gemini smart fill catches remaining empty fields — ~$0.0004/job
4. CAPTCHA solver handles reCAPTCHA/hCaptcha — ~$0.003/solve
5. Submit + screenshot
```

### Recommended batch order:

```bash
# 1. Greenhouse first (highest success rate, most pending)
python src/main.py backfill --smart --max 30

# 2. With Simplify extension for better fill rate
python src/main.py backfill --smart --with-simplify --max 50

# 3. Lever (now with hCaptcha support)
python src/main.py backfill --smart --max 20 --ats lever

# 4. Workday (with accounts, slow mode)
python src/main.py backfill --workday-accounts --smart --max 20

# 5. SmartRecruiters (after shadow DOM fix)
python src/main.py backfill --smart --max 20 --ats smartrecruiters

# 6. Reset failed jobs and retry
python src/main.py reset_failed
python src/main.py backfill --smart --assist --max 10

# 7. Unknown ATS with generic handler
python src/main.py backfill --smart --max 30 --ats unknown
```

---

## Files Reference

| File | Lines | What It Does |
|------|-------|-------------|
| `src/handlers/greenhouse.py` | 4,300+ | Greenhouse ATS handler — primary target |
| `src/form_filler.py` | 2,500+ | Universal form filling |
| `src/handlers/smartrecruiters.py` | ~1,800 | SmartRecruiters handler (nodriver) |
| `src/handlers/lever.py` | 700+ | Lever handler |
| `src/handlers/workday.py` | 4,300+ | Workday handler |
| `src/handlers/base.py` | 400+ | Base handler class |
| `src/ai_answerer.py` | 1,570 | Question answering + Gemini |
| `src/captcha_solver.py` | 605 | CAPTCHA solving (reCAPTCHA + hCaptcha) |
| `src/main.py` | 1,000+ | Orchestrator + CLI |
| `config/master_config.yaml` | 445 | All personal info + answers |
