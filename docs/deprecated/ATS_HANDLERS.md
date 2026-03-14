# ATS Handler Deep Dive

> Per-handler breakdown: status, success rate, form structure, known issues.
> Last updated: 2026-02-24

---

## Handler Summary

| ATS | Handler | Lines | Status | Success Rate | Login? | CAPTCHA | Pending |
|-----|---------|-------|--------|-------------|--------|---------|---------|
| Greenhouse | `greenhouse.py` | 3,992 | Production | ~95% | No | Invisible reCAPTCHA (solved) | 158 |
| SmartRecruiters | `smartrecruiters.py` | 863 | Production | ~85% | No | DataDome (bypassed) | 95 |
| Ashby | `ashby.py` | 721 | Production | ~95% | No | None | 59 |
| Lever | `lever.py` | 662 | Production | ~90% | No | None | 22 |
| Workday | `workday.py` | 479 | Partial | ~20% | YES (most) | Varies | 1,203 |
| iCIMS | `icims.py` | 1,089 | Skipped | 0% | YES (always) | N/A | 85 |
| Generic | `generic.py` | 404 | Basic | ~30% | Varies | Varies | 701 |

---

## Greenhouse (greenhouse.py) — 3,992 lines

### Status: Production — ~95% success rate

### Overview

Greenhouse is the most common ATS in our queue and the most thoroughly cracked. The handler supports multi-page forms, invisible reCAPTCHA solving, resume upload, and 100+ field types.

### Form Structure

```
Page 1: Personal Info
  ├── First Name, Last Name, Email, Phone
  ├── Resume upload (file input)
  ├── LinkedIn, Website, GitHub (optional)
  └── Location / Address fields

Page 2+: Custom Questions
  ├── Text inputs (short answer)
  ├── Textareas (long answer — cover letter, "why this role")
  ├── Dropdowns (work authorization, education level, etc.)
  ├── Radio buttons (yes/no, multiple choice)
  ├── Checkboxes (acknowledgements, agreements)
  └── Hidden fields (UTM tracking, source)

Final Page: Demographics (optional)
  ├── Gender, Race/Ethnicity, Veteran, Disability
  └── These are voluntary — filled from config.demographics

Submit Button: "Submit Application"
  └── May have invisible reCAPTCHA that fires on click
```

### Key Features

- Multi-page navigation with "Next" button detection
- Invisible reCAPTCHA solving via 2captcha
- React form validation sync (sets hidden input values)
- Dropdown typeahead for location/school fields
- Resume upload via `set_input_files()`
- "Job closed" detection (20+ text patterns)

### Known Issues

| Issue | Cause | Workaround |
|-------|-------|------------|
| Location typeahead fails | Can't select from autocomplete dropdown | Falls back to typing city name |
| School dropdown timeout | Typeahead search takes >5s | Extended timeout to 10s |
| Hidden required fields | React renders conditionally | Scroll-to-field detection |
| Multi-page form stuck | "Next" button disabled | Validates all fields before clicking |

---

## SmartRecruiters (smartrecruiters.py) — 863 lines

### Status: Production — ~85% success rate

### Overview

SmartRecruiters uses DataDome bot protection which blocks standard Playwright. This handler uses **nodriver** (undetected Chrome) to bypass detection.

### Form Structure

```
Single Page Application (React):
  ├── Personal Info (first name, last name, email, phone)
  ├── Resume upload
  ├── Screening questions (dynamic, varies by company)
  │   ├── Dropdowns (work authorization, visa status)
  │   ├── Text fields (years of experience, availability)
  │   └── Yes/No toggles
  ├── Privacy consent checkbox
  └── Submit button
```

### Key Features

- nodriver-based browser to bypass DataDome
- Separate browser context from Playwright handlers
- Cookie consent banner auto-dismiss
- Dynamic form field detection

### Known Issues

| Issue | Cause | Workaround |
|-------|-------|------------|
| DataDome block on first visit | IP reputation | Wait + retry |
| nodriver session instability | Chrome crashes | Restart browser |
| File upload dialog | nodriver file input quirks | Direct `set_input_files` |

---

## Ashby (ashby.py) — 721 lines

### Status: Production — ~95% success rate

### Overview

Ashby uses an API-first approach. The handler intercepts GraphQL requests and fills forms via API calls where possible, falling back to DOM manipulation.

### Form Structure

