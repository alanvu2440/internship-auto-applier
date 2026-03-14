# Progress Tracker

> Live dashboard for application progress across all ATS types.
> Last updated: 2026-02-24

---

## Overall Status

| Metric | Count |
|--------|-------|
| Total jobs in DB | ~2,323 |
| Applied (success) | 14+ |
| Failed | varies |
| Skipped | varies |
| Pending | ~2,300 |

> Run `python src/main.py stats` for live numbers.

---

## Per-ATS Breakdown

| ATS | Total | Applied | Failed | Skipped | Pending | Success % | Status |
|-----|-------|---------|--------|---------|---------|-----------|--------|
| Greenhouse | 158 | 14+ | ~5 | ~5 | ~134 | ~95% | **Active — batching** |
| SmartRecruiters | 95 | 0 | 0 | 0 | 95 | ~85% est. | Ready — needs dry run |
| Ashby | 59 | 0 | 0 | 0 | 59 | ~95% est. | Ready — needs dry run |
| Lever | 22 | 0 | 0 | 0 | 22 | ~90% est. | Ready — needs dry run |
| Workday | 1,203 | 0 | 0 | ~1,000 | ~200 | ~20% est. | Blocked — login walls |
| iCIMS | 85 | 0 | 0 | 85 | 0 | 0% | Skipped — needs accounts |
| Unknown/Generic | 701 | 0 | 0 | 0 | 701 | ~30% est. | Low priority |

---

## Batch History

### Batch 1 — Greenhouse (2026-02-24)

- **Target:** Greenhouse pending jobs
- **Mode:** Live submit (`backfill --max 50`)
- **Results:** 14 PASS (so far)
- **Issues:**
  - Some jobs closed between fetch and apply
  - Location typeahead occasionally fails
  - A few custom questions fell to generic fallback
- **Screenshots:** `data/screenshots/` (check for PASS/FAIL naming)

---

## Next Steps

1. **Continue Greenhouse batch** — drain remaining ~134 pending
2. **Dry-run SmartRecruiters** — `backfill --dry-run --ats smartrecruiters --max 5`
3. **Dry-run Ashby** — `backfill --dry-run --ats ashby --max 5`
4. **Dry-run Lever** — `backfill --dry-run --ats lever --max 5`
5. **Review question queue** — `python src/main.py review-questions`
6. **Account strategy for Workday** — see `docs/ACCOUNT_STRATEGY.md`

---

## Monitoring Commands

```bash
# Live progress
tail -f logs/applier.log | grep -E "PROGRESS|PASS|FAIL|WARNING"

# Screenshots (should have files after each submit)
ls -la data/screenshots/

# Question quality check
grep "generic_fallback" data/question_knowledge_base.md

# Pending review questions
python src/main.py review-questions

# Gemini spend
cat data/gemini_cost_tracker.json

# Full stats
python src/main.py stats
```

---

## Success Rate Targets

| ATS | Current | Target | Blocker |
|-----|---------|--------|---------|
| Greenhouse | ~95% | 98% | Location typeahead, edge-case questions |
| SmartRecruiters | ~85% est. | 90% | DataDome stability |
| Ashby | ~95% est. | 98% | API schema changes |
| Lever | ~90% est. | 95% | Custom question coverage |
| Workday | ~20% | 50% | Account creation automation |
| iCIMS | 0% | 30% | Account creation + email verification |
| Generic | ~30% | 40% | Better form detection heuristics |
