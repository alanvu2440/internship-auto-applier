# Internship Auto-Applier

Automated job application bot that monitors [SimplifyJobs/Summer2026-Internships](https://github.com/SimplifyJobs/Summer2026-Internships) for new postings and auto-applies using your pre-filled profile.

**Applied to 273+ jobs in a single session.** Handles Greenhouse, SmartRecruiters, Ashby, Lever, and Workday — fills every form field, uploads resume, solves CAPTCHAs, and screenshots every submission.

---

## What it does

1. **Watches GitHub** — polls the SimplifyJobs internship list for new postings
2. **Parses job links** — identifies the ATS (Greenhouse, Lever, etc.) from the URL
3. **Fills every form field** — name, email, phone, education, work experience, resume upload, custom questions
4. **Answers AI questions** — uses Gemini to answer open-ended questions like "Why do you want to work here?"
5. **Solves CAPTCHAs** — via 2captcha for Greenhouse's invisible reCAPTCHA
6. **Screenshots every submission** — saved to `data/screenshots/` for proof
7. **Tracks everything** — SQLite database with every job, status, and timestamp

---

## Supported ATS Platforms

| ATS | Success Rate | Notes |
|-----|-------------|-------|
| Greenhouse | ~95% | Best support, CAPTCHA solved |
| Ashby | ~95% | API-first, very reliable |
| Lever | ~90% | Good support |
| SmartRecruiters | ~85% | Shadow DOM — works via nodriver |
| Workday | ~20% | Requires account creation per tenant |
| iCIMS | Skipped | Always requires login |

---

## Setup

### 1. Prerequisites

- Python 3.11+
- Google Chrome (for browser automation)
- A 2captcha account (~$3 deposit covers hundreds of Greenhouse jobs)
- A Gemini API key (free at Google AI Studio)

### 2. Clone & install

```bash
git clone https://github.com/alanvu2440/internship-auto-applier
cd internship-auto-applier

python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

pip install -r requirements.txt
playwright install chromium
```

### 3. Fill in your profile

```bash
cp config/mock_config.yaml config/master_config.yaml
```

Open `config/master_config.yaml` and replace every field with **your real info**. This is what gets auto-filled into every application form. Key sections:

- `personal_info` — name, email, phone, address, LinkedIn, GitHub
- `education` — school, degree, GPA, graduation date
- `experience` — your work history
- `projects` — 2-3 projects you want highlighted
- `work_authorization` — citizenship/visa status
- `skills` — programming languages, frameworks, tools
- `common_answers` — pre-written answers to "Why do you want to work here?", "Tell me about yourself", etc.
- `preferences.blacklist_companies` — companies to skip (FAANG, finance, etc.)

### 4. Set up API keys

```bash
cp config/secrets.template.yaml config/secrets.yaml
```

Open `config/secrets.yaml` and fill in:

```yaml
gemini_api_key: "AIza..."          # Free at aistudio.google.com
twocaptcha_api_key: "abc123..."    # From 2captcha.com (~$3 deposit)
```

**Get a free Gemini key:** https://aistudio.google.com/app/apikey

**Get 2captcha key:** https://2captcha.com — deposit $3, covers 1000+ CAPTCHAs

### 5. Add your resume

```bash
cp /path/to/your/resume.pdf config/resume.pdf
```

Optional (some jobs ask for these):
```bash
cp /path/to/cover_letter.pdf config/cover_letter.pdf
cp /path/to/transcript.pdf config/transcript.pdf
```

---

## Running the bot

### Fetch new jobs first

```bash
python src/main.py fetch
```

This pulls the latest jobs from SimplifyJobs and adds them to your local queue.

### Dry run (fills forms but doesn't submit)

Always do this first to make sure everything looks right:

```bash
python src/main.py backfill --dry-run --max 5
```

Watch the browser — it should fill in all your info. If something looks wrong, fix it in `config/master_config.yaml`.

### Apply for real

```bash
python src/main.py backfill --max 50
```

For best results (AI scans for missed fields):

```bash
python src/main.py backfill --smart --max 50
```

With Simplify Copilot extension (helps pre-fill basic fields):

```bash
python src/main.py backfill --smart --with-simplify --max 50
```

### Apply to a single job

```bash
python src/main.py apply --url "https://boards.greenhouse.io/company/jobs/12345"
```

### Check stats

```bash
python src/main.py stats
```

### Reset failed jobs for retry

```bash
python src/main.py reset_failed
```

---

## All CLI Commands

```bash
# Fetch new jobs from GitHub
python src/main.py fetch

# Apply to pending jobs in queue
python src/main.py backfill [flags]

# Apply to a single URL
python src/main.py apply --url "https://..."

# Dry run — fills forms but never submits
python src/main.py backfill --dry-run --max 5

# View database stats
python src/main.py stats

# Export all applications to CSV
python src/main.py export applications.csv

# Reset failed jobs for retry
python src/main.py reset_failed

# Check email responses (requires Gmail setup in secrets.yaml)
python src/main.py check-responses --days 30
```

## Backfill Flags

| Flag | What it does |
|------|-------------|
| `--max N` | Process at most N jobs |
| `--ats greenhouse` | Only apply to Greenhouse jobs |
| `--ats smartrecruiters` | Only apply to SmartRecruiters jobs |
| `--dry-run` | Fill forms but never click Submit |
| `--smart` | After filling, use Gemini vision to scan for any missed fields (~$0.0004/job) |
| `--assist` | Bot fills what it can, then pauses and lets you fix the rest manually before submitting |
| `--review` | Bot fills everything, pauses before submit so you can inspect |
| `--with-simplify` | Load Simplify Copilot Chrome extension for basic field autofill |

---

## Monitoring a run

```bash
# Watch live logs
tail -f logs/applier.log | grep -E "PASS|FAIL|PROGRESS|WARNING"

# Check screenshots (proof of submission)
open data/screenshots/

# Check for bad AI answers
grep "generic_fallback" data/question_knowledge_base.md

# Check Gemini API spend
cat data/gemini_cost_tracker.json
```

---

## Project Structure

```
internship-auto-applier/
├── config/
│   ├── master_config.yaml          # YOUR PROFILE — fill this in (gitignored)
│   ├── secrets.yaml                # API keys (gitignored)
│   ├── secrets.template.yaml       # Copy this to secrets.yaml
│   ├── mock_config.yaml            # Fake data for testing
│   └── resume.pdf                  # Your resume (gitignored)
├── src/
│   ├── main.py                     # CLI entry point & orchestrator
│   ├── form_filler.py              # Universal form filling (730+ patterns)
│   ├── ai_answerer.py              # Gemini-powered question answering
│   ├── browser_manager.py          # Playwright stealth browser
│   ├── captcha_solver.py           # 2captcha reCAPTCHA solver
│   ├── job_queue.py                # SQLite job queue
│   ├── job_parser.py               # Parse SimplifyJobs README
│   ├── github_watcher.py           # Poll GitHub for new jobs
│   ├── application_tracker.py      # Session stats & reporting
│   ├── email_response_tracker.py   # Scan Gmail for application responses
│   ├── gemini_form_scanner.py      # AI vision scanner for missed fields
│   └── handlers/
│       ├── base.py                 # Base handler class
│       ├── greenhouse.py           # Greenhouse ATS handler
│       ├── lever.py                # Lever ATS handler
│       ├── ashby.py                # Ashby ATS handler
│       ├── smartrecruiters.py      # SmartRecruiters handler (nodriver)
│       ├── workday.py              # Workday handler
│       └── generic.py              # Fallback handler
├── data/
│   ├── jobs.db                     # SQLite — all jobs & statuses (gitignored)
│   ├── question_knowledge_base.md  # Log of every question seen
│   └── answer_cache.json           # Cached AI answers (gitignored)
├── docs/
│   ├── ISSUES.md                   # Known issues & fixes
│   └── system_flowchart.md         # System architecture diagram
├── requirements.txt
├── ARCHITECTURE.md                 # Deep technical architecture docs
└── CLAUDE.md                       # AI assistant instructions
```

---

## How question answering works

When the bot encounters a question it can't answer from your config (e.g. "Describe a challenge you overcame"), it uses this priority chain:

```
1. Config patterns (730+ regex)     → FREE, instant
2. Answer cache                     → FREE, instant (previously answered)
3. Gemini API (primary key)         → FREE tier, daily quota
4. Gemini API (backup key)          → GCP $300 credit, hard-capped
5. Generic fallback                 → Last resort, lower quality
```

Every question and answer gets logged to `data/question_knowledge_base.md`. Review this file after your first run — if you see any bad generic_fallback answers, add the correct answer to `config/master_config.yaml` under `common_answers`.

---

## Cost

| Item | Cost |
|------|------|
| Bot itself | Free |
| Gemini API (primary) | Free (generous daily quota) |
| 2captcha (reCAPTCHA) | ~$3 per 1000 solves |
| Gemini backup key | Optional, uses GCP free credits |

For 200-300 applications, expect ~$2-5 in CAPTCHA costs total.

---

## Customizing which jobs to apply to

In `config/master_config.yaml`, the `preferences` section controls targeting:

```yaml
preferences:
  job_types:
    - "Internship"
    - "Co-op"
    - "New Grad"
  roles:
    - "Software Engineer"
    - "Data Engineer"
    - "Backend Engineer"
  blacklist_companies:
    - "Google"
    - "Amazon"
    # add any others you don't want
  max_applications_per_day: 200
  max_applications_per_hour: 60
  delay_between_applications_seconds: 30
```

---

## Safety & rate limiting

- Default: 60 applications/hour, 30s delay between jobs
- Don't increase limits without a proxy (risk of IP ban)
- The bot uses stealth mode (playwright-stealth) to avoid detection
- All submissions are screenshotted for your records
- Review `data/screenshots/` after each run

---

## Troubleshooting

**"No form fields found"** — The job posting is expired/closed or requires login. The bot will skip it automatically.

**reCAPTCHA failures** — Check your 2captcha balance: https://2captcha.com

**Gemini quota exceeded** — Either wait for quota reset (midnight PST) or add a backup key in `secrets.yaml`

**Fields not filling** — Check `data/question_knowledge_base.md` for fields that got `generic_fallback` answers. Add better answers to `common_answers` in your config.

**Workday jobs failing** — Workday requires creating an account per company. Use `--assist` mode to handle these manually:
```bash
python src/main.py backfill --ats workday --assist --max 10
```

**Bot running too slow** — Reduce `slow_mo` in config or increase `max_applications_per_hour`.

---

## Contributing / Extending

The ATS handlers are modular — each lives in `src/handlers/<ats_name>.py`. To add a new ATS:

1. Create `src/handlers/newats.py` extending `BaseHandler`
2. Implement `async def apply(self, page, url, job_data) -> bool`
3. Add the ATS type detection to `src/job_parser.py`
4. Register the handler in `src/main.py`

---

## Credits

- [SimplifyJobs/Summer2026-Internships](https://github.com/SimplifyJobs/Summer2026-Internships) — the job list we watch
- [Playwright](https://playwright.dev/) — browser automation
- [nodriver](https://github.com/ultrafunkamsterdam/nodriver) — Chrome CDP for DataDome bypass
- [playwright-stealth](https://github.com/AtuboDad/playwright_stealth) — bot detection evasion
- [2captcha](https://2captcha.com) — CAPTCHA solving service
- [Google Gemini](https://aistudio.google.com) — AI question answering
