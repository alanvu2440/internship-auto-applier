# Internship Auto-Applier — System Architecture

## Full System Overview

```mermaid
flowchart TB
    subgraph FETCH["1. JOB DISCOVERY"]
        GH[("GitHub\nSimplifyJobs/Summer2026")]
        GW["GitHubWatcher\n(poll for changes)"]
        JP["JobParser\n(parse README table)"]
        GH -->|raw README.md| GW
        GW -->|new content| JP
    end

    subgraph QUEUE["2. JOB QUEUE (SQLite)"]
        DB[("data/jobs.db")]
        JP -->|parsed jobs| DB
        DB -->|"next pending\n(priority + ATS order)"| LOOP
    end

    subgraph LOOP["3. APPLICATION LOOP"]
        direction TB
        NJ["Get Next Job"]
        BL{"Blacklisted?\nLogin-required?"}
        NJ --> BL
        BL -->|yes| SKIP["Mark Skipped"]
        BL -->|no| ESC_CHECK{"ESC Monitor:\nManual mode?"}
        ESC_CHECK -->|paused| WAIT_ESC["Wait for ESC\nto resume"]
        WAIT_ESC --> ESC_CHECK
        ESC_CHECK -->|auto| ATJ["apply_to_job()"]
    end

    subgraph APPLY["4. APPLY TO JOB"]
        direction TB
        BROWSER["BrowserManager\n(stealth Chromium)"]
        ROUTE{"ATS Router"}

        BROWSER --> ROUTE
        ROUTE -->|greenhouse| GH_H["GreenhouseHandler"]
        ROUTE -->|lever| LV_H["LeverHandler"]
        ROUTE -->|ashby| AS_H["AshbyHandler\n(API-first)"]
        ROUTE -->|smartrecruiters| SR_H["SmartRecruitersHandler\n(nodriver bypass)"]
        ROUTE -->|workday| WD_H["WorkdayHandler\n(auth + multi-page)"]
        ROUTE -->|unknown| GN_H["GenericHandler"]

        GH_H & LV_H & AS_H & SR_H & WD_H & GN_H --> HANDLER_RESULT{"Handler\nResult"}
    end

    subgraph SMART["5. SMART MODE (on failure)"]
        direction TB
        GFS["GeminiFormScanner"]
        DOM["Pass 1: DOM Extract\n(JS field scan)"]
        VIS["Pass 2: Vision\n(screenshot → Gemini)"]
        FILL["Fill empty fields"]
        SUBMIT_RETRY["Click Submit"]

        GFS --> DOM --> VIS --> FILL --> SUBMIT_RETRY
    end

    subgraph ASSIST["6. ASSIST MODE (ESC or auto-fallback)"]
        direction TB
        NOTIFY["macOS Notification\n+ Terminal Bell"]
        SHOW["Show empty fields\n+ form errors"]
        USER["USER takes over browser"]
        RACE{"Race:\nSubmit detected?\nvs ESC pressed?"}

        NOTIFY --> SHOW --> USER --> RACE
        RACE -->|"submit detected\n(auto)"| APPLIED_A["SUCCESS"]
        RACE -->|ESC pressed| NEXT_JOB["Skip → Next Job"]
        RACE -->|timeout 10m| NEXT_JOB
    end

    subgraph RESULT["7. POST-APPLICATION"]
        direction TB
        SS["Screenshot\n(data/screenshots/)"]
        LOG["ApplicationTracker\n(JSONL + report)"]
        UPDATE_DB["Update DB status"]

        SS --> LOG --> UPDATE_DB
    end

    ATJ --> BROWSER
    HANDLER_RESULT -->|success| SS
    HANDLER_RESULT -->|"fail (no error)"| GFS
    HANDLER_RESULT -->|"ESC interrupt"| NOTIFY
    SUBMIT_RETRY -->|success| SS
    SUBMIT_RETRY -->|still failing| NOTIFY
    APPLIED_A --> SS
    UPDATE_DB -->|delay 30-300s| NJ

    style FETCH fill:#e1f5fe
    style QUEUE fill:#fff3e0
    style LOOP fill:#f3e5f5
    style APPLY fill:#e8f5e9
    style SMART fill:#fff9c4
    style ASSIST fill:#fce4ec
    style RESULT fill:#e0f2f1
```

