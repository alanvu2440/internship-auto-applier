# Edge Case Analysis - Internship Auto-Applier

## Summary Statistics

| Category | Handled | Missing | Critical |
|----------|---------|---------|----------|
| Network/Connection | 3 | 8 | 4 |
| Form Handling | 12 | 15 | 6 |
| ATS-Specific | 8 | 12 | 5 |
| Data/Config | 5 | 9 | 3 |
| Browser/Automation | 6 | 11 | 4 |
| Queue/State | 4 | 7 | 3 |
| **TOTAL** | **38** | **62** | **25** |

---

## 1. NETWORK/CONNECTION EDGE CASES

### Currently Handled (3)
- [x] HTTP timeout on page load (30s default)
- [x] GitHub API rate limiting (warns only)
- [x] Basic httpx timeout (30s)

### Missing - CRITICAL (8)
- [ ] **EC-NET-001**: No retry on network failure during form submission
- [ ] **EC-NET-002**: No handling for partial page load (JS not executed)
- [ ] **EC-NET-003**: No detection of CloudFlare/DDoS protection pages
- [ ] **EC-NET-004**: No handling for slow connections (progressive timeout)
- [ ] **EC-NET-005**: No proxy support for IP rotation
- [ ] **EC-NET-006**: No handling for SSL certificate errors
- [ ] **EC-NET-007**: No detection of VPN/geo-blocking (some jobs US-only)
- [ ] **EC-NET-008**: No handling for GitHub being down/unavailable

---

## 2. FORM HANDLING EDGE CASES

### Currently Handled (12)
- [x] Empty form fields (skips if no value)
- [x] Hidden fields (skips non-visible)
- [x] Disabled fields (skips disabled)
- [x] Basic dropdown selection
- [x] Checkbox toggling
- [x] Radio button groups
- [x] Text input filling
- [x] Textarea filling
- [x] File upload (basic)
- [x] Label association (for/id)
- [x] Parent label detection
- [x] Previous sibling label

### Missing - CRITICAL (15)
- [ ] **EC-FORM-001**: No handling for dynamic forms (React/Vue state updates)
- [ ] **EC-FORM-002**: No handling for conditional fields (show/hide based on answers)
- [ ] **EC-FORM-003**: No handling for autocomplete/typeahead fields
- [ ] **EC-FORM-004**: No handling for date pickers (custom UI components)
- [ ] **EC-FORM-005**: No handling for salary range sliders
- [ ] **EC-FORM-006**: No handling for multi-file uploads
- [ ] **EC-FORM-007**: No handling for drag-and-drop file uploads
- [ ] **EC-FORM-008**: No handling for required field validation before submit
- [ ] **EC-FORM-009**: No handling for phone number formatting (intl formats)
- [ ] **EC-FORM-010**: No handling for address autocomplete (Google Places)
- [ ] **EC-FORM-011**: No detection of form submission success/failure
- [ ] **EC-FORM-012**: No handling for forms split across multiple iframes
- [ ] **EC-FORM-013**: No handling for forms requiring OAuth (LinkedIn Apply)
- [ ] **EC-FORM-014**: No handling for WYSIWYG editors (rich text)
- [ ] **EC-FORM-015**: No handling for signature/consent checkboxes with modals

---

## 3. ATS-SPECIFIC EDGE CASES

### Currently Handled (8)
- [x] Greenhouse standard form
- [x] Greenhouse embedded iframe
- [x] Greenhouse job-boards subdomain
- [x] Lever basic apply flow
- [x] Lever EEO questions
- [x] Workday multi-page wizard
- [x] Workday custom dropdowns
- [x] Generic fallback handler

### Missing - CRITICAL (12)
- [ ] **EC-ATS-001**: Greenhouse - No handling for "Easy Apply" vs full application
- [ ] **EC-ATS-002**: Greenhouse - No handling for custom question types (ranking, ordering)
- [ ] **EC-ATS-003**: Lever - No handling for LinkedIn profile import prompt
- [ ] **EC-ATS-004**: Lever - No handling for referral code fields
- [ ] **EC-ATS-005**: Workday - No handling for account creation requirement
- [ ] **EC-ATS-006**: Workday - No handling for "Sign in to apply" pages
- [ ] **EC-ATS-007**: Workday - No handling for job requisition changes mid-apply
- [ ] **EC-ATS-008**: No Ashby-specific handler (uses generic)
- [ ] **EC-ATS-009**: No SmartRecruiters-specific handler
- [ ] **EC-ATS-010**: No Jobvite-specific handler
- [ ] **EC-ATS-011**: No iCIMS-specific handler
- [ ] **EC-ATS-012**: No BambooHR-specific handler

---

## 4. DATA/CONFIG EDGE CASES

