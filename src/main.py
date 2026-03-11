#!/usr/bin/env python3
"""
Internship Auto-Applier - Main Orchestrator

Coordinates all components to automatically apply to jobs from SimplifyJobs.
"""

import asyncio
import sys
import os
import threading
import select
from pathlib import Path
from typing import Dict, Any, Optional
import yaml
import click
from loguru import logger

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from github_watcher import GitHubWatcher
from job_parser import JobParser, Job, ATSType
from job_queue import JobQueue, JobStatus
from browser_manager import BrowserManager
from form_filler import FormFiller
from ai_answerer import AIAnswerer
from application_tracker import ApplicationTracker
from handlers import GreenhouseHandler, LeverHandler, WorkdayHandler, SmartRecruitersHandler, AshbyHandler, ICIMSHandler, GenericHandler
from captcha_solver import CaptchaSolver
from email_verifier import EmailVerifier
from email_response_tracker import EmailResponseTracker
from gemini_form_scanner import GeminiFormScanner
from extension_manager import ExtensionManager


class EscMonitor:
    """Monitor ESC key to toggle between auto/manual mode.

    Press ESC during automation -> bot pauses, you control the browser.
    Press ESC during manual mode -> bot resumes with next job.
    """

    def __init__(self):
        self.is_manual = False
        self._stop = False
        self._thread = None
        self._loop = None
        self._toggle_event = None
        self._old_settings = None
        self._fd = None

    def start(self, loop):
        if not sys.stdin.isatty():
            return
        import tty, termios
        self._loop = loop
        self._fd = sys.stdin.fileno()
        self._old_settings = termios.tcgetattr(self._fd)
        self._toggle_event = asyncio.Event()
        tty.setcbreak(self._fd)
        self._thread = threading.Thread(target=self._listen, daemon=True)
        self._thread.start()
        import atexit
        atexit.register(self.stop)
        logger.info("ESC monitor active — press ESC to toggle manual/auto mode")
        sys.stdout.write(
            "\r\n══════════════════════════════════════════════\r\n"
            "  ESC MONITOR ACTIVE — Press ESC to pause bot\r\n"
            "══════════════════════════════════════════════\r\n"
        )
        sys.stdout.flush()

    def stop(self):
        self._stop = True
        if self._old_settings and self._fd is not None:
            try:
                import termios
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_settings)
            except Exception:
                pass
            self._old_settings = None

    def _listen(self):
        while not self._stop:
            try:
                r, _, _ = select.select([self._fd], [], [], 0.15)
                if not r:
                    continue
                ch = os.read(self._fd, 1)
                if ch == b'\x1b':
                    # Distinguish standalone ESC from escape sequences (arrow keys)
                    r2, _, _ = select.select([self._fd], [], [], 0.05)
                    if r2:
                        os.read(self._fd, 10)  # consume sequence
                        continue
                    # Standalone ESC — toggle
                    self.is_manual = not self.is_manual
                    if self._loop and self._toggle_event:
                        self._loop.call_soon_threadsafe(self._toggle_event.set)
                    if self.is_manual:
                        sys.stdout.write(
                            "\a\r\n  >>> MANUAL MODE — Browser is yours. Press ESC to resume bot. <<<\r\n"
                        )
                        logger.warning("ESC toggle: MANUAL MODE — bot paused, browser is yours")
                    else:
                        sys.stdout.write(
                            "\a\r\n  >>> AUTO MODE — Bot resuming. Press ESC to take over. <<<\r\n"
                        )
                        logger.warning("ESC toggle: AUTO MODE — bot resuming")
                    sys.stdout.flush()
            except (OSError, ValueError):
                break
            except Exception:
                continue

    async def wait_for_toggle(self):
        """Async: wait for next ESC press. Blocks forever if no terminal."""
        if self._loop:
            self._toggle_event = asyncio.Event()  # Fresh event — no stale state from prior ESC
            await self._toggle_event.wait()
        else:
            # No terminal — block forever (never resolve)
            await asyncio.Event().wait()