---

## Question Answering Cascade

```mermaid
flowchart TD
    Q["Question Detected\non form field"]

    C1{"1. Config Patterns\n(730+ regex)"}
    C2{"2. Answer Cache\n(answer_cache.json)"}
    C3{"3. Dropdown Option\nMatching"}
    C4{"4. Primary Gemini\n(free tier)"}
    C5{"5. Backup Gemini\n(GCP $300 credit)"}
    C6["6. Generic Fallback\n(template answers)"]

    Q --> C1
    C1 -->|match| ANS["Answer Found"]
    C1 -->|no match| C2
    C2 -->|cache hit| ANS
    C2 -->|miss| C3
    C3 -->|option matched| ANS
    C3 -->|no match| C4
    C4 -->|success| ANS
    C4 -->|"429 / quota"| C5
    C5 -->|success| ANS
    C5 -->|"fail / over budget"| C6
    C6 --> ANS

    ANS --> LOG["Log to:\n• question_knowledge_base.md\n• answer_cache.json\n• session_answers"]

    style C1 fill:#c8e6c9
    style C2 fill:#c8e6c9
    style C3 fill:#c8e6c9
    style C4 fill:#fff9c4
    style C5 fill:#ffe0b2
    style C6 fill:#ffcdd2
    style ANS fill:#e1f5fe
```

---

## Job Status Lifecycle

```mermaid
stateDiagram-v2
    [*] --> pending: Job discovered
    pending --> in_progress: Bot picks up job
    in_progress --> applied: Form submitted + verified
    in_progress --> failed: Handler error / validation fail
    in_progress --> skipped: Closed / login wall / CAPTCHA

    failed --> pending: Reset (attempts < 3)
    failed --> skipped: Max attempts reached (3x)

    applied --> follow_up: Email: "received"
    applied --> assessment: Email: coding challenge
    applied --> interview_invite: Email: interview
    applied --> offer: Email: offer
    applied --> rejection: Email: rejected

    note right of applied
        Email tracking only upgrades:
        offer > interview > assessment >
        follow_up > rejection
    end note
```

---

## ESC Toggle Flow

```mermaid
sequenceDiagram
    participant User
    participant ESC as EscMonitor
    participant Bot as Handler/Loop
    participant Browser

    Note over Bot: AUTO MODE — Bot filling form
    User->>ESC: Press ESC
    ESC->>Bot: Cancel handler task
    ESC->>User: "MANUAL MODE — Browser is yours"

    Note over User,Browser: USER controls browser
    User->>Browser: Fill fields, fix errors

    alt User submits form
        Browser->>Bot: Page navigates to "thank you"
        Bot->>Bot: Auto-detect submission
        Bot->>User: "SUBMISSION DETECTED!"
        Bot->>Bot: Screenshot + mark applied
    else User presses ESC again
        User->>ESC: Press ESC
        ESC->>Bot: Resume automation
        ESC->>User: "AUTO MODE — Bot resuming"
        Bot->>Bot: Move to next job
    else Timeout (10 min)
        Bot->>Bot: Move to next job
    end
```

---

## Workday Auth Flow

```mermaid
flowchart TD
    START["Navigate to Workday job URL"]

    CHECK{"Login wall\ndetected?"}
    START --> CHECK
    CHECK -->|no| FILL["Fill application form"]
    CHECK -->|yes| AUTH

    subgraph AUTH["Authentication"]
        CREATE["Try Create Account\n(email + password)"]
        SIGNIN["Try Sign In\n(email + password)"]
        VERIFY["Email Verification"]
        GMAIL["EmailVerifier\n(Gmail IMAP)"]
        ACTIVATE["Click activate link"]

        CREATE -->|"already exists"| SIGNIN
        CREATE -->|"account created"| VERIFY
        VERIFY --> GMAIL
        GMAIL -->|"extract link"| ACTIVATE
        ACTIVATE --> SIGNIN
        SIGNIN -->|success| FILL
        SIGNIN -->|"forgot password"| SKIP_WD["Skip Job"]
    end

    FILL --> PAGES["Multi-page form\n(6-7 pages typical)"]
    PAGES --> SUBMIT["Submit Application"]

    style AUTH fill:#fff3e0
```

