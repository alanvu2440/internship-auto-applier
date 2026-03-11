"""
Base Handler

Abstract base class for ATS-specific handlers.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
from playwright.async_api import Page
from loguru import logger


class BaseHandler(ABC):
    """Base class for ATS handlers."""

    name: str = "base"

    def __init__(self, form_filler, ai_answerer, browser_manager, dry_run: bool = False,
                 captcha_solver=None):
        """
        Initialize handler.

        Args:
            form_filler: FormFiller instance
            ai_answerer: AIAnswerer instance
            browser_manager: BrowserManager instance
            dry_run: If True, fill forms but don't submit
            captcha_solver: CaptchaSolver instance (optional)
        """
        self.form_filler = form_filler
        self.ai_answerer = ai_answerer
        self.browser_manager = browser_manager
        self.dry_run = dry_run
        self.review_mode = False  # Set by main.py — pause before submit for manual review
        self.captcha_solver = captcha_solver
        self._last_status = "failed"  # Default; handlers update on success/closed/etc.
        self._fields_filled = {}  # Track which fields were filled
        self._fields_missed = {}  # Track which fields were missed/empty

    def get_fill_result(self) -> Dict[str, Any]:
        """Get the fill result for the last application attempt."""
        return {"filled": self._fields_filled, "missed": self._fields_missed}

    async def pause_for_review(self, page: Page, company: str = "", role: str = ""):
        """Pause and wait for user to review the form before submitting.

        In review mode, the bot fills everything then waits for you to:
        1. Look at the form in the browser
        2. Fix anything that's wrong
        3. Press Enter in the terminal to let the bot submit
        4. Or type 'skip' to skip this job
        """
        if not self.review_mode:
            return True  # Not in review mode, proceed normally

        import asyncio
        print("\n" + "=" * 60)
        print(f"  REVIEW MODE — {company} - {role}")
        print("=" * 60)
        print("  Form is filled. Check the browser window now.")
        print("  Options:")
        print("    [Enter]  = Submit the application")
        print("    [s]      = Skip this job")
        print("    [q]      = Quit the session")
        print("=" * 60)

        # Run input() in a thread so we don't block the event loop
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, input, "  Your choice: ")
        response = response.strip().lower()

        if response in ("s", "skip"):
            logger.info("User skipped this job in review mode")
            self._last_status = "skipped_by_user"
            return False
        elif response in ("q", "quit"):
            logger.info("User quit session in review mode")
            raise KeyboardInterrupt("User quit review session")

        logger.info("User approved — submitting application")
        return True

    @abstractmethod
    async def apply(self, page: Page, job_url: str, job_data: Dict[str, Any]) -> bool:
        """
        Apply to a job.

        Args:
            page: Playwright page
            job_url: URL of the job application
            job_data: Job metadata (company, role, etc.)

        Returns:
            True if application was submitted successfully
        """
        pass

    @abstractmethod
    async def detect_form_type(self, page: Page) -> str:
        """
        Detect the type of form on the page.

        Returns:
            Form type identifier (e.g., "single_page", "multi_page")
        """
        pass

    async def wait_for_page_load(self, page: Page, timeout: int = 10000):
        """Wait for page to fully load."""
        try:
            await page.wait_for_load_state("networkidle", timeout=timeout)
        except Exception as e:
            logger.debug(f"networkidle wait failed ({e}), falling back to domcontentloaded")
            await page.wait_for_load_state("domcontentloaded", timeout=timeout)

    async def has_captcha(self, page: Page) -> bool:
        """Check if page has a VISIBLE/BLOCKING CAPTCHA (not invisible reCAPTCHA)."""
        # Check for VISIBLE CAPTCHA challenges that actually block
        blocking_captcha_selectors = [
            # Visible reCAPTCHA challenge
            'iframe[src*="recaptcha"][style*="visibility: visible"]',
            'iframe[src*="hcaptcha"]:not([style*="display: none"])',
            '.g-recaptcha:not([data-size="invisible"])',
            '.h-captcha',
            '#captcha:not([style*="display: none"])',
            # CAPTCHA challenge pages
            '#challenge-running',
            '.cf-browser-verification',
            'div[class*="captcha-container"]',
            # Blocking modal
            '[class*="captcha-modal"]',
        ]

        for selector in blocking_captcha_selectors:
            try:
                elem = await page.query_selector(selector)
                if elem and await elem.is_visible():
                    # Double check it's actually visible and in viewport
                    box = await elem.bounding_box()
                    if box and box['height'] > 50:  # Real CAPTCHAs have significant height
                        logger.warning(f"Blocking CAPTCHA detected: {selector}")
                        return True
            except Exception:
                continue

        # Check for CAPTCHA-specific error messages
        captcha_texts = [
            "complete the captcha",
            "verify you are human",
            "security check",
            "prove you're not a robot",
            "captcha verification",
        ]
        try:
            page_text = (await page.text_content("body") or "").lower()
            for text in captcha_texts:
                if text in page_text:
                    # Make sure it's not in job description context
                    if "we use captcha" not in page_text and "captcha service" not in page_text:
                        logger.warning(f"CAPTCHA message detected: {text}")
                        return True
        except Exception as e:
            logger.debug(f"Error checking page text for CAPTCHA: {e}")

        return False

    async def handle_captcha(self, page: Page) -> bool:
        """
        Handle CAPTCHA if present.

        Returns:
            True if CAPTCHA was solved/bypassed
        """
        if await self.has_captcha(page):
            if self.captcha_solver and self.captcha_solver.is_configured:
                logger.info("Visible CAPTCHA detected - attempting to solve...")
                solved = await self.captcha_solver.solve_and_inject(page)
                if solved:
                    logger.info("CAPTCHA solved successfully")
                    return True
                logger.error("CAPTCHA solving failed")
                return False
            logger.error("CAPTCHA blocking application - no solver configured")
            return False
        return True

    async def solve_invisible_recaptcha(self, page: Page) -> bool:
        """
        Solve invisible reCAPTCHA or hCaptcha before form submission.

        Called right before clicking submit to inject a valid token
        for pages that use invisible reCAPTCHA / reCAPTCHA Enterprise / hCaptcha.

        Returns:
            True if solved (or no CAPTCHA present), False on failure
        """
        if not self.captcha_solver or not self.captcha_solver.is_configured:
            logger.debug("No CAPTCHA solver configured - submitting without token")
            return True

        # Check for hCaptcha first (Lever uses hCaptcha)
        hcaptcha_info = await self.captcha_solver.detect_hcaptcha(page)
        if hcaptcha_info.get("hasHcaptcha"):
            logger.info("hCaptcha detected - solving before submit...")
            return await self.captcha_solver.solve_and_inject_hcaptcha(
                page, hcaptcha_info.get("sitekey")
            )

        # Then check for reCAPTCHA
        recaptcha_info = await self.captcha_solver.detect_recaptcha_type(page)
        if not recaptcha_info.get("hasRecaptcha"):
            logger.debug("No CAPTCHA on page - proceeding with submit")
            return True

        logger.info("Invisible reCAPTCHA detected - solving before submit...")
        return await self.captcha_solver.solve_and_inject(page)

    async def is_application_complete(self, page: Page) -> bool:
        """Check if application was submitted successfully."""
        page_text = await page.text_content("body") or ""
        page_text_lower = page_text.lower()

        # Check for FAILURE indicators first — these override any success text
        failure_indicators = [
            "no longer accepting",
            "position is closed",
            "position has been filled",
            "no longer available",
            "this job has expired",
            "this job is no longer",
            "flagged as possible spam",
            "flagged as spam",
            "suspicious activity",
            "already applied",
            "already submitted",
            "you have already submitted an application",
            "duplicate application",
            "previously applied",
            "application already exists",
            "page not found",
            "page you are looking for doesn't exist",
            "page you are looking for doesn",
            "job is no longer posted",
            "this position is no longer",
            "this role has been filled",
            "this requisition has been closed",
            "posting has been removed",
            # Workday auth pages — never mark as success
            "password requirements:",
            "verify new password",
            "create your candidate home account",
        ]

        for indicator in failure_indicators:
            if indicator in page_text_lower:
                logger.debug(f"is_application_complete: matched failure indicator '{indicator}'")
                return False

        success_indicators = [
            "thank you for applying",
            "thanks for applying",
            "application received",
            "application submitted",
            "successfully applied",
            "we've received your application",
            "application complete",
            "thank you for your interest in",
            "thank you for submitting",
        ]

        for indicator in success_indicators:
            if indicator in page_text_lower:
                return True

        # Fallback: bare "thank you" only if page has few words (likely a confirmation page)
        if "thank you" in page_text_lower and len(page_text_lower.split()) < 200:
            return True

        return False

    async def get_error_message(self, page: Page) -> Optional[str]:
        """Get any actual error message displayed on the page."""
        error_selectors = [
            '.error-message',
            '.form-error',
            '.alert-danger',
            '.field-error-message',
            '[data-qa="error"]',
        ]

        errors = []
        for selector in error_selectors:
            elements = await page.query_selector_all(selector)
            for element in elements:
                try:
                    if not await element.is_visible():
                        continue
                    text = (await element.text_content() or "").strip()
                    # Only count as error if there's actual text content
                    if text and len(text) > 2:
                        errors.append(text)
                except Exception:
                    continue

        # Also check [role="alert"] but be more strict
        alert_elements = await page.query_selector_all('[role="alert"]')
        for element in alert_elements:
            try:
                if not await element.is_visible():
                    continue
                text = (await element.text_content() or "").strip()
                # Must have real error text, not just empty alert containers
                if text and len(text) > 5:
                    errors.append(text)
            except Exception:
                continue

        if errors:
            return "; ".join(errors[:3])  # Return first 3 errors max
        return None

    async def check_required_fields_filled(self, page: Page) -> list:
        """Check all required fields are filled before submit. Returns list of empty required field labels.

        This is a SAFETY CHECK to prevent submitting incomplete forms,
        which triggers spam/fraud detection on ATS platforms.
        """
        try:
            empty = await page.evaluate('''() => {
                const empty = [];
                // Check text/email/tel inputs
                const inputs = document.querySelectorAll(
                    'input:not([type="hidden"]):not([type="file"]):not([type="radio"]):not([type="checkbox"]):not([type="submit"]), textarea'
                );
                for (const inp of inputs) {
                    if (inp.getBoundingClientRect().width === 0) continue;
                    let labelText = "";
                    let p = inp.parentElement;
                    for (let i = 0; i < 4 && p; i++) {
                        const l = p.querySelector("label, legend, [class*='label']");
                        if (l && l.textContent.trim()) { labelText = l.textContent.trim(); break; }
                        p = p.parentElement;
                    }
                    const req = inp.required || inp.getAttribute("aria-required") === "true" || labelText.includes("*");
                    if (req && (!inp.value || !inp.value.trim())) {
                        empty.push(labelText || inp.name || inp.id || "?");
                    }
                }
                // Check required file inputs (resume)
                for (const inp of document.querySelectorAll('input[type="file"]')) {
                    let labelText = "";
                    let p = inp.parentElement;
                    for (let i = 0; i < 5 && p; i++) {
                        const l = p.querySelector("label, legend, [class*='label']");
                        if (l && l.textContent.trim()) { labelText = l.textContent.trim(); break; }
                        p = p.parentElement;
                    }
                    if ((labelText.includes("*") || /resume|cv/i.test(labelText)) && !inp.files.length) {
                        empty.push(labelText || "Resume");
                    }
                }
                return empty;
            }''')
            return empty or []
        except Exception:
            return []

    async def scroll_to_bottom(self, page: Page):
        """Scroll to bottom of page."""
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await self.browser_manager.human_delay(500, 1000)

    async def take_screenshot(self, page: Page, name: str):
        """Take a screenshot for debugging."""
        try:
            await page.screenshot(path=f"logs/{name}.png")
            logger.debug(f"Screenshot saved: logs/{name}.png")
        except Exception as e:
            logger.debug(f"Failed to take screenshot: {e}")

    async def is_job_closed(self, page: Page) -> bool:
        """Check if job posting is closed/unavailable."""
        closed_indicators = [
            "position has been filled",
            "no longer accepting",
            "job has been closed",
            "this position is closed",
            "this job is no longer available",
            "job posting has expired",
            "requisition has been closed",
            "role has been filled",
            "sorry, we couldn't find",
            "page not found",
            "job not found",
            "this posting has closed",
            "no longer available",
            "this job has been removed",
            "application is no longer active",
            "oops, you've gone too far",  # SmartRecruiters 404
            "sorry, this job has expired",  # SmartRecruiters expired
            "the page you are looking for doesn't exist",  # Workday 404
            "page you are looking for doesn",  # Workday 404 variant
            "something went wrong",  # Workday already-applied / error state
            "this position is no longer",
            "this role is no longer",
            "job is no longer posted",
            "posting has been removed",
            "this opening has been filled",
            "you have already submitted",
            "already applied to this",
        ]

        try:
            current_url = page.url.lower()

            # Greenhouse ?error=true redirect means job is gone
            if "error=true" in current_url and ("greenhouse" in current_url or "greenhouse" in getattr(self, 'name', '')):
                logger.info("Job appears closed: Greenhouse ?error=true redirect")
                return True

            page_text = (await page.text_content("body") or "").lower()
            for indicator in closed_indicators:
                if indicator in page_text:
                    logger.info(f"Job appears closed: found '{indicator}'")
                    return True

            # Check for explicit 404 page (h1 heading, not just "404" in body text
            # which can match job IDs like 744000104...)
            has_404_heading = await page.query_selector(
                'h1:has-text("404"), h1:has-text("Page Not Found"), '
                'h1:has-text("Not Found"), [class*="404"]'
            )
            if has_404_heading:
                logger.info("Job appears closed: 404 page detected")
                return True

            # Greenhouse-specific redirect checks — only apply to Greenhouse URLs
            if "greenhouse" in current_url or "greenhouse" in getattr(self, 'name', ''):
                if "greenhouse" not in current_url:
                    # Redirected away from Greenhouse to a company careers page
                    if any(x in current_url for x in ["/careers", "/openings"]):
                        logger.info(f"Job appears closed: redirected to company careers page ({current_url})")
                        return True
                    # Redirected to company homepage (no path or just /)
                    from urllib.parse import urlparse
                    parsed = urlparse(current_url)
                    if parsed.path.rstrip("/") == "":
                        logger.info(f"Job appears closed: redirected to company homepage ({current_url})")
                        return True

                # Check if Greenhouse page has no form elements
                has_form = await page.query_selector('form, input[name="first_name"], #application_form')
                has_apply_btn = await page.query_selector('a:has-text("Apply"), button:has-text("Apply")')
                if not has_form and not has_apply_btn:
                    body_len = len(page_text) if page_text else 0
                    if body_len < 500:
                        logger.info("Job appears closed: Greenhouse page with minimal content")
                        return True

        except Exception as e:
            logger.debug(f"Error checking job closed status: {e}")

        return False

    async def dismiss_popups(self, page: Page) -> None:
        """Dismiss common popups (cookie consent, newsletters, etc.)."""
        popup_selectors = [
            # Cookie consent
            'button:has-text("Accept")',
            'button:has-text("Accept All")',
            'button:has-text("I Accept")',
            'button:has-text("Got it")',
            'button:has-text("OK")',
            '[id*="cookie"] button',
            '[class*="cookie"] button',
            '[id*="consent"] button',
            # Newsletter/notification
            'button[aria-label="Close"]',
            'button[aria-label="Dismiss"]',
            '[class*="modal"] button[class*="close"]',
            '[class*="popup"] button[class*="close"]',
            '.modal-close',
            '.close-button',
            'button:has-text("No Thanks")',
            'button:has-text("Maybe Later")',
            # Generic X buttons
            'button:has-text("×")',
            'button:has-text("✕")',
        ]

        for selector in popup_selectors:
            try:
                btn = await page.query_selector(selector)
                if btn and await btn.is_visible():
                    await btn.click()
                    await self.browser_manager.human_delay(300, 600)
                    logger.debug(f"Dismissed popup with: {selector}")
            except Exception:
                continue

    async def handle_redirects(self, page: Page, original_url: str) -> bool:
        """Check if page redirected unexpectedly."""
        current_url = page.url.lower()
        original_lower = original_url.lower()

        # Check for login redirects
        if any(x in current_url for x in ['/login', '/signin', '/sso', '/auth', 'account']):
            if not any(x in original_lower for x in ['/login', '/signin', '/sso', '/auth']):
                logger.warning("Redirected to login page - job requires account")
                return False

        # Check for homepage redirect (job removed)
        from urllib.parse import urlparse
        parsed = urlparse(current_url)
        if parsed.path.rstrip("/") == "" or parsed.path.rstrip("/") in ("/careers", "/jobs"):
            if '/job' in original_lower or '/apply' in original_lower or 'greenhouse' in original_lower:
                logger.warning(f"Redirected to homepage/careers - job may be removed ({current_url})")
                return False

        # Check if redirected from Greenhouse to a completely different domain
        if "greenhouse" in original_lower and "greenhouse" not in current_url:
            logger.warning(f"Redirected away from Greenhouse to {current_url} - job may be removed")
            return False

        return True

    async def wait_for_extension_autofill(self, page: Page, timeout: int = 8000) -> bool:
        """Click Simplify Copilot autofill button and wait for it to fill fields.

        Finds the Simplify widget, clicks the autofill button, waits for fields
        to stabilize, then returns what was filled. The handler should only fill
        fields that Simplify left empty afterward.

        Returns:
            True if extension was detected and filled at least one field
        """
        import asyncio
        try:
            # Give extension 2s to inject after navigation before we start polling
            await asyncio.sleep(2)

            # Scroll slightly so Simplify can see the page content and activate
            try:
                await page.evaluate("window.scrollBy(0, 200)")
                await asyncio.sleep(0.5)
                await page.evaluate("window.scrollTo(0, 0)")
            except Exception:
                pass

            # Poll up to 4s for Simplify shadow root or icon to appear
            simplify_host = None
            simplify_selectors = [
                'simplify-jobs-shadow-root',
                '[class*="simplify-icon"]',
                '#simplify-sidebar',
                '[data-simplify]',
                '[class*="simplify"]',
            ]
            for _ in range(8):
                for sel in simplify_selectors:
                    el = await page.query_selector(sel)
                    if el:
                        simplify_host = el
                        logger.debug(f"Simplify detected via selector: {sel}")
                        break
                if simplify_host:
                    break
                await asyncio.sleep(0.5)

            if not simplify_host:
                logger.debug("Simplify extension not detected on page")
                return False

            # Debug: dump all simplify-related elements so we can find the right click target
            debug_info = await page.evaluate("""() => {
                const results = [];
                // All elements with simplify in class/id/tag
                for (const el of document.querySelectorAll('[class*="simplify"], [id*="simplify"], simplify-jobs-shadow-root')) {
                    results.push({
                        tag: el.tagName,
                        id: el.id,
                        className: el.className?.toString()?.substring(0, 100),
                        text: el.textContent?.trim()?.substring(0, 50),
                        hasShadow: !!el.shadowRoot
                    });
                }
                return results;
            }""")
            for item in debug_info[:5]:
                logger.info(f"[SIMPLIFY DOM] {item}")

            logger.info("Simplify extension detected — clicking autofill button...")

            # Snapshot fields BEFORE clicking
            before_snapshot = await page.evaluate("""() => {
                const result = {};
                for (const el of document.querySelectorAll('input:not([type="hidden"]):not([type="submit"]):not([type="file"]), textarea, select')) {
                    const key = el.id || el.name || el.getAttribute('data-qa') || el.placeholder || '';
                    if (key) result[key] = el.value || '';
                }
                return result;
            }""")

            # The Simplify widget uses an OPEN shadow DOM — we can access it via element.shadowRoot.
            # The widget shows a floating panel (resume match %). Clicking it opens the full sidebar.
            # The Autofill button appears inside the sidebar's shadow root after panel opens.
            autofill_clicked = False

            async def _find_autofill_in_shadows() -> dict:
                """Search ALL simplify shadow roots for Autofill button and click it."""
                return await page.evaluate("""() => {
                    const hosts = document.querySelectorAll('.simplify-jobs-shadow-root, [class*="simplify-jobs"]');
                    const allTexts = [];
                    for (const host of hosts) {
                        const shadow = host.shadowRoot;
                        if (!shadow) continue;
                        const clickables = shadow.querySelectorAll('button, [role="button"], a');
                        for (const el of clickables) {
                            const text = el.textContent?.trim() || '';
                            allTexts.push(text.substring(0, 60));
                            if (/autofill|auto.?fill/i.test(text)) {
                                el.click();
                                return {found: true, clicked: text, method: 'shadow_autofill_text'};
                            }
                        }
                    }
                    return {found: false, allTexts: allTexts.slice(0, 15)};
                }""")

            async def _open_simplify_panel() -> bool:
                """Click the Simplify widget to open the full panel."""
                return await page.evaluate("""() => {
                    const hosts = document.querySelectorAll('.simplify-jobs-shadow-root');
                    for (const host of hosts) {
                        const shadow = host.shadowRoot;
                        if (!shadow) continue;
                        const clickables = shadow.querySelectorAll('button, [role="button"], a');
                        for (const el of clickables) {
                            const rect = el.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0) {
                                el.click();
                                return true;
                            }
                        }
                    }
                    return false;
                }""")

            # Step 1: Try to find Autofill button directly (in case panel is already open)
            result = await _find_autofill_in_shadows()
            logger.info(f"[SIMPLIFY] Initial scan: {result}")

            if result.get('found'):
                autofill_clicked = True
                logger.info(f"Simplify Autofill clicked: '{result.get('clicked')}'")
                await asyncio.sleep(2)
            else:
                # Step 2: Open the Simplify panel by clicking the widget, then search again
                opened = await _open_simplify_panel()
                logger.info(f"[SIMPLIFY] Panel open click: {opened}")
                if opened:
                    await asyncio.sleep(2)  # Wait for panel animation
                    result2 = await _find_autofill_in_shadows()
                    logger.info(f"[SIMPLIFY] Post-open scan: {result2}")
                    if result2.get('found'):
                        autofill_clicked = True
                        logger.info(f"Simplify Autofill clicked (post-open): '{result2.get('clicked')}'")
                        await asyncio.sleep(2)
                    else:
                        # Step 3: Playwright shadow-piercing selectors as fallback
                        for autofill_sel in [
                            '.simplify-jobs-shadow-root >> button:has-text("Autofill")',
                            '.simplify-jobs-shadow-root >> [role="button"]:has-text("Autofill")',
                            'button:has-text("Autofill")',
                        ]:
                            try:
                                btn = page.locator(autofill_sel).first
                                if await btn.count() > 0:
                                    await btn.click(timeout=2000)
                                    autofill_clicked = True
                                    logger.info(f"Simplify Autofill clicked via Playwright: {autofill_sel}")
                                    await asyncio.sleep(2)
                                    break
                            except Exception:
                                pass

            if not autofill_clicked:
                logger.warning("Simplify detected but could NOT click Autofill — proceeding without")
            else:
                logger.info("Simplify triggered — waiting for fields to fill...")

            # Poll until field values stabilize (Simplify fills asynchronously)
            polls = timeout // 500
            prev_filled = 0
            stable_count = 0

            for _ in range(polls):
                await asyncio.sleep(0.5)
                filled_count = await page.evaluate("""() => {
                    let count = 0;
                    for (const inp of document.querySelectorAll('input:not([type="hidden"]):not([type="submit"]):not([type="file"]), textarea')) {
                        if (inp.value && inp.value.trim()) count++;
                    }
                    return count;
                }""")
                if filled_count > prev_filled:
                    prev_filled = filled_count
                    stable_count = 0
                else:
                    stable_count += 1
                    if stable_count >= 3:  # 1.5s stable = done filling
                        break

            # Diff: find which fields Simplify actually changed
            after_snapshot = await page.evaluate("""() => {
                const result = {};
                for (const el of document.querySelectorAll('input:not([type="hidden"]):not([type="submit"]):not([type="file"]), textarea, select')) {
                    const key = el.id || el.name || el.getAttribute('data-qa') || el.placeholder || '';
                    if (key) result[key] = el.value || '';
                }
                return result;
            }""")

            newly_filled = {k: v for k, v in after_snapshot.items()
                           if v and not before_snapshot.get(k)}
            changed = {k: v for k, v in after_snapshot.items()
                      if v and before_snapshot.get(k) and before_snapshot[k] != v}

            net_filled = len(newly_filled) + len(changed)
            logger.info(f"Simplify filled {net_filled} fields ({len(newly_filled)} new, {len(changed)} updated)")

            # Flag anything Simplify filled that looks wrong
            for field_key, value in {**newly_filled, **changed}.items():
                low_key = field_key.lower()
                if 'email' in low_key and '@' not in value:
                    logger.warning(f"[SIMPLIFY FLAG] email field '{field_key}' has bad value: '{value}'")
                elif ('first' in low_key or 'last' in low_key or 'name' in low_key) and len(value) > 40:
                    logger.warning(f"[SIMPLIFY FLAG] name field '{field_key}' looks long: '{value}'")

            return net_filled > 0

        except Exception as e:
            logger.debug(f"Extension autofill check failed: {e}")
            return False

    async def wait_for_navigation_stable(self, page: Page, timeout: int = 5000):
        """Wait for page to stabilize after navigation."""
        try:
            await page.wait_for_load_state("networkidle", timeout=timeout)
        except Exception:
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=timeout)
            except Exception as e:
                logger.debug(f"Page load wait failed: {e}")
        await self.browser_manager.human_delay(500, 1000)
