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
import random
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

    e.g. user@example.com + nvidia.wd5 -> user+nvidia-wd5@example.com
    """
    local, domain = base_email.split("@", 1)
    safe_tenant = tenant.replace(".", "-").replace("_", "-")
    return f"{local}+{safe_tenant}@{domain}"


class WorkdayHandler(BaseHandler):
    """Handler for Workday ATS applications."""

    name = "workday"

    _WORKDAY_PASSWORD_FALLBACK = "CHANGE_ME"

    @property
    def WORKDAY_PASSWORD(self):
        return self.form_filler.config.get("accounts", {}).get("workday_password", "") or self._WORKDAY_PASSWORD_FALLBACK

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

            # Let Simplify extension autofill boilerplate if loaded
            ext_filled = await self.wait_for_extension_autofill(page)
            if ext_filled:
                logger.info("Simplify extension pre-filled fields — handler will fill remaining gaps")

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

                    # If we're STILL on the auth page, try signing in with the account
                    still_auth2 = await self._detect_login_wall(page)
                    if still_auth2:
                        logger.info("Still on auth after re-navigate — trying signin")
                        accounts = _load_accounts()
                        base_email = self.form_filler.config.get("personal_info", {}).get("email", "")
                        alias_email = _make_alias_email(base_email, tenant)
                        signin_ok = await self._signin(page, alias_email)
                        if not signin_ok:
                            logger.warning("Signin after re-navigate also failed")
                            self._last_status = "login_required"
                            return False
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

                # Dry run: advance through pages but DON'T click Submit
                # (Submit detection handled in _click_next_or_submit)

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

            if self.dry_run:
                logger.info("DRY RUN: Reached end of wizard, taking screenshot")
                await self.take_screenshot(page, "workday_dry_run")
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
                return True
            # Both signin and create failed — try forgot password
            if self.email_verifier:
                logger.info("Signin + create both failed — trying forgot password")
                try:
                    await page.goto(job_url, wait_until="domcontentloaded", timeout=15000)
                    await self.browser_manager.human_delay(2000, 3000)
                    await self._click_apply_button(page)
                    await self.browser_manager.human_delay(2000, 3000)
                    await self._handle_start_application_modal(page)
                    await self.browser_manager.human_delay(3000, 5000)
                except Exception:
                    pass
                reset_ok = await self._forgot_password(page, alias_email, job_url)
                if reset_ok:
                    success = await self._signin(page, alias_email)
                    if success:
                        accounts[tenant] = {"email": alias_email, "password": self.WORKDAY_PASSWORD, "created": True}
                        _save_accounts(accounts)
                        return True
            return False
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
                return True
            # Both create and signin failed — try forgot password to reset
            if self.email_verifier:
                logger.info("Both create and signin failed — trying forgot password flow")
                try:
                    await page.goto(job_url, wait_until="domcontentloaded", timeout=15000)
                    await self.browser_manager.human_delay(2000, 3000)
                    await self._click_apply_button(page)
                    await self.browser_manager.human_delay(2000, 3000)
                    await self._handle_start_application_modal(page)
                    await self.browser_manager.human_delay(3000, 5000)
                except Exception:
                    pass
                reset_ok = await self._forgot_password(page, alias_email, job_url)
                if reset_ok:
                    # After password reset, try signing in with the new password
                    success = await self._signin(page, alias_email)
                    if success:
                        accounts[tenant] = {"email": alias_email, "password": self.WORKDAY_PASSWORD, "created": True}
                        _save_accounts(accounts)
                        return True
            return False

    async def _find_email_input(self, page: Page):
        """Find the email input on Workday auth pages — checks main page and iframes."""
        email_selectors = [
            'input[data-automation-id="email"]',
            'input[data-automation-id="signIn-email"]',
            'input[data-automation-id="createAccount-email"]',
            'input[data-automation-id="emailAddress"]',
            'input[data-automation-id="signInEmailAddress"]',
            'input[type="email"]',
            'input[name="email"]',
            'input[aria-label*="email" i]',
            'input[aria-label*="Email" i]',
            'input[placeholder*="email" i]',
            'input[placeholder*="Email" i]',
        ]
        # Try main page first
        for sel in email_selectors:
            try:
                inp = await page.query_selector(sel)
                if inp and await inp.is_visible():
                    return inp
            except Exception:
                continue

        # Try iframes (some Workday tenants embed auth in iframe)
        try:
            for frame in page.frames:
                if frame == page.main_frame:
                    continue
                for sel in email_selectors:
                    try:
                        inp = await frame.query_selector(sel)
                        if inp and await inp.is_visible():
                            logger.info(f"Found email input in iframe: {sel}")
                            return inp
                    except Exception:
                        continue
        except Exception:
            pass

        # Last resort: find the first text input near "Email" label
        try:
            result = await page.evaluate('''() => {
                const labels = document.querySelectorAll('label');
                for (const label of labels) {
                    if (label.textContent.toLowerCase().includes('email')) {
                        const forId = label.getAttribute('for');
                        if (forId) {
                            const input = document.getElementById(forId);
                            if (input) return forId;
                        }
                        // Try next sibling input
                        const next = label.nextElementSibling;
                        if (next && next.tagName === 'INPUT') return next.id || null;
                    }
                }
                return null;
            }''')
            if result:
                inp = await page.query_selector(f'#{result}') if result else None
                if inp and await inp.is_visible():
                    logger.info(f"Found email input via label association: #{result}")
                    return inp
        except Exception:
            pass

        return None

    async def _log_visible_inputs(self, page: Page):
        """Log all visible inputs for debugging auth issues."""
        try:
            inputs = await page.query_selector_all('input')
            visible_count = 0
            for inp in inputs[:10]:
                try:
                    if not await inp.is_visible():
                        continue
                    visible_count += 1
                    attrs = await inp.evaluate(
                        "el => ({type: el.type, name: el.name, id: el.id, "
                        "autoId: el.getAttribute('data-automation-id'), "
                        "placeholder: el.placeholder, ariaLabel: el.getAttribute('aria-label')})"
                    )
                    logger.info(f"  Visible input: {attrs}")
                except Exception:
                    continue
            if visible_count == 0:
                logger.info("  No visible inputs found on page")
                # Check if there are iframes
                iframe_count = len(page.frames) - 1
                if iframe_count > 0:
                    logger.info(f"  Found {iframe_count} iframes — auth may be inside iframe")
        except Exception as e:
            logger.debug(f"Error logging inputs: {e}")

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
            email_input = await self._find_email_input(page)

            if email_input:
                await email_input.click()
                await self.browser_manager.human_delay(200, 400)
                await email_input.type(email, delay=random.randint(40, 90))
                await self.browser_manager.human_delay(500, 800)
            else:
                logger.warning("Could not find email input for account creation")
                await self._log_visible_inputs(page)
                return False

            # Fill password — handle multiple password fields
            pw_fields = await page.query_selector_all('input[type="password"]')
            visible_pw = [pw for pw in pw_fields if await pw.is_visible()]

            async def _type_password(inp):
                await inp.click()
                await self.browser_manager.human_delay(150, 300)
                await inp.type(self.WORKDAY_PASSWORD, delay=random.randint(30, 70))
                await self.browser_manager.human_delay(300, 600)

            if len(visible_pw) >= 2:
                await _type_password(visible_pw[0])
                await _type_password(visible_pw[1])
                logger.debug("Filled 2 password fields")
            elif len(visible_pw) == 1:
                await _type_password(visible_pw[0])
                verify = await page.query_selector('input[data-automation-id="verifyPassword"]')
                if verify and await verify.is_visible():
                    await _type_password(verify)
                logger.debug("Filled 1 password field")
            else:
                pw_input = await page.query_selector(
                    'input[data-automation-id="password"], input[type="password"]'
                )
                if pw_input and await pw_input.is_visible():
                    await _type_password(pw_input)
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

            # Fill email — use shared helper
            email_input = await self._find_email_input(page)

            if email_input:
                await email_input.click()
                await self.browser_manager.human_delay(200, 400)
                await email_input.type(email, delay=random.randint(40, 90))
                await self.browser_manager.human_delay(500, 800)
            else:
                logger.warning("Could not find email input for signin")
                await self._log_visible_inputs(page)
                return False

            # Fill password
            pw_input = await page.query_selector(
                'input[data-automation-id="password"], '
                'input[type="password"]'
            )
            if pw_input and await pw_input.is_visible():
                await pw_input.click()
                await self.browser_manager.human_delay(150, 300)
                await pw_input.type(self.WORKDAY_PASSWORD, delay=random.randint(30, 70))
                await self.browser_manager.human_delay(500, 800)

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
                logger.info("Email verification required — attempting to verify via email link")
                verified = await self._handle_account_verification(page, email, _get_tenant(page.url))
                if verified:
                    # Re-attempt sign-in after verification
                    logger.info("Account verified, re-attempting sign-in")
                    await self.browser_manager.human_delay(2000, 3000)

                    # Dismiss cookie banners that may block interaction
                    await self.dismiss_popups(page)
                    await self.browser_manager.human_delay(500, 1000)

                    # Re-fill credentials and sign in again
                    email_input = await self._find_email_input(page)
                    if email_input:
                        await email_input.click()
                        await self.browser_manager.human_delay(200, 400)
                        await email_input.fill("")
                        await email_input.type(email, delay=random.randint(40, 90))
                        logger.info("Re-filled email after verification")
                    else:
                        logger.warning("Could not find email input after verification")
                    pw_input = await page.query_selector('input[type="password"]')
                    if pw_input and await pw_input.is_visible():
                        await pw_input.click()
                        await self.browser_manager.human_delay(150, 300)
                        await pw_input.fill("")
                        await pw_input.type(self.WORKDAY_PASSWORD, delay=random.randint(30, 70))
                        logger.info("Re-filled password after verification")
                    else:
                        logger.warning("Could not find password input after verification")
                    await self._click_workday_button(page, "Sign In")
                    await self.browser_manager.human_delay(3000, 5000)
                else:
                    logger.warning("Account verification failed")
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
                # Check one more time for verification
                if await self._needs_email_verification(page):
                    logger.warning("Still needs email verification after attempt")
                else:
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
                "verify your account", "account verification",
                "resend account verification", "verification email",
            ])
        except Exception:
            return False

    async def _handle_account_verification(self, page: Page, email: str, tenant: str) -> bool:
        """Handle Workday account verification — try code-based first, then link-based."""
        try:
            # First, click "Resend Account Verification" if available
            resend_selectors = [
                'a:has-text("Resend Account Verification")',
                'a:has-text("Resend")',
                'button:has-text("Resend Account Verification")',
                ':has-text("Resend Account Verification") >> visible=true',
            ]
            clicked_resend = False
            for sel in resend_selectors:
                try:
                    resend = await page.query_selector(sel)
                    if resend and await resend.is_visible():
                        await resend.click()
                        clicked_resend = True
                        logger.info(f"Clicked 'Resend Account Verification' via {sel}")
                        await self.browser_manager.human_delay(3000, 5000)
                        break
                except Exception:
                    continue
            if not clicked_resend:
                logger.info("Could not find Resend link — will try existing verification email")

            # Check if there's a code input on the page (code-based verification)
            code_input = await page.query_selector(
                'input[data-automation-id="verificationCode"], '
                'input[type="text"][placeholder*="code" i], '
                'input[data-automation-id*="code"], '
                'input[aria-label*="code" i]'
            )
            if code_input and await code_input.is_visible():
                return await self._verify_email(page, email, tenant)

            # Link-based verification — get verification link from email
            if not self.email_verifier:
                logger.warning("No email_verifier configured — cannot verify email")
                return False

            logger.info("Waiting for Workday verification email link...")
            loop = asyncio.get_event_loop()
            link = await loop.run_in_executor(
                None,
                lambda: self.email_verifier.get_verification_link(
                    sender_filter="workday",
                    link_pattern=None,  # Use default patterns (matches activate/verify/confirm)
                    max_age_seconds=86400,  # Workday links valid for 24 hours
                    timeout=90,
                    poll_interval=5,
                )
            )

            if not link:
                logger.warning("No verification link found in email")
                return False

            logger.info(f"Found verification link: {link[:80]}...")
            # Open the verification link in the browser
            await page.goto(link, wait_until="domcontentloaded", timeout=30000)
            await self.browser_manager.human_delay(3000, 5000)

            # Check if verification succeeded
            body_text = (await page.text_content("body") or "").lower()
            success_indicators = [
                "account verified", "email verified", "verified successfully",
                "sign in", "account has been verified", "verification successful",
                "email address", "password",  # Redirected to sign-in page
            ]
            if any(x in body_text for x in success_indicators):
                logger.info("Account verified via email link!")
                # If we landed on a page with Sign In option, click it
                signin_link = await page.query_selector('a:has-text("Sign In"), button:has-text("Sign In")')
                if signin_link and await signin_link.is_visible():
                    await signin_link.click()
                    await self.browser_manager.human_delay(2000, 3000)
                return True

            # Even if we don't see success text, if the verification error is gone
            # the account might now be verified
            logger.info("Verification link visited — assuming account is now verified")
            return True

        except Exception as e:
            logger.error(f"Account verification failed: {e}")
            return False

    async def _forgot_password(self, page: Page, email: str, job_url: str) -> bool:
        """Reset Workday password via 'Forgot Password' flow.

        Used when account exists but password is unknown/corrupted.
        """
        try:
            if not self.email_verifier:
                logger.warning("No email_verifier — cannot do forgot password")
                return False

            # First, make sure we're on the Sign In page (not Create Account)
            signin_link = await page.query_selector('[data-automation-id="signInLink"]')
            if signin_link and await signin_link.is_visible():
                try:
                    await signin_link.click(force=True)
                    await self.browser_manager.human_delay(1500, 2500)
                except Exception:
                    pass

            # Click "Forgot Password" / "Forgot your password?" link
            forgot_selectors = [
                '[data-automation-id="forgotPasswordLink"]',
                'a:has-text("Forgot your password")',
                'a:has-text("Forgot Password")',
                'a:has-text("forgot password")',
                'button:has-text("Forgot")',
                ':text("Forgot your password") >> visible=true',
            ]
            clicked = False
            for sel in forgot_selectors:
                try:
                    elem = await page.query_selector(sel)
                    if elem and await elem.is_visible():
                        await elem.click()
                        clicked = True
                        logger.info(f"Clicked Forgot Password: {sel}")
                        await self.browser_manager.human_delay(2000, 3000)
                        break
                except Exception:
                    continue

            if not clicked:
                logger.warning("Could not find 'Forgot Password' link")
                return False

            # Fill email on the forgot password page
            email_input = await self._find_email_input(page)
            if email_input:
                await email_input.click()
                await self.browser_manager.human_delay(200, 400)
                await email_input.type(email, delay=random.randint(40, 90))
                await self.browser_manager.human_delay(500, 800)
            else:
                logger.warning("Could not find email input on forgot password page")
                return False

            # Click submit — Workday uses "Reset Password" button text
            submit_clicked = False
            for label in ["Reset Password", "Submit", "Send", "Reset"]:
                if await self._click_workday_button(page, label):
                    submit_clicked = True
                    logger.info(f"Clicked forgot password submit: {label}")
                    break
            if not submit_clicked:
                for sel in [
                    'button:has-text("Reset Password")', 'button:has-text("Submit")',
                    'button:has-text("Send")', 'button[type="submit"]',
                ]:
                    try:
                        btn = await page.query_selector(sel)
                        if btn and await btn.is_visible():
                            await btn.click()
                            submit_clicked = True
                            logger.info(f"Clicked forgot password submit: {sel}")
                            break
                    except Exception:
                        continue
            if not submit_clicked:
                logger.warning("Could not click submit on forgot password page")
                return False

            logger.info("Forgot password submitted — waiting for reset email...")
            await self.browser_manager.human_delay(3000, 5000)

            # Wait for password reset link from email
            loop = asyncio.get_event_loop()
            reset_link = await loop.run_in_executor(
                None,
                lambda: self.email_verifier.get_verification_link(
                    sender_filter="workday",
                    link_pattern=r'(https?://[^\s"\'<>]*(?:password|reset|pwd|changePassword)[^\s"\'<>]*)',
                    max_age_seconds=600,
                    timeout=120,
                    poll_interval=5,
                )
            )

            if not reset_link:
                logger.warning("No password reset link found in email")
                return False

            logger.info(f"Found password reset link: {reset_link[:80]}...")

            # Navigate to the reset link
            await page.goto(reset_link, wait_until="domcontentloaded", timeout=30000)
            await self.browser_manager.human_delay(2000, 3000)

            # Fill new password fields
            pw_fields = await page.query_selector_all('input[type="password"]')
            visible_pw = [pw for pw in pw_fields if await pw.is_visible()]

            async def _type_new_pw(inp):
                await inp.click()
                await self.browser_manager.human_delay(150, 300)
                await inp.type(self.WORKDAY_PASSWORD, delay=random.randint(30, 70))
                await self.browser_manager.human_delay(300, 600)

            if len(visible_pw) >= 2:
                await _type_new_pw(visible_pw[0])
                await _type_new_pw(visible_pw[1])
            elif len(visible_pw) == 1:
                await _type_new_pw(visible_pw[0])
            else:
                logger.warning("No password fields found on reset page")
                return False

            # Click submit/save
            for label in ["Submit", "Change Password", "Reset Password", "Save"]:
                if await self._click_workday_button(page, label):
                    break
            else:
                for sel in ['button:has-text("Submit")', 'button:has-text("Change")', 'button[type="submit"]']:
                    try:
                        btn = await page.query_selector(sel)
                        if btn and await btn.is_visible():
                            await btn.click()
                            break
                    except Exception:
                        continue

            await self.browser_manager.human_delay(3000, 5000)

            body_text = (await page.text_content("body") or "").lower()
            if any(x in body_text for x in ["password changed", "password reset", "successfully", "sign in"]):
                logger.info("Password reset successful!")
                # Navigate back to the job page to sign in
                await page.goto(job_url, wait_until="domcontentloaded", timeout=15000)
                await self.browser_manager.human_delay(2000, 3000)
                await self._click_apply_button(page)
                await self.browser_manager.human_delay(2000, 3000)
                return True

            logger.warning("Password reset page did not confirm success")
            return False

        except Exception as e:
            logger.error(f"Forgot password failed: {e}")
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

        # Dismiss cookie banners and popups before filling
        await self.dismiss_popups(page)

        # Detect Self Identify page early — skip heavy generic filling that hangs.
        # IMPORTANT: Check the page HEADING or main content, not body text —
        # breadcrumbs contain "Self Identify" on every wizard page.
        is_self_identify = False
        try:
            page_heading = await page.evaluate('''
                () => {
                    // Strategy 1: Check ALL h1/h2 headings for "Self Identify"
                    const headings = document.querySelectorAll('h1, h2, [data-automation-id="pageHeaderTitle"]');
                    for (const h of headings) {
                        const text = h.textContent.trim().toLowerCase();
                        if (text === 'self identify' || text === 'self-identify') return text;
                    }
                    // Strategy 2: Check for CC-305 form content
                    const body = document.body.innerText.toLowerCase();
                    if (body.includes('voluntary self-identification of disability') &&
                        body.includes('cc-305')) return 'self identify (cc-305)';
                    if (body.includes('how do you know if you have a disability')) return 'self identify (disability)';
                    return '';
                }
            ''')
            if page_heading:
                is_self_identify = True
                logger.info(f"Self-identify page detected (heading='{page_heading}') — using specialized handler only")
        except Exception:
            pass

        if is_self_identify:
            # Only run the self-identify handler — skip generic fill that hangs
            await self._fill_self_identify_page(page, config)
            return

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

        # Handle self-identify / disability page (CC-305)
        await self._fill_self_identify_page(page, config)

        # Second radio pass — some radios only appear after other fields are filled
        await self._fill_radio_buttons(page, config)

        # Second checkbox pass — consent checkboxes may be at the bottom of the page
        await self._fill_checkboxes(page, config)

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
            # Middle name
            ('[data-automation-id="legalNameSection_middleName"]', personal.get("middle_name", "")),
            ('input#name--legalName--middleName', personal.get("middle_name", "")),
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
            # Address line 2 — apartment/unit (usually empty)
            ('[data-automation-id="addressSection_addressLine2"]', personal.get("apartment", "")),
            ('input#address--addressLine2', personal.get("apartment", "")),
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

        # Clear Address Line 2 if it was incorrectly filled with Address Line 1 value
        for addr2_sel in ['[data-automation-id="addressSection_addressLine2"]', 'input#address--addressLine2']:
            try:
                elem = await page.query_selector(addr2_sel)
                if elem and await elem.is_visible():
                    val = (await elem.input_value() or "").strip()
                    addr1 = personal.get("address", "")
                    apartment = personal.get("apartment", "")
                    if val and val == addr1 and not apartment:
                        await elem.fill("")
                        logger.debug(f"Cleared Address Line 2 (was duplicate of Line 1)")
            except Exception:
                pass

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

        # Clear referredBy field (generic form filler may fill it with email/name)
        # It must be empty when source is non-referral, or Workday throws a page error
        try:
            ref_container = await page.query_selector('[data-automation-id="formField-referredBy"]')
            if ref_container and await ref_container.is_visible():
                ref_input = await ref_container.query_selector('input[type="text"], textarea')
                if ref_input and await ref_input.is_visible():
                    val = (await ref_input.input_value() or "").strip()
                    if val:
                        await ref_input.fill("")
                        logger.debug(f"Cleanup: cleared referredBy (was '{val[:30]}')")
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

        # "How Did You Hear About Us?" — multiselect typeahead
        await self._fill_how_did_you_hear(page)

        # Handle generic dropdowns (sponsorship, authorization, etc.)
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

    async def _fill_how_did_you_hear(self, page: Page) -> bool:
        """Fill 'How Did You Hear About Us?' which is often a multiselect typeahead or dropdown."""
        try:
            # Find the formField container for "How Did You Hear"
            # Try specific automation ID first, then label text search
            container = await page.query_selector('[data-automation-id="formField-source"]')
            if not container:
                container_handle = await page.evaluate_handle('''
                    () => {
                        // Strategy 1: labels with formField ancestor
                        const labels = document.querySelectorAll('label, [data-automation-id="formLabel"]');
                        for (const l of labels) {
                            const text = (l.textContent || "").toLowerCase().trim();
                            if (text.includes("how did you hear") || text.includes("hear about us") ||
                                (text === "source" || text === "source *" || text === "source*")) {
                                const container = l.closest('[data-automation-id^="formField"]') ||
                                                  l.closest('[data-automation-id="questionItem"]') ||
                                                  l.parentElement;
                                if (container) return container;
                            }
                        }
                        // Strategy 2: div/legend with "how did you hear" text
                        const allEls = document.querySelectorAll('div, legend, fieldset');
                        for (const el of allEls) {
                            const text = (el.textContent || "").toLowerCase().trim();
                            if ((text.startsWith("how did you hear") || text.startsWith("source")) && text.length < 50) {
                                // Make sure this has a dropdown button inside it
                                const btn = el.querySelector('button[aria-haspopup="listbox"]');
                                if (btn) return el;
                                // Check parent
                                const parent = el.parentElement;
                                if (parent) {
                                    const pbtn = parent.querySelector('button[aria-haspopup="listbox"]');
                                    if (pbtn) return parent;
                                }
                            }
                        }
                        return null;
                    }
                ''')
                is_null = await container_handle.evaluate("el => el === null")
                if is_null:
                    logger.debug("How Did You Hear: field not found on this page")
                    return False
                container = container_handle.as_element()
                if not container:
                    return False
            logger.info("How Did You Hear: found container")

            # Check if already filled (selectedItemList has items)
            has_selected = await container.evaluate('''
                (el) => {
                    const items = el.querySelectorAll('[data-automation-id="selectedItemList"] li');
                    return items.length > 0;
                }
            ''')
            if has_selected:
                logger.debug("How Did You Hear already has a selection")
                return True

            # Strategy 1: Try regular dropdown button
            dropdown_btn = await container.query_selector('button[aria-haspopup="listbox"]')
            if dropdown_btn and await dropdown_btn.is_visible():
                btn_text = (await dropdown_btn.text_content() or "").strip()
                logger.info(f"How Did You Hear: Strategy 1 — dropdown btn text='{btn_text[:40]}'")
                # Skip if already filled
                if btn_text.lower() not in ("select one", "select", "choose", ""):
                    logger.info(f"How Did You Hear: already filled with '{btn_text[:40]}'")
                    return True
                # Get the dropdown's aria-controls or listbox ID to scope menu items
                btn_aria = await dropdown_btn.get_attribute("aria-controls") or ""
                try:
                    await dropdown_btn.scroll_into_view_if_needed(timeout=3000)
                except Exception:
                    pass
                try:
                    await dropdown_btn.click(timeout=5000)
                except Exception as e:
                    logger.warning(f"How Did You Hear: dropdown click failed: {e}")
                    await page.keyboard.press("Escape")
                    dropdown_btn = None  # Fall through to Strategy 2
                else:
                    await self.browser_manager.human_delay(500, 800)
                    # Look for menu items - try to scope to the specific listbox first
                    items = []
                    if btn_aria:
                        listbox = await page.query_selector(f'#{btn_aria}')
                        if listbox:
                            items = await listbox.query_selector_all('[data-automation-id="menuItem"], [role="option"]')
                    if not items:
                        # Fallback: use the most recently visible listbox/popup
                        items = await page.query_selector_all('[role="listbox"]:last-of-type [role="option"], [data-automation-id="menuItemList"]:last-of-type [data-automation-id="menuItem"]')
                    if not items:
                        items = await page.query_selector_all('[data-automation-id="menuItem"]')
                    if items:
                        candidates = ["online job board", "job board", "internet", "online", "website", "other"]
                        item_texts = []
                        for item in items:
                            item_texts.append((await item.text_content() or "").strip())

                        logger.info(f"How Did You Hear: dropdown has {len(items)} options: {item_texts[:6]}")

                        # Filter out obvious non-source items (phone country codes etc.)
                        filtered_items = [(i, t) for i, t in enumerate(item_texts) if not any(x in t.lower() for x in ["united states", "(+", "america", "canada", "country"])]
                        search_texts = filtered_items if filtered_items else list(enumerate(item_texts))

                        for candidate in candidates:
                            for idx, text in search_texts:
                                if candidate in text.lower():
                                    await items[idx].click()
                                    logger.info(f"How Did You Hear dropdown: '{text}'")
                                    self._fields_filled["how_did_you_hear"] = text
                                    return True

                        # Fallback: select first filtered option (not Select One)
                        for idx, text in search_texts:
                            if text.lower() not in ("select one", ""):
                                await items[idx].click()
                                logger.info(f"How Did You Hear dropdown (first): '{text}'")
                                self._fields_filled["how_did_you_hear"] = text
                                return True

                    await page.keyboard.press("Escape")

            # Strategy 2: Try typeahead input (multiselect / hierarchical)
            # Workday "How Did You Hear" can be hierarchical with sub-menus.
            # Use keyboard navigation: type search term, arrow down, Enter.
            type_input = await container.query_selector('input[type="text"], input:not([type])')
            if type_input and await type_input.is_visible():
                logger.info("How Did You Hear: Strategy 2 — typeahead input found")
                # Helper: check if THIS container's selectedItemList has items
                async def _has_selection():
                    return await container.evaluate('''
                        (el) => {
                            const items = el.querySelectorAll('[data-automation-id="selectedItemList"] li');
                            return items.length > 0;
                        }
                    ''')

                # Try different search terms — "Other" is simplest (often leaf node)
                for search_term in ["Other", "Job Board", "Internet", "Online"]:
                    if await _has_selection():
                        return True

                    await type_input.click()
                    await self.browser_manager.human_delay(200, 400)
                    await type_input.fill("")
                    await type_input.type(search_term, delay=50)
                    await self.browser_manager.human_delay(1000, 1500)

                    # Try arrow down + Enter to select first match
                    await page.keyboard.press("ArrowDown")
                    await self.browser_manager.human_delay(200, 300)
                    await page.keyboard.press("Enter")
                    await self.browser_manager.human_delay(800, 1200)

                    if await _has_selection():
                        logger.info(f"How Did You Hear: selected via keyboard '{search_term}'")
                        self._fields_filled["how_did_you_hear"] = search_term
                        return True

                    # If Enter opened a sub-menu, try another Enter for first sub-item
                    await page.keyboard.press("ArrowDown")
                    await self.browser_manager.human_delay(200, 300)
                    await page.keyboard.press("Enter")
                    await self.browser_manager.human_delay(500, 800)

                    if await _has_selection():
                        logger.info(f"How Did You Hear: selected via keyboard '{search_term}' > sub-item")
                        self._fields_filled["how_did_you_hear"] = search_term
                        return True

                    # Escape and try next search term
                    await page.keyboard.press("Escape")
                    await self.browser_manager.human_delay(300, 500)

                # Last resort: just click the container's input and press Enter
                await page.keyboard.press("Escape")

            # Strategy 3: Try clicking inside the container to reveal a hidden input/dropdown
            # Some Workday tenants hide the interactive element until clicked
            try:
                await container.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            clickable = await container.query_selector(
                'div[role="group"], div[data-automation-id], span, div'
            )
            if clickable:
                try:
                    await clickable.click(timeout=3000)
                    await self.browser_manager.human_delay(500, 800)
                    # Check if a dropdown or input appeared now
                    dropdown_btn = await container.query_selector('button[aria-haspopup="listbox"]')
                    type_input = await container.query_selector('input[type="text"], input:not([type])')
                    if dropdown_btn and await dropdown_btn.is_visible():
                        logger.info("How Did You Hear: Strategy 3 — dropdown appeared after click")
                        await dropdown_btn.click(timeout=3000)
                        await self.browser_manager.human_delay(500, 800)
                        items = await page.query_selector_all('[role="listbox"]:last-of-type [role="option"], [data-automation-id="menuItem"]')
                        if items:
                            item_texts = [(await item.text_content() or "").strip() for item in items]
                            logger.info(f"How Did You Hear Strategy 3: {len(items)} options: {item_texts[:6]}")
                            candidates = ["online job board", "job board", "internet", "online", "website", "other"]
                            for candidate in candidates:
                                for i, t in enumerate(item_texts):
                                    if candidate in t.lower():
                                        await items[i].click()
                                        logger.info(f"How Did You Hear Strategy 3: '{t}'")
                                        self._fields_filled["how_did_you_hear"] = t
                                        return True
                            # Fallback: first non-"Select One"
                            for i, t in enumerate(item_texts):
                                if t.lower() not in ("select one", ""):
                                    await items[i].click()
                                    logger.info(f"How Did You Hear Strategy 3 (first): '{t}'")
                                    self._fields_filled["how_did_you_hear"] = t
                                    return True
                        await page.keyboard.press("Escape")
                    elif type_input and await type_input.is_visible():
                        logger.info("How Did You Hear: Strategy 3 — typeahead appeared after click")
                        await type_input.fill("")
                        await type_input.type("Other", delay=50)
                        await self.browser_manager.human_delay(1000, 1500)
                        await page.keyboard.press("ArrowDown")
                        await self.browser_manager.human_delay(200, 300)
                        await page.keyboard.press("Enter")
                        await self.browser_manager.human_delay(500, 800)
                        self._fields_filled["how_did_you_hear"] = "Other"
                        return True
                except Exception as e:
                    logger.debug(f"How Did You Hear Strategy 3 failed: {e}")

            return False
        except Exception as e:
            logger.debug(f"How Did You Hear fill failed: {e}")
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

    async def _fill_workday_typeahead_elem(self, page: Page, elem, value: str, alt_values: list = None) -> bool:
        """Fill a Workday typeahead field using an already-found element handle.

        Workday typeaheads require selecting from the suggestion popup — free text is NOT accepted.
        We try progressively shorter search terms until we get suggestions.
        Skips non-selectable header items like "Partial List (First 500 Entries)".

        Args:
            alt_values: Alternative values to try if the primary value isn't found.
                        e.g. for school: ["San Jose State", "SJSU", "San José State University"]
        """
        try:
            suggestion_selectors = [
                '[data-automation-id="promptOption"]',
                '[data-automation-id="menuItem"]',
                '[role="option"]',
                'li[role="presentation"]',
            ]
            # Non-selectable header/info items to skip
            skip_patterns = ["partial list", "first 500", "first 100", "no results", "no items", "no match", "loading"]
            # Category headers that aren't real values
            category_only = ["all"]

            # Dismiss any existing popup before starting (prevents cross-contamination)
            await page.keyboard.press("Escape")
            await self.browser_manager.human_delay(200, 400)

            # Build a flat list of search terms: primary value's variants, then each alt's single best term
            words = value.split()
            search_terms = [value]
            if len(words) > 2:
                search_terms.append(" ".join(words[:3]))
                search_terms.append(" ".join(words[:2]))
            if len(words) > 1:
                search_terms.append(words[0])
            # Add each alt_value as a single search term (don't expand further)
            for alt in (alt_values or []):
                search_terms.append(alt)
            # Deduplicate while preserving order
            seen = set()
            search_terms = [s.strip() for s in search_terms if s.strip() and not (s.strip().lower() in seen or seen.add(s.strip().lower()))]
            # Cap to prevent timeout (each search takes ~4s)
            search_terms = search_terms[:10]

            for search_term in search_terms:
                # Dismiss any leftover popup from previous attempt
                await page.keyboard.press("Escape")
                await self.browser_manager.human_delay(150, 300)

                await elem.click()
                await elem.fill("")
                await self.browser_manager.human_delay(200, 400)
                await elem.type(search_term[:30], delay=80)
                await self.browser_manager.human_delay(1500, 2500)

                found_items = False
                # Scope items to the popup that appeared near this element
                scoped_items = await self._get_scoped_typeahead_items(
                    page, elem, suggestion_selectors, skip_patterns, category_only
                )

                if scoped_items:
                    found_items = True
                    real_items = scoped_items
                    logger.info(f"Typeahead '{search_term}': {len(real_items)} items: {[t[:40] for _, t in real_items[:5]]}")

                    # Best match: exact equality first (against original value and search_term)
                    for item, text in real_items:
                        text_lower = text.lower().strip()
                        if text_lower == value.lower().strip() or text_lower == search_term.lower().strip():
                            await item.click()
                            logger.info(f"Typeahead exact match: '{text}' (search='{search_term}')")
                            await self.browser_manager.human_delay(300, 500)
                            return True

                    # Good match: search value in option text (substring)
                    for item, text in real_items:
                        text_lower = text.lower()
                        if value.lower() in text_lower or search_term.lower() in text_lower:
                            await item.click()
                            logger.info(f"Typeahead selected: '{text}' (search='{search_term}')")
                            await self.browser_manager.human_delay(300, 500)
                            return True

                    # If dropdown shows unfiltered results (starting from A), try scrolling
                    if len(real_items) > 5 and real_items[0][1][0].upper() < value[0].upper():
                        listbox = await page.query_selector('[role="listbox"], [data-automation-id="menuItemList"]')
                        if listbox:
                            for scroll_attempt in range(10):
                                await listbox.evaluate('el => el.scrollTop += el.clientHeight')
                                await self.browser_manager.human_delay(300, 500)
                                scroll_items = await self._get_scoped_typeahead_items(
                                    page, elem, suggestion_selectors, skip_patterns, category_only
                                )
                                for item, text in scroll_items:
                                    if value.lower() in text.lower() or search_term.lower() in text.lower():
                                        await item.click()
                                        logger.info(f"Typeahead selected (scrolled): '{text}'")
                                        await self.browser_manager.human_delay(300, 500)
                                        return True

                    # Word match: prefer items matching MORE words from value
                    value_words = value.split()
                    best_word_match = None
                    best_word_count = 0
                    for item, text in real_items:
                        match_count = sum(1 for word in value_words if len(word) > 2 and word.lower() in text.lower())
                        if match_count > best_word_count:
                            best_word_count = match_count
                            best_word_match = (item, text)
                    if best_word_match and best_word_count > 0:
                        item, text = best_word_match
                        await item.click()
                        logger.info(f"Typeahead word-match: '{text}' ({best_word_count} words matched)")
                        await self.browser_manager.human_delay(300, 500)
                        return True

                    # Don't blindly click first option — might be wrong field's items
                    # Only click first if search_term matches something in the items
                    first_item, first_text = real_items[0]
                    if any(w.lower() in first_text.lower() for w in search_term.split() if len(w) > 2):
                        await first_item.click()
                        logger.info(f"Typeahead first-real: '{first_text}' (search='{search_term}')")
                        await self.browser_manager.human_delay(300, 500)
                        return True
                    else:
                        logger.info(f"Typeahead: skipping unrelated items (first='{first_text[:40]}', search='{search_term}')")

                if not found_items:
                    # No clickable items at all — try keyboard navigation
                    for _ in range(3):
                        await page.keyboard.press("ArrowDown")
                        await self.browser_manager.human_delay(150, 250)
                    await page.keyboard.press("Enter")
                    await self.browser_manager.human_delay(500, 800)

                    # Check if a selection was made
                    current_val = await elem.evaluate('''el => {
                        const container = el.closest('[data-automation-id^="formField"]') || el.parentElement?.parentElement;
                        if (container) {
                            const selected = container.querySelectorAll('[data-automation-id="selectedItemList"] li');
                            if (selected.length > 0) return '__SELECTED__';
                        }
                        return el.value || '';
                    }''')
                    if current_val == '__SELECTED__' or (current_val and current_val != search_term[:30]):
                        logger.info(f"Typeahead keyboard selected for '{value}' (search='{search_term}')")
                        return True

                await page.keyboard.press("Escape")
                await self.browser_manager.human_delay(300, 500)

            logger.warning(f"No typeahead suggestions for '{value}' after trying all search terms")
            return False

        except Exception as e:
            logger.debug(f"Typeahead elem fill failed: {e}")
            return False

    async def _get_scoped_typeahead_items(self, page: Page, elem, selectors: list, skip_patterns: list, category_only: list) -> list:
        """Get typeahead items scoped to the ACTIVE popup for this element.

        This prevents cross-contamination between multiple typeahead fields on the same page
        (e.g. Field of Study selected chips leaking into School search).

        Key insight: Workday typeahead popups appear as floating overlays, NOT inside the formField.
        The selected item chips (promptOption) inside other fields' selectedItemList must be excluded.
        """
        try:
            # Strategy 1: Look for the active floating popup/overlay
            # Workday popups use role="listbox" or specific popup containers
            popup_items = await page.evaluate_handle('''(elemId) => {
                // Find active popup overlays (NOT selected item chips inside form fields)
                // Active popups are typically in a portal/overlay layer
                const popups = document.querySelectorAll(
                    '[data-automation-id="popupContent"], ' +
                    '[role="listbox"]:not([data-automation-id="selectedItemList"]), ' +
                    '.css-1rc7hqf'
                );

                // Find the visible popup (there should only be one at a time)
                for (const popup of popups) {
                    if (popup.offsetParent !== null && popup.querySelector('[data-automation-id="promptOption"], [role="option"], li')) {
                        return popup;
                    }
                }
                return null;
            }''', await elem.get_attribute("id") or "")

            is_null = await popup_items.evaluate("el => el === null")
            if not is_null:
                scoped_popup = popup_items.as_element()
                if scoped_popup:
                    real_items = []
                    for sel in selectors:
                        items = await scoped_popup.query_selector_all(sel)
                        if items:
                            for item in items:
                                text = (await item.text_content() or "").strip()
                                if not text:
                                    continue
                                if any(s in text.lower() for s in skip_patterns):
                                    continue
                                if text.lower().strip() in category_only:
                                    continue
                                real_items.append((item, text))
                            if real_items:
                                return real_items

            # Strategy 2: Page-level but EXCLUDE items inside selectedItemList
            # (those are chips showing already-selected values in OTHER fields)
            for sel in selectors:
                items = await page.query_selector_all(sel)
                if not items:
                    continue

                real_items = []
                for item in items:
                    text = (await item.text_content() or "").strip()
                    if not text:
                        continue
                    if any(s in text.lower() for s in skip_patterns):
                        continue
                    if text.lower().strip() in category_only:
                        continue
                    # Exclude items inside selectedItemList (already-selected chips)
                    is_selected_chip = await item.evaluate('''el => {
                        return !!el.closest('[data-automation-id="selectedItemList"]');
                    }''')
                    if is_selected_chip:
                        continue
                    real_items.append((item, text))

                if real_items:
                    return real_items

            return []
        except Exception as e:
            logger.debug(f"Scoped typeahead items failed: {e}")
            return []

    async def _fill_generic_workday_dropdowns(self, page: Page, config: Dict[str, Any]) -> None:
        """Fill generic Workday dropdowns based on question text."""
        screening = config.get("screening", {})
        work_auth = config.get("work_authorization", {})
        demographics = config.get("demographics", {})

        # Find all dropdown buttons (exclude dateDropdown — that's for date pickers)
        dropdown_buttons = await page.query_selector_all(
            'button[aria-haspopup="listbox"]:not([data-automation-id="dateDropdown"]), '
            '[data-automation-id*="dropdown"]:not([data-automation-id="dateDropdown"]), '
            '[data-automation-id="multiselectInputContainer"] button'
        )

        for btn in dropdown_buttons:
            try:
                parent = await btn.evaluate_handle(
                    "el => el.closest('[data-automation-id^=\"formField\"]') || el.closest('[data-automation-id=\"questionItem\"]') || el.parentElement"
                )
                label_elem = await parent.query_selector(
                    'label, [data-automation-id="formLabel"], legend, [data-automation-id="richText"]'
                )
                if not label_elem:
                    # Try aria-label on the button itself
                    aria = (await btn.get_attribute('aria-label') or "")
                    if aria:
                        label_text = aria.lower()
                    else:
                        continue
                else:
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
                    value = "East Asian|Asian"
                elif "veteran" in label_text:
                    value = "__VETERAN__"  # Special marker — use smart matching below
                elif "disab" in label_text:
                    value = demographics.get("disability_status", "I do not wish to answer")
                elif "citizen" in label_text:
                    # "citizen of another country" is the OPPOSITE of us_citizen
                    if "another country" in label_text or "other country" in label_text or "foreign" in label_text:
                        value = "No" if work_auth.get("us_citizen", True) else "Yes"
                    else:
                        value = "Yes" if work_auth.get("us_citizen", True) else "No"
                elif "how did you hear" in label_text or "hear about" in label_text:
                    # Skip — handled by dedicated _fill_how_did_you_hear method
                    continue
                elif "source" in label_text and "open" not in label_text and len(label_text) < 20:
                    value = "Online Job Board|Job Board|Internet|Online|Website|Other"
                elif "previously" in label_text and "employed" in label_text:
                    value = "No"
                elif "background check" in label_text:
                    value = "Yes"
                elif "drug" in label_text and "test" in label_text:
                    value = "Yes"
                elif "commut" in label_text or "on-site" in label_text or "in.?office" in label_text:
                    value = "Yes"
                elif "consent" in label_text or "agree" in label_text or "acknowledge" in label_text:
                    value = "Yes|I Agree|I Accept|Agree|Accept"
                elif "prefix" in label_text and len(label_text) < 30:
                    value = "Mr."
                elif "suffix" in label_text and len(label_text) < 30:
                    # Suffix is optional — just skip/dismiss it
                    continue

                if value:
                    # Check if already filled (not "Select One" or empty)
                    btn_text = (await btn.text_content() or "").strip().lower()
                    if btn_text and btn_text not in ("select one", "select", "choose one", ""):
                        # Exception: fix wrong veteran answer (e.g. "I identify as a veteran" for non-veteran)
                        if "veteran" in label_text and "i identify as a veteran" in btn_text:
                            logger.info(f"Fixing wrong veteran answer: '{btn_text[:50]}'")
                        else:
                            continue

                    await btn.click()
                    await self.browser_manager.human_delay(400, 700)

                    # Wait for listbox to appear
                    try:
                        await page.wait_for_selector('[role="listbox"], [data-automation-id="menuItemList"]', timeout=3000)
                    except Exception:
                        logger.warning(f"Generic dropdown '{label_text[:30]}' — listbox did not appear after click")
                        await page.keyboard.press("Escape")
                        continue

                    # Scroll listbox to load lazy items
                    await page.evaluate('''
                        () => {
                            const lb = document.querySelector('[role="listbox"], [data-automation-id="menuItemList"]');
                            if (lb) { lb.scrollTop = lb.scrollHeight; }
                        }
                    ''')
                    await self.browser_manager.human_delay(200, 400)
                    await page.evaluate('''
                        () => {
                            const lb = document.querySelector('[role="listbox"], [data-automation-id="menuItemList"]');
                            if (lb) { lb.scrollTop = 0; }
                        }
                    ''')
                    await self.browser_manager.human_delay(200, 300)

                    # Support pipe-separated fallback values
                    candidates = [v.strip() for v in value.split("|")] if "|" in value else [value]
                    items = await page.query_selector_all('[data-automation-id="menuItem"], [role="option"]')
                    item_texts = []
                    for item in items:
                        item_texts.append((await item.text_content() or "").strip())
                    logger.info(f"Generic dropdown '{label_text[:30]}' has {len(items)} options: {item_texts[:10]}")

                    matched = False

                    # Special veteran matching — use priority-based matching
                    if value == "__VETERAN__":
                        item_texts_lower = [t.lower() for t in item_texts]
                        vet_idx = -1
                        # Priority 1: "I am not a protected veteran"
                        for i, t in enumerate(item_texts_lower):
                            if "i am not a protected veteran" in t:
                                vet_idx = i; break
                        # Priority 2: "I am not a veteran" / "I am not a Veteran"
                        if vet_idx < 0:
                            for i, t in enumerate(item_texts_lower):
                                if t.strip().startswith("i am not") and "veteran" in t:
                                    vet_idx = i; break
                        # Priority 3: "not" + "veteran" but NOT "I identify"
                        if vet_idx < 0:
                            for i, t in enumerate(item_texts_lower):
                                if "not" in t and "veteran" in t and not t.strip().startswith("i identify"):
                                    vet_idx = i; break
                        # Priority 4: "No" exact
                        if vet_idx < 0:
                            for i, t in enumerate(item_texts_lower):
                                if t.strip() == "no":
                                    vet_idx = i; break
                        if vet_idx >= 0:
                            try:
                                await items[vet_idx].click()
                                logger.info(f"Generic dropdown '{label_text[:30]}' → '{item_texts[vet_idx]}' (veteran priority)")
                            except Exception as click_err:
                                logger.warning(f"Generic dropdown click failed for veteran: {click_err}")
                            self._fields_filled[label_text[:40]] = item_texts[vet_idx]
                            matched = True
                    else:
                        # Try each candidate value
                        for candidate in candidates:
                            for i, text in enumerate(item_texts):
                                if candidate.lower() == text.lower() or candidate.lower() in text.lower():
                                    try:
                                        await items[i].click()
                                        logger.info(f"Generic dropdown '{label_text[:30]}' → '{text}' (matched '{candidate}')")
                                    except Exception as click_err:
                                        logger.warning(f"Generic dropdown click failed for '{text}': {click_err}")
                                    self._fields_filled[label_text[:40]] = text
                                    matched = True
                                    break
                            if matched:
                                break

                    if not matched and items:
                        # Required field — select first non-empty option as last resort
                        await items[0].click()
                        self._fields_filled[label_text[:40]] = item_texts[0] if item_texts else "first"
                        logger.info(f"Generic dropdown fallback: '{label_text[:40]}' → '{item_texts[0] if item_texts else '?'}'")
                        matched = True

                    if not matched:
                        await page.keyboard.press("Escape")
                    else:
                        await self.browser_manager.human_delay(200, 300)

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
                if "previously" in label_text and ("employed" in label_text or "worked" in label_text):
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
                    logger.info(f"Radio '{label_text[:60]}' — asking AI with options={opt_texts}")
                    ai_ans = await self.ai_answerer.answer_question(label_text, "radio", options=opt_texts)
                    if ai_ans:
                        answer = ai_ans.lower().strip()
                        logger.info(f"Radio AI answer: '{answer}'")

                if not answer:
                    logger.warning(f"No answer for radio group: {label_text[:60]}")
                    continue

                # Map yes/no to value attributes (Workday uses true/false)
                value_map = {"yes": "true", "no": "false"}
                target_value = value_map.get(answer, answer)

                logger.info(f"Radio '{label_text[:50]}' → answer='{answer}', target_value='{target_value}'")
                logger.info(f"  Options: {[(o.get('text',''), o.get('value','')) for o in options]}")

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
                    logger.warning(f"JS radio click failed for '{label_text[:60]}' answer='{answer}'")
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
                logger.warning(f"Error handling radio group '{group_data.get('label', 'unknown')[:50]}': {e}")

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
                'input[type="checkbox"], [role="checkbox"], '
                '[data-automation-id*="checkbox" i], [data-automation-id*="Checkbox" i]'
            )
            if all_checkboxes:
                logger.info(f"Found {len(all_checkboxes)} checkboxes on page")
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
                    if any(kw in pt for kw in ["consent", "agree", "terms", "acknowledge", "sms", "text message", "voluntarily", "accept"]):
                        await cb.click(force=True)
                        logger.info(f"Checked consent checkbox: '{parent_text[:60]}'")
                        await self.browser_manager.human_delay(200, 400)
                    else:
                        logger.debug(f"Skipped unchecked checkbox: '{parent_text[:60]}'")
                except Exception as e:
                    logger.debug(f"Error processing checkbox: {e}")
        except Exception as e:
            logger.warning(f"Error checking consent checkboxes: {e}")

    async def _fill_self_identify_page(self, page: Page, config: Dict[str, Any]) -> None:
        """Handle the Workday 'Self Identify' page (CC-305 disability form).

        Handles:
        - CC-305 disability form: Name, Date, disability radio/checkboxes
        - Language dropdown (usually pre-filled)
        """
        # Check main content area for CC-305 specific text (NOT breadcrumbs)
        try:
            main_content = await page.evaluate('''
                () => {
                    const container = document.querySelector(
                        '[data-automation-id="wizardPageContainer"]'
                    ) || document.body;
                    // Get text from main content, excluding nav/breadcrumbs
                    const nav = container.querySelector('[data-automation-id="progressBar"]');
                    if (nav) nav.remove();
                    return container.textContent.toLowerCase();
                }
            ''')
        except Exception:
            return

        if not any(x in main_content for x in [
            "self-identification of disability", "cc-305",
            "voluntary self-identification of disability",
            "how do you know if you have a disability",
        ]):
            return

        logger.info("Self-identify page detected — filling disability form")
        personal = config.get("personal_info", {})

        # --- Fill Name and Employee ID fields ---
        try:
            # Use broader selectors — find ALL text inputs on this page
            text_inputs = await page.query_selector_all('input[type="text"]')
            parts = [personal.get('first_name', ''), personal.get('middle_name', ''), personal.get('last_name', '')]
            full_name = " ".join(p for p in parts if p).strip()
            for inp in text_inputs:
                if not await inp.is_visible():
                    continue
                # Get label from multiple strategies (Workday varies)
                label = await inp.evaluate('''el => {
                    // Strategy 1: previous sibling label
                    const prev = el.previousElementSibling;
                    if (prev && prev.tagName === 'LABEL') return prev.textContent.trim().toLowerCase();
                    // Strategy 2: parent container label
                    const parent = el.closest('[data-automation-id]') || el.closest('.css-1wc04zy') || el.parentElement;
                    const lbl = parent ? (parent.querySelector('label') || parent.querySelector('[data-automation-id="formLabel"]')) : null;
                    if (lbl) return lbl.textContent.trim().toLowerCase();
                    // Strategy 3: placeholder
                    return (el.placeholder || '').trim().toLowerCase();
                }''')
                current_val = (await inp.input_value() or "").strip()
                if "employee" in label and ("id" in label or "number" in label or "applicable" in label):
                    if not current_val or current_val.lower() in ["i do not wish to answer"]:
                        await inp.fill("N/A")
                        logger.info("Self-identify: filled Employee ID = 'N/A'")
                elif "name" in label and "employee" not in label:
                    if not current_val or current_val.lower() in ["i do not wish to answer"]:
                        if full_name:
                            await inp.fill(full_name)
                            logger.info(f"Self-identify: filled Name = '{full_name}'")
        except Exception as e:
            logger.debug(f"Error filling self-identify name: {e}")

        # --- Fill Date field (CC-305 asks for today's date) ---
        try:
            import datetime
            today_str = datetime.date.today().strftime("%m/%d/%Y")
            date_digits = datetime.date.today().strftime("%m%d%Y")

            # Use JS to find the date input — Workday uses varying selectors
            date_info = await page.evaluate('''
                () => {
                    // Strategy 1: placeholder contains MM
                    let el = document.querySelector('input[placeholder*="MM"]');
                    if (el && el.offsetParent !== null) return {found: true, strategy: 'placeholder'};
                    // Strategy 2: data-automation-id contains date
                    el = document.querySelector('input[data-automation-id*="date" i]');
                    if (el && el.offsetParent !== null) return {found: true, strategy: 'data-auto'};
                    // Strategy 3: label "Date" nearby
                    const labels = document.querySelectorAll('label');
                    for (const lbl of labels) {
                        const text = lbl.textContent.trim().toLowerCase();
                        if (text === 'date' || text === 'date *' || text === 'date*') {
                            const parent = lbl.closest('[data-automation-id]') || lbl.parentElement;
                            const inp = parent ? parent.querySelector('input') : null;
                            if (inp && inp.offsetParent !== null) return {found: true, strategy: 'label'};
                        }
                    }
                    // Strategy 4: any input near "Date" text
                    const all = document.querySelectorAll('input[type="text"], input:not([type])');
                    for (const inp of all) {
                        if (inp.offsetParent === null) continue;
                        const prev = inp.previousElementSibling || (inp.parentElement && inp.parentElement.previousElementSibling);
                        if (prev && prev.textContent.trim().toLowerCase().startsWith('date')) {
                            return {found: true, strategy: 'sibling'};
                        }
                    }
                    return {found: false};
                }
            ''')
            logger.debug(f"Self-identify: date input search result: {date_info}")

            if date_info.get('found'):
                # Get the actual element using the matching strategy
                strategy = date_info['strategy']
                if strategy == 'placeholder':
                    dinp = await page.query_selector('input[placeholder*="MM"]')
                elif strategy == 'data-auto':
                    dinp = await page.query_selector('input[data-automation-id*="date" i]')
                else:
                    # For label/sibling strategies, use JS to get it
                    dinp = await page.evaluate_handle('''
                        () => {
                            const labels = document.querySelectorAll('label');
                            for (const lbl of labels) {
                                const text = lbl.textContent.trim().toLowerCase();
                                if (text === 'date' || text === 'date *' || text === 'date*') {
                                    const parent = lbl.closest('[data-automation-id]') || lbl.parentElement;
                                    const inp = parent ? parent.querySelector('input') : null;
                                    if (inp && inp.offsetParent !== null) return inp;
                                }
                            }
                            return null;
                        }
                    ''')

                if dinp:
                    await dinp.scroll_into_view_if_needed()
                    await self.browser_manager.human_delay(200, 400)

                    # Strategy 1: Focus + clear + type digits (mask adds slashes)
                    await dinp.focus()
                    await self.browser_manager.human_delay(100, 200)
                    await page.keyboard.press("Home")
                    for _ in range(12):
                        await page.keyboard.press("Delete")
                    await self.browser_manager.human_delay(100, 200)
                    await page.keyboard.type(date_digits, delay=100)
                    await self.browser_manager.human_delay(300, 500)

                    new_val = (await dinp.input_value() or "").strip()
                    if not new_val or len(new_val) < 8 or "DD" in new_val or "YYYY" in new_val:
                        # Strategy 2: Triple-click + type full date (with short timeout)
                        try:
                            await dinp.click(click_count=3, timeout=3000)
                            await self.browser_manager.human_delay(100, 200)
                            await page.keyboard.type(today_str, delay=80)
                            await self.browser_manager.human_delay(200, 300)
                        except Exception:
                            logger.debug("Self-identify: date click timeout, trying JS setter")

                    new_val = (await dinp.input_value() or "").strip()
                    if not new_val or len(new_val) < 8 or "DD" in new_val or "YYYY" in new_val:
                        # Strategy 3: JS native setter
                        await dinp.evaluate(f'''el => {{
                            const nativeSetter = Object.getOwnPropertyDescriptor(
                                window.HTMLInputElement.prototype, 'value').set;
                            nativeSetter.call(el, '{today_str}');
                            el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                            el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        }}''')

                    await page.keyboard.press("Tab")
                    final_val = (await dinp.input_value() or "").strip()
                    logger.info(f"Self-identify: filled Date = '{final_val}' (target='{today_str}')")
                else:
                    logger.warning("Self-identify: date input found by JS but couldn't get handle")
            else:
                logger.warning("Self-identify: could not find date input on page")
        except Exception as e:
            logger.warning(f"Self-identify: error filling date: {e}")

        # Scroll to bottom to make disability checkboxes visible
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await self.browser_manager.human_delay(500, 800)

        # --- Disability radio buttons / checkboxes ---
        # Options: "Yes, I have a disability" / "No, I do not have a disability" / "I do not want to answer"
        target_clicked = False
        try:
            # Try Workday-style radio buttons first
            radios = await page.query_selector_all('input[type="radio"], [role="radio"]')
            for radio in radios:
                if not await radio.is_visible():
                    continue
                label_text = await radio.evaluate('''el => {
                    const label = el.closest('label') || el.parentElement;
                    return (label ? label.textContent : '').trim().toLowerCase();
                }''')
                if "do not want to answer" in label_text or "don't want to answer" in label_text:
                    await radio.scroll_into_view_if_needed()
                    await self.browser_manager.human_delay(200, 400)
                    await radio.click(force=True)
                    logger.info("Self-identify: selected disability = 'I do not want to answer'")
                    target_clicked = True
                    break

            if not target_clicked:
                for radio in radios:
                    if not await radio.is_visible():
                        continue
                    label_text = await radio.evaluate('''el => {
                        const label = el.closest('label') || el.parentElement;
                        return (label ? label.textContent : '').trim().toLowerCase();
                    }''')
                    if "do not have a disability" in label_text or ("no" in label_text and "disability" in label_text):
                        await radio.scroll_into_view_if_needed()
                        await self.browser_manager.human_delay(200, 400)
                        await radio.click(force=True)
                        logger.info("Self-identify: selected disability = 'No'")
                        target_clicked = True
                        break
        except Exception as e:
            logger.debug(f"Error handling disability radios: {e}")

        # Also try checkboxes (some forms use checkboxes instead of radios)
        if not target_clicked:
            try:
                # Use JS to find disability checkbox options by their label text
                target_clicked = await page.evaluate('''
                    () => {
                        // Find all checkbox-like elements and their labels
                        const preferOrder = [
                            "i do not want to answer",
                            "do not want to answer",
                            "do not wish to answer",
                            "do not have a disability",
                        ];

                        // Strategy 1: Find labels containing target text, then click associated checkbox
                        const labels = document.querySelectorAll('label');
                        for (const target of preferOrder) {
                            for (const lbl of labels) {
                                const text = lbl.textContent.trim().toLowerCase();
                                if (text.includes(target)) {
                                    // Click the label itself (Workday checkboxes respond to label clicks)
                                    lbl.click();
                                    return true;
                                }
                            }
                        }

                        // Strategy 2: Find checkboxes and their nearby text
                        const checkboxes = document.querySelectorAll('input[type="checkbox"]');
                        for (const target of preferOrder) {
                            for (const cb of checkboxes) {
                                if (cb.offsetParent === null) continue;
                                const parent = cb.closest('label') || cb.parentElement;
                                const text = (parent ? parent.textContent : '').trim().toLowerCase();
                                if (text.includes(target)) {
                                    cb.click();
                                    return true;
                                }
                            }
                        }

                        // Strategy 3: role="checkbox" elements
                        const roleCheckboxes = document.querySelectorAll('[role="checkbox"]');
                        for (const target of preferOrder) {
                            for (const cb of roleCheckboxes) {
                                if (cb.offsetParent === null) continue;
                                const text = cb.textContent.trim().toLowerCase();
                                const aria = (cb.getAttribute('aria-label') || '').toLowerCase();
                                if (text.includes(target) || aria.includes(target)) {
                                    cb.click();
                                    return true;
                                }
                            }
                        }

                        return false;
                    }
                ''')
                if target_clicked:
                    logger.info("Self-identify: checked disability via JS click")
                    await self.browser_manager.human_delay(300, 500)
            except Exception as e:
                logger.warning(f"Self-identify: error handling disability checkboxes: {e}")

        # Fallback: try Playwright click on the checkbox or its label
        if not target_clicked:
            try:
                prefer_texts = [
                    "I do not want to answer",
                    "No, I do not have a disability",
                ]
                for text in prefer_texts:
                    if target_clicked:
                        break
                    # Try clicking the label text directly
                    label = await page.query_selector(f'label:has-text("{text}")')
                    if label and await label.is_visible():
                        await label.scroll_into_view_if_needed()
                        await self.browser_manager.human_delay(200, 400)
                        await label.click()
                        logger.info(f"Self-identify: clicked disability label = '{text}'")
                        target_clicked = True
                        break
                    # Try text selector on any element
                    elem = await page.query_selector(f'text="{text}"')
                    if elem and await elem.is_visible():
                        await elem.scroll_into_view_if_needed()
                        await self.browser_manager.human_delay(200, 400)
                        await elem.click()
                        logger.info(f"Self-identify: clicked disability text = '{text}'")
                        target_clicked = True
                        break
            except Exception as e:
                logger.debug(f"Self-identify: fallback click failed: {e}")

        if not target_clicked:
            logger.warning("Self-identify: could not find disability radio/checkbox to select")

        # Scroll back to top so nav button is accessible
        await page.evaluate("window.scrollTo(0, 0)")
        await self.browser_manager.human_delay(300, 500)

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

            company_input = await page.query_selector('[data-automation-id="company"], [data-automation-id="companyName"]')
            if company_input and await company_input.is_visible():
                await company_input.fill(exp.get("company", ""))
                self._fields_filled["companyName"] = exp.get("company", "")

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
            if school and "school" not in self._fields_filled:
                # Check if school is already filled (from resume upload or account)
                school_already = await page.evaluate('''() => {
                    // Check for selected items in school field
                    const schoolFields = document.querySelectorAll(
                        '[data-automation-id*="school" i], [id*="school" i]'
                    );
                    for (const f of schoolFields) {
                        const container = f.closest('[data-automation-id^="formField"]')
                            || f.closest('[data-automation-id^="education"]')
                            || f.parentElement?.parentElement;
                        if (container) {
                            const selected = container.querySelector('[data-automation-id="selectedItemList"]');
                            if (selected && selected.textContent.trim()) return selected.textContent.trim();
                        }
                    }
                    return '';
                }''')
                if school_already:
                    logger.info(f"Education: School already filled: '{school_already}'")
                    self._fields_filled["school"] = school_already
                else:
                    school_selectors = [
                        'input[data-automation-id="school"]',
                        'input[data-automation-id*="school" i]',
                        'input[id*="school" i]',
                    ]
                    # Build alternative school names for typeahead search
                    # Different Workday tenants list schools differently
                    school_alts = self._build_school_alternatives(school)

                    filled = False
                    for sch_sel in school_selectors:
                        elem = await page.query_selector(sch_sel)
                        if elem and await elem.is_visible():
                            logger.info(f"Education: School input found via '{sch_sel}'")
                            filled = await self._fill_workday_typeahead_elem(
                                page, elem, school, alt_values=school_alts
                            )
                            if filled:
                                self._fields_filled["school"] = school
                                break
                        elif elem:
                            logger.debug(f"Education: School input found but not visible: '{sch_sel}'")
                    if not filled:
                        # Workday says: "If you do not see your school, please type and select 'Other'."
                        logger.info(f"Education: School typeahead failed, trying 'Other' via browse icon")
                        for sch_sel in school_selectors:
                            school_input = await page.query_selector(sch_sel)
                            if school_input and await school_input.is_visible():
                                # Click the browse-all icon (hamburger menu ≡) next to school input
                                # This opens a full list including "Other"
                                browse_clicked = False
                                try:
                                    browse_icon = await school_input.evaluate_handle('''el => {
                                        const parent = el.parentElement;
                                        if (parent) {
                                            return parent.querySelector(
                                                'button[data-automation-id="promptSearchButton"], ' +
                                                'button[aria-label*="search"], ' +
                                                'button'
                                            );
                                        }
                                        return null;
                                    }''')
                                    is_null = await browse_icon.evaluate("el => el === null")
                                    if not is_null:
                                        icon_elem = browse_icon.as_element()
                                        if icon_elem and await icon_elem.is_visible():
                                            await icon_elem.click()
                                            await self.browser_manager.human_delay(2000, 3000)
                                            browse_clicked = True
                                            logger.info("Education: Clicked browse-all icon for school")
                                except Exception as e:
                                    logger.debug(f"Browse icon click failed: {e}")

                                # Now try "Other" or "School Not Found" in the typeahead
                                for fallback_name in ["Other", "School Not Found", "Not Found", "Not Listed"]:
                                    other_filled = await self._fill_workday_typeahead_elem(
                                        page, school_input, fallback_name
                                    )
                                    if other_filled:
                                        self._fields_filled["school"] = fallback_name
                                        logger.info(f"Education: School → '{fallback_name}' ('{school}' not in list)")
                                        break
                                if "school" in self._fields_filled:
                                    break

                                # Last resort: type school name and press Enter
                                # Some tenants: Enter selects the typed value directly
                                # Others (Leidos): Enter opens a search dialog with radio buttons
                                for short_search in [school, school.split()[0] if " " in school else school]:
                                    await page.keyboard.press("Escape")
                                    await self.browser_manager.human_delay(300, 500)
                                    await school_input.fill("")
                                    await self.browser_manager.human_delay(200, 400)
                                    await school_input.type(short_search, delay=50)
                                    await self.browser_manager.human_delay(500, 1000)
                                    await page.keyboard.press("Enter")
                                    await self.browser_manager.human_delay(2000, 3000)

                                    # Strategy A: Check for search dialog popup (Leidos-style)
                                    # These have labels with radio buttons inside a popup/dialog
                                    dialog_labels = await page.query_selector_all(
                                        '[data-automation-id="popupContent"] label, '
                                        '[role="dialog"] label, '
                                        '[data-automation-id="promptOption"]'
                                    )
                                    if dialog_labels and len(dialog_labels) >= 2:
                                        logger.info(f"Enter opened search dialog: {len(dialog_labels)} items")
                                        # Refine search in dialog if search box exists
                                        search_box = await page.query_selector(
                                            '[data-automation-id="searchBox"] input, '
                                            'input[placeholder*="Search" i], '
                                            '[role="dialog"] input[type="text"], '
                                            '[data-automation-id="popupContent"] input[type="text"]'
                                        )
                                        if search_box and await search_box.is_visible():
                                            await search_box.fill("")
                                            await search_box.type(school[:25], delay=60)
                                            await self.browser_manager.human_delay(2000, 3000)
                                            # Re-fetch after refined search
                                            dialog_labels = await page.query_selector_all(
                                                '[data-automation-id="popupContent"] label, '
                                                '[role="dialog"] label, '
                                                '[data-automation-id="promptOption"]'
                                            )
                                            logger.info(f"Refined search: {len(dialog_labels)} items")

                                        # Collect texts and find match
                                        school_lower = school.lower()
                                        found_school = False
                                        for lbl in dialog_labels[:50]:
                                            try:
                                                text = (await lbl.text_content() or "").strip()
                                                if not text or len(text) < 3:
                                                    continue
                                                if school_lower in text.lower():
                                                    await lbl.click()
                                                    await self.browser_manager.human_delay(500, 800)
                                                    found_school = True
                                                    # Click OK/Done/Submit button
                                                    ok_btn = await page.query_selector(
                                                        'button[data-automation-id="promptSubmit"], '
                                                        'button:has-text("OK"), button:has-text("Done")'
                                                    )
                                                    if ok_btn and await ok_btn.is_visible():
                                                        await ok_btn.click()
                                                        await self.browser_manager.human_delay(500, 800)
                                                    self._fields_filled["school"] = text
                                                    logger.info(f"Education: School from dialog: '{text}'")
                                                    break
                                            except Exception:
                                                continue
                                        if not found_school:
                                            # Try "School Not Found" / "Other"
                                            for lbl in dialog_labels[:50]:
                                                try:
                                                    text = (await lbl.text_content() or "").strip()
                                                    if any(fb in text.lower() for fb in ["not found", "other", "not listed"]):
                                                        await lbl.click()
                                                        await self.browser_manager.human_delay(500, 800)
                                                        ok_btn = await page.query_selector(
                                                            'button[data-automation-id="promptSubmit"], '
                                                            'button:has-text("OK"), button:has-text("Done")'
                                                        )
                                                        if ok_btn and await ok_btn.is_visible():
                                                            await ok_btn.click()
                                                            await self.browser_manager.human_delay(500, 800)
                                                        self._fields_filled["school"] = text
                                                        logger.info(f"Education: School → '{text}' from dialog")
                                                        break
                                                except Exception:
                                                    continue
                                        # Close dialog if still open
                                        await page.keyboard.press("Escape")
                                        await self.browser_manager.human_delay(300, 500)
                                        if "school" in self._fields_filled:
                                            break

                                    # Strategy B: Check if Enter selected value directly (NGC-style)
                                    school_selected = await school_input.evaluate('''el => {
                                        const container = el.closest('[data-automation-id^="formField"]')
                                            || el.parentElement?.parentElement?.parentElement;
                                        if (container) {
                                            const selected = container.querySelector('[data-automation-id="selectedItemList"]');
                                            if (selected && selected.textContent.trim()) return selected.textContent.trim();
                                        }
                                        return '';
                                    }''')
                                    if school_selected:
                                        self._fields_filled["school"] = school_selected
                                        logger.info(f"Education: School selected via Enter: '{school_selected}'")
                                        break

                                if "school" not in self._fields_filled:
                                    logger.warning(f"Education: School not selectable ('{school}')")
                                break

            # Degree — also typeahead
            degree = edu.get("degree", "")
            if degree and "degree" not in self._fields_filled:
                filled = await self._fill_workday_typeahead(
                    page, '[data-automation-id="degree"]', degree
                )
                if not filled:
                    degree_input = await page.query_selector('[data-automation-id="degree"]')
                    if degree_input and await degree_input.is_visible():
                        await degree_input.fill(degree)
                if filled:
                    self._fields_filled["degree"] = degree

            # Field of study — typeahead (ID varies: "education-NNN--fieldOfStudy")
            field_of_study = edu.get("field_of_study", "")
            if field_of_study and "field_of_study" not in self._fields_filled:
                # Check if FoS is already selected (chip visible)
                fos_already = await page.evaluate('''() => {
                    const inputs = document.querySelectorAll('input[id*="fieldOfStudy"]');
                    for (const inp of inputs) {
                        const container = inp.closest('[data-automation-id^="formField"]')
                            || inp.closest('[data-automation-id^="education"]')
                            || inp.parentElement?.parentElement;
                        if (container) {
                            const selected = container.querySelector('[data-automation-id="selectedItemList"]');
                            if (selected && selected.textContent.trim()) return selected.textContent.trim();
                        }
                    }
                    return '';
                }''')
                if fos_already:
                    logger.info(f"Education: Field of Study already filled: '{fos_already}'")
                    self._fields_filled["field_of_study"] = fos_already
                else:
                    pass  # Fall through to the filling logic below

            if field_of_study and "field_of_study" not in self._fields_filled:
                # Use JS to find the element — CSS selectors may miss it
                fos_info = await page.evaluate('''
                    () => {
                        // Strategy 1: ID contains fieldOfStudy
                        let el = document.querySelector('input[id*="fieldOfStudy"]');
                        if (el) return {found: true, id: el.id, tag: el.tagName, strategy: 'id'};
                        // Strategy 2: data-automation-id
                        el = document.querySelector('[data-automation-id*="fieldOfStudy"]');
                        if (el) return {found: true, id: el.id || '', tag: el.tagName, strategy: 'data-auto'};
                        // Strategy 3: Label-based search
                        const labels = document.querySelectorAll('label, [data-automation-id="formLabel"]');
                        for (const l of labels) {
                            const text = (l.textContent || "").trim().toLowerCase();
                            if (text.includes("field of study")) {
                                const container = l.closest('[data-automation-id^="formField"]') || l.parentElement?.parentElement;
                                if (container) {
                                    const input = container.querySelector('input');
                                    if (input) return {found: true, id: input.id || '', tag: input.tagName, strategy: 'label'};
                                }
                                return {found: false, labelFound: true, text: text};
                            }
                        }
                        return {found: false, labelFound: false};
                    }
                ''')
                logger.info(f"Education: Field of Study search result: {fos_info}")

                fos_elem = None
                if fos_info.get("found"):
                    strategy = fos_info.get("strategy")
                    if strategy == "id":
                        fos_elem = await page.query_selector(f'input[id*="fieldOfStudy"]')
                    elif strategy == "data-auto":
                        fos_elem = await page.query_selector('[data-automation-id*="fieldOfStudy"]')
                    elif strategy == "label":
                        eid = fos_info.get("id", "")
                        if eid:
                            fos_elem = await page.query_selector(f'#{eid}')
                        else:
                            # Fallback: use evaluate_handle
                            handle = await page.evaluate_handle('''
                                () => {
                                    const labels = document.querySelectorAll('label, [data-automation-id="formLabel"]');
                                    for (const l of labels) {
                                        const text = (l.textContent || "").trim().toLowerCase();
                                        if (text.includes("field of study")) {
                                            const container = l.closest('[data-automation-id^="formField"]') || l.parentElement?.parentElement;
                                            if (container) {
                                                return container.querySelector('input');
                                            }
                                        }
                                    }
                                    return null;
                                }
                            ''')
                            is_null = await handle.evaluate("el => el === null")
                            if not is_null:
                                fos_elem = handle.as_element()

                if fos_elem and await fos_elem.is_visible():
                    filled = await self._fill_workday_typeahead_elem(page, fos_elem, field_of_study)
                    if filled:
                        self._fields_filled["field_of_study"] = field_of_study
                        logger.info(f"Education: Field of Study → '{field_of_study}'")
                    else:
                        logger.warning("Education: Could not fill Field of Study via typeahead")
                elif fos_elem:
                    logger.debug("Education: Field of Study element found but not visible")
                else:
                    logger.debug("Education: Field of Study element not found on page")

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

    def _build_school_alternatives(self, school: str) -> list:
        """Build alternative school name formats for Workday typeahead search.

        Different Workday tenants list the same school differently:
        - "San Jose State University" vs "San José State University" vs
          "San Jose State Univ" vs "SJSU" vs "California State University, San Jose"
        """
        alts = []
        school_lower = school.lower()

        # Common abbreviation mappings
        abbrev_map = {
            "san jose state university": ["San José State University", "SJSU", "San Jose State", "California State University San Jose", "San Jose State Univ"],
            "university of california": ["UC"],
            "california institute of technology": ["Caltech", "Cal Tech"],
            "massachusetts institute of technology": ["MIT"],
            "georgia institute of technology": ["Georgia Tech"],
            "university of southern california": ["USC"],
        }

        # Check for known school
        for full_name, abbreviations in abbrev_map.items():
            if full_name in school_lower:
                alts.extend(abbreviations)
                break

        # Generic alternatives: drop "University", try abbreviation
        words = school.split()
        if len(words) >= 3:
            # Drop last word (e.g. "University" → "San Jose State")
            alts.append(" ".join(words[:-1]))
        if len(words) >= 4:
            # Drop last two words
            alts.append(" ".join(words[:-2]))

        # Try with "Univ" instead of "University"
        if "University" in school:
            alts.append(school.replace("University", "Univ"))

        # Try initials (e.g. "SJSU")
        if len(words) >= 2:
            initials = "".join(w[0].upper() for w in words if w[0].isupper())
            if len(initials) >= 2 and initials not in alts:
                alts.append(initials)

        # Deduplicate, exclude original
        seen = {school.lower()}
        result = []
        for alt in alts:
            if alt.lower() not in seen:
                seen.add(alt.lower())
                result.append(alt)
        return result

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
        "phone number", "phone extension", "country phone code", "region phone code", "phone device type",
        "first name", "last name", "preferred name", "legal name", "middle name",
        "email address", "address line 1", "address line 2",
        "postal code", "zip code",
        "country phone",
        "facebook share", "twitter share", "linkedin share",
        "type to add skills",
        "upload a file", "drop files here",
        # Auth page fields — never treat as application questions
        "password", "verify new password", "verify password", "create account",
        "enter website",  # honeypot field
        # Name prefix/suffix dropdowns — handled by _fill_generic_workday_dropdowns
        "prefix", "suffix",
        # Handled by dedicated methods in _fill_dropdowns
        "how did you hear",
        # Handled by _fill_education (skip only the education-section specific labels)
        "school or university", "overall result",
    }

    async def _handle_custom_questions(self, page: Page, job_data: Dict[str, Any]) -> None:
        """Handle Workday custom questions (text, textarea, dropdowns, radio)."""
        questions = await page.query_selector_all(
            '[data-automation-id="questionItem"], '
            '.WJLB, '
            '[data-automation-id*="question"], '
            '[data-automation-id^="formField"]'
        )

        logger.info(f"_handle_custom_questions: found {len(questions)} question containers")

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
                    try:
                        await dropdown_btn.scroll_into_view_if_needed(timeout=3000)
                    except Exception:
                        pass
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

                        if answer == "__SKIP__":
                            logger.info(f"Skipping dropdown: '{question_text[:50]}' (no answer needed)")
                            continue
                        if answer:
                            # Re-open dropdown to select
                            try:
                                await dropdown_btn.scroll_into_view_if_needed(timeout=3000)
                            except Exception:
                                pass
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
                    'input[data-automation-id*="Date"], '
                    'input[data-automation-id*="dateSectionMonth"], '
                    'input[data-automation-id*="startDate"], '
                    'input[data-automation-id*="endDate"]'
                )
                is_date_question = any(x in q_lower for x in [
                    "date available", "date of", "start date", "end date",
                    "from*", "to*", "to (actual", "graduation date", "expected",
                    "when could you start", "could you start", "available to start",
                    "earliest start", "earliest date",
                ]) or (q_lower.strip().rstrip("*") in ("from", "to", "date"))
                if not date_input and is_date_question:
                    # Try to find any input that looks like a date field (placeholder with MM or YYYY)
                    date_input = await q.query_selector(
                        'input[placeholder*="MM"], input[placeholder*="YYYY"], input[placeholder*="DD"], '
                        'input:not([type="radio"]):not([type="checkbox"]):not([type="file"]):not([placeholder="Search"])'
                    )
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
                            # Detect date format based on data-automation-id and placeholder
                            placeholder = (await date_input.get_attribute("placeholder") or "").strip()
                            max_attr = await date_input.get_attribute("maxlength") or ""
                            auto_id = (await date_input.get_attribute("data-automation-id") or "").lower()

                            # Determine date format: year-only, month/year, or full date
                            is_year_only = (
                                "datesectionyear" in auto_id
                                or placeholder == "YYYY"
                                or (max_attr and max_attr.isdigit() and int(max_attr) <= 4)
                            )
                            is_month_year = (
                                not is_year_only
                                and (
                                    "datesectionmonth" in auto_id
                                    or "MM/YYYY" in placeholder or "MM / YYYY" in placeholder
                                    or (placeholder and "YYYY" in placeholder and "DD" not in placeholder and "MM" in placeholder)
                                    or (max_attr and max_attr.isdigit() and int(max_attr) <= 7 and int(max_attr) > 4)
                                )
                            )
                            logger.info(f"Date field placeholder='{placeholder}' maxlen='{max_attr}' auto_id='{auto_id}' year_only={is_year_only} month_year={is_month_year}")

                            if is_year_only:
                                # YYYY format (education year fields)
                                import re as _re
                                year_match = _re.search(r'(\d{4})', answer)
                                if year_match:
                                    formatted = year_match.group(1)
                                else:
                                    # Extract year from "Month YYYY" format
                                    parts = answer.strip().split()
                                    formatted = parts[-1] if len(parts) >= 2 and parts[-1].isdigit() else "2026"
                                date_digits = formatted  # Just YYYY (4 digits)
                            elif is_month_year:
                                # MM/YYYY format (work experience From/To)
                                formatted = self._format_date(answer)  # Returns "MM/YYYY"
                                date_digits = formatted.replace("/", "")  # "MMYYYY" (6 digits)
                            else:
                                # MM/DD/YYYY format (full date)
                                formatted = self._format_date_for_workday(answer)
                                date_digits = formatted.replace("/", "")  # "MMDDYYYY" (8 digits)
                            logger.info(f"Date answer: '{answer}' → formatted: '{formatted}' digits: '{date_digits}'")

                            try:
                                await date_input.scroll_into_view_if_needed(timeout=3000)
                            except Exception:
                                pass

                            # Strategy 1: Focus + Home + Delete + type digits only
                            # Masked inputs auto-insert slashes, so we type just digits
                            await date_input.focus()
                            await self.browser_manager.human_delay(100, 200)
                            await page.keyboard.press("Home")
                            for _ in range(12):
                                await page.keyboard.press("Delete")
                            await self.browser_manager.human_delay(100, 200)
                            await page.keyboard.type(date_digits, delay=100)
                            await self.browser_manager.human_delay(300, 500)
                            new_val = (await date_input.input_value() or "").strip()
                            logger.info(f"Date after digit typing: '{new_val}'")

                            # Validate: check min length based on format
                            min_len = 4 if is_year_only else (7 if is_month_year else 8)
                            def _date_looks_bad(val):
                                if not val or len(val) < min_len:
                                    return True
                                if "DD" in val or "YYYY" in val or "MM" in val:
                                    return True
                                # Check for bad year (before 2000)
                                yr = re.search(r'(\d{4})', val)
                                if yr and int(yr.group(1)) < 2000:
                                    return True
                                return False

                            if _date_looks_bad(new_val):
                                # Strategy 2: Triple-click + type full formatted date
                                try:
                                    await date_input.click(click_count=3, timeout=3000)
                                    await self.browser_manager.human_delay(100, 200)
                                    await page.keyboard.type(formatted, delay=80)
                                    await self.browser_manager.human_delay(200, 300)
                                except Exception:
                                    logger.debug("Date click timeout, trying JS setter")
                                new_val = (await date_input.input_value() or "").strip()
                                logger.info(f"Date after type(formatted): '{new_val}'")

                            if _date_looks_bad(new_val):
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
                                logger.info(f"Date after nativeSetter: '{new_val}'")

                            if _date_looks_bad(new_val):
                                # Strategy 4: Use the date picker button if available
                                # Some Workday tenants have a calendar button next to the date input
                                date_btn = await q.query_selector('[data-automation-id="dateDropdown"], button[aria-label*="calendar"], button[aria-label*="Calendar"]')
                                if date_btn and await date_btn.is_visible():
                                    logger.info("Date: trying calendar button approach")
                                    # Just try select-all + type again with Ctrl+A
                                    await date_input.focus()
                                    await page.keyboard.press("Control+a")
                                    await self.browser_manager.human_delay(100, 200)
                                    await page.keyboard.type(formatted, delay=50)
                                    await self.browser_manager.human_delay(200, 300)
                                    new_val = (await date_input.input_value() or "").strip()
                                    logger.info(f"Date after Ctrl+A type: '{new_val}'")

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
                        logger.debug(f"Text field already filled: '{question_text[:40]}' = '{current[:20]}'")
                        continue  # Already filled

                    max_len = 500 if tag == "textarea" else 200
                    logger.info(f"Custom Q text: filling '{question_text[:50]}' ({tag})")
                    answer = await self.ai_answerer.answer_question(
                        question_text, tag, max_length=max_len
                    )
                    if answer == "__SKIP__":
                        logger.info(f"Skipping text field: '{question_text[:50]}' (no answer needed)")
                        continue
                    logger.info(f"Custom Q text answer: '{answer}' (type={type(answer).__name__})")
                    if answer:
                        try:
                            await input_elem.scroll_into_view_if_needed(timeout=3000)
                        except Exception:
                            pass
                        try:
                            await input_elem.click(timeout=3000)
                        except Exception:
                            # If click fails, try focus + fill directly
                            await input_elem.focus()
                        await self.browser_manager.human_delay(100, 250)
                        # Short answers: type humanly. Long answers: fill (more reliable)
                        if len(answer) <= 60:
                            await input_elem.type(answer, delay=random.randint(35, 80))
                        else:
                            await input_elem.fill(answer)
                        self._fields_filled[question_text[:40]] = answer[:30]
                        logger.info(f"Custom Q text: '{question_text[:50]}' → '{answer[:30]}'")
                    else:
                        logger.warning(f"Custom Q text: no answer for '{question_text[:50]}'")
                    await self.browser_manager.human_delay(300, 700)
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

                # --- Checkbox groups (multi-select questions like "which reasons") ---
                checkboxes = await q.query_selector_all('input[type="checkbox"], [role="checkbox"]')
                visible_cbs = []
                already_checked_count = 0
                for cb in checkboxes:
                    try:
                        if await cb.is_visible():
                            checked = await cb.evaluate("el => el.checked || el.getAttribute('aria-checked') === 'true'")
                            if checked:
                                already_checked_count += 1
                            else:
                                visible_cbs.append(cb)
                    except Exception:
                        continue

                if visible_cbs:
                    qt = question_text.lower()
                    if any(x in qt for x in ["reason", "important", "which of these", "select all", "check all"]):
                        # Skip if some are already checked (prevent over-checking on retries)
                        if already_checked_count >= 2:
                            logger.debug(f"Checkbox group already has {already_checked_count} checked, skipping: '{question_text[:50]}'")
                        else:
                            # Pick first 2-3 options (safe, generic choices)
                            for cb in visible_cbs[:3]:
                                try:
                                    await cb.click(force=True)
                                    await self.browser_manager.human_delay(200, 400)
                                except Exception:
                                    try:
                                        await cb.evaluate("el => el.click()")
                                    except Exception:
                                        pass
                            logger.info(f"Custom Q checkbox group: checked {min(3, len(visible_cbs))} options for '{question_text[:50]}'")
                    elif any(x in qt for x in ["consent", "agree", "acknowledge", "accept"]):
                        # Consent checkbox — check it (only if not already checked)
                        if already_checked_count == 0:
                            try:
                                await visible_cbs[0].click(force=True)
                            except Exception:
                                await visible_cbs[0].evaluate("el => el.click()")
                            logger.info(f"Custom Q consent checkbox: '{question_text[:50]}'")
                    await self.browser_manager.human_delay(200, 400)
                    continue

                # No matching input type found for this container
                logger.info(f"No input/dropdown/radio found for: '{question_text[:60]}'")

            except Exception as e:
                logger.warning(f"Error handling Workday question '{question_text[:40]}': {e}")

        if handled_count > 0:
            logger.info(f"Handled {handled_count} custom questions")

        # Catch-all pass: find ALL visible empty required textareas/inputs on the page
        # Some Workday tenants render text fields outside of formField containers
        try:
            all_textareas = await page.query_selector_all(
                'textarea, input[type="text"]:not([placeholder="Search"]):not([data-automation-id*="phone"]):not([data-automation-id*="Phone"])'
            )
            for ta in all_textareas:
                try:
                    if not await ta.is_visible():
                        continue
                    current = (await ta.input_value() or "").strip()
                    if current:
                        continue
                    # Get the label text from nearby elements
                    label_text = await ta.evaluate('''el => {
                        // Walk up to find a label
                        let parent = el.parentElement;
                        for (let i = 0; i < 5 && parent; i++) {
                            const label = parent.querySelector('label, legend, [data-automation-id="formLabel"], [data-automation-id="richText"]');
                            if (label && label.textContent.trim().length > 2) {
                                return label.textContent.trim();
                            }
                            // Check previous sibling
                            const prev = parent.previousElementSibling;
                            if (prev && prev.textContent.trim().length > 2 && prev.textContent.trim().length < 200) {
                                return prev.textContent.trim();
                            }
                            parent = parent.parentElement;
                        }
                        return '';
                    }''')
                    if not label_text or label_text.lower() in seen_labels:
                        continue
                    seen_labels.add(label_text.lower())
                    answer = await self.ai_answerer.answer_question(label_text, "textarea", max_length=500)
                    if answer:
                        await ta.click()
                        await self.browser_manager.human_delay(100, 250)
                        if len(answer) <= 60:
                            await ta.type(answer, delay=random.randint(35, 80))
                        else:
                            await ta.fill(answer)
                        self._fields_filled[label_text[:40]] = answer[:30]
                        logger.info(f"Custom Q textarea (catch-all): '{label_text[:50]}' → '{answer[:30]}'")
                        await self.browser_manager.human_delay(300, 700)
                except Exception as e:
                    logger.debug(f"Error in textarea catch-all: {e}")
        except Exception as e:
            logger.debug(f"Error scanning textareas: {e}")

        # Catch-all pass: find ALL visible unfilled dropdown buttons ("Select One") on the page
        # Some Workday tenants render consent/prefix dropdowns outside question containers
        try:
            all_dd_btns = await page.query_selector_all(
                'button[aria-haspopup="listbox"]:not([data-automation-id="dateDropdown"])'
            )
            for dd_btn in all_dd_btns:
                try:
                    if not await dd_btn.is_visible():
                        continue
                    btn_text = (await dd_btn.text_content() or "").strip().lower()
                    if btn_text not in ("select one", "select", "choose", "--select--", ""):
                        continue  # Already has a selection

                    # Get nearby label text via JS DOM traversal
                    nearby_label = await dd_btn.evaluate('''el => {
                        // Walk up to find a formField container
                        let parent = el.parentElement;
                        for (let i = 0; i < 8 && parent; i++) {
                            // Check for labels
                            const label = parent.querySelector('label, legend, [data-automation-id="formLabel"], [data-automation-id="richText"]');
                            if (label && label.textContent.trim().length > 2) {
                                return label.textContent.trim();
                            }
                            parent = parent.parentElement;
                        }
                        // Try aria-label on the button itself
                        return el.getAttribute("aria-label") || "";
                    }''')
                    if not nearby_label:
                        continue
                    nl = nearby_label.lower()
                    if nl in seen_labels:
                        continue

                    # Determine answer based on label text
                    answer = None
                    if any(x in nl for x in ["consent", "agree", "acknowledge", "accept"]):
                        answer = "Yes|I Agree|I Accept|Agree|Accept"
                    elif "prefix" in nl and len(nl) < 40:
                        answer = "Mr."
                    elif "suffix" in nl and len(nl) < 40:
                        continue  # Suffix is optional — skip
                    elif any(x in nl for x in ["sponsor", "visa"]):
                        answer = "No"
                    elif any(x in nl for x in ["authorized", "eligible", "right to work"]):
                        answer = "Yes"
                    elif any(x in nl for x in ["relocat"]):
                        answer = "Yes"
                    elif any(x in nl for x in ["background check"]):
                        answer = "Yes"

                    if not answer:
                        continue

                    # Open dropdown and select
                    await dd_btn.click()
                    await self.browser_manager.human_delay(400, 700)

                    candidates = [v.strip() for v in answer.split("|")]
                    items = await page.query_selector_all('[data-automation-id="menuItem"], [role="option"]')
                    item_texts = [(await it.text_content() or "").strip() for it in items]

                    matched = False
                    for candidate in candidates:
                        for i, text in enumerate(item_texts):
                            if candidate.lower() == text.lower() or candidate.lower() in text.lower():
                                await items[i].click()
                                self._fields_filled[nearby_label[:40]] = text[:30]
                                logger.info(f"Dropdown catch-all: '{nearby_label[:50]}' → '{text[:30]}'")
                                matched = True
                                break
                        if matched:
                            break

                    if not matched:
                        # Try first non-empty option as fallback for required fields
                        if items:
                            await items[0].click()
                            self._fields_filled[nearby_label[:40]] = item_texts[0][:30] if item_texts else "first"
                            logger.info(f"Dropdown catch-all fallback: '{nearby_label[:50]}' → '{item_texts[0][:30] if item_texts else '?'}'")
                        else:
                            await page.keyboard.press("Escape")

                    await self.browser_manager.human_delay(200, 400)
                    seen_labels.add(nl)

                except Exception as e:
                    logger.debug(f"Error in dropdown catch-all for button: {e}")
                    try:
                        await page.keyboard.press("Escape")
                    except Exception:
                        pass
        except Exception as e:
            logger.debug(f"Error scanning dropdowns: {e}")

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
        config = job_data.get("config", {})

        # Check for school typeahead error (common cause of page stall)
        try:
            school_error = await page.evaluate('''() => {
                const fields = document.querySelectorAll('[data-automation-id*="school" i], [id*="school" i]');
                for (const f of fields) {
                    const parent = f.closest('[data-automation-id^="formField"]')
                        || f.closest('[data-automation-id^="education"]')
                        || f.parentElement?.parentElement?.parentElement;
                    if (!parent) continue;
                    const err = parent.querySelector('[data-automation-id="errorMessage"]');
                    const isInvalid = f.getAttribute('aria-invalid') === 'true';
                    const selected = parent.querySelector('[data-automation-id="selectedItemList"]');
                    const hasSelection = selected && selected.textContent.trim();
                    if ((err || isInvalid) && !hasSelection) return true;
                }
                return false;
            }''')
            if school_error and "school" not in self._fields_filled:
                logger.info("Validation fix: retrying school typeahead")
                await self._fill_education(page, config)
        except Exception as e:
            logger.debug(f"Error checking school validation: {e}")

        # First pass: find ALL visible empty textareas and text inputs that have error styling
        try:
            empty_fields = await page.query_selector_all('textarea, input[type="text"]')
            for field in empty_fields:
                try:
                    if not await field.is_visible():
                        continue
                    current = (await field.input_value() or "").strip()
                    if current:
                        continue
                    # Check if this field has an error indicator nearby
                    has_error = await field.evaluate('''el => {
                        const parent = el.closest('[data-automation-id^="formField"]') || el.parentElement?.parentElement;
                        if (!parent) return false;
                        const errText = parent.textContent?.toLowerCase() || '';
                        return errText.includes('error') || errText.includes('required') ||
                               el.getAttribute('aria-invalid') === 'true' ||
                               parent.querySelector('[data-automation-id="errorMessage"]') !== null;
                    }''')
                    if not has_error:
                        continue
                    # Get label for this field
                    label_text = await field.evaluate('''el => {
                        const parent = el.closest('[data-automation-id^="formField"]') || el.parentElement?.parentElement;
                        if (!parent) return '';
                        const label = parent.querySelector('label, legend, [data-automation-id="formLabel"], [data-automation-id="richText"]');
                        return label ? label.textContent.trim() : '';
                    }''')
                    if not label_text:
                        continue
                    tag = await field.evaluate("el => el.tagName.toLowerCase()")
                    max_len = 500 if tag == "textarea" else 200
                    answer = await self.ai_answerer.answer_question(label_text, tag, max_length=max_len)
                    if answer:
                        await field.fill(answer)
                        logger.info(f"Validation fix: '{label_text[:50]}' → '{answer[:30]}'")
                except Exception as e:
                    logger.debug(f"Error fixing empty field: {e}")
        except Exception as e:
            logger.debug(f"Error in empty field scan: {e}")

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
