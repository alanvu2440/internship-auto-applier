"""
iCIMS Handler

Handles job applications on iCIMS ATS.
URLs: *.icims.com/jobs/*/job
Multi-step wizard: Contact Info → Select Source → Documents → EEO → Review/Submit

Authentication: iCIMS always requires login. This handler:
1. Detects login walls
2. Tries guest apply first (rarely available)
3. Creates accounts using Gmail aliases (user+icims-{subdomain}@gmail.com)
4. Handles email verification via EmailVerifier
5. Signs in with saved credentials
6. Persists cookies per-subdomain
"""

import asyncio
import json
import re
from pathlib import Path
from typing import Dict, Any, Optional, List
from urllib.parse import urlparse
from playwright.async_api import Page
from loguru import logger

from .base import BaseHandler

# Cookie persistence for iCIMS
ICIMS_COOKIE_DIR = Path("data/icims_cookies")
ICIMS_COOKIE_DIR.mkdir(parents=True, exist_ok=True)

# Account tracker
ICIMS_ACCOUNT_TRACKER = Path("data/icims_accounts.json")

ICIMS_PASSWORD = "AutoApply2026!#Xk"


def _load_icims_accounts() -> Dict[str, Any]:
    if ICIMS_ACCOUNT_TRACKER.exists():
        try:
            return json.loads(ICIMS_ACCOUNT_TRACKER.read_text())
        except Exception:
            return {}
    return {}


def _save_icims_accounts(accounts: Dict[str, Any]) -> None:
    ICIMS_ACCOUNT_TRACKER.parent.mkdir(parents=True, exist_ok=True)
    ICIMS_ACCOUNT_TRACKER.write_text(json.dumps(accounts, indent=2))


