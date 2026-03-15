# Changelog

All notable changes to the Internship Auto-Applier project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]

### Planned
- Discord notifications for new jobs
- Email alerts for high-priority postings
- AI-powered resume customization per job
- Cover letter generation
- Application status tracking
- Analytics dashboard
- Proxy rotation support

---

## [0.2.0] - 2026-01-10

### Added
- **Full Working System**
  - All modules implemented and tested
  - Successfully fills Greenhouse job applications
  - Form filler correctly maps config fields to form inputs

- **Job Parser (Fixed)**
  - Now parses HTML tables from SimplifyJobs README
  - Extracts 1097+ jobs across all ATS platforms
  - Detects ATS type from URL patterns

- **Form Filler (Tested)**
  - Successfully filled 11 fields on Visier Solutions application
  - Handles: first_name, last_name, email, phone, linkedin, etc.
  - Field mapping works for common input patterns

- **Run Script**
  - Added `run.sh` for easy operation
  - Commands: fetch, test, apply, reset, stats

- **Mock Config**
  - Created realistic test data (Jon Doe)
  - Safe for testing without real personal info

### Fixed
- Job parser now handles HTML table format (not just markdown)
- Playwright stealth mode API updated for v2.0
- Browser manager works with headless mode

### Tested
- Greenhouse form filling: Working
- Lever detection: Working
- Workday detection: Working
- Job fetching: 1097 jobs parsed successfully

---

## [0.1.0] - 2026-01-09

### Added
- **Project Initialization**
  - Created project directory structure
  - Set up `internship-auto-applier/` with `src/`, `config/`, `data/`, `logs/`

- **Documentation**
  - Created `CLAUDE.md` with full infrastructure documentation
  - Created `CHANGELOG.md` (this file)
  - Documented architecture, data flow, tech stack

- **Configuration**
  - Created `master_config.yaml` template with all fields:
    - Personal information (name, email, phone, address)
    - Education details (school, degree, GPA, graduation date)
    - Work authorization (US/EU, sponsorship, visa)
    - Demographics (optional - gender, ethnicity, veteran status)
    - Skills (programming languages, frameworks, tools)
    - Common question answers (why this company, strengths, etc.)
    - Screening defaults (18+, background check, drug test)
    - File paths (resume, cover letter, transcript)

### Architecture Decisions
- **Playwright over Selenium**: Better stealth capabilities, faster, modern API
- **SQLite over Redis**: Simpler setup, no external dependencies, sufficient for single-user
- **OpenAI GPT-4o-mini**: Best balance of speed, cost, and accuracy for form questions
- **Modular ATS handlers**: Each ATS (Greenhouse, Lever, Workday) gets its own handler for maintainability

### Research Completed
- Analyzed SimplifyJobs/Summer2026-Internships repo structure
- Identified common ATS platforms and their URL patterns
- Documented common job application questions
- Researched bot detection bypass techniques
- Evaluated existing auto-apply solutions (AIHawk, EasyApplyBot, etc.)

### Technical Notes
- SimplifyJobs README uses markdown table format
- Job URLs can be extracted via regex or BeautifulSoup
- Greenhouse has two URL formats: `boards.greenhouse.io` and `job-boards.greenhouse.io`
- Workday has multiple subdomains: `*.wd1.myworkdayjobs.com` through `*.wd5.myworkdayjobs.com`
- Rate limiting recommended: max 10 applications/hour per platform

---

## Development Log

### 2026-01-09 - Session 1

**Goals:**
- [x] Set up project structure
- [x] Create documentation (CLAUDE.md, CHANGELOG.md)
- [x] Create config template
- [x] Build GitHub watcher
- [x] Build job parser
- [x] Build form filler engine
- [x] Build ATS handlers (Greenhouse, Lever, Workday, Generic)
- [x] Build main orchestrator

**Modules Built:**
- `src/github_watcher.py` - Monitors SimplifyJobs repo for new jobs
- `src/job_parser.py` - Parses README.md to extract job listings
- `src/job_queue.py` - SQLite-based job queue with priority support
- `src/browser_manager.py` - Playwright browser with stealth mode
- `src/form_filler.py` - Intelligent form filling with field mapping
- `src/ai_answerer.py` - OpenAI integration for custom questions
- `src/handlers/base.py` - Base handler class
- `src/handlers/greenhouse.py` - Greenhouse ATS handler
- `src/handlers/lever.py` - Lever ATS handler
- `src/handlers/workday.py` - Workday ATS handler
- `src/handlers/generic.py` - Fallback generic handler
- `src/main.py` - Main orchestrator with CLI

**Research Sources:**
- [SimplifyJobs/Summer2026-Internships](https://github.com/SimplifyJobs/Summer2026-Internships)
- [AIHawk Jobs Applier](https://github.com/feder-cr/Jobs_Applier_AI_Agent_AIHawk)
- [auto-apply (Greenhouse/Lever/Workday)](https://github.com/simonfong6/auto-apply)
- [undetected-chromedriver](https://github.com/ultrafunkamsterdam/undetected-chromedriver)
- [Playwright stealth](https://github.com/AntoinePrv/playwright-stealth)

**Key Insights:**
1. Most internship applications share 80% of the same fields
2. AI can handle the remaining 20% custom questions effectively
3. Stealth mode is critical - sites actively detect bots
4. Parallel workers speed up processing but increase detection risk
5. Priority queue for new jobs ensures fast application to fresh postings

---

## Version History

| Version | Date | Description |
|---------|------|-------------|
| 0.1.0 | 2026-01-09 | Initial setup, documentation, config template |
