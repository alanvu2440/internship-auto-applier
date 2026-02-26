#!/usr/bin/env python3
"""
ATS Analysis Script

Fetches the SimplifyJobs Summer 2026 internship README, parses all jobs,
and produces a full breakdown of ATS types, counts, percentages, and
direct-apply vs login-required stats.
"""

import re
import sys
from collections import Counter, defaultdict
from urllib.parse import urlparse

import httpx


# ---------------------------------------------------------------------------
# ATS definitions (mirroring job_parser.py ATSType enum)
# ---------------------------------------------------------------------------

ATS_PATTERNS = {
    "greenhouse": [
        r"boards\.greenhouse\.io",
        r"job-boards\.greenhouse\.io",
        r"greenhouse\.io",
    ],
    "lever": [
        r"jobs\.lever\.co",
        r"lever\.co",
    ],
    "workday": [
        r"myworkdayjobs\.com",
        r"\.wd\d+\.myworkdayjobs\.com",
    ],
    "ashby": [
        r"jobs\.ashbyhq\.com",
        r"ashbyhq\.com",
    ],
    "bamboohr": [
        r"\.bamboohr\.com",
    ],
    "icims": [
        r"careers-.*\.icims\.com",
        r"\.icims\.com",
    ],
    "smartrecruiters": [
        r"jobs\.smartrecruiters\.com",
    ],
    "jobvite": [
        r"jobs\.jobvite\.com",
        r"\.jobvite\.com",
    ],
}

# Additional well-known ATS/platform patterns NOT in the enum (for discovery)
EXTRA_PLATFORM_PATTERNS = {
    "taleo": [r"taleo\.net", r"oracle\.taleo"],
    "successfactors": [r"successfactors\.com", r"jobs\.sap\.com"],
    "linkedin": [r"linkedin\.com/jobs"],
    "indeed": [r"indeed\.com"],
    "dayforce": [r"dayforce\.com"],
    "ultipro": [r"ultipro\.com", r"recruiting\.ultipro"],
    "paylocity": [r"paylocity\.com"],
    "paycom": [r"paycom\.com"],
    "ceridian": [r"ceridian\.com"],
    "breezyhr": [r"breezy\.hr"],
    "jazz": [r"jazz\.co", r"applytojob\.com"],
    "rippling": [r"ats\.rippling\.com"],
    "myworkday": [r"myworkday\.com"],  # different from myworkdayjobs
    "avature": [r"avature\.net"],
    "phenom": [r"phenom\.com"],
}

# Which ATS types have dedicated handlers (from handlers/ directory)
HANDLED_ATS = {"greenhouse", "lever", "workday"}  # dedicated .py files
# generic.py serves as fallback for everything else

# Direct-apply patterns (no login wall)
DIRECT_APPLY_PATTERNS = [
    "boards.greenhouse.io",
    "jobs.lever.co",
    "job-boards.greenhouse.io",
]

