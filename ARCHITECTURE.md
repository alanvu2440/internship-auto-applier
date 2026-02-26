# Internship Auto-Applier — Complete Architecture

> Last updated: 2026-02-16
> Codebase: ~14,800 lines of Python across 28 files
> Database: 2,535 jobs tracked, 99 applied, 100 failed, 2,320 pending

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Application Pipeline](#2-application-pipeline)
3. [ATS Templates — What Works](#3-ats-templates--what-works)
4. [Question Catalog — By Template](#4-question-catalog--by-template)
5. [Unanswered Questions — NEEDS YOUR INPUT](#5-unanswered-questions--needs-your-input)
6. [Edge Cases & How They're Handled](#6-edge-cases--how-theyre-handled)
7. [What Needs Gemini vs What Doesn't](#7-what-needs-gemini-vs-what-doesnt)
8. [Logging & Tracking](#8-logging--tracking)
9. [Rate Limiting & Stealth](#9-rate-limiting--stealth)
10. [Known Failures & Why](#10-known-failures--why)
11. [Config Checklist — Is Everything Filled?](#11-config-checklist--is-everything-filled)

---

## 1. System Overview

```
┌──────────────────────────────────────────────────────────────┐
│                      HOW IT WORKS                            │
│                                                              │
│  GitHub Watcher ──→ Job Parser ──→ Job Queue (SQLite)        │
│       │                               │                      │
│       │ polls every 5min              │ picks next job       │
│       │                               ▼                      │
│       │                        ATS Router                    │
│       │                     ┌─────┼─────┐                    │
│       │                     ▼     ▼     ▼                    │
│       │              Greenhouse Lever  Ashby  ...            │
│       │                     │     │     │                    │
│       │                     ▼     ▼     ▼                    │
│       │               Form Filler + AI Answerer              │
│       │                          │                           │
│       │                          ▼                           │
│       │                    Submit / Dry Run                   │
│       │                          │                           │
│       │                          ▼                           │
│       │              Screenshot + Log + Track                │
│       │                                                      │
│  Data: data/jobs.db              Logs: logs/applier.log      │
│        data/question_knowledge_base.md                       │
│        data/screenshots/*.png                                │
│        data/answer_cache.json                                │
│        logs/running_application_log.jsonl                    │
└──────────────────────────────────────────────────────────────┘
```

---

## 2. Application Pipeline

### Step-by-Step Flow

```
1. DISCOVER    github_watcher.py polls SimplifyJobs/Summer2026-Internships
               → Detects new commits → Fetches README.md

2. PARSE       job_parser.py extracts jobs from markdown/HTML table
               → Company, role, location, URL
               → Detects ATS type from URL pattern

3. QUEUE       job_queue.py adds to SQLite database
               → New jobs: priority=100 (apply first)
               → Backfill: priority=0 (apply slowly)
               → Deduplication by URL

4. ROUTE       main.py picks next job from queue
               → Identifies ATS handler (greenhouse, lever, ashby, etc.)
               → Opens browser with stealth mode

5. APPLY       Handler navigates to job URL
               → Clicks "Apply" button if needed
               → Fills form fields from config
               → Answers custom questions (config → cache → AI → fallback)
               → Uploads resume
               → Submits (or validates in dry-run mode)

6. CAPTURE     Screenshot taken (pass or fail)
               → Saved to data/screenshots/{STATUS}_{company}_{timestamp}.png
               → Questions logged to data/question_knowledge_base.md

7. TRACK       Result recorded
               → Success → status = "applied"
               → Failure → retry up to 3x, then "failed"
               → Job closed → "skipped"
               → Login required → "skipped"
```

### Job Status Lifecycle

```
pending ──→ in_progress ──→ applied     (SUCCESS)
                │
                ├──→ pending (retry)    (attempt < 3)
                ├──→ failed             (attempt >= 3)
                └──→ skipped            (closed / login / captcha)
```

---

## 3. ATS Templates — What Works

### Template 1: Greenhouse ✅ PRODUCTION READY
- **URLs:** `boards.greenhouse.io`, `job-boards.greenhouse.io`
- **Pending jobs:** 158
- **Success rate:** ~95% (dry run)
- **Login required:** No
- **CAPTCHA:** Invisible reCAPTCHA Enterprise (solved via 2captcha)
- **Form type:** Multi-page, React-based
- **What works:**
  - Listing page → Apply button click
  - Embedded iframe detection + switching
  - All standard fields (name, email, phone, location, LinkedIn, GitHub)
  - Education fields (school, degree, GPA, graduation date)
  - Resume + cover letter upload
  - Custom questions via AI answerer
  - React hidden input sync (question_XXXXXXXX fields)
  - Demographic/EEO questions
  - Multi-page navigation
- **What doesn't work:**
  - `preferred_name` field (can't fill — not in config)
  - `candidate-location` typeahead (can't select from dropdown)
  - Some education fields missed when form uses school/degree dropdowns instead of text

### Template 2: Lever ✅ PRODUCTION READY
- **URLs:** `jobs.lever.co`
- **Pending jobs:** 22
- **Success rate:** ~90% (dry run)
- **Login required:** No
- **CAPTCHA:** None usually
- **Form type:** Single-page modal
- **What works:**
  - Apply button → modal form
  - Single name field (fills "First Last")
  - Email, phone, LinkedIn, GitHub, portfolio
  - Resume upload
  - Education fields
  - Custom/additional questions
  - EEO questions
- **What doesn't work:**
  - LinkedIn profile import prompt (skipped)
  - Referral code fields

### Template 3: Ashby ✅ PRODUCTION READY (API-FIRST)
- **URLs:** `jobs.ashbyhq.com`
- **Pending jobs:** 59
- **Success rate:** ~95%
- **Login required:** No
- **CAPTCHA:** None
- **Form type:** API submission (no browser needed for most)
- **What works:**
  - Calls Ashby public API to get form structure
  - Submits via API (multipart/form-data)
  - Maps config fields to Ashby field IDs
  - Resume as base64 encoding
  - Falls back to browser if API fails
- **What doesn't work:**
  - Cover letter via API (only resume)

### Template 4: SmartRecruiters ✅ PRODUCTION READY (SPECIAL)
- **URLs:** `jobs.smartrecruiters.com`
- **Pending jobs:** 95
- **Success rate:** ~85%
- **Login required:** No
- **CAPTCHA:** DataDome (bypassed with `nodriver`)
- **Form type:** Shadow DOM (`oneclick-ui`)
- **What works:**
  - Uses `nodriver` instead of Playwright (bypasses DataDome)
  - Shadow DOM piercing via JavaScript
  - "I'm interested" button click
  - All standard fields via `spl-input`, `spl-phone-field`, `spl-select`
  - Screening questions
  - Resume upload via `spl-dropzone`
- **What doesn't work:**
  - Complex multi-step forms (rare for SmartRecruiters)

### Template 5: Workday ⚠️ PARTIAL (MOST REQUIRE LOGIN)
- **URLs:** `*.myworkdayjobs.com`
- **Pending jobs:** 1,203 (most will be skipped)
- **Success rate:** ~20% (most require login)
- **Login required:** YES (most jobs)
- **CAPTCHA:** Varies
- **Form type:** Multi-step wizard
- **What works:**
  - Apply button click
  - Multi-page wizard navigation
  - Standard text fields
  - Custom Workday dropdowns
  - Resume upload
  - Login wall detection → marks as skipped
- **What doesn't work:**
  - Account creation (can't create Workday accounts)
  - SSO redirects
  - Most Workday jobs simply get skipped

### Template 6: Generic ⚠️ BASIC FALLBACK
- **URLs:** Any unknown ATS
- **Pending jobs:** 701 (unknown ATS type)
- **Success rate:** ~30%
- **What works:**
  - Apply button detection
  - Basic form filling
  - Multi-page navigation (up to 5 pages)
- **What doesn't work:**
  - ATS-specific UI components
  - Custom dropdowns
  - Login walls

### Template 7: iCIMS ❌ NOT SUPPORTED
- **Pending jobs:** 85
- **Reason:** Almost always requires account creation/login
- **Status:** Skipped automatically

---

## 4. Question Catalog — By Template

### Questions Answered From Config (No AI Needed)

These are pattern-matched from `config/master_config.yaml`. They ALWAYS get the right answer.

#### Personal Info
| Question Pattern | Answer Source | Example Answer |
|-----------------|--------------|----------------|
| First Name / Given Name | `personal_info.first_name` | Alan |
| Last Name / Surname | `personal_info.last_name` | Vu |
| Email / Email Address | `personal_info.email` | alanvu2440@gmail.com |
| Phone / Mobile / Cell | `personal_info.phone` | +1 408-921-7836 |
| City / Location | `personal_info.city` | San Jose |
| State | `personal_info.state` | CA |
| LinkedIn URL | `personal_info.linkedin` | linkedin.com/in/alanvu2440 |
| GitHub URL | `personal_info.github` | github.com/alanvu2440 |
| Website / Portfolio | `personal_info.github` | github.com/alanvu2440 |
| Address / Street | `personal_info.address` | (from config) |
| Zip Code | `personal_info.zip_code` | (from config) |

#### Education
| Question Pattern | Answer Source | Example Answer |
|-----------------|--------------|----------------|
| School / University | `education[0].school` | San Jose State University |
| Degree | `education[0].degree` | Bachelor of Science |
| Major / Field of Study | `education[0].field_of_study` | Software Engineering |
| GPA / Grade Point Average | `education[0].gpa` | 3.49 |
| Graduation Date | `education[0].graduation_date` | May 2026 |
| Expected graduation year | extracted from grad date | 2026 |
| Currently enrolled? | from grad date (future = Yes) | Yes |
| What type of degree? | `education[0].degree` | Bachelor of Science |

#### Work Authorization
| Question Pattern | Answer Source | Example Answer |
|-----------------|--------------|----------------|
| Authorized to work in US? | `work_authorization.us_work_authorized` | Yes |
| Require sponsorship? | `work_authorization.require_sponsorship_now` | No |
| Sponsorship Requirement | negation of require_sponsorship | No |
| Will you require sponsorship in the future? | `require_sponsorship_future` | No |
| US Citizen? | `work_authorization.citizenship` | Yes |
| What is your visa status? | `work_authorization.citizenship` | Citizen |
| Work authorization status | citizenship/visa_status | US Citizen |
| CPT/OPT/STEM-OPT? | require_sponsorship | No |

#### Screening (Yes/No)
| Question Pattern | Answer Source | Example Answer |
|-----------------|--------------|----------------|
| Background check consent? | `screening.background_check` | Yes |
| Drug test consent? | `screening.drug_test` | Yes |
| Are you at least 18? | `screening.age_requirement` | Yes |
| Non-compete agreement? | `screening.non_compete` | No |
| NDA agreement? | screening default | Yes |
| Acknowledge privacy policy? | always | Yes |
| Convicted of a felony? | screening default | No |

#### Availability
| Question Pattern | Answer Source | Example Answer |
|-----------------|--------------|----------------|
| Start date / Earliest start | `availability.earliest_start_date` | May 2026 |
| Willing to relocate? | `availability.willing_to_relocate` | Yes |
| Willing to travel? | `availability.willing_to_travel` | Yes |
| Willing to work onsite? | `availability.willing_to_work_onsite` | Yes |
| Work schedule preference | availability | Full-time |
| Available for summer? | from dates | Yes |

#### Demographics (Optional — Can Decline)
| Question Pattern | Answer Source | Example Answer |
|-----------------|--------------|----------------|
| Gender | `demographics.gender` | Male |
| Race/Ethnicity | `demographics.race` | Asian |
| Veteran status | `demographics.veteran_status` | I am not a veteran |
| Disability status | `demographics.disability_status` | I don't wish to answer |

#### Common Essay Questions
| Question Pattern | Config Key | Status |
|-----------------|-----------|--------|
| Tell me about yourself | `common_answers.about_yourself` | ✅ Filled |
| Why this company? | `common_answers.why_company` | ✅ Filled |
| Why this role? | `common_answers.why_role` | ✅ Filled |
| Strengths | `common_answers.strengths` | ✅ Filled |
| Weakness | `common_answers.weakness` | ✅ Filled |
| Where in 5 years? | `common_answers.five_year_plan` | ✅ Filled |
| Project you're proud of | `common_answers.proud_project` | ✅ Filled |
| Why should we hire you? | `common_answers.why_hire_you` | ✅ Filled |
| Teamwork example | `common_answers.teamwork_example` | ✅ Filled |
| Challenge you overcame | `common_answers.challenge_overcome` | ✅ Filled |
| Career interests | `common_answers.career_interests` | ✅ Filled |
| Outstanding offers? | `common_answers.outstanding_offers` | ✅ No |
| Languages spoken? | `common_answers.languages_spoken` | ✅ English, Vietnamese |
| Attended recruiting events? | `common_answers.attended_recruiting_events` | ✅ No |
| Additional information | `common_answers.additional_information` | ✅ Filled |
| Tech experience | `common_answers.experience_with_technology` | ✅ Filled |
| Preferred language? | `common_answers.preferred_programming_language` | ✅ Python |
| Code sample link | `common_answers.code_sample_link` | ✅ GitHub URL |
| How did you hear? | `common_answers.how_did_you_hear` | ✅ Online Job Board |
| Salary expectations | `common_answers.salary_expectations` | ✅ Open to discuss |
| Cover letter | `common_answers.cover_letter_text` | ✅ Filled |

#### Tech Experience (Auto-calculated)
| Question Pattern | How It Works | Example |
|-----------------|-------------|---------|
| Years of experience with Python? | Looks up `skills.programming_languages[].years` | 3 |
| Years of experience with TypeScript? | Same lookup | 2 |
| Years of experience with JavaScript? | Same lookup | 4 |
| Years of experience with C#? | Same lookup | 2 |
| Years of experience with [unknown]? | Returns "0" | 0 |

---

## 5. Unanswered Questions — NEEDS YOUR INPUT

These questions were encountered during dry runs and got **garbage generic fallback answers**. You need to provide real answers.

### CATEGORY: Company-Specific Interest
| # | Question | Current (Bad) Answer | Your Answer |
|---|----------|---------------------|-------------|
| 1 | "What intrigues you the most about being a data engineer?" | "I'm a motivated Bachelor of Science student..." (generic) | _____ |
| 2 | "What are you hoping to learn from your internship experience?" | (generic fallback) | _____ |
| 3 | "Why are you interested in Gelber Group?" | (generic fallback — needs Gemini for company-specific) | _____ |

### CATEGORY: Work Location / Onsite
| # | Question | Current (Bad) Answer | Your Answer |
|---|----------|---------------------|-------------|
| 4 | "This role will be onsite at our Headquarters Office. Are you comfortable commuting?" | (no match) | _____ |
| 5 | "Are you open to working 4 days onsite in our San Francisco office?" | (no match) | _____ |
| 6 | "Will you be located in the SF Bay Area during Summer 2026?" | Yes (from config — CORRECT) | ✅ |

### CATEGORY: Authorization (Unusual Phrasing)
| # | Question | Current Answer | Correct? |
|---|----------|---------------|----------|
| 7 | "Work Authorization*" (standalone, no context) | (no match) | Should be "Authorized" or "US Citizen" |
| 8 | "Are you legally authorized to work in the country where this role is located?" | (no match) | Should be "Yes" |
| 9 | "What is the source of your right to work where this role is based?" | (no match) | Should be "US Citizen" |
| 10 | "If hired for this position, would you be required to have visa sponsorship now or in the future?" | (no match) | Should be "No" |

### CATEGORY: Referrals / How Did You Hear
| # | Question | Current Answer | Your Answer |
|---|----------|---------------|-------------|
| 11 | "Where did you first see this job before applying?" | (no match) | _____ |
| 12 | "Did someone refer you to [Company]? If so, please provide their name" | (no match) | _____ |

### CATEGORY: Personal / Legal
| # | Question | Current Answer | Your Answer |
|---|----------|---------------|-------------|
| 13 | "Personal Pronouns*" | (no match) | _____ |
| 14 | "Are any of your immediate family members practicing at a brokerage or financial institution?" | (no match) | _____ |
| 15 | "Have you ever been or are you currently debarred by the U.S. government?" | (no match) | _____ |

### CATEGORY: Education (Unusual Phrasing)
| # | Question | Current Answer | Your Answer |
|---|----------|---------------|-------------|
| 16 | "Degree Status - Please identify your highest degree completed or in progress" | (no match) | _____ |
| 17 | "Please specify details from your answer above (exact degree, school, etc.)" | (no match) | _____ |

### CATEGORY: Agreements / Acknowledgments
| # | Question | Current Answer | Your Answer |
|---|----------|---------------|-------------|
| 18 | "Acknowledge, confirm, and agree to the following statement..." | (no match) | _____ |

---

## 6. Edge Cases & How They're Handled

### ✅ HANDLED

| Edge Case | How |
|-----------|-----|
| Job closed / removed | 20+ text patterns detected ("position filled", 404, error redirect) |
| Login wall (Workday) | Detected → marked as "skipped (login_required)" |
| Invisible reCAPTCHA (Greenhouse) | Solved via 2captcha/AntiCaptcha before submit |
| DataDome CAPTCHA (SmartRecruiters) | Bypassed using `nodriver` instead of Playwright |
| Cookie consent banners | Auto-dismissed ("Accept" button clicked) |
| Newsletter pop-ups | Auto-dismissed |
| Multi-page forms (Greenhouse) | Navigate up to 10 pages |
| Multi-step wizard (Workday) | Navigate up to 10 steps |
| Embedded iframe forms | Detected and switched into |
| React form validation | Hidden input sync + change event dispatch |
| Resume upload | `set_input_files()` on file input |
| Rate limiting | 10 apps/hour, 30s delay between jobs |
| Retry on failure | Up to 3 attempts per job |
| Duplicate jobs | URL uniqueness in SQLite |

### ⚠️ PARTIALLY HANDLED

| Edge Case | Status |
|-----------|--------|
| Typeahead / autocomplete fields | Location field sometimes fails |
| `preferred_name` field | Not in config, always skipped |
| School/degree dropdowns (vs text) | Sometimes misses dropdown selection |
| Company-specific questions | Generic fallback without Gemini |
| Multi-select (choose many) | Not explicitly handled |

### ❌ NOT HANDLED

| Edge Case | Impact | Priority |
|-----------|--------|----------|
| "Already applied" detection | May re-apply to same job | Medium |
| Account creation (Workday, iCIMS) | 1,288 jobs skipped | High (but hard) |
| OAuth / LinkedIn Apply | Some jobs only accept LinkedIn | Low |
| Custom date pickers | Falls back to text, may fail | Low |
| Video/audio response questions | Cannot answer | Low (rare) |
| Conditional fields (show/hide) | May miss hidden fields | Medium |
| Multiple file uploads | Only uploads resume | Low |
| hCaptcha solving | Detection only, no solving | Medium |
| Cloudflare Turnstile | Not supported | Low |
| URL redirect chains | May lose track after 2+ redirects | Low |
| Chat widgets blocking form | Not dismissed | Low |

---

## 7. What Needs Gemini vs What Doesn't

### Does NOT Need Gemini (handled by config patterns):
- All personal info fields
- All education fields
- All work authorization questions
- All screening yes/no questions
- All demographic questions
- Common essay questions (about yourself, strengths, weakness, etc.)
- Language experience years
- Standard "how did you hear" / "salary" / "cover letter"

**Coverage without Gemini: ~85-90% of all questions**

### NEEDS Gemini (or your manual answer):
- "Why are you interested in [SPECIFIC COMPANY]?" — needs company name injected
- "What intrigues you about [SPECIFIC ROLE]?" — needs role context
- "Describe your experience with [SPECIFIC TECHNOLOGY]" — when tech isn't in your config
- Any truly novel/unique question never seen before
- Questions with unusual phrasing not in pattern list

**What happens without Gemini:**
Questions that don't match any config pattern get a generic fallback:
> "I'm a motivated Bachelor of Science student at San Jose State University with experience in software development. I'm eager to contribute to [Company] and continue growing as an engineer."

This is **bad** — it's obviously generic and could hurt your application. With Gemini, you'd get tailored answers using your full profile + job context.

### Recommendation:
1. **Short-term:** Answer the 18 questions in Section 5 above → I'll add them to config
2. **Long-term:** Get a Gemini API key from Google AI Studio (uses your $300 GCP credit)

---

## 8. Logging & Tracking

### Where Everything Goes

| What | Where | Format |
|------|-------|--------|
| Console output | stderr | Colored text |
| Debug log | `logs/applier.log` | Text (rotates at 10MB, 7-day retention) |
| Application records | `logs/running_application_log.jsonl` | JSON Lines (one record per line) |
| Session reports | `logs/application_report_YYYYMMDD_HHMMSS.json` | JSON |
| Question KB | `data/question_knowledge_base.md` | Markdown |
| Answer cache | `data/answer_cache.json` | JSON |
| Screenshots | `data/screenshots/` | PNG (full-page) |
| Job database | `data/jobs.db` | SQLite |

### Screenshot Naming Convention
```
data/screenshots/
├── PASS_Audax_Group_20260215_233547.png
├── PASS_Samsung_Research_America_20260215_233928.png
├── FAIL_Xometry_20260210_142301.png
└── ...
```

### What Gets Tracked Per Application
- Job ID, company, role, URL, ATS type
- Status (submitted / failed / skipped)
- Every field filled (field name → value)
- Every field missed (field name → reason)
- Every question answered (question → answer + source)
- Validation errors
- Error messages
- Screenshot path
- Timestamp

---

## 9. Rate Limiting & Stealth

### Rate Limits
| Limit | Value | Configurable? |
|-------|-------|--------------|
| Applications per hour | 10 | Yes (`preferences.max_applications_per_hour`) |
| Delay between apps | 30 seconds | Yes (`preferences.delay_between_applications_seconds`) |
| Human delay (actions) | 500-2000ms random | Hardcoded |
| Typing speed | 50-150ms per char | Hardcoded |

### Anti-Detection Measures
1. `playwright-stealth` library (patches webdriver, plugins, permissions)
2. JavaScript overrides (`navigator.webdriver = undefined`, etc.)
3. User agent rotation (4 Chrome/Safari/Firefox agents)
4. Human-like typing (character by character)
5. Random mouse movements
6. Random click position offset (±5px)
7. Browser args (`--disable-blink-features=AutomationControlled`)
8. Standard viewport (1920x1080)
9. US locale/timezone/geolocation

### What's NOT Bypassed
- Canvas/WebGL/Audio fingerprinting
- DataDome (needs `nodriver` — SmartRecruiters only)
- Cloudflare Turnstile
- PerimeterX
- IP-based rate limiting (no proxy rotation)

---

## 10. Known Failures & Why

### From Previous Real Runs (100 failed jobs)

| Failure Reason | Count | Fix |
|---------------|-------|-----|
| "Flagged as possible spam" | 3 | Collaborative Robotics, Etched.ai — anti-bot detected us |
| "Application failed" (generic) | 8 | Various — form fill incomplete or ATS rejected |
| Missing required fields | 4 | TENEX.AI, Xometry — dropdowns/multi-select not filled |
| End date year not filled | 2 | Virtu Financial — education date field format mismatch |
| Company name field | 1 | Realtor.com — unexpected field type |
| Internship Term dropdown | 1 | Benchling — custom dropdown not matched |
| How did you hear dropdown | 1 | Octaura — dropdown options didn't match config |
| Felony question | 1 | Aechelon — yes/no question not in patterns (now fixed) |

### From Dry Runs (Warnings, Not Failures)

| Warning | Frequency | Impact |
|---------|-----------|--------|
| "Could not fill preferred_name" | 3/12 jobs | Low — optional field |
| "Could not fill location field" | 4/12 jobs | Medium — some require it |
| "Missed required fields" (education) | 3/12 jobs | Medium — school/degree dropdowns |
| "AI unavailable, no config match" | 19 unique Qs | Medium — gets generic fallback |

---

## 11. Config Checklist — Is Everything Filled?

### ✅ Complete
- [x] Personal info (name, email, phone, address, LinkedIn, GitHub)
- [x] Education (school, degree, field, GPA, graduation date)
- [x] Work authorization (all fields)
- [x] Screening (background check, drug test, age, etc.)
- [x] Demographics (gender, race, veteran, disability)
- [x] Availability (start date, relocate, travel, onsite)
- [x] Skills (programming languages with years, frameworks, tools)
- [x] Experience (Kruiz internship details)
- [x] Projects (Scorecard, Canvas Extension, etc.)
- [x] Resume PDF path
- [x] 22 common answer templates
- [x] 730+ regex patterns for question matching

### ❌ Missing / Needs Attention
- [ ] `preferred_name` — add to config if you want it filled (or leave blank)
- [ ] Gemini API key — current key has $0 quota
- [ ] 18 unanswered questions from Section 5 above
- [ ] CAPTCHA solver API key (2captcha or AntiCaptcha) — check if configured

### How to Check CAPTCHA Config
```bash
cat config/secrets.yaml | grep -i captcha
```

---

## Pre-Launch Checklist

Before running for real (`python src/main.py backfill`):

- [ ] **Answer all 18 questions in Section 5** → I'll add to config
- [ ] **Get Gemini API key** (Google AI Studio → uses your $300 GCP credit) OR accept generic fallbacks
- [ ] **Verify CAPTCHA solver** is configured in `config/secrets.yaml`
- [ ] **Review screenshots** from dry runs in `data/screenshots/`
- [ ] **Set max applications** — decide how many per session
- [ ] **Choose ATS targets** — start with Greenhouse+Lever+Ashby (highest success rate)

### Recommended First Real Run
```bash
# Apply to 10 Greenhouse jobs first (highest success rate)
python src/main.py backfill --max 10
```

Then check `data/screenshots/` and `logs/running_application_log.jsonl` to verify everything looks good before scaling up.
