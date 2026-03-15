"""
CAPTCHA Solver

Handles solving reCAPTCHA (v2 invisible, v3, Enterprise) and hCaptcha
using 2captcha or anticaptcha.
Greenhouse uses reCAPTCHA Enterprise with a shared sitekey.
iCIMS uses hCaptcha.
"""

import asyncio
from typing import Optional, Dict, Any
from playwright.async_api import Page
from loguru import logger

# Greenhouse's shared reCAPTCHA Enterprise sitekey
GREENHOUSE_SITEKEY = "6LfmcbcpAAAAAChNTbhUShzUOAMj_wY9LQIvLFX0"


class CaptchaSolver:
    """Solves reCAPTCHA challenges using external solving services."""

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize CAPTCHA solver.

        Args:
            config: secrets config with captcha.service and captcha.api_key
        """
        captcha_config = config.get("captcha", {})
        self.service = captcha_config.get("service", "").lower()
        self.api_key = captcha_config.get("api_key", "")
        self._solver = None

        if self.service and self.api_key:
            self._init_solver()
        else:
            logger.warning("CAPTCHA solver not configured - set captcha.service and captcha.api_key in secrets.yaml")

    def _init_solver(self):
        """Initialize the solving service client."""
        if self.service == "2captcha":
            try:
                from twocaptcha import TwoCaptcha
                self._solver = TwoCaptcha(self.api_key)
                logger.info("2captcha solver initialized")
            except ImportError:
                logger.error("2captcha-python not installed. Run: pip install 2captcha-python")
        elif self.service == "anticaptcha":
            try:
                from python_anticaptcha import AnticaptchaClient
                self._solver = AnticaptchaClient(self.api_key)
                logger.info("AntiCaptcha solver initialized")
            except ImportError:
                logger.error("python-anticaptcha not installed. Run: pip install python-anticaptcha")
        else:
            logger.error(f"Unknown CAPTCHA service: {self.service}. Use '2captcha' or 'anticaptcha'")

    @property
    def is_configured(self) -> bool:
        """Check if solver is properly configured."""
        return self._solver is not None

    async def extract_sitekey(self, page: Page) -> Optional[str]:
        """Extract reCAPTCHA sitekey from a page."""
        sitekey = await page.evaluate('''() => {
            // Method 1: Check script tag render parameter (Enterprise/v3)
            const scripts = document.querySelectorAll('script[src*="recaptcha"]');
            for (const s of scripts) {
                const match = s.src.match(/render=([^&]+)/);
                if (match && match[1] !== 'explicit') return match[1];
            }

            // Method 2: Check data-sitekey attribute (exclude hCaptcha elements)
            const el = document.querySelector('[data-sitekey]:not(.h-captcha):not([data-hcaptcha-widget-id])');
            if (el && !el.closest('.h-captcha') && !el.id?.includes('hcaptcha')) return el.getAttribute('data-sitekey');

            // Method 3: Search HTML for reCAPTCHA key pattern
            const html = document.documentElement.innerHTML;
            const keyMatch = html.match(/['"]?(6L[a-zA-Z0-9_-]{38,42})['"]?/);
            if (keyMatch) return keyMatch[1];

            return null;
        }''')

        if sitekey:
            logger.debug(f"Extracted reCAPTCHA sitekey: {sitekey}")
        return sitekey

    async def detect_recaptcha_type(self, page: Page) -> Dict[str, Any]:
        """Detect what type of reCAPTCHA is on the page."""
        info = await page.evaluate('''() => {
            const result = {
                hasRecaptcha: false,
                isEnterprise: false,
                isV3: false,
                isInvisible: false,
                sitekey: null,
            };

            // Check for Enterprise
            const enterpriseScripts = document.querySelectorAll('script[src*="enterprise"]');
            if (enterpriseScripts.length > 0) {
                result.hasRecaptcha = true;
                result.isEnterprise = true;
            }

            // Check for standard reCAPTCHA scripts
            const recaptchaScripts = document.querySelectorAll('script[src*="recaptcha"]');
            if (recaptchaScripts.length > 0) {
                result.hasRecaptcha = true;
            }

            // Extract sitekey from script render param
            for (const s of recaptchaScripts) {
                const match = s.src.match(/render=([^&]+)/);
                if (match && match[1] !== 'explicit') {
                    result.sitekey = match[1];
                    result.isV3 = true;  // render= in URL means v3 or Enterprise
                }
            }

            // Check for data-sitekey (exclude hCaptcha elements)
            const sitekeyEl = document.querySelector('[data-sitekey]:not(.h-captcha):not([data-hcaptcha-widget-id])');
            if (sitekeyEl) {
                // Double-check this isn't an hCaptcha container
                const isHcaptcha = sitekeyEl.closest('.h-captcha') ||
                    sitekeyEl.querySelector('iframe[src*="hcaptcha"]') ||
                    sitekeyEl.id?.includes('hcaptcha');
                if (!isHcaptcha) {
                    result.sitekey = sitekeyEl.getAttribute('data-sitekey');
                    result.hasRecaptcha = true;
                    const size = sitekeyEl.getAttribute('data-size');
                    if (size === 'invisible') result.isInvisible = true;
                }
            }

            // Check for grecaptcha.enterprise
            if (typeof grecaptcha !== 'undefined' && typeof grecaptcha.enterprise !== 'undefined') {
                result.isEnterprise = true;
            }

            // Check for response textarea
            const responseField = document.querySelector(
                '#g-recaptcha-response, [name="g-recaptcha-response"]'
            );
            if (responseField) result.hasRecaptcha = true;

            return result;
        }''')

        logger.debug(f"reCAPTCHA detection: {info}")
        return info

    async def solve_recaptcha(self, page: Page, sitekey: Optional[str] = None) -> Optional[str]:
        """
        Solve reCAPTCHA on a page.

        Args:
            page: Playwright page with the reCAPTCHA
            sitekey: Optional sitekey override (auto-detected if not provided)

        Returns:
            Solved token string, or None if failed
        """
        if not self.is_configured:
            logger.error("CAPTCHA solver not configured")
            return None

        # Detect reCAPTCHA type
        recaptcha_info = await self.detect_recaptcha_type(page)
        if not recaptcha_info.get("hasRecaptcha"):
            logger.debug("No reCAPTCHA detected on page")
            return None

        # Get sitekey
        if not sitekey:
            sitekey = recaptcha_info.get("sitekey") or await self.extract_sitekey(page)
        if not sitekey:
            sitekey = GREENHOUSE_SITEKEY  # fallback to known Greenhouse key
            logger.debug(f"Using default Greenhouse sitekey: {sitekey}")

        page_url = page.url
        is_enterprise = recaptcha_info.get("isEnterprise", False)
        is_v3 = recaptcha_info.get("isV3", False)

        captcha_type = "Enterprise v3" if (is_enterprise and is_v3) else \
                       "Enterprise" if is_enterprise else \
                       "v3" if is_v3 else "v2"
        logger.info(f"Solving reCAPTCHA {captcha_type} for {page_url[:60]}...")

        # Solve in a thread to avoid blocking the event loop
        token = await asyncio.get_running_loop().run_in_executor(
            None, self._solve_sync, sitekey, page_url, is_enterprise, is_v3
        )

        if token:
            logger.info(f"reCAPTCHA solved (token: {token[:30]}...)")
        else:
            logger.error("Failed to solve reCAPTCHA")

        return token

    def _solve_sync(self, sitekey: str, page_url: str, is_enterprise: bool, is_v3: bool = False) -> Optional[str]:
        """Synchronous reCAPTCHA solving (runs in thread pool)."""
        if self.service == "2captcha":
            return self._solve_2captcha(sitekey, page_url, is_enterprise, is_v3)
        elif self.service == "anticaptcha":
            return self._solve_anticaptcha(sitekey, page_url, is_enterprise)
        return None

    def _solve_2captcha(self, sitekey: str, page_url: str, is_enterprise: bool, is_v3: bool = False) -> Optional[str]:
        """Solve using 2captcha service."""
        try:
            params = {
                "sitekey": sitekey,
                "url": page_url,
            }

            if is_enterprise:
                params["enterprise"] = 1
                # Enterprise v3 (score-based) — e.g. Greenhouse
                # Must specify version and action for score-based captchas
                params["version"] = "v3"
                params["action"] = "submit"
                params["min_score"] = 0.9
                logger.debug(f"Sending to 2captcha: Enterprise v3, sitekey={sitekey[:20]}..., action=submit, min_score=0.9")
            elif is_v3:
                params["version"] = "v3"
                params["action"] = "submit"
                params["min_score"] = 0.7
                logger.debug(f"Sending to 2captcha: v3, sitekey={sitekey[:20]}..., action=submit")
            else:
                params["invisible"] = 1
                logger.debug(f"Sending to 2captcha: v2 invisible, sitekey={sitekey[:20]}...")

            result = self._solver.recaptcha(**params)
            token = result.get("code") if isinstance(result, dict) else str(result)
            if token and len(token) > 20:
                return token
            logger.warning(f"2captcha returned unexpected result: {result}")
            return None
        except Exception as e:
            logger.error(f"2captcha solving failed: {e}")
            return None

    def _solve_anticaptcha(self, sitekey: str, page_url: str, is_enterprise: bool) -> Optional[str]:
        """Solve using AntiCaptcha service."""
        try:
            from python_anticaptcha import (
                RecaptchaV2EnterpriseTask,
                RecaptchaV2Task,
            )

            if is_enterprise:
                task = RecaptchaV2EnterpriseTask(page_url, sitekey, is_invisible=True)
            else:
                task = RecaptchaV2Task(page_url, sitekey, is_invisible=True)

            job = self._solver.createTask(task)
            job.join()
            return job.get_solution_response()
        except Exception as e:
            logger.error(f"AntiCaptcha solving failed: {e}")
            return None

    async def inject_token(self, page: Page, token: str) -> bool:
        """
        Inject a solved reCAPTCHA token into the page.

        For Greenhouse Enterprise reCAPTCHA, this overrides grecaptcha.enterprise.execute
        to return our solved token instead of making a real call to Google.

        Args:
            page: Playwright page
            token: Solved reCAPTCHA token

        Returns:
            True if token was injected successfully
        """
        safe_token = token.replace("\\", "\\\\").replace("'", "\\'").replace('"', '\\"').replace("\n", "\\n")

        result = await page.evaluate(f'''() => {{
            const TOKEN = "{safe_token}";
            let injected = false;

            // 1. Set token in all g-recaptcha-response textareas
            const textareas = document.querySelectorAll(
                '[name="g-recaptcha-response"], [id*="g-recaptcha-response"]'
            );
            for (const ta of textareas) {{
                ta.value = TOKEN;
                ta.innerHTML = TOKEN;
                injected = true;
            }}

            // 2. If no textarea found, create one in the form
            if (!injected) {{
                const form = document.querySelector('form');
                if (form) {{
                    const ta = document.createElement('textarea');
                    ta.name = 'g-recaptcha-response';
                    ta.id = 'g-recaptcha-response';
                    ta.style.display = 'none';
                    ta.value = TOKEN;
                    form.appendChild(ta);
                    injected = true;
                }}
            }}

            // 3. Override grecaptcha.enterprise.execute to return our token
            //    Handle both execute() and execute(sitekey, options) signatures
            try {{
                if (typeof grecaptcha !== 'undefined' && grecaptcha.enterprise) {{
                    grecaptcha.enterprise.execute = function() {{
                        return Promise.resolve(TOKEN);
                    }};
                    // Also override ready() to immediately call callback
                    if (typeof grecaptcha.enterprise.ready === 'function') {{
                        const origReady = grecaptcha.enterprise.ready;
                        grecaptcha.enterprise.ready = function(cb) {{
                            if (typeof cb === 'function') cb();
                        }};
                    }}
                    injected = true;
                }}
            }} catch(e) {{}}

            // 4. Also override standard grecaptcha if present
            try {{
                if (typeof grecaptcha !== 'undefined') {{
                    if (typeof grecaptcha.execute === 'function') {{
                        grecaptcha.execute = function() {{
                            return Promise.resolve(TOKEN);
                        }};
                    }}
                    if (typeof grecaptcha.getResponse === 'function') {{
                        grecaptcha.getResponse = function() {{
                            return TOKEN;
                        }};
                    }}
                    injected = true;
                }}
            }} catch(e) {{}}

            // 5. Hide any reCAPTCHA error elements that might block submit
            try {{
                const errors = document.querySelectorAll('.grecaptcha-error, [class*="recaptcha-error"]');
                errors.forEach(el => {{ el.style.display = 'none'; el.textContent = ''; }});
            }} catch(e) {{}}

            // 6. Also set any recaptcha_response_field hidden inputs
            try {{
                const hiddenFields = document.querySelectorAll(
                    'input[name="recaptcha_response_field"], input[name="g-recaptcha-response-v3"], input[id*="recaptcha"]'
                );
                hiddenFields.forEach(f => {{ f.value = TOKEN; }});
            }} catch(e) {{}}

            return injected;
        }}''')

        if result:
            logger.info("reCAPTCHA token injected into page")
        else:
            logger.warning("Failed to inject reCAPTCHA token")

        return result

    # ── hCaptcha Support ─────────────────────────────────────────────────

    async def detect_hcaptcha(self, page: Page) -> Dict[str, Any]:
        """Detect hCaptcha on the page (including iframes)."""
        # Check main page first
        info = await page.evaluate('''() => {
            const result = { hasHcaptcha: false, sitekey: null };

            // Check for hCaptcha script
            const scripts = document.querySelectorAll('script[src*="hcaptcha"]');
            if (scripts.length > 0) result.hasHcaptcha = true;

            // Check for hCaptcha container with data-sitekey
            const el = document.querySelector('[data-sitekey], .h-captcha[data-sitekey]');
            if (el) {
                result.hasHcaptcha = true;
                result.sitekey = el.getAttribute('data-sitekey');
            }

            // Check for hCaptcha iframe
            const iframe = document.querySelector('iframe[src*="hcaptcha.com"]');
            if (iframe) {
                result.hasHcaptcha = true;
                const src = iframe.getAttribute('src') || '';
                const match = src.match(/sitekey=([^&]+)/);
                if (match) result.sitekey = match[1];
            }

            // Check for h-captcha-response textarea
            const response = document.querySelector('[name="h-captcha-response"], #h-captcha-response');
            if (response) result.hasHcaptcha = true;

            return result;
        }''')

        # Also check all iframes (hCaptcha often nested in iframes on iCIMS)
        if not info.get("hasHcaptcha"):
            for frame in page.frames:
                try:
                    frame_info = await frame.evaluate('''() => {
                        const result = { hasHcaptcha: false, sitekey: null };
                        const iframe = document.querySelector('iframe[src*="hcaptcha.com"]');
                        if (iframe) {
                            result.hasHcaptcha = true;
                            const src = iframe.getAttribute('src') || '';
                            const match = src.match(/sitekey=([^&]+)/);
                            if (match) result.sitekey = match[1];
                        }
                        const el = document.querySelector('[data-sitekey], .h-captcha');
                        if (el) {
                            result.hasHcaptcha = true;
                            result.sitekey = el.getAttribute('data-sitekey') || result.sitekey;
                        }
                        return result;
                    }''')
                    if frame_info.get("hasHcaptcha"):
                        info = frame_info
                        break
                except Exception:
                    continue

        if info.get("hasHcaptcha"):
            logger.debug(f"hCaptcha detected: sitekey={info.get('sitekey', 'unknown')}")
        return info

    async def solve_hcaptcha(self, page: Page, sitekey: Optional[str] = None) -> Optional[str]:
        """Solve hCaptcha on a page."""
        if not self.is_configured:
            logger.error("CAPTCHA solver not configured")
            return None

        hcaptcha_info = await self.detect_hcaptcha(page)
        if not hcaptcha_info.get("hasHcaptcha"):
            logger.debug("No hCaptcha detected on page")
            return None

        if not sitekey:
            sitekey = hcaptcha_info.get("sitekey")
        if not sitekey:
            logger.warning("hCaptcha detected but no sitekey found")
            return None

        page_url = page.url
        logger.info(f"Solving hCaptcha for {page_url[:60]}... (sitekey: {sitekey[:20]}...)")

        token = await asyncio.get_running_loop().run_in_executor(
            None, self._solve_hcaptcha_sync, sitekey, page_url
        )

        if token:
            logger.info(f"hCaptcha solved (token: {token[:30]}...)")
        else:
            logger.error("Failed to solve hCaptcha")

        return token

    def _solve_hcaptcha_sync(self, sitekey: str, page_url: str) -> Optional[str]:
        """Synchronous hCaptcha solving."""
        if self.service == "2captcha":
            return self._solve_hcaptcha_2captcha(sitekey, page_url)
        elif self.service == "anticaptcha":
            return self._solve_hcaptcha_anticaptcha(sitekey, page_url)
        return None

    def _solve_hcaptcha_2captcha(self, sitekey: str, page_url: str) -> Optional[str]:
        """Solve hCaptcha using 2captcha."""
        try:
            logger.debug(f"Sending hCaptcha to 2captcha: sitekey={sitekey[:20]}...")
            result = self._solver.hcaptcha(sitekey=sitekey, url=page_url)
            token = result.get("code") if isinstance(result, dict) else str(result)
            if token and len(token) > 20:
                return token
            logger.warning(f"2captcha hCaptcha returned unexpected result: {result}")
            return None
        except Exception as e:
            logger.error(f"2captcha hCaptcha solving failed: {e}")
            return None

    def _solve_hcaptcha_anticaptcha(self, sitekey: str, page_url: str) -> Optional[str]:
        """Solve hCaptcha using AntiCaptcha."""
        try:
            from python_anticaptcha import HCaptchaTask
            task = HCaptchaTask(page_url, sitekey)
            job = self._solver.createTask(task)
            job.join()
            return job.get_solution_response()
        except Exception as e:
            logger.error(f"AntiCaptcha hCaptcha solving failed: {e}")
            return None

    async def inject_hcaptcha_token(self, page: Page, token: str) -> bool:
        """Inject a solved hCaptcha token into the page."""
        safe_token = token.replace("\\", "\\\\").replace("'", "\\'").replace('"', '\\"').replace("\n", "\\n")

        # Try injecting in main page and all frames
        injected = False
        targets = [page] + list(page.frames)

        for target in targets:
            try:
                result = await target.evaluate(f'''() => {{
                    const TOKEN = "{safe_token}";
                    let injected = false;

                    // Set h-captcha-response textarea
                    const textareas = document.querySelectorAll(
                        '[name="h-captcha-response"], [id*="h-captcha-response"], textarea[name*="captcha"]'
                    );
                    for (const ta of textareas) {{
                        ta.value = TOKEN;
                        ta.innerHTML = TOKEN;
                        injected = true;
                    }}

                    // Also set g-recaptcha-response (hCaptcha sometimes uses this name)
                    const gTextareas = document.querySelectorAll('[name="g-recaptcha-response"]');
                    for (const ta of gTextareas) {{
                        ta.value = TOKEN;
                        ta.innerHTML = TOKEN;
                        injected = true;
                    }}

                    // If no textarea found, create one
                    if (!injected) {{
                        const form = document.querySelector('form');
                        if (form) {{
                            const ta = document.createElement('textarea');
                            ta.name = 'h-captcha-response';
                            ta.style.display = 'none';
                            ta.value = TOKEN;
                            form.appendChild(ta);

                            // Also create g-recaptcha-response (some sites check both)
                            const ta2 = document.createElement('textarea');
                            ta2.name = 'g-recaptcha-response';
                            ta2.style.display = 'none';
                            ta2.value = TOKEN;
                            form.appendChild(ta2);
                            injected = true;
                        }}
                    }}

                    // Try to call hcaptcha callback if available
                    try {{
                        if (typeof hcaptcha !== 'undefined') {{
                            // Find the widget ID
                            const widgets = document.querySelectorAll('.h-captcha, [data-hcaptcha-widget-id]');
                            if (widgets.length > 0) {{
                                // Trigger the verification callback
                                const event = new CustomEvent('hcaptcha-verified', {{ detail: {{ token: TOKEN }} }});
                                document.dispatchEvent(event);
                            }}
                        }}
                    }} catch(e) {{}}

                    return injected;
                }}''')

                if result:
                    injected = True
                    break
            except Exception:
                continue

        if injected:
            logger.info("hCaptcha token injected into page")
        else:
            logger.warning("Failed to inject hCaptcha token")

        return injected

    async def solve_and_inject_hcaptcha(self, page: Page, sitekey: Optional[str] = None) -> bool:
        """Full flow: solve hCaptcha and inject the token."""
        token = await self.solve_hcaptcha(page, sitekey)
        if not token:
            return False
        return await self.inject_hcaptcha_token(page, token)

    # ── Universal solve_and_inject ───────────────────────────────────────

    async def solve_and_inject(self, page: Page, sitekey: Optional[str] = None) -> bool:
        """
        Full flow: detect CAPTCHA type, solve it, and inject the token.
        Supports both reCAPTCHA and hCaptcha.

        Args:
            page: Playwright page
            sitekey: Optional sitekey override

        Returns:
            True if CAPTCHA was solved and token injected
        """
        # Check for hCaptcha first (it's more specific)
        hcaptcha_info = await self.detect_hcaptcha(page)
        if hcaptcha_info.get("hasHcaptcha"):
            return await self.solve_and_inject_hcaptcha(page, sitekey or hcaptcha_info.get("sitekey"))

        # Fall back to reCAPTCHA
        token = await self.solve_recaptcha(page, sitekey)
        if not token:
            return False

        return await self.inject_token(page, token)