def _get_icims_subdomain(url: str) -> str:
    """Extract subdomain from iCIMS URL.
    e.g. 'https://careers-lmi.icims.com/jobs/13506/job' -> 'careers-lmi'
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    return host.replace(".icims.com", "")


def _make_icims_alias(base_email: str, subdomain: str) -> str:
    """Create Gmail alias for iCIMS subdomain.
    e.g. alanvu2440@gmail.com + careers-lmi -> alanvu2440+icims-careers-lmi@gmail.com
    """
    local, domain = base_email.split("@", 1)
    safe = subdomain.replace(".", "-").replace("_", "-")
    return f"{local}+icims-{safe}@{domain}"


class ICIMSHandler(BaseHandler):
    """Handler for iCIMS ATS applications."""

    name = "icims"

    # Step indicators in the progress bar
    STEP_NAMES = [
        "contact information",
        "select source",
        "documents",
        "equal employment opportunity",
        "e-verify",
        "review",
        "submit",
        "voluntary self-identification",
    ]

    async def apply(self, page: Page, job_url: str, job_data: Dict[str, Any]) -> bool:
        """Apply to an iCIMS job."""
        self._last_status = "failed"
        try:
            company = job_data.get("company", "Unknown")
            role = job_data.get("role", "Unknown")
            logger.info(f"Applying to iCIMS job: {company} - {role}")

            subdomain = _get_icims_subdomain(job_url)
            logger.debug(f"iCIMS subdomain: {subdomain}")

            # Set AI context
            self.ai_answerer.set_job_context(company, role)

            # Try to load saved cookies
            await self._load_icims_cookies(page, subdomain)

            # Navigate to job URL
            try:
                await page.goto(job_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                logger.warning(f"Page load issue: {e}")
                await page.goto(job_url, wait_until="commit", timeout=15000)

            await self.browser_manager.human_delay(1500, 2500)

            # Check if job is closed
            if await self.is_job_closed(page):
                logger.info("Job is closed/unavailable")
                self._last_status = "closed"
                return False

            # Check for login wall
            if await self._is_login_required(page):
                logger.info("iCIMS login wall detected — trying guest apply first")

                # Try guest apply first (rarely works but costs nothing)
                if await self._try_guest_apply(page):
                    logger.info("Guest apply succeeded!")
                    await self.browser_manager.human_delay(2000, 3000)
                else:
                    # Guest apply failed — attempt account creation/signin
                    logger.info("Guest apply unavailable — attempting account flow")
                    auth_ok = await self._handle_icims_auth(page, job_url, subdomain)
                    if not auth_ok:
                        logger.warning("iCIMS auth failed — skipping")
                        self._last_status = "login_required"
                        return False
                    # Save cookies on successful auth
                    await self._save_icims_cookies(page, subdomain)

            # Dismiss any popups
            await self.dismiss_popups(page)

            # Click Apply button if on job description page
            if await self._is_job_description_page(page):
                if not await self._click_apply_button(page):
                    logger.warning("Could not find Apply button on iCIMS job page")
                    return False
                # Wait for navigation — iCIMS often redirects through login page
                await self.browser_manager.human_delay(3000, 5000)
                await self.wait_for_page_load(page)

                # Re-check for login wall after clicking Apply
                if await self._is_login_required(page):
                    if not await self._try_guest_apply(page):
                        self._last_status = "login_required"
                        return False
                    await self.browser_manager.human_delay(2000, 3000)
                    await self.wait_for_page_load(page)

            # Now we should be on the multi-step application form
            # Process each step
            max_steps = 8  # Safety limit
            for step_num in range(max_steps):
                current_step = await self._detect_current_step(page)
                logger.info(f"iCIMS step {step_num + 1}: {current_step}")

                if current_step == "captcha":
                    logger.warning("CAPTCHA detected on iCIMS application page")
                    if self.captcha_solver:
                        if not await self._solve_captcha(page):
                            self._last_status = "captcha"
                            return False
                    else:
                        # Wait up to 60 seconds for manual solve
                        logger.info("Waiting up to 60s for manual CAPTCHA solve...")
                        solved = False
                        for _ in range(12):
                            await asyncio.sleep(5)
                            if not await self._detect_captcha(page):
                                logger.info("CAPTCHA solved!")
                                solved = True
                                break
                        if not solved:
                            logger.warning("CAPTCHA not solved — marking as captcha blocked")
                            self._last_status = "captcha"
                            return False
                    continue  # Re-detect step after CAPTCHA solved
                elif current_step == "email_entry":
                    await self._fill_email_entry(page, job_data)
                elif current_step == "contact_info":
                    await self._fill_contact_info(page, job_data)
                elif current_step == "source":
                    await self._fill_source(page, job_data)
                elif current_step == "documents":
                    await self._fill_documents(page, job_data)
                elif current_step == "eeo":
                    await self._fill_eeo(page, job_data)
                elif current_step == "everify":
                    await self._fill_everify(page, job_data)
                elif current_step == "voluntary_self_id":
                    await self._fill_voluntary_self_id(page, job_data)
                elif current_step == "review":
                    # Final review page — submit or dry-run
                    if self.dry_run:
                        validation = await self._run_dry_run_validation(page)
                        self._last_status = "success" if validation else "failed"
                        return validation
                    # Real submit
                    if await self._click_submit(page):
                        await self.browser_manager.human_delay(2000, 3000)
                        if await self.is_application_complete(page):
                            self._last_status = "success"
                            return True
                    break
                elif current_step == "unknown":
                    # Unknown step — try generic form fill
                    logger.warning(f"Unknown iCIMS step, attempting generic fill")
                    await self._fill_generic_step(page, job_data)
                elif current_step == "complete":
                    self._last_status = "success"
                    return True

                await self.browser_manager.human_delay(500, 1000)

                # Dry run: validate on last detectable step if no explicit review page
                if self.dry_run and step_num >= 2 and not await self._has_next_button(page):
                    validation = await self._run_dry_run_validation(page)
                    self._last_status = "success" if validation else "failed"
                    return validation

                # Click Next to advance
                if not await self._click_next(page):
                    # No next button — might be the last page
                    if self.dry_run:
                        validation = await self._run_dry_run_validation(page)
                        self._last_status = "success" if validation else "failed"
                        return validation
                    # Try submit
                    if await self._click_submit(page):
                        await self.browser_manager.human_delay(2000, 3000)
                        if await self.is_application_complete(page):
                            self._last_status = "success"
                            return True
                    break

                await self.browser_manager.human_delay(1500, 2500)

                # Check for validation errors after clicking Next
                error = await self._get_validation_error(page)
                if error:
                    logger.warning(f"iCIMS validation error: {error}")
                    # Try to fix and retry
                    await self._fill_generic_step(page, job_data)
                    if not await self._click_next(page):
                        break
                    await self.browser_manager.human_delay(1500, 2500)

            # Final check
            if await self.is_application_complete(page):
                self._last_status = "success"
                return True

            error = await self.get_error_message(page)
            if error:
                logger.error(f"iCIMS application error: {error}")
                self._last_status = f"error: {error[:80]}"

            return False

        except Exception as e:
            logger.error(f"iCIMS application failed: {e}")
            await self.take_screenshot(page, f"icims_error_{job_data.get('company', 'unknown')}")
            return False

    async def detect_form_type(self, page: Page) -> str:
        """Detect iCIMS form type."""
        # Check for multi-step wizard
        progress = await page.query_selector(
            '.iCIMS_Steps, .steps, [class*="step"], [class*="progress"], '
            'ol.breadcrumb, .wizard-steps, [role="tablist"]'
        )
        if progress:
            return "multi_step"

        # Check for single-page form
        form = await page.query_selector('form')
        if form:
            return "single_page"

        return "unknown"

    # ── Login / Guest Apply Detection ─────────────────────────────────

    async def _is_login_required(self, page: Page) -> bool:
        """Check if iCIMS is showing a login wall."""
        login_indicators = [
            'input[type="password"]',
            'form[action*="login"]',
            'form[action*="signin"]',
            '[class*="login-form"]',
            '[id*="login"]',
            'button:has-text("Sign In")',
            'a:has-text("Sign In")',
        ]
        for selector in login_indicators:
            try:
                elem = await page.query_selector(selector)
                if elem and await elem.is_visible():
                    return True
            except Exception:
                continue

        # Check page text
        body = await page.text_content("body") or ""
        body_lower = body.lower()
        if any(phrase in body_lower for phrase in [
            "sign in to apply",
            "login to apply",
            "create an account",
            "already have an account",
        ]):
            return True

        return False

    async def _try_guest_apply(self, page: Page) -> bool:
        """Try to find and click a guest/quick apply option."""
        guest_patterns = [
            'a:has-text("Apply as Guest")',
            'a:has-text("Guest")',
            'a:has-text("Apply without")',
            'a:has-text("Quick Apply")',
            'a:has-text("Continue as Guest")',
            'button:has-text("Apply as Guest")',
            'button:has-text("Guest")',
            'button:has-text("Quick Apply")',
            'button:has-text("Continue as Guest")',
            'a:has-text("apply")',  # Sometimes just "Apply" link
            '[class*="guest"]',
            '[id*="guest"]',
        ]
        for pattern in guest_patterns:
            try:
                elem = await page.query_selector(pattern)
                if elem and await elem.is_visible():
                    await elem.click()
                    logger.info(f"Clicked guest apply: {pattern}")
                    await self.browser_manager.human_delay(2000, 3000)
                    return True
            except Exception:
                continue
        return False

    # ── Account Creation / Sign-In ─────────────────────────────────────

    async def _handle_icims_auth(self, page: Page, job_url: str, subdomain: str) -> bool:
        """Handle iCIMS authentication — create account or sign in."""
        accounts = _load_icims_accounts()
        config = self.form_filler.config
        base_email = config.get("personal_info", {}).get("email", "")
        alias_email = _make_icims_alias(base_email, subdomain)

        if subdomain in accounts:
            logger.info(f"Existing iCIMS account for {subdomain}, signing in as {alias_email}")
            success = await self._icims_signin(page, alias_email)
            if success:
                return True
            # Sign-in failed — try creating new account
            logger.info("Sign-in failed, trying to create new account")
            try:
                await page.goto(job_url, wait_until="domcontentloaded", timeout=15000)
                await self.browser_manager.human_delay(2000, 3000)
            except Exception:
                pass
            success = await self._icims_create_account(page, alias_email, subdomain)
            if success:
                accounts[subdomain] = {"email": alias_email, "created": True}
                _save_icims_accounts(accounts)
            return success
        else:
            logger.info(f"No iCIMS account for {subdomain}, creating with {alias_email}")
            success = await self._icims_create_account(page, alias_email, subdomain)
            if success:
                accounts[subdomain] = {"email": alias_email, "created": True}
                _save_icims_accounts(accounts)
                return True
            # Try signin as fallback
            logger.info("Account creation failed, trying signin")
            return await self._icims_signin(page, alias_email)

    async def _icims_create_account(self, page: Page, email: str, subdomain: str) -> bool:
        """Create a new iCIMS account."""
        try:
            # Look for Create Account / Register link
            create_selectors = [
                'a:has-text("Create Account")',
                'a:has-text("Register")',
                'a:has-text("Sign Up")',
                'a:has-text("New User")',
                'button:has-text("Create Account")',
                'button:has-text("Register")',
                '[class*="register"]',
                '[class*="create-account"]',
            ]

            # Search in all frames (iCIMS uses iframes)
            frames = page.frames if len(page.frames) > 1 else [page]
            clicked_create = False
            for frame in frames:
                if clicked_create:
                    break
                for sel in create_selectors:
                    try:
                        btn = await frame.query_selector(sel)
                        if btn and await btn.is_visible():
                            await btn.click()
                            clicked_create = True
                            logger.info(f"Clicked iCIMS create account: {sel}")
                            await self.browser_manager.human_delay(2000, 3000)
                            break
                    except Exception:
                        continue

            if not clicked_create:
                logger.debug("No create account button found — may already be on registration form")

            # Fill registration form — search all frames
            personal = self.form_filler.config.get("personal_info", {})
            filled_email = False

            for frame in page.frames if len(page.frames) > 1 else [page]:
                try:
                    # Email field
                    email_input = await frame.query_selector(
                        'input[type="email"], input[name*="email" i], input[id*="email" i]'
                    )
                    if email_input and await email_input.is_visible():
                        await email_input.fill(email)
                        filled_email = True
                        await self.browser_manager.human_delay(200, 400)

                    # Password fields
                    pw_fields = await frame.query_selector_all('input[type="password"]')
                    for pw in pw_fields:
                        if await pw.is_visible():
                            await pw.fill(ICIMS_PASSWORD)
                            await self.browser_manager.human_delay(200, 400)

                    # First name
                    fname = await frame.query_selector(
                        'input[name*="firstname" i], input[name*="first_name" i], '
                        'input[id*="firstname" i], input[aria-label*="First Name" i]'
                    )
                    if fname and await fname.is_visible():
                        await fname.fill(personal.get("first_name", ""))
                        await self.browser_manager.human_delay(100, 200)

                    # Last name
                    lname = await frame.query_selector(
                        'input[name*="lastname" i], input[name*="last_name" i], '
                        'input[id*="lastname" i], input[aria-label*="Last Name" i]'
                    )
                    if lname and await lname.is_visible():
                        await lname.fill(personal.get("last_name", ""))
                        await self.browser_manager.human_delay(100, 200)

                    if filled_email:
                        break
                except Exception as e:
                    logger.debug(f"iCIMS create account frame error: {e}")
                    continue

            if not filled_email:
                logger.warning("Could not find email field for iCIMS registration")
                return False

            # Check agreement checkboxes
            for frame in page.frames if len(page.frames) > 1 else [page]:
                try:
                    checkboxes = await frame.query_selector_all('input[type="checkbox"]')
                    for cb in checkboxes:
                        if await cb.is_visible() and not await cb.is_checked():
                            label = await cb.evaluate(
                                'el => el.closest("label")?.textContent || el.getAttribute("aria-label") || ""'
                            )
                            if any(w in (label or "").lower() for w in ["agree", "terms", "privacy", "consent"]):
                                await cb.check()
                except Exception:
                    continue

            # Click submit/register button
            submit_selectors = [
                'button:has-text("Create Account")',
                'button:has-text("Register")',
                'button:has-text("Sign Up")',
                'input[type="submit"][value*="Create" i]',
                'input[type="submit"][value*="Register" i]',
                'button[type="submit"]',
            ]
            clicked = False
            for frame in page.frames if len(page.frames) > 1 else [page]:
                if clicked:
                    break
                for sel in submit_selectors:
                    try:
                        btn = await frame.query_selector(sel)
                        if btn and await btn.is_visible():
                            await btn.click()
                            clicked = True
                            logger.info("Clicked iCIMS register submit")
                            break
                    except Exception:
                        continue

            if not clicked:
                logger.warning("Could not find iCIMS register submit button")
                return False

            await self.browser_manager.human_delay(3000, 5000)

            # Check for "already exists" error
            for frame in page.frames if len(page.frames) > 1 else [page]:
                try:
                    body = (await frame.evaluate('() => document.body?.innerText || ""')).lower()
                    if "already" in body and ("exists" in body or "registered" in body or "in use" in body):
                        logger.info("iCIMS account already exists")
                        return False  # Caller tries signin
                except Exception:
                    continue

            # Check if email verification needed
            needs_verify = False
            for frame in page.frames if len(page.frames) > 1 else [page]:
                try:
                    body = (await frame.evaluate('() => document.body?.innerText || ""')).lower()
                    if any(x in body for x in ["verify your email", "verification", "check your email", "sent a code"]):
                        needs_verify = True
                        break
                except Exception:
                    continue

            if needs_verify:
                logger.info("iCIMS email verification required")
                verified = await self._icims_verify_email(page, email)
                if not verified:
                    return False

            # Check if we're past the login wall
            await self.browser_manager.human_delay(2000, 3000)
            if not await self._is_login_required(page):
                logger.info(f"iCIMS account created for {subdomain}")
                return True

            return False

        except Exception as e:
            logger.error(f"iCIMS account creation failed: {e}")
            return False

    async def _icims_signin(self, page: Page, email: str) -> bool:
        """Sign into an existing iCIMS account."""
        try:
            # Look for Sign In link
            signin_selectors = [
                'a:has-text("Sign In")',
                'a:has-text("Log In")',
                'button:has-text("Sign In")',
                'button:has-text("Log In")',
                'a:has-text("Already have an account")',
                '[class*="signin"]',
                '[class*="login"]',
            ]
            frames = page.frames if len(page.frames) > 1 else [page]
            for frame in frames:
                for sel in signin_selectors:
                    try:
                        btn = await frame.query_selector(sel)
                        if btn and await btn.is_visible():
                            await btn.click()
                            logger.info(f"Clicked iCIMS sign in: {sel}")
                            await self.browser_manager.human_delay(2000, 3000)
                            break
                    except Exception:
                        continue

            # Fill email
            filled = False
            for frame in page.frames if len(page.frames) > 1 else [page]:
                try:
                    email_input = await frame.query_selector(
                        'input[type="email"], input[name*="email" i], input[id*="email" i]'
                    )
                    if email_input and await email_input.is_visible():
                        await email_input.fill(email)
                        filled = True
                        await self.browser_manager.human_delay(200, 400)

                    # Password
                    pw = await frame.query_selector('input[type="password"]')
                    if pw and await pw.is_visible():
                        await pw.fill(ICIMS_PASSWORD)
                        await self.browser_manager.human_delay(200, 400)

                    if filled:
                        break
                except Exception:
                    continue

            if not filled:
                logger.warning("Could not find iCIMS sign-in email field")
                return False

            # Click Sign In submit
            for frame in page.frames if len(page.frames) > 1 else [page]:
                for sel in ['button:has-text("Sign In")', 'button:has-text("Log In")',
                            'input[type="submit"]', 'button[type="submit"]']:
                    try:
                        btn = await frame.query_selector(sel)
                        if btn and await btn.is_visible():
                            await btn.click()
                            logger.info("Clicked iCIMS sign in submit")
                            break
                    except Exception:
                        continue

            await self.browser_manager.human_delay(3000, 5000)

            # Check if sign-in succeeded (no longer on login page)
            if not await self._is_login_required(page):
                logger.info("iCIMS sign-in successful")
                return True

            # Check for error
            for frame in page.frames if len(page.frames) > 1 else [page]:
                try:
                    body = (await frame.evaluate('() => document.body?.innerText || ""')).lower()
                    if "invalid" in body or "incorrect" in body:
                        logger.warning("iCIMS sign-in failed: invalid credentials")
                except Exception:
                    continue

            return False

        except Exception as e:
            logger.error(f"iCIMS signin failed: {e}")
            return False

    async def _icims_verify_email(self, page: Page, email: str) -> bool:
        """Verify email using EmailVerifier."""
        try:
            if not self.email_verifier:
                logger.warning("No email_verifier configured")
                return False

            loop = asyncio.get_event_loop()
            code = await loop.run_in_executor(
                None,
                lambda: self.email_verifier.get_verification_code(
                    sender_filter="icims",
                    max_age_seconds=300,
                    timeout=90,
                    poll_interval=5,
                )
            )

            if not code:
                logger.warning("No iCIMS verification code found")
                return False

            logger.info(f"Found iCIMS verification code: {code}")

            # Enter code
            for frame in page.frames if len(page.frames) > 1 else [page]:
                try:
                    code_input = await frame.query_selector(
                        'input[type="text"], input[name*="code" i], input[id*="code" i], '
                        'input[type="number"], input[placeholder*="code" i]'
                    )
                    if code_input and await code_input.is_visible():
                        await code_input.fill(code)
                        await self.browser_manager.human_delay(500, 1000)

                        # Click verify
                        verify_btn = await frame.query_selector(
                            'button:has-text("Verify"), button:has-text("Submit"), '
                            'button[type="submit"]'
                        )
                        if verify_btn and await verify_btn.is_visible():
                            await verify_btn.click()
                            await self.browser_manager.human_delay(3000, 5000)
                            return True
                except Exception:
                    continue

            return False
        except Exception as e:
            logger.error(f"iCIMS email verification failed: {e}")
            return False

    # ── Cookie Persistence ─────────────────────────────────────────────

    async def _save_icims_cookies(self, page: Page, subdomain: str) -> None:
        try:
            cookies = await page.context.cookies()
            cookie_file = ICIMS_COOKIE_DIR / f"{subdomain}.json"
            cookie_file.write_text(json.dumps(cookies, indent=2))
            logger.debug(f"Saved {len(cookies)} iCIMS cookies for {subdomain}")
        except Exception as e:
            logger.debug(f"Could not save iCIMS cookies: {e}")

    async def _load_icims_cookies(self, page: Page, subdomain: str) -> bool:
        try:
            cookie_file = ICIMS_COOKIE_DIR / f"{subdomain}.json"
            if not cookie_file.exists():
                return False
            cookies = json.loads(cookie_file.read_text())
            if cookies:
                await page.context.add_cookies(cookies)
                logger.debug(f"Loaded {len(cookies)} iCIMS cookies for {subdomain}")
                return True
        except Exception as e:
            logger.debug(f"Could not load iCIMS cookies: {e}")
        return False

    async def _detect_captcha(self, page: Page) -> bool:
        """Detect if an active CAPTCHA challenge is visible on the page."""
        for frame in page.frames:
            try:
                # Check for visible hCaptcha/reCAPTCHA challenge dialog (not just the badge)
                captcha_challenge = await frame.query_selector(
                    'iframe[src*="hcaptcha.com/challenge"], '
                    'iframe[src*="recaptcha"][title*="challenge"], '
                    'div[class*="challenge-container"]'
                )
                if captcha_challenge:
                    try:
                        visible = await captcha_challenge.is_visible()
                        if visible:
                            return True
                    except Exception:
                        return True

                # Check for visible challenge text in frame content
                body_text = (await frame.evaluate('() => document.body ? document.body.innerText.substring(0, 1000) : ""')) or ""
                if any(phrase in body_text.lower() for phrase in ["find objects", "select all images", "click each image", "verify you are human"]):
                    return True
            except Exception:
                continue
        return False

    async def _solve_captcha(self, page: Page) -> bool:
        """Attempt to solve CAPTCHA using configured solver."""
        if not self.captcha_solver:
            return False
        try:
            return await self.captcha_solver.solve_and_inject(page)
        except Exception as e:
            logger.warning(f"CAPTCHA solver failed: {e}")
            return False

    # ── Job Description Page Detection ────────────────────────────────

    async def _is_job_description_page(self, page: Page) -> bool:
        """Check if we're on a job description page (not the application form)."""
        url = page.url.lower()
        logger.info(f"iCIMS JD page check — URL: {url}")

        # iCIMS often embeds content in iframes — check all frames
        all_frames = page.frames
        logger.info(f"iCIMS JD page check — {len(all_frames)} frames found: {[f.url[:80] for f in all_frames]}")

        # URL-based check FIRST: iCIMS JD pages end with /job or /jobs/NNN/job
        url_looks_like_jd = url.rstrip("/").endswith("/job") or bool(re.search(r'/jobs/\d+/job', url))
        # Also check frame URLs
        for frame in all_frames:
            frame_url = frame.url.lower()
            if frame_url.rstrip("/").endswith("/job") or re.search(r'/jobs/\d+/job', frame_url):
                url_looks_like_jd = True
                break
        logger.info(f"iCIMS JD page check — URL pattern match: {url_looks_like_jd}")

        # Search ALL frames for Apply button
        frames_to_search = all_frames if len(all_frames) > 1 else [page]

        for frame in frames_to_search:
            try:
                apply_info = await frame.evaluate('''() => {
                    const els = document.querySelectorAll('a, button, input[type="submit"]');
                    const results = [];
                    for (const el of els) {
                        const text = (el.textContent || el.value || "").trim().toLowerCase();
                        if (text.includes("apply")) {
                            results.push({text: text.substring(0, 80), tag: el.tagName, visible: el.offsetParent !== null || el.offsetWidth > 0});
                        }
                    }
                    return results;
                }''')
                logger.info(f"iCIMS JD page check — frame {frame.url[:60]} — elements with 'apply': {apply_info}")

                for item in (apply_info or []):
                    text = item.get("text", "")
                    if any(phrase in text for phrase in ["apply for this job", "apply online", "apply now"]):
                        logger.info(f"iCIMS: Found Apply element via JS: '{text}' — this is a JD page")
                        return True
            except Exception as e:
                logger.warning(f"iCIMS JD page check — frame evaluate failed: {e}")

        # If URL looks like JD, check body content in ALL frames for job description indicators
        if url_looks_like_jd:
            for frame in frames_to_search:
                try:
                    body_text = (await frame.evaluate('() => document.body ? document.body.innerText.toLowerCase().substring(0, 5000) : ""')) or ""
                    jd_indicators = ["overview", "responsibilities", "qualifications",
                                     "job description", "about the role", "what you'll do",
                                     "requirements", "about this", "position summary"]
                    found = [ind for ind in jd_indicators if ind in body_text]
                    if found:
                        logger.info(f"iCIMS JD page check — frame {frame.url[:60]} — body indicators: {found}")
                        return True
                except Exception as e:
                    logger.warning(f"iCIMS JD page check — body text check failed: {e}")

        logger.info("iCIMS JD page check — NOT a JD page")
        return False

    async def _click_apply_button(self, page: Page) -> bool:
        """Click the Apply button on a job description page."""
        apply_patterns = [
            'a:has-text("Apply for this job online")',
            'a:has-text("Apply for this job")',
            'a.iCIMS_PrimaryButton:has-text("Apply")',
            'button.iCIMS_PrimaryButton:has-text("Apply")',
            'a:has-text("Apply Now")',
            'a:has-text("Apply Online")',
            'a:has-text("Apply for this")',
            'button:has-text("Apply Now")',
            'button:has-text("Apply")',
            '.iCIMS_PrimaryButton',
            '[class*="apply-button"]',
            'a[href*="login"]',  # iCIMS sometimes goes through login page first
        ]
        # Search all frames (iCIMS often uses iframes)
        frames_to_search = page.frames if len(page.frames) > 1 else [page]
        for frame in frames_to_search:
            for pattern in apply_patterns:
                try:
                    elem = await frame.query_selector(pattern)
                    if elem and await elem.is_visible():
                        await elem.click()
                        logger.info(f"Clicked iCIMS Apply button: {pattern} (frame: {frame.url[:60]})")
                        await self.browser_manager.human_delay(2000, 3000)
                        return True
                except Exception:
                    continue
        return False

    # ── Step Detection ────────────────────────────────────────────────

    async def _detect_current_step(self, page: Page) -> str:
        """Detect which step of the application we're on."""
        # Check page heading/title
        # Search all frames for headings (iCIMS uses iframes)
        heading = ""
        frames_to_check = page.frames if len(page.frames) > 1 else [page]
        for frame in frames_to_check:
            if heading:
                break
            for selector in ["h1", "h2", ".iCIMS_Header", "[class*='header'] h1", ".page-title"]:
                try:
                    elem = await frame.query_selector(selector)
                    if elem:
                        text = (await elem.text_content() or "").strip().lower()
                        if text and len(text) > 3:  # Skip tiny fragments
                            heading = text
                            break
                except Exception:
                    continue

        # Check for active step in progress bar (all frames)
        active_step = ""
        for frame in frames_to_check:
            if active_step:
                break
            for selector in [".iCIMS_Steps .active", ".active-step", "[aria-current='step']",
                             "li.active", ".current-step", ".step.active"]:
                try:
                    elem = await frame.query_selector(selector)
                    if elem:
                        text = (await elem.text_content() or "").strip().lower()
                        if text:
                            active_step = text
                            break
                except Exception:
                    continue

        combined = f"{heading} {active_step}".lower()

        # Check URL for step hints
        url = page.url.lower()

        logger.info(f"iCIMS step detection — heading: '{heading}', active_step: '{active_step}', url: {url}")

        # Check for CAPTCHA
        if await self._detect_captcha(page):
            return "captcha"

        # Special check: iCIMS email-first login/entry page ("Enter Your Information" with just email + Next)
        if "login" in url or "enter your information" in combined:
            # Check if it's the simple email entry page (not a full contact form)
            email_field = None
            for frame in page.frames:
                email_field = await frame.query_selector('input[type="email"], input[name*="email" i], input[id*="email" i]')
                if email_field:
                    break
            if not email_field:
                email_field = await page.query_selector('input[type="email"], input[name*="email"], input[id*="email"]')
            has_name_field = False
            for frame in page.frames:
                nf = await frame.query_selector('input[name*="firstname"], input[name*="first_name"], input[id*="firstname"]')
                if nf:
                    has_name_field = True
                    break
            if email_field and not has_name_field:
                return "email_entry"

        # Match step
        if any(x in combined for x in ["contact", "personal info", "your info"]) or "contact" in url:
            return "contact_info"
        elif any(x in combined for x in ["source", "how did you", "hear about", "referral"]) or "source" in url:
            return "source"
        elif any(x in combined for x in ["document", "resume", "upload", "attachment", "cv"]) or "document" in url:
            return "documents"
        elif any(x in combined for x in ["equal employment", "eeo", "demographic"]) or "eeo" in url:
            return "eeo"
        elif any(x in combined for x in ["e-verify", "everify", "employment eligibility"]) or "verify" in url:
            return "everify"
        elif any(x in combined for x in ["voluntary", "self-identification", "self identification", "disability", "veteran"]):
            return "voluntary_self_id"
        elif any(x in combined for x in ["review", "summary", "confirm"]) or "review" in url:
            return "review"
        elif any(x in combined for x in ["thank", "success", "submitted", "received"]):
            return "complete"

        # Check body text as fallback — but only if we see actual form fields
        has_form_fields = await page.query_selector(
            'input[type="text"], input[type="email"], input[type="tel"], select, textarea, input[type="file"]'
        )
        if has_form_fields:
            body = (await page.text_content("body") or "").lower()[:3000]
            if "contact information" in body and ("first name" in body or "email address" in body):
                return "contact_info"
            elif "how did you hear" in body or "select source" in body:
                return "source"
            elif ("resume" in body or "document" in body) and ("upload" in body or "attach" in body):
                return "documents"
            elif "equal employment" in body and ("gender" in body or "race" in body or "ethnicity" in body):
                return "eeo"

        # Check for success/thank you page (no form fields needed)
        body = (await page.text_content("body") or "").lower()[:2000]
        if "thank you" in body and ("application" in body or "submitted" in body):
            return "complete"

        return "unknown"

    # ── Step Fillers ──────────────────────────────────────────────────

    async def _fill_email_entry(self, page: Page, job_data: Dict[str, Any]) -> None:
        """Fill the iCIMS email entry page and click Next."""
        logger.info("Filling iCIMS email entry page")
        personal = self.form_filler.config.get("personal_info", {})
        email = personal.get("email", "")

        # Find email field in any frame
        filled = False
        for frame in page.frames:
            try:
                email_field = await frame.query_selector(
                    'input[type="email"], input[name*="email"], input[id*="email"], '
                    'input[type="text"]'  # Sometimes it's just a text field
                )
                if email_field and await email_field.is_visible():
                    await email_field.click()
                    await email_field.fill("")
                    await email_field.type(email, delay=50)
                    logger.info(f"Entered email: {email}")
                    filled = True

                    # Click Next button
                    next_btn = await frame.query_selector(
                        'button:has-text("Next"), input[type="submit"], '
                        'button[type="submit"], a:has-text("Next")'
                    )
                    if next_btn and await next_btn.is_visible():
                        await next_btn.click()
                        logger.info("Clicked Next on email entry page")
                        await self.browser_manager.human_delay(3000, 5000)

                        # Check for CAPTCHA after clicking Next
                        captcha_detected = await self._detect_captcha(page)
                        if captcha_detected:
                            logger.warning("CAPTCHA detected after email entry — waiting for manual solve or solver")
                            # Try CAPTCHA solver if available
                            if self.captcha_solver:
                                solved = await self._solve_captcha(page)
                                if not solved:
                                    self._last_status = "captcha"
                                    return
                            else:
                                # Wait up to 60 seconds for manual CAPTCHA solve
                                logger.info("Waiting up to 60s for manual CAPTCHA solve...")
                                for _ in range(12):
                                    await asyncio.sleep(5)
                                    if not await self._detect_captcha(page):
                                        logger.info("CAPTCHA solved!")
                                        break
                                else:
                                    logger.warning("CAPTCHA not solved within 60s")
                                    self._last_status = "captcha"
                                    return

                        await self.wait_for_page_load(page)
                    break
            except Exception as e:
                logger.debug(f"Email entry frame error: {e}")
                continue

        if not filled:
            logger.warning("Could not find email field on entry page")

    async def _fill_contact_info(self, page: Page, job_data: Dict[str, Any]) -> None:
        """Fill Step 1: Contact Information."""
        logger.info("Filling iCIMS Contact Information step")

        # Wait for form fields to be visible
        try:
            await page.wait_for_selector(
                'input[type="text"], input[type="email"], input[type="tel"]',
                timeout=10000
            )
        except Exception:
            logger.warning("Timed out waiting for contact info form fields")

        config = self.form_filler.config
        personal = config.get("personal_info", {})

        # Map of field patterns to config values
        field_map = {
            # First Name
            ('input[name*="firstname" i]', 'input[name*="first_name" i]',
             'input[id*="firstname" i]', 'input[aria-label*="First Name" i]'): personal.get("first_name", ""),
            # Preferred First Name
            ('input[name*="preferred" i]', 'input[id*="preferred" i]',
             'input[aria-label*="Preferred" i]'): personal.get("first_name", ""),
            # Middle Name — leave blank
            # Last Name
            ('input[name*="lastname" i]', 'input[name*="last_name" i]',
             'input[id*="lastname" i]', 'input[aria-label*="Last Name" i]'): personal.get("last_name", ""),
            # Email
            ('input[name*="email" i]', 'input[type="email"]',
             'input[id*="email" i]'): personal.get("email", ""),
            # Phone
            ('input[name*="phone" i]', 'input[type="tel"]',
             'input[id*="phone" i]'): personal.get("phone", ""),
            # Address
            ('input[name*="address1" i]', 'input[name*="address" i]:not([name*="address2" i])',
             'input[id*="address1" i]', 'input[aria-label*="Address 1" i]'): personal.get("address", ""),
            # City
            ('input[name*="city" i]', 'input[id*="city" i]',
             'input[aria-label*="City" i]'): personal.get("city", ""),
            # ZIP Code
            ('input[name*="zip" i]', 'input[name*="postal" i]',
             'input[id*="zip" i]', 'input[aria-label*="ZIP" i]'): personal.get("zip_code", ""),
        }

        for selectors, value in field_map.items():
            if not value:
                continue
            for selector in selectors:
                try:
                    elem = await page.query_selector(selector)
                    if elem and await elem.is_visible():
                        current = await elem.input_value() or ""
                        if not current.strip():
                            await elem.click()
                            await self.browser_manager.human_delay(100, 200)
                            await elem.fill("")
                            await self.browser_manager.human_type(elem, str(value))
                            self._fields_filled[selector.split("[")[1].split("]")[0] if "[" in selector else selector] = value
                            logger.debug(f"Filled iCIMS field {selector}: {str(value)[:30]}")
                        break
                except Exception as e:
                    logger.debug(f"Could not fill {selector}: {e}")
                    continue

        # Handle State dropdown
        await self._select_dropdown(page, "state", personal.get("state", "CA"))

        # Handle Country dropdown
        await self._select_dropdown(page, "country", "United States")

        # Handle any remaining unfilled visible inputs with form_filler
        await self._fill_remaining_fields(page, job_data)

    async def _fill_source(self, page: Page, job_data: Dict[str, Any]) -> None:
        """Fill Step 2: Select Source (How did you hear about us?)."""
        logger.info("Filling iCIMS Source step")

        # Try dropdown first
        source_value = self.form_filler.config.get("common_answers", {}).get("how_did_you_hear", "Online Job Board")

        # Common source dropdown patterns
        for selector in ['select[name*="source" i]', 'select[id*="source" i]',
                         'select[name*="hear" i]', 'select[aria-label*="source" i]',
                         'select']:
            try:
                elem = await page.query_selector(selector)
                if elem and await elem.is_visible():
                    options = await elem.evaluate('el => Array.from(el.options).map(o => ({value: o.value, text: o.textContent.trim()}))')
                    # Try to match source value
                    matched = self._match_dropdown_option(options, source_value)
                    if matched:
                        await elem.select_option(value=matched)
                        logger.info(f"Selected source: {matched}")
                        self._fields_filled["source"] = matched
                        break
            except Exception as e:
                logger.debug(f"Source dropdown {selector} failed: {e}")
                continue

        # Handle any text fields on this page (e.g., "If other, please specify")
        await self._fill_remaining_fields(page, job_data)

    async def _fill_documents(self, page: Page, job_data: Dict[str, Any]) -> None:
        """Fill Step 3: Documents (Resume upload)."""
        logger.info("Filling iCIMS Documents step")

        resume_path = self.form_filler.config.get("files", {}).get("resume", "")
        if not resume_path:
            logger.warning("No resume path configured")
            return

        # Find file input
        file_inputs = await page.query_selector_all('input[type="file"]')
        for file_input in file_inputs:
            try:
                await file_input.set_input_files(resume_path)
                logger.info(f"Uploaded resume: {resume_path}")
                self._fields_filled["resume"] = resume_path
                await self.browser_manager.human_delay(2000, 3000)
                break
            except Exception as e:
                logger.debug(f"File upload failed: {e}")

        # If no file input found, try clicking upload button
        if not file_inputs:
            upload_patterns = [
                'button:has-text("Upload")',
                'a:has-text("Upload")',
                'button:has-text("Browse")',
                'label:has-text("Upload")',
                '[class*="upload"]',
                '[class*="dropzone"]',
            ]
            for pattern in upload_patterns:
                try:
                    elem = await page.query_selector(pattern)
                    if elem and await elem.is_visible():
                        # Trigger file chooser
                        async with page.expect_file_chooser(timeout=5000) as fc_info:
                            await elem.click()
                        file_chooser = await fc_info.value
                        await file_chooser.set_files(resume_path)
                        logger.info(f"Uploaded resume via file chooser: {resume_path}")
                        self._fields_filled["resume"] = resume_path
                        await self.browser_manager.human_delay(2000, 3000)
                        break
                except Exception as e:
                    logger.debug(f"Upload button {pattern} failed: {e}")

        # Handle any additional document fields
        await self._fill_remaining_fields(page, job_data)

    async def _fill_eeo(self, page: Page, job_data: Dict[str, Any]) -> None:
        """Fill Step 4: Equal Employment Opportunity."""
        logger.info("Filling iCIMS EEO step")
        config = self.form_filler.config
        demographics = config.get("demographics", {})

        # Gender dropdown
        gender = demographics.get("gender", "")
        if gender:
            await self._select_dropdown(page, "gender", gender)

        # Race/Ethnicity dropdown
        race = demographics.get("race", "")
        if race:
            await self._select_dropdown(page, "race", race)
            await self._select_dropdown(page, "ethnicity", race)

        # Veteran status
        veteran = demographics.get("veteran_status", "")
        if veteran:
            await self._select_dropdown(page, "veteran", veteran)

        # Disability
        disability = demographics.get("disability_status", "")
        if disability:
            await self._select_dropdown(page, "disability", disability)

        # Handle remaining fields (iCIMS EEO varies per company)
        await self._fill_remaining_fields(page, job_data)

    async def _fill_everify(self, page: Page, job_data: Dict[str, Any]) -> None:
        """Fill E-Verify step."""
        logger.info("Filling iCIMS E-Verify step")
        # Most E-Verify steps just need acknowledgment checkboxes
        checkboxes = await page.query_selector_all('input[type="checkbox"]')
        for cb in checkboxes:
            try:
                if not await cb.is_checked():
                    await cb.check()
                    await self.browser_manager.human_delay(200, 400)
            except Exception:
                continue

        await self._fill_remaining_fields(page, job_data)

    async def _fill_voluntary_self_id(self, page: Page, job_data: Dict[str, Any]) -> None:
        """Fill Voluntary Self-Identification step (disability, veteran)."""
        logger.info("Filling iCIMS Voluntary Self-Identification step")
        config = self.form_filler.config
        demographics = config.get("demographics", {})

        # Disability - usually radio or dropdown with "I do not wish to answer"
        disability = demographics.get("disability_status", "I don't wish to answer")
        await self._select_dropdown(page, "disability", disability)

        # Veteran - usually radio or dropdown
        veteran = demographics.get("veteran_status", "I am not a veteran")
        await self._select_dropdown(page, "veteran", veteran)

        # Handle remaining (some have additional questions)
        await self._fill_remaining_fields(page, job_data)

    async def _fill_generic_step(self, page: Page, job_data: Dict[str, Any]) -> None:
        """Fill any unrecognized step using generic form filling."""
        logger.info("Filling iCIMS step with generic form filler")

        # Use the form filler for all visible fields
        await self._fill_remaining_fields(page, job_data)

        # Check for checkboxes that should be checked (acknowledgments, agreements)
        checkboxes = await page.query_selector_all('input[type="checkbox"]')
        for cb in checkboxes:
            try:
                if not await cb.is_checked():
                    label = await cb.evaluate('el => el.closest("label")?.textContent || el.getAttribute("aria-label") || ""')
                    label_lower = (label or "").lower()
                    # Auto-check agreements, acknowledgments, consent
                    if any(word in label_lower for word in ["agree", "acknowledge", "consent", "confirm", "accept", "certif"]):
                        await cb.check()
                        self._fields_filled[f"checkbox_{label[:30]}"] = "checked"
            except Exception:
                continue

    # ── Navigation ────────────────────────────────────────────────────

    async def _click_next(self, page: Page) -> bool:
        """Click Next button to advance to next step."""
        next_patterns = [
            'button:has-text("Next")',
            'input[type="submit"][value*="Next" i]',
            'a:has-text("Next")',
            'button:has-text("Continue")',
            'button:has-text("Save and Continue")',
            'input[type="submit"][value*="Continue" i]',
            '.iCIMS_PrimaryButton:has-text("Next")',
            'button[type="submit"]',
        ]
        for pattern in next_patterns:
            try:
                elem = await page.query_selector(pattern)
                if elem and await elem.is_visible():
                    await elem.click()
                    logger.debug(f"Clicked Next: {pattern}")
                    await self.browser_manager.human_delay(1000, 2000)
                    return True
            except Exception:
                continue
        return False

    async def _has_next_button(self, page: Page) -> bool:
        """Check if a Next button exists."""
        for pattern in ['button:has-text("Next")', 'input[value*="Next" i]',
                        'button:has-text("Continue")', 'input[value*="Continue" i]']:
            try:
                elem = await page.query_selector(pattern)
                if elem and await elem.is_visible():
                    return True
            except Exception:
                continue
        return False

    async def _click_submit(self, page: Page) -> bool:
        """Click Submit button on the final step."""
        submit_patterns = [
            'button:has-text("Submit")',
            'input[type="submit"][value*="Submit" i]',
            'a:has-text("Submit")',
            'button:has-text("Submit Application")',
            'button:has-text("Apply")',
            '.iCIMS_PrimaryButton:has-text("Submit")',
        ]
        for pattern in submit_patterns:
            try:
                elem = await page.query_selector(pattern)
                if elem and await elem.is_visible():
                    if self.dry_run:
                        logger.info("DRY RUN: Would click Submit")
                        return True
                    await elem.click()
                    logger.info(f"Clicked Submit: {pattern}")
                    return True
            except Exception:
                continue
        return False

    # ── Helper Methods ────────────────────────────────────────────────

    async def _select_dropdown(self, page: Page, field_hint: str, value: str) -> bool:
        """Try to select a value in a dropdown matching the field hint."""
        # Find select elements matching the hint
        selects = await page.query_selector_all("select")
        for select in selects:
            try:
                name = await select.get_attribute("name") or ""
                id_attr = await select.get_attribute("id") or ""
                label_text = await select.evaluate('''el => {
                    const label = document.querySelector(`label[for="${el.id}"]`);
                    return label ? label.textContent.trim() : "";
                }''')
                combined = f"{name} {id_attr} {label_text}".lower()

                if field_hint.lower() in combined:
                    options = await select.evaluate(
                        'el => Array.from(el.options).map(o => ({value: o.value, text: o.textContent.trim()}))'
                    )
                    matched = self._match_dropdown_option(options, value)
                    if matched:
                        await select.select_option(value=matched)
                        self._fields_filled[field_hint] = matched
                        logger.debug(f"Selected {field_hint}: {matched}")
                        return True
            except Exception:
                continue
        return False

    def _match_dropdown_option(self, options: List[Dict], target: str) -> Optional[str]:
        """Match a target value to dropdown options."""
        target_lower = target.lower().strip()

        # Exact match first
        for opt in options:
            if opt["text"].lower().strip() == target_lower:
                return opt["value"]

        # Contains match
        for opt in options:
            text = opt["text"].lower().strip()
            if target_lower in text or text in target_lower:
                return opt["value"]

        # Partial word match
        target_words = set(target_lower.split())
        best_match = None
        best_score = 0
        for opt in options:
            if not opt["value"] or opt["text"].strip() in ("", "---", "-- Please Specify --", "Select"):
                continue
            text_words = set(opt["text"].lower().split())
            overlap = len(target_words & text_words)
            if overlap > best_score:
                best_score = overlap
                best_match = opt["value"]

        return best_match

    async def _fill_remaining_fields(self, page: Page, job_data: Dict[str, Any]) -> None:
        """Fill any remaining visible form fields using the form filler and AI answerer."""
        # Get all visible inputs that are empty
        inputs = await page.query_selector_all(
            'input[type="text"]:visible, input[type="email"]:visible, '
            'input[type="tel"]:visible, input[type="url"]:visible, '
            'textarea:visible, select:visible'
        )

        for inp in inputs:
            try:
                tag = await inp.evaluate('el => el.tagName.toLowerCase()')
                input_type = await inp.get_attribute("type") or "text"
                name = await inp.get_attribute("name") or ""
                id_attr = await inp.get_attribute("id") or ""

                # Get current value
                if tag == "select":
                    current = await inp.evaluate('el => el.options[el.selectedIndex]?.text || ""')
                    if current and current.strip() not in ("", "---", "-- Please Specify --", "Select"):
                        continue  # Already has a selection
                else:
                    current = await inp.input_value() or ""
                    if current.strip():
                        continue  # Already filled

                # Get label
                label = await inp.evaluate('''el => {
                    if (el.id) {
                        const label = document.querySelector(`label[for="${el.id}"]`);
                        if (label) return label.textContent.trim();
                    }
                    const parent = el.closest("label, .form-group, .field-wrapper, tr, td");
                    if (parent) {
                        const label = parent.querySelector("label, .label, th");
                        if (label) return label.textContent.trim();
                    }
                    return el.getAttribute("aria-label") || el.getAttribute("placeholder") || el.name || "";
                }''')

                if not label:
                    continue

                # Use AI answerer for the question
                field_type = "select" if tag == "select" else ("textarea" if tag == "textarea" else "text")

                options = []
                if tag == "select":
                    options = await inp.evaluate(
                        'el => Array.from(el.options).filter(o => o.value).map(o => o.textContent.trim())'
                    )

                answer = await self.ai_answerer.answer_question(
                    label, field_type=field_type, options=options if options else None
                )

                if answer:
                    if tag == "select":
                        matched = self._match_dropdown_option(
                            await inp.evaluate('el => Array.from(el.options).map(o => ({value: o.value, text: o.textContent.trim()}))'),
                            answer
                        )
                        if matched:
                            await inp.select_option(value=matched)
                            self._fields_filled[label[:40]] = answer
                    else:
                        await inp.click()
                        await self.browser_manager.human_delay(100, 200)
                        await inp.fill("")
                        await self.browser_manager.human_type(inp, str(answer))
                        self._fields_filled[label[:40]] = str(answer)[:50]

            except Exception as e:
                logger.debug(f"Could not fill remaining field: {e}")
                continue

    async def _get_validation_error(self, page: Page) -> Optional[str]:
        """Check for validation error messages on the page."""
        error_selectors = [
            '.iCIMS_Error', '.error-message', '.validation-error',
            '[class*="error"]', '.alert-danger', '.form-error',
            '[role="alert"]',
        ]
        for selector in error_selectors:
            try:
                elem = await page.query_selector(selector)
                if elem and await elem.is_visible():
                    text = (await elem.text_content() or "").strip()
                    if text and len(text) > 5:
                        return text
            except Exception:
                continue
        return None

    async def _run_dry_run_validation(self, page: Page) -> bool:
        """Validate form fill in dry-run mode."""
        logger.info("DRY RUN: Running iCIMS validation checks...")
        await self.take_screenshot(page, "icims_dry_run")

        config = self.form_filler.config
        personal = config.get("personal_info", {})

        # Check core fields
        filled = {}
        core_checks = {
            "first_name": ('input[name*="firstname" i], input[name*="first_name" i], input[id*="firstname" i]',
                           personal.get("first_name", "")),
            "last_name": ('input[name*="lastname" i], input[name*="last_name" i], input[id*="lastname" i]',
                          personal.get("last_name", "")),
            "email": ('input[name*="email" i], input[type="email"]', personal.get("email", "")),
            "phone": ('input[name*="phone" i], input[type="tel"]', personal.get("phone", "")),
        }

        for field_name, (selectors, expected) in core_checks.items():
            for selector in selectors.split(", "):
                try:
                    elem = await page.query_selector(selector)
                    if elem:
                        val = await elem.input_value() or ""
                        if val.strip():
                            filled[field_name] = val.strip()
                            break
                except Exception:
                    continue

        logger.info(f"DRY RUN: Fields filled: {filled}")

        # Check resume
        body_text = (await page.text_content("body") or "").lower()
        resume_uploaded = any(ext in body_text for ext in [".pdf", ".docx", ".doc"])

        core_filled = sum(1 for f in ["first_name", "last_name", "email"] if f in filled)
        core_total = 3

        logger.info(f"""
  VALIDATION SUMMARY:
    Core fields: {core_filled}/{core_total}
    Resume: {"uploaded" if resume_uploaded else "NOT checked (may be on later step)"}
    Fields tracked: {len(self._fields_filled)}
    Fields missed: {len(self._fields_missed)}
    RESULT: {"PASS" if core_filled >= 2 else "FAIL"}""")

        return core_filled >= 2
