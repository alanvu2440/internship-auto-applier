# Browser Architecture Tradeoffs

> Last updated: 2026-03-14
> Decision status: DECIDED — Option A implemented (dual browser)

---

## The Problem

nodriver and Playwright both talk to the same Chrome via CDP (DevTools Protocol). Two drivers, one steering wheel = crashes. 60-100% crash rate on GH/Lever/Ashby. SmartRecruiters uses nodriver alone = 90% success.

## Test Results (2026-03-14)

| Template | Rate | Driver | Crashes |
|----------|------|--------|---------|
| SmartRecruiters | **90%** | nodriver direct | 0 |
| Greenhouse | **30%** | Playwright via CDP to nodriver | 6/10 |
| Lever | **0%** | Playwright via CDP to nodriver | 3/3 |
| Ashby | **0%** | Playwright via CDP to nodriver | 4/4 |

---

## The Five Options

### Option A: Two Persistent Browsers ⭐ RECOMMENDED

```
┌─────────────────────────────────────┐
│         SESSION LIFECYCLE           │
│                                     │
│  Browser 1: nodriver Chrome         │
│    └── Simplify extension loaded    │
│    └── Keeper tab (stays alive)     │
│    └── Used for: SmartRecruiters    │
│    └── CDP: nodriver only           │
│                                     │
│  Browser 2: Playwright Chrome       │
│    └── Simplify extension loaded    │
│    └── Keeper tab (stays alive)     │
│    └── Used for: GH/Lever/Ashby    │
│    └── CDP: Playwright only         │
│                                     │
│  Bot applies one job at a time:     │
│    SR job → opens tab in Browser 1  │
│    GH job → opens tab in Browser 2  │
│    Only one actively filling        │
│    Other browser idle (50MB RAM)    │
└─────────────────────────────────────┘
```

| Criterion | Rating |
|-----------|--------|
| Success rate | **90-95%** (each driver in its proven lane) |
| Simplify | **Both browsers** (--load-extension on each) |
| Dev effort | **4-6 hours** |
| Handler changes | **Zero** |
| Risk | **Very low** |
| Windows | **2** (only 1 active at a time) |
| Tabs stay open | **Yes** (both browsers persist) |

### Option B: One nodriver Chrome + Adapter

```
┌─────────────────────────────────────┐
│  Single nodriver Chrome             │
│    └── Simplify extension           │
│    └── NodriverPage adapter         │
│         └── wraps Tab → Page API    │
│         └── page.fill() → JS eval  │
│         └── page.goto() → tab.get  │
│         └── iframes → ??? (hard)   │
│    └── ALL handlers use adapter     │
└─────────────────────────────────────┘
```

| Criterion | Rating |
|-----------|--------|
| Success rate | **70-85%** initially (adapter bugs) |
| Simplify | **Yes** |
| Dev effort | **1-2 weeks** |
| Handler changes | **Zero** (adapter absorbs) |
| Risk | **High** (Playwright API surface is huge) |
| Windows | **1** |
| Tabs stay open | **Yes** |

**Problem**: Playwright's auto-waiting, iframe traversal, and locator chaining are semantic guarantees, not just methods. Replicating on nodriver is a multi-week project with a long tail of edge cases. 122 distinct Playwright API calls in greenhouse.py alone.

### Option C: One nodriver Chrome, Full Handler Rewrite

| Criterion | Rating |
|-----------|--------|
| Success rate | **Unknown** (months to stabilize) |
| Dev effort | **2-4 weeks** (4700 lines of GH alone) |
| Risk | **Very high** |
| Windows | **1** |

Not recommended. Rewriting 6000+ lines of working Playwright handler code is not justified.

### Option D: Playwright-Only (Drop nodriver)

| Criterion | Rating |
|-----------|--------|
| Success rate | **85-95% GH/Lever/Ashby, 0% SR** (DataDome blocks) |
| Dev effort | **2 hours** (but kills SR entirely) |
| Risk | **Medium** (95 SR jobs lost) |
| Windows | **1** |

Not recommended. Sacrifices SmartRecruiters and 95 pending jobs.

### Option E: nodriver Primary + Playwright Fallback

| Criterion | Rating |
|-----------|--------|
| Success rate | **50-70%** (fallback loses session state) |
| Dev effort | **2-4 hours** |
| Risk | **Medium** (band-aid, doesn't fix root cause) |
| Windows | **1-2** (unpredictable) |

Not recommended. Doesn't solve the problem, just masks it.

---

## Comparison Matrix

| | A: Two Browsers | B: Adapter | C: Rewrite | D: PW-Only | E: Fallback |
|--|--|--|--|--|--|
| Success rate | **90-95%** | 70-85% | Unknown | 85% (0% SR) | 50-70% |
| Simplify | **Both** | Yes | Yes | Yes | Partial |
| Dev effort | **4-6 hrs** | 1-2 wks | 2-4 wks | 2 hrs | 2-4 hrs |
| Handler changes | **Zero** | Zero | Full rewrite | SR only | Retry logic |
| Risk | **Very low** | High | Very high | Medium | Medium |
| Windows | 2 | 1 | 1 | 1 | 1-2 |
| Tabs persist | **Yes** | Yes | Yes | Yes | Unclear |

---

## CTO Recommendation: Option A

**Pick Option A.** Here's why:

1. **Solves the crash problem immediately.** No CDP collision = no crashes. This is the correct fix, not a workaround.

2. **Zero handler code changes.** GH's 4700 lines of Playwright code keep working. SR's 1000 lines of nodriver code keep working. Nothing breaks.

3. **Already half-implemented.** `_start_playwright_only()` fallback in browser_manager.py already works standalone. nodriver launch code already works standalone. Just need routing logic.

4. **Two windows is fine.** Bot does one job at a time. Idle browser uses ~50MB RAM. Both stay alive for the session. Both have Simplify.

5. **Option B is a trap.** Looks clean but the Playwright API surface is enormous. Every edge case bug = a failed application. This is a "get to 300 applications" project, not an architecture showcase.

### Future: Option B if desired

Option A doesn't prevent building an adapter later. Once crashes are zero, build adapter incrementally (start with Lever at 700 lines), test one handler at a time, consolidate to one browser. That's a Q3 project.

---

## Implementation Plan (Option A)

```
Step 1: BrowserManager refactor (2 hrs)
  - Split into start_nodriver() and start_playwright()
  - Remove CDP bridge code entirely
  - Both load Simplify via --load-extension
  - Separate profile directories

Step 2: main.py routing (1 hr)
  - SR → nodriver tab (page=None, handler uses nodriver)
  - GH/Lever/Ashby → Playwright page
  - Both browsers start at session begin, stay alive

Step 3: Test (2 hrs)
  - Run 5 GH dry-runs (Playwright path)
  - Run 5 SR dry-runs (nodriver path)
  - Verify Simplify loads in both
  - Verify tabs persist on failure

Step 4: Full batch test
  - 10 jobs mixed ATS types
  - Target: 80%+ across all templates
```
