# CTO Bug Report — Active Observation March 16, 2026

## Batch: 100 jobs, observed first 10

### Browser Health: STABLE
- 1 Chrome start, 0 restarts (FIXED from 45 restarts before)

### Results: 1 PASS / 9 FAIL

---

## BUG 1: OnLogic (Workable ATS) — Cookie banner blocks everything
**Impact:** HIGH — Workable is a common ATS, all Workable jobs fail
**Root cause:** Cookie consent modal covers the entire page. Bot can't find Apply button or form fields.
**Evidence:** Screenshot shows "About Cookies" modal with "Accept all" button visible. Simplify says "Click into the application to get started" — never reached the form.
**Fix deployed?** YES — added cookie banner dismissal to generic handler. But OnLogic ran before fix was live. Next run should work.
**Also:** The "Apply" button on Workable is behind the cookie banner — need to dismiss first, THEN click Apply.

## BUG 2: Tenstorrent (Greenhouse) — Missing "internship length" + "hours per week" questions
**Impact:** MEDIUM — affects Tenstorrent specifically, 3 failed attempts
**Root cause:** Two required React-Select dropdowns not in template bank: "What length of internship are you available for?" and "Please specify how many hours per week you are available:"
**Evidence:** PRE-SUBMIT GUARD blocked submission with 4 empty required fields.
**Fix deployed?** YES — added to greenhouse.yaml template bank just now.

## BUG 3: Generic handler fills 0 fields on job listing pages
**Impact:** HIGH — many generic ATS jobs never reach the application form
**Root cause:** Bot navigates to job URL which shows a listing page, not the form. Needs to click "Apply" button first. The `_click_apply_button()` method exists but fails when overlays (cookies, location) block it.
**Fix:** Cookie dismissal added. But may also need: scroll down to find Apply button, wait for page load, handle iframe-based forms.

## BUG 4: Gemini API rate limit hit
**Impact:** LOW — only affects --smart mode scanner
**Root cause:** "429 You exceeded your current quota" on vision pass. Primary key exhausted.
**Fix:** Auto-switches to backup key. Not blocking applications.

## BUG 5: Same job retried 3x (Tenstorrent, OnLogic)
**Impact:** MEDIUM — wastes time on jobs that will always fail
**Root cause:** Job stays "pending" with incremented attempts. Same bug hits 3 times.
**Fix:** After 2 failures with same error, should mark as skipped not retry.

---

## Summary Table
| Bug | ATS | Fix Status | Impact |
|-----|-----|------------|--------|
| Cookie banner blocks page | Workable/Generic | DEPLOYED | HIGH |
| Missing internship length Q | Greenhouse | DEPLOYED | MEDIUM |
| Generic handler can't find Apply | All Generic | PARTIAL | HIGH |
| Gemini 429 rate limit | All | AUTO-FALLBACK | LOW |
| Same job retried 3x | All | NOT FIXED | MEDIUM |
