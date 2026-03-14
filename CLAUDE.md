# Internship Auto-Applier Bot

## CRITICAL RULES — READ FIRST

1. **NEVER submit an application without a post-submit screenshot** saved to `data/screenshots/`
2. **NEVER guess answers** — if a question isn't in config or cache, use Gemini. If Gemini fails, use generic fallback AND log to `data/question_knowledge_base.md`
3. **NEVER commit secrets** — `config/secrets.yaml` must stay in `.gitignore`
4. **ALWAYS use primary Gemini key first** — only failover to backup when primary returns 429/quota error
5. **ALWAYS track backup key spend** — hard cap at $300. Check `data/gemini_cost_tracker.json` before every session
6. **ALWAYS log every question encountered** — no exceptions, even if answered from config

## Gemini API Key Strategy

```
PRIORITY ORDER:
1. Config patterns (730+ regex) → FREE, instant, no API call
2. Answer cache (data/answer_cache.json) → FREE, instant, previously answered
3. Primary Gemini key (free tier) → FREE but quota-limited
4. Backup Gemini key (GCP $300 credit) → PAID, $300 hard cap
5. Generic fallback → FREE but BAD quality, last resort

KEYS:
- Primary: config/secrets.yaml → gemini_api_key (free tier, use first)
- Backup: config/secrets.yaml → gemini_backup_api_key (GCP billing, $300 cap)
- Cost tracking: data/gemini_cost_tracker.json (auto-updated every AI call)
- Budget cap: config/secrets.yaml → gemini_backup_budget_cap (default: 300.00)

FAILOVER:
- Primary 429/quota → auto-switch to backup key (logged)
- Backup budget exceeded → fallback to generic (logged as warning)
- Both exhausted → generic_fallback only (review data/question_knowledge_base.md for bad answers)
```

## Project Overview

Automated job application system that monitors SimplifyJobs/Summer2026-Internships for new postings and auto-applies using pre-filled profile info.

## Architecture

```
GitHub Watcher → Job Parser → Job Queue (SQLite) → ATS Router → Handler → Form Filler + AI Answerer → Submit → Screenshot + Log
```

### Files

| File | Lines | Purpose | Status |
|------|-------|---------|--------|
| `src/main.py` | 1,023 | Orchestrator, CLI, validation | Production |
| `src/form_filler.py` | 2,513 | Universal form filling | Production |
| `src/ai_answerer.py` | 1,570 | Question answering + Gemini | Production |
| `src/browser_manager.py` | 251 | Playwright + stealth | Production |
| `src/captcha_solver.py` | 337 | reCAPTCHA via 2captcha | Production |
| `src/job_queue.py` | 365 | SQLite job queue | Production |
| `src/job_parser.py` | 420 | Parse SimplifyJobs README | Production |
| `src/github_watcher.py` | 171 | Poll GitHub for new jobs | Production |
| `src/application_tracker.py` | 202 | Session tracking + reports | Production |
| `src/handlers/greenhouse.py` | 3,500+ | Greenhouse ATS | Production |
| `src/handlers/lever.py` | 700+ | Lever ATS | Production |
| `src/handlers/smartrecruiters.py` | 1,000+ | SmartRecruiters (nodriver) | Production |
| `src/handlers/ashby.py` | 800+ | Ashby ATS (API-first) | Production |
| `src/handlers/workday.py` | 500+ | Workday ATS | Partial (login) |
| `src/handlers/generic.py` | 400+ | Fallback handler | Basic |
| `src/handlers/base.py` | 391 | Base handler class | Production |
| `src/email_response_tracker.py` | 450 | Gmail response scanner + categorizer | Production |

### Data Files

| File | Purpose |
|------|---------|
| `data/jobs.db` | SQLite — all jobs, statuses, attempts |
| `data/question_knowledge_base.md` | Every question ever seen + answer used |
| `data/answer_cache.json` | Cached AI answers for instant reuse |
| `data/gemini_cost_tracker.json` | Backup key spend tracking |
| `data/screenshots/` | Post-submit screenshots (PASS/FAIL) |
| `logs/applier.log` | Debug log (rotates 10MB, 7-day retention) |
| `logs/running_application_log.jsonl` | Real-time application records |
| `logs/application_report_*.json` | Session summary reports |
| `config/master_config.yaml` | All personal info + answer templates |
| `config/secrets.yaml` | API keys — NEVER COMMIT |
| `data/response_summary.json` | Email response scan results |

