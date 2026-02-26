# LOGIN SCRAPER BUILD GUIDE

## Mission

Build automated login + account creation for **Workday** (1,611 jobs) and **iCIMS** (122 jobs). These are the two ATS systems that require authentication before applying.

---

## PART A: ARCHITECTURE OVERVIEW

```
Job URL → Handler.apply()
              ↓
         Navigate to job page
              ↓
         Click "Apply" button
              ↓
     ┌── Login wall detected? ──┐
     │                          │
    YES                         NO
     │                          │
     ↓                          ↓
  _handle_auth()          Fill form normally
     │
     ├── Tenant already in accounts.json?
     │     YES → _signin(email, password)
     │     NO  → _create_account(email, password)
     │              ↓
     │         Fill email + password + verify
     │              ↓
     │         Email verification needed?
     │              YES → Read code from Gmail IMAP
     │              NO  → Continue
     │              ↓
     │         Save to accounts.json
     │         Save cookies to data/workday_cookies/
     │
     ↓
  Re-navigate to job → Fill form → Submit
```

---

## PART B: FILES TO READ (IN ORDER)

### Required Reading Before Coding

| # | File | Lines | Why Read It |
|---|------|-------|-------------|
| 1 | `CLAUDE.md` | ~200 | Project rules, architecture, all ATS info |
| 2 | `src/handlers/base.py` | ~391 | Base class all handlers inherit from. Has `is_job_closed()`, `handle_captcha()`, `is_application_complete()`, `take_screenshot()`, `dismiss_popups()` |
| 3 | `src/handlers/workday.py` | ~620 | **ALREADY HAS** account creation + login + cookie persistence built in. Needs testing and debugging. |
| 4 | `src/handlers/greenhouse.py` | ~3500 | Reference implementation — highest success rate. Study how it fills forms, handles dropdowns, uploads resume, handles multi-page |
| 5 | `src/browser_manager.py` | ~251 | Browser launch, stealth settings, `human_delay()`, Playwright config |
| 6 | `src/form_filler.py` | ~2513 | Universal form filling — text fields, dropdowns, checkboxes, file uploads, React-Select |
| 7 | `src/ai_answerer.py` | ~1700 | Question answering chain — config patterns, cache, Gemini, fallback |
| 8 | `src/main.py` | ~1161 | Orchestrator — `apply_to_job()` at line ~310, `backfill()` at line ~630. See how handlers are routed |
| 9 | `config/master_config.yaml` | ~300 | All personal info, education, work auth — the data that fills forms |
| 10 | `config/secrets.yaml` | ~41 | API keys, Gmail IMAP creds (for email verification) |

### Reference Material

| File | Why |
|------|-----|
| `docs/ARCHITECTURE.md` | Full system overview with ASCII diagrams |
| `docs/ATS_HANDLERS.md` | Per-handler breakdown, success rates, known issues |
| `docs/ACCOUNT_STRATEGY.md` | Gmail alias strategy, session persistence plan |
| `docs/QUESTION_SYSTEM.md` | How questions get answered |

---

## PART C: WORKDAY HANDLER — CURRENT STATE

### What's Already Built (`src/handlers/workday.py`)

The Workday handler already has:

1. **Account creation flow** (`_create_account()`)
   - Detects "Create Account" / "Sign Up" buttons
   - Fills email using Gmail alias: `alanvu2440+{tenant}@gmail.com`
   - Fills password: `AutoApply2026!#Xk`
   - Fills verify password
   - Checks terms checkboxes
   - Clicks submit
   - Detects "already in use" → falls back to signin

2. **Sign-in flow** (`_signin()`)
   - Finds "Sign In" link
   - Fills email + password
   - Clicks submit
   - Verifies success by checking URL

3. **Email verification** (`_verify_email()`)
   - Connects to Gmail IMAP
   - Searches for recent Workday verification emails
   - Extracts 6-digit code
   - Enters code on page

4. **Cookie persistence** (`_save_cookies()` / `_load_cookies()`)
   - Saves cookies per-tenant to `data/workday_cookies/{tenant}.json`
   - Loads cookies before navigating (skips login if session valid)

