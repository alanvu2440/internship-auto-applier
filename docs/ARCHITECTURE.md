# System Architecture Deep Dive

> Living source of truth for the Internship Auto-Applier system.
> Last updated: 2026-02-24

---

## PART A: System Overview

### What This System Does

Automated job application bot that:
1. Monitors [SimplifyJobs/Summer2026-Internships](https://github.com/SimplifyJobs/Summer2026-Internships) for new postings
2. Parses job URLs and routes them to the correct ATS handler
3. Fills out application forms using pre-configured profile data + AI
4. Submits applications and captures post-submit screenshots
5. Tracks everything in SQLite with full audit trail

### High-Level Pipeline

```
┌─────────────────┐     ┌──────────────┐     ┌──────────────┐
│ GitHub Watcher   │────▶│ Job Parser   │────▶│ Job Queue    │
│ (github_watcher) │     │ (job_parser) │     │ (SQLite)     │
└─────────────────┘     └──────────────┘     └──────┬───────┘
                                                     │
                                              ┌──────▼───────┐
                                              │ ATS Router   │
                                              │ (main.py)    │
                                              └──────┬───────┘
                                                     │
                    ┌────────────┬────────────┬───────┴───────┬────────────┐
                    ▼            ▼            ▼               ▼            ▼
             ┌───────────┐┌───────────┐┌───────────┐  ┌───────────┐┌───────────┐
             │Greenhouse ││  Lever    ││SmartRecr. │  │  Ashby    ││ Workday   │
             │ Handler   ││ Handler   ││ Handler   │  │ Handler   ││ Handler   │
             └─────┬─────┘└─────┬─────┘└─────┬─────┘  └─────┬─────┘└─────┬─────┘
                   │            │            │               │            │
                   └────────────┴────────────┴───────┬───────┴────────────┘
                                                     │
                                              ┌──────▼───────┐
                                              │ Form Filler  │
                                              │ + AI Answerer│
                                              └──────┬───────┘
                                                     │
                                              ┌──────▼───────┐
                                              │  Submit +    │
                                              │  Screenshot  │
                                              └──────────────┘
```

---

## PART B: File-by-File Breakdown

### Core Files

| File | Lines | Purpose |
|------|-------|---------|
| `src/main.py` | 1,161 | CLI entrypoint, orchestrator, validation, job routing |
| `src/form_filler.py` | 2,572 | Universal form filling — detects fields, types, fills values |
| `src/ai_answerer.py` | 2,087 | 730+ regex patterns, Gemini AI fallback, answer caching |
| `src/browser_manager.py` | 263 | Playwright browser + stealth anti-detection |
| `src/captcha_solver.py` | 336 | reCAPTCHA/invisible CAPTCHA via 2captcha API |
| `src/job_queue.py` | 391 | SQLite job queue with status lifecycle |
| `src/job_parser.py` | 419 | Parse SimplifyJobs README markdown into Job objects |
| `src/github_watcher.py` | 170 | Poll GitHub API for new commits/changes |
| `src/application_tracker.py` | 201 | Session tracking, reports, JSONL logging |
| `src/question_verifier.py` | 270 | Human-in-the-loop answer verification |

### ATS Handlers

| File | Lines | ATS | Status |
|------|-------|-----|--------|
| `src/handlers/base.py` | 430 | Base class | All handlers inherit from this |
| `src/handlers/greenhouse.py` | 3,992 | Greenhouse | Production — 95% success |
| `src/handlers/smartrecruiters.py` | 863 | SmartRecruiters | Production — 85% (nodriver bypass) |
| `src/handlers/ashby.py` | 721 | Ashby | Production — 95% (API-first) |
| `src/handlers/lever.py` | 662 | Lever | Production — 90% |
| `src/handlers/workday.py` | 479 | Workday | Partial — login walls block most |
| `src/handlers/icims.py` | 1,089 | iCIMS | Skipped — always requires login |
| `src/handlers/generic.py` | 404 | Unknown ATS | Basic — 30% success rate |
| `src/handlers/__init__.py` | 21 | Exports | Re-exports all handlers |

### Data Files

| File | Format | Purpose |
|------|--------|---------|
| `data/jobs.db` | SQLite | All jobs: URL, company, ATS type, status, attempts |
| `data/verified_answers.db` | SQLite | Human-verified answers + review queue |
| `data/answer_cache.json` | JSON | Cached AI answers for instant reuse |
| `data/question_knowledge_base.md` | Markdown | Every question ever seen + answer + source |
| `data/gemini_cost_tracker.json` | JSON | Backup Gemini key spend tracking |
| `data/screenshots/` | PNG | Post-submit screenshots (PASS/FAIL naming) |

### Config Files

| File | Purpose | Committed? |
|------|---------|------------|
| `config/master_config.yaml` | Personal info, 730+ answer patterns | Yes |
| `config/secrets.yaml` | API keys (Gemini, 2captcha) | **NEVER** |

### Logs

| File | Purpose |
|------|---------|
| `logs/applier.log` | Debug log (rotates 10MB, 7-day retention) |
| `logs/running_application_log.jsonl` | Real-time application records |
| `logs/application_report_*.json` | Session summary reports |

---

## PART C: Data Flow

### 1. Job Discovery Flow

```
GitHub API (SimplifyJobs repo)
  │
  ▼
github_watcher.py — polls for new commits
  │
  ▼
job_parser.py — parses README markdown table
  │ Extracts: company, role, URL, locations, date
  │ Detects ATS type from URL pattern
  ▼
job_queue.py — INSERT INTO jobs (status='pending')
  │ Deduplicates by URL
  ▼
data/jobs.db — persistent storage
```

### 2. Application Flow

```
main.py backfill --max 10
  │
  ▼
SELECT jobs WHERE status='pending' ORDER BY date LIMIT 10
  │
  ▼
For each job:
  ├── Set status = 'in_progress'
  ├── Detect ATS type from URL
  ├── Route to correct handler
  │     │
  │     ▼
  │   handler.apply(url)
  │     ├── Navigate to URL
  │     ├── Check for "job closed" (20+ text patterns)
  │     ├── Check for login wall → skip
  │     ├── Fill personal info fields
  │     ├── Upload resume (set_input_files)
  │     ├── Answer custom questions (AI answerer chain)
  │     ├── Handle multi-page forms (next buttons)
  │     ├── Solve CAPTCHA if present
  │     ├── Submit (or pause if --dry-run / --review)
  │     └── Take screenshot → data/screenshots/
  │
  ├── On success: status = 'applied'
  ├── On failure: status = 'failed', increment attempts
  └── On skip:    status = 'skipped' (closed/login/captcha)
```

### 3. Question Answering Chain

```
Question detected on form
  │
  ▼
1. Config patterns (730+ regex)           ─── confidence: 100%
  │ FREE, instant, no API call
  │ no match ──▶
  ▼
2. Option matcher (dropdowns)             ─── confidence: 100%
  │ Maps config values to dropdown options
  │ no match ──▶
  ▼
3. Verified answers DB                    ─── confidence: 100%
  │ Human-approved answers from review
  │ no match ──▶
  ▼
4. Answer cache (JSON)                    ─── confidence: 90%
  │ Previously generated AI answers
  │ no cache hit ──▶
  ▼
5. Primary Gemini API (free tier)         ─── confidence: 80%
  │ gemini-2.0-flash model
  │ 429/quota ──▶
  ▼
6. Backup Gemini API (GCP $300 credit)    ─── confidence: 80%
  │ Same model, paid key
  │ also fails ──▶
  ▼
7. Generic fallback                       ─── confidence: 0%
  │ Template-based, low quality
  │ QUEUED FOR HUMAN REVIEW
  ▼
All paths → log to question_knowledge_base.md
All paths → track in session_answers
```

---

## PART D: ATS Routing Logic

### URL Pattern Detection

```python
# In job_parser.py — ATS detection from URL
"greenhouse.io"      → ATSType.GREENHOUSE
"lever.co"           → ATSType.LEVER
"smartrecruiters.com"→ ATSType.SMARTRECRUITERS
"ashbyhq.com"        → ATSType.ASHBY
"myworkdayjobs.com"  → ATSType.WORKDAY
"icims.com"          → ATSType.ICIMS
everything else      → ATSType.UNKNOWN → generic handler
```

### Handler Initialization

```python
# In main.py — handler registry
handlers = {
    ATSType.GREENHOUSE:      GreenhouseHandler(browser, ai, config),
    ATSType.LEVER:           LeverHandler(browser, ai, config),
    ATSType.SMARTRECRUITERS: SmartRecruitersHandler(browser, ai, config),
    ATSType.ASHBY:           AshbyHandler(browser, ai, config),
    ATSType.WORKDAY:         WorkdayHandler(browser, ai, config),
    ATSType.ICIMS:           ICIMSHandler(browser, ai, config),
    ATSType.UNKNOWN:         GenericHandler(browser, ai, config),
}
```

---

## PART E: Rate Limiting & Safety

### Rate Controls

| Parameter | Default | Config Key |
|-----------|---------|------------|
| Max apps/hour | 10 | `preferences.max_applications_per_hour` |
| Delay between apps | 30s | `preferences.delay_between_applications_seconds` |
| Action delay | 500-2000ms | Human-like randomized |
| Typing delay | 50-150ms | Per-keystroke randomized |
| AI call timeout | 15s | Hardcoded |
| Max retries per job | 3 | Hardcoded |

### Anti-Detection

- Playwright with stealth mode (browser_manager.py)
- nodriver for DataDome bypass (SmartRecruiters)
- Randomized delays between actions
- Human-like typing speed
- Cookie consent banner auto-dismiss
- Real browser fingerprint (not headless by default)

### Budget Controls

```
Primary Gemini key:  FREE tier (quota-limited)
Backup Gemini key:   GCP $300 credit
  └── Hard cap checked before every call
  └── Tracked in data/gemini_cost_tracker.json
  └── Auto-failover only when primary returns 429
```

### Job Status Lifecycle

```
pending ──▶ in_progress ──▶ applied    (success)
                       └──▶ failed     (retry up to 3x)
                       └──▶ skipped    (closed/login/captcha)
```
