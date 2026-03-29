"""
Job Parser Module

Parses the SimplifyJobs README.md to extract job listings.
Extracts company name, role, location, and application URL.
Handles both markdown tables and HTML tables.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional
from enum import Enum
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from loguru import logger


class ATSType(Enum):
    """Known Applicant Tracking Systems."""
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    WORKDAY = "workday"
    ASHBY = "ashby"
    BAMBOOHR = "bamboohr"
    ICIMS = "icims"
    TALEO = "taleo"
    SMARTRECRUITERS = "smartrecruiters"
    JOBVITE = "jobvite"
    UNKNOWN = "unknown"


@dataclass
class Job:
    """Represents a job listing."""
    company: str
    role: str
    location: str
    url: str
    ats_type: ATSType = ATSType.UNKNOWN
    date_added: datetime = field(default_factory=datetime.now)
    is_closed: bool = False
    requires_sponsorship: bool = True  # Assume true unless stated otherwise
    is_us_only: bool = False
    raw_text: str = ""

    def __hash__(self):
        return hash((self.company, self.role, self.url))

    def __eq__(self, other):
        if not isinstance(other, Job):
            return False
        return self.url == other.url


class JobParser:
    """Parses SimplifyJobs README to extract job listings."""

    # Regex patterns for parsing the markdown table
    TABLE_ROW_PATTERN = re.compile(
        r'\|\s*(?:\*\*)?(?:🔥\s*)?(?:\[([^\]]+)\])?(?:\(([^)]+)\))?(?:\*\*)?\s*([^|]*)\s*'  # Company
        r'\|\s*([^|]*)\s*'  # Role
        r'\|\s*([^|]*)\s*'  # Location
        r'\|\s*(?:<a[^>]*href="([^"]+)"[^>]*>)?[^|]*\s*'  # Apply link
        r'\|\s*([^|]*)\s*\|',  # Date/Age
        re.IGNORECASE
    )

    # Alternative simpler pattern
    SIMPLE_ROW_PATTERN = re.compile(
        r'\|\s*(?:🔥\s*)?\*?\*?\[?([^\]\|]+)\]?(?:\([^)]*\))?\*?\*?\s*'  # Company
        r'\|\s*([^|]+)\s*'  # Role
        r'\|\s*([^|]+)\s*'  # Location
        r'\|\s*(?:<a[^>]*href="([^"]+)")?[^|]*\s*'  # URL
        r'\|\s*([^|]*)\s*\|',  # Age
        re.IGNORECASE
    )

    # URL pattern to extract from href
    URL_PATTERN = re.compile(r'href="([^"]+)"')

    # ATS detection patterns
    ATS_PATTERNS = {
        ATSType.GREENHOUSE: [
            r'boards\.greenhouse\.io',
            r'job-boards\.greenhouse\.io',
            r'greenhouse\.io',
            r'weareroku\.com',
            r'careers\.rivian\.com',
            r'app\.careerpuck\.com',
            r'sofi\.com/careers',
            r'ripple\.com/careers',
            r'careers\.formlabs\.com',
            r'ithaka\.org/job',
        ],
        ATSType.LEVER: [
            r'jobs\.lever\.co',
            r'lever\.co',
        ],
        ATSType.WORKDAY: [
            r'myworkdayjobs\.com',
            r'\.wd\d+\.myworkdayjobs\.com',
        ],
        ATSType.ASHBY: [
            r'jobs\.ashbyhq\.com',
            r'ashbyhq\.com',
        ],
        ATSType.BAMBOOHR: [
            r'\.bamboohr\.com',
        ],
        ATSType.ICIMS: [
            r'careers-.*\.icims\.com',
            r'\.icims\.com',
        ],
        ATSType.SMARTRECRUITERS: [
            r'jobs\.smartrecruiters\.com',
        ],
        ATSType.JOBVITE: [
            r'jobs\.jobvite\.com',
            r'\.jobvite\.com',
        ],
    }

    def __init__(self):
        self.jobs: List[Job] = []

    def detect_ats(self, url: str) -> ATSType:
        """Detect the ATS type from the URL."""
        if not url:
            return ATSType.UNKNOWN

        url_lower = url.lower()
        for ats_type, patterns in self.ATS_PATTERNS.items():
            for pattern in patterns:
                if re.search(pattern, url_lower):
                    return ats_type

        return ATSType.UNKNOWN

    def parse_readme(self, content: str) -> List[Job]:
        """
        Parse the SimplifyJobs README content.

        Args:
            content: Raw markdown/HTML content

        Returns:
            List of Job objects
        """
        jobs = []

        # Check if content has HTML tables (some repos use <tr>/<td> without <table>)
        if '<table>' in content.lower() or '<tr>' in content.lower():
            jobs = self._parse_html_tables(content)
        if not jobs:
            jobs = self._parse_markdown_tables(content)

        self.jobs = jobs
        logger.info(f"Parsed {len(jobs)} jobs from README")
        return jobs

    def _parse_html_tables(self, content: str) -> List[Job]:
        """Parse HTML tables from the README."""
        jobs = []
        soup = BeautifulSoup(content, 'lxml')

        # Find all tables, or fall back to all <tr> rows directly
        tables = soup.find_all('table')
        current_company = ""

        if tables:
            all_rows = []
            for table in tables:
                all_rows.extend(table.find_all('tr'))
        else:
            # Some repos use <tr>/<td> without <table> wrapper
            all_rows = soup.find_all('tr')

        for row in all_rows:
                cells = row.find_all('td')
                if len(cells) < 4:
                    continue

                try:
                    # Cell 0: Company (or ↳ for continuation)
                    company_cell = cells[0].get_text(strip=True)
                    if company_cell == '↳':
                        company = current_company
                    else:
                        # Extract company name from link
                        company_link = cells[0].find('a')
                        if company_link:
                            company = company_link.get_text(strip=True)
                        else:
                            company = company_cell
                        current_company = company

                    # Cell 1: Role
                    role = cells[1].get_text(strip=True)

                    # Cell 2: Location
                    location = cells[2].get_text(strip=True)

                    # Cell 3: Application link
                    app_cell = cells[3]
                    app_links = app_cell.find_all('a')

                    # Find the actual job URL (not Simplify link)
                    url = ""
                    for link in app_links:
                        href = link.get('href', '')
                        # Skip Simplify redirect links
                        if 'simplify.jobs/p/' in href:
                            continue
                        if href and ('greenhouse' in href or 'lever' in href or
                                    'workday' in href or 'myworkdayjobs' in href or
                                    'icims' in href or 'jobvite' in href or
                                    'ashby' in href or 'bamboohr' in href or
                                    'careers' in href or 'jobs' in href):
                            url = href
                            break

                    # If no job URL found, try first link
                    if not url and app_links:
                        for link in app_links:
                            href = link.get('href', '')
                            if href and 'simplify.jobs/p/' not in href:
                                url = href
                                break

                    if not url or not company or not role:
                        continue

                    # Remove tracking params for cleaner URL
                    if '?' in url:
                        base_url = url.split('?')[0]
                        # Keep some URLs intact
                        if 'myworkdayjobs' not in url:
                            url = base_url

                    # Detect ATS
                    ats_type = self.detect_ats(url)

                    job = Job(
                        company=company,
                        role=role,
                        location=location,
                        url=url,
                        ats_type=ats_type,
                        is_closed=False,
                    )
                    jobs.append(job)

                except Exception as e:
                    logger.debug(f"Error parsing HTML row: {e}")
                    continue

        return jobs

    def _parse_markdown_tables(self, content: str) -> List[Job]:
        """Parse markdown tables from the README."""
        jobs = []
        lines = content.split('\n')

        in_table = False
        for line in lines:
            if '| Company' in line or '| company' in line.lower():
                in_table = True
                continue

            if in_table and line.strip().startswith('| ---'):
                continue

            if in_table and line.strip().startswith('|'):
                job = self._parse_table_row(line)
                if job:
                    jobs.append(job)

            if in_table and not line.strip().startswith('|') and line.strip():
                if line.strip().startswith('#'):
                    in_table = False

        return jobs

    def _parse_table_row(self, line: str) -> Optional[Job]:
        """Parse a single table row into a Job object."""
        try:
            # Clean the line
            line = line.strip()
            if not line or line.startswith('| ---'):
                return None

            # Check if job is closed
            is_closed = '🔒' in line

            # Split by |
            parts = [p.strip() for p in line.split('|')]
            parts = [p for p in parts if p]  # Remove empty

            if len(parts) < 4:
                return None

            # Extract company name
            company_part = parts[0]
            company = self._extract_company_name(company_part)

            # Extract role
            role = self._clean_text(parts[1])

            # Extract location
            location = self._clean_text(parts[2])

            # Extract URL
            url = ""
            for part in parts:
                url_match = self.URL_PATTERN.search(part)
                if url_match:
                    url = url_match.group(1)
                    break

            # Skip if no URL or closed
            if not url or is_closed:
                return None

            # Check for sponsorship/citizenship requirements
            requires_sponsorship = '🛂' not in line  # 🛂 = no sponsorship
            is_us_only = '🇺🇸' in line

            # Detect ATS
            ats_type = self.detect_ats(url)

            return Job(
                company=company,
                role=role,
                location=location,
                url=url,
                ats_type=ats_type,
                is_closed=is_closed,
                requires_sponsorship=requires_sponsorship,
                is_us_only=is_us_only,
                raw_text=line,
            )

        except Exception as e:
            logger.debug(f"Failed to parse row: {line[:100]}... Error: {e}")
            return None

    def _extract_company_name(self, text: str) -> str:
        """Extract company name from markdown formatted text."""
        # Remove emojis
        text = re.sub(r'[🔥🛂🇺🇸🔒]', '', text)

        # Extract from markdown link [Company](url)
        link_match = re.search(r'\[([^\]]+)\]', text)
        if link_match:
            return self._clean_text(link_match.group(1))

        # Extract from bold **Company**
        bold_match = re.search(r'\*\*([^*]+)\*\*', text)
        if bold_match:
            return self._clean_text(bold_match.group(1))

        return self._clean_text(text)

    def _clean_text(self, text: str) -> str:
        """Clean text by removing markdown and extra whitespace."""
        # Remove markdown formatting
        text = re.sub(r'\*\*', '', text)
        text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
        text = re.sub(r'<[^>]+>', '', text)
        text = re.sub(r'[🔥🛂🇺🇸🔒🎓]', '', text)

        # Clean whitespace
        text = ' '.join(text.split())
        return text.strip()

    def get_jobs_by_ats(self, ats_type: ATSType) -> List[Job]:
        """Get all jobs for a specific ATS."""
        return [j for j in self.jobs if j.ats_type == ats_type]

    def get_new_jobs(self, existing_urls: set) -> List[Job]:
        """Get jobs that aren't in the existing set."""
        return [j for j in self.jobs if j.url not in existing_urls]


def main():
    """Test the job parser."""
    import asyncio
    from github_watcher import GitHubWatcher

    async def test():
        watcher = GitHubWatcher()
        try:
            _, content = await watcher.check_for_changes()
            if content:
                parser = JobParser()
                jobs = parser.parse_readme(content)

                print(f"\n{'='*60}")
                print(f"Found {len(jobs)} open jobs")
                print(f"{'='*60}\n")

                # Show ATS distribution
                ats_counts = {}
                for job in jobs:
                    ats_counts[job.ats_type] = ats_counts.get(job.ats_type, 0) + 1

                print("ATS Distribution:")
                for ats, count in sorted(ats_counts.items(), key=lambda x: -x[1]):
                    print(f"  {ats.value}: {count}")

                print(f"\n{'='*60}")
                print("Sample Jobs:")
                print(f"{'='*60}\n")

                for job in jobs[:5]:
                    print(f"Company: {job.company}")
                    print(f"Role: {job.role}")
                    print(f"Location: {job.location}")
                    print(f"ATS: {job.ats_type.value}")
                    print(f"URL: {job.url[:60]}...")
                    print("-" * 40)

        finally:
            await watcher.close()

    asyncio.run(test())


if __name__ == "__main__":
    main()
