# Internship Auto-Applier — System Guide

## How It Works (Simple Version)

```
GitHub Repo (SimplifyJobs) → Bot finds new jobs → Queues them in SQLite
                                                         ↓
                              For each job: Open browser → Fill form → Submit → Screenshot
```

1. **Job Discovery**: Polls [SimplifyJobs/Summer2026-Internships](https://github.com/SimplifyJobs/Summer2026-Internships) for new postings
2. **Job Queue**: Stores all jobs in `data/jobs.db` (SQLite) with status tracking
3. **ATS Detection**: Identifies which system the job uses (Greenhouse, Lever, etc.)
4. **Form Filling**: Uses your config (`config/master_config.yaml`) to fill every field
5. **Question Answering**: 183 regex patterns → answer cache → Gemini AI → fallback
6. **Submit**: Clicks submit, takes screenshot, logs everything

---

## Commands

```bash
# TWO MODES:

# Mode 1: FULL AUTO — bot fills AND submits
python src/main.py backfill --max 10

# Mode 2: REVIEW — bot fills, YOU verify in browser, YOU press Enter to submit
python src/main.py backfill --max 10 --review

# Single job (auto submit):
python src/main.py apply "https://boards.greenhouse.io/company/jobs/12345"

# Single job (you review + submit):
python src/main.py apply "https://boards.greenhouse.io/company/jobs/12345" --review

# Dry run (fills but NEVER submits, for testing):
python src/main.py backfill --max 5 --dry-run

# Fetch new jobs from GitHub:
python src/main.py fetch

# View stats:
python src/main.py stats
```

---

## Your Config File

Everything about YOU lives in `config/master_config.yaml`. Edit this file to change any answer.

| Section | What It Controls |
|---|---|
| `personal_info` | Name, email, phone, address, LinkedIn, GitHub |
| `education` | School, degree, GPA, graduation date, coursework |
| `work_authorization` | US citizen, sponsorship, visa status |
| `demographics` | Gender (Male), Ethnicity (Asian), Veteran (No), Disability (decline) |
| `availability` | Start date, relocation, remote preference |
| `skills` | Languages, frameworks, tools, years of experience |
| `experience` | Work history (Kruiz, SJSU, SCE Club) |
| `projects` | Scorecard, Canvas Autograder, Wrapify |
| `common_answers` | Essay templates (why interested, strengths, tell me about yourself) |
| `screening` | Background check, drug test, 18+, criminal record, etc. |
| `files` | Resume path (`config/resume.pdf`) |

---

## Question Answering — How It Decides What to Fill

```
Question arrives (e.g., "Are you authorized to work in the US?")
  ↓
Step 1: Check 183 regex patterns → "Yes" (FREE, instant)
  ↓ no match
Step 2: Check answer cache (previously answered) → instant
  ↓ no cache
Step 3: Call Gemini AI → smart answer (needs API key)
  ↓ Gemini fails/unavailable
Step 4: Generic fallback → "I'm a motivated student..." (BAD quality)
```

**94% of questions are answered by patterns (Step 1) — no AI needed.**

Only company-specific questions like "Why do you want to work at Samsung?" need Gemini.

---

## What Each ATS Template Asks

### Greenhouse (205 pending) — ~95% success rate
- First/Last name, Email, Phone, Country, Location
- Resume upload
- School, Degree, Discipline, GPA
- LinkedIn, GitHub, Website
- Work authorization (authorized? sponsorship?)
- How did you hear about this job?
- EEO: Gender, Race/Ethnicity, Veteran, Disability
- **Custom questions vary by company** (why interested, experience, etc.)

### Lever (61 pending) — ~90% success rate
- Name, Email, Phone, Location, Current company
- Resume upload
- LinkedIn, GitHub, Portfolio, Twitter/X URL
- Acknowledgment checkboxes
- School, Degree level, Major
- Work authorization, Visa status
- Custom questions + EEO

### Ashby (68 pending) — ~95% success rate
- Name, Email, Phone, Location
- Resume upload
- LinkedIn, How did you hear?
- Work authorization
- Custom questions (varies)
- Uses API-first approach (faster, more reliable)

### SmartRecruiters (95 pending) — ~85% success rate
- Name, Email, Phone, Address, City, State, Zip
- Resume upload
- Work authorization
- Custom questions
- Uses nodriver to bypass DataDome CAPTCHA

### iCIMS (85 pending) — BLOCKED by hCaptcha
- Email entry → hCaptcha challenge → Contact info → Documents → EEO → Submit
- **Needs CAPTCHA solver to work**

### Workday (1,209 pending) — BLOCKED by login
- Requires account creation (can't automate)
- Would need email verification + account setup

---

## Logging & Screenshots

Every application saves to organized folders:

```
data/applications/
├── successful/
│   └── CompanyName_Role_20260216_012345/
│       ├── summary.json    ← everything: fields filled, questions answered, timing
│       └── screenshot.png  ← final form state
├── failed/
│   └── CompanyName_Role_20260216_012345/
│       ├── summary.json    ← error message, what went wrong
│       └── screenshot.png
└── skipped/
    └── CompanyName_Role_20260216_012345/
        ├── summary.json    ← reason: closed, login required, captcha
        └── screenshot.png
```

Also:
- `data/screenshots/` — flat folder with all screenshots (PASS_*.png / FAIL_*.png)
- `logs/applier.log` — detailed debug log
- `data/question_knowledge_base.md` — every question ever seen + answer used

---

## Current Numbers

| Status | Count |
|---|---|
| Already applied | 89 |
| **Ready to apply** (Greenhouse, Lever, Ashby, SmartRecruiters) | **429** |
| Maybe (unknown ATS) | 702 |
| Blocked (Workday login, iCIMS CAPTCHA) | 1,294 |
| **Total pending** | **2,425** |

---

## What's Needed for 100%

| Item | Status | How to Fix |
|---|---|---|
| Personal info & config | DONE | All filled in `master_config.yaml` |
| Resume | DONE | `config/resume.pdf` exists |
| Pattern matching (183 rules) | DONE | Covers 94% of questions |
| Organized logging | DONE | successful/failed/skipped folders |
| Screenshots | DONE | Every submission |
| Review mode (you verify) | DONE | Use `--review` flag |
| **Gemini AI key** | **NEED** | Enable GCP billing → get API key |
| **CAPTCHA solver** | **OPTIONAL** | 2captcha.com key (~$3) for iCIMS |
| **Workday login** | **UNSOLVABLE** | Requires manual account creation |

---

## Monitoring a Run

```bash
# Watch live progress
tail -f logs/applier.log | grep -E "PASS|FAIL|CLOSED|PROGRESS"

# Check successful applications
ls data/applications/successful/

# Check failures
ls data/applications/failed/

# Check screenshots
open data/screenshots/

# View database stats
python src/main.py stats

# Check which questions got bad answers
grep "generic_fallback" data/question_knowledge_base.md
```

---

## Safety

- **Rate limited**: 10 applications/hour, 30s between jobs
- **Human-like delays**: 500-2000ms between actions, 50-150ms typing
- **Never commits secrets**: `config/secrets.yaml` is in `.gitignore`
- **Gemini budget cap**: $300 hard limit on backup key
- **Review mode available**: Use `--review` to verify before submitting