### Currently Handled (5)
- [x] Missing config file (warning)
- [x] Missing resume file path
- [x] Empty config values (skips)
- [x] Nested config flattening
- [x] List values (joins with comma)

### Missing - CRITICAL (9)
- [ ] **EC-DATA-001**: No validation of config on startup (required fields)
- [ ] **EC-DATA-002**: No validation of email format
- [ ] **EC-DATA-003**: No validation of phone number format
- [ ] **EC-DATA-004**: No validation of URL formats (LinkedIn, GitHub)
- [ ] **EC-DATA-005**: No validation of resume file existence/type
- [ ] **EC-DATA-006**: No handling for special characters in names (accents, apostrophes)
- [ ] **EC-DATA-007**: No handling for very long values (truncation)
- [ ] **EC-DATA-008**: No config encryption for sensitive data
- [ ] **EC-DATA-009**: No backup/restore of config during updates

---

## 5. BROWSER/AUTOMATION EDGE CASES

### Currently Handled (6)
- [x] Basic CAPTCHA detection (reCAPTCHA, hCAPTCHA)
- [x] Human-like delays
- [x] Human-like typing
- [x] Navigator.webdriver masking
- [x] User agent rotation
- [x] Viewport standardization

### Missing - CRITICAL (11)
- [ ] **EC-BROWSER-001**: No handling for invisible CAPTCHA (score-based)
- [ ] **EC-BROWSER-002**: No integration with CAPTCHA solving services
- [ ] **EC-BROWSER-003**: No handling for bot detection via canvas fingerprinting
- [ ] **EC-BROWSER-004**: No handling for JavaScript challenges (Cloudflare)
- [ ] **EC-BROWSER-005**: No cookie consent popup handling
- [ ] **EC-BROWSER-006**: No newsletter/notification popup dismissal
- [ ] **EC-BROWSER-007**: No handling for session expiration mid-application
- [ ] **EC-BROWSER-008**: No handling for browser crash/restart
- [ ] **EC-BROWSER-009**: No handling for download prompts (job descriptions)
- [ ] **EC-BROWSER-010**: No detection of being blocked/banned
- [ ] **EC-BROWSER-011**: No handling for 2FA prompts on company portals

---

## 6. QUEUE/STATE EDGE CASES

### Currently Handled (4)
- [x] Duplicate URL prevention
- [x] Job status tracking (pending/applied/failed)
- [x] Retry count (max 3)
- [x] Priority ordering

### Missing - CRITICAL (7)
- [ ] **EC-QUEUE-001**: No handling for job being filled/closed during application
- [ ] **EC-QUEUE-002**: No handling for URL redirects (shortened URLs)
- [ ] **EC-QUEUE-003**: No detection of duplicate jobs with different URLs
- [ ] **EC-QUEUE-004**: No resume from crash (in-progress jobs orphaned)
- [ ] **EC-QUEUE-005**: No concurrent application limit per company
- [ ] **EC-QUEUE-006**: No rate limiting per ATS platform
- [ ] **EC-QUEUE-007**: No job expiration detection before applying

---

## 7. FORESIGHT ISSUES (Future Problems)

### 7.1 Scalability Issues
| ID | Issue | Current Impact | Future Impact | Risk |
|----|-------|---------------|---------------|------|
| FS-001 | SQLite single-writer lock | None | High at >100 concurrent | HIGH |
| FS-002 | No connection pooling | None | Memory issues | MEDIUM |
| FS-003 | No job deduplication by content | Minor | Duplicate applications | HIGH |
| FS-004 | Linear job processing | Slow | Very slow at >5000 jobs | MEDIUM |

### 7.2 Data Structure Issues
| ID | Issue | Problem |
|----|-------|---------|
| FS-005 | `Job.url` as unique identifier | URLs change, jobs duplicated |
| FS-006 | No job versioning | Can't track if job was updated |
| FS-007 | Flat config structure | Can't support multiple profiles |
| FS-008 | No application history per company | May apply to same company twice |
| FS-009 | ATS type stored as string | No migration path for new types |

### 7.3 API/Service Changes
| ID | Issue | Impact |
|----|-------|--------|
| FS-010 | SimplifyJobs README format change | Parser breaks |
| FS-011 | Greenhouse DOM structure change | Handler breaks |
| FS-012 | Lever API changes | Handler breaks |
| FS-013 | OpenAI API deprecation | AI answerer fails |
| FS-014 | playwright-stealth detection | Bot blocking |

### 7.4 Security/Compliance
| ID | Issue | Risk |
|----|-------|------|
| FS-015 | No config encryption | Credentials exposed |
| FS-016 | Logs contain PII | Data leak risk |
| FS-017 | No consent tracking | GDPR issues |
| FS-018 | Bot detection evasion | TOS violations |

---