## ATS Support Matrix

| ATS | Handler | Pending | Success Rate | Login? | CAPTCHA |
|-----|---------|---------|-------------|--------|---------|
| Greenhouse | greenhouse.py | 158 | ~95% | No | Invisible reCAPTCHA (solved) |
| SmartRecruiters | smartrecruiters.py | 95 | ~85% | No | DataDome (bypassed via nodriver) |
| Ashby | ashby.py | 59 | ~95% | No | None |
| Lever | lever.py | 22 | ~90% | No | None |
| Workday | workday.py | 1,203 | ~20% | YES (most) | Varies |
| iCIMS | SKIPPED | 85 | 0% | YES (always) | N/A |
| Unknown | generic.py | 701 | ~30% | Varies | Varies |

## Question Answering Flow (Golden Path)

```
Question detected by handler
  ↓
0. Is it required? → skip optional fields, skip social media (Facebook/Twitter/Instagram)
   Exception: keep LinkedIn and GitHub (always fill from config)
  ↓ required
1. Check TEMPLATE BANK (config/question_banks/{ats_type}.yaml) — 95% hit rate
  ↓ no match
2. Check option matching for dropdowns (_match_option_from_config)
  ↓ no match
3. Check _get_config_answer() — 730+ regex patterns
  ↓ no match
4. Check answer cache (data/answer_cache.json)
  ↓ no cache hit
5. Call Gemini AI (primary key first, backup failover on 429/quota)
  ↓ AI fails
6. Generic fallback (only if confidence >= 85%)
  ↓ still no answer
7. Leave field empty → DON'T SUBMIT → leave tab open for manual intervention

ALL PATHS → log to data/question_knowledge_base.md
ALL PATHS → track in session_answers for reporting
```

## Job Status Lifecycle

```
pending → in_progress → applied (success)
                      → failed (retry up to 3x)
                      → skipped (closed/login/captcha)

Applied jobs get response_status from email tracking:
applied → follow_up → assessment → interview_invite → offer
                                                     → rejection
(only upgrades — offer > interview > assessment > follow_up > rejection)
```

## Rate Limiting

- 10 applications/hour (configurable: `preferences.max_applications_per_hour`)
- 30s delay between jobs (configurable: `preferences.delay_between_applications_seconds`)
- Human-like delays: 500-2000ms between actions, 50-150ms typing

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Backfill — apply to pending jobs
python src/main.py backfill --max 10

# Dry run — fill forms but don't submit
python src/main.py backfill --dry-run --max 5

# Single URL
python src/main.py apply --url "https://boards.greenhouse.io/..."

# Fetch new jobs from GitHub
python src/main.py fetch

# View stats
python src/main.py stats

# Export to CSV
python src/main.py export applications.csv

# Reset failed jobs for retry
python src/main.py reset_failed

# Check email responses to applications (on-demand)
python src/main.py check-responses --days 30
python src/main.py check-responses --days 14 --category interview_invite

# Continuously monitor email responses
python src/main.py track --interval 48 --days 7
```

## Run Modes (flags for backfill and apply)

| Flag | What it does | When to use |
|------|-------------|-------------|
| `--smart` | Gemini scans form after handler fails, fills missed fields via AI (DOM + vision) | Always-on for better success rate, costs ~$0.0004/job |
| `--assist` | Bot fills what it can → **browser stays open** → shows missing fields → YOU fix manually → press Enter → bot submits + screenshots | For rescuing failed jobs that need 1-2 manual fields |
| `--review` | Bot fills everything → pauses before submit → YOU inspect → press Enter to submit | When you want to double-check before submit |
| ~~`--with-simplify`~~ | **DEPRECATED** — Simplify Copilot is now ALWAYS loaded automatically | N/A |
| `--workday-accounts` | Only apply to Workday tenants with saved accounts, slow mode (90s gaps, 4/hr) | For Workday batch runs |
| `--dry-run` | Fill forms but never click submit | Testing |

### Browser Behavior Rules

- **Simplify is always loaded** — the `--with-simplify` flag is deprecated; Simplify fills first on every page
- **Browser never closes automatically** — user presses Enter to close
- **Tabs only close on success** — 10-second wait, screenshot, then close
- **Failures leave tab open** — for manual inspection or assist mode
- **Social media fields are always skipped** — Facebook, Twitter, Instagram
- **Optional fields are skipped** — except LinkedIn and GitHub (always filled from config)

### Dedicated Commands

```bash
# ASSIST MODE — retry ONLY failed jobs with human help
# Bot fills → browser stays open → you fix remaining fields → Enter → bot submits
python src/main.py assist                          # all failed jobs
python src/main.py assist --ats greenhouse         # only Greenhouse failures
python src/main.py assist --max 5                  # limit to 5 jobs

