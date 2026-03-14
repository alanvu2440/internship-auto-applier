#!/usr/bin/env python3
"""
Multi-Platform Batch Tester

Tests SmartRecruiters, Lever, and Ashby handlers in dry-run mode.
Reports per-platform success rates and detailed field fill results.

Usage:
    python src/batch_test_multi.py [--smart N] [--lever N] [--ashby N] [--all N]
    python src/batch_test_multi.py --greenhouse N   # Regression test
"""

import asyncio
import sys
import time
from pathlib import Path
from typing import Dict, List, Any, Optional
import click
from loguru import logger

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from job_parser import ATSType
from browser_manager import BrowserManager
from form_filler import FormFiller
from ai_answerer import AIAnswerer
from captcha_solver import CaptchaSolver
from handlers import (
    SmartRecruitersHandler,
    LeverHandler,
    AshbyHandler,
    GreenhouseHandler,
)

import yaml


# Sample test URLs for each platform (used when DB has no live jobs for a platform)
# Updated February 2026 — Summer 2026 internships
SAMPLE_URLS = {
    ATSType.GREENHOUSE: [
        # Confirmed live Feb 2026
        "https://job-boards.greenhouse.io/cloudflare/jobs/7206269",
        "https://job-boards.greenhouse.io/scaleai/jobs/4606014005",
        "https://job-boards.greenhouse.io/fiveringsllc/jobs/4806713008",
    ],
    ATSType.SMARTRECRUITERS: [
        # Confirmed live Feb 2026 — requires HEADFUL mode to bypass DataDome
        "https://jobs.smartrecruiters.com/CERN/744000085340717-short-term-internship-2026",
        "https://jobs.smartrecruiters.com/Solidigm/744000088896295-2026-undergrad-engineering-internship-united-states-",
        "https://jobs.smartrecruiters.com/Solidigm/744000094081665-2026-undergrad-business-internship-united-states-",
        "https://jobs.smartrecruiters.com/OECD/744000101644285-internship-programme",
    ],
    ATSType.LEVER: [
        # Confirmed live Feb 2026
        "https://jobs.lever.co/belvederetrading/eddfd030-1b27-46db-9ef6-5b65e1e2484c",
        "https://jobs.lever.co/weride/8f84c602-8a79-43f6-b662-74a92ef761f5",
        "https://jobs.lever.co/field-ai/29a99694-254b-4b18-9d28-cb70906f06d6",
    ],
    ATSType.ASHBY: [
        # Confirmed live Feb 2026
        "https://jobs.ashbyhq.com/notion/23ac2477-0008-4bed-b1c1-81f90a32e9e6",
        "https://jobs.ashbyhq.com/replit/12737078-74c7-4e63-98a7-5e8da1e9deb1",
        "https://jobs.ashbyhq.com/fizz/c33b0bb7-d87f-4666-8622-36604fe10b20",
        "https://jobs.ashbyhq.com/lightspark/1063b990-1f08-45ae-8155-506165e82c95",
        "https://jobs.ashbyhq.com/gigaml/aa903645-854f-4404-9d49-8a96f0dcc2cc",
        "https://jobs.ashbyhq.com/decagon/aa9c9d2a-aba9-429e-bf91-8303247fbcd6",
    ],
}