5. **Account tracker** (`data/workday_accounts.json`)
   - Tracks which tenants have accounts
   - Maps tenant → email alias

6. **Login wall detection** (`_detect_login_wall()`)
   - URL pattern matching (`/login`, `/signin`, `/sso`, `/auth`)
   - Page text scanning ("sign in", "create account", etc.)

### What Needs Testing/Fixing

```
PRIORITY 1: Test account creation on ONE Workday tenant
  → Pick a job URL from the DB
  → Run: python src/main.py apply --url "https://company.wd5.myworkdayjobs.com/..."
  → Watch what happens — screenshot will be in data/screenshots/
  → Fix selectors as needed

PRIORITY 2: Test email verification
  → After account creation, check if verification email arrives
  → Gmail IMAP is configured (app password in secrets.yaml)
  → If code format is different (not 6 digits), fix regex in _verify_email()

PRIORITY 3: Test cookie persistence
  → Apply to TWO jobs on the same tenant
  → Second job should skip login (cookies loaded)
  → Check data/workday_cookies/{tenant}.json exists

PRIORITY 4: Test form filling after login
  → Workday uses multi-page wizard forms
  → data-automation-id selectors for all fields
  → Custom dropdowns (not standard <select>) — need click → menu → option flow
```

### Key Workday Selectors

```
# Account Creation
Email:           input[data-automation-id='email']
Password:        input[data-automation-id='password']
Verify Password: input[data-automation-id='verifyPassword']
Create Account:  button[aria-label='Create Account']
Sign In:         button[aria-label='Sign In']

# Application Form
First Name:      [data-automation-id='legalNameSection_firstName']
Last Name:       [data-automation-id='legalNameSection_lastName']
Email:           [data-automation-id='email']
Phone:           [data-automation-id='phone-number']
Address:         [data-automation-id='addressSection_addressLine1']
City:            [data-automation-id='addressSection_city']
Zip:             [data-automation-id='addressSection_postalCode']
Country:         [data-automation-id='addressSection_countryRegion']
State:           [data-automation-id='addressSection_countryRegionState']
Resume Upload:   input[data-automation-id='file-upload-input-ref']
Next Button:     [data-automation-id='bottom-navigation-next-button']
Submit Button:   button:has-text('Submit Application')

# Workday Custom Dropdowns (NOT standard <select>)
# Must click dropdown → wait for menu → click option
Dropdown:        button[aria-haspopup='listbox']
Menu Item:       div[data-automation-id='menuItem']
Form Field:      [data-automation-id='formField']
Question Item:   [data-automation-id='questionItem']
Error Message:   [data-automation-id='errorMessage']
Wizard:          [data-automation-id='wizardPageContainer']
```

### Workday URL Patterns

```
Job Page:     https://{company}.{wd#}.myworkdayjobs.com/{path}/job/{id}
Login Page:   https://{company}.{wd#}.myworkdayjobs.com/{path}/login
Account Page: https://{company}.{wd#}.myworkdayjobs.com/{path}/account

Tenant ID = "{company}.{wd#}" (e.g., "nvidia.wd5", "google.wd1")
The wd# varies: wd1, wd3, wd5 are common
```

### Gmail Alias Strategy

```
Base email: alanvu2440@gmail.com
Per-tenant:
  nvidia.wd5    → alanvu2440+nvidia-wd5@gmail.com
  google.wd1    → alanvu2440+google-wd1@gmail.com
  meta.wd3      → alanvu2440+meta-wd3@gmail.com

All aliases route to the same Gmail inbox.
Gmail ignores everything after +, so IMAP reads all verification emails.
```

---

## PART D: iCIMS HANDLER — NEEDS TO BE BUILT

### Overview

iCIMS is the second login-walled ATS. 122 pending jobs. No handler exists yet beyond the generic fallback.

### What to Build

Create `src/handlers/icims.py` following the same pattern as Workday:

```python
# src/handlers/icims.py
"""
iCIMS Handler

URLs: *.icims.com, careers-*.icims.com
Always requires login. Account creation similar to Workday.
"""

from .base import BaseHandler

class ICIMSHandler(BaseHandler):
    name = "icims"

    # TODO: Implement
    # 1. _detect_login_wall()
    # 2. _create_account()
    # 3. _signin()
    # 4. _verify_email() (if needed)
    # 5. _fill_form()
    # 6. _submit()
```