class InternshipAutoApplier:
    """Main application orchestrator."""

    def __init__(
        self,
        config_path: str = "config/master_config.yaml",
        secrets_path: str = "config/secrets.yaml",
        db_path: str = "data/jobs.db",
    ):
        self.config_path = Path(config_path)
        self.secrets_path = Path(secrets_path)
        self.db_path = db_path

        self.config: Dict[str, Any] = {}
        self.secrets: Dict[str, Any] = {}

        # Components
        self.watcher: Optional[GitHubWatcher] = None
        self.parser: Optional[JobParser] = None
        self.queue: Optional[JobQueue] = None
        self.browser_manager: Optional[BrowserManager] = None
        self.form_filler: Optional[FormFiller] = None
        self.ai_answerer: Optional[AIAnswerer] = None

        # Handlers
        self.handlers: Dict[ATSType, Any] = {}

        # State
        self.running = False
        self.stats = {
            "applied": 0,
            "failed": 0,
            "skipped": 0,
        }

        # Application tracker for detailed reporting
        self.tracker: Optional[ApplicationTracker] = None

    def load_config(self):
        """Load configuration files."""
        # Load main config
        if self.config_path.exists():
            with open(self.config_path) as f:
                self.config = yaml.safe_load(f) or {}
            logger.info(f"Loaded config from {self.config_path}")
        else:
            logger.warning(f"Config not found: {self.config_path}")
            self.config = {}

        # Load secrets
        if self.secrets_path.exists():
            with open(self.secrets_path) as f:
                self.secrets = yaml.safe_load(f) or {}
            logger.info(f"Loaded secrets from {self.secrets_path}")
        else:
            logger.warning(f"Secrets not found: {self.secrets_path}")
            self.secrets = {}

        # Merge secrets into config
        if self.secrets:
            self.config["secrets"] = self.secrets

        # Validate config
        self._validate_config()

    def _validate_config(self):
        """Validate config has required fields."""
        warnings = []
        errors = []

        personal = self.config.get("personal_info", {})

        # Required personal fields
        required_personal = ["first_name", "last_name", "email", "phone", "city", "state"]
        for field in required_personal:
            val = personal.get(field, "")
            if not val or str(val).strip() == "":
                errors.append(f"Missing personal_info.{field}")

        # Check for mock/placeholder data
        mock_indicators = ["jon doe", "jane doe", "john doe", "jondoe", "555-", "example.com"]
        for field_name in ["first_name", "last_name", "email", "phone", "linkedin"]:
            val = str(personal.get(field_name, "")).lower()
            for mock in mock_indicators:
                if mock in val:
                    errors.append(f"personal_info.{field_name} appears to be placeholder data: '{personal.get(field_name)}'")
                    break

        # Validate email format
        email = personal.get("email", "")
        if email and "@" not in email:
            errors.append(f"Invalid email format: {email}")

        # Validate phone
        phone = str(personal.get("phone", ""))
        if phone and len(phone.replace("-", "").replace(" ", "").replace("+", "").replace("(", "").replace(")", "")) < 10:
            warnings.append(f"Phone number may be incomplete: {phone}")

        # Check resume file
        resume = self.config.get("files", {}).get("resume", "")
        if not resume:
            errors.append("Missing files.resume — path to your resume PDF is required")
        elif not Path(resume).exists():
            errors.append(f"Resume file not found: {resume} — place your resume PDF there")

        # Education
        education = self.config.get("education", [])
        if isinstance(education, dict):
            education = [education]  # Normalize dict to list
        if not education or not isinstance(education, list) or not education[0].get("school"):
            warnings.append("Education section is empty — many forms require this")

        # Validate URLs
        linkedin = personal.get("linkedin", "")
        if not linkedin:
            warnings.append("LinkedIn URL is empty — most applications require this")
        elif "linkedin.com" not in linkedin.lower():
            warnings.append(f"LinkedIn URL may be invalid: {linkedin}")

        github = personal.get("github", "")
        if github and "github.com" not in github.lower():
            warnings.append(f"GitHub URL may be invalid: {github}")

        # Check AI key
        api_key = self.secrets.get("gemini_api_key") or self.config.get("secrets", {}).get("gemini_api_key")
        use_ai = self.config.get("preferences", {}).get("use_ai_for_custom_questions", False)
        if use_ai and not api_key:
            warnings.append("use_ai_for_custom_questions is true but no gemini_api_key in secrets.yaml")

        # Log results
        for warn in warnings:
            logger.warning(f"Config warning: {warn}")

        if errors:
            for err in errors:
                logger.error(f"Config error: {err}")
            raise ValueError(
                f"\n{'='*60}\n"
                f"CONFIG INCOMPLETE — Fill in config/master_config.yaml first!\n"
                f"{'='*60}\n"
                f"Missing fields:\n" +
                "\n".join(f"  - {e}" for e in errors) +
                f"\n\nRun 'python src/main.py setup' to check your config.\n"
                f"{'='*60}"
            )

    async def initialize(self):
        """Initialize all components."""
        logger.info("Initializing Internship Auto-Applier...")

        # Load configs
        self.load_config()

        # Initialize job queue
        self.queue = JobQueue(self.db_path)
        await self.queue.initialize()

        # Initialize parser
        self.parser = JobParser()

        # Initialize browser manager (with proxy if configured)
        headless = self.config.get("preferences", {}).get("headless", False)
        proxy_config = self.secrets.get("proxy", {})
        proxy = None
        if proxy_config.get("enabled"):
            proxy = {
                "host": proxy_config["host"],
                "port": proxy_config["port"],
                "username": proxy_config.get("username", ""),
                "password": proxy_config.get("password", ""),
            }
            logger.info(f"Proxy enabled: {proxy_config['host']}:{proxy_config['port']}")
        self.browser_manager = BrowserManager(
            headless=headless,
            slow_mo=50,
            proxy=proxy,
        )

        # Initialize form filler
        self.form_filler = FormFiller(self.config)

        # Initialize AI answerer (using Gemini) with backup key failover
        api_key = self.secrets.get("gemini_api_key") or self.config.get("secrets", {}).get("gemini_api_key")
        self.ai_answerer = AIAnswerer(api_key=api_key, secrets=self.secrets)
        self.ai_answerer.set_profile(self.config)

        # Initialize Gemini form scanner (DOM + vision cleanup pass)
        self.gemini_scanner = GeminiFormScanner(self.ai_answerer)
        self._smart_mode = False  # Enabled via --smart flag
        self._assist_mode = False  # Enabled via --assist flag
        self._extension_path = None  # Set via --with-simplify flag
        self._url_patterns = None  # URL LIKE patterns for filtering (e.g. workday accounts only)
        self.esc_monitor = None  # Initialized when smart mode starts

        # Initialize application tracker
        self.tracker = ApplicationTracker(report_dir="logs")

        # Initialize email verifier (for ATS systems that send confirmation codes)
        gmail_config = self.secrets.get("gmail", {})
        if gmail_config.get("email") and gmail_config.get("app_password"):
            self.email_verifier = EmailVerifier(
                gmail_email=gmail_config["email"],
                app_password=gmail_config["app_password"],
            )
            logger.info("Email verifier initialized (Gmail IMAP)")
        else:
            self.email_verifier = None
            logger.debug("Email verifier not configured — skipping")

        # Initialize handlers
        dry_run = self.config.get("preferences", {}).get("dry_run", False)
        await self._init_handlers(dry_run=dry_run)

        # Initialize watcher
        self.watcher = GitHubWatcher(
            poll_interval=300,  # 5 minutes
            on_change=self._on_new_jobs,
        )

        logger.info("Initialization complete!")

    async def _init_handlers(self, dry_run: bool = False):
        """Initialize ATS handlers."""
        # Initialize CAPTCHA solver
        captcha_solver = CaptchaSolver(self.secrets) if self.secrets else None

        handler_args = (self.form_filler, self.ai_answerer, self.browser_manager, dry_run)
        handler_kwargs = {"captcha_solver": captcha_solver}

        # Email verifier for handlers that need it
        email_verifier = getattr(self, "email_verifier", None)

        self.handlers = {
            ATSType.GREENHOUSE: GreenhouseHandler(*handler_args, **handler_kwargs),
            ATSType.LEVER: LeverHandler(*handler_args, **handler_kwargs),
            ATSType.WORKDAY: WorkdayHandler(*handler_args, **handler_kwargs),
            ATSType.SMARTRECRUITERS: SmartRecruitersHandler(*handler_args, **handler_kwargs),
            ATSType.ASHBY: AshbyHandler(*handler_args, **handler_kwargs),
            ATSType.ICIMS: ICIMSHandler(*handler_args, **handler_kwargs),
            ATSType.UNKNOWN: GenericHandler(*handler_args, **handler_kwargs),
        }

        # Attach email verifier to handlers that might need it
        for handler in self.handlers.values():
            handler.email_verifier = email_verifier

        # Map other ATS types to generic handler
        for ats_type in ATSType:
            if ats_type not in self.handlers:
                self.handlers[ats_type] = self.handlers[ATSType.UNKNOWN]

        if dry_run:
            logger.info("DRY RUN MODE: Forms will be filled but not submitted")

    async def _on_new_jobs(self, readme_content: str, priority: int = 100) -> int:
        """Handle new jobs from GitHub watcher."""
        logger.info("Processing new jobs from SimplifyJobs...")

        # Parse jobs
        jobs = self.parser.parse_readme(readme_content)
        logger.info(f"Found {len(jobs)} total jobs")

        # Get existing URLs
        existing_urls = await self.queue.get_all_urls()

        # Filter to new jobs
        new_jobs = [j for j in jobs if j.url not in existing_urls]
        logger.info(f"Found {len(new_jobs)} new jobs")

        added = 0
        if new_jobs:
            added = await self.queue.add_jobs(new_jobs, priority=priority)
            logger.info(f"Added {added} new jobs to queue (priority={priority})")
        return added

    async def fetch_and_queue_jobs(self):
        """Fetch current jobs from all SimplifyJobs repos and add to queue."""
        logger.info("Fetching jobs from all SimplifyJobs repos...")

        # Fetch from all repos (Summer2026, New-Grad, etc.)
        repo_results = await self.watcher.fetch_all_repos()
        total_added = 0
        for repo_name, content, priority in repo_results:
            if content:
                added = await self._on_new_jobs(content, priority=priority)
                total_added += added
                logger.info(f"  {repo_name}: +{added} new jobs (priority={priority})")

        if total_added == 0:
            # Fallback to legacy single-repo fetch
            _, content = await self.watcher.check_for_changes()
            if content:
                await self._on_new_jobs(content)

        logger.info(f"Total new jobs added: {total_added}")

    async def apply_to_job(self, job_data: Dict[str, Any], job_index: int = 0, total_jobs: int = 0) -> bool:
        """Apply to a single job."""
        job_id = job_data["id"]
        url = job_data["url"]
        company = job_data["company"]
        role = job_data["role"]
        try:
            ats_type = ATSType(job_data.get("ats_type", "unknown"))
        except ValueError:
            ats_type = ATSType.UNKNOWN
        attempts = job_data.get("attempts", 0)

        progress = f"[{job_index}/{total_jobs}] " if total_jobs > 0 else ""
        logger.info(f"\n{'='*60}")
        logger.info(f"{progress}Applying to: {company} — {role}")
        logger.info(f"  ATS: {ats_type.value} | URL: {url}")
        logger.info(f"  Attempt: {attempts + 1}/3")
        logger.info(f"{'='*60}")

        # Set AI context for this job
        self.ai_answerer.set_job_context(company, role)

        # Get handler
        handler = self.handlers.get(ats_type, self.handlers[ATSType.UNKNOWN])

        # Set review mode if enabled
        review_mode = self.config.get("preferences", {}).get("review_mode", False)
        if review_mode:
            handler.review_mode = True

        # Track filled/missed fields
        fields_filled = {}
        fields_missed = {}
        questions_answered = {}
        error_msg = None
        success = False
        _close_tab = False  # Set True for skipped/closed jobs that don't need manual help
        _timed_out = False   # Set True if job hit 5-min stall timeout

        # Create page
        try:
            await self.browser_manager.start()
            page = await self.browser_manager.create_stealth_page()

            # Apply with timeout — ESC cancels handler and enters manual mode
            import time as _time
            start_time = _time.time()
            esc_interrupted = False
            try:
                handler_task = asyncio.create_task(
                    asyncio.wait_for(handler.apply(page, url, job_data), timeout=300)
                )
                if self.esc_monitor and not self.esc_monitor.is_manual:
                    esc_task = asyncio.create_task(self.esc_monitor.wait_for_toggle())
                    done, pending = await asyncio.wait(
                        {handler_task, esc_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                    for t in pending:
                        t.cancel()
                        try:
                            await t
                        except (asyncio.CancelledError, Exception):
                            pass
                    if esc_task in done and self.esc_monitor.is_manual:
                        logger.info("[ESC] User took over — entering manual mode")
                        success = False
                        esc_interrupted = True
                    elif handler_task in done:
                        success = handler_task.result()
                    else:
                        success = False
                elif self.esc_monitor and self.esc_monitor.is_manual:
                    # Already in manual mode before handler started — go straight to assist
                    handler_task.cancel()
                    try:
                        await handler_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    success = False
                    esc_interrupted = True
                else:
                    success = await handler_task
            except asyncio.TimeoutError:
                success = False
                _timed_out = True
                error_msg = "Timed out after 300s — tab left open for manual review"
                logger.warning(f"Application timed out after 300s — leaving tab open for manual review")
            except asyncio.CancelledError:
                success = False
                esc_interrupted = True

            # SMART MODE: If handler failed and page is still up, run Gemini scanner
            if not success and self._smart_mode and not error_msg and not esc_interrupted:
                try:
                    logger.info(f"[SMART] Running Gemini form scanner on {company}...")
                    scan_result = await asyncio.wait_for(
                        self.gemini_scanner.scan_and_fill(page, max_retries=1),
                        timeout=60,
                    )
                    scanner_filled = scan_result.get("filled", {})
                    scanner_empty = scan_result.get("still_empty", [])
                    if scanner_filled:
                        logger.info(f"[SMART] Scanner filled {len(scanner_filled)} fields, attempting submit...")
                        # Try clicking submit button directly (don't re-run full handler)
                        try:
                            submit_selectors = [
                                'button[type="submit"]',
                                'input[type="submit"]',
                                'button:has-text("Submit")',
                                'button:has-text("Apply")',
                                'button:has-text("Submit Application")',
                                'a:has-text("Submit")',
                            ]
                            is_dry = self.config.get("preferences", {}).get("dry_run", False)
                            if not is_dry:
                                for sel in submit_selectors:
                                    btn = await page.query_selector(sel)
                                    if btn and await btn.is_visible():
                                        await btn.scroll_into_view_if_needed()
                                        await asyncio.sleep(0.5)
                                        await btn.click()
                                        await asyncio.sleep(3)
                                        break
                            # Check if submission succeeded
                            success = await handler.is_application_complete(page)
                            if success:
                                logger.info(f"[SMART] Submit succeeded after scanner fill!")
                        except Exception as retry_e:
                            logger.warning(f"[SMART] Submit retry failed: {retry_e}")
                    else:
                        logger.info(f"[SMART] Scanner found nothing to fill ({len(scanner_empty)} still empty)")
                except asyncio.TimeoutError:
                    logger.warning("[SMART] Scanner timed out after 60s")
                except Exception as scan_e:
                    logger.warning(f"[SMART] Scanner error: {scan_e}")

            # ASSIST MODE: If failed (or ESC interrupted), pause for user.
            # Browser stays open. Bot watches for submit or next ESC.
            if not success and (self._assist_mode or self._smart_mode or esc_interrupted) and not error_msg and sys.stdin.isatty():
                try:
                    # Show what's missing
                    scan_info = await self.gemini_scanner.quick_scan(page)
                    empty_required = scan_info.get("empty_required", [])
                    page_errors = await handler.get_error_message(page)

                    # NOTIFY — bell + macOS notification
                    import subprocess
                    sys.stdout.write("\a\a\a")
                    sys.stdout.flush()
                    try:
                        subprocess.Popen([
                            "osascript", "-e",
                            f'display notification "Fill remaining fields for {company} - {role}" '
                            f'with title "ASSIST MODE" sound name "Glass"'
                        ])
                    except Exception:
                        pass

                    sys.stdout.write("\r\n" + "=" * 60 + "\r\n")
                    sys.stdout.write(f"  >>> YOUR TURN — {company} - {role}\r\n")
                    sys.stdout.write("=" * 60 + "\r\n")
                    sys.stdout.write(f"  URL: {url}\r\n")
                    if page_errors:
                        sys.stdout.write(f"  Form errors: {page_errors}\r\n")
                    if empty_required:
                        sys.stdout.write(f"  Empty required fields ({len(empty_required)}):\r\n")
                        for fld in empty_required[:10]:
                            sys.stdout.write(f"    - {fld['label'][:60]} ({fld['type']})\r\n")
                    else:
                        sys.stdout.write("  No obviously empty fields detected (may be custom components)\r\n")
                    sys.stdout.write("\r\n")
                    sys.stdout.write("  >>> Browser is OPEN. Fill fields + submit yourself.\r\n")
                    sys.stdout.write("  >>> Press ESC when done to move to next job.\r\n")
                    sys.stdout.write("  >>> Bot auto-detects submission too.\r\n")
                    sys.stdout.write("=" * 60 + "\r\n")
                    sys.stdout.flush()

                    # Watch for submission on the page
                    async def _watch_for_submit():
                        original_url = page.url
                        while True:
                            await asyncio.sleep(3)
                            try:
                                if page.url != original_url:
                                    await asyncio.sleep(2)
                                    if await handler.is_application_complete(page):
                                        return "auto_detected"
                                if await handler.is_application_complete(page):
                                    return "auto_detected"
                            except Exception:
                                return "page_closed"

                    watch_task = asyncio.create_task(_watch_for_submit())
                    tasks = {watch_task}

                    if self.esc_monitor:
                        # Wait for ESC (resume auto / skip job) OR submission detected
                        esc_task = asyncio.create_task(self.esc_monitor.wait_for_toggle())
                        tasks.add(esc_task)
                    else:
                        esc_task = None

                    done, pending = await asyncio.wait(
                        tasks, return_when=asyncio.FIRST_COMPLETED, timeout=600,
                    )
                    for task in pending:
                        task.cancel()
                        try:
                            await task
                        except (asyncio.CancelledError, Exception):
                            pass

                    if not done:
                        logger.info(f"[ASSIST] Timed out waiting for {company}")
                    elif watch_task in done:
                        result = watch_task.result()
                        if result == "auto_detected":
                            success = True
                            sys.stdout.write("\r\n  >>> SUBMISSION DETECTED! Screenshotting...\r\n")
                            sys.stdout.flush()
                            logger.info(f"[ASSIST] Auto-detected submission for {company}")
                            try:
                                subprocess.Popen([
                                    "osascript", "-e",
                                    f'display notification "Auto-detected submission for {company}!" '
                                    f'with title "APPLIED" sound name "Hero"'
                                ])
                            except Exception:
                                pass
                        else:
                            logger.info(f"[ASSIST] Page closed for {company}")
                    elif esc_task and esc_task in done:
                        if not self.esc_monitor.is_manual:
                            # ESC pressed = back to auto mode, move to next job
                            logger.info(f"[ASSIST] User pressed ESC — moving to next job")
                            sys.stdout.write("  >>> Skipping to next job...\r\n")
                            sys.stdout.flush()
                        else:
                            # Toggled back to manual? Wait for another ESC
                            logger.info(f"[ASSIST] Still in manual mode for {company}")

                except Exception as assist_e:
                    logger.warning(f"[ASSIST] Error: {assist_e}")

            duration = round(_time.time() - start_time, 1)

            # Get fill result from form filler or handler directly
            fill_result = self.form_filler.get_last_fill_result()
            fields_filled = fill_result.get("filled", {})
            fields_missed = fill_result.get("missed", {})

            # If form_filler didn't track anything, check handler's own tracking
            if not fields_filled and hasattr(handler, 'get_fill_result'):
                handler_fill = handler.get_fill_result()
                fields_filled = handler_fill.get("filled", {})
                fields_missed = handler_fill.get("missed", {})

            if error_msg:
                # Already set from timeout
                success = False

            # Get handler status for detailed tracking
            handler_status = getattr(handler, '_last_status', None)

            # Collect AI-answered questions for this job
            questions_answered = {}
            if self.ai_answerer and hasattr(self.ai_answerer, 'session_answers'):
                questions_answered = {
                    a["question"][:80]: a["answer"][:80]
                    for a in self.ai_answerer.session_answers
                }
                # Clear for next job
                self.ai_answerer.session_answers = []

            # Determine outcome folder and save organized logs
            safe_company = "".join(c if c.isalnum() or c in "-_ " else "" for c in company).strip().replace(" ", "_")[:40]
            safe_role = "".join(c if c.isalnum() or c in "-_ " else "" for c in role).strip().replace(" ", "_")[:40]
            timestamp = _time.strftime("%Y%m%d_%H%M%S")
            screenshot_path = None

            # Take screenshot of the final state
            try:
                screenshots_dir = Path("data/screenshots")
                screenshots_dir.mkdir(parents=True, exist_ok=True)
                status_tag = "PASS" if success else "FAIL"
                screenshot_path = screenshots_dir / f"{status_tag}_{safe_company}_{timestamp}.png"
                await page.screenshot(path=str(screenshot_path), full_page=True)
                logger.info(f"Screenshot saved: {screenshot_path}")
            except Exception as e:
                logger.debug(f"Could not take screenshot: {e}")

            # Capture page text as backup confirmation (works even if screenshot fails)
            confirmation_text = ""
            final_url = ""
            try:
                final_url = page.url
                body_text = await page.text_content("body") or ""
                # Extract key confirmation phrases
                for phrase in ["thank you", "application received", "application submitted",
                               "successfully applied", "we've received", "application complete",
                               "already applied", "error", "required field"]:
                    if phrase in body_text.lower():
                        # Get surrounding context (200 chars around the match)
                        idx = body_text.lower().index(phrase)
                        start = max(0, idx - 50)
                        end = min(len(body_text), idx + 150)
                        confirmation_text = body_text[start:end].strip()
                        break
                if not confirmation_text and len(body_text) > 0:
                    # Just grab the first 300 chars as fallback
                    confirmation_text = body_text[:300].strip()
            except Exception:
                pass

            # Build detailed application record
            app_record = {
                "timestamp": _time.strftime("%Y-%m-%d %H:%M:%S"),
                "company": company,
                "role": role,
                "url": url,
                "final_url": final_url,
                "ats_type": ats_type.value if ats_type else "unknown",
                "attempt": attempts + 1,
                "duration_seconds": round(duration, 1),
                "success": success,
                "handler_status": handler_status,
                "fields_filled": fields_filled,
                "fields_missed": fields_missed,
                "questions_answered": questions_answered,
                "screenshot": str(screenshot_path) if screenshot_path else None,
                "confirmation_text": confirmation_text,
            }

            if success:
                is_dry_run = self.config.get("preferences", {}).get("dry_run", False)
                if is_dry_run:
                    # Don't mark as applied in dry run — reset to pending
                    await self.queue.reset_job(job_id)
                    self.stats["applied"] += 1
                    logger.info(f"[DRY RUN PASS] {company} — {role} ({duration}s)")
                else:
                    await self.queue.mark_applied(job_id, f"Applied via {ats_type.value}")
                    self.stats["applied"] += 1
                    logger.info(f"[PASS] {company} — {role} ({duration}s)")

                # Save to successful folder
                self._save_application_log("successful", safe_company, safe_role, timestamp, app_record, screenshot_path)

                # Track successful application
                if self.tracker:
                    self.tracker.record_application(
                        job_data=job_data,
                        status="submitted",
                        fields_filled=fields_filled,
                        fields_missed=fields_missed,
                        questions_answered=questions_answered
                    )

                return True
            else:
                # FILL-ONLY MODE (Ashby): form filled, browser stays open for manual submit
                if handler_status == "fill_only":
                    logger.info(f"[FILL-ONLY] {company} — {role}: Form filled, waiting for manual submit")
                    # Don't mark as failed or skipped — leave as pending for retry
                    # Browser stays open via the finally block
                    app_record["status"] = "fill_only"
                    self._save_application_log("fill_only", safe_company, safe_role, timestamp, app_record, screenshot_path)
                    return False

                # Check if handler already flagged as closed
                is_closed = handler_status == "closed"

                # Also check page if handler didn't flag it
                if not is_closed:
                    try:
                        is_closed = await handler.is_job_closed(page)
                    except Exception:
                        pass

                if is_closed:
                    await self.queue.mark_skipped(job_id, "Job closed/unavailable")
                    self.stats["skipped"] += 1
                    logger.info(f"[CLOSED] {company} — job is no longer available")
                    _close_tab = True  # Nothing to manually fix

                    app_record["error"] = "Job closed/unavailable"
                    self._save_application_log("skipped", safe_company, safe_role, timestamp, app_record, screenshot_path)

                    if self.tracker:
                        self.tracker.record_application(
                            job_data=job_data,
                            status="skipped",
                            error_message="Job closed/unavailable",
                            questions_answered=questions_answered
                        )

                    return False

                # Check if login required
                if handler_status == "login_required":
                    _close_tab = True  # Nothing to manually fix for login walls
                    # Workday and iCIMS have auth flows — retry up to 3 times
                    ats_with_auth = ("workday", "icims")
                    if ats_type.value in ats_with_auth and attempts < 2:
                        await self.queue.mark_failed(job_id, "Login auth failed (retryable)", retry=True)
                        self.stats["failed"] += 1
                        logger.info(f"[LOGIN] {company} — auth failed, will retry (attempt {attempts + 1}/3)")
                    else:
                        await self.queue.mark_skipped(job_id, "Login required")
                        self.stats["skipped"] += 1
                        logger.info(f"[LOGIN] {company} — requires login, skipping")

                    app_record["error"] = "Login/account required"
                    self._save_application_log("skipped" if ats_type.value not in ats_with_auth or attempts >= 2 else "failed",
                                               safe_company, safe_role, timestamp, app_record, screenshot_path)

                    if self.tracker:
                        self.tracker.record_application(
                            job_data=job_data,
                            status="skipped" if ats_type.value not in ats_with_auth or attempts >= 2 else "failed",
                            error_message="Login/account required",
                            questions_answered=questions_answered
                        )

                    return False

                # Check if CAPTCHA blocked - limit retries
                # Also check handler_status for handlers that use nodriver (SmartRecruiters)
                captcha_blocked = handler_status == "captcha_blocked"
                if not captcha_blocked:
                    try:
                        captcha_blocked = await handler.has_captcha(page)
                    except Exception:
                        pass
                if captcha_blocked:
                    _close_tab = True  # Nothing to manually fix for CAPTCHA
                    error_msg = "CAPTCHA blocked"
                    if attempts >= 2:
                        await self.queue.mark_skipped(job_id, "CAPTCHA blocked (max retries)")
                        self.stats["skipped"] += 1
                    else:
                        await self.queue.mark_failed(job_id, "CAPTCHA blocked", retry=True)
                        self.stats["failed"] += 1

                    app_record["error"] = error_msg
                    self._save_application_log("failed", safe_company, safe_role, timestamp, app_record, screenshot_path)

                    if self.tracker:
                        self.tracker.record_application(
                            job_data=job_data,
                            status="failed",
                            fields_filled=fields_filled,
                            fields_missed=fields_missed,
                            error_message=error_msg,
                            questions_answered=questions_answered
                        )

                    return False

                # SPAM FLAG HANDLING — never retry, skip all remaining jobs of this ATS
                if handler_status == "spam_flagged":
                    error_msg = "SPAM FLAGGED — email burned, skipping permanently"
                    await self.queue.mark_skipped(job_id, error_msg)
                    self.stats["skipped"] += 1
                    logger.error(f"[SPAM] {company} — {role}: {error_msg}")
                    # Skip ALL remaining jobs of this ATS type to prevent further damage
                    ats_type_str = job_data.get("ats_type", "")
                    if ats_type_str:
                        skip_count = await self._skip_all_ats_jobs(ats_type_str, "Spam flagged — ATS disabled")
                        logger.error(f"SAFETY: Skipped {skip_count} remaining {ats_type_str} jobs due to spam flag")

                    app_record["error"] = error_msg
                    self._save_application_log("failed", safe_company, safe_role, timestamp, app_record, screenshot_path)
                    if self.tracker:
                        self.tracker.record_application(
                            job_data=job_data, status="spam_flagged",
                            fields_filled=fields_filled, fields_missed=fields_missed,
                            error_message=error_msg, questions_answered=questions_answered
                        )
                    return False

                # Get error message from page or handler status
                page_error = await handler.get_error_message(page)
                error_msg = page_error or handler_status or "Application failed"
                await self.queue.mark_failed(job_id, error_msg, retry=(attempts < 2))
                self.stats["failed"] += 1
                logger.warning(f"[FAIL] {company} — {role}: {error_msg} ({duration}s)")

                app_record["error"] = error_msg
                self._save_application_log("failed", safe_company, safe_role, timestamp, app_record, screenshot_path)

                if self.tracker:
                    self.tracker.record_application(
                        job_data=job_data,
                        status="failed",
                        fields_filled=fields_filled,
                        fields_missed=fields_missed,
                        error_message=error_msg,
                        questions_answered=questions_answered
                    )

                return False

        except Exception as e:
            error_msg = str(e)
            logger.error(f"[ERROR] {company} — {role}: {error_msg}")

            # Don't retry on certain errors
            no_retry_errors = ["404", "not found", "closed", "timeout"]
            should_retry = not any(err in error_msg.lower() for err in no_retry_errors)
            should_retry = should_retry and attempts < 2

            if should_retry:
                await self.queue.mark_failed(job_id, error_msg, retry=True)
                self.stats["failed"] += 1
            else:
                await self.queue.mark_skipped(job_id, error_msg)
                self.stats["skipped"] += 1

            if self.tracker:
                self.tracker.record_application(
                    job_data=job_data,
                    status="failed",
                    fields_filled=fields_filled,
                    fields_missed=fields_missed,
                    error_message=error_msg,
                    questions_answered=questions_answered
                )

            return False

        finally:
            if success and screenshot_path and Path(screenshot_path).exists():
                # SUCCESS with screenshot — close this tab, move on
                logger.info(f"[BROWSER] SUCCESS + SCREENSHOT — closing tab")
                try:
                    sys.stdout.write("\a\a\a")  # Triple bell
                    sys.stdout.write(f"\r\n{'='*60}\r\n")
                    sys.stdout.write(f"  >>> APPLIED SUCCESSFULLY — {company} — {role}\r\n")
                    sys.stdout.write(f"  >>> Screenshot saved. Moving to next job.\r\n")
                    sys.stdout.write(f"{'='*60}\r\n")
                    sys.stdout.flush()
                except Exception:
                    pass
                # Close successful tab to keep browser clean
                try:
                    if page and not page.is_closed():
                        await page.close()
                except Exception:
                    pass
                await asyncio.sleep(2)
            elif _close_tab:
                # SKIPPED/CLOSED job — close tab, nothing to manually fix
                logger.info(f"[BROWSER] Skipped job — closing tab")
                try:
                    if page and not page.is_closed():
                        await page.close()
                except Exception:
                    pass
                await asyncio.sleep(1)
            elif _timed_out:
                # TIMEOUT — leave tab open for manual review, move on
                logger.warning(f"[BROWSER] Tab left open for manual review (timed out): {company} — {role}")
                try:
                    sys.stdout.write(f"\r\n{'='*60}\r\n")
                    sys.stdout.write(f"  MANUAL REVIEW NEEDED: {company} — {role}\r\n")
                    sys.stdout.write(f"  Tab left open — fill and submit it yourself.\r\n")
                    sys.stdout.write(f"  Moving to next job...\r\n")
                    sys.stdout.write(f"{'='*60}\r\n")
                    sys.stdout.flush()
                except Exception:
                    pass
                await asyncio.sleep(1)
            else:
                # FAILURE — close tab and move on autonomously, no manual help needed
                logger.warning(f"[BROWSER] Closing failed tab — moving on autonomously.")
                try:
                    if page and not page.is_closed():
                        await page.close()
                except Exception:
                    pass
                await asyncio.sleep(1)

    def _save_application_log(self, outcome: str, company: str, role: str, timestamp: str,
                              record: dict, screenshot_path=None):
        """Save detailed application log to organized folders.

        Folder structure:
          data/applications/successful/CompanyName_Role_20260216_012345/
            summary.json    — all fields, answers, metadata
            screenshot.png  — copy of the final screenshot
          data/applications/failed/CompanyName_Role_20260216_012345/
            summary.json
            screenshot.png
          data/applications/skipped/CompanyName_Role_20260216_012345/
            summary.json
            screenshot.png
        """
        import json, shutil

        try:
            folder_name = f"{company}_{role}_{timestamp}"
            folder = Path(f"data/applications/{outcome}/{folder_name}")
            folder.mkdir(parents=True, exist_ok=True)

            # Save summary JSON
            summary_path = folder / "summary.json"
            with open(summary_path, "w") as f:
                json.dump(record, f, indent=2, default=str)

            # Copy screenshot into the folder
            if screenshot_path and Path(screenshot_path).exists():
                shutil.copy2(str(screenshot_path), str(folder / "screenshot.png"))

            logger.info(f"Application log saved: {folder}")
        except Exception as e:
            logger.debug(f"Could not save application log: {e}")

    async def run_application_loop(self, max_applications: int = 0):
        """Run the main application loop. max_applications=0 means unlimited."""
        if max_applications > 0:
            logger.info(f"Starting application loop (max: {max_applications})")
        else:
            logger.info("Starting application loop (processing ALL jobs)")

        # Count total actionable jobs for progress display
        total_pending = await self.queue.get_pending_count()
        target = min(total_pending, max_applications) if max_applications > 0 else total_pending

        # Start ESC monitor for manual/auto toggle (ALL modes)
        if not self.esc_monitor and sys.stdin.isatty():
            self.esc_monitor = EscMonitor()
            self.esc_monitor.start(asyncio.get_event_loop())

        applications = 0
        preferences = self.config.get("preferences", {})
        max_per_hour = preferences.get("max_applications_per_hour", 10)
        delay_seconds = preferences.get("delay_between_applications_seconds", 30)
        import time as _time_loop
        _batch_start = _time_loop.time()

        logger.info(f"\n{'#'*60}")
        logger.info(f"  TARGET: {target} jobs to process")
        logger.info(f"  Rate: max {max_per_hour}/hour, {delay_seconds}s delay")
        logger.info(f"  Session stats: {self.stats['applied']} applied so far")
        logger.info(f"{'#'*60}\n")

        # ATS filter from config
        ats_filter_str = preferences.get("ats_filter", None)
        ats_filter_type = None
        if ats_filter_str:
            try:
                ats_filter_type = ATSType(ats_filter_str)
            except ValueError:
                logger.warning(f"Unknown ATS filter: {ats_filter_str} — processing all")

        consecutive_failures = 0
        MAX_CONSECUTIVE_FAILURES = 3  # Stop after 3 failures in a row
        open_manual_tabs = 0
        max_open_tabs = getattr(self, '_max_open_tabs', 0)  # 0 = unlimited
        self._failed_urls = []  # Track failed job URLs for manual tab opening

        while max_applications == 0 or applications < max_applications:
            # Get next job (with optional ATS filter)
            job = await self.queue.get_next_job(ats_type=ats_filter_type, url_patterns=self._url_patterns)
            if not job:
                logger.info("No more jobs in queue — all done!")
                break

            # Check blacklists
            if self._should_skip_job(job):
                await self.queue.mark_skipped(job["id"], "Blacklisted/login-required")
                self.stats["skipped"] += 1
                continue

            # If in manual mode (ESC was pressed between jobs), wait for resume
            if self.esc_monitor and self.esc_monitor.is_manual:
                sys.stdout.write("\r\n  >>> PAUSED between jobs. Press ESC to resume bot. <<<\r\n")
                sys.stdout.flush()
                await self.esc_monitor.wait_for_toggle()
                # Reset failure counter when user resumes — they chose to continue
                consecutive_failures = 0

            # Apply with progress tracking
            applications += 1
            applied_before = self.stats.get("applied", 0)
            await self.apply_to_job(job, job_index=applications, total_jobs=target)
            applied_after = self.stats.get("applied", 0)

            # Track consecutive failures — stop spamming if nothing works
            if applied_after > applied_before:
                consecutive_failures = 0  # Reset on success
            else:
                consecutive_failures += 1

            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                logger.warning(
                    f"\n  {MAX_CONSECUTIVE_FAILURES} consecutive failures — skipping problematic jobs and continuing."
                )
                consecutive_failures = 0  # Reset and keep going — don't stop

            # Log progress summary
            pending = await self.queue.get_pending_count()
            pct = (applications / target * 100) if target > 0 else 0
            logger.info(
                f"\n  PROGRESS: {applications}/{target} ({pct:.0f}%) | "
                f"Applied: {self.stats['applied']} | "
                f"Failed: {self.stats['failed']} | "
                f"Skipped: {self.stats['skipped']} | "
                f"Remaining: {pending}"
            )

            # Per-ATS rate limiting — each ATS has different bot detection
            # Ashby: burned email, fill-only mode, still space out to look human
            # Greenhouse: velocity monitoring, invisible reCAPTCHA
            # Lever: reCAPTCHA Enterprise + email verification
            # SmartRecruiters: DataDome anti-bot per page
            # Workday: login walls, session-based detection
            ats_delays = {
                "ashby": 120,          # 30/hour — BURNED, max spacing to avoid further flags
                "greenhouse": 30,      # 120/hour — highest success, keep fast
                "lever": 90,           # 40/hour — heavy CAPTCHA, space out
                "smartrecruiters": 60, # 60/hour — DataDome per-page checks
                "workday": 90,         # 40/hour — login walls, session detection
                "icims": 60,           # 60/hour — login walls
                "unknown": 45,         # 80/hour — varies
            }
            job_ats = job.get("ats_type", "unknown")
            ats_delay = max(ats_delays.get(job_ats, delay_seconds), delay_seconds)

            # Per-COMPANY spacing — don't hit the same company back-to-back
            # Skip cooldown if job was closed/skipped (no form engagement occurred)
            job_company = job.get("company", "").lower()
            if not hasattr(self, '_last_company_time'):
                self._last_company_time = {}
            now = _time_loop.time()
            handler = self.handlers.get(job.get("ats_type", "unknown"), self.handlers.get("unknown"))
            last_handler_status = getattr(handler, "_last_status", "failed") if handler else "failed"
            skip_company_cooldown = last_handler_status in ("closed", "skipped", "skipped_by_user", "login_required")
            if job_company in self._last_company_time and not skip_company_cooldown:
                since_last = now - self._last_company_time[job_company]
                company_cooldown = 120  # 2 min minimum between same company
                if since_last < company_cooldown:
                    extra_wait = company_cooldown - since_last
                    logger.info(f"Same company ({job_company}) cooldown — waiting {extra_wait:.0f}s extra")
                    ats_delay += extra_wait
            self._last_company_time[job_company] = now + ats_delay

            if max_applications == 0 or applications < max_applications:
                logger.debug(f"Waiting {ats_delay:.0f}s before next (ATS: {job_ats}, company: {job_company})...")
                await asyncio.sleep(ats_delay)

            # Hourly rate limit check — dynamic pause based on elapsed time
            if applications % max_per_hour == 0 and applications > 0:
                elapsed = _time_loop.time() - _batch_start
                remaining_in_hour = max(0, 3600 - elapsed)
                if remaining_in_hour > 60:
                    pause = min(remaining_in_hour, 600)  # Cap at 10 min
                    logger.info(f"Hourly rate limit ({max_per_hour}/hr) reached, pausing {pause:.0f}s...")
                    await asyncio.sleep(pause)
                # Reset batch timer for next hour window
                _batch_start = _time_loop.time()

        # Restore terminal
        if self.esc_monitor:
            self.esc_monitor.stop()
            self.esc_monitor = None

        logger.info(f"\n{'='*60}")
        logger.info(f"APPLICATION LOOP COMPLETE")
        logger.info(f"  Applied:  {self.stats['applied']}")
        logger.info(f"  Failed:   {self.stats['failed']}")
        logger.info(f"  Skipped:  {self.stats['skipped']}")
        logger.info(f"{'='*60}")

        # Print detailed session report
        if self.tracker:
            self.tracker.print_session_report()
            report_path = self.tracker.save_session_report()
            logger.info(f"Session report saved: {report_path}")

    async def _skip_all_ats_jobs(self, ats_type: str, reason: str) -> int:
        """Skip ALL pending jobs for a given ATS type. Used when spam is detected."""
        try:
            cursor = await self.queue._db.execute(
                "UPDATE jobs SET status = 'skipped', error_message = ? WHERE ats_type = ? AND status IN ('pending', 'failed')",
                (reason, ats_type),
            )
            await self.queue._db.commit()
            count = cursor.rowcount
            logger.warning(f"Skipped {count} {ats_type} jobs: {reason}")
            return count
        except Exception as e:
            logger.error(f"Failed to skip {ats_type} jobs: {e}")
            return 0

    def _should_skip_job(self, job: Dict[str, Any]) -> bool:
        """Check if job should be skipped based on preferences."""
        preferences = self.config.get("preferences", {})
        blacklist_companies = preferences.get("blacklist_companies", [])
        blacklist_locations = preferences.get("blacklist_locations", [])
        skip_login_required = preferences.get("skip_login_required", True)

        company = job.get("company", "").lower()
        location = job.get("location", "").lower()
        ats_type = job.get("ats_type", "").lower()

        # Skip ATS types that require login (but NOT workday/icims — we have handlers for them)
        if skip_login_required and ats_type in ("taleo", "successfactors"):
            logger.info(f"Skipping {company} - {ats_type} requires login")
            return True

        for blacklisted in blacklist_companies:
            if blacklisted.lower() in company:
                return True

        for blacklisted in blacklist_locations:
            if blacklisted.lower() in location:
                return True

        return False

    async def watch_and_apply(self):
        """Watch for new jobs and apply automatically."""
        logger.info("Starting watch and apply mode...")

        # Initial fetch
        await self.fetch_and_queue_jobs()

        # Start watching
        async def watch_task():
            await self.watcher.watch()

        async def apply_task():
            while self.running:
                pending = await self.queue.get_pending_count()
                if pending > 0:
                    await self.run_application_loop(max_applications=10)
                await asyncio.sleep(60)  # Check every minute

        self.running = True

        try:
            await asyncio.gather(
                watch_task(),
                apply_task(),
            )
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("Stopping...")
        finally:
            self.running = False

    async def backfill(self, max_applications: int = 0):
        """Apply to all existing jobs in the queue. max_applications=0 means ALL."""
        logger.info("Starting backfill mode - processing all jobs until done...")

        # Fetch all jobs first
        await self.fetch_and_queue_jobs()

        # Show initial stats
        await self.show_stats()

        # Apply to all (0 = unlimited)
        await self.run_application_loop(max_applications=max_applications)

    async def apply_to_url(self, url: str):
        """Apply to a single URL."""
        logger.info(f"Applying to single URL: {url}")

        # Look up job in database by URL first
        existing = await self.queue.get_job_by_url(url)
        if existing:
            job_data = dict(existing)
            logger.info(f"Found job in DB: {job_data.get('company', 'Unknown')} — {job_data.get('role', 'Unknown')} (id={job_data['id']})")
        else:
            # Not in DB — detect ATS type and create a temporary entry
            from job_parser import JobParser
            parser = JobParser()
            ats_type = parser.detect_ats(url)

            # Insert into DB so we get a real ID
            new_id = await self.queue.add_job_url(url, ats_type.value)
            job_data = {
                "id": new_id,
                "url": url,
                "company": "Unknown",
                "role": "Unknown",
                "ats_type": ats_type.value,
            }
            logger.info(f"Created new job entry (id={new_id})")

        await self.apply_to_job(job_data)

    async def export_applications(self, filepath: str):
        """Export applications to CSV."""
        await self.queue.export_to_csv(filepath)
        logger.info(f"Exported to {filepath}")

    async def show_stats(self):
        """Show queue statistics."""
        stats = await self.queue.get_stats()
        print("\n" + "=" * 50)
        print("JOB QUEUE STATISTICS")
        print("=" * 50)
        print(f"Total jobs:    {stats.get('total', 0)}")
        print(f"Pending:       {stats.get('pending', 0)}")
        print(f"Applied:       {stats.get('applied', 0)}")
        print(f"Failed:        {stats.get('failed', 0)}")
        print(f"Skipped:       {stats.get('skipped', 0)}")
        print(f"In Progress:   {stats.get('in_progress', 0)}")
        print("=" * 50 + "\n")

    def _open_manual_tabs(self, urls: list):
        """Open URLs as tabs in a single Chrome window (survives process exit)."""
        import subprocess
        if not urls:
            return
        # Dedupe URLs
        unique_urls = list(dict.fromkeys(urls))
        # Save URLs to file for reference
        urls_file = Path("data/manual_tabs.txt")
        urls_file.write_text("\n".join(unique_urls))
        logger.info(f"Saved {len(unique_urls)} unique URLs to {urls_file}")
        # Open all URLs in one Chrome window using --new-window for first, rest are tabs
        chrome_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        try:
            subprocess.Popen(
                [chrome_path, "--new-window"] + unique_urls,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            logger.info(f"Opened {len(unique_urls)} tabs in one Chrome window")
        except Exception:
            # Fallback: use 'open' command
            for url in unique_urls:
                try:
                    subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception as e:
                    logger.debug(f"Failed to open {url}: {e}")

    async def cleanup(self):
        """Clean up resources. Browser stays open — user closes it manually."""
        if self.watcher:
            await self.watcher.close()
        if self.queue:
            await self.queue.close()
        # DON'T close browser — tabs stay open for user to review/finish
        # User closes the browser window themselves when done
        logger.info("[CLEANUP] Resources released. Browser stays open — close it yourself when done.")


# CLI Commands
@click.group()
def cli():
    """Internship Auto-Applier CLI"""
    pass


@cli.command()
@click.option("--max", "-m", default=50, help="Maximum applications to submit")
def run(max):
    """Start watching for jobs and applying automatically."""
    async def main():
        app = InternshipAutoApplier()
        try:
            await app.initialize()
            await app.watch_and_apply()
        finally:
            await app.cleanup()

    asyncio.run(main())


@cli.command()
@click.option("--max", "-m", default=0, help="Maximum applications (0=unlimited)")
@click.option("--headless/--headful", default=False, help="Run browser in headless mode")
@click.option("--dry-run", is_flag=True, help="Fill forms but don't submit")
@click.option("--review", is_flag=True, help="Fill forms, pause for your review, YOU click submit")
@click.option("--ats", default=None, help="Only process specific ATS type (greenhouse, lever, ashby, smartrecruiters)")
@click.option("--smart", is_flag=True, help="Enable Gemini form scanner — catches empty fields after handler fill")
@click.option("--with-simplify", is_flag=True, help="Load Simplify Copilot extension for boilerplate autofill")
@click.option("--workday-accounts", is_flag=True, help="Only apply to Workday jobs where we already have accounts (slow mode)")
@click.option("--assist", is_flag=True, help="Assist mode — bot fills what it can, YOU finish the rest, bot submits + screenshots")
@click.option("--max-open-tabs", default=0, help="Max tabs to leave open for manual help (0=unlimited)")
def backfill(max, headless, dry_run, review, ats, smart, with_simplify, workday_accounts, assist, max_open_tabs):
    """Apply to all existing jobs in the database."""
    async def main():
        app = InternshipAutoApplier()
        try:
            await app.initialize()
            # Override headless setting from CLI
            app.browser_manager.headless = headless
            # Re-init handlers with dry_run if needed
            if dry_run:
                await app._init_handlers(dry_run=True)
                app.config.setdefault("preferences", {})["dry_run"] = True
                logger.info("DRY RUN MODE — forms will be filled but NOT submitted")
            if review:
                app.config.setdefault("preferences", {})["review_mode"] = True
                logger.info("REVIEW MODE — forms will be filled, then PAUSED for your review. YOU submit.")

            # ATS filter — only process jobs from a specific ATS
            if ats:
                ats_lower = ats.lower().strip()
                app.config.setdefault("preferences", {})["ats_filter"] = ats_lower
                logger.info(f"ATS FILTER: Only processing {ats_lower} jobs")

            # Smart mode — enable Gemini form scanner as cleanup pass
            if smart:
                app._smart_mode = True
                logger.info("SMART MODE — Gemini will scan for missed fields after each application")

            # Assist mode — bot fills, user finishes, bot submits
            if assist:
                app._assist_mode = True
                app._smart_mode = True  # Always use smart mode with assist
                app.browser_manager.headless = False  # Must be headed for user interaction
                logger.info("ASSIST MODE — bot fills what it can, YOU finish the rest, bot submits + screenshots")

            # Simplify extension — load browser extension for autofill
            if with_simplify:
                ext_mgr = ExtensionManager()
                ext_path = await ext_mgr.ensure_extension()
                if ext_path:
                    app._extension_path = ext_path
                    app.browser_manager.extension_paths = [ext_path]
                    app.browser_manager.headless = False  # Extensions require headed mode
                    logger.info(f"SIMPLIFY EXTENSION loaded from {ext_path}")
                else:
                    logger.warning("Failed to load Simplify extension — continuing without it")

            # Workday accounts only — filter to jobs where we have existing accounts + slow mode
            if workday_accounts:
                import json
                accounts_path = Path("data/workday_accounts.json")
                if accounts_path.exists():
                    with open(accounts_path) as f:
                        accounts = json.load(f)
                    # Build URL LIKE patterns from tenant names (e.g. "amat.wd1" → "%amat.wd%")
                    tenants = list(accounts.keys())
                    url_patterns = [f"%{tenant.split('.')[0]}.wd%" for tenant in tenants]
                    app._url_patterns = url_patterns

                    # Count matching jobs
                    import sqlite3 as _sq
                    _conn = _sq.connect(app.db_path)
                    _c = _conn.cursor()
                    or_clauses = " OR ".join([f"url LIKE ?" for _ in url_patterns])
                    _c.execute(f"SELECT COUNT(*) FROM jobs WHERE status='pending' AND ({or_clauses})", url_patterns)
                    count = _c.fetchone()[0]
                    _conn.close()

                    # Force ATS filter to workday
                    app.config.setdefault("preferences", {})["ats_filter"] = "workday"
                    # Slow mode: 90s between apps, 4/hour max
                    app.config["preferences"]["delay_between_applications_seconds"] = 90
                    app.config["preferences"]["max_applications_per_hour"] = 4
                    app.browser_manager.headless = False  # Headed mode — less suspicious

                    logger.info(f"WORKDAY ACCOUNTS MODE — {len(tenants)} tenants, {count} pending jobs")
                    logger.info(f"  Tenants: {', '.join(tenants)}")
                    logger.info(f"  Rate: 4/hour, 90s delay (slow mode to avoid detection)")
                else:
                    logger.error(f"No accounts file found at {accounts_path}")
                    return

            # Max open tabs — pause when N tabs are open for manual help
            if max_open_tabs > 0:
                app._max_open_tabs = max_open_tabs
                logger.info(f"MAX OPEN TABS: Will pause after {max_open_tabs} tabs left open for manual help")

            await app.backfill(max_applications=max)
        except KeyboardInterrupt:
            logger.info("\nGracefully stopping... saving session report.")
        finally:
            # Always save report on exit
            if app.tracker and app.tracker.session_records:
                app.tracker.print_session_report()
                app.tracker.save_session_report()
            await app.cleanup()

            # If there are open tabs for manual help, keep browser alive
            if app.browser_manager and (app.browser_manager._persistent_context or app.browser_manager._browser):
                open_pages = []
                if app.browser_manager._persistent_context:
                    open_pages = [p for p in app.browser_manager._persistent_context.pages if not p.is_closed()]
                if open_pages:
                    try:
                        sys.stdout.write(f"\r\n{'='*60}\r\n")
                        sys.stdout.write(f"  DONE — {len(open_pages)} tab(s) still open for manual help\r\n")
                        sys.stdout.write(f"  Fill out the remaining fields and submit manually.\r\n")
                        sys.stdout.write(f"  Press Enter here when you're done to close browser.\r\n")
                        sys.stdout.write(f"{'='*60}\r\n")
                        sys.stdout.flush()
                        await asyncio.get_event_loop().run_in_executor(None, input)
                    except (EOFError, KeyboardInterrupt):
                        pass
                # Now close browser
                await app.browser_manager.close()

    asyncio.run(main())


@cli.command()
@click.option("--max", "-m", default=0, help="Max jobs to assist (0=all failed)")
@click.option("--ats", default=None, help="Only assist specific ATS type")
def assist(max, ats):
    """Retry failed jobs with human assist — bot fills, YOU finish, bot submits + screenshots."""
    async def main():
        app = InternshipAutoApplier()
        try:
            await app.initialize()
            app._assist_mode = True
            app._smart_mode = True
            app.browser_manager.headless = False

            # Reset failed jobs back to pending for retry
            ats_filter = ""
            params = []
            if ats:
                ats_filter = " AND ats_type = ?"
                params.append(ats.lower().strip())

            cursor = await app.queue._db.execute(
                f"SELECT COUNT(*) FROM jobs WHERE status = 'failed'{ats_filter}",
                params,
            )
            failed_count = (await cursor.fetchone())[0]

            if failed_count == 0:
                print("No failed jobs to assist with!")
                return

            target = min(failed_count, max) if max > 0 else failed_count
            print(f"\n{'='*60}")
            print(f"  ASSIST MODE — {failed_count} failed jobs available")
            print(f"  Will process: {target}")
            print(f"  Bot fills what it can → YOU fix the rest → bot submits")
            print(f"{'='*60}\n")

            # Reset failed jobs to pending
            await app.queue._db.execute(
                f"UPDATE jobs SET status = 'pending', attempts = 0 WHERE status = 'failed'{ats_filter}",
                params,
            )
            await app.queue._db.commit()
            logger.info(f"Reset {failed_count} failed jobs to pending for assist")

            if ats:
                app.config.setdefault("preferences", {})["ats_filter"] = ats.lower().strip()

            await app.run_application_loop(max_applications=target)
        except KeyboardInterrupt:
            logger.info("\nStopping assist mode...")
        finally:
            if app.tracker and app.tracker.session_records:
                app.tracker.print_session_report()
                app.tracker.save_session_report()
            await app.cleanup()

    asyncio.run(main())


@cli.command()
@click.argument("url")
@click.option("--dry-run", is_flag=True, help="Fill form but don't submit")
@click.option("--review", is_flag=True, help="Fill form, pause for your review, YOU click submit")
@click.option("--smart", is_flag=True, help="Enable Gemini form scanner")
@click.option("--with-simplify", is_flag=True, help="Load Simplify Copilot extension")
def apply(url, dry_run, review, smart, with_simplify):
    """Apply to a single job URL."""
    async def main():
        app = InternshipAutoApplier()
        try:
            await app.initialize()
            if dry_run:
                await app._init_handlers(dry_run=True)
                app.config.setdefault("preferences", {})["dry_run"] = True
                logger.info("DRY RUN MODE — form will be filled but NOT submitted")
            if review:
                app.config.setdefault("preferences", {})["review_mode"] = True
                logger.info("REVIEW MODE — form will be filled, then PAUSED for your review. YOU submit.")
            if smart:
                app._smart_mode = True
                logger.info("SMART MODE enabled")
            if with_simplify:
                ext_mgr = ExtensionManager()
                ext_path = await ext_mgr.ensure_extension()
                if ext_path:
                    app._extension_path = ext_path
                    app.browser_manager.extension_paths = [ext_path]
                    app.browser_manager.headless = False
                    logger.info(f"SIMPLIFY EXTENSION loaded from {ext_path}")
            await app.apply_to_url(url)
        finally:
            await app.cleanup()

    asyncio.run(main())


@cli.command()
def fetch():
    """Fetch jobs from SimplifyJobs and add to queue."""
    async def main():
        app = InternshipAutoApplier()
        try:
            # Minimal init — fetch doesn't need full config
            app.config = {}
            if app.config_path.exists():
                with open(app.config_path) as f:
                    app.config = yaml.safe_load(f) or {}
            app.queue = JobQueue(app.db_path)
            await app.queue.initialize()
            app.parser = JobParser()
            app.watcher = GitHubWatcher(poll_interval=300)
            await app.fetch_and_queue_jobs()
            await app.show_stats()
        finally:
            await app.cleanup()

    asyncio.run(main())


@cli.command()
def stats():
    """Show queue statistics."""
    async def main():
        app = InternshipAutoApplier()
        try:
            # Minimal init for stats
            app.queue = JobQueue(app.db_path)
            await app.queue.initialize()
            await app.show_stats()
        finally:
            await app.cleanup()

    asyncio.run(main())


@cli.command()
@click.argument("filepath", default="applications.csv")
def export(filepath):
    """Export applications to CSV."""
    async def main():
        app = InternshipAutoApplier()
        try:
            await app.initialize()
            await app.export_applications(filepath)
        finally:
            await app.cleanup()

    asyncio.run(main())


@cli.command()
def setup():
    """Check your config is ready to go. Run this before applying."""
    import yaml as _yaml

    print("\n" + "=" * 60)
    print("  INTERNSHIP AUTO-APPLIER — CONFIG CHECK")
    print("=" * 60)

    config_path = Path("config/master_config.yaml")
    secrets_path = Path("config/secrets.yaml")

    # Check config exists
    if not config_path.exists():
        print("\n[FAIL] config/master_config.yaml not found!")
        print("       Copy the template and fill in your info.")
        return

    with open(config_path) as f:
        config = _yaml.safe_load(f) or {}

    secrets = {}
    if secrets_path.exists():
        with open(secrets_path) as f:
            secrets = _yaml.safe_load(f) or {}

    errors = []
    warnings = []
    filled = []

    personal = config.get("personal_info", {})

    # Required personal fields
    required_fields = {
        "first_name": "First Name",
        "last_name": "Last Name",
        "full_name": "Full Name",
        "email": "Email",
        "phone": "Phone",
        "city": "City",
        "state": "State",
        "linkedin": "LinkedIn URL",
    }

    print("\n-- Personal Info --")
    for key, label in required_fields.items():
        val = personal.get(key, "")
        if val and str(val).strip():
            print(f"  [OK]   {label}: {val}")
            filled.append(key)
        else:
            print(f"  [MISS] {label}: (empty)")
            errors.append(f"personal_info.{key}")

    # Check for mock data
    mock_indicators = ["jon doe", "jane doe", "john doe", "jondoe", "555-", "example.com"]
    for key in ["first_name", "last_name", "email", "phone", "linkedin"]:
        val = str(personal.get(key, "")).lower()
        for mock in mock_indicators:
            if mock in val:
                print(f"  [WARN] {key} looks like placeholder data!")
                errors.append(f"personal_info.{key} has placeholder data")
                break

    # Education
    print("\n-- Education --")
    edu = config.get("education", [])
    if edu and edu[0].get("school"):
        print(f"  [OK]   School: {edu[0]['school']}")
        print(f"  [OK]   Degree: {edu[0].get('degree', '')} in {edu[0].get('field_of_study', '')}")
        print(f"  [OK]   Graduation: {edu[0].get('graduation_date', '')}")
    else:
        print("  [MISS] No education filled in")
        warnings.append("education")

    # Resume
    print("\n-- Files --")
    resume = config.get("files", {}).get("resume", "")
    if resume and Path(resume).exists():
        size = Path(resume).stat().st_size
        print(f"  [OK]   Resume: {resume} ({size:,} bytes)")
    elif resume:
        print(f"  [FAIL] Resume not found: {resume}")
        errors.append(f"Resume file missing: {resume}")
    else:
        print("  [MISS] No resume path configured")
        errors.append("files.resume")

    # API Key
    print("\n-- API Keys --")
    api_key = secrets.get("gemini_api_key", "")
    use_ai = config.get("preferences", {}).get("use_ai_for_custom_questions", False)
    if api_key:
        print(f"  [OK]   Gemini API Key: {api_key[:10]}...")
    else:
        if use_ai:
            print("  [WARN] No Gemini API key — AI answers disabled")
            warnings.append("gemini_api_key")
        else:
            print("  [INFO] No Gemini API key (AI answers disabled)")

    # Work Authorization
    print("\n-- Work Authorization --")
    wa = config.get("work_authorization", {})
    if wa.get("us_work_authorized"):
        print(f"  [OK]   US Work Authorized: Yes")
        print(f"  [OK]   Sponsorship needed: {'Yes' if wa.get('require_sponsorship_future') else 'No'}")
    else:
        print("  [WARN] us_work_authorized is false — check if correct")
        warnings.append("work_authorization")

    # Database stats
    print("\n-- Job Database --")
    try:
        import sqlite3
        db = sqlite3.connect("data/jobs.db")
        cur = db.execute("SELECT ats_type, COUNT(*) FROM jobs WHERE status = 'pending' GROUP BY ats_type ORDER BY COUNT(*) DESC")
        rows = cur.fetchall()
        total_pending = sum(r[1] for r in rows)
        actionable = sum(r[1] for r in rows if r[0] in ("greenhouse", "lever", "ashby", "smartrecruiters"))
        print(f"  Total pending: {total_pending}")
        print(f"  Actionable (no-login ATS): {actionable}")
        for ats, count in rows:
            marker = "*" if ats in ("greenhouse", "lever", "ashby", "smartrecruiters") else " "
            print(f"    {marker} {ats}: {count}")
        cur2 = db.execute("SELECT COUNT(*) FROM jobs WHERE status = 'applied'")
        applied = cur2.fetchone()[0]
        print(f"  Already applied: {applied}")
        db.close()
    except Exception as e:
        print(f"  Could not read database: {e}")

    # Summary
    print("\n" + "=" * 60)
    if errors:
        print(f"  STATUS: NOT READY — {len(errors)} issue(s) to fix")
        print(f"\n  Fix these in config/master_config.yaml:")
        for e in errors:
            print(f"    - {e}")
        print(f"\n  Then run: python src/main.py setup")
    else:
        print("  STATUS: READY TO GO!")
        if warnings:
            print(f"  ({len(warnings)} warning(s) — optional fixes)")
        print(f"\n  Next steps:")
        print(f"    1. Dry run:  python src/main.py backfill --dry-run --max 3")
        print(f"    2. For real: python src/main.py backfill --max 10")
        print(f"    3. Full run: python src/main.py backfill")
    print("=" * 60 + "\n")


@cli.command(name="review-questions")
def review_questions():
    """Review and approve/edit/reject pending question answers."""
    from question_verifier import QuestionVerifier

    verifier = QuestionVerifier()
    pending = verifier.get_pending_reviews()

    if not pending:
        print("\nNo questions pending review.")
        stats = verifier.get_stats()
        print(f"  Verified answers: {stats['verified_answers']}")
        print(f"  Previously approved: {stats['approved']}")
        print(f"  Previously rejected: {stats['rejected']}")
        print()
        return

    print(f"\n{'=' * 62}")
    print(f"  Question Review Queue — {len(pending)} pending")
    print(f"{'=' * 62}\n")

    reviewed = 0
    for i, item in enumerate(pending):
        print(f"─── Question {i + 1} of {len(pending)} {'─' * 40}\n")
        print(f"  Q: {item['question_text']}")
        print(f"  Proposed: {item['proposed_answer']}")
        print(f"  Source: {item['source']} | Company: {item['company'] or 'unknown'} | Type: {item['field_type']}")

        if item['options'] and item['options'] != '[]':
            import json
            try:
                opts = json.loads(item['options'])
                if opts:
                    print(f"  Options: {', '.join(opts)}")
            except Exception:
                pass

        print()
        print("  [a]pprove  [e]dit  [r]eject  [s]kip  [q]uit")
        print()

        try:
            choice = input("  > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n\nExiting review.")
            break

        if choice == 'q':
            print("\nExiting review.")
            break
        elif choice == 'a':
            verifier.approve_answer(item['id'])
            print("  Approved.\n")
            reviewed += 1
        elif choice == 'e':
            print("  Enter your answer (type your answer, then press Enter):")
            try:
                new_answer = input("  > ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n  Skipped.\n")
                continue
            if new_answer:
                verifier.approve_answer(item['id'], answer=new_answer)
                print(f"  Saved with edited answer.\n")
                reviewed += 1
            else:
                print("  Empty answer — skipped.\n")
        elif choice == 'r':
            verifier.reject_answer(item['id'])
            print("  Rejected.\n")
            reviewed += 1
        elif choice == 's':
            print("  Skipped.\n")
        else:
            print("  Unknown choice — skipped.\n")

    print(f"\n{'=' * 62}")
    print(f"  Reviewed {reviewed} questions this session")
    stats = verifier.get_stats()
    print(f"  Total verified: {stats['verified_answers']} | Pending: {stats['pending_review']}")
    print(f"{'=' * 62}\n")


def _get_response_tracker() -> EmailResponseTracker:
    """Create an EmailResponseTracker from secrets config."""
    secrets_path = Path("config/secrets.yaml")
    if not secrets_path.exists():
        raise click.ClickException("config/secrets.yaml not found")

    with open(secrets_path) as f:
        secrets = yaml.safe_load(f) or {}

    gmail_config = secrets.get("gmail", {})
    gmail_email = gmail_config.get("email")
    app_password = gmail_config.get("app_password")

    if not gmail_email or not app_password:
        raise click.ClickException(
            "Gmail not configured in config/secrets.yaml. "
            "Set gmail.email and gmail.app_password."
        )

    return EmailResponseTracker(
        gmail_email=gmail_email,
        app_password=app_password,
    )


@cli.command(name="check-responses")
@click.option("--days", default=30, help="How many days back to search")
@click.option("--limit", default=500, help="Max emails to fetch")
@click.option("--category", default=None,
              type=click.Choice(["rejection", "follow_up", "assessment",
                                 "interview_invite", "offer", "other"]),
              help="Filter by response category")
def check_responses(days, limit, category):
    """Scan Gmail for responses to your applications."""
    tracker = _get_response_tracker()
    tracker.scan(days=days, limit=limit, category_filter=category)


@cli.command()
@click.option("--interval", default=48, help="Hours between scans")
@click.option("--days", default=7, help="Lookback window per scan cycle")
def track(interval, days):
    """Continuously monitor Gmail for application responses."""
    tracker = _get_response_tracker()
    tracker.track(interval_hours=interval, days=days)


@cli.command()
def reset_failed():
    """Reset all failed jobs back to pending for retry."""
    async def main():
        import aiosqlite
        async with aiosqlite.connect("data/jobs.db") as db:
            # Reset failed jobs
            await db.execute(
                "UPDATE jobs SET status = 'pending', attempts = 0 WHERE status = 'failed'"
            )
            # Reset stuck in_progress
            await db.execute(
                "UPDATE jobs SET status = 'pending' WHERE status = 'in_progress'"
            )
            await db.commit()

            cursor = await db.execute(
                "SELECT status, COUNT(*) FROM jobs GROUP BY status"
            )
            rows = await cursor.fetchall()
            print("\nJob statuses after reset:")
            for status, count in rows:
                print(f"  {status}: {count}")
            print()

    asyncio.run(main())


if __name__ == "__main__":
    # Configure logging
    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{message}</cyan>",
        level="INFO",
    )
    logger.add(
        "logs/applier.log",
        rotation="10 MB",
        retention="7 days",
        level="DEBUG",
    )

    cli()
