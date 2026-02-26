# Question Answering System Deep Dive

> How every application question is detected, answered, verified, and logged.
> Last updated: 2026-02-24

---

## PART A: Answer Chain Overview

Every question encountered during a job application flows through this chain:

```
Question detected by ATS handler
  │
  ▼
┌──────────────────────────────────────────┐
│ 1. Config Patterns (730+ regex)          │  confidence: 100%
│    _get_config_answer()                  │  source: "config"
│    FREE — instant — no API call          │
└───────────────┬──────────────────────────┘
                │ no match
                ▼
┌──────────────────────────────────────────┐
│ 2. Option Matcher (dropdowns/radios)     │  confidence: 100%
│    _match_option_from_config()           │  source: "config_option"
│    Maps config values → dropdown choices │
└───────────────┬──────────────────────────┘
                │ no match
                ▼
┌──────────────────────────────────────────┐
│ 3. Verified Answers DB                   │  confidence: 100%
│    question_verifier.get_verified()      │  source: "verified_*"
│    Human-approved answers from review    │
└───────────────┬──────────────────────────┘
                │ no match
                ▼
┌──────────────────────────────────────────┐
│ 4. Answer Cache (JSON file)              │  confidence: 90%
│    self._answer_cache[cache_key]         │  source: "cache"
│    Previously generated AI answers       │
└───────────────┬──────────────────────────┘
                │ no cache hit
                ▼
┌──────────────────────────────────────────┐
│ 5. Primary Gemini API (free tier)        │  confidence: 80%
│    gemini-2.0-flash                      │  source: "ai"
│    15s timeout, 2 retries                │
└───────────────┬──────────────────────────┘
                │ 429/quota error
                ▼
┌──────────────────────────────────────────┐
│ 6. Backup Gemini API (GCP $300)          │  confidence: 80%
│    Same model, paid key                  │  source: "ai_backup"
│    Budget-capped, tracked per-call       │
└───────────────┬──────────────────────────┘
                │ also fails
                ▼
┌──────────────────────────────────────────┐
│ 7. Generic Fallback                      │  confidence: 0%
│    _generate_generic_answer()            │  source: "generic_fallback"
│    Template-based, QUEUED FOR REVIEW     │
└──────────────────────────────────────────┘

ALL PATHS:
  → Log to data/question_knowledge_base.md
  → Track in session_answers[]
  → Low-confidence → queue for human review
```

---

## PART B: Config Pattern System

### How It Works

`_get_config_answer()` in `ai_answerer.py` contains 730+ regex patterns organized by question type:

```python
patterns = [
    # LinkedIn URL
    (r"(linkedin)\s*(profile|url|page)?\s*(url)?",
     personal.get("linkedin", "")),

    # Work authorization
    (r"(authorized|eligible|legally).*(work|employ).*(u\.?s|united states|america)",
     "Yes" if work_auth.get("authorized_us") else "No"),

    # ... 728 more patterns
]
```

### Pattern Categories

| Category | # Patterns | Examples |
|----------|-----------|----------|
| Personal info | ~50 | Name, email, phone, address, LinkedIn, GitHub |
| Work authorization | ~40 | US citizen, visa status, sponsorship, I-9 |
| Education | ~60 | School, degree, GPA, graduation date, major |
| Experience | ~30 | Current title, years of experience, prior internships |
| Skills | ~20 | Programming languages, frameworks, proficiency |
| Availability | ~30 | Start date, internship term, relocation, travel |
| Demographics | ~30 | Gender, race, veteran, disability, pronouns |
| Essay questions | ~20 | Why interested, tell me about yourself, strengths |
| Yes/No questions | ~100 | Authorization, agreements, eligibility, requirements |
| How did you hear | ~15 | Referral source, recruiting events |
| Location | ~20 | City, state, zip, current location, willingness to relocate |
| Salary | ~10 | Compensation expectations |
| Screening | ~30 | Drug test, background check, export control |
| Misc | ~275 | Edge cases, ATS-specific quirks |

### Config Sources

Patterns pull answers from these `master_config.yaml` sections:

```yaml
personal_info:
  first_name, last_name, email, phone, linkedin, github, portfolio, ...

education:
  - school, degree, field_of_study, gpa, graduation_date, ...

experience:
  - company, title, start_date, end_date, description, ...

work_authorization:
  us_citizen, authorized_us, require_sponsorship_now, visa_status, ...

skills:
  programming_languages, frameworks, tools, years_of_coding_experience, ...

common_answers:
  why_interested_in_company, tell_me_about_yourself, greatest_strength, ...

demographics:
  gender, ethnicity, veteran_status, disability_status, pronouns, ...

availability:
  earliest_start_date, preferred_start_date, timezone, ...

screening:
  drug_test, background_check, travel_percentage, ...
```

---

## PART C: Answer Caching

### How It Works

After any AI-generated answer, it's cached to `data/answer_cache.json`:

```python
# Cache key = normalized question + field type + sorted options
cache_key = "what programming languages do you know?|text"

# Stored as:
{
    "what programming languages do you know?|text": "Python, JavaScript, Java, C++, SQL",
    "are you authorized to work in the us?|radio|no|yes": "Yes",
    ...
}
```

