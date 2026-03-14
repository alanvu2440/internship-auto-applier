#!/usr/bin/env python3
"""
Test Script - Apply to a single job to verify the system works.
Uses mock config and skips jobs requiring login.
"""

import asyncio
import sys
import random
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

import yaml
from loguru import logger

from github_watcher import GitHubWatcher
from job_parser import JobParser, Job, ATSType
from browser_manager import BrowserManager
from form_filler import FormFiller
from handlers.greenhouse import GreenhouseHandler
from ai_answerer import AIAnswerer


# URLs that DON'T require login (direct application forms)
DIRECT_APPLY_PATTERNS = [
    "boards.greenhouse.io",
    "jobs.lever.co",
    "job-boards.greenhouse.io",
]

# URLs that DO require login (skip these)
LOGIN_REQUIRED_PATTERNS = [
    "myworkdayjobs.com",
    "linkedin.com",
    "indeed.com",
    "careers.",  # Many company career portals
    "icims.com",
    "taleo.",
    "successfactors.",
]


def requires_login(url: str) -> bool:
    """Check if URL likely requires login."""
    url_lower = url.lower()

    # Check if it's a direct apply URL
    for pattern in DIRECT_APPLY_PATTERNS:
        if pattern in url_lower:
            return False

    # Check if it requires login
    for pattern in LOGIN_REQUIRED_PATTERNS:
        if pattern in url_lower:
            return True

    # Default: assume it might work
    return False


async def find_test_jobs() -> list:
    """Find jobs that don't require login for testing."""
    logger.info("Fetching jobs from SimplifyJobs...")

    watcher = GitHubWatcher()
    parser = JobParser()

    try:
        _, content = await watcher.check_for_changes()
        if not content:
            logger.error("Could not fetch job listings")
            return []

        jobs = parser.parse_readme(content)
        logger.info(f"Found {len(jobs)} total jobs")

        # Filter to direct apply jobs (Greenhouse, Lever)
        direct_jobs = []
        for job in jobs:
            if job.ats_type in (ATSType.GREENHOUSE, ATSType.LEVER):
                if not requires_login(job.url):
                    direct_jobs.append(job)

        logger.info(f"Found {len(direct_jobs)} jobs that don't require login")
        return direct_jobs

    finally:
        await watcher.close()