# Login-required patterns
LOGIN_REQUIRED_PATTERNS = [
    "myworkdayjobs.com",
    "linkedin.com",
    "indeed.com",
    "careers.",
    "icims.com",
    "taleo.",
    "successfactors.",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def detect_ats(url: str) -> str:
    """Detect ATS type from URL, returns lowercase name or 'unknown'."""
    if not url:
        return "unknown"
    url_lower = url.lower()
    for ats_name, patterns in ATS_PATTERNS.items():
        for pat in patterns:
            if re.search(pat, url_lower):
                return ats_name
    return "unknown"


def detect_extra_platform(url: str) -> "str | None":
    """Try to detect platforms beyond the main ATS_PATTERNS."""
    if not url:
        return None
    url_lower = url.lower()
    for name, patterns in EXTRA_PLATFORM_PATTERNS.items():
        for pat in patterns:
            if re.search(pat, url_lower):
                return name
    return None


def classify_login(url: str) -> str:
    """Classify as 'direct_apply', 'login_required', or 'uncertain'."""
    url_lower = url.lower()
    for pat in DIRECT_APPLY_PATTERNS:
        if pat in url_lower:
            return "direct_apply"
    for pat in LOGIN_REQUIRED_PATTERNS:
        if pat in url_lower:
            return "login_required"
    return "uncertain"


def extract_domain(url: str) -> str:
    """Extract the domain from a URL."""
    try:
        parsed = urlparse(url)
        return parsed.netloc.lower()
    except Exception:
        return url


# ---------------------------------------------------------------------------
# README Parsing (self-contained, no project imports needed)
# ---------------------------------------------------------------------------

URL_HREF_RE = re.compile(r'href="([^"]+)"')
TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    """Remove HTML tags and collapse whitespace."""
    return TAG_RE.sub("", text).strip()


def parse_readme_jobs(content: str) -> list[dict]:
    """
    Parse the SimplifyJobs README.md (HTML table format).

    The README contains multiple <table> sections. Each data row is:
        <tr>
        <td>Company (with optional <a>, <strong>, emojis)</td>
        <td>Role</td>
        <td>Location</td>
        <td>Application link(s)</td>
        <td>Age</td>
        </tr>

    Returns a list of dicts:
      {company, role, location, url, is_closed}
    """
    jobs = []
    lines = content.split("\n")
    current_company = ""

    i = 0
    while i < len(lines):
        line = lines[i].strip()

        # Look for start of a table row
        if line != "<tr>":
            i += 1
            continue

        # Collect all lines until </tr>
        row_lines = []
        i += 1
        while i < len(lines) and lines[i].strip() != "</tr>":
            row_lines.append(lines[i].strip())
            i += 1
        i += 1  # skip the </tr>

        # Extract <td> cells -- each <td>...</td> is on one line
        cells = []
        for rl in row_lines:
            if rl.startswith("<td"):
                # Extract content between <td...> and </td>
                m = re.match(r"<td[^>]*>(.*?)</td>", rl, re.DOTALL)
                if m:
                    cells.append(m.group(1))
                else:
                    # <td> without closing on same line (shouldn't happen but handle)
                    cells.append(rl)

        # Skip header rows (<th>) or rows with too few cells
        if len(cells) < 4:
            continue

        # Cell 0: Company
        company_raw = cells[0]
        company_text = _strip_html(company_raw)
        # Remove emojis
        company_text = re.sub(
            r"[\U0001f525\U0001f6c2\U0001f1fa\U0001f1f8\U0001f512\U0001f393]",
            "", company_text
        ).strip()

        if company_text in ("\u21b3", ""):
            company = current_company
        else:
            # Try to extract from <a> tag first
            a_match = re.search(r'<a[^>]*>([^<]+)</a>', company_raw)
            if a_match:
                company = a_match.group(1).strip()
            else:
                company = company_text
            current_company = company

        # Cell 1: Role
        role = _strip_html(cells[1])

        # Cell 2: Location
        location = _strip_html(cells[2])

        # Cell 3: Application link(s) -- extract URL
        app_cell = cells[3]
        is_closed = "\U0001f512" in app_cell or "Closed" in app_cell

        all_hrefs = URL_HREF_RE.findall(app_cell)
        url = ""
        for href in all_hrefs:
            if "simplify.jobs" not in href:
                url = href
                break
        # If all links are simplify, use the first one
        if not url and all_hrefs:
            url = all_hrefs[0]

        # Strip tracking params from URL for cleaner detection
        # (but keep workday params since they are part of the URL)
        clean_url = url.split("?utm_source=")[0] if "?utm_source=" in url else url

        if not company or not role:
            continue

        jobs.append({
            "company": company,
            "role": role,
            "location": location,
            "url": clean_url,
            "is_closed": is_closed,
        })

    return jobs


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    readme_url = "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/README.md"

    print("Fetching SimplifyJobs Summer 2026 README...")
    resp = httpx.get(readme_url, follow_redirects=True, timeout=30)
    resp.raise_for_status()
    content = resp.text
    print(f"  Downloaded {len(content):,} bytes\n")

    # Parse
    all_jobs = parse_readme_jobs(content)
    open_jobs = [j for j in all_jobs if not j["is_closed"]]
    closed_jobs = [j for j in all_jobs if j["is_closed"]]

    print("=" * 70)
    print("SIMPLIFY JOBS SUMMER 2026 INTERNSHIP - ATS ANALYSIS")
    print("=" * 70)
    print(f"\nTotal rows parsed:  {len(all_jobs)}")
    print(f"  Open jobs:        {len(open_jobs)}")
    print(f"  Closed jobs:      {len(closed_jobs)}")

    # ------------------------------------------------------------------
    # 1. ATS breakdown (open jobs only -- closed ones have no URL)
    # ------------------------------------------------------------------
    ats_counter = Counter()
    login_counter = Counter()
    unknown_domains = Counter()
    extra_platforms = Counter()
    no_url_count = 0

    for job in open_jobs:
        url = job["url"]
        if not url:
            no_url_count += 1
            ats_counter["(no url)"] += 1
            continue

        ats = detect_ats(url)
        ats_counter[ats] += 1
        login_counter[classify_login(url)] += 1

        if ats == "unknown":
            domain = extract_domain(url)
            unknown_domains[domain] += 1
            extra = detect_extra_platform(url)
            if extra:
                extra_platforms[extra] += 1

    total_open = len(open_jobs)

    print(f"\n{'=' * 70}")
    print("ATS TYPE BREAKDOWN (open jobs)")
    print(f"{'=' * 70}")
    print(f"{'ATS Type':<22} {'Count':>7} {'Percent':>9}")
    print("-" * 40)
    for ats, count in ats_counter.most_common():
        pct = count / total_open * 100
        marker = ""
        if ats not in ("unknown", "(no url)") and ats not in HANDLED_ATS:
            marker = "  [no dedicated handler]"
        elif ats in HANDLED_ATS:
            marker = "  [handler: {}.py]".format(ats)
        print(f"  {ats:<20} {count:>5}   {pct:>6.1f}%{marker}")
    print("-" * 40)
    print(f"  {'TOTAL':<20} {total_open:>5}   100.0%")

    # ------------------------------------------------------------------
    # 2. Direct Apply vs Login Required
    # ------------------------------------------------------------------
    print(f"\n{'=' * 70}")
    print("DIRECT APPLY vs LOGIN REQUIRED (open jobs with URLs)")
    print(f"{'=' * 70}")
    jobs_with_url = total_open - no_url_count
    for cat in ["direct_apply", "login_required", "uncertain"]:
        cnt = login_counter.get(cat, 0)
        pct = cnt / jobs_with_url * 100 if jobs_with_url else 0
        label = {
            "direct_apply": "Direct apply (no login)",
            "login_required": "Login required",
            "uncertain": "Uncertain / other",
        }[cat]
        print(f"  {label:<30} {cnt:>5}   {pct:>6.1f}%")
    print(f"  {'(no URL / closed links)':<30} {no_url_count:>5}")

    # ------------------------------------------------------------------
    # 3. ATS types WITHOUT dedicated handlers
    # ------------------------------------------------------------------
    known_ats_names = set(ATS_PATTERNS.keys())
    handled = HANDLED_ATS
    unhandled = known_ats_names - handled

    print(f"\n{'=' * 70}")
    print("HANDLER COVERAGE")
    print(f"{'=' * 70}")
    print(f"\n  ATS types with DEDICATED handlers ({len(handled)}):")
    for name in sorted(handled):
        cnt = ats_counter.get(name, 0)
        print(f"    - {name:<20} ({cnt} open jobs)")

    print(f"\n  ATS types WITHOUT dedicated handlers ({len(unhandled)}) -- uses generic.py:")
    for name in sorted(unhandled):
        cnt = ats_counter.get(name, 0)
        print(f"    - {name:<20} ({cnt} open jobs)")

    # ------------------------------------------------------------------
    # 4. Platforms detected in 'unknown' URLs
    # ------------------------------------------------------------------
    if extra_platforms:
        print(f"\n{'=' * 70}")
        print("ADDITIONAL PLATFORMS DETECTED IN 'UNKNOWN' URLs")
        print(f"{'=' * 70}")
        for plat, cnt in extra_platforms.most_common():
            print(f"  {plat:<25} {cnt:>5}")

    # ------------------------------------------------------------------
    # 5. Top domains in 'unknown' category
    # ------------------------------------------------------------------
    if unknown_domains:
        print(f"\n{'=' * 70}")
        print("TOP 30 DOMAINS IN 'UNKNOWN' CATEGORY")
        print(f"{'=' * 70}")
        for domain, cnt in unknown_domains.most_common(30):
            extra = detect_extra_platform(f"https://{domain}/")
            tag = f"  [{extra}]" if extra else ""
            print(f"  {domain:<45} {cnt:>4}{tag}")

    # ------------------------------------------------------------------
    # 6. Summary stats
    # ------------------------------------------------------------------
    known_ats_count = sum(c for name, c in ats_counter.items()
                         if name not in ("unknown", "(no url)"))
    unknown_count = ats_counter.get("unknown", 0)

    print(f"\n{'=' * 70}")
    print("SUMMARY")
    print(f"{'=' * 70}")
    if total_open > 0:
        print(f"  Open jobs with recognized ATS: {known_ats_count} "
              f"({known_ats_count / total_open * 100:.1f}%)")
        print(f"  Open jobs with unknown ATS:    {unknown_count} "
              f"({unknown_count / total_open * 100:.1f}%)")
    else:
        print(f"  Open jobs with recognized ATS: {known_ats_count}")
        print(f"  Open jobs with unknown ATS:    {unknown_count}")
    print(f"  Open jobs with no URL:         {no_url_count}")

    direct = login_counter.get("direct_apply", 0)
    if total_open > 0:
        print(f"\n  Direct-apply (automatable):    {direct} "
              f"({direct / total_open * 100:.1f}%)")
        handled_count = sum(ats_counter.get(h, 0) for h in handled)
        print(f"  Jobs with dedicated handler:   {handled_count} "
              f"({handled_count / total_open * 100:.1f}%)")
    else:
        print(f"\n  Direct-apply (automatable):    {direct}")
        handled_count = sum(ats_counter.get(h, 0) for h in handled)
        print(f"  Jobs with dedicated handler:   {handled_count}")


if __name__ == "__main__":
    main()