### Benefits

- Identical questions across companies get instant answers
- No API call needed
- Saves Gemini quota
- Consistency: same question always gets same answer

---

## PART D: Human-in-the-Loop Verification

### The Problem

Generic fallback answers are low quality:
```
Q: "Describe your experience with distributed systems"
A: "I am excited about this opportunity and believe my skills align well..."
   ^^^ This is terrible. It doesn't answer the question.
```

### The Solution: Question Verifier

New module `src/question_verifier.py` with SQLite-backed storage:

```
data/verified_answers.db
├── verified_answers table — approved answers
│   ├── question_text, answer, confidence, source
│   ├── verified_by (human / auto)
│   └── verified_at (timestamp)
│
└── review_queue table — pending human review
    ├── question_text, proposed_answer, source
    ├── confidence (0 = needs review)
    └── status (pending / approved / rejected)
```

### Verification Flow

```
Answer generated with low confidence
  │
  ▼
queue_for_review(question, answer, source, confidence=0)
  │
  ▼
Human runs: python src/main.py review-questions
  │
  ├── Shows each pending question + proposed answer
  ├── User types: (a)pprove / (e)dit / (r)eject / (s)kip
  │
  ├── Approve → store in verified_answers (confidence=100, verified_by=human)
  ├── Edit → user types new answer → store as verified
  ├── Reject → marked as rejected, won't be used
  └── Skip → stays in queue for later
  │
  ▼
Next time same question appears:
  → Verified answer returned instantly (step 3 in chain)
  → confidence: 100%
  → source: "verified_config" or "verified_ai"
```

### Confidence Tiers

| Tier | Score | Source | Review Needed? |
|------|-------|--------|---------------|
| Config match | 100 | Regex pattern in config | No — always trusted |
| Human verified | 100 | Approved via review | No — human said it's good |
| Cached AI | 90 | Previous Gemini answer | Optional — spot check |
| Fresh AI | 80 | New Gemini answer | Optional — usually good |
| Generic fallback | 0 | Template answer | **YES — always review** |

---

## PART E: Question Types Encountered

### Text Input (Short Answer)

```
Examples:
- "LinkedIn Profile URL"          → config: personal_info.linkedin
- "Phone Number"                  → config: personal_info.phone
- "Expected Graduation Date"      → config: education.graduation_date
- "Years of experience with X"    → config: skills lookup
```

### Textarea (Long Answer)

```
Examples:
- "Why are you interested in this role?"  → config: common_answers.why_interested
- "Tell me about a project you're proud of" → config: common_answers.proud_project
- "Cover letter"                  → AI-generated per company
- "Additional information"        → config: common_answers.additional_info
```

### Dropdown (Select)

```
Examples:
- Work authorization status       → match config value to options
- Education level                 → match "Bachelor's" to option list
- Gender / Race / Ethnicity       → match config demographics
- How did you hear about us       → "LinkedIn" or "Online Job Board"
- State / Country                 → match config location
```

### Radio Buttons (Yes/No, Multiple Choice)

```
Examples:
- "Are you authorized to work in the US?"     → Yes
- "Do you require sponsorship?"               → No
- "Are you 18 or older?"                      → Yes
- "Do you have a valid driver's license?"     → Yes
- "Are you willing to relocate?"              → Yes
```

### Checkbox (Acknowledgements)

```
Examples:
- "I agree to the privacy policy"             → Check
- "I certify the information is accurate"     → Check
- "I have read the job description"           → Check
```

---

## PART F: CLI Review Interface

```bash
$ python src/main.py review-questions

╔══════════════════════════════════════════════════════════════╗
║                    Question Review Queue                      ║
║                  3 questions pending review                   ║
╚══════════════════════════════════════════════════════════════╝

─── Question 1 of 3 ──────────────────────────────────────────

  Q: Describe your experience with distributed systems
  Proposed: I am excited about this opportunity and believe
            my skills align well with this role.
  Source: generic_fallback | Company: Google | Type: textarea

  [a]pprove  [e]dit  [r]eject  [s]kip  [q]uit

  > e
  Enter your answer (press Enter twice to finish):
  > I built distributed data pipelines at Kruiz that ingested
  > 831K+ records across 7 hotel chains into PostgreSQL, using
  > async workers with Redis for job coordination.
  >

  ✓ Saved as verified answer (confidence: 100%)

─── Question 2 of 3 ──────────────────────────────────────────
...
```

---

## PART G: Knowledge Base

All questions are logged to `data/question_knowledge_base.md`:

```markdown
**Q:** Are you authorized to work in the United States?
**A:** Yes
**Source:** config | **Type:** radio | **Company:** Google

---

**Q:** Why are you interested in this role at Meta?
**A:** I'm drawn to this role because it aligns with my...
**Source:** ai | **Type:** textarea | **Company:** Meta

---

**Q:** Describe your experience with cloud computing
**A:** I am excited about this opportunity...
**Source:** generic_fallback | **Type:** textarea | **Company:** Amazon
```

### Reviewing the KB

```bash
# Find all bad generic answers
grep "generic_fallback" data/question_knowledge_base.md

# Count questions by source
grep "Source:" data/question_knowledge_base.md | sort | uniq -c | sort -rn
```
