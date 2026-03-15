# Quick Reference Cheat Sheet

## Common Commands

```bash
# Apply to 10 pending jobs
python src/main.py backfill --max 10

# Apply to specific ATS only
python src/main.py backfill --ats greenhouse --max 5

# Dry run (fill but don't submit)
python src/main.py backfill --dry-run --max 5

# Single URL
python src/main.py apply --url "https://boards.greenhouse.io/..."

# Rescue failed jobs with manual help
python src/main.py assist --max 5

# Smart + Assist combo (recommended for failures)
python src/main.py backfill --smart --assist --max 10

# Fetch new jobs from GitHub
python src/main.py fetch

# View stats
python src/main.py stats

# Reset failed jobs for retry
python src/main.py reset_failed
```

## Golden Path (10 bullets)

1. Job picked from SQLite queue (`data/jobs.db`), routed to ATS handler
2. Simplify extension fills boilerplate first (name, email, phone) -- always loaded
3. Handler encounters a question field
4. Skip if optional (except LinkedIn/GitHub). Skip social media always.
5. Look up answer in **template bank** (`config/question_banks/{ats}.yaml`) -- 95% hit rate
6. If no template match: try option matching, then config regex (730+ patterns), then answer cache
7. If still no answer: call Gemini AI (primary key, then backup on quota error)
8. If AI fails: generic fallback only if confidence >= 85%. Otherwise leave empty.
9. If required fields are still empty: **don't submit** -- leave tab open for manual fix
10. On success: screenshot to `data/screenshots/`, wait 10s, close tab

## ATS Handlers

| ATS | Handler | Notes |
|-----|---------|-------|
| Greenhouse | `src/handlers/greenhouse.py` | Multi-page, React-Select dropdowns, reCAPTCHA solved via 2captcha |
| SmartRecruiters | `src/handlers/smartrecruiters.py` | Uses nodriver for DataDome bypass, Shadow DOM, Angular forms |
| Lever | `src/handlers/lever.py` | Standard forms, straightforward |
| Ashby | `src/handlers/ashby.py` | API-first (submits via API when possible), browser fallback |
| Workday | `src/handlers/workday.py` | Needs saved accounts, multi-page, conditional fields |

## Template Bank Files

```
config/question_banks/
  common.yaml          -- shared across all ATS types
  greenhouse.yaml      -- Greenhouse-specific Q&A
  smartrecruiters.yaml -- SmartRecruiters-specific Q&A
  lever.yaml           -- Lever-specific Q&A
  workday.yaml         -- Workday-specific Q&A
```

## Where to Check Things

| What | Where |
|------|-------|
| Live progress | `tail -f logs/applier.log \| grep -E "PROGRESS\|PASS\|FAIL"` |
| Screenshots | `data/screenshots/` |
| Every question seen | `data/question_knowledge_base.md` |
| Bad answers | `grep "generic_fallback" data/question_knowledge_base.md` |
| Cached AI answers | `data/answer_cache.json` |
| Gemini spend | `data/gemini_cost_tracker.json` |
| Job database | `data/jobs.db` (or `python src/main.py stats`) |
| Application log | `logs/running_application_log.jsonl` |
| Session reports | `logs/application_report_*.json` |
| Known issues | `docs/ISSUES.md` |

## Reset and Retry

```bash
# Reset all failed jobs back to pending
python src/main.py reset_failed

# Then re-run (fix bugs first!)
python src/main.py backfill --max 10
```

Rule: fix root causes BEFORE resetting. Same jobs will fail the same way on retry.