---

## ATS Handler Comparison

```mermaid
graph LR
    subgraph SUCCESS["High Success ~90%+"]
        GH["Greenhouse\n~95%\nInvisible reCAPTCHA"]
        AS["Ashby\n~95%\nAPI-first"]
        LV["Lever\n~90%\nreCAPTCHA"]
    end

    subgraph MEDIUM["Medium ~85%"]
        SR["SmartRecruiters\n~85%\nDataDome bypass\n(nodriver)"]
    end

    subgraph LOW["Low / Blocked"]
        WD["Workday\n~20%\nLogin required"]
        IC["iCIMS\n0%\nLogin always"]
        UK["Unknown\n~30%\nVaries"]
    end

    style SUCCESS fill:#c8e6c9
    style MEDIUM fill:#fff9c4
    style LOW fill:#ffcdd2
```

---

## Data Flow & File Map

```mermaid
flowchart LR
    subgraph INPUT["Input"]
        CFG["config/master_config.yaml\n(profile + patterns)"]
        SEC["config/secrets.yaml\n(API keys)"]
        RES["config/resume.pdf"]
        TRANS["config/transcript.pdf"]
        CL["config/cover_letter.pdf"]
    end

    subgraph RUNTIME["Runtime Storage"]
        DB[("data/jobs.db\nSQLite")]
        CACHE["data/answer_cache.json"]
        COST["data/gemini_cost_tracker.json"]
        KB["data/question_knowledge_base.md"]
    end

    subgraph OUTPUT["Output"]
        SHOTS["data/screenshots/\nPASS/FAIL per job"]
        APPLOG["logs/applier.log\n(debug, 10MB rotate)"]
        JSONL["logs/running_application_log.jsonl"]
        REPORT["logs/application_report_*.json"]
        RESP["data/response_summary.json"]
    end

    CFG --> RUNTIME
    SEC --> RUNTIME
    RES --> RUNTIME
    RUNTIME --> OUTPUT

    style INPUT fill:#e1f5fe
    style RUNTIME fill:#fff3e0
    style OUTPUT fill:#e0f2f1
```

---

## Smart Mode: Gemini Form Scanner

```mermaid
flowchart TD
    FAIL["Handler.apply() failed\n(no explicit error)"]

    subgraph PASS1["Pass 1: DOM Extraction"]
        JS["Run JS: extract all\nform fields from DOM"]
        FIELDS["Get: label, type, value,\nrequired, selector"]
        EMPTY["Filter: empty required fields"]
        PROMPT1["Send to Gemini 2.5 Flash:\n'Fill these fields with user profile'"]
        FILL1["Fill via page.fill(selector, value)"]

        JS --> FIELDS --> EMPTY --> PROMPT1 --> FILL1
    end

    subgraph PASS2["Pass 2: Vision (if fields remain)"]
        SHOT["Take page screenshot"]
        PROMPT2["Send screenshot to Gemini:\n'What fields still need filling?'"]
        MATCH["Match Gemini output\nto DOM selectors"]
        FILL2["Fill remaining fields"]

        SHOT --> PROMPT2 --> MATCH --> FILL2
    end

    SUBMIT["Click Submit button"]
    CHECK{"Application\ncomplete?"}

    FAIL --> JS
    FILL1 --> SHOT
    FILL2 --> SUBMIT --> CHECK
    CHECK -->|yes| SUCCESS["Mark Applied"]
    CHECK -->|no| ASSIST_MODE["Enter Assist Mode\n(user takes over)"]

    style PASS1 fill:#fff9c4
    style PASS2 fill:#ffe0b2
```
