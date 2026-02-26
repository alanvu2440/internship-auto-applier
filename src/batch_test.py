#!/usr/bin/env python3
"""
Batch Test Script - Apply to multiple jobs to measure success rate.
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


# Number of jobs to test
NUM_JOBS = 20

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
    "careers.",
    "icims.com",
    "taleo.",
    "successfactors.",
]


def requires_login(url: str) -> bool:
    """Check if URL likely requires login."""
    url_lower = url.lower()
    for pattern in DIRECT_APPLY_PATTERNS:
        if pattern in url_lower:
            return False
    for pattern in LOGIN_REQUIRED_PATTERNS:
        if pattern in url_lower:
            return True
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

        # Filter to direct apply jobs (Greenhouse only for now)
        direct_jobs = []
        for job in jobs:
            if job.ats_type == ATSType.GREENHOUSE:
                if not requires_login(job.url):
                    direct_jobs.append(job)

        logger.info(f"Found {len(direct_jobs)} Greenhouse jobs that don't require login")
        return direct_jobs

    finally:
        await watcher.close()


async def apply_to_job(job: Job, browser: BrowserManager, handler: GreenhouseHandler) -> tuple:
    """Apply to a single job. Returns (status, error_msg).
    status is one of: 'success', 'closed', 'failed'
    """
    try:
        page = await browser.create_stealth_page()
        job_data = {"company": job.company, "role": job.role}

        result = await handler.apply(page, job.url, job_data)

        await page.close()

        if result:
            return ("success", None)
        else:
            # Check if handler flagged this as a closed job
            status = getattr(handler, "_last_status", "failed")
            if status == "closed":
                return ("closed", "Job closed/removed")
            return ("failed", "Application failed")

    except Exception as e:
        return ("failed", str(e)[:100])


async def main():
    """Main batch test function."""
    # Setup logging
    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{message}</cyan>",
        level="INFO",
    )

    # Create logs dir and add file logging
    Path("logs").mkdir(exist_ok=True)
    logger.add(
        "logs/batch_test_latest.log",
        format="{time:HH:mm:ss} | {level: <8} | {message}",
        level="INFO",
        rotation="1 MB",
        mode="w",  # Overwrite each run
    )

    # Load mock config
    config_path = Path(__file__).parent.parent / "config" / "mock_config.yaml"
    with open(config_path) as f:
        config = yaml.safe_load(f)

    logger.info("Loaded mock config")
    logger.info(f"Test user: {config['personal_info']['first_name']} {config['personal_info']['last_name']}")

    # Find test jobs
    jobs = await find_test_jobs()

    if not jobs:
        logger.error("No suitable test jobs found")
        return

    # Pick random jobs to test
    test_jobs = random.sample(jobs, min(NUM_JOBS, len(jobs)))
    logger.info(f"Selected {len(test_jobs)} jobs for testing")

    # Setup browser and handler
    browser = BrowserManager(headless=False, slow_mo=50)
    form_filler = FormFiller(config)
    ai_answerer = AIAnswerer(config)
    handler = GreenhouseHandler(form_filler, ai_answerer, browser, dry_run=True)

    try:
        await browser.start()

        results = {"success": 0, "failed": 0, "closed": 0, "forms_filled": 0, "errors": []}

        for i, job in enumerate(test_jobs):
            logger.info(f"[{i+1}/{len(test_jobs)}] Applying to {job.company} - {job.role}")
            logger.info(f"  URL: {job.url}")

            status, error = await apply_to_job(job, browser, handler)

            if status == "success":
                results["success"] += 1
                results["forms_filled"] += 1
                logger.info(f"  ✓ SUCCESS")
            elif status == "closed":
                results["closed"] += 1
                logger.info(f"  ⊘ SKIPPED (closed/removed)")
            else:
                results["failed"] += 1
                # Even if validation failed, count as "form filled" if it reached dry run stage
                if error and "Application failed" in error:
                    results["forms_filled"] += 1
                results["errors"].append(f"{job.company}: {error}")
                logger.warning(f"  ✗ FAILED: {error}")

            # Small delay between applications
            await asyncio.sleep(2)

        # Print summary
        total = results["success"] + results["failed"] + results["closed"]
        live_total = results["success"] + results["failed"]  # Exclude closed jobs
        success_rate = (results["success"] / live_total * 100) if live_total > 0 else 0
        overall_rate = (results["success"] / total * 100) if total > 0 else 0

        forms_filled_rate = (results["forms_filled"] / live_total * 100) if live_total > 0 else 0

        logger.info("=" * 60)
        logger.info(f"BATCH TEST RESULTS")
        logger.info(f"  Total Attempted: {total}")
        logger.info(f"  Closed/Removed (skipped): {results['closed']}")
        logger.info(f"  Live Jobs Tested: {live_total}")
        logger.info(f"  Forms Filled (reached dry run): {results['forms_filled']}")
        logger.info(f"  Success (passed validation): {results['success']}")
        logger.info(f"  Failed: {results['failed']}")
        logger.info(f"  Form Fill Rate (live): {forms_filled_rate:.1f}%")
        logger.info(f"  Success Rate (live jobs): {success_rate:.1f}%")
        logger.info(f"  Overall Rate (incl. closed): {overall_rate:.1f}%")
        logger.info("=" * 60)

        if results["errors"]:
            logger.info("Errors:")
            for err in results["errors"][:10]:
                logger.info(f"  - {err}")

    finally:
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