class BatchTester:
    """Tests multiple ATS handlers in dry-run mode."""

    def __init__(self, config_path: str = "config/master_config.yaml",
                 secrets_path: str = "config/secrets.yaml",
                 db_path: str = "data/jobs.db",
                 headless: bool = True,
                 use_samples: bool = False):
        self.config_path = config_path
        self.secrets_path = secrets_path
        self.db_path = db_path
        self.headless = headless
        self.use_samples = use_samples
        self.config: Dict[str, Any] = {}
        self.secrets: Dict[str, Any] = {}
        self.results: Dict[str, List[Dict]] = {}

    def load_config(self):
        """Load configuration files."""
        config_file = Path(self.config_path)
        if config_file.exists():
            with open(config_file) as f:
                self.config = yaml.safe_load(f)

        secrets_file = Path(self.secrets_path)
        if secrets_file.exists():
            with open(secrets_file) as f:
                self.secrets = yaml.safe_load(f) or {}

        if self.secrets:
            self.config["secrets"] = self.secrets

    async def get_jobs_for_platform(self, ats_type: ATSType, count: int) -> List[Dict]:
        """Get test jobs for a platform from DB or sample URLs."""
        jobs = []

        # Try to get from database first (skip if --use-samples)
        if not self.use_samples:
            try:
                import aiosqlite
                async with aiosqlite.connect(self.db_path) as db:
                    db.row_factory = aiosqlite.Row
                    cursor = await db.execute(
                        "SELECT * FROM jobs WHERE ats_type = ? AND status IN ('pending', 'applied', 'failed') "
                        "ORDER BY created_at DESC LIMIT ?",
                        (ats_type.value, count)
                    )
                    rows = await cursor.fetchall()
                    jobs = [dict(r) for r in rows]

                if jobs:
                    logger.info(f"Found {len(jobs)} {ats_type.value} jobs in database")
            except Exception as e:
                logger.debug(f"Could not fetch from DB: {e}")

        # Fill remaining with sample URLs
        remaining = count - len(jobs)
        if remaining > 0 and ats_type in SAMPLE_URLS:
            samples = SAMPLE_URLS[ats_type][:remaining]
            for i, url in enumerate(samples):
                jobs.append({
                    "id": f"sample_{ats_type.value}_{i}",
                    "url": url,
                    "company": f"Sample_{ats_type.value}_{i}",
                    "role": "Software Engineer Intern",
                    "ats_type": ats_type.value,
                })

        return jobs[:count]

    async def test_platform(self, ats_type: ATSType, handler, jobs: List[Dict],
                            browser_manager: BrowserManager) -> List[Dict]:
        """Test a single platform with a list of jobs."""
        platform_results = []

        bm = browser_manager

        for i, job in enumerate(jobs):
            result = {
                "job_url": job["url"],
                "company": job.get("company", "Unknown"),
                "role": job.get("role", "Unknown"),
                "success": False,
                "status": "unknown",
                "error": None,
                "duration_s": 0,
            }

            logger.info(f"\n{'='*60}")
            logger.info(f"[{ats_type.value.upper()}] Test {i+1}/{len(jobs)}: {job.get('company')} - {job.get('role')}")
            logger.info(f"URL: {job['url']}")

            start = time.time()
            try:
                await bm.start()
                page = await bm.create_stealth_page()

                # Set AI context
                self.ai_answerer.set_job_context(
                    job.get("company", "Unknown"),
                    job.get("role", "Unknown")
                )

                # Run with per-job timeout (180s for complex forms like Greenhouse)
                try:
                    success = await asyncio.wait_for(
                        handler.apply(page, job["url"], job),
                        timeout=180
                    )
                except asyncio.TimeoutError:
                    success = False
                    result["error"] = "Timed out after 180s"
                    result["status"] = "timeout"
                    logger.warning(f"Job timed out after 90s")

                if result["status"] != "timeout":
                    result["success"] = success
                    result["status"] = getattr(handler, "_last_status", "success" if success else "failed")

            except Exception as e:
                result["error"] = str(e)
                result["status"] = "error"
                logger.error(f"Test error: {e}")
            finally:
                result["duration_s"] = round(time.time() - start, 1)
                await bm.close()

            platform_results.append(result)

            status_icon = "PASS" if result["success"] else "FAIL"
            logger.info(f"[{status_icon}] {result['company']} — {result['status']} ({result['duration_s']}s)")

        return platform_results

    async def run(self, smart_count=0, lever_count=0, ashby_count=0, greenhouse_count=0):
        """Run batch tests across all requested platforms."""
        self.load_config()

        # Initialize shared components
        form_filler = FormFiller(self.config)
        api_key = self.secrets.get("gemini_api_key") or self.config.get("secrets", {}).get("gemini_api_key")
        self.ai_answerer = AIAnswerer(api_key=api_key)
        self.ai_answerer.set_profile(self.config)

        browser_manager = BrowserManager(headless=self.headless, slow_mo=50)
        captcha_solver = CaptchaSolver(self.secrets) if self.secrets else None

        handler_args = (form_filler, self.ai_answerer, browser_manager)
        handler_kwargs = {"dry_run": True, "captcha_solver": captcha_solver}

        # Build test plan
        test_plan = []

        if smart_count > 0:
            jobs = await self.get_jobs_for_platform(ATSType.SMARTRECRUITERS, smart_count)
            handler = SmartRecruitersHandler(*handler_args, **handler_kwargs)
            test_plan.append((ATSType.SMARTRECRUITERS, handler, jobs))

        if lever_count > 0:
            jobs = await self.get_jobs_for_platform(ATSType.LEVER, lever_count)
            handler = LeverHandler(*handler_args, **handler_kwargs)
            test_plan.append((ATSType.LEVER, handler, jobs))

        if ashby_count > 0:
            jobs = await self.get_jobs_for_platform(ATSType.ASHBY, ashby_count)
            handler = AshbyHandler(*handler_args, **handler_kwargs)
            test_plan.append((ATSType.ASHBY, handler, jobs))

        if greenhouse_count > 0:
            jobs = await self.get_jobs_for_platform(ATSType.GREENHOUSE, greenhouse_count)
            handler = GreenhouseHandler(*handler_args, **handler_kwargs)
            test_plan.append((ATSType.GREENHOUSE, handler, jobs))

        if not test_plan:
            logger.error("No platforms selected for testing!")
            return

        # Run tests
        total_start = time.time()
        all_results = {}

        for ats_type, handler, jobs in test_plan:
            if not jobs:
                logger.warning(f"No jobs found for {ats_type.value} — skipping")
                continue

            logger.info(f"\n{'#'*60}")
            logger.info(f"TESTING: {ats_type.value.upper()} ({len(jobs)} jobs)")
            logger.info(f"{'#'*60}")

            results = await self.test_platform(ats_type, handler, jobs, browser_manager)
            all_results[ats_type.value] = results

        total_duration = round(time.time() - total_start, 1)

        # Print summary report
        self._print_report(all_results, total_duration)

    def _print_report(self, all_results: Dict[str, List[Dict]], total_duration: float):
        """Print a summary report of all test results."""
        print(f"\n{'='*70}")
        print("MULTI-PLATFORM BATCH TEST REPORT")
        print(f"{'='*70}")
        print(f"Total duration: {total_duration}s")
        print()

        overall_pass = 0
        overall_total = 0

        for platform, results in all_results.items():
            passed = sum(1 for r in results if r["success"])
            total = len(results)
            closed = sum(1 for r in results if r["status"] == "closed")
            failed = total - passed - closed
            rate = (passed / total * 100) if total > 0 else 0

            overall_pass += passed
            overall_total += total

            print(f"  {platform.upper()}")
            print(f"    Jobs tested:  {total}")
            print(f"    Passed:       {passed}")
            print(f"    Failed:       {failed}")
            print(f"    Closed:       {closed}")
            print(f"    Success rate: {rate:.0f}%")
            print()

            # Per-job details
            for r in results:
                icon = "PASS" if r["success"] else ("CLOSED" if r["status"] == "closed" else "FAIL")
                print(f"    [{icon:>6}] {r['company'][:30]:<30} {r['duration_s']:>5.1f}s  {r['job_url'][:60]}")
                if r["error"]:
                    print(f"           Error: {r['error'][:80]}")
            print()

        # Overall
        overall_rate = (overall_pass / overall_total * 100) if overall_total > 0 else 0
        print(f"{'='*70}")
        print(f"OVERALL: {overall_pass}/{overall_total} passed ({overall_rate:.0f}%)")
        print(f"{'='*70}")


