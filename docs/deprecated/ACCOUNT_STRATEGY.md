# Account Strategy — Login-Walled ATS Systems

> Plan for cracking Workday (1,203 pending) and iCIMS (85 pending).
> Last updated: 2026-02-24

---

## PART A: The Problem

### Workday — 1,203 pending jobs (~52% of total)

Most Workday instances require you to **create an account** before applying. Each company has its own Workday tenant, meaning a separate account per company.

Current behavior:
- Handler navigates to job URL
- Detects "Sign In" / "Create Account" page
- Marks job as `skipped` (reason: login_wall)
- ~20% of Workday jobs DON'T require login → these succeed

### iCIMS — 85 pending jobs

iCIMS **always** requires login. No exceptions.

Current behavior:
- All iCIMS jobs immediately skipped

---

## PART B: Workday Registration Flow

### Typical Workday Sign-Up

```
1. Navigate to job URL
   ▼
2. Redirected to: "Sign In to Apply"
   ├── "Sign In" button
   └── "Create Account" link
   ▼
3. Click "Create Account"
   ▼
4. Registration Form:
   ├── Email address
   ├── Password (complex requirements: 8+ chars, upper, lower, number, special)
   ├── First Name, Last Name
   ├── Country
   └── Accept Terms checkbox
   ▼
5. Email Verification:
   ├── "We've sent a verification link to your email"
   └── Must click link in email to activate
   ▼
6. Account Active → Can now apply
```

### Challenges

1. **Per-company accounts** — each Workday tenant is separate
2. **Email verification** — must access inbox programmatically
3. **Password complexity** — varies by tenant
4. **CAPTCHA** — some tenants have reCAPTCHA on registration
5. **Session management** — must persist login cookies
6. **Rate limiting** — too many accounts from same IP may trigger blocks

---

## PART C: Gmail Alias Strategy

### How Gmail Aliases Work

Gmail ignores dots and supports `+` aliases:
```
Base email:     alan@gmail.com
With dots:      a.lan@gmail.com      → same inbox
With plus:      alan+workday1@gmail.com → same inbox
Combined:       a.lan+wd@gmail.com   → same inbox
```

### Per-Company Alias Pattern

```python
def generate_alias(company_name: str, base_email: str) -> str:
    """Generate a unique Gmail alias for each company."""
    # Sanitize company name
    slug = company_name.lower().replace(" ", "")[:10]
    user, domain = base_email.split("@")
    return f"{user}+{slug}@{domain}"

# Examples:
# alan+google@gmail.com
# alan+meta@gmail.com
# alan+apple@gmail.com
```

### Pros

- Unlimited aliases from one inbox
- All verification emails arrive at same inbox
- Easy to track which company = which alias
- No new email accounts needed

### Cons

- Some forms reject `+` in emails (rare for Workday)
- All linked to same base email (privacy concern if cross-referenced)
- If one gets flagged, all aliases share the same inbox

---

## PART D: Email Verification Automation

### Option 1: Gmail API (Recommended)

```python
# Use Gmail API to monitor for verification emails
# 1. Watch for new emails matching "Workday" or "verify" in subject
# 2. Extract verification link from email body
# 3. Navigate to link in browser
# 4. Return to application flow

# Requires:
# - Google Cloud project with Gmail API enabled
# - OAuth2 credentials (one-time setup)
# - config/secrets.yaml: gmail_oauth_token
```

### Option 2: IMAP Polling

```python
# Connect via IMAP and poll for new emails
# Simpler setup but slower (polling interval)

# Requires:
# - Gmail IMAP enabled
# - App password (2FA required)
# - config/secrets.yaml: gmail_imap_password
```

### Option 3: Manual Verification Queue

```
# Don't automate email verification
# Instead:
# 1. Bot creates account + starts application
# 2. Bot queues the job as "awaiting_verification"
# 3. Human checks email, clicks verify link
# 4. Bot retries the job (now logged in via cookie)
```

---

## PART E: Session Persistence

### Cookie Storage

```python
# After successful login, save cookies:
cookies = await browser_context.cookies()
save_cookies(company_slug, cookies)

# Before applying, load cookies:
cookies = load_cookies(company_slug)
await browser_context.add_cookies(cookies)

# Storage: data/cookies/{company_slug}.json
```

### Session Validation

```python
# Before using saved cookies:
# 1. Load cookies
# 2. Navigate to job URL
# 3. Check if still logged in (no redirect to login page)
# 4. If session expired → re-login or re-create account
```

---

## PART F: Implementation Plan

### Phase 1: Workday Account Registration (High Impact)

```
Priority: HIGH (1,203 pending jobs)

Steps:
1. Add account_manager.py
   - create_account(company, email_alias, password)
   - login(company, email, password)
   - save_session(company, cookies)
   - load_session(company) -> cookies

2. Add to workday.py handler:
   - detect_login_wall() → already exists
   - attempt_login(saved_cookies) → new
   - create_account_if_needed() → new
   - handle_email_verification() → new (or queue)

3. Account storage:
   - data/accounts.db (SQLite)
   - Schema: company, email_alias, password_hash, created_at, last_login, status

4. Cookie storage:
   - data/cookies/{company_slug}.json
   - Auto-expire after 24 hours
```

### Phase 2: Email Verification (Medium)

```
Priority: MEDIUM (needed for Phase 1)

Options (in order of preference):
1. Gmail API — fully automated
2. Manual queue — human clicks verify links
3. IMAP polling — automated but slower
```

### Phase 3: iCIMS (Low)

```
Priority: LOW (only 85 jobs)

Same approach as Workday but:
- Different registration flow
- Different form structure
- May not be worth the engineering effort for 85 jobs
```

---

## PART G: Risk Assessment

| Risk | Impact | Mitigation |
|------|--------|------------|
| IP ban from mass account creation | HIGH | Rate limit: 1 account per 5 minutes |
| Email flagged as spam | MEDIUM | Use Gmail aliases, not throwaway emails |
| Account banned for automation | MEDIUM | Human-like delays, vary behavior |
| Password complexity failures | LOW | Generate passwords meeting all requirements |
| CAPTCHA on registration | MEDIUM | Extend 2captcha solver to registration flow |
| Cross-company detection | LOW | Unique alias per company |

---

## PART H: Not Yet Decided

- [ ] Gmail API vs IMAP vs manual verification
- [ ] Password generation strategy (random vs template)
- [ ] Account reuse policy (one-time vs persistent)
- [ ] Cookie expiration handling
- [ ] Whether to attempt iCIMS at all (85 jobs may not justify effort)
