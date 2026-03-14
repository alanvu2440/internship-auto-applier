# Spring Application Plan — Fix First, Then Batch

**Created**: 2026-03-12
**Applied**: 288 | **Target**: 500+ | **Rejections**: ~109 | **Interview Invites**: 2

---

## Current State (Honest Assessment)

### What Works
| ATS | Applied | Failed | Pending | Success Rate | Notes |
|-----|---------|--------|---------|-------------|-------|
| Greenhouse | 222 | 9 | 13 | ~96% | **Best handler. Near-depleted.** |
| Ashby | 14 | 0 | 0 | 100% | **Depleted. No pending jobs.** |
| Lever | 5 | 1 | 0 | 83% | **Depleted. No pending jobs.** |
| SmartRecruiters | 30 | 2 | 65 | ~94% (simple forms) | **Broken on multi-step forms.** |
| Workday | 5 | 0 | 1,012 | ~20% | **Massive inventory, login walls.** |
| Other/Unknown | 12 | 0 | 1,051 | ~30% | iCIMS(188), Oracle(~120), Microsoft(79) |

### Follow-ups (17 total)
| Company | Role | Status |
|---------|------|--------|
| **True Anomaly** | Software Engineering Intern - Elixir | **INTERVIEW INVITE** |
| **True Anomaly** | Flight Software Intern | **INTERVIEW INVITE** |
| Moloco | Machine Learning Engineer | Follow-up |
| Sila Nanotechnologies | Software Intern - AI & Battery | Follow-up |
| Gelber Group | Technical Operations Intern | Follow-up |
| Gelber Group | Backoffice Engineering Intern | Follow-up |
| AXQ Capital | Quantitative Research Intern | Follow-up |
| Clockwork Systems | Software Engineer Intern | Follow-up |
| Bio-Techne | Hardware Engineering Intern | Follow-up |
| Bracebridge Capital | Software Engineer – BI Co-op | Follow-up |
| Spring Venture Group | Data Science Intern | Follow-up |
| Codeage | Web Developer Intern | Follow-up |
| LAXIR | Junior Full-Stack Engineer | Follow-up |
| Treehouse Strategy | Python Developer Intern | Follow-up |
| Jobsbridge | Junior Front End Engineer | Follow-up |
| KGS Technology Group | Junior Software Engineer | Follow-up |
| 360 IT Professionals | Entry Level Business/Data Analyst | Follow-up |

---

## The Problem

We keep running batches that hit the SAME broken infrastructure and waste attempts. 12 jobs are permanently failed (3 attempts each = 36 wasted runs). SmartRecruiters multi-step forms have been broken for days — Angular's form model doesn't pick up our typed values, so the form bounces back on validation.

**Root cause for SmartRecruiters**: CDP typing, `execCommand`, native value setters, and `dispatchEvent` all set the DOM value but NONE update Angular's internal form model. Zone.js patches `addEventListener`, so only REAL browser events (CDP mouse clicks at coordinates + CDP key events) go through zone.js and trigger Angular change detection. But `CDP DOM.requestNode` fails for shadow DOM elements, preventing CDP focus → preventing CDP key events from targeting the right element.

---

## Fix Plan (Priority Order)

### P0: SmartRecruiters Multi-Step Forms (65 pending jobs)
**Problem**: Values typed via CDP don't persist through Angular validation bounce. Confirm-email, linkedin, website, phone, city show empty after clicking "Next".

**Root Cause**: Angular's spl-input components use zone.js-patched event listeners. Only REAL browser events go through zone.js. Our synthetic events (`dispatchEvent`) bypass zone.js entirely.

**Fix Strategy** (coordinate-based click + CDP key events for ALL fields):
1. Get inner input's bounding rect via JS (`getBoundingClientRect()`)
2. CDP `Input.dispatchMouseEvent` at those coordinates (REAL mouse click → REAL focus)
3. CDP `Input.dispatchKeyEvent` Cmd+A → Backspace to clear
4. CDP `Input.dispatchKeyEvent` char-by-char to type (REAL key events → zone.js picks up)
5. CDP `Input.dispatchKeyEvent` Tab to blur

**Testing**:
- [ ] Dry-run a multi-step SR job (e.g., Western Digital)
- [ ] Verify confirm-email field persists after typing
- [ ] Verify clicking "Next" advances to page 2 (not bouncing back)
- [ ] Verify screening questions on page 2+ are answered
- [ ] Run 5 real SR jobs and check pass rate

**Status**: Code written (coordinate-based approach in `_nd_cdp_type_into_shadow`), NOT YET TESTED. The previous test was interrupted before it could run.

---

### P1: Greenhouse — Clear Remaining 13 Jobs
**Problem**: Only 13 pending. 9 failed (3 attempts each). These are likely edge cases.