@click.command()
@click.option("--smart", default=0, help="Number of SmartRecruiters jobs to test")
@click.option("--lever", default=0, help="Number of Lever jobs to test")
@click.option("--ashby", default=0, help="Number of Ashby jobs to test")
@click.option("--greenhouse", default=0, help="Number of Greenhouse jobs to test (regression)")
@click.option("--all", "all_count", default=0, help="Test N jobs per platform (overrides individual counts)")
@click.option("--headless/--headful", default=True, help="Run headless (default) or headful")
@click.option("--use-samples", is_flag=True, help="Use sample URLs instead of DB jobs")
def main(smart, lever, ashby, greenhouse, all_count, headless, use_samples):
    """Run multi-platform batch tests in dry-run mode."""
    if all_count > 0:
        smart = lever = ashby = all_count

    if smart == 0 and lever == 0 and ashby == 0 and greenhouse == 0:
        # Default: test a few of each
        smart, lever, ashby = 3, 3, 2

    # Configure logging
    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{message}</cyan>",
        level="INFO",
    )
    logger.add(
        "logs/batch_test_multi.log",
        rotation="10 MB",
        retention="7 days",
        level="DEBUG",
    )

    async def run():
        tester = BatchTester(headless=headless, use_samples=use_samples)
        await tester.run(
            smart_count=smart,
            lever_count=lever,
            ashby_count=ashby,
            greenhouse_count=greenhouse,
        )

    asyncio.run(run())


if __name__ == "__main__":
    main()
