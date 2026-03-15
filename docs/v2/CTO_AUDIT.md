# CTO Architecture Audit

> Last updated: 2026-03-14
> Status: All issues catalogued, fix sprints planned

---

## Executive Summary

The codebase is **functional and ships value** — 183 applications submitted, handlers for 6 ATS platforms, a working golden path for question answering. Key issues are: duplicated logic across modules, extracted modules only partially wired up, dead code, hardcoded secrets, and a 620-line monolith in main.py.

---

## Per-File Status

| File | Lines | State | Issues |
|------|-------|-------|--------|
| `src/main.py` | ~1550 | Working, bloated | 620-line `apply_to_job()` monolith, dead `_open_manual_tabs`/`max_open_tabs` code |
| `src/ai_answerer.py` | ~1700 | Working | Uncached OptionMatcher, dead `_call_ai()`, duplicated helpers |
| `src/browser_manager.py` | 389 | Working | Proxy param accepted but never used |
| `src/handlers/base.py` | 838 | Working | Dead `check_required_fields_filled()` (superseded) |
| `src/handlers/greenhouse.py` | ~4700 | Working (~95%) | Dead `_fill_basic_fields()`, duplicated standard/embedded form fill |
| `src/handlers/smartrecruiters.py` | ~1000 | Working (~85%) | Own closed-job detection (8 vs 25 shared indicators) |
| `src/handlers/lever.py` | ~700 | Working (~90%) | Clean |
| `src/handlers/ashby.py` | ~800 | Working (~95%) | Hardcoded email alias |
| `src/handlers/workday.py` | ~500 | Partial (~20%) | Hardcoded password |
| `src/handlers/icims.py` | ~1500 | Skipped (0%) | Hardcoded password |
| `src/form_filler.py` | ~2500 | Working | Greenhouse-specific selectors in "universal" filler |
| `src/form/option_matcher.py` | 680 | Working | Duplicate helpers, single caller |
| `src/detection/job_status.py` | 144 | Working | Only used by base.py |
| `src/modes/esc_monitor.py` | 100 | Working | Clean |

---

## Issue Matrix

### Phase 1: Critical Blockers

| # | Issue | File | Impact | Effort |
|---|-------|------|--------|--------|
| 1.1 | SR uses 8 closed-job indicators (shared has 25+) | `smartrecruiters.py:784` | Wastes time on dead jobs | 15 min |
| 1.2 | Proxy param never passed to browser | `browser_manager.py:36` | Anti-detection broken | 30 min |
| 1.3 | Hardcoded passwords in source | `workday.py:84`, `icims.py:35` | Security risk | 15 min |
| 1.4 | Hardcoded Ashby email | `ashby.py:39` | Config issue | 10 min |
| 1.5 | `max_open_tabs` accepted but never enforced | `main.py:1076` | UX mismatch | 20 min |

### Phase 2: Integration Gaps

| # | Issue | File | Impact | Effort |
|---|-------|------|--------|--------|
| 2.1 | SR should use `detection.job_status.is_job_closed()` | `smartrecruiters.py:784` | -17 missing patterns | 20 min |
| 2.2 | OptionMatcher created fresh every call | `ai_answerer.py:1664` | Perf waste | 10 min |
| 2.3 | Standard/embedded form fill are copy-paste | `greenhouse.py:469,551` | 50 lines duplicated | 30 min |
| 2.4 | `_failed_urls` populated but never consumed | `main.py:1077,1329` | Dead feature | 15 min |

### Phase 3: Dead Code

| # | Issue | File | Lines |
|---|-------|------|-------|
| 3.1 | `check_required_fields_filled()` never called | `base.py:277` | 40 |
| 3.2 | `_fill_basic_fields()` never called | `greenhouse.py:702` | 27 |
| 3.3 | Dead `_call_ai()` nested function | `ai_answerer.py:1557` | 8 |
| 3.4 | `_get_grad_date()`/`_get_internship_term()` duplicated | ai_answerer + option_matcher | 24 |

### Phase 4: Missing Features

| # | Feature | Status | Effort |
|---|---------|--------|--------|
| 4.1 | "Already applied" pre-check | Not implemented | 2 hrs |
| 4.2 | Conditional fields (show/hide) | Workday only | 4 hrs |
| 4.3 | Education typeahead (non-Workday) | Missing for GH/Lever | 2 hrs |

---

## Fix Sprint Plan

| Sprint | What | Time | Items |
|--------|------|------|-------|
| 1 | Security — move hardcoded passwords/emails to secrets.yaml | 30 min | 1.3, 1.4 |
| 2 | SR closed-job detection — use shared module | 30 min | 1.1, 2.1 |
| 3 | Dead code cleanup | 20 min | 3.1, 3.2, 3.3 |
| 4 | Cache OptionMatcher, dedup helpers | 30 min | 2.2, 3.4 |
| 5 | GH form fill dedup — extract shared helper | 45 min | 2.3 |
| 6 | Wire proxy, fix max_open_tabs | 1 hr | 1.2, 1.5 |
| 7 | Decompose `apply_to_job()` | 2 hrs | main.py monolith |
| 8 | Education typeahead for GH/Lever | 2 hrs | 4.3 |

---

## What Works Well

1. **Golden path** — template bank → config → cache → Gemini → fallback (properly prioritized)
2. **Unified browser** — nodriver + Playwright CDP, single Chrome window, clean fallback
3. **Pre-submit guard** — thorough required-field checking with React-Select awareness
4. **Gemini cost tracking** — $300 hard cap with auto-failover
5. **Simplify integration** — auto-detect, auto-click, field diffing
6. **Per-ATS rate limiting** — different delays based on detection sensitivity
7. **Application logging** — screenshots, JSONL stream, JSON summaries, knowledge base