async def test_form_detection(url: str, config: dict, job: Job = None):
    """Test if we can detect and fill form fields on a job page."""
    logger.info(f"Testing form detection on: {url}")

    browser = BrowserManager(headless=False, slow_mo=100)
    form_filler = FormFiller(config)
    ai_answerer = AIAnswerer(config)
    handler = GreenhouseHandler(form_filler, ai_answerer, browser, dry_run=True)

    try:
        await browser.start()
        page = await browser.create_stealth_page()

        # Navigate to job
        await page.goto(url, wait_until="networkidle")
        await asyncio.sleep(2)

        # Take screenshot
        await page.screenshot(path="logs/test_job_page.png")
        logger.info("Screenshot saved to logs/test_job_page.png")

        # Try to find Apply button
        apply_selectors = [
            'a:has-text("Apply for this job")',
            'a:has-text("Apply Now")',
            'button:has-text("Apply for this job")',
            'button:has-text("Apply Now")',
            '[data-qa="btn-apply"]',
            'a[href*="/application"]',
            'a.postings-btn',
            'a:has-text("Apply")',
            'button:has-text("Apply")',
        ]

        applied_clicked = False
        for selector in apply_selectors:
            try:
                btn = await page.query_selector(selector)
                if btn and await btn.is_visible():
                    logger.info(f"Found Apply button: {selector}")
                    await btn.click()
                    applied_clicked = True
                    # Wait for form to load
                    try:
                        await page.wait_for_selector('input#first_name, input[name="first_name"], input[type="email"]', timeout=10000)
                        logger.info("Form inputs appeared after clicking Apply")
                    except:
                        logger.warning("Form inputs did not appear after clicking Apply, waiting longer...")
                        await asyncio.sleep(5)
                    break
            except Exception as e:
                logger.debug(f"Apply button selector {selector} failed: {e}")

        if not applied_clicked:
            # Try navigating directly to #app
            logger.info("No Apply button found, trying #app navigation")
            current_url = page.url
            if "#app" not in current_url:
                await page.goto(current_url + "#app", wait_until="networkidle")
                await asyncio.sleep(3)

        # Take screenshot of form
        await page.screenshot(path="logs/test_form_page.png")
        logger.info("Screenshot saved to logs/test_form_page.png")

        # Detect form fields
        text_inputs = await page.query_selector_all('input[type="text"], input[type="email"], input[type="tel"], input:not([type])')
        selects = await page.query_selector_all('select')
        textareas = await page.query_selector_all('textarea')
        file_inputs = await page.query_selector_all('input[type="file"]')

        logger.info(f"Detected form fields:")
        logger.info(f"  - Text inputs: {len(text_inputs)}")
        logger.info(f"  - Dropdowns: {len(selects)}")
        logger.info(f"  - Textareas: {len(textareas)}")
        logger.info(f"  - File uploads: {len(file_inputs)}")

        # Try filling the form using the Greenhouse handler (but don't submit in test mode)
        logger.info("Attempting to fill form fields using Greenhouse handler...")
        job_data = {"company": job.company if job else "Test Company", "role": job.role if job else "Test Role"}
        result = await handler.apply(page, url, job_data)
        logger.info(f"Handler result: {result}")

        # Take screenshot after filling
        await page.screenshot(path="logs/test_filled_form.png")
        logger.info("Screenshot saved to logs/test_filled_form.png")

        # Take screenshot after Greenhouse handler finished
        await page.screenshot(path="logs/test_handler_result.png")
        logger.info("Screenshot saved to logs/test_handler_result.png")
        logger.info(f"Handler returned: {result}")

        # Wait a bit to see final state
        logger.info("Waiting 5 seconds for final state...")
        await asyncio.sleep(5)

        return result

    except Exception as e:
        logger.error(f"Test failed: {e}")
        return False

    finally:
        await browser.close()


async def main():
    """Main test function."""
    # Setup logging
    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{message}</cyan>",
        level="INFO",
    )

    # Create logs dir
    Path("logs").mkdir(exist_ok=True)

    # Load mock config
    config_path = Path(__file__).parent.parent / "config" / "mock_config.yaml"
    with open(config_path) as f:
        config = yaml.safe_load(f)

    logger.info("Loaded mock config")
    logger.info(f"Test user: {config['personal_info']['first_name']} {config['personal_info']['last_name']}")
    logger.info(f"Test email: {config['personal_info']['email']}")

    # Find test jobs
    jobs = await find_test_jobs()

    if not jobs:
        logger.error("No suitable test jobs found")
        return

    # Pick a random Greenhouse or Lever job
    greenhouse_jobs = [j for j in jobs if j.ats_type == ATSType.GREENHOUSE]
    lever_jobs = [j for j in jobs if j.ats_type == ATSType.LEVER]

    logger.info(f"Greenhouse jobs: {len(greenhouse_jobs)}")
    logger.info(f"Lever jobs: {len(lever_jobs)}")

    # Test with first available - skip first few as they might be closed
    test_job = None
    if greenhouse_jobs:
        # Try to find a job that looks like it's still accepting applications
        for i, job in enumerate(greenhouse_jobs[:10]):  # Check first 10
            logger.info(f"Candidate job {i}: {job.company} - {job.role} - {job.url}")
        test_job = greenhouse_jobs[2] if len(greenhouse_jobs) > 2 else greenhouse_jobs[0]
        logger.info(f"Testing with Greenhouse job: {test_job.company}")
    elif lever_jobs:
        test_job = lever_jobs[0]
        logger.info(f"Testing with Lever job: {test_job.company}")

    if test_job:
        logger.info(f"Company: {test_job.company}")
        logger.info(f"Role: {test_job.role}")
        logger.info(f"URL: {test_job.url}")

        # Test form detection and filling
        await test_form_detection(test_job.url, config, test_job)
    else:
        logger.warning("No test jobs available")


if __name__ == "__main__":
    asyncio.run(main())
