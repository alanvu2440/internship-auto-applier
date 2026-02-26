"""
Workday Handler

Handles job applications on Workday ATS.
URLs: *.myworkdayjobs.com

Workday career sites require account creation per-tenant. This handler:
1. Detects login walls
2. Creates accounts using Gmail aliases (user+tenant@gmail.com)
3. Handles email verification via EmailVerifier (Gmail IMAP)
4. Persists sessions via cookies per-tenant
5. Fills multi-page application wizards with typeahead support

Selectors reference (Workday data-automation-id pattern):
  - email: input[data-automation-id='email']
  - password: input[data-automation-id='password']
  - verifyPassword: input[data-automation-id='verifyPassword']
  - createAccountBtn: button[aria-label='Create Account']
  - signInBtn: button[aria-label='Sign In']
"""

import json
import re
import asyncio
from pathlib import Path
from typing import Dict, Any, Optional, List
from urllib.parse import urlparse
from playwright.async_api import Page
from loguru import logger

from .base import BaseHandler

# Directory for persisted Workday session cookies
COOKIE_DIR = Path("data/workday_cookies")
COOKIE_DIR.mkdir(parents=True, exist_ok=True)

# Tracking file for which tenants we've created accounts on
ACCOUNT_TRACKER = Path("data/workday_accounts.json")


def _load_accounts() -> Dict[str, Any]:
    """Load the Workday account tracker."""
    if ACCOUNT_TRACKER.exists():
        try:
            return json.loads(ACCOUNT_TRACKER.read_text())
        except Exception:
            return {}
    return {}


def _save_accounts(accounts: Dict[str, Any]) -> None:
    """Save the Workday account tracker."""
    ACCOUNT_TRACKER.parent.mkdir(parents=True, exist_ok=True)
    ACCOUNT_TRACKER.write_text(json.dumps(accounts, indent=2))