# SMART + ASSIST combo (recommended for rescuing failures)
python src/main.py backfill --smart --assist --max 10

# Workday accounts batch
python src/main.py backfill --workday-accounts --max 20
```

### Assist Mode Flow
```
1. Bot picks a failed job, opens browser, navigates to form
2. Handler fills everything it can (name, email, resume, etc.)
3. If fields still empty → Gemini scanner tries to fill them
4. If STILL empty → browser stays open, terminal shows:
     ASSIST MODE — Company - Role
     Empty required fields (2):
       - What location? (select)
       - How long internship? (checkbox)
     [Enter] = Submit  [s] = Skip  [d] = Already submitted manually
5. YOU fill the remaining 1-2 fields in the browser
6. Press Enter → bot clicks Submit → takes screenshot → marks applied
```

## Pre-Run Checklist

Before running `python src/main.py backfill`:

- [ ] `config/master_config.yaml` — all personal info filled (no placeholders)
- [ ] `config/secrets.yaml` — primary Gemini key set
- [ ] `config/secrets.yaml` — backup Gemini key set (GCP $300 credit)
- [ ] `config/secrets.yaml` — CAPTCHA solver configured (2captcha/anticaptcha)
- [ ] `resume.pdf` exists at configured path
- [ ] Review `data/question_knowledge_base.md` — fix any bad generic_fallback answers
- [ ] Check `data/gemini_cost_tracker.json` — backup spend under $300

## Edge Cases Handled

- Job closed/removed (20+ text patterns)
- Login walls (Workday) → skip
- Invisible reCAPTCHA (Greenhouse) → solve via 2captcha
- DataDome (SmartRecruiters) → bypass via nodriver
- Cookie consent banners → auto-dismiss
- Multi-page forms (Greenhouse, Workday) → navigate
- Embedded iframe forms → detect and switch
- React form validation → hidden input sync
- Resume upload → set_input_files()
- Retry on failure → up to 3 attempts
- Duplicate jobs → URL uniqueness in SQLite

## Edge Cases NOT Handled

- "Already applied" detection
- Account creation (Workday, iCIMS)
- OAuth / LinkedIn Apply
- Custom date pickers
- hCaptcha solving
- Cloudflare Turnstile
- Conditional fields (show/hide)
- Video/audio response questions

## Known Failure Patterns

| Pattern | Cause | Fix |
|---------|-------|-----|
| "Flagged as spam" | Anti-bot detection | Increase delays, use proxy |
| Missing required fields | Dropdown not matched | Add to config patterns |
| Education fields missed | School/degree is dropdown not text | Handler-specific fix |
| "preferred_name" not filled | Not in config | Add if needed |
| Location typeahead fails | Can't select from autocomplete | Needs typeahead support |

## Monitoring During a Run

```bash
# Watch live progress
tail -f logs/applier.log | grep -E "PROGRESS|PASS|FAIL|WARNING"

# Check screenshots
ls -la data/screenshots/

# Check question KB for bad answers
grep "generic_fallback" data/question_knowledge_base.md

# Check backup key spend
cat data/gemini_cost_tracker.json

# Check database stats
python src/main.py stats
```

## Safety

1. **Account Risk** — job sites may ban for automation. Use rate limiting.
2. **Rate Limits** — 10/hour default. Don't increase without proxy.
3. **Review Applications** — check `data/screenshots/` and `logs/running_application_log.jsonl`
4. **Secrets** — never commit `config/secrets.yaml`
5. **Budget** — backup Gemini key hard-capped at $300. Check `data/gemini_cost_tracker.json`