```
Single Page:
  ├── Personal Info
  │   ├── Name, Email, Phone, LinkedIn
  │   └── Resume upload
  ├── Custom Questions (JSON-defined)
  │   ├── Text, Textarea, Select, Multi-select
  │   └── File upload fields
  └── Submit (via API or button click)
```

### Key Features

- API-first: tries to submit via Ashby GraphQL API
- Falls back to browser-based form filling
- Clean JSON schema for question types
- No CAPTCHA

### Known Issues

| Issue | Cause | Workaround |
|-------|-------|------------|
| API schema changes | Ashby updates endpoints | Fall back to DOM |
| Multi-select fields | Complex interaction | Click each option individually |

---

## Lever (lever.py) — 662 lines

### Status: Production — ~90% success rate

### Overview

Lever has a straightforward form structure. No CAPTCHA, no login wall. The main challenge is custom questions that vary wildly between companies.

### Form Structure

```
Single Page:
  ├── Personal Info
  │   ├── Full Name, Email, Phone
  │   ├── Current Company, LinkedIn, GitHub, Portfolio
  │   └── Resume upload
  ├── Custom Questions
  │   ├── Text inputs
  │   ├── Textareas (cover letter, essays)
  │   ├── Dropdowns
  │   └── URLs (portfolio, code samples)
  ├── EEO Section (optional)
  │   └── Gender, Race, Veteran, Disability
  └── Submit button
```

### Key Features

- Clean HTML structure (easy to parse)
- No CAPTCHA
- No login wall
- Straightforward field detection

### Known Issues

| Issue | Cause | Workaround |
|-------|-------|------------|
| Label passed as question text | Lever sends bare labels like "Bachelor's" | Regex patterns handle it |
| Dropdown option leak | Option text appears as question | Skip patterns in config |

---

## Workday (workday.py) — 479 lines

### Status: Partial — ~20% success rate (1,203 pending)

### Overview

Workday is the biggest blocker. Most Workday instances require **account creation + login** before you can apply. The handler currently:
- Detects login walls → marks as skipped
- Can fill forms when no login is required (~20% of Workday jobs)

### Form Structure

```
Login Wall (most companies):
  ├── "Sign In" / "Create Account" page
  └── Blocks all further progress

If no login:
  Single/Multi-page form:
    ├── Personal Info
    ├── Education (complex — school/degree dropdowns)
    ├── Work Experience (multiple entries)
    ├── Screening Questions
    └── Submit
```

### Known Issues

| Issue | Cause | Status |
|-------|-------|--------|
| Login wall | Requires account per company | See ACCOUNT_STRATEGY.md |
| Education dropdowns | Complex typeahead with validation | Partially handled |
| Multi-step wizard | 5+ pages with validation | Basic navigation |
| Session timeout | Long forms cause timeout | Not handled |

---

## iCIMS (icims.py) — 1,089 lines

### Status: Skipped — 0% success (85 pending)

### Overview

iCIMS **always** requires login/account creation. Currently skipped entirely.

### Blocking Issues

1. Account creation required for every company
2. Email verification step
3. Complex multi-step registration form
4. Session management across pages

See `docs/ACCOUNT_STRATEGY.md` for the cracking plan.

---

## Generic Handler (generic.py) — 404 lines

### Status: Basic — ~30% success rate (701 pending)

### Overview

Fallback handler for unknown ATS systems. Tries to detect common form patterns and fill them generically.

### Approach

1. Navigate to URL
2. Look for common form elements (`input[type=text]`, `input[type=email]`, etc.)
3. Try to match labels to config values
4. Upload resume if file input found
5. Look for submit button
6. Hope for the best

### Why 30%

- No handler-specific logic for form structure
- Can't handle login walls
- Can't handle custom JavaScript frameworks
- Can't handle multi-step forms
- Can't handle iframes
- Can't handle CAPTCHAs

---

## Base Handler (base.py) — 430 lines

### What It Provides

All handlers inherit from `BaseHandler`:

```python
class BaseHandler:
    # Shared methods:
    - async apply(url) → orchestrates the full flow
    - _detect_job_closed(page) → 20+ text patterns
    - _detect_login_wall(page) → login detection
    - _fill_field(field, value) → type into input
    - _upload_resume(page) → find file input, upload
    - _take_screenshot(page, status) → save to data/screenshots/
    - _wait_human_like() → randomized delay
```