### iCIMS Research Needed

1. **Navigate to any iCIMS job URL** from the database:
   ```sql
   SELECT url, company FROM jobs WHERE ats_type='icims' AND status='pending' LIMIT 5;
   ```

2. **Document the login flow**:
   - What does the login page look like?
   - Is it SSO or email/password?
   - Account creation available?
   - Email verification required?

3. **Document the form structure**:
   - What selectors does iCIMS use?
   - Standard `<select>` or custom dropdowns?
   - Multi-page wizard or single form?
   - File upload mechanism?

4. **Reference**: Look at amgenene/workday_auto on GitHub — same approach can be adapted for iCIMS

### iCIMS URL Patterns

```
Job Page:   https://careers-{company}.icims.com/jobs/{id}/job
Login Page: https://careers-{company}.icims.com/jobs/{id}/login
Apply Page: https://careers-{company}.icims.com/jobs/{id}/candidate
```

---

## PART E: TESTING WORKFLOW

### Step-by-step Testing

```bash
# 1. Test Workday on a single job (headed, so you can watch)
python src/main.py apply --url "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/..."

# 2. Watch the browser — note what happens at each step
# 3. Check screenshots: ls -la data/screenshots/
# 4. Check logs: tail -100 logs/applier.log

# 5. After fixing issues, dry-run 5 Workday jobs
python src/main.py backfill --dry-run --max 5 --ats workday

# 6. If dry-run passes, go live with 10
python src/main.py backfill --max 10 --ats workday

# 7. Check results
python src/main.py stats
```

### Common Issues You'll Hit

| Issue | Symptom | Fix |
|-------|---------|-----|
| Selector not found | "Could not find email input" | Inspect element, update selector |
| Wrong page after login | "Login wall detected after Apply" | Cookie not saved, or redirect issue |
| Email not found in Gmail | "No verification email found" | Check spam, increase IMAP wait time |
| Workday custom dropdown | Menu doesn't appear | Need to click the button, not the label |
| React re-render | Field clears after typing | Use `type()` with delay instead of `fill()` |
| Different Workday versions | Selectors don't match | Some tenants have older Workday UI — need fallback selectors |

### Debug Commands

```bash
# Check which Workday tenants exist in the DB
python -c "
import sqlite3
conn = sqlite3.connect('data/jobs.db')
c = conn.cursor()
c.execute(\"SELECT url FROM jobs WHERE ats_type='workday' AND status='pending' LIMIT 10\")
for row in c.fetchall():
    print(row[0])
"

# Check account tracker
cat data/workday_accounts.json

# Check saved cookies
ls -la data/workday_cookies/

# Check logs for Workday-specific issues
grep -i "workday" logs/applier.log | tail -50
```

---

## PART F: REGISTRATION WITH HANDLER SYSTEM

After building a handler, register it in `src/main.py`:

```python
# In InternshipAutoApplier.__init__() around line 240
from handlers.icims import ICIMSHandler

self.handlers = {
    ATSType.GREENHOUSE: GreenhouseHandler(...),
    ATSType.LEVER: LeverHandler(...),
    ATSType.ASHBY: AshbyHandler(...),
    ATSType.SMARTRECRUITERS: SmartRecruitersHandler(...),
    ATSType.WORKDAY: WorkdayHandler(...),
    ATSType.ICIMS: ICIMSHandler(...),  # ADD THIS
    ATSType.UNKNOWN: GenericHandler(...),
}
```

Also add `ICIMS = "icims"` to the `ATSType` enum in `src/job_parser.py` if not already there.

---

## SUMMARY: WHAT TO DO

```
TERMINAL 1 (This guide):
  1. Read files in order from PART B table
  2. Test Workday handler on single URL
  3. Debug and fix selectors
  4. Test cookie persistence
  5. Run small Workday batch (5 jobs)
  6. Build iCIMS handler following Workday pattern
  7. Test iCIMS on single URL
  8. Scale up

TERMINAL 2 (Main session):
  - Running application batches
  - Monitoring results
  - Fixing other handlers
```