## 8. PRIORITY FIXES (Ordered)

### Critical (Fix Immediately)
1. **EC-FORM-001**: Dynamic form handling
2. **EC-ATS-005/006**: Workday login handling (skip properly)
3. **EC-BROWSER-001**: Invisible CAPTCHA handling
4. **EC-QUEUE-004**: Crash recovery
5. **EC-DATA-001**: Config validation

### High (Fix Soon)
6. **EC-FORM-004**: Date picker handling
7. **EC-FORM-003**: Autocomplete fields
8. **EC-BROWSER-005/006**: Popup handling
9. **EC-NET-003**: CloudFlare detection
10. **FS-003**: Job deduplication

### Medium (Fix Later)
11. **EC-FORM-009**: Phone formatting
12. **EC-NET-005**: Proxy support
13. **EC-ATS-008-012**: Additional ATS handlers
14. **FS-007**: Multiple profile support
15. **EC-BROWSER-002**: CAPTCHA service integration

---

## 9. IMPLEMENTATION TRACKING

```
Status: [ ] Not Started  [~] In Progress  [x] Complete  [-] Won't Fix

Network:
[ ] EC-NET-001  [ ] EC-NET-002  [ ] EC-NET-003  [ ] EC-NET-004
[ ] EC-NET-005  [ ] EC-NET-006  [ ] EC-NET-007  [ ] EC-NET-008

Form:
[ ] EC-FORM-001 [ ] EC-FORM-002 [ ] EC-FORM-003 [ ] EC-FORM-004
[ ] EC-FORM-005 [ ] EC-FORM-006 [ ] EC-FORM-007 [ ] EC-FORM-008
[ ] EC-FORM-009 [ ] EC-FORM-010 [ ] EC-FORM-011 [ ] EC-FORM-012
[ ] EC-FORM-013 [ ] EC-FORM-014 [ ] EC-FORM-015

ATS:
[ ] EC-ATS-001  [ ] EC-ATS-002  [ ] EC-ATS-003  [ ] EC-ATS-004
[ ] EC-ATS-005  [ ] EC-ATS-006  [ ] EC-ATS-007  [ ] EC-ATS-008
[ ] EC-ATS-009  [ ] EC-ATS-010  [ ] EC-ATS-011  [ ] EC-ATS-012

Data:
[ ] EC-DATA-001 [ ] EC-DATA-002 [ ] EC-DATA-003 [ ] EC-DATA-004
[ ] EC-DATA-005 [ ] EC-DATA-006 [ ] EC-DATA-007 [ ] EC-DATA-008
[ ] EC-DATA-009

Browser:
[ ] EC-BROWSER-001 [ ] EC-BROWSER-002 [ ] EC-BROWSER-003 [ ] EC-BROWSER-004
[ ] EC-BROWSER-005 [ ] EC-BROWSER-006 [ ] EC-BROWSER-007 [ ] EC-BROWSER-008
[ ] EC-BROWSER-009 [ ] EC-BROWSER-010 [ ] EC-BROWSER-011

Queue:
[ ] EC-QUEUE-001 [ ] EC-QUEUE-002 [ ] EC-QUEUE-003 [ ] EC-QUEUE-004
[ ] EC-QUEUE-005 [ ] EC-QUEUE-006 [ ] EC-QUEUE-007

Foresight:
[ ] FS-001 [ ] FS-002 [ ] FS-003 [ ] FS-004 [ ] FS-005
[ ] FS-006 [ ] FS-007 [ ] FS-008 [ ] FS-009 [ ] FS-010
[ ] FS-011 [ ] FS-012 [ ] FS-013 [ ] FS-014 [ ] FS-015
[ ] FS-016 [ ] FS-017 [ ] FS-018
```

---

## 10. CURRENT OBSERVED FAILURES

| Date | Job | ATS | Error | Edge Case ID |
|------|-----|-----|-------|--------------|
| 2026-01-10 | Visier | Greenhouse | CAPTCHA blocked | EC-BROWSER-001 |
| 2026-01-10 | STR | Greenhouse | CAPTCHA blocked | EC-BROWSER-001 |
| 2026-01-10 | Unity | Greenhouse | No submit button (embed) | EC-FORM-012 |
| 2026-01-10 | Verkada | Lever | Job closed (404) | EC-QUEUE-001 |
| 2026-01-10 | SteerBridge | Lever | URL has /apply suffix already | EC-QUEUE-002 |
| 2026-01-10 | Manulife | Workday | Requires login | EC-ATS-006 |
| 2026-01-10 | A Thinking Ape | Greenhouse | CAPTCHA blocked | EC-BROWSER-001 |

---

*Document generated: 2026-01-10*
*Total edge cases identified: 100 (38 handled, 62 missing)*
*Critical issues requiring immediate attention: 25*