**Fix**:
- [ ] Check what the 9 failed jobs have in common (login wall? specific question type?)
- [ ] Reset and retry with `--smart --assist` for manual intervention on hard cases
- [ ] Run 13 pending jobs

---

### P2: Workday — Unlock 1,012 Pending Jobs
**Problem**: Login walls block most jobs. Only 5 applied out of 1,012+ pending.

**Current capabilities**:
- Account creation works (email + password)
- Email verification works (Gmail link extraction)
- Form filling works (7-page forms pass in dry-run)
- Login with saved credentials works

**Blockers**:
- Most Workday tenants require account creation first
- Each tenant is a separate account (different wd1/wd5 subdomains)
- Rate limiting: 90s gaps, 4/hr to avoid detection
- Some tenants have additional verification (2FA, captcha)

**Fix**:
- [ ] Run `--workday-accounts` batch to create accounts for top tenants
- [ ] Build list of tenants with accounts, batch apply
- [ ] Target 50-100 Workday apps

---

### P3: Generic/Other Handler — 1,051 Pending Jobs
**Breakdown**: iCIMS (188), Oracle Cloud (120+), Microsoft Careers (79), Workable (21), EA (16), Cisco (13), etc.

**Problem**: Each is a different ATS with its own form structure. Generic handler has ~30% success rate.

**Fix**:
- [ ] iCIMS: Always requires login → skip for now
- [ ] Oracle Cloud HCM: Similar to Workday, requires accounts → investigate
- [ ] Microsoft Careers: Custom portal → investigate feasibility
- [ ] Run generic handler with `--smart` flag on best-effort basis
- [ ] Target 20-30 apps from Workable, Jobvite, BambooHR (simpler ATSes)

---

## Execution Order

```
WEEK 1 (NOW):
  Day 1: Fix + test SmartRecruiters coordinate-based typing
  Day 1: Run 5 dry-run SR tests → verify multi-step works
  Day 2: Run 65 real SR jobs
  Day 2: Run 13 remaining Greenhouse jobs

WEEK 2:
  Day 3: Workday account creation batch (top 20 tenants)
  Day 4: Workday application batch (50-100 jobs)
  Day 5: Generic handler batch with --smart (30-50 jobs)

TARGET: 288 + 65 + 13 + 75 + 30 = ~470 applied
```

---

## Testing Checklist (Before ANY Batch Run)

- [ ] **Dry-run 3 jobs** from target ATS → all pass
- [ ] **Check screenshots** in `data/screenshots/` → fields visually filled
- [ ] **Check logs** for "FAIL", "empty required fields" → none
- [ ] **Verify multi-step navigation** → form advances past page 1
- [ ] **Only then** run real batch

---

## SmartRecruiters Technical Deep-Dive

### DOM Structure
- `spl-input` → Shadow DOM → `<input>` (text fields)
- `spl-phone-field` → Shadow DOM → `spl-input` → Shadow DOM → `<input type="tel">`
- `spl-autocomplete` → Shadow DOM → `spl-input` → Shadow DOM → `<input>` (city)
- `spl-select` → Shadow DOM → `<select>` (dropdowns)
- `spl-textarea` → Shadow DOM → `<textarea>` (message)
- `spl-checkbox` → Shadow DOM → `<input type="checkbox">` (consent)
- `spl-dropzone` → Shadow DOM → `<input type="file">` (resume)

### What We Tried (ALL FAILED to update Angular model)
1. ❌ `execCommand('insertText')` — sets DOM value, Angular ignores
2. ❌ Native value setter + `dispatchEvent('input')` — Angular ignores synthetic events
3. ❌ `host.value = text` + events — host property doesn't propagate to component model
4. ❌ `CustomEvent('spl-change')` — zone.js symbol exists but listener not triggered
5. ❌ CDP `DOM.focus` + `Input.dispatchKeyEvent` — DOM.requestNode fails for shadow DOM

### What Should Work (UNTESTED)
6. **CDP mouse click at coordinates + CDP key events** — REAL browser events that go through zone.js patched addEventListener. This is how the phone field gets filled (when it works). Need to generalize to all fields.

### Zone.js Symbols Found on spl-input Host
- `__ngContext__` — Angular component context
- `__zone_symbol__spl-changefalse` — zone patched custom event listener
- `__zone_symbol__spl-touchedfalse` — zone patched custom event listener
- `__zone_symbol__spl-clearfalse` — zone patched custom event listener
- `__zone_symbol__ononinputpatched` — zone patched standard input event
- `__zone_symbol__ononchangepatched` — zone patched standard change event
- `window.ng` — NOT available (production mode, no debug API)
