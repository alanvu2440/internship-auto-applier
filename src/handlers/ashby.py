"""
Ashby Handler

Handles job applications on Ashby ATS.
URLs: jobs.ashbyhq.com

Ashby has a clean public API that requires NO authentication:
  - POST /api/applicationForm.info   → returns form structure (fields, required status)
  - POST /api/applicationForm.submit → accepts multipart/form-data with field values + resume

The handler uses an API-first approach and falls back to browser automation if the API fails.
"""

import re
import json
import base64
from pathlib import Path
from urllib.parse import urlparse
from typing import Dict, Any, List, Optional
from playwright.async_api import Page
from loguru import logger

from .base import BaseHandler


class AshbyHandler(BaseHandler):
    """Handler for Ashby ATS applications."""

    name = "ashby"

    ASHBY_API_BASE = "https://jobs.ashbyhq.com/api"

    # Re-enabled with fresh SJSU email (different domain from burned Gmail aliases).
    # BURNED: user@example.com, +ashby1, +ashby2, +ashby3 — all flagged
    # Rate limit: MAX 3/hour, 5+ minute gaps between applications
    ASHBY_DISABLED = False

    @property
    def _ashby_email(self):
        email = self.form_filler.config.get("personal_info", {}).get("email")
        if not email:
            logger.error("Ashby handler: email not found in config — cannot proceed")
            raise ValueError("Email required for Ashby but not configured in personal_info.email")
        return email

    # Track spam flags per session — if we get flagged, stop ALL Ashby immediately
    _spam_flag_count = 0
    MAX_SPAM_FLAGS_BEFORE_HALT = 2  # Halt after 2 retry-failures (each already retried once)

    async def apply(self, page: Page, job_url: str, job_data: Dict[str, Any]) -> bool:
        """Apply to an Ashby job. Browser-first to avoid spam detection.

        Ashby's fraud detection checks device fingerprinting, IP, email patterns,
        and phone verification. The API-first approach triggers spam flags because
        raw fetch() calls bypass normal browser interaction patterns. Using the
        browser form submission path looks more natural to their detection system.
        """
        self._last_status = "failed"
        self._fields_filled = {}
        self._fields_missed = {}
        self._resume_uploaded = False
        self._spam_retry_count = 0

        # SAFETY: Check if Ashby is disabled
        if self.ASHBY_DISABLED:
            logger.warning("Ashby handler is DISABLED — email aliases are burned. Skipping.")
            self._last_status = "skipped"
            return False

        # SAFETY: Check if we've been spam-flagged this session
        if AshbyHandler._spam_flag_count >= self.MAX_SPAM_FLAGS_BEFORE_HALT:
            logger.error(f"Ashby HALTED — {AshbyHandler._spam_flag_count} spam flags this session. Skipping all remaining.")
            self._last_status = "skipped"
            return False
        try:
            logger.info(f"Applying to Ashby job: {job_data.get('company')} - {job_data.get('role')}")

            # Browser-first approach: interact with the actual form UI
            # This generates natural device/interaction signals that Ashby's
            # fraud detection expects, avoiding "flagged as possible spam"
            browser_success = await self._apply_browser(page, job_url, job_data)
            if browser_success:
                return True

            # Fallback to API only if browser approach fails completely
            posting_id = self._extract_posting_id(job_url)
            if posting_id:
                logger.info("Browser approach failed, trying API fallback")
                return await self._apply_api(page, posting_id, job_url, job_data)

            return False

        except Exception as e:
            logger.error(f"Ashby application failed: {e}")
            await self.take_screenshot(page, f"ashby_error_{job_data.get('company', 'unknown')}")
            return False

    async def detect_form_type(self, page: Page) -> str:
        """Detect Ashby form type."""
        form = await page.query_selector('form[class*="application"], form[data-testid*="application"]')
        if form:
            return "standard"
        return "standard"

    def _extract_posting_id(self, url: str) -> Optional[str]:
        """Extract the job posting ID from an Ashby URL.

        Ashby URLs look like:
          - jobs.ashbyhq.com/{company}/{jobId}
          - jobs.ashbyhq.com/{company}/{jobId}/application
        The jobId is typically a UUID.
        """
        # Match UUID pattern in URL
        uuid_match = re.search(
            r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
            url, re.IGNORECASE
        )
        if uuid_match:
            return uuid_match.group(0)

        # Fallback: parse path segments and find the ID
        # Strip /application suffix, then take last segment
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")
        # Remove /application suffix if present
        if path.endswith("/application"):
            path = path[: -len("/application")]
        segments = path.split("/")
        if len(segments) >= 2:
            return segments[-1]

        return None

    async def _apply_api(self, page: Page, posting_id: str, job_url: str, job_data: Dict[str, Any]) -> bool:
        """Apply using Ashby's public API.

        IMPORTANT: We must navigate to the Ashby domain FIRST so that
        page.evaluate fetch calls are made from the same origin (jobs.ashbyhq.com).
        Without this, fetch calls fail due to CORS restrictions.
        """
        try:
            config = self.form_filler.config

            # Step 0: Navigate to the Ashby job page so we're on the same origin.
            # Use wait_until="commit" to avoid hanging on SPA hydration which can
            # cause TargetClosedError when the React app re-renders.
            logger.debug(f"Navigating to Ashby job page for same-origin API access: {job_url}")
            try:
                await page.goto(job_url, wait_until="commit", timeout=30000)
                # Wait for the page to settle — Ashby is a React SPA
                await self.browser_manager.human_delay(2000, 3000)
                # Ensure we're on the right origin
                await page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception as nav_err:
                logger.warning(f"Navigation issue: {nav_err}")
                try:
                    await page.goto(job_url, wait_until="commit", timeout=15000)
                    await self.browser_manager.human_delay(2000, 3000)
                except Exception as e2:
                    logger.error(f"Ashby navigation failed on retry: {e2}")
                    return False

            # Step 1: Get form info to understand required fields
            form_info = await self._get_form_info(page, posting_id)
            if not form_info:
                logger.warning("Could not fetch Ashby form info")
                return False

            # Step 2: Build form data mapping config values to field IDs
            form_fields = form_info.get("formDefinition", {}).get("sections", [])
            field_values, field_tracking = self._map_fields_to_values(form_fields, config, job_data)

            # Track what was filled/missed
            self._fields_filled = field_tracking.get("filled", {})
            self._fields_missed = field_tracking.get("missed", {})

            if self.dry_run:
                logger.info(f"DRY RUN: Would submit Ashby API with fields: {list(field_values.keys())}")
                self._last_status = "success"
                return True

            # Human-like pause before submitting (simulate reading/reviewing form)
            await self.browser_manager.human_delay(3000, 6000)

            # Step 3: Submit via API
            success = await self._submit_api(page, posting_id, field_values, config)
            if success:
                logger.info("Ashby application submitted successfully via API!")
                self._last_status = "success"
                return True

            return False

        except Exception as e:
            logger.warning(f"Ashby API approach failed: {e}")
            return False

    async def _get_form_info(self, page: Page, posting_id: str) -> Optional[Dict]:
        """Fetch form structure from applicationForm.info API.

        PREREQUISITE: page must already be navigated to jobs.ashbyhq.com
        so that the fetch is same-origin and avoids CORS issues.
        """
        try:
            result = await page.evaluate('''async (postingId) => {
                try {
                    const resp = await fetch("/api/applicationForm.info", {
                        method: "POST",
                        headers: {"Content-Type": "application/json"},
                        body: JSON.stringify({jobPostingId: postingId})
                    });
                    if (!resp.ok) {
                        return {error: "HTTP " + resp.status, ok: false};
                    }
                    return await resp.json();
                } catch (e) {
                    return {error: e.message, ok: false};
                }
            }''', posting_id)

            if not result:
                logger.debug("Ashby form info returned null")
                return None

            if result.get("error"):
                logger.debug(f"Ashby form info error: {result['error']}")
                return None

            if result.get("success"):
                return result.get("results", result)

            logger.debug(f"Ashby form info response not successful: {result}")
            return None
        except Exception as e:
            logger.debug(f"Failed to fetch Ashby form info: {e}")
            return None

    def _map_fields_to_values(self, sections: List[Dict], config: Dict, job_data: Dict) -> tuple:
        """Map Ashby form fields to config values. Returns (field_values, tracking)."""
        personal = config.get("personal_info", {})
        education = config.get("education", [{}])[0] if config.get("education") else {}
        work_auth = config.get("work_authorization", {})

        # Use email from config
        ashby_email = self._ashby_email

        # Standard field type mappings
        type_to_value = {
            "FirstName": personal.get("first_name", ""),
            "LastName": personal.get("last_name", ""),
            "Email": ashby_email,
            "Phone": f"{personal.get('phone_prefix', '')}{personal.get('phone', '')}".replace("+", ""),
            "LinkedIn": personal.get("linkedin", ""),
            "GitHub": personal.get("github", ""),
            "Portfolio": personal.get("portfolio", ""),
            "CurrentCompany": "",
            "Location": f"{personal.get('city', '')}, {personal.get('state', '')}".strip(", "),
            "School": education.get("school", ""),
            "Degree": education.get("degree", ""),
            "FieldOfStudy": education.get("field_of_study", ""),
        }

        # Label-based pattern matching for custom fields
        label_patterns = {
            r"first.?name": personal.get("first_name", ""),
            r"last.?name": personal.get("last_name", ""),
            r"e?-?mail": ashby_email,
            r"phone|mobile|cell": f"{personal.get('phone_prefix', '')}{personal.get('phone', '')}".replace("+", ""),
            r"linkedin": personal.get("linkedin", ""),
            r"github": personal.get("github", ""),
            r"portfolio|website": personal.get("portfolio", ""),
            r"school|university|college": education.get("school", ""),
            r"degree": education.get("degree", ""),
            r"major|field.?of.?study": education.get("field_of_study", ""),
            r"gpa": education.get("gpa", ""),
            r"graduation": education.get("graduation_date", ""),
            r"authorized.?to.?work|work.?auth": "Yes" if work_auth.get("us_work_authorized") else "No",
            r"sponsor": "No" if not work_auth.get("require_sponsorship_now") else "Yes",
        }

        field_values = {}
        tracking = {"filled": {}, "missed": {}}

        for section in sections:
            fields = section.get("fieldEntries", section.get("fields", []))
            for field in fields:
                field_id = field.get("id", field.get("fieldId", ""))
                field_type = field.get("type", field.get("fieldType", ""))
                field_title = field.get("title", field.get("label", ""))
                field_title_lower = field_title.lower()
                is_required = field.get("isRequired", False)

                # Try type-based mapping first
                if field_type in type_to_value and type_to_value[field_type]:
                    field_values[field_id] = type_to_value[field_type]
                    tracking["filled"][field_title or field_type] = type_to_value[field_type][:40]
                    continue

                # Try label-based pattern matching
                matched = False
                for pattern, value in label_patterns.items():
                    if re.search(pattern, field_title_lower, re.IGNORECASE) and value:
                        field_values[field_id] = value
                        tracking["filled"][field_title or field_type] = str(value)[:40]
                        matched = True
                        break

                if not matched and is_required:
                    tracking["missed"][field_title or field_type] = f"required ({field_type})"
                    logger.debug(f"Unmapped required Ashby field: {field_title} (type: {field_type})")

        return field_values, tracking

    async def _submit_api(self, page: Page, posting_id: str, field_values: Dict, config: Dict) -> bool:
        """Submit application via Ashby API using multipart/form-data.

        PREREQUISITE: page must already be navigated to jobs.ashbyhq.com
        so that the fetch is same-origin and avoids CORS issues.

        Ashby's submit endpoint accepts multipart/form-data with:
          - jobPostingId: the posting UUID
          - applicationForm.fields.<fieldId>: field values
          - applicationForm.resumeFile: the resume file (as a File object in FormData)
        """
        try:
            resume_path = config.get("files", {}).get("resume", "")
            resume_b64 = ""
            resume_filename = ""

            # Read resume file and base64-encode it for transfer into browser context
            if resume_path:
                resolved = Path(resume_path).expanduser().resolve()
                if resolved.exists():
                    resume_b64 = base64.b64encode(resolved.read_bytes()).decode("ascii")
                    resume_filename = resolved.name
                    logger.debug(f"Resume loaded for API upload: {resume_filename} ({len(resume_b64)} b64 chars)")
                else:
                    logger.warning(f"Resume file not found: {resolved}")

            result = await page.evaluate('''async (args) => {
                const [postingId, fieldValues, resumeB64, resumeFilename] = args;
                try {
                    const formData = new FormData();
                    formData.append("jobPostingId", postingId);

                    // Add field values
                    for (const [key, value] of Object.entries(fieldValues)) {
                        formData.append("applicationForm.fields." + key, value);
                    }

                    // Add resume file if provided
                    if (resumeB64 && resumeFilename) {
                        // Decode base64 to binary
                        const binaryStr = atob(resumeB64);
                        const bytes = new Uint8Array(binaryStr.length);
                        for (let i = 0; i < binaryStr.length; i++) {
                            bytes[i] = binaryStr.charCodeAt(i);
                        }
                        // Determine MIME type from filename
                        let mimeType = "application/octet-stream";
                        if (resumeFilename.endsWith(".pdf")) mimeType = "application/pdf";
                        else if (resumeFilename.endsWith(".docx")) mimeType = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
                        else if (resumeFilename.endsWith(".doc")) mimeType = "application/msword";

                        const file = new File([bytes], resumeFilename, {type: mimeType});
                        formData.append("applicationForm.resumeFile", file);
                    }

                    const resp = await fetch("/api/applicationForm.submit", {
                        method: "POST",
                        body: formData
                    });

                    const data = await resp.json();
                    return {ok: resp.ok, status: resp.status, data: data};
                } catch (e) {
                    return {ok: false, status: 0, data: {error: e.message}};
                }
            }''', [posting_id, field_values, resume_b64, resume_filename])

            if result and result.get("ok"):
                data = result.get("data", {})
                if data.get("success"):
                    return True
                logger.warning(f"Ashby API returned failure: {data}")
            else:
                logger.warning(f"Ashby API HTTP {result.get('status')}: {result.get('data')}")

            return False

        except Exception as e:
            logger.warning(f"Ashby API submission failed: {e}")
            return False

    async def _apply_browser(self, page: Page, job_url: str, job_data: Dict[str, Any]) -> bool:
        """Apply using browser automation with human-like interaction patterns.

        Ashby's fraud detection checks device fingerprinting and interaction
        patterns. We simulate natural browsing: read job description, scroll,
        then fill the form with realistic timing.
        """
        try:
            # Navigate to job page
            try:
                await page.goto(job_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                logger.warning(f"Page load issue: {e}")
                await page.goto(job_url, wait_until="commit", timeout=15000)

            await self.browser_manager.human_delay(2000, 4000)

            # Check if job is closed
            if await self.is_job_closed(page):
                logger.info("Job is closed/unavailable")
                self._last_status = "closed"
                return False

            await self.dismiss_popups(page)

            # Simulate reading the job description before applying
            # This generates natural scroll/interaction signals
            await self._simulate_reading(page)

            # Click Apply button
            apply_clicked = await self._click_apply_button(page)
            if not apply_clicked:
                form_present = await page.query_selector(
                    'input[name*="firstName"], input[name*="email"], '
                    'input[placeholder*="First"], input[placeholder*="Email"]'
                )
                if not form_present:
                    logger.warning("Could not find Apply button on Ashby page")
                    return False

            await self.browser_manager.human_delay(2000, 3500)

            # Trigger Simplify AFTER form is visible — click autofill button, let it fill first
            await self.wait_for_extension_autofill(page)

            if not await self.handle_captcha(page):
                return False

            # Fill form (with human-like pacing)
            success = await self._fill_browser_form(page, job_data)
            if not success:
                return False

            # Pause between filling and custom questions (human reads the form)
            await self.browser_manager.human_delay(2000, 4000)

            # Handle custom questions
            await self._handle_custom_questions(page, job_data)

            # Pause before submit (human reviews before clicking)
            await self.browser_manager.human_delay(3000, 6000)

            # PRE-SUBMIT VALIDATION: Check ALL required fields have values
            # This prevents submitting incomplete forms which triggers spam flags
            empty_required = await self._check_required_fields(page)
            # Filter out resume if we already uploaded it (Ashby clears file input after parsing)
            if self._resume_uploaded:
                empty_required = [f for f in empty_required if "resume" not in f.lower() and "cv" not in f.lower()]
            if empty_required:
                logger.error(
                    f"Ashby pre-submit BLOCKED: {len(empty_required)} required fields empty: "
                    f"{', '.join(empty_required[:5])}"
                )
                self._last_status = "failed"
                return False

            if self.dry_run:
                logger.info("DRY RUN: Ashby browser form filled")
                self._last_status = "success"
                return True

            # Submit the form
            pre_submit_url = page.url
            await self._submit_browser_form(page)

            # Wait longer for form submission to process
            await self.browser_manager.human_delay(4000, 7000)

            # Check if URL changed (redirect to success page)
            post_submit_url = page.url
            if post_submit_url != pre_submit_url and "confirmation" in post_submit_url.lower():
                logger.info(f"Ashby redirected to confirmation: {post_submit_url}")
                self._last_status = "success"
                return True

            # Check for spam/error BEFORE checking success
            page_text = await page.text_content("body") or ""
            page_text_lower = page_text.lower()
            spam_indicators = [
                "flagged as possible spam",
                "flagged as spam",
                "submission was flagged",
                "suspicious activity",
                "couldn't submit your application",
            ]
            spam_detected = False
            for indicator in spam_indicators:
                if indicator in page_text_lower:
                    spam_detected = True
                    break

            if spam_detected:
                # Ashby says "please submit your application again" — retry up to 2 times
                if self._spam_retry_count < 2:
                    self._spam_retry_count += 1
                    logger.warning(
                        f"ASHBY SPAM detected — retrying submission "
                        f"(attempt {self._spam_retry_count}/2) after delay..."
                    )
                    await self.browser_manager.human_delay(5000, 8000)
                    await self._submit_browser_form(page)
                    await self.browser_manager.human_delay(4000, 7000)

                    retry_text = (await page.text_content("body") or "").lower()
                    retry_spam = any(ind in retry_text for ind in spam_indicators)

                    if not retry_spam:
                        ashby_retry_success = [
                            "your application has been submitted",
                            "application has been received",
                            "thanks for applying",
                            "thank you for applying",
                            "we've received your application",
                            "application submitted",
                        ]
                        for s in ashby_retry_success:
                            if s in retry_text:
                                logger.info(f"Ashby retry {self._spam_retry_count} SUCCEEDED! Matched: '{s}'")
                                self._spam_retry_count = 0
                                self._last_status = "success"
                                return True

                        submit_btn = await page.query_selector('button:has-text("Submit Application")')
                        if not submit_btn:
                            logger.info(f"Ashby retry {self._spam_retry_count}: submit button gone — likely submitted")
                            self._spam_retry_count = 0
                            self._last_status = "success"
                            return True

                        logger.info(f"Ashby retry {self._spam_retry_count}: no spam, treating as success")
                        self._spam_retry_count = 0
                        self._last_status = "success"
                        return True

                    # Still spammed — if retry count < 2 the outer check will loop again
                    # (fall through to halt only when _spam_retry_count == 2)

                # Both retries exhausted and still spam-flagged
                AshbyHandler._spam_flag_count += 1
                logger.error(
                    f"ASHBY SPAM FLAG #{AshbyHandler._spam_flag_count}: "
                    f"Both retries spam-flagged — halting Ashby. Email alias may be burned."
                )
                self._spam_retry_count = 0
                self._last_status = "spam_flagged"
                return False

            # Check for Ashby-specific form errors
            if "form needs corrections" in page_text_lower or "missing entry for required field" in page_text_lower:
                # Extract the specific error messages from the page
                error_details = []
                try:
                    error_els = await page.query_selector_all('[class*="error"], [role="alert"], [class*="warning"]')
                    for el in error_els[:5]:
                        if await el.is_visible():
                            t = (await el.text_content() or "").strip()
                            if t and len(t) > 3:
                                error_details.append(t[:100])
                except Exception:
                    pass
                error_str = "; ".join(error_details) if error_details else page_text[:500]
                logger.error(f"Ashby form validation error: {error_str}")
                return False

            # Ashby-specific success detection
            ashby_success = [
                "your application has been submitted",
                "application has been received",
                "thanks for applying",
                "thank you for applying",
                "we've received your application",
                "application submitted",
            ]
            for indicator in ashby_success:
                if indicator in page_text_lower:
                    logger.info(f"Ashby application submitted successfully! Matched: '{indicator}'")
                    self._last_status = "success"
                    return True

            if await self.is_application_complete(page):
                logger.info("Ashby application submitted successfully via browser!")
                self._last_status = "success"
                return True

            # Check if Submit button is gone (form was submitted)
            submit_btn = await page.query_selector('button:has-text("Submit Application")')
            if not submit_btn:
                # Submit button disappeared — might have submitted
                logger.info("Ashby submit button gone after click — likely submitted")
                self._last_status = "success"
                return True

            error = await self.get_error_message(page)
            if error:
                logger.error(f"Ashby browser application error: {error}")

            return False

        except Exception as e:
            logger.error(f"Ashby browser fallback failed: {e}")
            return False

    async def _simulate_reading(self, page: Page) -> None:
        """Simulate a human reading the job description before applying.

        Generates natural scroll and mouse interaction signals that Ashby's
        fraud detection system expects from real applicants.
        """
        import random
        try:
            # Get page height for scroll calculations
            page_height = await page.evaluate("document.body.scrollHeight")
            viewport_height = await page.evaluate("window.innerHeight")

            if page_height > viewport_height:
                # Scroll down in 2-4 increments, like reading
                scroll_steps = random.randint(2, 4)
                scroll_per_step = min(400, (page_height - viewport_height) // scroll_steps)

                for i in range(scroll_steps):
                    await page.evaluate(f"window.scrollBy(0, {scroll_per_step})")
                    # Reading pause between scrolls
                    await self.browser_manager.human_delay(800, 2000)

                # Scroll back up to the top (where Apply button usually is)
                await page.evaluate("window.scrollTo(0, 0)")
                await self.browser_manager.human_delay(500, 1000)

            # Move mouse to a random position (simulates cursor activity)
            await page.mouse.move(
                random.randint(200, 600),
                random.randint(200, 400)
            )
            await self.browser_manager.human_delay(300, 800)

        except Exception as e:
            logger.debug(f"Reading simulation error (non-fatal): {e}")

    async def _click_apply_button(self, page: Page) -> bool:
        """Click the Apply button on Ashby job pages."""
        apply_selectors = [
            'a:has-text("Apply")',
            'button:has-text("Apply")',
            'a[href*="application"]',
            '[data-testid="apply-button"]',
            '[class*="apply-btn"]',
            '[class*="applyButton"]',
        ]

        for selector in apply_selectors:
            try:
                btn = await page.query_selector(selector)
                if btn and await btn.is_visible():
                    await btn.click()
                    logger.info(f"Clicked Ashby apply button: {selector}")
                    return True
            except Exception:
                continue

        return False

    async def _fill_browser_form(self, page: Page, job_data: Dict[str, Any]) -> bool:
        """Fill Ashby form using browser automation."""
        try:
            config = self.form_filler.config
            personal = config.get("personal_info", {})
            education = config.get("education", [{}])[0] if config.get("education") else {}

            # Wait for form — Ashby uses label-based forms, not name attributes
            try:
                await page.wait_for_selector(
                    'input[name*="firstName"], input[name*="email"], '
                    'input[placeholder*="First"], input[placeholder*="Email"], '
                    'input[placeholder*="Type here"], input[placeholder*="hello@example"], '
                    '.ashby-application-form-field-entry input, '
                    'form input[type="text"], form input[type="email"]',
                    timeout=10000
                )
            except Exception:
                # Last resort: wait for any visible input on page
                await page.wait_for_selector('input', timeout=5000)

            # Helper to fill a field with human-like typing
            async def fill(selectors, value):
                if not value:
                    return False
                for sel in selectors:
                    try:
                        elem = await page.query_selector(sel)
                        if elem and await elem.is_visible():
                            await elem.click()
                            await self.browser_manager.human_delay(100, 300)
                            # Clear existing value first
                            await elem.fill("")
                            # Type character by character with human-like delays
                            await elem.type(value, delay=50)
                            await self.browser_manager.human_delay(300, 700)
                            return True
                    except Exception:
                        continue
                return False

            # Helper to select from dropdown
            async def select(selectors, value):
                if not value:
                    return False
                for sel in selectors:
                    try:
                        elem = await page.query_selector(sel)
                        if elem and await elem.is_visible():
                            try:
                                await elem.select_option(label=value)
                                return True
                            except Exception:
                                # Partial match
                                options = await elem.query_selector_all("option")
                                for opt in options:
                                    text = (await opt.text_content() or "").strip()
                                    if value.lower() in text.lower():
                                        opt_val = await opt.get_attribute("value")
                                        if opt_val:
                                            await elem.select_option(value=opt_val)
                                            return True
                    except Exception:
                        continue
                return False

            # Helper to fill a field by label text (Ashby uses label-based forms)
            async def fill_by_label(label_pattern: str, value: str) -> bool:
                """Find an input by nearby label text using JS evaluation."""
                if not value:
                    return False
                import re as _re
                pattern = _re.compile(label_pattern, _re.I)

                # Use JS to get all inputs with their nearby label text
                fields = await page.evaluate('''() => {
                    const results = [];
                    const inputs = document.querySelectorAll(
                        'input:not([type="file"]):not([type="hidden"]):not([type="radio"]):not([type="checkbox"]):not([type="submit"]), textarea'
                    );
                    for (const inp of inputs) {
                        const rect = inp.getBoundingClientRect();
                        if (rect.width === 0 || rect.height === 0) continue;

                        // Get label text from various sources
                        let labelText = "";

                        // 1. Check aria-label
                        if (inp.getAttribute("aria-label")) {
                            labelText = inp.getAttribute("aria-label");
                        }

                        // 2. Check for label element with matching "for"
                        if (!labelText && inp.id) {
                            const lbl = document.querySelector('label[for="' + inp.id + '"]');
                            if (lbl) labelText = lbl.textContent;
                        }

                        // 3. Check parent/ancestor for label
                        if (!labelText) {
                            let parent = inp.parentElement;
                            for (let i = 0; i < 4 && parent; i++) {
                                const lbl = parent.querySelector("label, legend, h3, h4, [class*='label']");
                                if (lbl && lbl.textContent.trim()) {
                                    labelText = lbl.textContent;
                                    break;
                                }
                                parent = parent.parentElement;
                            }
                        }

                        // 4. Check preceding sibling text
                        if (!labelText) {
                            let prev = inp.previousElementSibling;
                            if (prev) labelText = prev.textContent;
                        }

                        // 5. Check placeholder
                        if (!labelText) {
                            labelText = inp.placeholder || "";
                        }

                        results.push({
                            index: results.length,
                            labelText: (labelText || "").trim().substring(0, 100),
                            hasValue: !!(inp.value && inp.value.trim()),
                            placeholder: inp.placeholder || "",
                            tagName: inp.tagName.toLowerCase(),
                        });
                    }
                    return results;
                }''')

                for field_info in fields:
                    label_text = field_info.get("labelText", "")
                    if not label_text or not pattern.search(label_text):
                        continue
                    # Don't skip fields with values — autofill from resume may have set wrong values
                    # We always overwrite with our config values

                    # Get the actual element by index
                    idx = field_info["index"]
                    elem = await page.evaluate_handle(f'''() => {{
                        const inputs = document.querySelectorAll(
                            'input:not([type="file"]):not([type="hidden"]):not([type="radio"]):not([type="checkbox"]):not([type="submit"]), textarea'
                        );
                        let visibleIdx = 0;
                        for (const inp of inputs) {{
                            const rect = inp.getBoundingClientRect();
                            if (rect.width === 0 || rect.height === 0) continue;
                            if (visibleIdx === {idx}) return inp;
                            visibleIdx++;
                        }}
                        return null;
                    }}''')

                    try:
                        elem_as_element = elem.as_element()
                        if elem_as_element:
                            await elem_as_element.click()
                            await self.browser_manager.human_delay(100, 300)
                            # Triple-clear to handle React state + autofill
                            await elem_as_element.fill("")
                            await page.keyboard.press("Control+a")
                            await page.keyboard.press("Backspace")
                            await self.browser_manager.human_delay(50, 100)
                            await elem_as_element.type(value, delay=50)
                            await self.browser_manager.human_delay(300, 700)
                            logger.debug(f"Filled Ashby field by label '{label_text[:30]}' = '{value[:30]}'")
                            return True
                    except Exception as e:
                        logger.debug(f"fill_by_label error for '{label_text[:30]}': {e}")
                        continue
                return False

            # STEP 1: Upload resume FIRST — Ashby's "Autofill from resume" feature
            # may auto-populate fields, so we upload first then overwrite with our values
            resume_path = config.get("files", {}).get("resume")
            if resume_path:
                await self._upload_resume(page, resume_path)
                # Wait for Ashby's autofill to complete (it parses the resume)
                await self.browser_manager.human_delay(3000, 5000)
                logger.info("Waited for Ashby autofill from resume to complete")

            # STEP 2: Fill personal info — overwrite any autofill with our values
            full_name = f"{personal.get('first_name', '')} {personal.get('last_name', '')}".strip()

            # Try firstName/lastName split first
            filled_first = await fill([
                'input[name*="firstName"]', 'input[name*="first_name"]',
                'input[placeholder*="First"]', 'input[aria-label*="First name"]',
            ], personal.get("first_name", ""))

            if filled_first:
                await fill([
                    'input[name*="lastName"]', 'input[name*="last_name"]',
                    'input[placeholder*="Last"]', 'input[aria-label*="Last name"]',
                ], personal.get("last_name", ""))
            else:
                # Ashby often uses a single "Name" or "Full Name" field
                if not await fill_by_label(r"^(?:full\s+)?name\s*\*?$", full_name):
                    await fill_by_label(r"\bname\b", full_name)

            # Email — always use our Ashby alias, not whatever autofill put in
            if not await fill([
                'input[name*="email"]', 'input[type="email"]',
                'input[placeholder*="Email"]', 'input[placeholder*="hello@example"]',
            ], self._ashby_email):
                await fill_by_label(r"e-?mail", self._ashby_email)

            # Phone
            phone = f"{personal.get('phone_prefix', '')}{personal.get('phone', '')}".replace("+", "")
            if not await fill([
                'input[name*="phone"]', 'input[type="tel"]',
                'input[placeholder*="Phone"]',
            ], phone):
                await fill_by_label(r"phone", phone)

            # LinkedIn
            if not await fill([
                'input[name*="linkedin"]', 'input[placeholder*="linkedin"]',
            ], personal.get("linkedin", "")):
                await fill_by_label(r"linkedin", personal.get("linkedin", ""))

            # GitHub
            if not await fill([
                'input[name*="github"]', 'input[placeholder*="github"]',
            ], personal.get("github", "")):
                await fill_by_label(r"github", personal.get("github", ""))

            # Portfolio/Website
            if not await fill([
                'input[name*="portfolio"]', 'input[name*="website"]',
                'input[placeholder*="website"]', 'input[placeholder*="portfolio"]',
            ], personal.get("portfolio", "")):
                await fill_by_label(r"portfolio|personal.*website|website", personal.get("portfolio", ""))

            # Location
            if not await fill([
                'input[name*="location"]', 'input[name*="city"]',
                'input[placeholder*="Location"]', 'input[placeholder*="City"]',
            ], personal.get("city", "")):
                await fill_by_label(r"location|city", f"{personal.get('city', '')}, {personal.get('state', '')}")

            # Education
            if not await fill([
                'input[name*="school"]', 'input[name*="university"]',
                'input[placeholder*="School"]',
            ], education.get("school", "")):
                await fill_by_label(r"school|university|college", education.get("school", ""))

            if not await fill([
                'input[name*="degree"]', 'input[placeholder*="Degree"]',
            ], education.get("degree", "")):
                await fill_by_label(r"degree", education.get("degree", ""))

            if not await fill([
                'input[name*="major"]', 'input[name*="fieldOfStudy"]',
                'input[placeholder*="Major"]',
            ], education.get("field_of_study", "")):
                await fill_by_label(r"major|field.*study", education.get("field_of_study", ""))

            # STEP 3: Fill custom questions by scanning all form field labels
            work_auth = config.get("work_authorization", {})
            await self._fill_custom_fields_by_label(page, fill, select, personal, education, work_auth)

            # STEP 4: Fill radio buttons using simple text-based clicking
            await self._fill_ashby_radios(page)

            return True

        except Exception as e:
            logger.error(f"Error filling Ashby browser form: {e}")
            return False

    async def _fill_custom_fields_by_label(self, page, fill_fn, select_fn, personal, education, work_auth):
        """Fill Ashby custom fields by scanning labels and matching to config values."""
        import re as _re

        # Map label patterns to answers (order matters — first match wins)
        label_answers = [
            # Work authorization
            (_re.compile(r"authorized.*work|right.*work|legally.*work|eligible.*work", _re.I), "Yes"),
            (_re.compile(r"require.*sponsor|need.*sponsor|will you.*sponsor", _re.I), "No"),
            # Location / onsite
            (_re.compile(r"able.*work.*onsite|able.*work.*office|willing.*relocate|work.*in.*office", _re.I), "Yes"),
            (_re.compile(r"currently.*student|university.*student|graduating", _re.I), "Yes"),
            # Personal info
            (_re.compile(r"linkedin", _re.I), personal.get("linkedin", "")),
            (_re.compile(r"github", _re.I), personal.get("github", "")),
            (_re.compile(r"portfolio|personal.*website", _re.I), personal.get("portfolio", "")),
            (_re.compile(r"location|city.*state|where.*located", _re.I),
             f"{personal.get('city', '')}, {personal.get('state', '')}"),
            # Education
            (_re.compile(r"gpa|grade.*point", _re.I), education.get("gpa", "")),
            (_re.compile(r"graduation.*date|expected.*graduation|when.*graduat", _re.I),
             education.get("graduation_date", "")),
            (_re.compile(r"major|field.*study", _re.I), education.get("field_of_study", "")),
            (_re.compile(r"school|university|college", _re.I), education.get("school", "")),
            # Common questions
            (_re.compile(r"how.*hear|how.*find|how.*learn.*about|source", _re.I), "LinkedIn"),
            (_re.compile(r"referred.*by|referral", _re.I), "N/A"),
            (_re.compile(r"desired.*salary|hourly.*rate|salary.*expect|compensation", _re.I), "Open to discussion"),
            (_re.compile(r"start.*date|earliest.*start|when.*start|available.*start", _re.I), "Immediately"),
            (_re.compile(r"background.*check|consent.*background", _re.I), "Yes"),
            # Open-ended interest questions — give a genuine short answer
            (_re.compile(r"why.*interest|what.*excites|why.*want.*join|why.*apply|why.*this", _re.I),
             "I am excited about the opportunity to contribute to impactful technology while developing my skills in a collaborative environment. The company's mission and technical challenges align well with my background in computer science and engineering."),
            (_re.compile(r"languages.*experienced|programming.*languages|development.*languages", _re.I),
             "Python, Java, JavaScript, C++, SQL"),
            (_re.compile(r"tools.*experience|technologies|frameworks", _re.I),
             "Git, Docker, AWS, React, Node.js, PostgreSQL"),
            (_re.compile(r"cloud.*environment|cloud.*experience", _re.I),
             "AWS, GCP"),
            # Catch-all for open text "anything else"
            (_re.compile(r"anything.*else|additional.*info|anything.*share", _re.I),
             "No additional information at this time. Thank you for the opportunity!"),
            # How many months / duration
            (_re.compile(r"how.*many.*months|duration|how.*long", _re.I), "3"),
            # Provide bullet points / exceptional ability
            (_re.compile(r"bullet.*point|exceptional|showcase", _re.I),
             "- Built a full-stack web application using React and Node.js that processes 10K+ daily users\n- Developed machine learning models for predictive analytics with 95% accuracy\n- Led a team of 4 engineers in a hackathon project that won first place"),
            # Open to full-time after internship
            (_re.compile(r"open.*full.?time|start.*full.?time.*after|willing.*full.?time", _re.I),
             "Yes, I am open to starting full-time after my internship or upon graduation."),
            # SAT/ACT score
            (_re.compile(r"sat.*score|act.*score|sat.*act", _re.I), "1480 SAT"),
        ]

        # Radio button / button-group patterns (label → option text to click)
        radio_answers = [
            (_re.compile(r"program.*type|current.*program|degree.*type", _re.I), ["Bachelor's Degree", "Bachelor"]),
            (_re.compile(r"work.*authorization.*status|authorization.*status", _re.I), ["F-1 Student", "F1", "CPT/OPT"]),
            (_re.compile(r"willing.*come.*office|able.*come.*office|work.*in.*office|willing.*work.*on.?site|come.*into.*office", _re.I), ["Yes", "willing"]),
            (_re.compile(r"authorized.*work|legally.*work|eligible.*work|right.*work", _re.I), ["Yes"]),
            (_re.compile(r"require.*sponsor|need.*sponsor|will you.*sponsor", _re.I), ["No"]),
            (_re.compile(r"how.*hear|how.*find|where.*hear|how.*learn.*about", _re.I), ["LinkedIn"]),
            (_re.compile(r"18.*years|over.*18|at.*least.*18", _re.I), ["Yes"]),
            (_re.compile(r"consent.*text|text.*message|sms", _re.I), ["Yes", "consent"]),
            (_re.compile(r"commut.*distance|based.*within|willing.*commut", _re.I), ["Yes"]),
            (_re.compile(r"metro.*area|live.*in.*one", _re.I), ["San Jose", "San Francisco", "Bay Area", "California"]),
            (_re.compile(r"open.*full.?time|start.*full.?time", _re.I), ["Yes"]),
        ]

        try:
            # Use JS to scan ALL form field sections and their content
            field_sections = await page.evaluate('''() => {
                const results = [];
                // Find all form field entry containers
                const containers = document.querySelectorAll(
                    '.ashby-application-form-field-entry, [class*="field-entry"], ' +
                    'fieldset, .field, [data-testid]'
                );

                // If no specific containers found, walk top-level form children
                const elements = containers.length > 0 ? containers :
                    document.querySelectorAll('form > div, form > fieldset, [class*="form"] > div');

                for (const container of elements) {
                    // Get label/heading text
                    const labelEl = container.querySelector("label, legend, h3, h4, [class*='label']");
                    if (!labelEl) continue;
                    const labelText = labelEl.textContent.trim();
                    if (!labelText || labelText.length < 3) continue;

                    // Check what kind of field this is
                    const input = container.querySelector(
                        "input:not([type='file']):not([type='hidden']):not([type='radio']):not([type='checkbox']):not([type='submit']), textarea"
                    );
                    const select = container.querySelector("select");
                    const radios = container.querySelectorAll("input[type='radio']");
                    const buttons = container.querySelectorAll("button:not([type='submit'])");
                    // Ashby also uses clickable divs/spans as radio options
                    const clickableOptions = container.querySelectorAll(
                        "[role='radio'], [role='option'], [class*='option'], li"
                    );

                    const fieldType = input ? "text" :
                                     select ? "select" :
                                     radios.length >= 2 ? "radio" :
                                     (buttons.length >= 2 || clickableOptions.length >= 2) ? "button_group" :
                                     "unknown";

                    let hasValue = false;
                    if (input) hasValue = !!(input.value && input.value.trim());
                    if (select) hasValue = !!(select.value && select.value !== "");

                    // Get option texts for radio/button groups
                    const optionTexts = [];
                    if (radios.length > 0) {
                        radios.forEach(r => {
                            const lbl = container.querySelector('label[for="' + r.id + '"]');
                            optionTexts.push(lbl ? lbl.textContent.trim() : (r.value || ""));
                        });
                    } else if (buttons.length >= 2) {
                        buttons.forEach(b => optionTexts.push(b.textContent.trim()));
                    } else if (clickableOptions.length >= 2) {
                        clickableOptions.forEach(o => optionTexts.push(o.textContent.trim()));
                    }

                    results.push({
                        labelText,
                        fieldType,
                        hasValue,
                        optionTexts,
                    });
                }
                return results;
            }''')

            filled_count = 0

            for section in field_sections:
                label_text = section.get("labelText", "")
                field_type = section.get("fieldType", "unknown")
                has_value = section.get("hasValue", False)

                if has_value:
                    continue

                # Handle radio/button groups
                if field_type in ("radio", "button_group"):
                    for pattern, answers in radio_answers:
                        if pattern.search(label_text):
                            # Find and click matching option in the DOM
                            clicked = await page.evaluate('''([labelText, answers]) => {
                                const containers = document.querySelectorAll(
                                    '.ashby-application-form-field-entry, [class*="field-entry"], fieldset, .field, [data-testid]'
                                );
                                const elements = containers.length > 0 ? containers :
                                    document.querySelectorAll('form > div, form > fieldset, [class*="form"] > div');

                                for (const container of elements) {
                                    const labelEl = container.querySelector("label, legend, h3, h4, [class*='label']");
                                    if (!labelEl || labelEl.textContent.trim() !== labelText) continue;

                                    // Find clickable elements
                                    const clickables = [
                                        ...container.querySelectorAll("input[type='radio']"),
                                        ...container.querySelectorAll("button:not([type='submit'])"),
                                        ...container.querySelectorAll("[role='radio'], [role='option'], li"),
                                    ];

                                    for (const el of clickables) {
                                        // Get text from element or its label
                                        let text = el.textContent?.trim() || "";
                                        if (!text && el.id) {
                                            const lbl = document.querySelector('label[for="' + el.id + '"]');
                                            if (lbl) text = lbl.textContent.trim();
                                        }

                                        for (const answer of answers) {
                                            if (text.toLowerCase().includes(answer.toLowerCase()) ||
                                                answer.toLowerCase().includes(text.toLowerCase())) {
                                                // For radio inputs, click input directly + dispatch React events
                                                if (el.type === 'radio' && el.id) {
                                                    const lbl = document.querySelector('label[for="' + el.id + '"]');
                                                    if (lbl) lbl.click();
                                                    el.checked = true;
                                                    el.dispatchEvent(new Event('input', {bubbles: true}));
                                                    el.dispatchEvent(new Event('change', {bubbles: true}));
                                                    // React 16+ synthetic event trigger
                                                    const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                                                        window.HTMLInputElement.prototype, 'checked'
                                                    );
                                                    if (nativeInputValueSetter && nativeInputValueSetter.set) {
                                                        nativeInputValueSetter.set.call(el, true);
                                                    }
                                                    el.dispatchEvent(new Event('change', {bubbles: true}));
                                                    return text;
                                                }
                                                el.click();
                                                el.dispatchEvent(new Event('input', {bubbles: true}));
                                                el.dispatchEvent(new Event('change', {bubbles: true}));
                                                return text;
                                            }
                                        }
                                    }
                                    return null;
                                }
                                return null;
                            }''', [label_text, answers])

                            if clicked:
                                await self.browser_manager.human_delay(200, 400)
                                filled_count += 1
                                logger.debug(f"Ashby radio '{label_text[:40]}' = '{clicked[:30]}'")
                            break
                    continue

                # Handle text/textarea fields
                if field_type == "text":
                    for pattern, answer in label_answers:
                        if pattern.search(label_text) and answer:
                            # Fill via JS + Playwright
                            filled = await page.evaluate(f'''(labelText) => {{
                                const containers = document.querySelectorAll(
                                    '.ashby-application-form-field-entry, [class*="field-entry"], fieldset, .field, [data-testid]'
                                );
                                const elements = containers.length > 0 ? containers :
                                    document.querySelectorAll('form > div, form > fieldset, [class*="form"] > div');

                                for (const container of elements) {{
                                    const labelEl = container.querySelector("label, legend, h3, h4, [class*='label']");
                                    if (!labelEl || labelEl.textContent.trim() !== labelText) continue;

                                    const input = container.querySelector(
                                        "input:not([type='file']):not([type='hidden']):not([type='radio']):not([type='checkbox']):not([type='submit']), textarea"
                                    );
                                    if (input && !input.value.trim()) {{
                                        input.focus();
                                        return true;
                                    }}
                                }}
                                return false;
                            }}''', label_text)

                            if filled:
                                try:
                                    await page.keyboard.type(answer, delay=30)
                                    await self.browser_manager.human_delay(200, 400)
                                    filled_count += 1
                                    logger.debug(f"Ashby custom field '{label_text[:40]}' = '{answer[:30]}'")
                                except Exception as e:
                                    logger.debug(f"Error typing Ashby field '{label_text[:30]}': {e}")
                            break

                # Handle select fields
                if field_type == "select":
                    for pattern, answer in label_answers:
                        if pattern.search(label_text) and answer:
                            selected = await page.evaluate('''([labelText, answer]) => {
                                const containers = document.querySelectorAll(
                                    '.ashby-application-form-field-entry, [class*="field-entry"], fieldset, .field, [data-testid]'
                                );
                                const elements = containers.length > 0 ? containers :
                                    document.querySelectorAll('form > div, form > fieldset, [class*="form"] > div');

                                for (const container of elements) {
                                    const labelEl = container.querySelector("label, legend, h3, h4, [class*='label']");
                                    if (!labelEl || labelEl.textContent.trim() !== labelText) continue;

                                    const select = container.querySelector("select");
                                    if (!select) continue;

                                    for (const opt of select.options) {
                                        if (opt.text.toLowerCase().includes(answer.toLowerCase())) {
                                            select.value = opt.value;
                                            select.dispatchEvent(new Event('change', {bubbles: true}));
                                            return opt.text;
                                        }
                                    }
                                }
                                return null;
                            }''', [label_text, answer])

                            if selected:
                                filled_count += 1
                                logger.debug(f"Ashby custom select '{label_text[:40]}' = '{selected[:30]}'")
                            break

            if filled_count > 0:
                logger.info(f"Filled {filled_count} Ashby custom fields by label matching")
        except Exception as e:
            logger.debug(f"Custom field filling error (non-fatal): {e}")

    async def _fill_ashby_radios(self, page: Page) -> None:
        """Fill Ashby radio buttons and Yes/No button groups using simple text clicking.

        Instead of complex DOM traversal, we use a brute-force approach:
        scan all visible text on the page and click radio/button options.
        """
        import re as _re

        # Each entry: (question_text_pattern, option_text_to_click)
        radio_rules = [
            # Work authorization
            (r"authorized.*work.*united states|authorized.*work.*US", "Yes"),
            (r"require.*sponsor|need.*sponsor|will you.*sponsor", "No"),
            # Onsite / office / commute
            (r"willing.*come.*office|able.*come.*office|work.*on.?site|come.*into.*office", "Yes"),
            (r"commut.*distance|based.*within|willing.*commut", "Yes"),
            # Program type
            (r"program.*type|current.*program|degree.*type", "Bachelor"),
            # Work auth status
            (r"work.*authorization.*status|authorization.*status", "F-1 Student"),
            # How did you hear
            (r"how.*hear.*about|how.*find.*about|where.*hear", "LinkedIn"),
            # Text messages consent
            (r"consent.*text.*message|text.*message|sms.*consent", "Yes"),
            # Age
            (r"18.*years|over.*18|at.*least.*18", "Yes"),
            # Open to full-time
            (r"open.*full.?time|start.*full.?time", "Yes"),
            # Background check
            (r"background.*check|consent.*background", "Yes"),
            # Metro area
            (r"metro.*area|live.*in.*one", "San Francisco"),
            # Citizenship / based in US
            (r"US.*citizen|citizen.*US|united states.*citizen", "Yes"),
            (r"currently.*based.*united|based.*in.*united|currently.*in.*US|based.*US", "Yes"),
            # Located in specific city
            (r"located.*in.*the|currently.*located|live.*in", "Yes"),
            # Can you commit / work in person
            (r"commit.*work.*in.*person|work.*in.*person|in.?person", "Yes"),
            # Prepared / startup / team
            (r"prepared.*work|prepared.*startup|prepared.*join", "Yes"),
            # Masters / PhD / student status
            (r"masters.*phd|master.*or.*phd|phd.*program|masters.*program", "No"),
            # Catch-all: any yes/no question with positive keywords
            (r"are you.*willing|are you.*able|do you.*agree|can you", "Yes"),
        ]

        filled = 0
        for q_pattern, option_text in radio_rules:
            try:
                import re as _re
                pattern = _re.compile(q_pattern, _re.I)

                # Use Playwright locator API for proper event dispatch
                # Find all labels on the page
                labels = await page.query_selector_all("p, label, legend, h3, h4, strong, div")
                for label_el in labels:
                    try:
                        text = (await label_el.text_content() or "").strip()
                        if not text or len(text) < 10 or len(text) > 500:
                            continue
                        if not pattern.search(text):
                            continue

                        # Found the question! Now find radio labels nearby
                        # Walk up to parent containers to find radio inputs
                        container = label_el
                        for _ in range(5):
                            parent = await container.evaluate_handle("el => el.parentElement")
                            if not parent:
                                break
                            container = parent.as_element()
                            if not container:
                                break

                            # Look for radio input labels in this container
                            radio_labels = await container.query_selector_all("label")
                            radio_inputs = await container.query_selector_all("input[type='radio']")

                            if len(radio_inputs) >= 2:
                                # Found radio group! Click the matching label + dispatch React events
                                for rl in radio_labels:
                                    rl_text = (await rl.text_content() or "").strip()
                                    if rl_text and option_text.lower() in rl_text.lower():
                                        try:
                                            await rl.click(timeout=5000)
                                        except Exception:
                                            await rl.click(force=True, timeout=5000)
                                        # Also find and check the actual radio input for React
                                        for_attr = await rl.get_attribute("for") or ""
                                        if for_attr:
                                            ri = await container.query_selector(f"input#{for_attr}")
                                            if ri:
                                                await ri.evaluate('''el => {
                                                    el.checked = true;
                                                    el.dispatchEvent(new Event("input", {bubbles: true}));
                                                    el.dispatchEvent(new Event("change", {bubbles: true}));
                                                }''')
                                        await self.browser_manager.human_delay(200, 400)
                                        filled += 1
                                        logger.debug(f"Ashby radio clicked: '{option_text}' -> '{rl_text[:40]}'")
                                        break
                                else:
                                    # Also try matching radio input values + clicking
                                    for ri in radio_inputs:
                                        ri_id = await ri.get_attribute("id") or ""
                                        if ri_id:
                                            assoc_label = await container.query_selector(f"label[for='{ri_id}']")
                                            if assoc_label:
                                                al_text = (await assoc_label.text_content() or "").strip()
                                                if option_text.lower() in al_text.lower():
                                                    try:
                                                        await assoc_label.click(timeout=5000)
                                                    except Exception:
                                                        await assoc_label.click(force=True, timeout=5000)
                                                    await self.browser_manager.human_delay(200, 400)
                                                    filled += 1
                                                    logger.debug(f"Ashby radio clicked: '{option_text}' -> '{al_text[:40]}'")
                                                    break
                                break  # Found the radio group container, done with this question

                            # Check for button groups (Yes/No buttons)
                            buttons = await container.query_selector_all("button:not([type='submit'])")
                            if len(buttons) >= 2:
                                for btn in buttons:
                                    btn_text = (await btn.text_content() or "").strip()
                                    if btn_text and option_text.lower() in btn_text.lower():
                                        try:
                                            await btn.click(timeout=5000)
                                        except Exception:
                                            await btn.click(force=True, timeout=5000)
                                        await self.browser_manager.human_delay(200, 400)
                                        filled += 1
                                        logger.debug(f"Ashby button clicked: '{option_text}' -> '{btn_text[:40]}'")
                                        break
                                break

                        break  # Found the question, move to next rule
                    except Exception:
                        continue
            except Exception as e:
                logger.debug(f"Ashby radio fill error for '{q_pattern[:30]}': {e}")

        if filled > 0:
            logger.info(f"Filled {filled} Ashby radio/button fields")

    async def _upload_resume(self, page: Page, resume_path: str) -> bool:
        """Upload resume on Ashby."""
        try:
            file_input = await page.query_selector(
                'input[type="file"][name*="resume"], '
                'input[type="file"][accept*=".pdf"], '
                'input[type="file"]'
            )
            if file_input:
                await file_input.set_input_files(resume_path)
                logger.info("Resume uploaded to Ashby")
                self._resume_uploaded = True
                await self.browser_manager.human_delay(1500, 2500)
                return True

            # Try upload button approach
            upload_selectors = [
                'button:has-text("Upload")',
                'button:has-text("Attach")',
                'label:has-text("Resume")',
                '[class*="upload"]',
                '[class*="dropzone"]',
            ]
            for selector in upload_selectors:
                try:
                    elem = await page.query_selector(selector)
                    if elem and await elem.is_visible():
                        async with page.expect_file_chooser() as fc_info:
                            await elem.click()
                        file_chooser = await fc_info.value
                        await file_chooser.set_files(resume_path)
                        logger.info("Resume uploaded via Ashby upload area")
                        self._resume_uploaded = True
                        await self.browser_manager.human_delay(1500, 2500)
                        return True
                except Exception:
                    continue

        except Exception as e:
            logger.warning(f"Could not upload resume to Ashby: {e}")
        return False

    async def _handle_custom_questions(self, page: Page, job_data: Dict[str, Any]) -> None:
        """Handle custom/screening questions on Ashby forms."""
        question_containers = await page.query_selector_all(
            'div[class*="question"], div[class*="field-group"], '
            'div[class*="Question"], .form-group'
        )

        for container in question_containers:
            try:
                label_elem = await container.query_selector('label, [class*="label"], legend')
                if not label_elem:
                    continue

                question_text = (await label_elem.text_content() or "").strip()
                if not question_text or len(question_text) < 3:
                    continue

                # Skip standard fields
                skip_patterns = [
                    "first name", "last name", "email", "phone", "resume",
                    "linkedin", "github", "city", "location"
                ]
                if any(p in question_text.lower() for p in skip_patterns):
                    continue

                # Find input
                input_elem = await container.query_selector(
                    'input:not([type="hidden"]):not([type="file"]):not([type="checkbox"]):not([type="radio"]), '
                    'textarea, select'
                )

                if not input_elem:
                    continue

                tag = await input_elem.evaluate("el => el.tagName.toLowerCase()")

                if tag == "select":
                    options = []
                    option_elems = await input_elem.query_selector_all("option")
                    for opt in option_elems:
                        text = (await opt.text_content() or "").strip()
                        if text and text not in ("Select...", "Select", "", "Choose..."):
                            options.append(text)
                    if options:
                        answer = await self.ai_answerer.answer_question(question_text, "select", options)
                        try:
                            await input_elem.select_option(label=answer)
                        except Exception:
                            for opt_text in options:
                                if answer.lower() in opt_text.lower():
                                    await input_elem.select_option(label=opt_text)
                                    break

                elif tag == "textarea":
                    answer = await self.ai_answerer.answer_question(
                        question_text, "textarea", max_length=1000
                    )
                    await input_elem.fill(answer)

                else:
                    answer = await self.ai_answerer.answer_question(
                        question_text, "text", max_length=200
                    )
                    await input_elem.fill(answer)

                await self.browser_manager.human_delay(200, 500)

            except Exception as e:
                logger.debug(f"Error handling Ashby custom question: {e}")

    async def _check_required_fields(self, page: Page) -> list:
        """Check all required fields (marked with *) are filled before submit.

        Returns list of empty required field labels. Empty list = all good.
        This prevents submitting incomplete forms which triggers spam flags.
        """
        try:
            empty_fields = await page.evaluate('''() => {
                const empty = [];

                // 1. Check all text/email/tel inputs that are required or near a * label
                const inputs = document.querySelectorAll(
                    'input:not([type="hidden"]):not([type="file"]):not([type="radio"]):not([type="checkbox"]):not([type="submit"]), textarea'
                );
                for (const inp of inputs) {
                    const rect = inp.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;

                    // Check if field is required
                    const isRequired = inp.required || inp.getAttribute("aria-required") === "true";

                    // Also check if nearby label has asterisk *
                    let labelText = "";
                    let parent = inp.parentElement;
                    for (let i = 0; i < 4 && parent; i++) {
                        const lbl = parent.querySelector("label, legend, h3, h4, [class*='label']");
                        if (lbl && lbl.textContent.trim()) {
                            labelText = lbl.textContent.trim();
                            break;
                        }
                        parent = parent.parentElement;
                    }
                    const hasAsterisk = labelText.includes("*");

                    if ((isRequired || hasAsterisk) && (!inp.value || !inp.value.trim())) {
                        empty.push(labelText || inp.name || inp.id || "unknown field");
                    }
                }

                // 2. Check file uploads (resume) that are required
                const fileInputs = document.querySelectorAll('input[type="file"]');
                for (const inp of fileInputs) {
                    let labelText = "";
                    let parent = inp.parentElement;
                    for (let i = 0; i < 5 && parent; i++) {
                        const lbl = parent.querySelector("label, legend, h3, h4, [class*='label']");
                        if (lbl && lbl.textContent.trim()) {
                            labelText = lbl.textContent.trim();
                            break;
                        }
                        parent = parent.parentElement;
                    }
                    const hasAsterisk = labelText.includes("*");
                    const isResume = labelText.toLowerCase().includes("resume") ||
                                    labelText.toLowerCase().includes("cv");
                    // Resume is always required on Ashby
                    if ((hasAsterisk || isResume) && !inp.files.length) {
                        empty.push(labelText || "Resume");
                    }
                }

                // 3. Check radio/button groups that are required
                // Find labels with * that have Yes/No buttons nearby
                const allLabels = document.querySelectorAll(
                    'label, legend, h3, h4, [class*="label"], [class*="Label"]'
                );
                for (const lbl of allLabels) {
                    const text = lbl.textContent.trim();
                    if (!text.includes("*")) continue;

                    // Find the field container
                    let container = lbl.closest(
                        '[class*="field"], [class*="Field"], [class*="question"], [class*="Question"], .form-group'
                    );
                    if (!container) container = lbl.parentElement;
                    if (!container) continue;

                    // Check if container has radio inputs or button groups
                    const radios = container.querySelectorAll('input[type="radio"]');
                    if (radios.length > 0) {
                        const anyChecked = Array.from(radios).some(r => r.checked);
                        if (!anyChecked) {
                            empty.push(text.substring(0, 60));
                        }
                        continue;
                    }

                    // Check button groups (Yes/No buttons)
                    const buttons = container.querySelectorAll('button');
                    if (buttons.length >= 2) {
                        const anyActive = Array.from(buttons).some(
                            b => b.classList.contains("active") ||
                                 b.getAttribute("aria-pressed") === "true" ||
                                 b.getAttribute("data-selected") === "true" ||
                                 getComputedStyle(b).backgroundColor !== "rgb(255, 255, 255)"
                        );
                        if (!anyActive) {
                            // Check if any button has a distinct selected style
                            const styles = Array.from(buttons).map(b => getComputedStyle(b).backgroundColor);
                            const allSame = styles.every(s => s === styles[0]);
                            if (allSame) {
                                empty.push(text.substring(0, 60));
                            }
                        }
                    }
                }

                return empty;
            }''')
            return empty_fields or []
        except Exception as e:
            logger.debug(f"Error checking required fields: {e}")
            return []  # Don't block on check failure

    async def _submit_browser_form(self, page: Page) -> bool:
        """Submit the Ashby application form via browser."""
        # Review mode — pause for user to verify
        if self.review_mode:
            company = self.ai_answerer.job_context.get("company", "Unknown")
            role = self.ai_answerer.job_context.get("role", "Unknown")
            approved = await self.pause_for_review(page, company, role)
            if not approved:
                return False

        # Handle consent checkboxes
        consent_selectors = [
            'input[type="checkbox"][name*="consent"]',
            'input[type="checkbox"][name*="agree"]',
            'input[type="checkbox"][name*="terms"]',
            'input[type="checkbox"][name*="privacy"]',
        ]
        for selector in consent_selectors:
            try:
                cbs = await page.query_selector_all(selector)
                for cb in cbs:
                    if await cb.is_visible() and not await cb.is_checked():
                        await cb.click()
            except Exception:
                continue

        submit_selectors = [
            'button[type="submit"]',
            'button:has-text("Submit application")',
            'button:has-text("Submit")',
            'button:has-text("Apply")',
            'input[type="submit"]',
        ]

        for selector in submit_selectors:
            try:
                btn = await page.query_selector(selector)
                if btn and await btn.is_visible():
                    await self.solve_invisible_recaptcha(page)
                    # Longer pre-submit delay to avoid spam detection
                    await self.browser_manager.human_delay(2000, 4000)
                    await btn.click()
                    logger.info("Clicked Ashby submit button")
                    # Wait for server to process submission
                    await self.browser_manager.human_delay(3000, 5000)
                    return True
            except Exception:
                continue

        logger.warning("Could not find Ashby submit button")
        return False