def _get_tenant(url: str) -> str:
    """Extract the Workday tenant from a URL.

    e.g. 'https://nvidia.wd5.myworkdayjobs.com/...' -> 'nvidia.wd5'
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    tenant = host.replace(".myworkdayjobs.com", "")
    return tenant


def _make_alias_email(base_email: str, tenant: str) -> str:
    """Create a Gmail alias for a given tenant.

    e.g. alanvu2440@gmail.com + nvidia.wd5 -> alanvu2440+nvidia-wd5@gmail.com
    """
    local, domain = base_email.split("@", 1)
    safe_tenant = tenant.replace(".", "-").replace("_", "-")
    return f"{local}+{safe_tenant}@{domain}"


class WorkdayHandler(BaseHandler):
    """Handler for Workday ATS applications."""

    name = "workday"

    WORKDAY_PASSWORD = "AutoApply2026!#Xk"

    # Tenants known to use SSO (Okta, Azure AD, etc.) — skip these
    SSO_TENANTS: set = set()

    async def apply(self, page: Page, job_url: str, job_data: Dict[str, Any]) -> bool:
        """Apply to a Workday job, handling login walls with auto-account creation."""
        self._last_status = "failed"
        try:
            company = job_data.get("company", "Unknown")
            role = job_data.get("role", "Unknown")
            logger.info(f"Applying to Workday job: {company} - {role}")

            tenant = _get_tenant(job_url)
            logger.debug(f"Workday tenant: {tenant}")

            # Skip known SSO tenants
            if tenant in self.SSO_TENANTS:
                logger.info(f"Skipping SSO tenant: {tenant}")
                self._last_status = "login_required"
                return False

            # Set AI context
            self.ai_answerer.set_job_context(company, role)

            # Try to load saved cookies for this tenant
            await self._load_cookies(page, tenant)

            # Navigate to job URL
            try:
                await page.goto(job_url, wait_until="networkidle", timeout=30000)
            except Exception:
                await page.goto(job_url, wait_until="domcontentloaded", timeout=15000)
            await self.browser_manager.human_delay(2000, 3000)

            # Check if job is closed first
            if await self.is_job_closed(page):
                self._last_status = "closed"
                return False

            # Workday often requires clicking Apply first
            await self._click_apply_button(page)
            await self.browser_manager.human_delay(2000, 3000)

            # Handle "Start Your Application" modal (Apply Manually / Autofill with Resume)
            # Always choose "Apply Manually" — "Use My Last Application" requires auth
            modal_clicked = await self._handle_start_application_modal(page)
            if modal_clicked:
                # Wait for the next page to load after modal selection
                await self.wait_for_page_load(page, timeout=10000)
                await self.browser_manager.human_delay(2000, 3000)

            # Check for SSO redirect (Okta, Azure AD, etc.)
            if await self._is_sso_redirect(page):
                logger.warning(f"SSO redirect detected for tenant {tenant} — cannot authenticate")
                self.SSO_TENANTS.add(tenant)
                self._last_status = "login_required"
                return False

            # Check if we're on the Create Account/Sign In wizard step
            # This IS the application flow — not a separate login wall
            login_detected = await self._detect_login_wall(page)
            if login_detected:
                logger.info("Workday login wall detected — attempting account flow")
                auth_success = await self._handle_auth(page, job_url, tenant)
                if not auth_success:
                    self._last_status = "login_required"
                    logger.warning("Workday auth failed — skipping")
                    return False

                # After auth on the wizard, it should auto-advance to next step
                # Wait for the next wizard step to load
                await self.browser_manager.human_delay(3000, 5000)

                # Check if we're still on Create Account / Sign In page
                still_on_auth = await page.query_selector(
                    'button[data-automation-id="createAccountSubmitButton"], '
                    'button[data-automation-id="signInSubmitButton"], '
                    'div[data-automation-id="click_filter"][aria-label="Create Account"], '
                    'div[data-automation-id="click_filter"][aria-label="Sign In"]'
                )
                if still_on_auth and await still_on_auth.is_visible():
                    logger.info("Still on auth page after account creation — re-navigating")
                    try:
                        await page.goto(job_url, wait_until="networkidle", timeout=30000)
                    except Exception:
                        await page.goto(job_url, wait_until="domcontentloaded", timeout=15000)
                    await self.browser_manager.human_delay(2000, 3000)

                    if await self.is_job_closed(page):
                        self._last_status = "closed"
                        return False

                    await self._click_apply_button(page)
                    await self.browser_manager.human_delay(2000, 3000)
                    await self._handle_start_application_modal(page)
                    await self.browser_manager.human_delay(2000, 3000)
                else:
                    logger.info("Wizard advanced past auth step")

                # Save cookies after successful auth
                await self._save_cookies(page, tenant)

            # Check for "already applied" before filling
            if await self._already_applied(page):
                logger.info(f"Already applied to {company} — skipping")
                self._last_status = "already_applied"
                return False

            # Check for CAPTCHA
            if not await self.handle_captcha(page):
                return False

            # Wait for the application form/wizard to load
            try:
                await page.wait_for_selector(
                    '[data-automation-id="wizardPageContainer"], '
                    'input[data-automation-id="legalNameSection_firstName"], '
                    'input[data-automation-id="file-upload-input-ref"], '
                    'input[type="file"]',
                    timeout=10000,
                )
                logger.debug("Workday application form detected")
            except Exception:
                logger.debug("No Workday form wizard detected after 10s")

            # Handle multi-page application
            max_pages = 10
            prev_page_fields = ""
            stall_count = 0
            for page_num in range(max_pages):
                logger.debug(f"Processing Workday page {page_num + 1}")

                # Fill current page
                await self._fill_current_page(page, job_data)
                await self.browser_manager.human_delay(1000, 2000)

                # Check if we're done
                if await self.is_application_complete(page):
                    logger.info("Workday application submitted successfully!")
                    self._last_status = "success"
                    return True

                # Dry run: validate but don't submit/advance
                if self.dry_run:
                    logger.info("DRY RUN: Workday form filled, skipping submit")
                    await self.take_screenshot(page, "workday_dry_run")
                    self._last_status = "success"
                    return True

                # Detect page change by comparing visible form field IDs
                current_fields = await page.evaluate('''
                    () => Array.from(document.querySelectorAll('[data-automation-id^="formField"]'))
                        .filter(el => el.offsetParent !== null)
                        .map(el => el.getAttribute('data-automation-id'))
                        .join(',')
                ''')

                # Try to advance to next page
                advanced = await self._click_next_or_submit(page)
                if not advanced:
                    break

                await self.browser_manager.human_delay(2000, 3000)

                # Check for Workday validation errors (red error messages)
                wd_errors = await page.query_selector_all('[data-automation-id="errorMessage"]')
                if wd_errors:
                    error_texts = []
                    for err in wd_errors:
                        t = (await err.text_content() or "").strip()
                        if t:
                            error_texts.append(t)
                    if error_texts:
                        logger.warning(f"Workday validation errors: {error_texts}")

                # Check for standard errors too (ignore success messages)
                error = await self.get_error_message(page)
                if error:
                    # Filter out success messages that aren't real errors
                    error_parts = [e for e in error.split("; ") if "successfully" not in e.lower()]
                    if error_parts:
                        logger.warning(f"Workday page error: {'; '.join(error_parts)}")

                # Detect if page actually changed
                new_fields = await page.evaluate('''
                    () => Array.from(document.querySelectorAll('[data-automation-id^="formField"]'))
                        .filter(el => el.offsetParent !== null)
                        .map(el => el.getAttribute('data-automation-id'))
                        .join(',')
                ''')

                if new_fields == current_fields:
                    stall_count += 1
                    logger.warning(f"Page did not advance (stall #{stall_count})")
                    if stall_count >= 3:
                        logger.warning("Stuck on same page after 3 attempts — giving up")
                        break
                    # Try fixing validation errors
                    await self._handle_validation_errors(page, job_data)
                else:
                    stall_count = 0  # Reset on page change
                    logger.info(f"Wizard advanced to new page")

            # Final check
            if await self.is_application_complete(page):
                logger.info("Workday application submitted successfully!")
                self._last_status = "success"
                return True

            if await self.is_job_closed(page):
                self._last_status = "closed"
                return False

            return False

        except Exception as e:
            logger.error(f"Workday application failed: {e}")
            self._last_status = f"exception: {str(e)[:80]}"
            await self.take_screenshot(page, f"workday_error_{job_data.get('company', 'unknown')}")
            return False

    async def detect_form_type(self, page: Page) -> str:
        """Detect Workday form type."""
        wizard = await page.query_selector('[data-automation-id="wizardPageContainer"]')
        if wizard:
            return "wizard"
        return "standard"

    # ── Authentication Flow ──────────────────────────────────────────────

    async def _is_sso_redirect(self, page: Page) -> bool:
        """Check if the page redirected to an SSO provider."""
        url = page.url.lower()
        sso_domains = [
            "okta.com", "login.microsoftonline.com", "auth0.com",
            "onelogin.com", "ping", "sso.", "adfs.",
            "login.windows.net", "accounts.google.com",
        ]
        return any(d in url for d in sso_domains)

    async def _already_applied(self, page: Page) -> bool:
        """Check if we've already applied to this job."""
        try:
            body = (await page.text_content("body") or "").lower()
            return any(x in body for x in [
                "already applied", "you have already",
                "previously submitted", "duplicate application",
            ])
        except Exception:
            return False

    async def _detect_login_wall(self, page: Page) -> bool:
        """Check if the current page is a Workday login/signup wall."""
        current_url = page.url.lower()
        url_indicators = ['/login', '/signin', '/sso', '/auth', '/account/']
        if any(x in current_url for x in url_indicators):
            # Double-check it's not SSO
            if await self._is_sso_redirect(page):
                return True
            return True

        try:
            page_text = (await page.text_content("body") or "").lower()
        except Exception:
            return False

        text_indicators = [
            'sign in', 'create account', 'log in to apply',
            'existing user', 'new user', 'create your account',
            'sign in with your email', 'already have an account',
        ]
        if any(x in page_text for x in text_indicators):
            # Check if the application form is already present (not a login wall)
            form = await page.query_selector(
                '[data-automation-id="wizardPageContainer"], '
                'input[data-automation-id="legalNameSection_firstName"]'
            )
            if form:
                return False

            # Check if this is a "Start Your Application" modal — NOT a login wall
            # These modals have "Apply Manually" / "Autofill with Resume" alongside "Sign In"
            apply_modal = await page.query_selector(
                'button:has-text("Apply Manually"), '
                'a:has-text("Apply Manually"), '
                'button:has-text("Autofill with Resume"), '
                'a:has-text("Autofill with Resume"), '
                'button:has-text("Use My Last Application"), '
                'a:has-text("Use My Last Application")'
            )
            if apply_modal:
                logger.debug("Detected 'Start Your Application' modal, not a login wall")
                return False

            return True
        return False

    async def _handle_auth(self, page: Page, job_url: str, tenant: str) -> bool:
        """Handle Workday authentication — create account or sign in."""
        # Don't attempt auth on SSO pages
        if await self._is_sso_redirect(page):
            logger.warning(f"SSO detected for {tenant} — cannot auto-authenticate")
            self.SSO_TENANTS.add(tenant)
            return False

        accounts = _load_accounts()
        config = self.form_filler.config
        base_email = config.get("personal_info", {}).get("email", "")
        alias_email = _make_alias_email(base_email, tenant)

        if tenant in accounts:
            logger.info(f"Existing Workday account for {tenant}, signing in as {alias_email}")
            success = await self._signin(page, alias_email)
            if success:
                # Update account record if needed
                if "password" not in accounts.get(tenant, {}):
                    accounts[tenant]["password"] = self.WORKDAY_PASSWORD
                    _save_accounts(accounts)
                return True
            # Sign-in failed — maybe password changed or account was deleted
            logger.info("Sign-in failed, trying to create new account")
            # Navigate back to the login page
            try:
                await page.goto(job_url, wait_until="domcontentloaded", timeout=15000)
                await self.browser_manager.human_delay(2000, 3000)
                await self._click_apply_button(page)
                await self.browser_manager.human_delay(2000, 3000)
            except Exception:
                pass
            success = await self._create_account(page, alias_email, tenant)
            if success:
                accounts[tenant] = {"email": alias_email, "created": True}
                _save_accounts(accounts)
            return success
        else:
            logger.info(f"No Workday account for {tenant}, creating with {alias_email}")
            success = await self._create_account(page, alias_email, tenant)
            if success:
                accounts[tenant] = {"email": alias_email, "password": self.WORKDAY_PASSWORD, "created": True}
                _save_accounts(accounts)
                return True
            # Create failed — try signin (maybe account exists from a previous run)
            logger.info("Account creation failed, trying signin as fallback")
            success = await self._signin(page, alias_email)
            if success:
                accounts[tenant] = {"email": alias_email, "password": self.WORKDAY_PASSWORD, "created": True}
                _save_accounts(accounts)
            return success

    async def _create_account(self, page: Page, email: str, tenant: str) -> bool:
        """Create a new Workday account."""
        try:
            # Wait for the create account/sign-in form to load (Workday loads async)
            try:
                await page.wait_for_selector(
                    'input[data-automation-id="email"], '
                    'input[data-automation-id="password"], '
                    'button[data-automation-id="createAccountSubmitButton"], '
                    'button[data-automation-id="signInSubmitButton"]',
                    timeout=15000,
                )
            except Exception:
                logger.debug("Timed out waiting for account form to load")

            # Check if we're already on the create account page (has email input)
            already_on_create = await page.query_selector(
                'input[data-automation-id="email"], '
                'button[data-automation-id="createAccountSubmitButton"]'
            )
            if not already_on_create:
                # Navigate TO the create account form (click links, NOT submit buttons)
                create_selectors = [
                    '[data-automation-id="createAccountLink"]',
                    'a:has-text("Create Account")',
                    'a:has-text("Sign Up")',
                    'a:has-text("New User")',
                ]
                for sel in create_selectors:
                    try:
                        btn = await page.query_selector(sel)
                        if btn and await btn.is_visible():
                            await btn.click()
                            logger.debug(f"Clicked: {sel}")
                            await self.browser_manager.human_delay(1500, 2500)
                            break
                    except Exception:
                        continue

            # Fill email — Workday uses various automation IDs
            email_selectors = [
                'input[data-automation-id="email"]',
                'input[data-automation-id="signIn-email"]',
                'input[data-automation-id="createAccount-email"]',
                'input[data-automation-id="emailAddress"]',
                'input[type="email"]',
                'input[name="email"]',
                'input[aria-label*="email" i]',
                'input[placeholder*="email" i]',
            ]
            email_input = None
            for sel in email_selectors:
                email_input = await page.query_selector(sel)
                if email_input and await email_input.is_visible():
                    break
                email_input = None

            if email_input:
                await email_input.fill(email)
                await self.browser_manager.human_delay(300, 600)
            else:
                logger.warning("Could not find email input for account creation")
                # Debug: log all visible inputs to help diagnose
                try:
                    inputs = await page.query_selector_all('input:visible')
                    for inp in inputs[:5]:
                        attrs = await inp.evaluate(
                            "el => ({type: el.type, name: el.name, id: el.id, "
                            "autoId: el.getAttribute('data-automation-id'), "
                            "placeholder: el.placeholder})"
                        )
                        logger.debug(f"  Visible input: {attrs}")
                except Exception:
                    pass
                return False

            # Fill password — handle multiple password fields
            # Use evaluate to find ALL visible password inputs (some may not have type="password" initially)
            pw_fields = await page.query_selector_all('input[type="password"]')
            # Filter to only visible ones
            visible_pw = []
            for pw in pw_fields:
                if await pw.is_visible():
                    visible_pw.append(pw)

            if len(visible_pw) >= 2:
                await visible_pw[0].fill(self.WORKDAY_PASSWORD)
                await self.browser_manager.human_delay(200, 400)
                await visible_pw[1].fill(self.WORKDAY_PASSWORD)
                await self.browser_manager.human_delay(200, 400)
                logger.debug("Filled 2 password fields")
            elif len(visible_pw) == 1:
                await visible_pw[0].fill(self.WORKDAY_PASSWORD)
                await self.browser_manager.human_delay(200, 400)
                # Check for verify field that loaded after
                verify = await page.query_selector('input[data-automation-id="verifyPassword"]')
                if verify and await verify.is_visible():
                    await verify.fill(self.WORKDAY_PASSWORD)
                    await self.browser_manager.human_delay(200, 400)
                logger.debug("Filled 1 password field")
            else:
                # Try specific selectors
                pw_input = await page.query_selector(
                    'input[data-automation-id="password"], input[type="password"]'
                )
                if pw_input and await pw_input.is_visible():
                    await pw_input.fill(self.WORKDAY_PASSWORD)
                    await self.browser_manager.human_delay(200, 400)
                else:
                    logger.warning("Could not find any password fields for account creation")

            # Check any terms/agreement checkboxes — try all visible checkboxes on the page
            terms_selectors = [
                'input[data-automation-id="createAccountCheckbox"]',
                'input[type="checkbox"][data-automation-id*="agree"]',
                'input[type="checkbox"][data-automation-id*="terms"]',
                'input[type="checkbox"][data-automation-id*="consent"]',
                'input[type="checkbox"]',  # Generic fallback — any unchecked checkbox
            ]
            for sel in terms_selectors:
                try:
                    cbs = await page.query_selector_all(sel)
                    for cb in cbs:
                        if await cb.is_visible() and not await cb.is_checked():
                            await cb.click()
                            await self.browser_manager.human_delay(200, 400)
                            logger.debug(f"Checked checkbox: {sel}")
                            break
                    else:
                        continue
                    break
                except Exception:
                    continue

            # Click Create Account submit button
            # Workday uses click_filter overlay divs as the actual click targets
            clicked = await self._click_workday_button(page, "Create Account")
            if not clicked:
                logger.warning("Could not find Create Account submit button")
                return False

            await self.browser_manager.human_delay(3000, 5000)

            # Check for "already in use" error
            try:
                body_text = (await page.text_content("body") or "").lower()
                if "already in use" in body_text or "already exists" in body_text or "sign into this account" in body_text:
                    logger.info("Account already exists for this tenant")
                    return False  # Caller will try signin
            except Exception:
                pass

            # Check if we need email verification
            if await self._needs_email_verification(page):
                logger.info("Email verification required — checking Gmail via EmailVerifier")
                verified = await self._verify_email(page, email, tenant)
                if not verified:
                    logger.warning("Email verification failed")
                    return False

            # Check if account creation succeeded — wizard should advance past Create Account
            await self.browser_manager.human_delay(2000, 3000)

            # Multiple indicators that we're still on Create Account page
            still_on_create = False
            for check_sel in [
                'button[data-automation-id="createAccountSubmitButton"]',
                'input[data-automation-id="verifyPassword"]',
            ]:
                elem = await page.query_selector(check_sel)
                if elem and await elem.is_visible():
                    still_on_create = True
                    break

            if not still_on_create:
                # Also check for visible password fields (means we're still on Create Account)
                pw_visible = await page.query_selector_all('input[type="password"]')
                for pw in pw_visible:
                    if await pw.is_visible():
                        still_on_create = True
                        break

            if not still_on_create:
                # Check page text for "Create Account" heading
                try:
                    body = (await page.text_content("body") or "")
                    # Look for "Create Account" as a heading (not just in footer links)
                    if "Password Requirements:" in body or "Verify New Password" in body:
                        still_on_create = True
                except Exception:
                    pass

            if still_on_create:
                logger.warning("Still on Create Account page after submission")
                return False

            logger.info(f"Workday account created for {tenant}")
            return True

        except Exception as e:
            logger.error(f"Workday account creation failed: {e}")
            return False

    async def _signin(self, page: Page, email: str) -> bool:
        """Sign into an existing Workday account."""
        try:
            # Wait for the form to load first (Workday loads async)
            try:
                await page.wait_for_selector(
                    'input[data-automation-id="email"], input[type="email"], '
                    'button[data-automation-id="signInLink"], '
                    'button[data-automation-id="createAccountSubmitButton"]',
                    timeout=10000,
                )
            except Exception:
                logger.debug("Timed out waiting for sign-in form to load")

            # Click "Sign In" link to switch from Create Account to Sign In view
            # The signInLink is a form button, NOT the header Sign In
            switched = False
            # Try clicking the signInLink button directly (it's usually a small text link)
            signin_link = await page.query_selector('[data-automation-id="signInLink"]')
            if signin_link and await signin_link.is_visible():
                try:
                    await signin_link.click(force=True, timeout=5000)
                    switched = True
                    logger.debug("Clicked signInLink (force)")
                except Exception:
                    try:
                        await signin_link.evaluate("el => el.click()")
                        switched = True
                        logger.debug("Clicked signInLink (JS)")
                    except Exception:
                        pass
            if not switched:
                # Fallback to text-based selectors
                for sel in ['a:has-text("Already have an account")', 'a:has-text("Sign In")']:
                    try:
                        btn = await page.query_selector(sel)
                        if btn and await btn.is_visible():
                            await btn.click(force=True, timeout=5000)
                            switched = True
                            break
                    except Exception:
                        continue
            if switched:
                await self.browser_manager.human_delay(1500, 2500)

            # Fill email — Workday uses various automation IDs
            email_selectors = [
                'input[data-automation-id="email"]',
                'input[data-automation-id="signIn-email"]',
                'input[data-automation-id="signInEmailAddress"]',
                'input[data-automation-id="emailAddress"]',
                'input[type="email"]',
                'input[name="email"]',
                'input[aria-label*="email" i]',
                'input[placeholder*="email" i]',
            ]
            email_input = None
            for sel in email_selectors:
                email_input = await page.query_selector(sel)
                if email_input and await email_input.is_visible():
                    break
                email_input = None

            if email_input:
                await email_input.fill(email)
                await self.browser_manager.human_delay(300, 600)
            else:
                logger.warning("Could not find email input for signin")
                # Debug: log visible inputs
                try:
                    inputs = await page.query_selector_all('input:visible')
                    for inp in inputs[:5]:
                        attrs = await inp.evaluate(
                            "el => ({type: el.type, name: el.name, id: el.id, "
                            "autoId: el.getAttribute('data-automation-id'), "
                            "placeholder: el.placeholder})"
                        )
                        logger.debug(f"  Visible input: {attrs}")
                except Exception:
                    pass
                return False

            # Fill password
            pw_input = await page.query_selector(
                'input[data-automation-id="password"], '
                'input[type="password"]'
            )
            if pw_input and await pw_input.is_visible():
                await pw_input.fill(self.WORKDAY_PASSWORD)
                await self.browser_manager.human_delay(300, 600)

            # Click Sign In submit button
            # Must target signInSubmitButton specifically, NOT the header Sign In
            clicked_submit = False
            signin_submit = await page.query_selector('button[data-automation-id="signInSubmitButton"]')
            if signin_submit and await signin_submit.is_visible():
                # Find its click_filter parent/sibling
                filter_elem = await page.evaluate_handle('''
                    () => {
                        const btn = document.querySelector('[data-automation-id="signInSubmitButton"]');
                        if (!btn) return null;
                        const parent = btn.parentElement;
                        if (!parent) return null;
                        return parent.querySelector('[data-automation-id="click_filter"]') || null;
                    }
                ''')
                if filter_elem:
                    try:
                        await filter_elem.click(timeout=5000)
                        clicked_submit = True
                        logger.info("Clicked Sign In submit (click_filter)")
                    except Exception:
                        pass
                if not clicked_submit:
                    try:
                        await signin_submit.click(force=True, timeout=5000)
                        clicked_submit = True
                        logger.info("Clicked Sign In submit (force)")
                    except Exception:
                        pass
                if not clicked_submit:
                    try:
                        await signin_submit.evaluate("el => el.click()")
                        clicked_submit = True
                        logger.info("Clicked Sign In submit (JS)")
                    except Exception:
                        pass

            await self.browser_manager.human_delay(3000, 5000)

            # Check if email verification needed after signin
            if await self._needs_email_verification(page):
                logger.info("Email verification required after signin")
                verified = await self._verify_email(page, email, _get_tenant(page.url))
                if not verified:
                    return False

            # Check for sign-in errors
            try:
                body_text = (await page.text_content("body") or "").lower()
                if "invalid" in body_text and ("password" in body_text or "credentials" in body_text):
                    logger.warning("Workday sign-in failed: invalid credentials")
                    return False
                if "account is locked" in body_text:
                    logger.warning("Workday account locked")
                    return False
            except Exception:
                pass

            # Verify sign-in succeeded: the Create Account form should be gone
            # and replaced with application form fields or next wizard step
            still_on_signin = await page.query_selector(
                'button[data-automation-id="signInSubmitButton"], '
                'button[data-automation-id="createAccountSubmitButton"]'
            )
            if still_on_signin and await still_on_signin.is_visible():
                logger.warning("Still on sign-in page after clicking Sign In")
                return False

            logger.info("Workday sign-in successful")
            return True

        except Exception as e:
            logger.error(f"Workday signin failed: {e}")
            return False

    async def _needs_email_verification(self, page: Page) -> bool:
        """Check if Workday is asking for email verification."""
        try:
            body_text = (await page.text_content("body") or "").lower()
            return any(x in body_text for x in [
                "verify your email", "verification code", "check your email",
                "sent a code", "enter the code", "email verification",
            ])
        except Exception:
            return False

    async def _verify_email(self, page: Page, email: str, tenant: str) -> bool:
        """Read verification code from Gmail using EmailVerifier and enter it."""
        try:
            # Use the robust EmailVerifier attached by main.py
            if not self.email_verifier:
                logger.warning("No email_verifier configured — cannot verify email")
                return False

            logger.info("Waiting for Workday verification email via EmailVerifier...")

            # EmailVerifier.get_verification_code is synchronous (blocking)
            # Run in executor to not block the event loop
            loop = asyncio.get_event_loop()
            code = await loop.run_in_executor(
                None,
                lambda: self.email_verifier.get_verification_code(
                    sender_filter="workday",
                    subject_filter=None,
                    max_age_seconds=300,
                    timeout=90,
                    poll_interval=5,
                )
            )

            if not code:
                logger.warning("No verification code found via EmailVerifier")
                return False

            logger.info(f"Found Workday verification code: {code}")

            # Enter the code on the page
            code_input = await page.query_selector(
                'input[data-automation-id="verificationCode"], '
                'input[type="text"][placeholder*="code" i], '
                'input[type="number"], '
                'input[data-automation-id*="code"], '
                'input[aria-label*="code" i]'
            )
            if code_input and await code_input.is_visible():
                await code_input.fill(code)
                await self.browser_manager.human_delay(500, 1000)

                # Click verify/submit
                verify_btn = await page.query_selector(
                    'button:has-text("Verify"), button:has-text("Submit"), '
                    'button[data-automation-id="verifyButton"], button[type="submit"]'
                )
                if verify_btn and await verify_btn.is_visible():
                    await verify_btn.click()
                    await self.browser_manager.human_delay(3000, 5000)
                    return True

            return False

        except Exception as e:
            logger.error(f"Email verification failed: {e}")
            return False

    async def _click_workday_button(self, page: Page, label: str) -> bool:
        """Click a Workday button using the click_filter overlay.

        Workday renders custom buttons with a transparent click_filter overlay div
        that intercepts pointer events. The actual click handler is on this overlay,
        not on the underlying <button> element.

        Args:
            label: The aria-label or text of the button (e.g., 'Create Account', 'Sign In')
        """
        try:
            # Try the click_filter overlay first (the real interactive element)
            filter_elem = await page.query_selector(
                f'div[data-automation-id="click_filter"][aria-label="{label}"]'
            )
            if filter_elem and await filter_elem.is_visible():
                await filter_elem.click(timeout=5000)
                logger.debug(f"Clicked click_filter for '{label}'")
                return True
        except Exception as e:
            logger.debug(f"click_filter click failed for '{label}': {e}")

        # Fallback: try force-clicking the button directly
        try:
            btn = await page.query_selector(f'button:has-text("{label}")')
            if btn and await btn.is_visible():
                await btn.click(force=True, timeout=5000)
                logger.debug(f"Force-clicked button '{label}'")
                return True
        except Exception:
            pass

        # Last resort: JS click
        try:
            btn = await page.query_selector(f'button:has-text("{label}")')
            if btn:
                await btn.evaluate("el => el.click()")
                logger.debug(f"JS-clicked button '{label}'")
                return True
        except Exception:
            pass

        return False

    # ── Cookie Persistence ───────────────────────────────────────────────

    async def _save_cookies(self, page: Page, tenant: str) -> None:
        """Save browser cookies for a Workday tenant."""
        try:
            cookies = await page.context.cookies()
            cookie_file = COOKIE_DIR / f"{tenant}.json"
            cookie_file.write_text(json.dumps(cookies, indent=2))
            logger.debug(f"Saved {len(cookies)} cookies for tenant {tenant}")
        except Exception as e:
            logger.debug(f"Could not save cookies: {e}")

    async def _load_cookies(self, page: Page, tenant: str) -> bool:
        """Load saved cookies for a Workday tenant."""
        try:
            cookie_file = COOKIE_DIR / f"{tenant}.json"
            if not cookie_file.exists():
                return False
            cookies = json.loads(cookie_file.read_text())
            if cookies:
                await page.context.add_cookies(cookies)
                logger.debug(f"Loaded {len(cookies)} cookies for tenant {tenant}")
                return True
        except Exception as e:
            logger.debug(f"Could not load cookies: {e}")
        return False

    # ── Application Flow ─────────────────────────────────────────────────

    async def _click_apply_button(self, page: Page) -> bool:
        """Click the Apply button on Workday."""
        apply_selectors = [
            'button[data-automation-id="applyBtn"]',
            'a[data-automation-id="applyBtn"]',
            '[data-automation-id="jobPostingApplyButton"]',
            'button:has-text("Continue Application")',
            'a:has-text("Continue Application")',
            'button:has-text("Apply")',
            'a:has-text("Apply")',
        ]
        for selector in apply_selectors:
            try:
                btn = await page.query_selector(selector)
                if btn and await btn.is_visible():
                    await btn.click()
                    logger.debug("Clicked Workday Apply button")
                    return True
            except Exception:
                continue
        return False

    async def _handle_start_application_modal(self, page: Page) -> bool:
        """Handle Workday 'Start Your Application' modal.

        This modal appears on some Workday sites after clicking Apply and offers:
        - 'Autofill with Resume' (preferred — auto-fills from last application)
        - 'Apply Manually'
        - 'Use My Last Application'

        We prefer 'Use My Last Application' > 'Autofill with Resume' > 'Apply Manually'.
        """
        try:
            # Preferred: "Apply Manually" — goes to wizard step 1 (Create Account/Sign In)
            # "Use My Last Application" requires existing auth — avoid before sign-in
            modal_selectors = [
                'button:has-text("Apply Manually")',
                'a:has-text("Apply Manually")',
                'button[data-automation-id="applyManually"]',
                'button:has-text("Autofill with Resume")',
                'a:has-text("Autofill with Resume")',
                'button:has-text("Use My Last Application")',
                'a:has-text("Use My Last Application")',
            ]

            for sel in modal_selectors:
                try:
                    btn = await page.query_selector(sel)
                    if btn and await btn.is_visible():
                        await btn.click()
                        logger.info(f"Clicked Workday start application modal: {sel}")
                        await self.browser_manager.human_delay(2000, 3000)
                        return True
                except Exception:
                    continue

            return False
        except Exception:
            return False

    async def _fill_current_page(self, page: Page, job_data: Dict[str, Any]) -> None:
        """Fill all fields on the current Workday page."""
        config = self.form_filler.config
        personal = config.get("personal_info", {})

        await self._fill_text_fields(page, personal)
        await self._fill_dropdowns(page, config)
        await self._fill_well_known_fields(page, config)
        await self._fill_checkboxes(page, config)
        await self._upload_resume_if_needed(page, config)
        await self._fill_work_experience(page, config)
        await self._fill_education(page, config)

        # Scroll through the page to trigger lazy loading of all form fields
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await self.browser_manager.human_delay(500, 1000)
        await page.evaluate("window.scrollTo(0, 0)")
        await self.browser_manager.human_delay(500, 1000)

        await self._handle_custom_questions(page, job_data)

        # Second radio pass — some radios only appear after other fields are filled
        await self._fill_radio_buttons(page, config)

        # Final cleanup — fix fields that _handle_custom_questions may have corrupted
        await self._final_field_cleanup(page, personal)

    async def _fill_well_known_fields(self, page: Page, config: Dict[str, Any]) -> None:
        """Fill well-known Workday form fields by their formField automation IDs.

        These are fields that appear across many Workday tenants with consistent IDs.
        """
        field_values = {
            "formField-source": "Online Job Board",  # How Did You Hear About Us?
            "formField-referredBy": "",  # Referred By — leave empty if no referral
        }

        for field_id, default_value in field_values.items():
            try:
                container = await page.query_selector(f'[data-automation-id="{field_id}"]')
                if not container or not await container.is_visible():
                    continue

                # Check for text input
                text_input = await container.query_selector('input[type="text"], textarea')
                if text_input and await text_input.is_visible():
                    current = await text_input.input_value() or ""
                    if not current.strip() and default_value:
                        await text_input.fill(default_value)
                        self._fields_filled[field_id] = default_value[:30]
                        logger.debug(f"Filled {field_id} = '{default_value}'")
                    continue

                # Check for dropdown button
                dropdown_btn = await container.query_selector('button[aria-label]')
                if dropdown_btn and await dropdown_btn.is_visible():
                    aria = (await dropdown_btn.get_attribute('aria-label') or "")
                    if "select one" in aria.lower() and default_value:
                        await dropdown_btn.click()
                        await self.browser_manager.human_delay(400, 700)
                        items = await page.query_selector_all('[data-automation-id="menuItem"], [role="option"]')
                        for item in items:
                            text = (await item.text_content() or "").strip()
                            if default_value.lower() in text.lower():
                                await item.click()
                                self._fields_filled[field_id] = text[:30]
                                break
                        else:
                            # Select first option if no match
                            if items:
                                text = (await items[0].text_content() or "").strip()
                                await items[0].click()
                                self._fields_filled[field_id] = text[:30]
                            else:
                                await page.keyboard.press("Escape")
                    continue

                # Check for typeahead (multiselect search)
                search_input = await container.query_selector('input[placeholder="Search"]')
                if search_input and await search_input.is_visible() and default_value:
                    # Check if already has a selection
                    has_sel = await container.query_selector('[data-automation-id="selectedItemList"] li')
                    if not has_sel:
                        await search_input.click()
                        await search_input.type(default_value, delay=50)
                        await self.browser_manager.human_delay(1000, 1500)
                        items = await page.query_selector_all('[data-automation-id="promptOption"], [data-automation-id="menuItem"]')
                        if items:
                            await items[0].click()
                            self._fields_filled[field_id] = default_value[:30]
                        else:
                            await page.keyboard.press("Escape")

            except Exception as e:
                logger.debug(f"Error filling {field_id}: {e}")

    async def _fill_text_fields(self, page: Page, personal: Dict[str, Any]) -> None:
        """Fill text input fields."""
        # Strip country code from phone — Workday has a separate country code dropdown
        phone = personal.get("phone", "")
        phone = re.sub(r"^(\+?1[-.\s]?)", "", phone)  # strip +1, 1-, etc.
        phone = re.sub(r"[^\d]", "", phone)  # digits only (e.g. 4089217836)

        # Workday uses different IDs across tenants — try both patterns
        field_mappings = [
            # First name (two patterns)
            ('[data-automation-id="legalNameSection_firstName"]', personal.get("first_name")),
            ('input#name--legalName--firstName', personal.get("first_name")),
            # Last name
            ('[data-automation-id="legalNameSection_lastName"]', personal.get("last_name")),
            ('input#name--legalName--lastName', personal.get("last_name")),
            # Email
            ('[data-automation-id="email"]', personal.get("email")),
            # Phone number
            ('[data-automation-id="phone-number"]', phone),
            ('input#phoneNumber--phoneNumber', phone),
            # Address
            ('[data-automation-id="addressSection_addressLine1"]', personal.get("address")),
            ('input#address--addressLine1', personal.get("address")),
            # City
            ('[data-automation-id="addressSection_city"]', personal.get("city")),
            ('input#address--city', personal.get("city")),
            # Zip
            ('[data-automation-id="addressSection_postalCode"]', personal.get("zip_code")),
            ('input#address--postalCode', personal.get("zip_code")),
            # LinkedIn
            ('input[data-automation-id="linkedinQuestion"]', personal.get("linkedin")),
            # County (not country — this is e.g. "Santa Clara")
            ('input#address--county', personal.get("county", "Santa Clara")),
        ]

        for selector, value in field_mappings:
            if value:
                try:
                    elem = await page.query_selector(selector)
                    if elem and await elem.is_visible():
                        current = await elem.input_value() or ""
                        if not current.strip():
                            await elem.fill(str(value))
                            self._fields_filled[selector.split('"')[1] if '"' in selector else selector] = str(value)[:30]
                            await self.browser_manager.human_delay(100, 300)
                except Exception as e:
                    logger.debug(f"Could not fill {selector}: {e}")

        # Also try generic form filler for any remaining fields
        await self.form_filler.fill_form(page)

        # Re-correct phone number — generic form filler may format as (408) 921-7836 or +14089217836
        for phone_sel in ['[data-automation-id="phone-number"]', 'input#phoneNumber--phoneNumber']:
            try:
                elem = await page.query_selector(phone_sel)
                if elem and await elem.is_visible():
                    current = (await elem.input_value() or "").strip()
                    if current:
                        digits = re.sub(r"[^\d]", "", current)
                        # Strip leading 1 (country code) if 11 digits
                        if len(digits) == 11 and digits.startswith("1"):
                            digits = digits[1:]
                        if digits != current:
                            await elem.fill(digits)
                            logger.debug(f"Re-corrected phone: '{current}' → '{digits}'")
            except Exception:
                pass

        # Clear phone extension if it was accidentally filled with the phone number
        for ext_sel in ['input#phoneNumber--extension', '[data-automation-id="phone-extension"]']:
            try:
                ext = await page.query_selector(ext_sel)
                if ext and await ext.is_visible():
                    val = (await ext.input_value() or "").strip()
                    if val and len(val) > 5:  # Real extensions are short
                        await ext.fill("")
                        logger.debug(f"Cleared phone extension (was '{val}')")
            except Exception:
                pass

    async def _final_field_cleanup(self, page: Page, personal: Dict[str, Any]) -> None:
        """Final cleanup pass — fix fields corrupted by generic form filler or _handle_custom_questions."""
        # --- Social Network URLs: fill LinkedIn, clear garbage from Facebook/Twitter ---
        linkedin = personal.get("linkedin", "")
        social_fields = {
            "linkedin": linkedin,
            "facebook": "",
            "twitter": "",
        }
        for key, value in social_fields.items():
            try:
                # Find input with label containing the social network name
                inputs = await page.query_selector_all('input[type="text"], input:not([type])')
                for inp in inputs:
                    if not await inp.is_visible():
                        continue
                    label = await inp.evaluate('''el => {
                        const label = el.closest('[data-automation-id^="formField"]')?.querySelector('label');
                        return label?.textContent?.trim().toLowerCase() || '';
                    }''')
                    if key in label:
                        current = (await inp.input_value() or "").strip()
                        if key == "linkedin" and not current and value:
                            await inp.fill(value)
                            logger.debug(f"Cleanup: set {key} = {value}")
                        elif key != "linkedin" and current:
                            # Clear garbage from non-LinkedIn social fields
                            if not current.startswith("http"):
                                await inp.fill("")
                                logger.debug(f"Cleanup: cleared {key} (was '{current[:30]}')")
                        break
            except Exception:
                pass

        # Strip phone number to digits only (form_filler may format as (408) 921-7836)
        phone_raw = personal.get("phone", "")
        phone_digits = re.sub(r"^(\+?1[-.\s]?)", "", phone_raw)
        phone_digits = re.sub(r"[^\d]", "", phone_digits)

        for phone_sel in ['[data-automation-id="phone-number"]', 'input#phoneNumber--phoneNumber']:
            try:
                elem = await page.query_selector(phone_sel)
                if elem and await elem.is_visible():
                    current = (await elem.input_value() or "").strip()
                    digits_only = re.sub(r"[^\d]", "", current)
                    if current and digits_only != current:
                        await elem.fill(phone_digits or digits_only)
                        logger.debug(f"Cleanup: phone '{current}' → '{phone_digits or digits_only}'")
            except Exception:
                pass

        # Clear phone extension if it has garbage (> 5 chars = not a real extension)
        for ext_sel in ['input#phoneNumber--extension', '[data-automation-id="phone-extension"]']:
            try:
                ext = await page.query_selector(ext_sel)
                if ext and await ext.is_visible():
                    val = (await ext.input_value() or "").strip()
                    if val and len(val) > 5:
                        await ext.fill("")
                        logger.debug(f"Cleanup: cleared phone extension (was '{val[:30]}')")
            except Exception:
                pass

        # Clear country phone code search input if it has garbage text
        for cpc_sel in ['input#phoneNumber--countryPhoneCode', 'input[data-automation-id="countryPhoneCode"]']:
            try:
                elem = await page.query_selector(cpc_sel)
                if elem and await elem.is_visible():
                    val = (await elem.input_value() or "").strip()
                    # If the search input has text that's not a search query, clear it
                    if val and len(val) > 20:
                        await elem.fill("")
                        logger.debug(f"Cleanup: cleared country phone code input (was '{val[:30]}')")
            except Exception:
                pass

        # Fix county field — form_filler often puts state abbreviation (e.g. "CA") instead of county name
        county = personal.get("county", "Santa Clara")
        for county_sel in ['input#address--county', '[data-automation-id="addressSection_county"]']:
            try:
                elem = await page.query_selector(county_sel)
                if elem and await elem.is_visible():
                    current = (await elem.input_value() or "").strip()
                    if current and len(current) <= 3:  # State abbrev like "CA" — replace
                        await elem.fill(county)
                        logger.debug(f"Cleanup: county '{current}' → '{county}'")
                    elif not current:
                        await elem.fill(county)
            except Exception:
                pass

    async def _fill_dropdowns(self, page: Page, config: Dict[str, Any]) -> None:
        """Fill dropdown fields (Workday uses custom dropdowns, not standard select).

        Workday uses two different dropdown mechanisms across tenants:
        1. Custom div dropdowns with data-automation-id (older style)
        2. Button dropdowns with aria-label (newer style, e.g. Zoom)
        """
        personal = config.get("personal_info", {})
        country = personal.get("country", "United States")
        state = personal.get("state")

        # Country dropdown — try both selector patterns
        if not await self._fill_workday_dropdown(page, '[data-automation-id="addressSection_countryRegion"]', country):
            await self._fill_aria_dropdown(page, "Country", country)

        # State dropdown — use formField container to find the right button
        if state:
            if not await self._fill_workday_dropdown(page, '[data-automation-id="addressSection_countryRegionState"]', state):
                # Find state dropdown via formField-countryRegion container (avoids matching "United States" in Country)
                state_filled = False
                state_container = await page.query_selector('[data-automation-id="formField-countryRegion"]')
                if state_container:
                    state_btn = await state_container.query_selector('button[aria-label]')
                    if state_btn and await state_btn.is_visible():
                        aria = (await state_btn.get_attribute('aria-label') or "")
                        if "select one" in aria.lower():
                            await state_btn.click()
                            await self.browser_manager.human_delay(500, 800)
                            try:
                                await page.wait_for_selector('[data-automation-id="menuItem"], [role="option"]', timeout=3000)
                                items = await page.query_selector_all('[data-automation-id="menuItem"], [role="option"]')
                                for item in items:
                                    text = (await item.text_content() or "").strip()
                                    if state.lower() in text.lower():
                                        await item.click()
                                        logger.debug(f"Selected state: {text}")
                                        state_filled = True
                                        break
                                if not state_filled:
                                    await page.keyboard.press("Escape")
                            except Exception:
                                await page.keyboard.press("Escape")
                if not state_filled:
                    await self._fill_aria_dropdown(page, "State ", state)  # trailing space to avoid "States"

        # Phone device type
        if not await self._fill_workday_dropdown(page, '[data-automation-id="phone-device-type"]', "Mobile"):
            await self._fill_aria_dropdown(page, "Phone Device Type", "Mobile")

        # Phone country code — multiselect typeahead
        await self._fill_country_phone_code(page)

        # Handle generic dropdowns (How Did You Hear, etc.)
        await self._fill_generic_workday_dropdowns(page, config)

        # Handle radio button groups
        await self._fill_radio_buttons(page, config)

    async def _fill_aria_dropdown(self, page: Page, label_contains: str, value: str) -> bool:
        """Fill a dropdown button identified by aria-label (newer Workday pattern).

        These are <button aria-label="State Select One Required"> style dropdowns.
        """
        try:
            # Find button whose aria-label starts with the label
            buttons = await page.query_selector_all('button[aria-label]')
            for btn in buttons:
                aria = (await btn.get_attribute('aria-label') or "")
                # Match at word boundary — "State" matches "State Select One" but not "United States"
                aria_words = aria.lower().split()
                label_words = label_contains.strip().lower().split()
                if not all(w in aria_words for w in label_words):
                    continue
                if not await btn.is_visible():
                    continue

                # Check if already selected (aria doesn't contain "Select One")
                if "select one" not in aria.lower():
                    logger.debug(f"Aria dropdown '{label_contains}' already has value: {aria}")
                    return True

                await btn.click()
                await self.browser_manager.human_delay(500, 800)

                # Wait for menu to appear
                try:
                    await page.wait_for_selector('[data-automation-id="menuItem"], [role="option"]', timeout=3000)
                except Exception:
                    await page.keyboard.press("Escape")
                    return False

                # Find matching option
                items = await page.query_selector_all('[data-automation-id="menuItem"], [role="option"]')
                for item in items:
                    text = (await item.text_content() or "").strip()
                    if value.lower() in text.lower():
                        await item.click()
                        logger.debug(f"Selected aria dropdown '{label_contains}' → '{text}'")
                        await self.browser_manager.human_delay(200, 400)
                        return True

                # No match — try first option or dismiss
                await page.keyboard.press("Escape")
                return False
            return False
        except Exception as e:
            logger.debug(f"Aria dropdown '{label_contains}' failed: {e}")
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            return False

    async def _fill_country_phone_code(self, page: Page) -> bool:
        """Fill the Country Phone Code multiselect typeahead.

        This field is a multiselect with a search input. When already filled,
        the input value is empty but there's a selectedItemList with items.
        """
        try:
            # Check if already filled via selectedItemList
            has_selected = await page.evaluate('''
                () => {
                    // Check both possible containers
                    const containers = [
                        document.querySelector('[data-automation-id="formField-countryPhoneCode"]'),
                        document.querySelector('[data-automation-id="countryPhoneCode"]')
                    ].filter(Boolean);
                    for (const c of containers) {
                        const items = c.querySelectorAll('[data-automation-id="selectedItemList"] li');
                        if (items.length > 0) return true;
                    }
                    return false;
                }
            ''')
            if has_selected:
                logger.debug("Country phone code already has a selection")
                return True

            # Try the typeahead input
            code_input = await page.query_selector(
                'input#phoneNumber--countryPhoneCode, '
                'input[data-automation-id="countryPhoneCode"]'
            )
            if not code_input or not await code_input.is_visible():
                return False

            # Click and type to search
            await code_input.click()
            await self.browser_manager.human_delay(200, 400)
            await code_input.fill("")
            await code_input.type("United States", delay=50)
            await self.browser_manager.human_delay(1000, 2000)

            # Look for suggestion popup
            suggestion_selectors = [
                '[data-automation-id="promptOption"]',
                '[data-automation-id="menuItem"]',
                '[role="option"]',
                'li[role="presentation"]',
            ]
            for sel in suggestion_selectors:
                items = await page.query_selector_all(sel)
                if items:
                    for item in items:
                        text = (await item.text_content() or "").strip()
                        if "united states" in text.lower() and "+1" in text:
                            await item.click()
                            logger.debug(f"Selected country phone code: {text}")
                            await self.browser_manager.human_delay(300, 500)
                            return True
                    # Select first if no exact match
                    if items:
                        text = (await items[0].text_content() or "").strip()
                        await items[0].click()
                        logger.debug(f"Selected first country phone code option: {text}")
                        return True

            # Escape if no options appeared
            await page.keyboard.press("Escape")
            return False

        except Exception as e:
            logger.debug(f"Country phone code fill failed: {e}")
            return False

    async def _fill_workday_dropdown(self, page: Page, selector: str, value: str) -> bool:
        """Fill a Workday custom dropdown."""
        try:
            dropdown = await page.query_selector(selector)
            if not dropdown or not await dropdown.is_visible():
                return False

            await dropdown.click()
            await self.browser_manager.human_delay(500, 800)

            # Wait for menu
            try:
                await page.wait_for_selector('[data-automation-id="menuItem"]', timeout=3000)
            except Exception:
                await page.keyboard.press("Escape")
                return False

            # Try exact match first
            option = await page.query_selector(f'div[data-automation-id="menuItem"]:has-text("{value}")')
            if option:
                await option.click()
                await self.browser_manager.human_delay(200, 400)
                return True

            # Try partial match
            menu_items = await page.query_selector_all('[data-automation-id="menuItem"]')
            for item in menu_items:
                text = await item.text_content()
                if text and value.lower() in text.lower():
                    await item.click()
                    await self.browser_manager.human_delay(200, 400)
                    return True

            await page.keyboard.press("Escape")
            return False

        except Exception as e:
            logger.debug(f"Could not fill dropdown {selector}: {e}")
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            return False

    async def _fill_workday_typeahead(self, page: Page, selector: str, value: str) -> bool:
        """Fill a Workday typeahead/autocomplete field (school, degree, etc.).

        These fields require typing text and then selecting from a popup list.
        """
        try:
            elem = await page.query_selector(selector)
            if not elem or not await elem.is_visible():
                return False

            # Clear and type to trigger autocomplete
            await elem.click()
            await elem.fill("")
            await self.browser_manager.human_delay(200, 400)

            # Type the search term slowly
            search_term = value[:30]  # Workday typeahead searches on partial text
            await elem.type(search_term, delay=80)
            await self.browser_manager.human_delay(1000, 2000)

            # Wait for suggestions popup
            suggestion_selectors = [
                '[data-automation-id="promptOption"]',
                '[data-automation-id="menuItem"]',
                '[role="option"]',
                'li[role="presentation"]',
                '.css-1rc7hqf',  # Workday suggestion list item
            ]

            for sel in suggestion_selectors:
                items = await page.query_selector_all(sel)
                if items:
                    # Find best match
                    for item in items:
                        text = (await item.text_content() or "").strip()
                        if value.lower() in text.lower():
                            await item.click()
                            logger.debug(f"Selected typeahead: '{text}' for '{value}'")
                            await self.browser_manager.human_delay(300, 500)
                            return True

                    # If no exact match, click first option
                    if items:
                        first_text = (await items[0].text_content() or "").strip()
                        if first_text:
                            await items[0].click()
                            logger.debug(f"Selected first typeahead option: '{first_text}'")
                            await self.browser_manager.human_delay(300, 500)
                            return True

            # No suggestions appeared — the value stays as typed text
            logger.debug(f"No typeahead suggestions for '{value}' — leaving as typed text")
            return True

        except Exception as e:
            logger.debug(f"Typeahead fill failed for {selector}: {e}")
            return False

    async def _fill_generic_workday_dropdowns(self, page: Page, config: Dict[str, Any]) -> None:
        """Fill generic Workday dropdowns based on question text."""
        screening = config.get("screening", {})
        work_auth = config.get("work_authorization", {})
        demographics = config.get("demographics", {})

        # Find all dropdown buttons
        dropdown_buttons = await page.query_selector_all(
            'button[data-automation-id="dateDropdown"], '
            'button[aria-haspopup="listbox"], '
            '[data-automation-id*="dropdown"]'
        )

        for btn in dropdown_buttons:
            try:
                parent = await btn.evaluate_handle(
                    "el => el.closest('[data-automation-id^=\"formField\"]') || el.parentElement"
                )
                label_elem = await parent.query_selector('label')
                if not label_elem:
                    continue

                label_text = (await label_elem.text_content() or "").lower()

                value = None
                if "sponsor" in label_text or "visa" in label_text:
                    needs = work_auth.get("require_sponsorship_now", False) or work_auth.get("require_sponsorship_future", False)
                    value = "Yes" if needs else "No"
                elif "authorized" in label_text or "eligible" in label_text or "right to work" in label_text:
                    value = "Yes" if work_auth.get("us_work_authorized", True) else "No"
                elif "18" in label_text or "legal age" in label_text:
                    value = "Yes"
                elif "relocate" in label_text:
                    value = "Yes" if screening.get("willing_to_relocate", True) else "No"
                elif "gender" in label_text:
                    value = demographics.get("gender", "Male")
                elif "race" in label_text or "ethnicity" in label_text:
                    value = demographics.get("ethnicity", "Asian")
                elif "veteran" in label_text:
                    value = demographics.get("veteran_status", "I am not a protected veteran")
                elif "disab" in label_text:
                    value = demographics.get("disability_status", "I do not wish to answer")
                elif "citizen" in label_text:
                    value = "Yes" if work_auth.get("us_citizen", True) else "No"
                elif "how did you hear" in label_text or "hear about" in label_text or "source" in label_text:
                    value = "Job Board"
                elif "previously" in label_text and "employed" in label_text:
                    value = "No"
                elif "background check" in label_text:
                    value = "Yes"
                elif "drug" in label_text and "test" in label_text:
                    value = "Yes"
                elif "commut" in label_text or "on-site" in label_text or "in.?office" in label_text:
                    value = "Yes"

                if value:
                    await btn.click()
                    await self.browser_manager.human_delay(300, 500)

                    option = await page.query_selector(f'div[data-automation-id="menuItem"]:has-text("{value}")')
                    if option:
                        await option.click()
                        self._fields_filled[label_text[:40]] = value
                        await self.browser_manager.human_delay(200, 300)
                    else:
                        # Partial match
                        items = await page.query_selector_all('[data-automation-id="menuItem"]')
                        matched = False
                        for item in items:
                            text = (await item.text_content() or "").strip()
                            if value.lower() in text.lower():
                                await item.click()
                                self._fields_filled[label_text[:40]] = text
                                matched = True
                                break
                        if not matched:
                            await page.keyboard.press("Escape")

            except Exception as e:
                logger.debug(f"Error handling Workday dropdown: {e}")
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass

    async def _fill_radio_buttons(self, page: Page, config: Dict[str, Any]) -> None:
        """Fill radio button groups on Workday forms.

        Workday radio groups use various patterns:
        - input[type="radio"] (standard)
        - [role="radio"] (ARIA)
        - [data-automation-id="radioBtn"] (Workday custom)
        The question label is in a parent formField container.
        """
        work_auth = config.get("work_authorization", {})
        screening = config.get("screening", {})

        # Use JS to find radio groups — more reliable than Playwright selectors
        # because it can traverse the DOM and find radios inside any ancestor
        radio_groups_data = await page.evaluate('''
            () => {
                const results = [];
                const containers = document.querySelectorAll('[data-automation-id^="formField"]');
                for (const c of containers) {
                    if (c.offsetParent === null) continue;
                    const radios = c.querySelectorAll(
                        'input[type="radio"], [role="radio"], [data-automation-id="radioBtn"]'
                    );
                    if (radios.length === 0) continue;
                    const label = c.querySelector('label, [data-automation-id="formLabel"]');
                    const labelText = label?.textContent?.trim() || '';
                    if (!labelText) continue;
                    // Check if any radio is already checked
                    let anyChecked = false;
                    const radioInfos = [];
                    for (const r of radios) {
                        const checked = r.checked || r.getAttribute('aria-checked') === 'true';
                        if (checked) anyChecked = true;
                        // Get radio label text — try multiple strategies
                        // 1. The <label> wrapping the radio (li > label > input)
                        const wrappingLabel = r.closest('label');
                        // 2. Parent element text
                        const parent = r.parentElement;
                        // 3. Next sibling text
                        const nextSib = r.nextElementSibling || r.nextSibling;
                        // 4. The radio value attribute (true/false or Yes/No)
                        const val = r.value || r.getAttribute('aria-label') || '';
                        // Combine all text sources
                        let text = '';
                        if (wrappingLabel) {
                            // Get only direct text, not the full question label
                            const clone = wrappingLabel.cloneNode(true);
                            // Remove nested labels to avoid picking up the question text
                            clone.querySelectorAll('label').forEach(l => l.remove());
                            text = clone.textContent?.trim() || '';
                        }
                        if (!text && parent) {
                            text = parent.textContent?.trim() || '';
                        }
                        if (!text && nextSib) {
                            text = nextSib.textContent?.trim() || '';
                        }
                        radioInfos.push({
                            text: text,
                            value: val,
                            checked: checked
                        });
                    }
                    if (anyChecked) continue;
                    results.push({
                        containerId: c.getAttribute('data-automation-id'),
                        label: labelText,
                        options: radioInfos
                    });
                }
                return results;
            }
        ''')

        if radio_groups_data:
            logger.info(f"Found {len(radio_groups_data)} unfilled radio groups")

        for group_data in radio_groups_data:
            try:
                label_text = group_data["label"].lower()
                container_id = group_data["containerId"]
                options = group_data["options"]

                logger.debug(f"Radio group '{label_text[:60]}' in {container_id}: options={[(o['text'], o.get('value','')) for o in options]}")

                # Determine the answer
                answer = None
                if "previously" in label_text and "employed" in label_text:
                    answer = "no"
                elif "sponsor" in label_text or "visa" in label_text:
                    needs = work_auth.get("require_sponsorship_now", False) or work_auth.get("require_sponsorship_future", False)
                    answer = "yes" if needs else "no"
                elif "authorized" in label_text or "eligible" in label_text or "right to work" in label_text:
                    answer = "yes" if work_auth.get("us_work_authorized", True) else "no"
                elif "18" in label_text or "legal age" in label_text:
                    answer = "yes"
                elif "relocate" in label_text:
                    answer = "yes" if screening.get("willing_to_relocate", True) else "no"
                elif "background check" in label_text or "drug" in label_text:
                    answer = "yes"
                elif "commut" in label_text or "on-site" in label_text:
                    answer = "yes"

                if not answer:
                    opt_texts = [o["text"] for o in options]
                    ai_ans = await self.ai_answerer.answer_question(label_text, "radio", options=opt_texts)
                    if ai_ans:
                        answer = ai_ans.lower()

                if not answer:
                    logger.debug(f"No answer for radio group: {label_text[:60]}")
                    continue

                # Map yes/no to value attributes (Workday uses true/false)
                value_map = {"yes": "true", "no": "false"}
                target_value = value_map.get(answer, answer)

                # Click the matching radio via JS — find the container, then the matching radio
                clicked = await page.evaluate('''
                    (args) => {
                        const [containerId, answer, targetValue] = args;
                        const container = document.querySelector(
                            `[data-automation-id="${containerId}"]`
                        );
                        if (!container) return false;
                        const radios = container.querySelectorAll(
                            'input[type="radio"], [role="radio"], [data-automation-id="radioBtn"]'
                        );
                        for (const r of radios) {
                            // Match by value attribute (true/false), text content, or aria-label
                            const val = (r.value || '').toLowerCase();
                            const ariaLabel = (r.getAttribute('aria-label') || '').toLowerCase();
                            const parentEl = r.closest('label') || r.parentElement;
                            const text = (parentEl?.textContent || '').trim().toLowerCase();
                            const nextSib = r.nextElementSibling || r.nextSibling;
                            const sibText = (nextSib?.textContent || '').trim().toLowerCase();

                            if (val === targetValue || val === answer ||
                                text.includes(answer) || sibText.includes(answer) ||
                                ariaLabel.includes(answer)) {
                                r.click();
                                // For input[type="radio"], also set checked and dispatch events
                                if (r.type === 'radio') {
                                    r.checked = true;
                                    r.dispatchEvent(new Event('change', {bubbles: true}));
                                    r.dispatchEvent(new Event('input', {bubbles: true}));
                                }
                                return true;
                            }
                        }
                        return false;
                    }
                ''', [container_id, answer, target_value])

                if clicked:
                    self._fields_filled[label_text[:40]] = answer
                    logger.info(f"Filled radio '{label_text[:60]}' → {answer}")
                    await self.browser_manager.human_delay(200, 400)
                else:
                    # Fallback: try Playwright click
                    container = await page.query_selector(f'[data-automation-id="{container_id}"]')
                    if container:
                        radios = await container.query_selector_all(
                            'input[type="radio"], [role="radio"], [data-automation-id="radioBtn"]'
                        )
                        for r in radios:
                            r_label = await r.evaluate(
                                "el => (el.closest('label') || el.parentElement).textContent"
                            )
                            if r_label and answer in r_label.lower():
                                try:
                                    await r.click(force=True)
                                except Exception:
                                    await r.evaluate("el => el.click()")
                                self._fields_filled[label_text[:40]] = answer
                                logger.info(f"Filled radio (fallback) '{label_text[:60]}' → {answer}")
                                await self.browser_manager.human_delay(200, 400)
                                break

            except Exception as e:
                logger.debug(f"Error handling radio group: {e}")

    async def _fill_checkboxes(self, page: Page, config: Dict[str, Any]) -> None:
        """Fill checkbox fields."""
        checkbox_mappings = [
            ('[data-automation-id="agreementCheckbox"]', True),
            ('[data-automation-id="termsCheckbox"]', True),
        ]

        for selector, value in checkbox_mappings:
            try:
                elem = await page.query_selector(selector)
                if elem and await elem.is_visible():
                    is_checked = await elem.is_checked()
                    if value and not is_checked:
                        await elem.click()
                    elif not value and is_checked:
                        await elem.click()
            except Exception as e:
                logger.debug(f"Error handling checkbox {selector}: {e}")

        # Check all visible unchecked checkboxes — consent/SMS/terms
        try:
            all_checkboxes = await page.query_selector_all(
                'input[type="checkbox"], [role="checkbox"]'
            )
            for cb in all_checkboxes:
                try:
                    if not await cb.is_visible():
                        continue
                    checked = await cb.evaluate(
                        "el => el.checked || el.getAttribute('aria-checked') === 'true'"
                    )
                    if checked:
                        continue
                    # Get context — only check consent/agreement/terms checkboxes
                    parent_text = await cb.evaluate(
                        "el => (el.closest('label') || el.closest('div') || el.parentElement).textContent?.substring(0, 200) || ''"
                    )
                    pt = parent_text.lower()
                    if any(kw in pt for kw in ["consent", "agree", "terms", "acknowledge", "sms", "text message"]):
                        await cb.click(force=True)
                        logger.debug(f"Checked consent checkbox: '{parent_text[:50]}'")
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"Error checking consent checkboxes: {e}")

    async def _upload_resume_if_needed(self, page: Page, config: Dict[str, Any]) -> None:
        """Upload resume if on resume upload page."""
        resume_path = config.get("files", {}).get("resume")
        if not resume_path:
            return

        try:
            # Check if resume already uploaded (look for "Successfully Uploaded" text or filename)
            already_uploaded = await page.evaluate('''
                () => {
                    const text = document.body.innerText;
                    return text.includes('Successfully Uploaded') ||
                           text.includes('resume.pdf') ||
                           text.includes('Resume.pdf');
                }
            ''')
            if already_uploaded:
                logger.debug("Resume appears already uploaded")
                return

            uploaded_indicator = await page.query_selector(
                '[data-automation-id*="file-upload"] .css-1lry38b, '
                '[data-automation-id*="filename"]'
            )
            if uploaded_indicator:
                logger.debug("Resume appears already uploaded (indicator found)")
                return

            # Workday file input
            file_input = await page.query_selector(
                'input[data-automation-id="file-upload-input-ref"], '
                'input[type="file"][data-automation-id*="resume"], '
                'input[type="file"]'
            )

            if file_input:
                await file_input.set_input_files(resume_path)
                logger.info("Resume uploaded to Workday")
                self._fields_filled["resume"] = resume_path
                await self.browser_manager.human_delay(2000, 3000)
                return

            # Try button upload
            upload_btn = await page.query_selector(
                'button[data-automation-id="file-upload-button"], '
                'button:has-text("Select Files"), '
                'button:has-text("Upload")'
            )
            if upload_btn:
                async with page.expect_file_chooser() as fc_info:
                    await upload_btn.click()
                file_chooser = await fc_info.value
                await file_chooser.set_files(resume_path)
                logger.info("Resume uploaded via Workday button")
                self._fields_filled["resume"] = resume_path

        except Exception as e:
            logger.debug(f"Could not upload resume to Workday: {e}")

    async def _fill_work_experience(self, page: Page, config: Dict[str, Any]) -> None:
        """Fill work experience section."""
        experience = config.get("experience", [])
        if not experience:
            return

        exp = experience[0]  # Most recent

        try:
            title_input = await page.query_selector('[data-automation-id="jobTitle"]')
            if title_input and await title_input.is_visible():
                await title_input.fill(exp.get("title", ""))
                self._fields_filled["jobTitle"] = exp.get("title", "")

            company_input = await page.query_selector('[data-automation-id="company"]')
            if company_input and await company_input.is_visible():
                await company_input.fill(exp.get("company", ""))

            location_input = await page.query_selector('[data-automation-id="location"]')
            if location_input and await location_input.is_visible():
                await location_input.fill(exp.get("location", ""))

        except Exception as e:
            logger.debug(f"Could not fill work experience: {e}")

    async def _fill_education(self, page: Page, config: Dict[str, Any]) -> None:
        """Fill education section — handles typeahead dropdowns for school/degree."""
        education = config.get("education", [])
        if not education:
            return

        edu = education[0]

        try:
            # School — Workday uses typeahead autocomplete
            school = edu.get("school", "")
            if school:
                # Try typeahead first (most Workday sites use this)
                filled = await self._fill_workday_typeahead(
                    page, '[data-automation-id="school"]', school
                )
                if not filled:
                    # Fallback to regular text input
                    school_input = await page.query_selector('[data-automation-id="school"]')
                    if school_input and await school_input.is_visible():
                        await school_input.fill(school)
                if filled:
                    self._fields_filled["school"] = school

            # Degree — also typeahead
            degree = edu.get("degree", "")
            if degree:
                filled = await self._fill_workday_typeahead(
                    page, '[data-automation-id="degree"]', degree
                )
                if not filled:
                    degree_input = await page.query_selector('[data-automation-id="degree"]')
                    if degree_input and await degree_input.is_visible():
                        await degree_input.fill(degree)
                if filled:
                    self._fields_filled["degree"] = degree

            # Field of study — typeahead
            field_of_study = edu.get("field_of_study", "")
            if field_of_study:
                filled = await self._fill_workday_typeahead(
                    page, '[data-automation-id="fieldOfStudy"]', field_of_study
                )
                if not filled:
                    fos_input = await page.query_selector('[data-automation-id="fieldOfStudy"]')
                    if fos_input and await fos_input.is_visible():
                        await fos_input.fill(field_of_study)

            # GPA
            gpa = edu.get("gpa", "")
            if gpa:
                gpa_input = await page.query_selector('[data-automation-id="gpa"]')
                if gpa_input and await gpa_input.is_visible():
                    await gpa_input.fill(str(gpa))

            # Education dates — try date fields
            start_date = edu.get("start_date", "")
            grad_date = edu.get("graduation_date", "")

            if start_date:
                # Workday date format: MM/YYYY or MM/DD/YYYY
                start_formatted = self._format_date(start_date)
                start_input = await page.query_selector(
                    '[data-automation-id="dateSectionMonth-input"]:first-of-type, '
                    '[data-automation-id="startDate"], '
                    'input[data-automation-id*="start"][data-automation-id*="date" i]'
                )
                if start_input and await start_input.is_visible():
                    await start_input.fill(start_formatted)

            if grad_date:
                grad_formatted = self._format_date(grad_date)
                end_input = await page.query_selector(
                    '[data-automation-id="endDate"], '
                    'input[data-automation-id*="end"][data-automation-id*="date" i]'
                )
                if end_input and await end_input.is_visible():
                    await end_input.fill(grad_formatted)

        except Exception as e:
            logger.debug(f"Could not fill education: {e}")

    _MONTHS = {
        "january": "01", "february": "02", "march": "03", "april": "04",
        "may": "05", "june": "06", "july": "07", "august": "08",
        "september": "09", "october": "10", "november": "11", "december": "12",
    }

    def _format_date(self, date_str: str) -> str:
        """Convert 'August 2021' or 'May 2026' to MM/YYYY format."""
        parts = date_str.strip().split()
        if len(parts) == 2:
            month_name = parts[0].lower()
            year = parts[1]
            month_num = self._MONTHS.get(month_name, "01")
            return f"{month_num}/{year}"
        return date_str

    def _format_date_for_workday(self, date_str: str) -> str:
        """Convert various date formats to MM/DD/YYYY for Workday date inputs.

        Handles: 'May 2026', '05/2026', '05/19/2026', 'May 19, 2026', etc.
        """
        s = date_str.strip()

        # Already MM/DD/YYYY
        m = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{4})$', s)
        if m:
            return f"{int(m.group(1)):02d}/{int(m.group(2)):02d}/{m.group(3)}"

        # MM/YYYY → MM/01/YYYY
        m = re.match(r'^(\d{1,2})/(\d{4})$', s)
        if m:
            return f"{int(m.group(1)):02d}/01/{m.group(2)}"

        # "May 2026" → 05/19/2026 (use 19th as default day)
        parts = s.split()
        if len(parts) == 2:
            month_num = self._MONTHS.get(parts[0].lower())
            if month_num and re.match(r'\d{4}$', parts[1]):
                return f"{month_num}/19/{parts[1]}"

        # "May 19, 2026" or "May 19 2026"
        m = re.match(r'^(\w+)\s+(\d{1,2}),?\s+(\d{4})$', s)
        if m:
            month_num = self._MONTHS.get(m.group(1).lower(), "01")
            return f"{month_num}/{int(m.group(2)):02d}/{m.group(3)}"

        # Fallback: return as-is if already looks date-like, else use default
        if re.match(r'\d', s):
            return s
        return "05/19/2026"

    # Fields handled by dedicated methods — skip in _handle_custom_questions
    # Exact-ish labels for fields handled by dedicated fill methods — skip in _handle_custom_questions
    # These are checked with "any(kw in q_lower)" so be precise to avoid over-matching
    _SKIP_FIELD_KEYWORDS = {
        "phone number", "phone extension", "country phone code", "phone device type",
        "first name", "last name", "preferred name", "legal name",
        "email address", "address line 1",
        "postal code", "zip code",
        "facebook share", "twitter share", "linkedin share",
        "type to add skills",
        "upload a file", "drop files here",
    }

    async def _handle_custom_questions(self, page: Page, job_data: Dict[str, Any]) -> None:
        """Handle Workday custom questions (text, textarea, dropdowns, radio)."""
        questions = await page.query_selector_all(
            '[data-automation-id="questionItem"], '
            '.WJLB, '
            '[data-automation-id*="question"], '
            '[data-automation-id^="formField"]'
        )

        logger.debug(f"_handle_custom_questions: found {len(questions)} question containers")

        # If containers exist but labels haven't loaded, wait briefly
        if questions:
            first_label = None
            for attempt in range(3):
                first_label = await questions[0].query_selector(
                    'label, [data-automation-id="formLabel"], legend, [data-automation-id="richText"]'
                )
                if first_label:
                    break
                await asyncio.sleep(1)
            if not first_label:
                logger.debug("No labels found in question containers after waiting")

        seen_labels = set()
        handled_count = 0
        for q in questions:
            try:
                # Workday uses different label patterns:
                # 1. <label> (My Information page)
                # 2. <legend> > <div> > <div data-automation-id="richText"> (Application Questions page)
                # 3. [data-automation-id="formLabel"]
                label_el = await q.query_selector(
                    'label, [data-automation-id="formLabel"], legend, [data-automation-id="richText"]'
                )
                if not label_el:
                    continue

                question_text = (await label_el.text_content() or "").strip()
                if not question_text or question_text.lower() in seen_labels:
                    continue
                seen_labels.add(question_text.lower())

                # Skip fields that are handled by dedicated fill methods
                q_lower = question_text.lower()
                if any(kw in q_lower for kw in self._SKIP_FIELD_KEYWORDS):
                    logger.debug(f"Skipping known field: '{question_text[:50]}'")
                    continue

                # --- Check for dropdown FIRST (Workday dropdowns have both buttons and inputs) ---
                # EXCLUDE dateDropdown — date pickers are handled separately below
                dropdown_btn = await q.query_selector(
                    'button[aria-haspopup="listbox"]:not([data-automation-id="dateDropdown"]), '
                    '[data-automation-id*="dropdown"]:not([data-automation-id="dateDropdown"])'
                )
                if dropdown_btn and await dropdown_btn.is_visible():
                    btn_text = (await dropdown_btn.text_content() or "").strip()
                    if btn_text and btn_text.lower() not in ("select one", "select", "choose", "--select--", ""):
                        continue  # Already has a selection

                    # Open dropdown FIRST to read actual menu items
                    await dropdown_btn.click()
                    await self.browser_manager.human_delay(400, 700)

                    # Scroll the dropdown list to load all items (Workday lazy-loads)
                    await page.evaluate('''
                        () => {
                            const listbox = document.querySelector('[role="listbox"], [data-automation-id="menuItemList"]');
                            if (listbox) {
                                listbox.scrollTop = listbox.scrollHeight;
                            }
                        }
                    ''')
                    await self.browser_manager.human_delay(300, 500)
                    await page.evaluate('''
                        () => {
                            const listbox = document.querySelector('[role="listbox"], [data-automation-id="menuItemList"]');
                            if (listbox) {
                                listbox.scrollTop = 0;
                            }
                        }
                    ''')
                    await self.browser_manager.human_delay(200, 300)

                    items = await page.query_selector_all('[data-automation-id="menuItem"], [role="option"]')
                    item_texts = []
                    for item in items:
                        text = (await item.text_content() or "").strip()
                        if text:
                            item_texts.append(text)

                    logger.debug(f"Dropdown '{question_text[:40]}' options: {item_texts[:10]}")

                    # If no items found, wait a bit more and try again
                    if not item_texts:
                        await self.browser_manager.human_delay(500, 800)
                        items = await page.query_selector_all('[data-automation-id="menuItem"], [role="option"]')
                        for item in items:
                            text = (await item.text_content() or "").strip()
                            if text:
                                item_texts.append(text)

                    # Close dropdown before calling AI (may take time)
                    await page.keyboard.press("Escape")
                    await self.browser_manager.human_delay(200, 300)

                    # If no items at all, this might not be a real dropdown — fall through to text input
                    if not item_texts:
                        logger.debug(f"Dropdown '{question_text[:40]}' had no items, falling through to text input")
                        pass  # Fall through to date/text input handlers below
                    else:
                        # Pass actual menu options to answer_question so _match_option_from_config works
                        answer = await self.ai_answerer.answer_question(
                            question_text, "dropdown", options=item_texts, max_length=100
                        )

                        if answer:
                            # Re-open dropdown to select
                            await dropdown_btn.click()
                            await self.browser_manager.human_delay(400, 700)

                            # Scroll to load all items again
                            await page.evaluate('''
                                () => {
                                    const listbox = document.querySelector('[role="listbox"], [data-automation-id="menuItemList"]');
                                    if (listbox) { listbox.scrollTop = listbox.scrollHeight; }
                                }
                            ''')
                            await self.browser_manager.human_delay(200, 300)
                            await page.evaluate('''
                                () => {
                                    const listbox = document.querySelector('[role="listbox"], [data-automation-id="menuItemList"]');
                                    if (listbox) { listbox.scrollTop = 0; }
                                }
                            ''')
                            await self.browser_manager.human_delay(200, 300)

                            items = await page.query_selector_all('[data-automation-id="menuItem"], [role="option"]')
                            matched = False

                            # Strategy 1: Exact text match
                            for item in items:
                                text = (await item.text_content() or "").strip()
                                if text.lower() == answer.lower():
                                    await item.click()
                                    self._fields_filled[question_text[:40]] = text[:30]
                                    logger.info(f"Custom Q dropdown: '{question_text[:50]}' → '{text[:30]}'")
                                    matched = True
                                    break

                            # Strategy 2: Partial match (answer in option text or vice versa)
                            if not matched:
                                for item in items:
                                    text = (await item.text_content() or "").strip()
                                    if answer.lower() in text.lower() or text.lower() in answer.lower():
                                        await item.click()
                                        self._fields_filled[question_text[:40]] = text[:30]
                                        logger.info(f"Custom Q dropdown (partial): '{question_text[:50]}' → '{text[:30]}'")
                                        matched = True
                                        break

                            # Strategy 3: Word overlap matching (for "Yes" → "Yes, I agree")
                            if not matched:
                                answer_words = set(answer.lower().split())
                                best_item = None
                                best_score = 0
                                for item in items:
                                    text = (await item.text_content() or "").strip()
                                    text_words = set(text.lower().split())
                                    overlap = len(answer_words & text_words)
                                    if overlap > best_score:
                                        best_score = overlap
                                        best_item = item

                                if best_item and best_score > 0:
                                    text = (await best_item.text_content() or "").strip()
                                    await best_item.click()
                                    self._fields_filled[question_text[:40]] = text[:30]
                                    logger.info(f"Custom Q dropdown (word match): '{question_text[:50]}' → '{text[:30]}'")
                                    matched = True

                            if not matched:
                                # Last resort — close dropdown
                                await page.keyboard.press("Escape")
                                logger.warning(f"Dropdown no match: '{question_text[:50]}' answer='{answer}' options={item_texts[:5]}")

                            await self.browser_manager.human_delay(200, 400)
                        continue

                # --- Date input fields (detect by data-automation-id or question text) ---
                date_input = await q.query_selector(
                    'input[data-automation-id*="date" i], '
                    'input[data-automation-id*="Date"]'
                )
                is_date_question = any(x in q_lower for x in ["date available", "date of", "start date", "end date"])
                if not date_input and is_date_question:
                    date_input = await q.query_selector('input:not([type="radio"]):not([type="checkbox"]):not([type="file"]):not([placeholder="Search"])')
                if date_input and is_date_question:
                    is_visible = await date_input.is_visible()
                    logger.debug(f"Date input found for '{question_text[:40]}': visible={is_visible}")
                if date_input and await date_input.is_visible() and is_date_question:
                    current = (await date_input.input_value() or "").strip()
                    logger.debug(f"Date field value: '{current}' for '{question_text[:40]}'")
                    # Check if current value looks invalid or empty
                    year_match = re.search(r'(\d{4})', current)
                    bad_year = year_match and int(year_match.group(1)) < 2025 if year_match else False
                    is_invalid = not current or "/00/" in current or len(current) < 6 or bad_year or "DD" in current
                    if is_invalid:
                        answer = await self.ai_answerer.answer_question(
                            question_text, "text", max_length=20
                        )
                        if answer:
                            formatted = self._format_date_for_workday(answer)
                            logger.debug(f"Date answer: '{answer}' → formatted: '{formatted}'")

                            # Strategy 1: Use Playwright fill()
                            await date_input.fill(formatted)
                            await self.browser_manager.human_delay(200, 300)
                            new_val = (await date_input.input_value() or "").strip()
                            logger.debug(f"Date after fill(): '{new_val}'")

                            if not new_val or len(new_val) < 8 or "DD" in new_val:
                                # Strategy 2: Click, clear, type with full format
                                await date_input.click(click_count=3)
                                await self.browser_manager.human_delay(100, 200)
                                await page.keyboard.type(formatted, delay=80)
                                await self.browser_manager.human_delay(200, 300)
                                new_val = (await date_input.input_value() or "").strip()
                                logger.debug(f"Date after type(formatted): '{new_val}'")

                            if not new_val or len(new_val) < 8 or "DD" in new_val:
                                # Strategy 3: JS nativeInputValueSetter (React-compatible)
                                await date_input.evaluate(
                                    '''(el) => {
                                        const nativeSetter = Object.getOwnPropertyDescriptor(
                                            window.HTMLInputElement.prototype, 'value'
                                        ).set;
                                        nativeSetter.call(el, "''' + formatted + '''");
                                        el.dispatchEvent(new Event('input', {bubbles: true}));
                                        el.dispatchEvent(new Event('change', {bubbles: true}));
                                    }'''
                                )
                                await self.browser_manager.human_delay(200, 300)
                                new_val = (await date_input.input_value() or "").strip()
                                logger.debug(f"Date after nativeSetter: '{new_val}'")

                            # Tab out to trigger validation
                            await page.keyboard.press("Tab")
                            await self.browser_manager.human_delay(100, 200)
                            final_val = (await date_input.input_value() or "").strip()
                            self._fields_filled[question_text[:40]] = final_val or formatted
                            logger.info(f"Custom Q date: '{question_text[:50]}' → '{final_val or formatted}'")
                        await self.browser_manager.human_delay(200, 400)
                    continue

                # --- Text / textarea inputs ---
                input_elem = await q.query_selector('input:not([type="radio"]):not([type="checkbox"]):not([type="file"]):not([placeholder="Search"]), textarea')
                if input_elem and await input_elem.is_visible():
                    tag = await input_elem.evaluate("el => el.tagName.toLowerCase()")
                    current = await input_elem.input_value() or ""
                    if current.strip():
                        continue  # Already filled

                    max_len = 500 if tag == "textarea" else 200
                    answer = await self.ai_answerer.answer_question(
                        question_text, tag, max_length=max_len
                    )
                    if answer:
                        await input_elem.fill(answer)
                        self._fields_filled[question_text[:40]] = answer[:30]
                    await self.browser_manager.human_delay(200, 400)
                    continue

                # --- Radio buttons (already handled by _fill_radio_buttons mostly, but catch stragglers) ---
                radios = await q.query_selector_all('[data-automation-id="radioBtn"], input[type="radio"], [role="radio"]')
                if radios:
                    already_answered = False
                    for r in radios:
                        checked = await r.evaluate("el => el.checked || el.getAttribute('aria-checked') === 'true'")
                        if checked:
                            already_answered = True
                            break
                    if already_answered:
                        continue

                    # Get options text
                    options = []
                    for r in radios:
                        r_text = await r.evaluate(
                            "el => (el.closest('label') || el.parentElement).textContent"
                        )
                        options.append((r_text or "").strip())

                    answer = await self.ai_answerer.answer_question(
                        question_text, "radio", options=options
                    )
                    if answer:
                        for i, r in enumerate(radios):
                            if answer.lower() in (options[i] or "").lower():
                                try:
                                    await r.click(force=True)
                                except Exception:
                                    await r.evaluate("el => el.click()")
                                self._fields_filled[question_text[:40]] = answer[:30]
                                break
                    await self.browser_manager.human_delay(200, 400)
                    continue

                # No matching input type found for this container
                logger.debug(f"No input/dropdown/radio found for: '{question_text[:60]}'")

            except Exception as e:
                logger.debug(f"Error handling Workday question: {e}")

        if handled_count > 0:
            logger.info(f"Handled {handled_count} custom questions")

        # Second pass — catch dynamically-appearing fields (e.g. "Desired Hourly Rate"
        # only appears after "Hourly" is selected in the position type dropdown)
        await self.browser_manager.human_delay(500, 800)
        new_questions = await page.query_selector_all('[data-automation-id^="formField"]')
        for q2 in new_questions:
            try:
                label_el = await q2.query_selector(
                    'label, [data-automation-id="formLabel"], legend, [data-automation-id="richText"]'
                )
                if not label_el:
                    continue
                question_text = (await label_el.text_content() or "").strip()
                if not question_text or question_text.lower() in seen_labels:
                    continue
                q_lower = question_text.lower()
                if any(kw in q_lower for kw in self._SKIP_FIELD_KEYWORDS):
                    continue

                # Check dropdown
                dropdown_btn = await q2.query_selector('button[aria-haspopup="listbox"]')
                if dropdown_btn and await dropdown_btn.is_visible():
                    btn_text = (await dropdown_btn.text_content() or "").strip()
                    if btn_text and btn_text.lower() not in ("select one", "select", "choose", "--select--", ""):
                        continue

                    await dropdown_btn.click()
                    await self.browser_manager.human_delay(500, 800)
                    items = await page.query_selector_all('[data-automation-id="menuItem"], [role="option"]')
                    item_texts = []
                    for item in items:
                        text = (await item.text_content() or "").strip()
                        if text:
                            item_texts.append(text)

                    if not item_texts:
                        await page.keyboard.press("Escape")
                        continue

                    await page.keyboard.press("Escape")
                    await self.browser_manager.human_delay(200, 300)

                    answer = await self.ai_answerer.answer_question(
                        question_text, "dropdown", options=item_texts, max_length=100
                    )
                    if answer:
                        await dropdown_btn.click()
                        await self.browser_manager.human_delay(400, 700)
                        items = await page.query_selector_all('[data-automation-id="menuItem"], [role="option"]')
                        for item in items:
                            text = (await item.text_content() or "").strip()
                            if text.lower() == answer.lower() or answer.lower() in text.lower():
                                await item.click()
                                self._fields_filled[question_text[:40]] = text[:30]
                                logger.info(f"Custom Q dropdown (2nd pass): '{question_text[:50]}' → '{text[:30]}'")
                                break
                        else:
                            await page.keyboard.press("Escape")
                    continue

                # Check text input
                input_elem = await q2.query_selector('input:not([type="radio"]):not([type="checkbox"]):not([type="file"]):not([placeholder="Search"]), textarea')
                if input_elem and await input_elem.is_visible():
                    current = await input_elem.input_value() or ""
                    if current.strip():
                        continue
                    tag = await input_elem.evaluate("el => el.tagName.toLowerCase()")
                    max_len = 500 if tag == "textarea" else 200
                    answer = await self.ai_answerer.answer_question(question_text, tag, max_length=max_len)
                    if answer:
                        await input_elem.fill(answer)
                        self._fields_filled[question_text[:40]] = answer[:30]
                        logger.info(f"Custom Q text (2nd pass): '{question_text[:50]}' → '{answer[:30]}'")

            except Exception as e:
                logger.debug(f"Error in 2nd pass: {e}")

    async def _handle_validation_errors(self, page: Page, job_data: Dict[str, Any]) -> None:
        """Try to fix validation errors by re-filling missing fields."""
        errors = await page.query_selector_all('[data-automation-id="errorMessage"]')

        for error in errors:
            try:
                error_text = (await error.text_content() or "").strip()
                logger.warning(f"Workday validation error: {error_text}")

                # Find the parent form field
                parent = await error.evaluate_handle(
                    "el => el.closest('[data-automation-id^=\"formField\"]') || el.parentElement"
                )
                if parent:
                    input_elem = await parent.query_selector('input, textarea, select')
                    if input_elem and await input_elem.is_visible():
                        label_elem = await parent.query_selector('label')
                        label_text = (await label_elem.text_content() or "") if label_elem else "Unknown"

                        tag = await input_elem.evaluate("el => el.tagName.toLowerCase()")
                        if tag in ("input", "textarea"):
                            answer = await self.ai_answerer.answer_question(
                                label_text, "text", max_length=200
                            )
                            if answer:
                                await input_elem.fill(answer)
            except Exception as e:
                logger.debug(f"Error fixing validation: {e}")

    async def _click_next_or_submit(self, page: Page) -> bool:
        """Click next or submit button using click_filter overlay."""
        # Strategy 1: Find the next/submit button by known data-automation-ids
        try:
            nav_btn = await page.query_selector(
                '[data-automation-id="bottom-navigation-next-button"], '
                '[data-automation-id="pageFooterNextButton"]'
            )
            if nav_btn and await nav_btn.is_visible():
                btn_text = (await nav_btn.text_content() or "").strip()
                logger.info(f"Found nav button: '{btn_text}'")
                if self.dry_run and "submit" in btn_text.lower():
                    logger.info("DRY RUN: Would click Submit")
                    return False
                # Try click_filter overlay in parent
                filter_clicked = await page.evaluate('''
                    () => {
                        const selectors = [
                            '[data-automation-id="bottom-navigation-next-button"]',
                            '[data-automation-id="pageFooterNextButton"]'
                        ];
                        for (const sel of selectors) {
                            const btn = document.querySelector(sel);
                            if (!btn) continue;
                            const parent = btn.parentElement;
                            if (!parent) continue;
                            const filter = parent.querySelector('[data-automation-id="click_filter"]');
                            if (filter) { filter.click(); return true; }
                        }
                        return false;
                    }
                ''')
                if filter_clicked:
                    logger.info(f"Clicked nav button via click_filter: '{btn_text}'")
                    return True
                # Fallback: force-click the button directly
                await nav_btn.click(force=True, timeout=5000)
                logger.info(f"Force-clicked nav button: '{btn_text}'")
                return True
        except Exception as e:
            logger.warning(f"Nav button click failed: {e}")

        # Strategy 2: Try click_filter labels
        for label in ["Submit", "Submit Application", "Next", "Continue", "Save and Continue"]:
            if self.dry_run and "Submit" in label:
                logger.info("DRY RUN: Would click Submit")
                return False
            if await self._click_workday_button(page, label):
                logger.debug(f"Clicked Workday button via click_filter: {label}")
                return True

        # Strategy 3: Fallback to force-clicking any visible button with matching text
        for text in ["Next", "Continue", "Save and Continue", "Submit"]:
            try:
                btn = await page.query_selector(f'button:has-text("{text}")')
                if btn and await btn.is_visible():
                    if self.dry_run and "Submit" in text:
                        logger.info("DRY RUN: Would click Submit")
                        return False
                    await btn.click(force=True, timeout=5000)
                    logger.debug(f"Force-clicked button: {text}")
                    return True
            except Exception:
                continue

        return False
