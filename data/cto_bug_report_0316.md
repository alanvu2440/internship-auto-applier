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

## BUG 6: STCU (SmartRecruiters) — City empty + Resume not uploaded
**Impact:** HIGH — SR forms fail when city autocomplete and resume upload break
**Root cause:** PRE-SUBMIT field dump shows `city*: '(no input)'` and `resume: 'no-file, attr='`.
The city autocomplete (`spl-autocomplete`) never gets a value, and resume upload via CDP `setFileInputFiles` silently fails.
**Evidence:** 3 consecutive failures on same job. Form has name/email/phone filled but city and resume empty.
**How to reproduce:** Apply to any STCU SR job — city field stays empty.
**Fix needed:**
1. City: the `_nd_fill_city_autocomplete()` method types the city but the autocomplete suggestion may not appear or get clicked. Need to verify suggestion click works, add fallback to just type "San Jose, CA" and press Enter.
2. Resume: `_nd_upload_resume()` uses CDP `Runtime.evaluate` + `setFileInputFiles`. If the `spl-dropzone` shadow DOM structure changed or file input isn't found, upload silently fails. Need to verify `input[type="file"]` exists in shadow root.
**File:** `src/handlers/smartrecruiters.py` — `_nd_fill_city_autocomplete()` (~line 530) and `_nd_upload_resume()` (~line 1400)

## BUG 7: Browser context dies — 146 CRASHES per batch (ROOT CAUSE FOUND + FIXED)
**Impact:** CRITICAL — 146 crashes in one 100-job batch, 76 jobs skipped
**Root cause found:** `start_playwright()` calls `_kill_orphaned_chrome(profile_dir)` on EVERY restart. This runs `pkill -9 -f profile_dir` which kills ANY Chrome process using that profile — including nodriver Chrome. Then `_clean_stale_locks()` removes SingletonLock files mid-session, corrupting the profile. The new Chrome launches into a corrupt profile and immediately dies.
**Evidence:** `<gracefully close start>` + `kill ESRCH` in logs = Chrome PID killed before it can start.
**Fix deployed:** YES (commit b383132)
1. Lock cleaning + orphan killing now runs ONCE on first-ever start only (`_ever_started_pw` flag)
2. Restarts never touch locks or kill processes
3. Reset restart count instead of crashing batch after 3 restarts
**File:** `src/browser_manager.py` lines 141-151

---

## Summary Table
| # | Bug | ATS | Fix Status | Impact | File |
|---|-----|-----|------------|--------|------|
| 1 | Cookie banner blocks page | Workable/Generic | DEPLOYED | HIGH | `handlers/generic.py` |
| 2 | Missing internship length Q | Greenhouse | DEPLOYED | MEDIUM | `question_banks/greenhouse.yaml` |
| 3 | Generic handler can't find Apply | All Generic | PARTIAL | HIGH | `handlers/generic.py` |
| 4 | Gemini 429 rate limit | All | AUTO-FALLBACK | LOW | `ai_answerer.py` |
| 5 | Same job retried 3x | All | NOT FIXED | MEDIUM | `job_queue.py` |
| 6 | SR city empty + resume fail | SmartRecruiters | NOT FIXED | HIGH | `handlers/smartrecruiters.py` |
| 7 | Browser context dies between jobs | Greenhouse | PARTIAL | HIGH | `browser_manager.py` |

## For Future Agents
When fixing these bugs:
- Bug 6: Read `_nd_fill_city_autocomplete()` and `_nd_upload_resume()` in SR handler. The city autocomplete needs CDP click on the suggestion item after typing. Resume needs `input[type="file"]` inside `spl-dropzone` shadow root.
- Bug 7: Read `create_stealth_page()` in browser_manager.py. The persistent context dies when profile locks conflict between nodriver and Playwright. Need to clean locks or use separate profiles.
- Always test with `python src/main.py apply "URL" --smart` on a single job before running batches.
- Check screenshots in `data/screenshots/` to visually verify what the browser sees.
