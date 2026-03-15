# System Architecture v2

> Last updated: 2026-03-14
> Status: Production — dual browser, golden path enforced

---

## 1. Full Application Flow

```
┌──────────────────────────────────────────────────────────┐
│                    JOB DISCOVERY                         │
│  GitHub API → job_parser.py → job_queue.py → SQLite     │
└──────────────────────┬───────────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────────┐
│                  PRE-APPLY CHECKS                        │
│                                                          │
│  1. Interview check — active interview? SKIP company     │
│  2. Company limit — 3+ apps at company? SKIP             │
│  3. Role priority — SWE/AI/ML/Data first, then others    │
│  4. Rate limit — 10/hr, 90s delays, 40/day max           │
└──────────────────────┬───────────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────────┐
│                  DUAL BROWSER                            │
│                                                          │
│  Browser 1: nodriver Chrome (SmartRecruiters only)       │
│    └── DataDome bypass, Simplify extension loaded        │
│                                                          │
│  Browser 2: Playwright Chrome (GH/Lever/Ashby/WD)       │
│    └── Simplify extension loaded, persistent profile     │
│                                                          │
│  WHY TWO: CDP collision when both drivers share one      │
│  Chrome = 60% crash rate. Separate = zero crashes.       │
└──────────────────────┬───────────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────────┐
│              GOLDEN PATH (per field)                     │
│                                                          │
│  1. Simplify fills first (10s wait)                      │
│     └── If already filled → SKIP (don't override)        │
│  2. Template bank (per-ATS YAML, instant, auto-growing)  │
│  3. Option matcher (dropdowns — derives from config)     │
│  4. Config regex patterns (730+ patterns)                │
│  5. Answer cache                                         │
│  6. Gemini AI (primary → backup failover)                │
│  7. UNSOLVED → leave EMPTY → tab stays open for manual   │
│                                                          │
│  AUTO-LEARN: Steps 2-6 save new Q&A to template bank    │
└──────────────────────┬───────────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────────┐
│                  SUBMIT FLOW                             │
│                                                          │
│  Pre-submit guard checks required fields                 │
│  If empty → DON'T submit → leave tab open                │
│  If Simplify already submitted → detect "Thank you"      │
│  Success → screenshot → 10s wait → close tab             │
│  Failure → screenshot → leave tab open for manual        │
└──────────────────────────────────────────────────────────┘
```

## 2. Rate Limiting

| Setting | Value |
|---------|-------|
| Max per day | 40 |
| Max per hour | 10 |
| Base delay | 60s |
| GH delay | 90s |
| SR delay | 90s |
| Lever delay | 120s |
| Ashby delay | 180s |
| Workday delay | 120s |
| Company cooldown | 120s min |
| Max per company | 3 roles |
| Interview block | Any active interview → skip company |

## 3. Browser Architecture

```
nodriver Chrome ← SmartRecruiters (DataDome bypass)
  └── Profile: nodriver_profile/
  └── Simplify loaded

Playwright Chrome ← GH/Lever/Ashby/Workday/Generic
  └── Profile: extension_default/ (has Simplify login)
  └── Simplify loaded

Both stay alive all session. One job at a time.
Tabs close on success. Failures stay open.
```

## 4. Template Banks (Auto-Learning)

```
config/question_banks/
├── greenhouse.yaml      940+ Qs (auto-growing)
├── smartrecruiters.yaml 160+ Qs
├── lever.yaml           98+ Qs
├── workday.yaml         62+ Qs
└── common.yaml          214+ Qs
```

Every successful answer auto-appended to the right YAML file.

## 5. Key Files

| File | Purpose |
|------|---------|
| `src/main.py` | Orchestrator, CLI, rate limiting, pre-apply checks |
| `src/browser_manager.py` | Dual browser (nodriver + Playwright) |
| `src/ai_answerer.py` | Golden path + auto-learn |
| `src/form_filler.py` | Form filling (preserves Simplify values) |
| `src/form/option_matcher.py` | Dropdown answer derivation |
| `src/handlers/*.py` | Per-ATS handlers |
| `src/handlers/base.py` | Simplify integration + pre-submit guard |
| `config/question_banks/*.yaml` | Template banks |
| `config/master_config.yaml` | Personal profile (NEVER commit) |
| `config/secrets.yaml` | API keys (NEVER commit) |
