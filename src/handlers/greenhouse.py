"""
Greenhouse Handler

Handles job applications on Greenhouse ATS.
URLs: boards.greenhouse.io, job-boards.greenhouse.io
"""

import asyncio
from typing import Dict, Any
from playwright.async_api import Page
from loguru import logger

from .base import BaseHandler


class GreenhouseHandler(BaseHandler):
    """Handler for Greenhouse ATS applications."""

    name = "greenhouse"

    async def apply(self, page: Page, job_url: str, job_data: Dict[str, Any]) -> bool:
        """Apply to a Greenhouse job."""
        self._last_status = "failed"  # Default; updated on success or closed detection
        self._current_company = job_data.get("company", "")  # Store for email verification filtering
        try:
            logger.info(f"Applying to Greenhouse job: {job_data.get('company')} - {job_data.get('role')}")

            # Navigate to job URL
            try:
                await page.goto(job_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                logger.warning(f"Page load issue: {e}")
                # Try again with shorter timeout
                await page.goto(job_url, wait_until="commit", timeout=15000)

            await self.browser_manager.human_delay(1000, 2000)

            # Check if job is closed
            if await self.is_job_closed(page):
                logger.info("Job is closed/unavailable")
                self._last_status = "closed"
                return False

            # Check for redirect issues
            if not await self.handle_redirects(page, job_url):
                self._last_status = "closed"
                return False

            # Dismiss any popups
            await self.dismiss_popups(page)

            # Check for CAPTCHA
            if not await self.handle_captcha(page):
                return False

            # Check if this is a job listing page that needs to click "Apply"
            if await self._is_job_listing_page(page):
                logger.debug("Job listing page detected - clicking Apply button")
                if not await self._click_apply_button(page):
                    logger.warning("Could not find/click Apply button")
                    return False
                await self.browser_manager.human_delay(1000, 2000)

            # Detect form type
            form_type = await self.detect_form_type(page)
            logger.info(f"Greenhouse form type: {form_type}")

            # Fill the application
            if form_type == "embedded":
                success = await self._fill_embedded_form(page, job_data)
            elif form_type in ("standard", "job_board"):
                success = await self._fill_standard_form(page, job_data)
            else:
                # Unknown form type - try standard form filling anyway as fallback
                logger.warning(f"Unknown form type, attempting standard form fill")
                success = await self._fill_standard_form(page, job_data)

            if not success:
                logger.warning("Form filling returned failure")
                return False

            # In dry run mode, success from _submit_application means form was filled OK
            if self.dry_run:
                logger.info("DRY RUN: Application form filled successfully")
                self._last_status = "success"
                return True

            # Check if application was successful (only for real submissions)
            await self.browser_manager.human_delay(2000, 3000)
            if await self.is_application_complete(page):
                logger.info("Greenhouse application submitted successfully!")
                self._last_status = "success"
                return True

            # Second check after longer wait (some sites have slow redirects)
            await self.browser_manager.human_delay(2000, 3000)
            if await self.is_application_complete(page):
                logger.info("Greenhouse application submitted (detected after delay)")
                self._last_status = "success"
                return True

            # Check for errors
            error = await self.get_error_message(page)
            if error:
                logger.error(f"Greenhouse application error: {error}")

            # Debug: log page state after submit failure
            try:
                url_after = page.url
                title_after = await page.title()
                error_elements = await page.query_selector_all('.field--error, .error, [class*="error"], [class*="invalid"], [aria-invalid="true"]')
                error_count = len(error_elements)
                logger.warning(f"POST-SUBMIT DEBUG: url={url_after[:80]}, title={title_after[:50]}, error_elements={error_count}")
                if error_count > 0:
                    for el in error_elements[:5]:
                        try:
                            el_text = (await el.text_content() or "").strip()[:100]
                            el_class = await el.get_attribute("class") or ""
                            logger.warning(f"  Error element: class='{el_class[:50]}' text='{el_text}'")
                        except Exception:
                            pass
                page_text = (await page.text_content("body") or "").lower()
                if "security code" in page_text or "verification code" in page_text:
                    logger.warning("POST-SUBMIT: Security/verification code field detected on page!")
            except Exception as debug_err:
                logger.debug(f"Post-submit debug error: {debug_err}")

            return False

        except Exception as e:
            logger.error(f"Greenhouse application failed: {e}")
            await self.take_screenshot(page, f"greenhouse_error_{job_data.get('company', 'unknown')}")
            return False

    async def is_application_complete(self, page: Page) -> bool:
        """Greenhouse-specific post-submit verification.

        Much stricter than base class — checks multiple signals to prevent false positives:
        1. Confirmation text MUST appear in the top portion of the page (not buried in job description)
        2. The application form should be GONE (no submit button visible)
        3. No validation errors on page
        """
        try:
            # Check for validation errors — if present, form submission failed
            error_elements = await page.query_selector_all(
                '.field--error, .error-message, [class*="error"]:not([class*="error-"]), '
                '[aria-invalid="true"], .field--has-errors'
            )
            visible_errors = 0
            for el in error_elements:
                try:
                    if await el.is_visible():
                        text = (await el.text_content() or "").strip()
                        if text and len(text) > 2:
                            visible_errors += 1
                except Exception:
                    continue
            if visible_errors > 0:
                logger.debug(f"Post-submit: {visible_errors} validation errors found — NOT complete")
                return False

            # Check if submit button is still visible (form still on page = not submitted)
            submit_btn = await page.query_selector('button[type="submit"], input[type="submit"], #submit_app')
            if submit_btn:
                try:
                    if await submit_btn.is_visible():
                        logger.debug("Post-submit: Submit button still visible — NOT complete")
                        return False
                except Exception:
                    pass

            # Now check for confirmation text — look in multiple locations
            confirmation_result = await page.evaluate('''() => {
                const body = document.body;
                if (!body) return { found: false };

                const fullText = body.textContent.toLowerCase();

                // Strong confirmation indicators (high confidence)
                const strongIndicators = [
                    "application has been submitted",
                    "application received",
                    "application submitted",
                    "successfully applied",
                    "we've received your application",
                    "we have received your application",
                    "application complete",
                    "thanks for applying",
                    "thank you for applying",
                    "thank you for your application",
                    "thank you for your interest",
                    "your application has been received",
                ];

                for (const indicator of strongIndicators) {
                    if (fullText.includes(indicator)) {
                        return { found: true, indicator, confidence: "strong" };
                    }
                }

                // Weaker indicator: "thank you" — but ONLY if the form is gone
                // (to avoid matching "thank you" in job descriptions)
                if (fullText.includes("thank you")) {
                    // Check that there's NO application form on the page
                    const hasForm = document.querySelector(
                        '#application_form, form[action*="application"], ' +
                        'input[name="first_name"], input[name="email"]'
                    );
                    // Check for Greenhouse confirmation page markers
                    const hasConfirmMarker = document.querySelector(
                        '.confirmation, [class*="confirmation"], [class*="success"], ' +
                        '[data-qa="confirmation"], #confirmation'
                    );
                    if (!hasForm || hasConfirmMarker) {
                        return { found: true, indicator: "thank you (form gone)", confidence: "medium" };
                    }
                }

                return { found: false };
            }''')

            if confirmation_result and confirmation_result.get("found"):
                indicator = confirmation_result.get("indicator", "")
                confidence = confirmation_result.get("confidence", "")
                logger.info(f"POST-SUBMIT VERIFIED: '{indicator}' (confidence: {confidence})")
                return True

            # Check if URL changed to a confirmation/success page
            current_url = page.url.lower()
            if any(x in current_url for x in ["/confirmation", "/success", "/thank-you", "/thankyou", "/complete"]):
                logger.info(f"POST-SUBMIT VERIFIED: URL indicates success: {current_url[:80]}")
                return True

            logger.debug(f"Post-submit: No confirmation signals found on {current_url[:60]}")
            return False

        except Exception as e:
            logger.debug(f"Error in is_application_complete: {e}")
            return False

    async def _is_job_listing_page(self, page: Page) -> bool:
        """Check if we're on a job listing page (not the application form)."""
        url = page.url
        title = await page.title()
        logger.debug(f"Checking if job listing page - URL: {url}, Title: {title}")

        # Check for signs of a listing page
        # 1. Page title starts with "Jobs at" (listing page)
        if title.startswith("Jobs at"):
            logger.debug("Detected listing page: title starts with 'Jobs at'")
            return True

        # 2. URL patterns that indicate listing pages
        if "/jobs/" in url and "/application" not in url and "#app" not in url:
            # Check if there's an Apply button visible
            apply_btn = await page.query_selector('a[href*="application"], a:has-text("Apply"), button:has-text("Apply")')
            if apply_btn and await apply_btn.is_visible():
                logger.debug("Detected listing page: /jobs/ URL with Apply button")
                return True

        # 3. Has Apply button but no form inputs
        apply_selectors = [
            'a:has-text("Apply for this job")',
            'a:has-text("Apply Now")',
            'a:has-text("Apply")',
            'button:has-text("Apply for this job")',
            'button:has-text("Apply")',
            '[data-qa="btn-apply"]',
            'a[href*="/application"]',
        ]

        for sel in apply_selectors:
            apply_btn = await page.query_selector(sel)
            if apply_btn and await apply_btn.is_visible():
                # Check if there are actual form fields
                inputs = await page.query_selector_all(
                    'input#first_name, input#email, input[name="first_name"], '
                    'input[name="email"], #application_form input'
                )
                visible_inputs = 0
                for inp in inputs:
                    if await inp.is_visible():
                        visible_inputs += 1

                if visible_inputs == 0:
                    logger.debug(f"Detected listing page: Apply button found ({sel}) but no form inputs")
                    return True
                break

        # 4. Check for job description without form
        job_desc = await page.query_selector('.job-description, [class*="posting-description"], section[class*="content"]')
        form = await page.query_selector('form#application_form, form[action*="greenhouse"], form[class*="application"]')
        if job_desc and not form:
            # Verify no visible form inputs
            inputs = await page.query_selector_all('input[type="text"]:visible, input[type="email"]:visible')
            if len(inputs) == 0:
                logger.debug("Detected listing page: job description but no form")
                return True

        logger.debug("Not a listing page - should be application form")
        return False

    async def _click_apply_button(self, page: Page) -> bool:
        """Click the Apply button to navigate to the application form."""
        apply_selectors = [
            # Most specific first
            'a:has-text("Apply for this job")',
            'a:has-text("Apply Now")',
            'a:has-text("Apply for this Job")',
            'button:has-text("Apply for this job")',
            'button:has-text("Apply Now")',
            'button:has-text("Apply for this Job")',
            '[data-qa="btn-apply"]',
            '.btn-apply',
            '#apply-button',
            'a.btn-apply',
            'a[href*="/application"]',
            'a[href*="/apply"]',
            # More specific patterns
            '.posting-btn-submit',
            '#submit_app',
            'a.button:has-text("Apply")',
            'a.btn:has-text("Apply")',
            # Check for Continue/Submit buttons if form already visible
            'button:has-text("Continue")',
            'button:has-text("Start Application")',
            'button:has-text("Begin Application")',
            # Generic last (may have false positives)
            'a:has-text("Apply")',
            'button:has-text("Apply")',
        ]

        original_url = page.url

        for selector in apply_selectors:
            try:
                btn = await page.query_selector(selector)
                if btn and await btn.is_visible():
                    # Get href to check if it's a link vs button
                    href = await btn.get_attribute("href") or ""
                    logger.info(f"Found Apply button: {selector}, href: {href[:50] if href else 'none'}")

                    # Click the button
                    await btn.click()
                    await self.browser_manager.human_delay(1500, 2500)

                    # Wait for navigation or form to appear
                    try:
                        await page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:
                        pass

                    # Check if we successfully navigated to application
                    new_url = page.url
                    if new_url != original_url:
                        logger.info(f"Navigated to application form: {new_url}")
                        return True

                    # Even if URL same, form might have appeared dynamically
                    form_appeared = await page.query_selector('input#first_name, input[name="first_name"], #application_form')
                    if form_appeared:
                        logger.info("Application form appeared after clicking Apply")
                        return True

                    # Give it one more chance - wait for form elements
                    try:
                        await page.wait_for_selector('input#first_name, input[name="first_name"]', timeout=5000)
                        logger.info("Form inputs appeared after clicking Apply")
                        return True
                    except Exception:
                        pass

            except Exception as e:
                logger.debug(f"Apply button selector {selector} failed: {e}")

        # Last resort: check if we can navigate directly to an application URL
        try:
            current_url = page.url
            # Try appending #app or /application to the URL
            if "/jobs/" in current_url:
                app_url = current_url + "#app"
                await page.goto(app_url)
                await self.browser_manager.human_delay(1000, 2000)

                # Check if form appeared
                form_appeared = await page.query_selector('input#first_name, input[name="first_name"], #application_form')
                if form_appeared:
                    logger.info("Navigated directly to application form via #app")
                    return True
        except Exception as e:
            logger.debug(f"Direct navigation failed: {e}")

        logger.warning("Could not find or click Apply button")
        return False

    async def detect_form_type(self, page: Page) -> str:
        """Detect type of Greenhouse form."""
        url = page.url
        logger.debug(f"Detecting form type for URL: {url}")

        # Check for embedded iframe form
        iframe = await page.query_selector('iframe[src*="greenhouse"]')
        if iframe:
            logger.debug("Form type: embedded (iframe detected)")
            return "embedded"

        # Check for standard application form
        form = await page.query_selector('#application_form, form[action*="greenhouse"], form[method="post"]')
        if form:
            logger.debug("Form type: standard (form element detected)")
            return "standard"

        # Check for job-boards.greenhouse.io style with inputs
        if "job-boards.greenhouse.io" in url or "boards.greenhouse.io" in url:
            # Verify there are form inputs
            inputs = await page.query_selector_all('input#first_name, input#email, input[name="first_name"], input[name="email"]')
            visible_count = 0
            for inp in inputs:
                if await inp.is_visible():
                    visible_count += 1
            if visible_count > 0:
                logger.debug(f"Form type: job_board ({visible_count} inputs found)")
                return "job_board"

        # Check for any visible form inputs (fallback detection)
        basic_inputs = await page.query_selector_all('input[type="text"], input[type="email"]')
        for inp in basic_inputs:
            if await inp.is_visible():
                name = await inp.get_attribute("name") or await inp.get_attribute("id") or ""
                if any(x in name.lower() for x in ["first", "name", "email"]):
                    logger.debug(f"Form type: standard (detected via input: {name})")
                    return "standard"

        logger.warning(f"Form type: unknown - no form elements detected on {url}")
        return "unknown"

    async def _fill_standard_form(self, page: Page, job_data: Dict[str, Any]) -> bool:
        """Fill standard Greenhouse application form."""
        try:
            # Fill basic fields using form filler
            await self.form_filler.fill_form(page)
            await self.browser_manager.human_delay(500, 1000)

            # CRITICAL: Re-verify and trigger events on basic fields (fixes React validation)
            await self._verify_and_trigger_basic_fields(page)
            await self.browser_manager.human_delay(300, 500)

            # Fill Greenhouse-specific education fields (hidden inputs)
            await self._fill_greenhouse_education_fields(page)
            await self.browser_manager.human_delay(300, 600)

            # Fill country field (React-Select dropdown)
            await self._fill_greenhouse_country(page)
            await self.browser_manager.human_delay(200, 400)

            # Fill location field specifically (handles hidden input sync)
            await self._fill_greenhouse_location(page)
            await self.browser_manager.human_delay(300, 600)

            # Upload resume
            resume_path = self._resolve_file_path(self.form_filler.config.get("files", {}).get("resume"))
            if resume_path:
                uploaded = await self._upload_resume(page, resume_path)
                if uploaded:
                    await self._verify_resume_upload(page)

            # Upload cover letter if available
            cover_letter_path = self._resolve_file_path(self.form_filler.config.get("files", {}).get("cover_letter"))
            if cover_letter_path:
                await self._upload_cover_letter(page, cover_letter_path)

            # Upload transcript if available and there's a field for it
            transcript_path = self._resolve_file_path(self.form_filler.config.get("files", {}).get("transcript"))
            if transcript_path:
                await self._upload_transcript(page, transcript_path)

            # Handle custom questions
            await self._handle_custom_questions(page, job_data)

            # Fill demographic questions if present
            await self._fill_demographics(page)

            # CRITICAL: Sync all hidden question_XXXXXXXX inputs that might still be empty
            await self._sync_all_hidden_question_inputs(page)

            # Handle email verification code if present (must be before submit)
            await self._handle_email_verification(page)

            # Submit
            return await self._submit_application(page)

        except Exception as e:
            logger.error(f"Error filling Greenhouse form: {e}")
            return False

    async def _fill_embedded_form(self, page: Page, job_data: Dict[str, Any]) -> bool:
        """Fill embedded Greenhouse iframe form."""
        try:
            # Find and switch to iframe
            iframe = await page.query_selector('iframe[src*="greenhouse"]')
            if not iframe:
                logger.warning("Could not find Greenhouse iframe, falling back to standard form")
                return await self._fill_standard_form(page, job_data)

            frame = await iframe.content_frame()
            if not frame:
                logger.warning("Could not access iframe content, falling back to standard form")
                return await self._fill_standard_form(page, job_data)

            logger.info("Accessing embedded Greenhouse iframe")

            # Check if iframe actually has form content
            form_check = await frame.query_selector('input#first_name, input[name="first_name"], input[name="email"]')
            if not form_check:
                logger.warning("Iframe doesn't contain form inputs, falling back to standard form")
                return await self._fill_standard_form(page, job_data)

            # Fill form in iframe context using the full form filler
            # Note: Playwright handles frames, so we work with the frame like a page
            logger.info("Filling embedded form fields")

            # Use the form filler directly on the frame
            filled = await self.form_filler.fill_form(frame)
            logger.info(f"Filled {len(filled)} fields in embedded form")
            await self.browser_manager.human_delay(500, 1000)

            # Fill education fields in iframe
            await self._fill_greenhouse_education_fields_in_frame(frame)
            await self.browser_manager.human_delay(300, 600)

            # Fill location field in iframe
            await self._fill_greenhouse_location_in_frame(frame)
            await self.browser_manager.human_delay(300, 600)

            # Upload resume
            resume_path = self._resolve_file_path(self.form_filler.config.get("files", {}).get("resume"))
            if resume_path:
                uploaded = await self._upload_resume_in_frame(frame, resume_path)
                if uploaded:
                    logger.info("Resume uploaded to iframe")

            # Upload cover letter if available
            cover_letter_path = self._resolve_file_path(self.form_filler.config.get("files", {}).get("cover_letter"))
            if cover_letter_path:
                await self._upload_cover_letter_in_frame(frame, cover_letter_path)

            # Handle custom questions
            await self._handle_custom_questions_in_frame(frame, job_data)

            # Fill demographic questions
            await self._fill_demographics_in_frame(frame)

            # CRITICAL: Sync all hidden question_XXXXXXXX inputs in iframe
            await self._sync_all_hidden_question_inputs(frame)

            # Handle email verification code if present (check parent page — code field is outside iframe)
            await self._handle_email_verification(page)

            # Submit (check for dry run)
            if self.dry_run:
                logger.info("DRY RUN: Embedded form filled, running validation on iframe")
                return await self._run_dry_run_validation(frame)

            # Solve invisible reCAPTCHA before submit (check parent page - reCAPTCHA is usually there)
            captcha_solved = await self.solve_invisible_recaptcha(page)
            if not captcha_solved:
                logger.error("Failed to solve reCAPTCHA for embedded form - cannot submit")
                return False

            submit_selectors = [
                'button[type="submit"]',
                'input[type="submit"]',
                'button:has-text("Submit")',
                'button:has-text("Apply")',
                '#submit_app',
            ]

            for sel in submit_selectors:
                submit_btn = await frame.query_selector(sel)
                if submit_btn and await submit_btn.is_visible():
                    await self.browser_manager.human_delay(500, 1000)
                    await submit_btn.click()
                    logger.info("Clicked submit button in iframe")
                    await self.browser_manager.human_delay(2000, 3000)
                    return True

            # No submit in frame - maybe it's on the parent page?
            logger.warning("No submit button found in iframe, checking parent page")
            return await self._submit_application(page)

        except Exception as e:
            logger.error(f"Error with embedded Greenhouse form: {e}")
            # Fall back to standard form
            logger.info("Falling back to standard form after iframe error")
            return await self._fill_standard_form(page, job_data)

    async def _fill_greenhouse_education_fields_in_frame(self, frame) -> None:
        """Fill Greenhouse education fields in iframe context."""
        # Reuse the same logic but with frame instead of page
        education = self.form_filler.config.get("education", [])
        if isinstance(education, list) and education:
            edu = education[0]
        else:
            edu = education if isinstance(education, dict) else {}

        if not edu:
            return

        logger.info("Filling education fields in iframe")

        # Find all React-select dropdowns in the frame
        dropdowns = await frame.query_selector_all('.select__control, [role="combobox"]')

        for dropdown in dropdowns:
            try:
                if not await dropdown.is_visible():
                    continue

                label_text = await dropdown.evaluate('''(el) => {
                    let container = el.closest('.field, .select, [class*="field"]');
                    if (!container) container = el.parentElement?.parentElement?.parentElement;
                    if (!container) return "";
                    let label = container.querySelector("label, .select__label, [class*='label']");
                    return label ? label.textContent.toLowerCase().trim() : "";
                }''')

                value = None
                keywords = []

                if any(x in label_text for x in ["school", "university", "college"]):
                    value = edu.get("school", "")
                    keywords = ["school"]
                elif "degree" in label_text and "discipline" not in label_text:
                    value = edu.get("degree", "Bachelor's degree")
                    keywords = ["degree"]
                elif any(x in label_text for x in ["discipline", "major", "field of study"]):
                    value = edu.get("field_of_study", "Software Engineering")
                    keywords = ["discipline"]
                elif any(x in label_text for x in ["end month", "graduation month"]):
                    value = "May"
                    keywords = ["month"]
                elif any(x in label_text for x in ["end year", "graduation year", "expected"]):
                    grad = edu.get("graduation_date", "May 2026")
                    import re
                    year_match = re.search(r'20\d{2}', str(grad))
                    value = year_match.group() if year_match else "2026"
                    keywords = ["year"]

                if not value:
                    continue

                # Check if already filled — check BOTH display AND hidden input
                display_val = await dropdown.evaluate('''(el) => {
                    const sv = el.querySelector('.select__single-value, [class*="singleValue"]');
                    if (sv && sv.textContent && sv.textContent.trim() !== "Select..." && sv.textContent.trim().length > 1) {
                        return sv.textContent.trim();
                    }
                    return "";
                }''')
                if display_val:
                    logger.debug(f"Iframe dropdown '{label_text}' already filled: {display_val}")
                    continue

                logger.info(f"Filling iframe dropdown: {label_text} -> {value}")

                # Discipline alternatives
                values_to_try = [value]
                if "discipline" in keywords:
                    values_to_try = [value, "Engineering", "Computer Science", "Computer Engineering"]

                found = False
                for try_value in values_to_try:
                    await dropdown.click()
                    await self.browser_manager.human_delay(400, 600)

                    if "school" in keywords:
                        await frame.page.keyboard.type(try_value[:20], delay=50)
                        await self.browser_manager.human_delay(600, 900)

                    found = await self.form_filler._select_dropdown_option(frame, try_value)
                    if found:
                        value = try_value
                        break
                    await frame.page.keyboard.press("Escape")
                    await self.browser_manager.human_delay(200, 300)

                if not found and "school" in keywords:
                    await dropdown.click()
                    await self.browser_manager.human_delay(300, 500)
                    await frame.page.keyboard.type(value[:20], delay=50)
                    await self.browser_manager.human_delay(600, 900)
                    await frame.page.keyboard.press("Enter")
                    found = True

                await self.browser_manager.human_delay(300, 500)

            except Exception as e:
                logger.debug(f"Error filling iframe education dropdown: {e}")

    async def _fill_greenhouse_location_in_frame(self, frame) -> None:
        """Fill location field in iframe context."""
        personal = self.form_filler.config.get("personal_info", {})
        city = personal.get("city", "")
        if not city:
            return

        location_input = await frame.query_selector('input#candidate-location, input[name="candidate-location"], .geosuggest input')
        if not location_input or not await location_input.is_visible():
            return

        current = await location_input.input_value()
        if current and len(current.strip()) > 2:
            return

        logger.info(f"Filling location in iframe: {city}")
        await location_input.click()
        await location_input.type(city, delay=80)
        await self.browser_manager.human_delay(800, 1200)

        # Try to select from autocomplete
        await frame.page.keyboard.press("ArrowDown")
        await self.browser_manager.human_delay(200, 400)
        await frame.page.keyboard.press("Enter")
        await self.browser_manager.human_delay(300, 500)

    async def _fill_demographics_in_frame(self, frame) -> None:
        """Fill optional demographic questions in iframe."""
        demographics = self.form_filler.config.get("demographics", {})
        logger.info("Filling demographics in iframe")

        # Gender
        gender_select = await frame.query_selector('select[name*="gender"], select[id*="gender"]')
        if gender_select and demographics.get("gender"):
            try:
                await gender_select.select_option(label=demographics["gender"])
                logger.info("Filled gender select in iframe")
            except Exception:
                pass

        # Ethnicity
        ethnicity_select = await frame.query_selector(
            'select[name*="race"], select[name*="ethnic"], select[id*="ethnic"]'
        )
        if ethnicity_select and demographics.get("ethnicity"):
            try:
                await ethnicity_select.select_option(label=demographics["ethnicity"])
                logger.info("Filled ethnicity select in iframe")
            except Exception:
                pass

        # Veteran
        veteran_select = await frame.query_selector('select[name*="veteran"], select[id*="veteran"]')
        if veteran_select and demographics.get("veteran_status"):
            try:
                await veteran_select.select_option(label=demographics["veteran_status"])
                logger.info("Filled veteran status select in iframe")
            except Exception:
                pass

        # Also handle React-select style dropdowns in iframe
        await self._fill_react_select_demographics_in_frame(frame, demographics)

    async def _fill_react_select_demographics_in_frame(self, frame, demographics: Dict) -> None:
        """Fill React-select style demographics dropdowns in iframe."""
        demographic_mappings = [
            ("gender", demographics.get("gender", "Prefer not to say")),
            ("race", demographics.get("ethnicity", "Prefer not to say")),
            ("ethnic", demographics.get("ethnicity", "Prefer not to say")),
            ("veteran", demographics.get("veteran_status", "No")),
            ("disability", demographics.get("disability_status", "Prefer not to say")),
        ]

        dropdowns = await frame.query_selector_all('.select__control, [role="combobox"]')

        for dropdown in dropdowns:
            try:
                if not await dropdown.is_visible():
                    continue

                label_text = await dropdown.evaluate('''(el) => {
                    let container = el.closest('.field, .select, [class*="field"]');
                    if (!container) container = el.parentElement?.parentElement?.parentElement;
                    if (!container) return "";
                    let label = container.querySelector("label, .select__label, [class*='label']");
                    return label ? label.textContent.toLowerCase().trim() : "";
                }''')

                for keyword, value in demographic_mappings:
                    if keyword not in label_text:
                        continue

                    # Check if already filled
                    current_text = (await dropdown.text_content() or "").strip().lower()
                    if current_text and "select" not in current_text and "choose" not in current_text:
                        continue

                    logger.info(f"Filling iframe demographics dropdown: {label_text} -> {value}")
                    await dropdown.click()
                    await self.browser_manager.human_delay(400, 600)

                    # Find and click option
                    options = await frame.query_selector_all('.select__option, [class*="option"], [role="option"]')
                    for opt in options:
                        if not await opt.is_visible():
                            continue
                        opt_text = (await opt.text_content() or "").strip().lower()

                        if value.lower() in opt_text:
                            await opt.click()
                            break
                        # Handle "prefer not to say" variations
                        if "prefer" in value.lower() and any(x in opt_text for x in ["prefer not", "decline", "do not want"]):
                            await opt.click()
                            break
                    else:
                        await frame.page.keyboard.press("Escape")

                    await self.browser_manager.human_delay(200, 400)
                    break

            except Exception as e:
                logger.debug(f"Error filling iframe demographics dropdown: {e}")

    async def _fill_basic_fields(self, page_or_frame) -> None:
        """Fill basic application fields."""
        field_mappings = {
            'input[name="first_name"], #first_name': self.form_filler.config.get("personal_info", {}).get("first_name"),
            'input[name="last_name"], #last_name': self.form_filler.config.get("personal_info", {}).get("last_name"),
            'input[name="email"], #email': self.form_filler.config.get("personal_info", {}).get("email"),
            'input[name="phone"], #phone': self.form_filler.config.get("personal_info", {}).get("phone"),
        }

        for selector, value in field_mappings.items():
            if value:
                try:
                    element = await page_or_frame.query_selector(selector)
                    if element and await element.is_visible():
                        await element.fill(str(value))
                        await self.browser_manager.human_delay(100, 300)
                except Exception as e:
                    logger.debug(f"Could not fill {selector}: {e}")

    async def _upload_resume(self, page: Page, resume_path: str) -> bool:
        """Upload resume file."""
        try:
            # Greenhouse typically uses data-field="resume" or similar
            file_input = await page.query_selector(
                'input[type="file"][data-field*="resume"], '
                'input[type="file"][name*="resume"], '
                'input[type="file"]#resume, '
                'input[type="file"]'
            )

            if file_input:
                await file_input.set_input_files(resume_path)
                # Dispatch change event to trigger React's file handler
                await file_input.evaluate('''(el) => {
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                }''')
                logger.info("Resume uploaded successfully")
                await self.browser_manager.human_delay(1000, 2000)
                return True

            # Try clicking a button that triggers file upload
            upload_btn = await page.query_selector(
                'button:has-text("Upload"), '
                'button:has-text("Attach"), '
                '[data-field="resume"] button'
            )
            if upload_btn:
                async with page.expect_file_chooser() as fc_info:
                    await upload_btn.click()
                file_chooser = await fc_info.value
                await file_chooser.set_files(resume_path)
                logger.info("Resume uploaded via button")
                return True

        except Exception as e:
            logger.warning(f"Could not upload resume: {e}")

        return False

    async def _upload_resume_in_frame(self, frame, resume_path: str) -> bool:
        """Upload resume in iframe context."""
        try:
            file_input = await frame.query_selector('input[type="file"]')
            if file_input:
                await file_input.set_input_files(resume_path)
                logger.info("Resume uploaded in iframe")
                return True
        except Exception as e:
            logger.warning(f"Could not upload resume in iframe: {e}")
        return False

    async def _upload_cover_letter(self, page: Page, cover_letter_path: str) -> bool:
        """Upload cover letter file if there's a field for it."""
        try:
            # Look for cover letter specific file inputs
            file_input = await page.query_selector(
                'input[type="file"][data-field*="cover"], '
                'input[type="file"][name*="cover"], '
                'input[type="file"]#cover_letter, '
                'input[type="file"][id*="cover"]'
            )

            if file_input:
                await file_input.set_input_files(cover_letter_path)
                logger.info("Cover letter uploaded successfully")
                await self.browser_manager.human_delay(500, 1000)
                return True

            # Try to find any second file input (first is usually resume)
            all_file_inputs = await page.query_selector_all('input[type="file"]')
            if len(all_file_inputs) >= 2:
                # Second file input is often cover letter
                await all_file_inputs[1].set_input_files(cover_letter_path)
                logger.info("Cover letter uploaded to second file input")
                return True

            # Check for a labeled cover letter section
            cover_sections = await page.query_selector_all('[data-field*="cover"], [class*="cover-letter"], [id*="cover"]')
            for section in cover_sections:
                file_input = await section.query_selector('input[type="file"]')
                if file_input:
                    await file_input.set_input_files(cover_letter_path)
                    logger.info("Cover letter uploaded via labeled section")
                    return True

            logger.debug("No cover letter field found")
            return False

        except Exception as e:
            logger.debug(f"Could not upload cover letter: {e}")
        return False

    async def _upload_transcript(self, page: Page, transcript_path: str) -> bool:
        """Upload transcript file if there's a field for it."""
        try:
            # Look for transcript-specific upload labels
            upload_labels = await page.query_selector_all('.upload-label, label[class*="upload"]')
            for label in upload_labels:
                label_text = ((await label.text_content()) or "").lower()
                if "transcript" in label_text:
                    # Find the file input in or near this label
                    parent = await label.evaluate_handle('el => el.closest(".field, .upload-field, .form-field, div")')
                    if parent:
                        file_input = await parent.query_selector('input[type="file"]')
                        if file_input:
                            await file_input.set_input_files(transcript_path)
                            logger.info(f"Transcript uploaded successfully")
                            await self.browser_manager.human_delay(500, 1000)
                            return True

            # Try by data-field or name attributes
            file_input = await page.query_selector(
                'input[type="file"][data-field*="transcript"], '
                'input[type="file"][name*="transcript"], '
                'input[type="file"][id*="transcript"]'
            )
            if file_input:
                await file_input.set_input_files(transcript_path)
                logger.info("Transcript uploaded via attribute match")
                await self.browser_manager.human_delay(500, 1000)
                return True

            logger.debug("No transcript field found")
            return False

        except Exception as e:
            logger.debug(f"Could not upload transcript: {e}")
            return False

    async def _upload_cover_letter_in_frame(self, frame, cover_letter_path: str) -> bool:
        """Upload cover letter in iframe context."""
        try:
            # Look for cover letter specific inputs
            file_inputs = await frame.query_selector_all('input[type="file"]')
            if len(file_inputs) >= 2:
                await file_inputs[1].set_input_files(cover_letter_path)
                logger.info("Cover letter uploaded in iframe")
                return True

            # Try data-field attribute
            cover_input = await frame.query_selector('input[type="file"][data-field*="cover"]')
            if cover_input:
                await cover_input.set_input_files(cover_letter_path)
                logger.info("Cover letter uploaded in iframe via data-field")
                return True

        except Exception as e:
            logger.debug(f"Could not upload cover letter in iframe: {e}")
        return False

    async def _handle_custom_questions(self, page: Page, job_data: Dict[str, Any]) -> None:
        """Handle custom application questions using AI."""
        # Standard field IDs to skip - these should already be filled
        standard_fields = {
            'first_name', 'last_name', 'email', 'phone', 'resume', 'cover_letter',
            'country', 'candidate-location', 'address', 'city', 'state', 'zip',
            'linkedin', 'github', 'website', 'portfolio',
            'school', 'degree', 'discipline', 'gpa', 'graduation',
        }

        # FIRST: Handle Greenhouse question_XXXXXXXX inputs directly
        await self._fill_greenhouse_question_inputs(page, standard_fields)

        # Find custom question fields - be more specific to avoid matching basic fields
        custom_fields = await page.query_selector_all(
            '.custom-question, '
            '[data-qa="custom-question"], '
            '.application-question'
        )

        # Also find all unfilled textareas directly (fallback for forms without .custom-question)
        all_textareas = await page.query_selector_all('textarea')
        for textarea in all_textareas:
            try:
                if not await textarea.is_visible():
                    continue

                # Skip if already has content
                current_value = await textarea.input_value()
                if current_value and len(current_value.strip()) > 0:
                    continue

                # Get the question text from nearby label
                textarea_id = await textarea.get_attribute("id") or ""
                textarea_name = await textarea.get_attribute("name") or ""

                # Skip standard fields
                if any(std in textarea_id.lower() or std in textarea_name.lower() for std in standard_fields):
                    continue

                # Find label for this textarea
                question_text = await self._get_textarea_label(page, textarea, textarea_id)
                if not question_text:
                    question_text = f"Please provide additional information for {textarea_name or textarea_id or 'this field'}"

                logger.info(f"Found unfilled textarea question: {question_text[:60]}...")

                # Use AI to answer the question
                answer = await self.ai_answerer.answer_question(
                    question_text,
                    field_type="textarea",
                    max_length=500
                )

                if answer:
                    await textarea.fill(answer)
                    logger.info(f"AI filled textarea: {question_text[:40]}... with {len(answer)} chars")
                    await self.browser_manager.human_delay(300, 600)

            except Exception as e:
                logger.debug(f"Error handling textarea: {e}")

        for field in custom_fields:
            try:
                # Get question text
                label = await field.query_selector('label, .field-label, .question-text')
                if not label:
                    continue

                question_text = await label.text_content()
                if not question_text:
                    continue

                # Find input element
                input_elem = await field.query_selector(
                    'input:not([type="hidden"]):not([type="file"]), textarea, select'
                )
                if not input_elem:
                    continue

                # Check if this is a standard field we should skip
                input_id = await input_elem.get_attribute("id") or ""
                input_name = await input_elem.get_attribute("name") or ""

                # Skip standard fields
                if any(std in input_id.lower() or std in input_name.lower() for std in standard_fields):
                    logger.debug(f"Skipping standard field: {input_id or input_name}")
                    continue

                # Skip if field already has a value
                try:
                    current_value = await input_elem.input_value()
                    if current_value and len(current_value.strip()) > 0:
                        logger.debug(f"Skipping already-filled field: {input_id or question_text[:30]}")
                        continue
                except Exception:
                    pass

                # Determine input type
                tag_name = await input_elem.evaluate("el => el.tagName.toLowerCase()")

                if tag_name == "select":
                    # Get options and let AI choose
                    options = await input_elem.query_selector_all("option")
                    option_texts = []
                    for opt in options:
                        text = await opt.text_content()
                        if text and text.strip():
                            option_texts.append(text.strip())

                    if option_texts:
                        answer = await self.ai_answerer.answer_question(
                            question_text,
                            field_type="select",
                            options=option_texts
                        )
                        if answer:  # Only fill if AI returned a valid answer
                            await input_elem.select_option(label=answer)

                elif tag_name == "textarea":
                    answer = await self.ai_answerer.answer_question(
                        question_text,
                        field_type="textarea",
                        max_length=500
                    )
                    if answer:  # Only fill if AI returned a valid answer
                        await input_elem.fill(answer)

                else:
                    # Text input
                    input_type = await input_elem.get_attribute("type") or "text"
                    if input_type in ("text", ""):
                        answer = await self.ai_answerer.answer_question(
                            question_text,
                            field_type="text",
                            max_length=200
                        )
                        if answer:  # Only fill if AI returned a valid answer
                            await input_elem.fill(answer)

                await self.browser_manager.human_delay(300, 600)

            except Exception as e:
                logger.debug(f"Error handling custom question: {e}")

    async def _fill_greenhouse_question_inputs(self, page, standard_fields: set) -> None:
        """Fill Greenhouse custom questions with IDs like question_XXXXXXXX."""
        try:
            # First, handle checkboxes for terms/agreements
            await self._handle_checkbox_questions(page)

            # Find all inputs with IDs starting with "question_"
            question_inputs = await page.query_selector_all(
                'input[id^="question_"], textarea[id^="question_"], select[id^="question_"]'
            )

            logger.debug(f"Found {len(question_inputs)} question_* inputs")

            for inp in question_inputs:
                try:
                    inp_id = await inp.get_attribute("id") or ""
                    inp_type = await inp.get_attribute("type") or ""
                    tag_name = await inp.evaluate("el => el.tagName.toLowerCase()")

                    is_visible = await inp.is_visible()
                    logger.info(f"Processing question {inp_id}: type={inp_type}, tag={tag_name}, visible={is_visible}")

                    if not is_visible:
                        logger.debug(f"Skipping {inp_id} - not visible")
                        continue

                    # Skip hidden, file, and checkbox inputs (checkboxes handled separately)
                    if inp_type in ("hidden", "file", "checkbox"):
                        logger.debug(f"Skipping {inp_id} - type is {inp_type}")
                        continue

                    # Check if already filled
                    try:
                        current_value = await inp.input_value() if tag_name != "select" else ""
                        if current_value and len(current_value.strip()) > 0:
                            logger.debug(f"Skipping {inp_id} - already has value: {current_value[:20]}")
                            continue
                    except Exception:
                        pass

                    # Get question text from label
                    question_text = await self._get_question_label(page, inp, inp_id)
                    if not question_text:
                        question_text = f"Additional information required for {inp_id}"

                    logger.info(f"Found Greenhouse question input: {question_text[:50]}...")

                    # Get answer based on field type
                    if tag_name == "select":
                        options = await inp.query_selector_all("option")
                        option_texts = []
                        for opt in options:
                            text = (await opt.text_content() or "").strip()
                            if text and text != "Select...":
                                option_texts.append(text)

                        if option_texts:
                            answer = await self.ai_answerer.answer_question(
                                question_text, "select", options=option_texts
                            )
                            if answer:
                                try:
                                    await inp.select_option(label=answer)
                                    logger.info(f"AI filled select {inp_id}: {answer}")
                                except Exception:
                                    # Try by value if label fails
                                    pass

                    elif tag_name == "textarea":
                        answer = await self.ai_answerer.answer_question(
                            question_text, "textarea", max_length=500
                        )
                        if answer:
                            await inp.fill(answer)
                            logger.info(f"AI filled textarea {inp_id}")

                    else:
                        # Text input - check if it's actually part of a React-Select
                        # First try to find associated React-Select dropdown
                        logger.info(f"Trying React-Select fill for {inp_id}")
                        react_select_filled = await self._fill_associated_react_select(page, inp, inp_id, question_text)
                        logger.info(f"React-Select fill result for {inp_id}: {react_select_filled}")

                        if not react_select_filled:
                            # Fall back to direct text input fill
                            logger.info(f"Falling back to text input fill for {inp_id}")
                            # Check if this input is inside a React-Select container
                            # If so, it's really a dropdown, not a text field
                            is_in_react_select = await inp.evaluate('''(el) => {
                                let current = el.parentElement;
                                for (let i = 0; i < 5 && current; i++) {
                                    if (current.querySelector('.select__control, [role="combobox"]')) return true;
                                    current = current.parentElement;
                                }
                                return false;
                            }''')
                            actual_field_type = "select" if is_in_react_select else "text"
                            answer = await self.ai_answerer.answer_question(
                                question_text, actual_field_type, max_length=200
                            )
                            logger.info(f"AI answer for {inp_id}: {answer[:50] if answer else 'None'}...")
                            if answer:
                                await inp.fill(answer)
                                logger.info(f"AI filled text input {inp_id}")
                                # Trigger change event to ensure React picks up the value
                                await inp.evaluate('(el) => el.dispatchEvent(new Event("change", {bubbles: true}))')
                            else:
                                logger.warning(f"No answer for {inp_id}: {question_text[:50]}")

                    await self.browser_manager.human_delay(200, 400)

                except Exception as e:
                    logger.debug(f"Error filling question input {inp_id}: {e}")

        except Exception as e:
            logger.debug(f"Error in _fill_greenhouse_question_inputs: {e}")

    async def _fill_associated_react_select(self, page, inp, inp_id: str, question_text: str) -> bool:
        """
        Try to find and fill a React-Select dropdown associated with a hidden input.
        Returns True if successfully filled, False if no React-Select found.
        """
        try:
            # Find the React-Select that's specifically associated with this input
            # We need to find the closest parent that contains ONLY this field
            react_select = await inp.evaluate_handle('''(el) => {
                // Start from the input and go up until we find a React-Select
                let current = el.parentElement;
                for (let i = 0; i < 5 && current; i++) {
                    // Look for React-Select in this level
                    let select = current.querySelector('.select__control, [role="combobox"]');
                    if (select) {
                        // Make sure this is the right field by checking the container
                        // doesn't have OTHER question_ inputs between them
                        let allInputs = current.querySelectorAll('input[id^="question_"]:not([type="hidden"])');
                        // If there's only 1 input (our target) or the select is very close, use it
                        if (allInputs.length <= 1) {
                            return select;
                        }
                    }
                    current = current.parentElement;
                }
                return null;
            }''')

            if not react_select:
                return False

            react_select_elem = react_select.as_element()
            if not react_select_elem or not await react_select_elem.is_visible():
                return False

            # Check if dropdown already has a value (not placeholder)
            current_text = (await react_select_elem.text_content() or "").strip().lower()
            logger.info(f"React-Select {inp_id} current text: '{current_text[:50]}...'")
            if current_text and "select" not in current_text and "choose" not in current_text and len(current_text) > 2:
                logger.info(f"React-Select for {inp_id} already has value: {current_text[:30]}")
                return True

            # Get answer from AI/config
            answer = await self.ai_answerer.answer_question(question_text, "select", max_length=50)
            if not answer:
                answer = self.form_filler._get_dropdown_value_for_label(question_text)

            if not answer:
                return False

            logger.info(f"Filling React-Select for {inp_id} with: {answer}")

            # Click to open dropdown
            await react_select_elem.click()
            await self.browser_manager.human_delay(600, 900)

            # Wait for dropdown menu to appear
            try:
                await page.wait_for_selector('.select__menu, [class*="menu"]', timeout=2000)
            except Exception:
                logger.debug(f"Dropdown menu not found for {inp_id}, may already be open or text input")

            # Smart answer adjustment: if answer is "Yes"/"No" but options are city names,
            # try to map from the question text to the right city
            if answer.lower() in ("yes", "no"):
                options = await page.query_selector_all('.select__option, [role="option"]')
                option_texts = []
                for opt in options:
                    if await opt.is_visible():
                        t = (await opt.text_content() or "").strip()
                        if t:
                            option_texts.append(t)

                # Check if options look like city names (no "Yes"/"No" in options)
                has_yes_no = any(t.lower() in ("yes", "no") for t in option_texts)
                if not has_yes_no and option_texts:
                    q_lower = question_text.lower()
                    city_map = {
                        "san francisco": "SF", "sf": "SF",
                        "new york": "NYC", "nyc": "NYC",
                        "los angeles": "LA", "la": "LA",
                        "chicago": "Chicago", "seattle": "Seattle",
                        "boston": "Boston", "austin": "Austin",
                        "denver": "Denver", "london": "London",
                    }
                    for city_name, abbrev in city_map.items():
                        if city_name in q_lower:
                            # Check if this abbreviation or city is in options
                            for opt_text in option_texts:
                                if abbrev.lower() == opt_text.lower() or city_name in opt_text.lower():
                                    answer = opt_text
                                    logger.info(f"Adjusted answer from Yes/No to city: {answer}")
                                    break
                            break

                    # If still Yes/No and options are non-Yes/No, try matching from config city
                    if answer.lower() in ("yes", "no"):
                        personal = self.form_filler.config.get("personal_info", {})
                        config_city = personal.get("city", "").lower()
                        for opt_text in option_texts:
                            if config_city and config_city in opt_text.lower():
                                answer = opt_text
                                logger.info(f"Adjusted answer to config city match: {answer}")
                                break

                    # Close and re-open dropdown since options may have changed
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(200)
                    await react_select_elem.click()
                    await self.browser_manager.human_delay(400, 600)

            # Smart pre-processing: map graduation date to semester if needed
            # "May 2026" → "Spring 2026" for dropdowns with semester-based options
            import re as _re_gh
            answer_alternatives = [answer]
            month_to_semester = {
                "january": "Spring", "february": "Spring", "march": "Spring",
                "april": "Spring", "may": "Spring",
                "june": "Summer", "july": "Summer", "august": "Summer",
                "september": "Fall", "october": "Fall", "november": "Fall", "december": "Fall",
            }
            year_match = _re_gh.search(r'20\d{2}', answer)
            if year_match:
                year = year_match.group()
                for month, semester in month_to_semester.items():
                    if month in answer.lower():
                        answer_alternatives.append(f"{semester} {year}")
                        answer_alternatives.append(year)
                        break

            # "Online Job Board" / "LinkedIn" alternatives for referral source
            answer_lower_check = answer.lower()
            if any(x in answer_lower_check for x in ["online job board", "job board"]):
                answer_alternatives.extend(["LinkedIn", "Internet", "Other"])
            elif "linkedin" in answer_lower_check:
                answer_alternatives.extend(["Online Job Board", "Internet", "Other"])

            # Strategy 1: Type-to-search — use React-Select's native search behavior
            # Types the answer text, waits for filter, then picks the BEST match (not just first).
            found = False
            try:
                # Type the answer text to search/filter
                await page.keyboard.type(answer[:30], delay=30)
                await page.wait_for_timeout(600)

                # Check if there are filtered options
                filtered_options = await page.query_selector_all('.select__option, [role="option"]')
                visible_options = []
                for opt in filtered_options:
                    if await opt.is_visible():
                        text = (await opt.text_content() or "").strip()
                        if text:
                            visible_options.append((opt, text))

                if visible_options:
                    # Use proper matching instead of blindly pressing Enter
                    # Build option_data for _find_best_option_match
                    option_data = [(i, opt, text, text.lower()) for i, (opt, text) in enumerate(visible_options)]
                    best_idx = self.form_filler._find_best_option_match(answer.lower(), option_data)

                    if best_idx is not None:
                        # Click the best matching option directly
                        target_opt = option_data[best_idx][1]
                        matched_text = option_data[best_idx][2]
                        try:
                            await target_opt.click()
                            await page.wait_for_timeout(400)
                            found = True
                            logger.info(f"Selected React-Select option via type-to-search: {matched_text[:50]}")
                        except Exception:
                            await page.keyboard.press("Enter")
                            await page.wait_for_timeout(400)
                            found = True
                    else:
                        # No good match with typed text — clear and try alternatives
                        await page.keyboard.press("Escape")
                        await page.wait_for_timeout(200)

                        # Try semester alternatives (e.g., "Spring 2026" for "May 2026")
                        for alt_answer in answer_alternatives[1:]:
                            await react_select_elem.click()
                            await self.browser_manager.human_delay(300, 500)
                            await page.keyboard.type(alt_answer[:30], delay=30)
                            await page.wait_for_timeout(600)

                            alt_options = await page.query_selector_all('.select__option, [role="option"]')
                            alt_visible = []
                            for opt in alt_options:
                                if await opt.is_visible():
                                    text = (await opt.text_content() or "").strip()
                                    if text:
                                        alt_visible.append((opt, text))

                            if alt_visible:
                                alt_data = [(i, opt, text, text.lower()) for i, (opt, text) in enumerate(alt_visible)]
                                alt_idx = self.form_filler._find_best_option_match(alt_answer.lower(), alt_data)
                                if alt_idx is not None:
                                    target_opt = alt_data[alt_idx][1]
                                    matched_text = alt_data[alt_idx][2]
                                    try:
                                        await target_opt.click()
                                        await page.wait_for_timeout(400)
                                        found = True
                                        logger.info(f"Selected React-Select option via alternative '{alt_answer}': {matched_text[:50]}")
                                    except Exception:
                                        await page.keyboard.press("Enter")
                                        await page.wait_for_timeout(400)
                                        found = True
                                    break

                            await page.keyboard.press("Escape")
                            await page.wait_for_timeout(200)
                else:
                    # Clear typed text and try direct option click
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(200)
            except Exception as type_err:
                logger.debug(f"Type-to-search failed for {inp_id}: {type_err}")

            # Strategy 2: Direct option click fallback
            if not found:
                try:
                    await react_select_elem.click()
                    await self.browser_manager.human_delay(400, 600)
                    found = await self.form_filler._select_dropdown_option(page, answer)
                except Exception:
                    pass

            # Strategy 3: Acknowledgment/disclosure/policy fallback
            if not found and any(x in question_text.lower() for x in ["california", "ccpa", "disclosure", "additional information", "acknowledgment", "policy", "usage policy", "employment history"]):
                try:
                    await react_select_elem.click()
                    await self.browser_manager.human_delay(400, 600)
                    options = await page.query_selector_all('.select__option, [role="option"]')
                    for i, opt in enumerate(options):
                        if not await opt.is_visible():
                            continue
                        opt_text = (await opt.text_content() or "").strip()
                        opt_lower = opt_text.lower()
                        if any(x in opt_lower for x in ["acknowledge", "i have read", "understand", "agree", "accept", "consent"]):
                            await opt.click()
                            found = True
                            logger.info(f"Selected acknowledgment option: {opt_text[:50]}...")
                            break
                        # For employment history / policy with Yes/No options
                        if opt_lower in ("yes", "no"):
                            # For "employment history" → select "No"
                            if "employment history" in question_text.lower() or "previously" in question_text.lower():
                                if opt_lower == "no":
                                    await opt.click()
                                    found = True
                                    logger.info(f"Selected 'No' for employment/previous question")
                                    break
                            # For policy/disclosure → select "Yes" (acknowledge)
                            elif "policy" in question_text.lower() or "disclosure" in question_text.lower():
                                if opt_lower == "yes":
                                    await opt.click()
                                    found = True
                                    logger.info(f"Selected 'Yes' for policy/disclosure question")
                                    break
                except Exception:
                    pass

            if not found:
                await page.keyboard.press("Escape")
                return False

            await self.browser_manager.human_delay(300, 500)

            # Verify the hidden input was updated by React
            try:
                inp_value = await inp.input_value()
                inp_type = await inp.get_attribute("type") or ""
                if not inp_value or inp_value.strip() == "":
                    if inp_type == "text":
                        # type="text" inputs are React-Select search inputs.
                        # Setting their value via nativeInputValueSetter triggers React re-render
                        # which CLEARS the dropdown's visible selection. Do NOT set it.
                        logger.debug(f"Input {inp_id} is type=text (React-Select search), skipping force-sync to avoid clearing dropdown")
                    else:
                        # type="hidden" inputs are safe to set directly
                        await inp.evaluate('''(el, val) => {
                            const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                                window.HTMLInputElement.prototype, 'value'
                            ).set;
                            nativeInputValueSetter.call(el, val);
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        }''', answer)
                        logger.info(f"Force-synced hidden input {inp_id} with React setter: {answer[:30]}")
            except Exception as sync_err:
                logger.debug(f"Could not force-sync input {inp_id}: {sync_err}")

            return True

        except Exception as e:
            logger.debug(f"Error in _fill_associated_react_select for {inp_id}: {e}")
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            return False

    async def _handle_checkbox_questions(self, page) -> None:
        """Handle checkbox questions like terms/conditions agreements."""
        try:
            screening = self.form_filler.config.get("screening", {})

            # Find all checkboxes
            checkboxes = await page.query_selector_all('input[type="checkbox"]')

            for checkbox in checkboxes:
                try:
                    if not await checkbox.is_visible():
                        continue

                    # Check if already checked
                    if await checkbox.is_checked():
                        continue

                    checkbox_id = await checkbox.get_attribute("id") or ""
                    checkbox_name = await checkbox.get_attribute("name") or ""

                    # Get the label text
                    label_text = await self._get_question_label(page, checkbox, checkbox_id)
                    label_lower = label_text.lower()

                    should_check = False

                    # Agreement/consent checkboxes (always check these)
                    if any(x in label_lower for x in ["agree", "consent", "acknowledge", "terms", "conditions", "read", "accept", "privacy", "policy", "understand"]):
                        should_check = True
                        reason = "agreement"

                    # Authorization-related checkboxes
                    elif any(x in label_lower for x in ["authorize", "permission", "verify", "confirm", "certify"]):
                        should_check = True
                        reason = "authorization"

                    # Background check checkbox
                    elif "background" in label_lower and ("check" in label_lower or "screen" in label_lower or "investigation" in label_lower):
                        should_check = screening.get("agree_to_background_check", True)
                        reason = "background check"

                    # Drug test checkbox
                    elif "drug" in label_lower and ("test" in label_lower or "screen" in label_lower):
                        should_check = screening.get("agree_to_drug_test", True)
                        reason = "drug test"

                    # Arbitration agreement
                    elif "arbitration" in label_lower:
                        should_check = screening.get("agree_to_arbitration", True)
                        reason = "arbitration"

                    # EEOC/diversity consent
                    elif any(x in label_lower for x in ["eeoc", "diversity", "equal opportunity", "voluntary"]):
                        should_check = True
                        reason = "EEOC/diversity"

                    # Communication/contact preferences (opt-in to receive info)
                    elif any(x in label_lower for x in ["contact me", "receive information", "notify", "updates", "communication"]):
                        should_check = True
                        reason = "communication"

                    # Checkbox groups with "[]" in name — demographics or "how did you hear"
                    elif not should_check and "[]" in checkbox_name:
                        # Check if parent fieldset/group is "how did you hear"
                        parent_label = ""
                        try:
                            fieldset = await checkbox.evaluate_handle('el => el.closest("fieldset, .field, .checkbox-grouping")')
                            if fieldset:
                                legend = await fieldset.query_selector("legend, label, .checkbox__description")
                                if legend:
                                    parent_label = ((await legend.text_content()) or "").lower()
                        except Exception:
                            pass

                        if parent_label and any(x in parent_label for x in ["how did you hear", "where did you hear", "source"]):
                            how_heard = self.form_filler._flat_config.get("common_answers.how_did_you_hear", "LinkedIn").lower()
                            if any(x in label_lower for x in [how_heard, "linkedin", "online", "job board", "internet", "website"]):
                                should_check = True
                                reason = f"source ({label_text[:20]})"
                        demographics = self.form_filler.config.get("demographics", {})
                        gender = demographics.get("gender", "").lower()
                        ethnicity = demographics.get("ethnicity", "").lower()

                        # Gender checkbox options: "Man"/"Male", "Woman"/"Female", "Non-binary"
                        gender_options = {
                            "male": ["man", "male", "cis male", "cisgender male"],
                            "female": ["woman", "female", "cis female", "cisgender female"],
                        }
                        gender_matches = gender_options.get(gender, [gender])
                        if any(label_lower == m or label_lower.startswith(m) for m in gender_matches):
                            should_check = True
                            reason = f"gender ({label_text[:20]})"

                        # Ethnicity checkbox options — pick the FIRST match only
                        if not should_check:
                            ethnicity_matches = {
                                "asian": ["east asian"],  # Pick "East Asian" as default for "Asian"
                                "black": ["black"],
                                "hispanic": ["hispanic"],
                                "white": ["white"],
                                "native": ["indigenous"],
                                "pacific islander": ["native hawaiian"],
                                "middle eastern": ["middle eastern"],
                            }
                            matches = ethnicity_matches.get(ethnicity, [ethnicity])
                            if any(label_lower.startswith(m) or label_lower == m for m in matches):
                                should_check = True
                                reason = f"ethnicity ({label_text[:20]})"

                        # Sexual orientation: prefer not to say
                        if not should_check:
                            if any(x in label_lower for x in ["prefer not", "decline to", "not wish", "not to say"]):
                                # Only check "prefer not to say" for sexual orientation groups
                                # Don't check it for ethnicity/gender groups (we want specific answers there)
                                pass  # handled by ethnicity/gender above

                    if should_check:
                        await checkbox.check()
                        logger.info(f"Checked {reason} checkbox: {label_text[:50]}...")
                        await self.browser_manager.human_delay(100, 200)

                except Exception as e:
                    logger.debug(f"Error handling checkbox: {e}")

        except Exception as e:
            logger.debug(f"Error in _handle_checkbox_questions: {e}")

    async def _get_question_label(self, page, element, element_id: str) -> str:
        """Get the label text for a question element."""
        try:
            # Try label with for attribute
            if element_id:
                label = await page.query_selector(f'label[for="{element_id}"]')
                if label:
                    text = (await label.text_content() or "").strip()
                    if text:
                        return text

            # Try finding in parent container
            label_text = await element.evaluate('''(el) => {
                let container = el.closest('.field, .question, div[class*="field"]');
                if (!container) container = el.parentElement?.parentElement;
                if (!container) return "";

                let label = container.querySelector('label, .field-label, [class*="label"]');
                if (label) return label.textContent.trim();

                // Also check for text in preceding sibling
                let prev = el.previousElementSibling;
                if (prev) return prev.textContent.trim().slice(0, 200);

                return "";
            }''')

            return label_text or ""

        except Exception:
            return ""

    async def _get_textarea_label(self, page: Page, textarea, textarea_id: str) -> str:
        """Get the label text for a textarea element."""
        try:
            # Try finding label by 'for' attribute
            if textarea_id:
                label = await page.query_selector(f'label[for="{textarea_id}"]')
                if label:
                    return (await label.text_content() or "").strip()

            # Try finding label in parent container
            label_text = await textarea.evaluate('''(el) => {
                // Check parent for label
                let container = el.closest('.field, .question, div[class*="field"], div[class*="question"]');
                if (!container) container = el.parentElement?.parentElement;
                if (!container) return "";

                let label = container.querySelector('label, .field-label, [class*="label"]');
                return label ? label.textContent.trim() : "";
            }''')

            if label_text:
                return label_text

            # Try previous sibling
            prev_text = await textarea.evaluate('''(el) => {
                let prev = el.previousElementSibling;
                if (prev && (prev.tagName === 'LABEL' || prev.tagName === 'DIV')) {
                    return prev.textContent.trim();
                }
                return '';
            }''')

            return prev_text or ""

        except Exception:
            return ""

    async def _handle_custom_questions_in_frame(self, frame, job_data: Dict[str, Any]) -> None:
        """Handle custom questions in iframe."""
        # Similar logic but for frame context
        await self._handle_custom_questions(frame, job_data)

    async def _fill_demographics(self, page: Page) -> None:
        """Fill optional demographic questions."""
        demographics = self.form_filler.config.get("demographics", {})

        # Gender - try multiple selector patterns
        gender_selectors = [
            'select[name*="gender"]', 'select[id*="gender"]',
            '[data-qa="gender"] select', '#gender', 'select#gender',
            '[aria-label*="gender" i] select',
        ]
        for sel in gender_selectors:
            gender_select = await page.query_selector(sel)
            if gender_select and demographics.get("gender"):
                try:
                    await gender_select.select_option(label=demographics["gender"])
                    logger.info(f"Filled gender dropdown")
                    break
                except Exception:
                    pass

        # Also try React-select style gender dropdown
        await self._fill_react_select_by_label(page, "gender", demographics.get("gender", "Prefer not to say"))

        # Ethnicity
        ethnicity_selectors = [
            'select[name*="race"]', 'select[name*="ethnic"]', 'select[id*="ethnic"]',
            'select[id*="race"]', '[data-qa*="race"] select', '[data-qa*="ethnic"] select',
        ]
        for sel in ethnicity_selectors:
            ethnicity_select = await page.query_selector(sel)
            if ethnicity_select and demographics.get("ethnicity"):
                try:
                    await ethnicity_select.select_option(label=demographics["ethnicity"])
                    logger.info(f"Filled ethnicity dropdown")
                    break
                except Exception:
                    pass

        # Also try React-select style
        await self._fill_react_select_by_label(page, "race", demographics.get("ethnicity", "Prefer not to say"))
        await self._fill_react_select_by_label(page, "ethnic", demographics.get("ethnicity", "Prefer not to say"))

        # Veteran
        veteran_selectors = [
            'select[name*="veteran"]', 'select[id*="veteran"]',
            '[data-qa*="veteran"] select',
        ]
        for sel in veteran_selectors:
            veteran_select = await page.query_selector(sel)
            if veteran_select and demographics.get("veteran_status"):
                try:
                    await veteran_select.select_option(label=demographics["veteran_status"])
                    logger.info(f"Filled veteran status dropdown")
                    break
                except Exception:
                    pass

        # Also try React-select style
        await self._fill_react_select_by_label(page, "veteran", demographics.get("veteran_status", "No"))

        # Disability
        await self._fill_react_select_by_label(page, "disability", demographics.get("disability_status", "Prefer not to say"))

    async def _fill_react_select_by_label(self, page: Page, label_keyword: str, value: str) -> bool:
        """Fill a React-select dropdown by finding it via label keyword."""
        if not value:
            return False

        try:
            # Find all select controls and check their labels
            dropdowns = await page.query_selector_all('.select__control, [role="combobox"]')

            for dropdown in dropdowns:
                if not await dropdown.is_visible():
                    continue

                # Get the label for this dropdown
                label_text = await dropdown.evaluate('''(el) => {
                    let container = el.closest('.field, .select, [class*="field"], [class*="question"]');
                    if (!container) container = el.parentElement?.parentElement?.parentElement;
                    if (!container) return "";
                    let label = container.querySelector("label, .select__label, [class*='label']");
                    return label ? label.textContent.toLowerCase().trim() : "";
                }''')

                if label_keyword.lower() in label_text:
                    # Check if already has a value (not placeholder)
                    current_text = (await dropdown.text_content() or "").strip().lower()
                    if current_text and "select" not in current_text and "choose" not in current_text:
                        logger.debug(f"React-select for '{label_keyword}' already filled: {current_text}")
                        return True

                    logger.info(f"Filling React-select dropdown for '{label_keyword}' with '{value}'")
                    await dropdown.click()
                    await self.browser_manager.human_delay(500, 800)

                    # Find and click matching option
                    options = await page.query_selector_all('.select__option, [class*="option"], [role="option"]')
                    for opt in options:
                        if not await opt.is_visible():
                            continue
                        opt_text = (await opt.text_content() or "").strip().lower()
                        value_lower = value.lower()

                        # Match the value
                        if value_lower in opt_text or opt_text in value_lower:
                            await opt.click()
                            logger.info(f"Selected '{opt_text}' for '{label_keyword}'")
                            return True

                        # Handle "prefer not to say" variations
                        if "prefer" in value_lower:
                            if any(x in opt_text for x in ["prefer not", "decline", "do not want", "don't want"]):
                                await opt.click()
                                logger.info(f"Selected '{opt_text}' for '{label_keyword}'")
                                return True

                    # Close dropdown if no match
                    await page.keyboard.press("Escape")
                    return False

        except Exception as e:
            logger.debug(f"Error filling React-select for '{label_keyword}': {e}")
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass

        return False

    async def _fill_greenhouse_education_fields(self, page: Page) -> None:
        """Fill Greenhouse education fields (school, degree, discipline).

        Greenhouse uses React-Select for these. The key is:
        1. Clicking an option in the dropdown menu properly triggers React's onChange
        2. React-Select then updates its own hidden input automatically
        3. We MUST click the option (not just set el.value) for React state to update
        4. After clicking, we verify the hidden input was updated; if not, we re-click
        """
        education = self.form_filler.config.get("education", [])
        if isinstance(education, list) and education:
            edu = education[0]
        else:
            edu = education if isinstance(education, dict) else {}

        if not edu:
            logger.debug("No education data configured, skipping education fields")
            return

        logger.info("Filling Greenhouse education fields")

        # Discipline alternatives: if "Software Engineering" isn't in the options,
        # try these alternatives in order
        discipline_value = edu.get("field_of_study", "Software Engineering")
        discipline_alternatives = [
            discipline_value,
            "Engineering",
            "Computer Science",
            "Computer Engineering",
            "Information Technology",
        ]

        education_fields = [
            ("school", edu.get("school", ""), ["school", "university", "college", "institution"], None),
            ("degree", edu.get("degree", "Bachelor's degree"), ["degree"], None),
            ("discipline", discipline_value, ["discipline", "major", "field"], discipline_alternatives),
        ]

        for field_name, value, keywords, alternatives in education_fields:
            if not value:
                continue

            try:
                # Check if the actual hidden input already has a real value
                # (not the React-Select search input — that's type="text" and always empty)
                hidden_has_value = await page.evaluate(f'''() => {{
                    // Check type="hidden" inputs first (the real value store)
                    const selectors = [
                        'input[type="hidden"][id="{field_name}--0"]',
                        'input[type="hidden"][id$="--0"][id*="{field_name}"]',
                        'input[type="hidden"][name*="{field_name}"]',
                    ];
                    for (const sel of selectors) {{
                        const el = document.querySelector(sel);
                        if (el && el.value && el.value.trim()) return el.value.trim();
                    }}
                    return "";
                }}''')

                if hidden_has_value:
                    logger.debug(f"Education hidden input {field_name} already has value: {hidden_has_value}")
                    continue

                # Find the React-Select dropdown for this field
                dropdowns = await page.query_selector_all('.select__control, [role="combobox"]')

                for dropdown in dropdowns:
                    if not await dropdown.is_visible():
                        continue

                    label_text = await dropdown.evaluate('''(el) => {
                        let container = el.closest('.field, .select, [class*="field"]');
                        if (!container) container = el.parentElement?.parentElement?.parentElement;
                        if (!container) return "";
                        let label = container.querySelector("label, .select__label, [class*='label']");
                        return label ? label.textContent.toLowerCase().trim() : "";
                    }''')

                    if not any(kw in label_text for kw in keywords):
                        continue

                    # Check if dropdown visually shows a value (not placeholder)
                    display_value = await dropdown.evaluate('''(el) => {
                        const sv = el.querySelector('.select__single-value, [class*="singleValue"]');
                        if (sv && sv.textContent && sv.textContent.trim() !== "Select..." && sv.textContent.trim().length > 1) {
                            return sv.textContent.trim();
                        }
                        return "";
                    }''')

                    if display_value:
                        # Dropdown visually shows a value — React state should be set already
                        # Just verify the hidden input got it
                        logger.debug(f"Education dropdown '{field_name}' visually shows: {display_value}")
                        # Force-sync to hidden input in case React didn't update it
                        await self._sync_education_hidden_input_robust(page, field_name, display_value)
                        break

                    # Dropdown is empty — fill it by clicking to open and selecting
                    logger.info(f"Filling education dropdown '{field_name}' with '{value}'")

                    # For school, type to search (typeahead)
                    if field_name == "school":
                        await dropdown.click()
                        await self.browser_manager.human_delay(500, 800)
                        search_term = value.split(",")[0][:20]
                        await page.keyboard.type(search_term, delay=50)
                        await self.browser_manager.human_delay(800, 1200)

                        found_match = await self.form_filler._select_dropdown_option(page, value)
                        if not found_match:
                            # Select first search result
                            await page.keyboard.press("Enter")
                            logger.info(f"Selected first search result for {field_name}")
                            found_match = True

                    else:
                        # For degree and discipline, try direct selection
                        values_to_try = alternatives if alternatives else [value]
                        found_match = False

                        for try_value in values_to_try:
                            await dropdown.click()
                            await self.browser_manager.human_delay(400, 600)

                            found_match = await self.form_filler._select_dropdown_option(page, try_value)
                            if found_match:
                                logger.info(f"Selected {field_name} option: {try_value}")
                                value = try_value  # Update for sync
                                break
                            else:
                                # Close dropdown before trying next alternative
                                await page.keyboard.press("Escape")
                                await self.browser_manager.human_delay(200, 300)

                        if not found_match:
                            # Last resort: type and Enter for first match
                            await dropdown.click()
                            await self.browser_manager.human_delay(300, 500)
                            search_term = value[:15]
                            await page.keyboard.type(search_term, delay=50)
                            await self.browser_manager.human_delay(500, 800)
                            await page.keyboard.press("Enter")
                            found_match = True
                            logger.info(f"Selected {field_name} via typed search: {value}")

                    # Wait for React-Select to process the selection
                    await self.browser_manager.human_delay(500, 800)

                    # Verify and sync hidden input
                    if found_match:
                        await self._sync_education_hidden_input_robust(page, field_name, value)

                    break

            except Exception as e:
                logger.debug(f"Error filling education field {field_name}: {e}")
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass

        # Fill graduation date fields (start/end month/year)
        await self._fill_graduation_date_fields(page, edu)

        # Final sync pass: ensure all education hidden inputs are populated
        for field_name, value, keywords, alternatives in education_fields:
            if value:
                await self._sync_education_hidden_input_robust(page, field_name, value)

    async def _sync_education_hidden_input_robust(self, page: Page, field_name: str, value: str) -> None:
        """Robustly sync education hidden input with the selected dropdown value.

        Searches for BOTH type="hidden" AND any input with matching education IDs.
        Uses React-compatible event dispatching via nativeInputValueSetter.
        """
        try:
            safe_value = value.replace("\\", "\\\\").replace('"', '\\"').replace('\n', ' ')

            # Use a single JS call that:
            # 1. Finds the hidden input (type="hidden" only — NOT the search text input)
            # 2. Reads the display value from React-Select single-value if available
            # 3. Sets the value using React-compatible nativeInputValueSetter
            result = await page.evaluate(f'''() => {{
                const fieldName = "{field_name}";
                const fallbackValue = "{safe_value}";

                // Find the value input — ONLY type="hidden" inputs are safe to set directly.
                // type="text" inputs in React-Select are the search field; setting them
                // triggers React re-render which CLEARS the dropdown display.
                const hiddenSelectors = [
                    'input[type="hidden"][id="' + fieldName + '--0"]',
                    'input[type="hidden"][id$="--0"][id*="' + fieldName + '"]',
                    'input[type="hidden"][name*="' + fieldName + '"]',
                ];

                let hidden = null;
                let isHiddenType = false;
                for (const sel of hiddenSelectors) {{
                    hidden = document.querySelector(sel);
                    if (hidden) {{ isHiddenType = true; break; }}
                }}

                // If no hidden input found, check for type="text" but DON'T set its value
                if (!hidden) {{
                    const textInput = document.querySelector('input[id="' + fieldName + '--0"]');
                    if (textInput) {{
                        // This is a React-Select search input — check if dropdown visually shows a value
                        let container = textInput.closest('.field, .select, [class*="field"]');
                        if (!container) container = textInput.parentElement?.parentElement?.parentElement;
                        if (container) {{
                            const sv = container.querySelector('.select__single-value, [class*="singleValue"]');
                            if (sv && sv.textContent && sv.textContent.trim() !== "Select..." && sv.textContent.trim().length > 1) {{
                                return {{ status: "react_select_has_display", value: sv.textContent.trim(), inputType: "text" }};
                            }}
                        }}
                        // Dropdown shows nothing — we can't fix this by setting text input
                        // (it would trigger React re-render and make things worse)
                        return {{ status: "text_input_no_display", fieldName, inputType: "text" }};
                    }}
                    return {{ status: "no_hidden_input", fieldName }};
                }}

                // For type="hidden" inputs, safe to set directly
                if (hidden.value && hidden.value.trim()) {{
                    return {{ status: "already_set", value: hidden.value.trim() }};
                }}

                // Try to read the display value from the React-Select single-value element
                let container = hidden.closest('.field, .select, [class*="field"]');
                if (!container) container = hidden.parentElement?.parentElement?.parentElement;
                let displayValue = "";
                if (container) {{
                    const sv = container.querySelector('.select__single-value, [class*="singleValue"]');
                    if (sv && sv.textContent && sv.textContent.trim() !== "Select...") {{
                        displayValue = sv.textContent.trim();
                    }}
                }}

                const syncValue = displayValue || fallbackValue;
                if (!syncValue) return {{ status: "no_value" }};

                // Set value using React-compatible approach (ONLY for type="hidden"):
                const nativeSetter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                ).set;
                nativeSetter.call(hidden, syncValue);

                // Dispatch React-compatible events
                hidden.dispatchEvent(new Event('input', {{ bubbles: true }}));
                hidden.dispatchEvent(new Event('change', {{ bubbles: true }}));

                // Also try React's synthetic event system
                const reactKey = Object.keys(hidden).find(k => k.startsWith('__reactFiber$') || k.startsWith('__reactInternalInstance$'));
                if (reactKey) {{
                    const fiber = hidden[reactKey];
                    let current = fiber;
                    while (current) {{
                        if (current.memoizedProps && current.memoizedProps.onChange) {{
                            try {{
                                current.memoizedProps.onChange({{ target: {{ value: syncValue }} }});
                            }} catch(e) {{}}
                            break;
                        }}
                        current = current.return;
                    }}
                }}

                return {{ status: "synced", value: syncValue, source: displayValue ? "display" : "fallback" }};
            }}''')

            if result:
                status = result.get("status", "unknown")
                if status == "synced":
                    logger.info(f"Synced education hidden input {field_name} = {result.get('value')} (source: {result.get('source')})")
                elif status == "already_set":
                    logger.debug(f"Hidden {field_name} already has value: {result.get('value')}")
                elif status == "react_select_has_display":
                    logger.info(f"Education {field_name} React-Select shows: {result.get('value')} (type=text input, NOT setting value to avoid React re-render)")
                elif status == "text_input_no_display":
                    logger.warning(f"Education {field_name} has type=text input but React-Select shows no value — dropdown may need re-selection")
                elif status == "no_hidden_input":
                    logger.debug(f"No hidden input found for {field_name} — React-Select may manage state internally")
                else:
                    logger.warning(f"Sync education {field_name}: {result}")

        except Exception as e:
            logger.debug(f"Error syncing education hidden input {field_name}: {e}")

    async def _fill_greenhouse_country(self, page: Page) -> None:
        """Fill the Greenhouse country field (React-Select dropdown or hidden input)."""
        country = self.form_filler.config.get("personal_info", {}).get("country", "United States")
        if not country:
            return

        try:
            # 1. Check if hidden input 'country' already has a value
            hidden = await page.query_selector('input[type="hidden"][id="country"], input[type="hidden"][name="country"]')
            if hidden:
                current = await hidden.get_attribute("value") or ""
                if current and current.strip():
                    logger.debug(f"Country hidden input already filled: {current}")
                    return

            # 2. Find the React-Select dropdown near a "Country" label
            dropdowns = await page.query_selector_all('.select__control, [role="combobox"]')
            for dropdown in dropdowns:
                if not await dropdown.is_visible():
                    continue

                label_text = await dropdown.evaluate('''(el) => {
                    let container = el.closest('.field, .select, [class*="field"]');
                    if (!container) container = el.parentElement?.parentElement?.parentElement;
                    if (!container) return "";
                    let label = container.querySelector("label, .select__label, [class*='label']");
                    return label ? label.textContent.toLowerCase().trim() : "";
                }''')

                if "country" not in label_text:
                    continue

                # Check if already filled
                current_text = (await dropdown.text_content() or "").strip()
                if current_text and "select" not in current_text.lower() and len(current_text) > 2:
                    logger.debug(f"Country dropdown already filled: {current_text}")
                    return

                logger.info(f"Filling country dropdown with '{country}'")
                await dropdown.click()
                await self.browser_manager.human_delay(400, 600)

                # Type to search
                await page.keyboard.type(country[:15], delay=50)
                await self.browser_manager.human_delay(600, 900)

                found = await self.form_filler._select_dropdown_option(page, country)
                if not found:
                    await page.keyboard.press("Enter")
                    found = True
                    logger.info("Selected first country search result")

                await self.browser_manager.human_delay(300, 500)

                # Sync hidden input
                if hidden:
                    await hidden.evaluate(f'''(el) => {{
                        el.value = "{country}";
                        el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    }}''')
                    logger.info(f"Synced country hidden input: {country}")

                return

            # 3. Fallback: force-set the hidden input directly
            if hidden:
                await hidden.evaluate(f'''(el) => {{
                    el.value = "{country}";
                    el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                }}''')
                logger.info(f"Force-set country hidden input: {country}")

        except Exception as e:
            logger.debug(f"Error filling country: {e}")

    async def _force_fill_education_hidden_inputs(self, page: Page) -> None:
        """Force-fill education hidden inputs as last resort if React-Select failed.

        ONLY targets type="hidden" inputs — NOT the type="text" search inputs,
        which are just React-Select's search box and not the value store.
        Uses React-compatible nativeInputValueSetter for proper state sync.
        """
        education = self.form_filler.config.get("education", [])
        if isinstance(education, list) and education:
            edu = education[0]
        else:
            edu = education if isinstance(education, dict) else {}
        if not edu:
            return

        force_map = {
            "school": edu.get("school", "San Jose State University"),
            "degree": edu.get("degree", "Bachelor of Science"),
            "discipline": edu.get("field_of_study", "Software Engineering"),
        }

        for field_name, value in force_map.items():
            if not value:
                continue
            try:
                # ONLY target type="hidden" inputs. Do NOT touch type="text" inputs —
                # those are React-Select's search inputs. Setting their value triggers
                # React re-render which CLEARS the dropdown's visual selection.
                selectors = [
                    f'input[type="hidden"][id="{field_name}--0"]',
                    f'input[type="hidden"][id$="--0"][id*="{field_name}"]',
                    f'input[type="hidden"][name*="{field_name}"]',
                ]
                inp = None
                for sel in selectors:
                    inp = await page.query_selector(sel)
                    if inp:
                        break

                if not inp:
                    # No hidden input — check if React-Select visually shows a value
                    # (type="text" forms: the value lives in React state, not in the input)
                    has_display = await page.evaluate(f'''() => {{
                        const textInput = document.querySelector('input[id="{field_name}--0"]');
                        if (!textInput) return false;
                        let container = textInput.closest('.field, .select, [class*="field"]');
                        if (!container) container = textInput.parentElement?.parentElement?.parentElement;
                        if (!container) return false;
                        const sv = container.querySelector('.select__single-value, [class*="singleValue"]');
                        return !!(sv && sv.textContent && sv.textContent.trim() !== "Select..." && sv.textContent.trim().length > 1);
                    }}''')
                    if has_display:
                        logger.debug(f"Education {field_name} React-Select has display value (type=text form, skipping force-fill)")
                    else:
                        logger.warning(f"No hidden input for {field_name} and React-Select shows no value")
                    continue

                current = await inp.get_attribute("value") or ""
                if current and current.strip():
                    logger.debug(f"Education hidden {field_name} already has value: {current.strip()}")
                    continue

                # Read display value from React-Select if available
                sel_str = selectors[0]  # Use first selector for JS query
                display_val = await page.evaluate(f'''() => {{
                    const el = document.querySelector('{sel_str}');
                    if (!el) return "";
                    let container = el.closest('.field, .select, [class*="field"]');
                    if (!container) container = el.parentElement?.parentElement?.parentElement;
                    if (!container) return "";
                    const sv = container.querySelector('.select__single-value, [class*="singleValue"]');
                    return (sv && sv.textContent && sv.textContent.trim() !== "Select...") ? sv.textContent.trim() : "";
                }}''')
                sync_value = display_val or value
                safe_value = sync_value.replace("\\", "\\\\").replace('"', '\\"').replace('\n', ' ')
                await inp.evaluate(f'''(el) => {{
                    const nativeSetter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value'
                    ).set;
                    nativeSetter.call(el, "{safe_value}");
                    el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                }}''')
                logger.info(f"Force-filled education hidden input {field_name} = {sync_value}")
            except Exception as e:
                logger.debug(f"Error force-filling {field_name}: {e}")

        # Also force-fill date hidden inputs
        grad_date = edu.get("graduation_date", "May 2026")
        start_date = edu.get("start_date", "August 2021")
        import re
        month_match = re.search(r'(January|February|March|April|May|June|July|August|September|October|November|December)', grad_date, re.IGNORECASE)
        year_match = re.search(r'20\d{2}', grad_date)
        end_month = month_match.group(1).capitalize() if month_match else "May"
        end_year = year_match.group() if year_match else "2026"

        start_month_match = re.search(r'(January|February|March|April|May|June|July|August|September|October|November|December)', start_date, re.IGNORECASE)
        start_year_match = re.search(r'20\d{2}', start_date)
        start_month = start_month_match.group(1).capitalize() if start_month_match else "August"
        start_year = start_year_match.group() if start_year_match else "2021"

        date_map = {
            "start-month": start_month,
            "start-year": start_year,
            "end-month": end_month,
            "end-year": end_year,
        }
        for field_prefix, value in date_map.items():
            try:
                # Try hidden first, then any input with matching ID
                inp = await page.query_selector(f'input[type="hidden"][id*="{field_prefix}"]')
                if not inp:
                    inp = await page.query_selector(f'input[id*="{field_prefix}--"]')
                if not inp:
                    inp = await page.query_selector(f'select[id*="{field_prefix}"]')
                if not inp:
                    continue
                tag = await inp.evaluate("el => el.tagName.toLowerCase()")
                if tag == "select":
                    try:
                        await inp.select_option(label=value)
                        logger.info(f"Force-selected education date {field_prefix} = {value}")
                    except Exception:
                        try:
                            await inp.select_option(value)
                        except Exception:
                            pass
                    continue
                current = await inp.get_attribute("value") or ""
                if current and current.strip():
                    continue
                await inp.evaluate(f'''(el) => {{
                    const nativeSetter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value'
                    ).set;
                    nativeSetter.call(el, "{value}");
                    el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                }}''')
                logger.info(f"Force-filled education date input {field_prefix} = {value}")
            except Exception as e:
                logger.debug(f"Error force-filling date {field_prefix}: {e}")

    async def _sync_hidden_input(self, page: Page, hidden_input, value: str) -> None:
        """Set value on a hidden input if empty, using React-compatible event dispatch."""
        try:
            current = await hidden_input.get_attribute("value") or ""
            if not current:
                safe_value = value.replace("\\", "\\\\").replace('"', '\\"').replace('\n', ' ')
                await hidden_input.evaluate(f'''(el) => {{
                    const nativeSetter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value'
                    ).set;
                    nativeSetter.call(el, "{safe_value}");
                    el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                }}''')
                logger.debug(f"Synced hidden input with: {value}")
        except Exception as e:
            logger.debug(f"Error syncing hidden input: {e}")

    async def _sync_all_hidden_question_inputs(self, page: Page) -> None:
        """
        Sync all hidden inputs that might still be empty.
        This is a final pass to ensure React-Select values are properly synced.
        Covers question_ IDs, gender, candidate-location, and other form fields.
        """
        try:
            # Find all hidden inputs — question_ IDs plus other common Greenhouse field IDs
            hidden_inputs = await page.query_selector_all(
                'input[type="hidden"][id^="question_"], '
                'input[type="hidden"][id="gender"], '
                'input[type="hidden"][id="candidate-location"], '
                'input[type="hidden"][id="hispanic_ethnicity"], '
                'input[type="hidden"][id="veteran_status"], '
                'input[type="hidden"][id="disability_status"]'
            )
            logger.info(f"Syncing {len(hidden_inputs)} hidden inputs (questions + demographics)")

            for hidden in hidden_inputs:
                try:
                    hidden_id = await hidden.get_attribute("id") or ""
                    current_value = await hidden.get_attribute("value") or ""

                    # Skip if already has a value
                    if current_value and current_value.strip():
                        continue

                    # Try to find the corresponding visible element and get its value
                    # Look for select, text input, or React-Select dropdown with same ID
                    value_found = None

                    # 1. Check for visible select with same ID
                    visible_select = await page.query_selector(f'select#{hidden_id}')
                    if visible_select and await visible_select.is_visible():
                        value_found = await visible_select.input_value()

                    # 2. Check for visible text input with same ID
                    if not value_found:
                        visible_input = await page.query_selector(f'input[type="text"]#{hidden_id}')
                        if visible_input and await visible_input.is_visible():
                            value_found = await visible_input.input_value()

                    # 3. Look for React-Select dropdown in the same field container
                    if not value_found:
                        dropdown_value = await page.evaluate(f'''() => {{
                            // Find the hidden input
                            const hidden = document.getElementById("{hidden_id}");
                            if (!hidden) return null;

                            // Go up to the field container
                            let container = hidden.closest('.field, .select, [class*="field"], [class*="question"]');
                            if (!container) container = hidden.parentElement?.parentElement?.parentElement;
                            if (!container) return null;

                            // Look for React-Select single-value (selected option text)
                            const singleValue = container.querySelector('.select__single-value, [class*="singleValue"]');
                            if (singleValue && singleValue.textContent && singleValue.textContent.trim() !== "Select...") {{
                                return singleValue.textContent.trim();
                            }}

                            // Look for multi-select values (React-Select multi-value)
                            const multiValues = container.querySelectorAll('.select__multi-value__label, [class*="multiValue"] [class*="label"]');
                            if (multiValues.length > 0) {{
                                const vals = Array.from(multiValues).map(v => v.textContent.trim()).filter(v => v);
                                if (vals.length > 0) return vals.join(", ");
                            }}

                            // Look for visible select element's selected option
                            const select = container.querySelector('select');
                            if (select && select.options[select.selectedIndex]) {{
                                const text = select.options[select.selectedIndex].text;
                                if (text && text !== "Select...") return text;
                            }}

                            // Look for filled text input
                            const textInput = container.querySelector('input[type="text"]:not([type="hidden"])');
                            if (textInput && textInput.value) return textInput.value;

                            return null;
                        }}''')
                        if dropdown_value:
                            value_found = dropdown_value

                    # 4. Try to get value from label and config fallback
                    if not value_found:
                        label_text = await page.evaluate(f'''() => {{
                            const hidden = document.getElementById("{hidden_id}");
                            if (!hidden) return "";

                            let container = hidden.closest('.field, .select, [class*="field"], [class*="question"]');
                            if (!container) container = hidden.parentElement?.parentElement?.parentElement;
                            if (!container) return "";

                            const label = container.querySelector('label, .field-label, [class*="label"]');
                            return label ? label.textContent.trim() : "";
                        }}''')

                        if label_text:
                            # Use form filler to get the value
                            config_value = self.form_filler._get_dropdown_value_for_label(label_text)
                            if config_value:
                                # Try to actually fill the React-Select dropdown with this value
                                # (the dropdown may not have been filled earlier)
                                react_select = await page.evaluate(f'''() => {{
                                    const hidden = document.getElementById("{hidden_id}");
                                    if (!hidden) return false;
                                    let container = hidden.closest('.field, .select, [class*="field"], [class*="question"]');
                                    if (!container) container = hidden.parentElement?.parentElement?.parentElement;
                                    if (!container) return false;
                                    return !!container.querySelector('.select__control, [role="combobox"]');
                                }}''')

                                if react_select:
                                    # There's a React-Select - fill it via the dropdown
                                    rs_elem = await page.evaluate_handle(f'''() => {{
                                        const hidden = document.getElementById("{hidden_id}");
                                        if (!hidden) return null;
                                        let container = hidden.closest('.field, .select, [class*="field"], [class*="question"]');
                                        if (!container) container = hidden.parentElement?.parentElement?.parentElement;
                                        if (!container) return null;
                                        return container.querySelector('.select__control, [role="combobox"]');
                                    }}''')

                                    if rs_elem:
                                        elem = rs_elem.as_element()
                                        if elem and await elem.is_visible():
                                            logger.info(f"Late-filling React-Select for {hidden_id}: '{label_text[:40]}' -> '{config_value}'")
                                            await elem.click()
                                            await self.browser_manager.human_delay(400, 600)
                                            filled = await self.form_filler._select_dropdown_option(page, config_value)
                                            if filled:
                                                value_found = config_value
                                                # Wait for React to update hidden input
                                                await self.browser_manager.human_delay(300, 500)
                                                # Re-check if hidden input now has value
                                                new_val = await hidden.get_attribute("value") or ""
                                                if new_val:
                                                    logger.info(f"React-Select updated hidden input {hidden_id}: {new_val}")
                                                    continue  # Skip manual sync, React handled it
                                            else:
                                                await page.keyboard.press("Escape")
                                else:
                                    value_found = config_value

                    # Sync the value if found
                    if value_found and value_found.strip():
                        # Escape quotes in value for JavaScript
                        safe_value = value_found.replace("\\", "\\\\").replace('"', '\\"').replace('\n', ' ')
                        await page.evaluate(f'''() => {{
                            const el = document.getElementById("{hidden_id}");
                            if (el) {{
                                const nativeSetter = Object.getOwnPropertyDescriptor(
                                    window.HTMLInputElement.prototype, 'value'
                                ).set;
                                nativeSetter.call(el, "{safe_value}");
                                el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                                el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                            }}
                        }}''')
                        logger.info(f"Synced hidden input {hidden_id} with value: {value_found}")
                    else:
                        logger.warning(f"Could not find value for hidden input: {hidden_id}")

                except Exception as e:
                    logger.debug(f"Error syncing hidden input: {e}")

        except Exception as e:
            logger.debug(f"Error in _sync_all_hidden_question_inputs: {e}")

    async def _fill_graduation_date_fields(self, page: Page, edu: Dict) -> None:
        """Fill graduation date dropdowns (start/end month/year)."""
        grad_date = edu.get("graduation_date", "May 2026")
        start_date = edu.get("start_date", "August 2022")  # Default start date

        # Parse graduation date
        import re
        month_match = re.search(r'(January|February|March|April|May|June|July|August|September|October|November|December)', grad_date, re.IGNORECASE)
        year_match = re.search(r'20\d{2}', grad_date)

        end_month = month_match.group(1).capitalize() if month_match else "May"
        end_year = year_match.group() if year_match else "2026"

        # Parse start date
        start_month_match = re.search(r'(January|February|March|April|May|June|July|August|September|October|November|December)', start_date, re.IGNORECASE)
        start_year_match = re.search(r'20\d{2}', start_date)

        start_month = start_month_match.group(1).capitalize() if start_month_match else "August"
        start_year = start_year_match.group() if start_year_match else "2022"

        logger.info(f"Filling education dates: Start {start_month} {start_year}, End {end_month} {end_year}")

        # Find and fill month/year dropdowns (both start and end dates)
        # Use tuples of (required_words, value, field_name) - ALL words in tuple must be present
        date_field_config = [
            # End date fields (graduation) - need both "end" and "month" (or similar)
            (["end", "month"], end_month, "end month"),
            (["end", "year"], end_year, "end year"),
            (["graduation", "month"], end_month, "graduation month"),
            (["graduation", "year"], end_year, "graduation year"),
            (["expected", "month"], end_month, "expected month"),
            (["expected", "year"], end_year, "expected year"),
            # Start date fields - need both "start" and "year" (or "month")
            (["start", "month"], start_month, "start month"),
            (["start", "year"], start_year, "start year"),
        ]

        date_dropdowns = await page.query_selector_all('.select__control, [role="combobox"]')
        logger.debug(f"Found {len(date_dropdowns)} React-Select dropdowns for date filling")
        for dropdown in date_dropdowns:
            try:
                if not await dropdown.is_visible():
                    continue

                label_text = await dropdown.evaluate('''(el) => {
                    let container = el.closest('.field, .select, [class*="field"]');
                    if (!container) container = el.parentElement?.parentElement?.parentElement;
                    if (!container) return "";
                    let label = container.querySelector("label, .select__label, [class*='label']");
                    return label ? label.textContent.toLowerCase().trim() : "";
                }''')
                logger.debug(f"Date dropdown label: '{label_text}'")

                for required_words, value, field_name in date_field_config:
                    # ALL required words must be present in the label
                    if not all(word in label_text for word in required_words):
                        continue

                    current_text = (await dropdown.text_content() or "").strip()
                    if current_text and "select" not in current_text.lower() and current_text != "--":
                        logger.debug(f"{field_name} already filled: {current_text}")
                        break

                    logger.info(f"Filling {field_name} with {value}")
                    await dropdown.click()
                    await self.browser_manager.human_delay(400, 600)

                    # Use keyboard-based selection for proper React state update
                    found = await self.form_filler._select_dropdown_option(page, value)

                    if not found:
                        # Fallback: type the value and press Enter
                        await page.keyboard.type(value[:10], delay=50)
                        await page.wait_for_timeout(300)
                        await page.keyboard.press("Enter")
                        found = True
                        logger.info(f"Selected {field_name} via typed search: {value}")

                    if not found:
                        await page.keyboard.press("Escape")
                    else:
                        logger.info(f"Selected {field_name}: {value}")

                    await self.browser_manager.human_delay(300, 500)
                    break

            except Exception as e:
                logger.debug(f"Error filling date dropdown: {e}")
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass

        # Also try standard HTML selects for year/month
        # Use broader selector to catch IDs like start-year--0, end-month--0 etc
        html_selects = await page.query_selector_all('select[id*="year"], select[name*="year"], select[id*="month"], select[name*="month"], select[id^="start-"], select[id^="end-"]')
        logger.debug(f"Found {len(html_selects)} HTML selects for date filling")
        for sel in html_selects:
            try:
                if not await sel.is_visible():
                    continue

                sel_id = (await sel.get_attribute("id") or "").lower()
                sel_name = (await sel.get_attribute("name") or "").lower()
                # Also get label text for this select
                label_text = await self._get_label_for_select(page, sel)
                sel_context = f"{sel_id} {sel_name} {label_text}".lower()
                logger.debug(f"HTML select id={sel_id}, name={sel_name}, label={label_text}")

                # Determine which value to use (check label text first for context)
                value = None
                # Handle IDs like "start-year--0", "end-month--0"
                if "start" in sel_context and ("year" in sel_context or "year" in sel_id):
                    value = start_year
                elif "start" in sel_context and ("month" in sel_context or "month" in sel_id):
                    value = start_month
                elif ("end" in sel_context or "graduation" in sel_context) and ("year" in sel_context or "year" in sel_id):
                    value = end_year
                elif ("end" in sel_context or "graduation" in sel_context) and ("month" in sel_context or "month" in sel_id):
                    value = end_month
                elif "year" in sel_id or "year" in sel_context:
                    # Default to end year for generic year fields in education context
                    value = end_year

                if value:
                    current = await sel.input_value()
                    if not current or current == "":
                        try:
                            await sel.select_option(value)
                            logger.info(f"Filled HTML select {sel_id or sel_name} with {value}")
                        except Exception:
                            # Try by label if value doesn't match
                            try:
                                await sel.select_option(label=value)
                                logger.info(f"Filled HTML select {sel_id or sel_name} with label {value}")
                            except Exception as e2:
                                logger.debug(f"Could not fill {sel_id}: {e2}")

            except Exception as e:
                logger.debug(f"Error filling HTML date select: {e}")

        # Also handle number inputs for year fields (e.g., end-year--0 with type="number")
        year_inputs = await page.query_selector_all('input[type="number"][id*="year"], input[type="number"][id^="end-"], input[type="number"][id^="start-"]')
        logger.debug(f"Found {len(year_inputs)} number inputs for year filling")
        for inp in year_inputs:
            try:
                if not await inp.is_visible():
                    continue
                inp_id = (await inp.get_attribute("id") or "").lower()
                current = await inp.input_value()
                if current and current.strip():
                    continue  # Already has value
                value = None
                if "start" in inp_id and "year" in inp_id:
                    value = start_year
                elif "end" in inp_id and "year" in inp_id:
                    value = end_year
                elif "year" in inp_id:
                    value = end_year
                if value:
                    await inp.fill(value)
                    # Trigger events for React
                    await inp.evaluate('''(el) => {
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        el.dispatchEvent(new Event('blur', { bubbles: true }));
                    }''')
                    logger.info(f"Filled year number input {inp_id} with {value}")
            except Exception as e:
                logger.debug(f"Error filling year number input: {e}")

        # CRITICAL: Sync hidden inputs for education date fields
        # Greenhouse may have hidden inputs (end-month--0, end-year--0, start-month--0, start-year--0)
        # that need to match the visible dropdown/input values
        await self._sync_education_date_hidden_inputs(page, start_month, start_year, end_month, end_year)

    async def _sync_education_date_hidden_inputs(self, page, start_month, start_year, end_month, end_year):
        """Sync hidden inputs for education date fields."""
        date_mappings = {
            "end-month": end_month,
            "end-year": end_year,
            "start-month": start_month,
            "start-year": start_year,
        }

        for field_prefix, value in date_mappings.items():
            try:
                # ONLY target type="hidden" inputs — NOT type="text" inputs.
                # Setting type="text" inputs triggers React re-render which clears dropdowns.
                hidden = await page.query_selector(f'input[type="hidden"][id*="{field_prefix}"]')
                if not hidden:
                    # Check if a type="text" input exists — if so, React-Select manages it
                    text_input = await page.query_selector(f'input[id*="{field_prefix}--0"]')
                    if text_input:
                        logger.debug(f"Date field {field_prefix} uses type=text input (React-Select managed, skipping)")
                    continue

                current = await hidden.get_attribute("value") or ""
                if current and current.strip():
                    continue  # Already has a value

                inp_id = await hidden.get_attribute("id") or ""

                # Also check the visible React-Select for the current text
                visible_value = await page.evaluate(f'''() => {{
                    // Check for visible number input or text input
                    const numInput = document.querySelector('input[type="number"][id*="{field_prefix}"]');
                    if (numInput && numInput.value) return numInput.value;

                    // Check for React-Select single value
                    const el = document.getElementById("{inp_id}");
                    if (!el) return null;
                    let container = el.closest('.field, .select, [class*="field"]');
                    if (!container) container = el.parentElement?.parentElement?.parentElement;
                    if (!container) return null;
                    const sv = container.querySelector('.select__single-value');
                    if (sv && sv.textContent) return sv.textContent.trim();
                    return null;
                }}''')

                sync_value = visible_value or value
                if sync_value:
                    safe_value = sync_value.replace("\\", "\\\\").replace('"', '\\"').replace('\n', ' ')
                    await hidden.evaluate(f'''(el) => {{
                        const nativeSetter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value'
                        ).set;
                        nativeSetter.call(el, "{safe_value}");
                        el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    }}''')
                    logger.info(f"Synced date input {field_prefix} = {sync_value}")
            except Exception as e:
                logger.debug(f"Error syncing date input {field_prefix}: {e}")

    async def _get_label_for_select(self, page: Page, select_elem) -> str:
        """Get the label text for a select element."""
        try:
            label_text = await select_elem.evaluate('''(el) => {
                // Try via for attribute
                if (el.id) {
                    const label = document.querySelector('label[for="' + el.id + '"]');
                    if (label) return label.textContent.trim();
                }
                // Try finding label in parent container
                let container = el.closest('.field, .select, div[class*="field"], div[class*="question"]');
                if (!container) container = el.parentElement?.parentElement;
                if (container) {
                    const label = container.querySelector('label, .field-label, [class*="label"]');
                    if (label) return label.textContent.trim();
                }
                return "";
            }''')
            return label_text or ""
        except Exception:
            return ""

    async def _fill_greenhouse_location(self, page: Page) -> None:
        """Fill Greenhouse location field with hidden input sync."""
        personal = self.form_filler.config.get("personal_info", {})
        city = personal.get("city", "")
        state = personal.get("state", "")

        if not city:
            logger.debug("No city configured, skipping location field")
            return

        location_value = f"{city}, {state}" if state else city
        logger.info(f"Filling Greenhouse location field: {location_value}")

        # FIRST: Try React-Select location dropdowns (common in newer Greenhouse forms)
        if await self._fill_react_select_location(page, city, location_value):
            return

        # Check if location is already filled
        location_input = await page.query_selector('input#candidate-location, input[name="candidate-location"]')
        if location_input:
            current = await location_input.input_value()
            if current and len(current.strip()) > 2:
                logger.debug(f"Location field already filled: {current}")
                return

        # Try multiple selectors for location field
        location_selectors = [
            # Greenhouse geosuggest (most common pattern)
            '.geosuggest input',
            '.geosuggest__input',
            'input[class*="geosuggest"]',
            'input[id*="location"][class*="geosuggest"]',
            # Greenhouse-specific
            'input#candidate-location',
            'input[name="candidate-location"]',
            '#candidate-location input',
            '[data-qa="candidate-location"] input',
            # By field class/container
            '.field--location input',
            '[data-field="location"] input',
            '.candidate-location input',
            # Placeholder-based
            'input[placeholder*="Enter a location" i]',
            'input[placeholder*="Enter your location" i]',
            'input[placeholder*="Location" i]',
            'input[aria-label*="Location" i]',
        ]

        for selector in location_selectors:
            try:
                elem = await page.query_selector(selector)
                if not elem or not await elem.is_visible():
                    continue

                logger.debug(f"Found location field with selector: {selector}")

                # Click and clear
                await elem.click()
                await self.browser_manager.human_delay(200, 400)
                await page.keyboard.press("Control+a")
                await page.keyboard.press("Backspace")

                # Type just the city for better autocomplete matches
                search_term = city
                await elem.type(search_term, delay=80)
                await self.browser_manager.human_delay(1000, 1500)

                # Try to find and click autocomplete suggestion
                autocomplete_selectors = [
                    '.geosuggest__suggests .geosuggest__item',
                    '.geosuggest__item',
                    '[class*="location-autocomplete"] li',
                    '[class*="autocomplete"] [class*="item"]',
                    '[class*="suggestion"]',
                    '[role="listbox"] [role="option"]',
                    '.pac-container .pac-item',
                ]

                found_option = False
                for ac_sel in autocomplete_selectors:
                    options = await page.query_selector_all(ac_sel)
                    for opt in options:
                        if not await opt.is_visible():
                            continue

                        opt_text = (await opt.text_content() or "").strip()
                        if not opt_text:
                            continue

                        logger.debug(f"Location autocomplete option: {opt_text}")

                        # Click best matching option — prefer one with our state/country
                        opt_lower = opt_text.lower()
                        if city.lower() in opt_lower:
                            state = self.form_filler.config.get("personal_info", {}).get("state", "")
                            state_lower = state.lower() if state else ""
                            # Prefer option with our state (e.g., "San Jose, CA" over "San Jose, Costa Rica")
                            if (state_lower and state_lower in opt_lower) or \
                               "united states" in opt_lower or ", us" in opt_lower:
                                await opt.click()
                                await self.browser_manager.human_delay(500, 800)
                                found_option = True
                                break
                    if found_option:
                        break

                if not found_option:
                    # Try keyboard selection
                    await page.keyboard.press("ArrowDown")
                    await self.browser_manager.human_delay(200, 400)
                    await page.keyboard.press("Enter")
                    await self.browser_manager.human_delay(300, 500)

                # Verify the field got filled
                new_value = await elem.input_value()
                if new_value and len(new_value.strip()) > len(city) // 2:
                    logger.info(f"Location field filled: {new_value}")

                    # Check and set hidden input if needed
                    await self._sync_location_hidden_input(page, new_value)
                    return

            except Exception as e:
                logger.debug(f"Location selector {selector} failed: {e}")

        # Last fallback: try to fill any input near "Location" or "City" label
        await self._fill_location_by_label(page, city)

        logger.warning("Could not fill location field")

    async def _fill_react_select_location(self, page: Page, city: str, full_location: str) -> bool:
        """Fill location field if it's a React-Select dropdown."""
        try:
            # Find React-Select dropdowns that might be location fields
            dropdowns = await page.query_selector_all('.select__control, [role="combobox"]')

            for dropdown in dropdowns:
                if not await dropdown.is_visible():
                    continue

                # Get label text
                label_text = await dropdown.evaluate('''(el) => {
                    let container = el.closest('.field, .select, [class*="field"]');
                    if (!container) container = el.parentElement?.parentElement?.parentElement;
                    if (!container) return "";
                    let label = container.querySelector("label, .select__label, [class*='label']");
                    return label ? label.textContent.toLowerCase().trim() : "";
                }''')

                # Check if this is a location field
                if not any(x in label_text for x in ["location", "city", "where", "state"]):
                    continue

                # Check if already filled
                current_text = (await dropdown.text_content() or "").strip()
                if current_text and "select" not in current_text.lower() and len(current_text) > 2:
                    logger.debug(f"Location dropdown already filled: {current_text}")
                    return True

                logger.info(f"Found React-Select location dropdown: {label_text}")

                # Click to open
                await dropdown.click()
                await self.browser_manager.human_delay(500, 800)

                # Type to search - try city first, then state
                search_term = city
                await page.keyboard.type(search_term, delay=50)
                await self.browser_manager.human_delay(800, 1200)

                # Use keyboard-based selection to find best match
                options = await page.query_selector_all('.select__option, [role="option"]')
                visible_options = []
                for i, opt in enumerate(options):
                    if not await opt.is_visible():
                        continue
                    opt_text = (await opt.text_content() or "").strip()
                    if opt_text and "no result" not in opt_text.lower():
                        visible_options.append((i, opt_text))

                if visible_options:
                    # Find best match - prefer US/state matches
                    best_idx = 0
                    state = self.form_filler.config.get("personal_info", {}).get("state", "")
                    for pos, (i, opt_text) in enumerate(visible_options):
                        opt_lower = opt_text.lower()
                        if city.lower() in opt_lower and (
                            "united states" in opt_lower or
                            "usa" in opt_lower or
                            (state and state.lower() in opt_lower) or
                            ", ca" in opt_lower
                        ):
                            best_idx = pos
                            break

                    # Navigate via keyboard
                    for _ in range(best_idx):
                        await page.keyboard.press("ArrowDown")
                        await page.wait_for_timeout(50)
                    await page.keyboard.press("Enter")
                    await self.browser_manager.human_delay(300, 500)
                    logger.info(f"Selected location from dropdown: {visible_options[best_idx][1]}")
                    return True
                else:
                    # No results with city, try state
                    # Clear and retype
                    await page.keyboard.press("Control+a")
                    await page.keyboard.press("Backspace")
                    await page.wait_for_timeout(200)

                    personal = self.form_filler.config.get("personal_info", {})
                    state = personal.get("state", "California")
                    await page.keyboard.type(state, delay=50)
                    await self.browser_manager.human_delay(800, 1200)

                    # Try first result
                    await page.keyboard.press("ArrowDown")
                    await page.wait_for_timeout(100)
                    await page.keyboard.press("Enter")
                    await self.browser_manager.human_delay(300, 500)

                    # Verify
                    new_text = (await dropdown.text_content() or "").strip()
                    if new_text and "select" not in new_text.lower():
                        logger.info(f"Selected location from dropdown with state search: {new_text}")
                        return True

                # Close dropdown
                await page.keyboard.press("Escape")

        except Exception as e:
            logger.debug(f"React-Select location fill error: {e}")

        return False

    async def _fill_location_by_label(self, page: Page, city: str) -> bool:
        """Fill location field by finding input near Location/City label."""
        try:
            # Find all labels and check for location-related ones
            labels = await page.query_selector_all('label')
            for label in labels:
                try:
                    label_text = (await label.text_content() or "").lower()
                    if not any(x in label_text for x in ["location", "city"]):
                        continue

                    # Get the input associated with this label
                    label_for = await label.get_attribute("for")
                    if label_for:
                        inp = await page.query_selector(f'#{label_for}')
                        if inp and await inp.is_visible():
                            current = await inp.input_value()
                            if not current or len(current.strip()) < 2:
                                await inp.fill(city)
                                logger.info(f"Filled location by label: {city}")
                                return True
                    else:
                        # Try finding input in same container
                        container = await label.evaluate('el => el.parentElement')
                        if container:
                            inp = await page.query_selector(':scope >> input[type="text"], :scope >> input:not([type])')
                            if inp and await inp.is_visible():
                                await inp.fill(city)
                                logger.info(f"Filled location by container: {city}")
                                return True

                except Exception:
                    continue

        except Exception as e:
            logger.debug(f"Location by label fill error: {e}")

        return False

    async def _sync_location_hidden_input(self, page: Page, value: str) -> None:
        """Ensure the hidden location input is synced with the visible value."""
        try:
            # Greenhouse sometimes uses a hidden input to store the selected location
            hidden_selectors = [
                'input[type="hidden"][name*="location"]',
                'input[type="hidden"][id*="location"]',
                'input[type="hidden"][name="candidate_location"]',
            ]

            for sel in hidden_selectors:
                hidden = await page.query_selector(sel)
                if hidden:
                    current = await hidden.get_attribute("value") or ""
                    if not current:
                        # Set the value via JavaScript
                        await hidden.evaluate(f'(el) => {{ el.value = "{value}"; }}')
                        logger.debug(f"Synced hidden location input: {value}")
                    return

        except Exception as e:
            logger.debug(f"Error syncing hidden location input: {e}")

    async def _verify_resume_upload(self, page: Page) -> bool:
        """Verify that resume was uploaded successfully."""
        try:
            # Look for upload success indicators
            success_indicators = [
                '.upload-success',
                '[class*="upload"][class*="complete"]',
                '[class*="file-name"]',
                '.file-preview',
                '[class*="uploaded"]',
                '.attachment-filename',
                'span:has-text(".pdf")',
                '[data-field="resume"] [class*="name"]',
            ]

            for sel in success_indicators:
                elem = await page.query_selector(sel)
                if elem and await elem.is_visible():
                    text = await elem.text_content()
                    logger.info(f"Resume upload verified: {text}")
                    return True

            # Check if file input has a file
            file_input = await page.query_selector('input[type="file"]')
            if file_input:
                files = await file_input.evaluate('(el) => el.files.length')
                if files > 0:
                    logger.info("Resume upload verified via file input")
                    return True

            # Check for any text containing the resume filename
            resume_path = self.form_filler.config.get("files", {}).get("resume", "")
            if resume_path:
                import os
                filename = os.path.basename(resume_path)
                page_text = await page.text_content("body") or ""
                if filename in page_text or filename.replace(".pdf", "") in page_text:
                    logger.info(f"Resume upload verified: found '{filename}' on page")
                    return True

            logger.warning("Could not verify resume upload")
            return False

        except Exception as e:
            logger.debug(f"Error verifying resume upload: {e}")
            return False

    async def _run_dry_run_validation(self, page_or_frame) -> bool:
        """
        Run thorough dry-run validation on a filled form.

        Checks core fields, resume upload, hidden input sync, education fields,
        and validation errors. Returns True if the form passes minimum criteria
        (core identity fields + resume if applicable).
        """
        logger.info("DRY RUN: Running detailed validation checks...")
        await self.take_screenshot(page_or_frame, "dry_run_form_filled")

        # ── 1. Core fields ──────────────────────────────────────────────
        filled_fields = await self._check_filled_fields(page_or_frame)
        logger.info(f"DRY RUN: Fields detected as filled: {filled_fields}")

        core_field_names = ["first_name", "last_name", "email"]
        core_filled = sum(1 for f in core_field_names if f in filled_fields)
        core_total = len(core_field_names)

        # Phone is checked separately — only counts if the field exists on page
        phone_field_exists = await page_or_frame.query_selector(
            'input[name="phone"], input#phone, input[type="tel"]'
        )
        if phone_field_exists:
            core_field_names_extended = core_field_names + ["phone"]
            core_filled_ext = sum(1 for f in core_field_names_extended if f in filled_fields)
            core_total_ext = len(core_field_names_extended)
        else:
            core_field_names_extended = core_field_names
            core_filled_ext = core_filled
            core_total_ext = core_total

        core_missing = [f for f in core_field_names_extended if f not in filled_fields]

        # ── 2. Resume upload check ──────────────────────────────────────
        resume_field_exists = await page_or_frame.query_selector(
            'input[type="file"], [class*="resume"], [data-field="resume"], '
            'button:has-text("Attach"), label:has-text("Resume")'
        )
        resume_uploaded = False
        if resume_field_exists:
            body_text = await page_or_frame.text_content("body") or ""
            resume_uploaded = any(
                ext in body_text.lower()
                for ext in [".pdf", ".docx", ".doc", ".rtf"]
            )
            if not resume_uploaded:
                # Also check for file-name-like strings near resume containers
                resume_uploaded = await page_or_frame.evaluate('''() => {
                    const containers = document.querySelectorAll(
                        '[class*="resume"], [class*="attach"], [class*="upload"], [class*="file"]'
                    );
                    for (const c of containers) {
                        const text = c.textContent || "";
                        if (/\\.(pdf|docx?|rtf)/i.test(text)) return true;
                    }
                    return false;
                }''')
        resume_status = "uploaded" if resume_uploaded else ("NOT uploaded" if resume_field_exists else "n/a (no field)")

        # ── 3. Dropdown/React-Select sync ─────────────────────────────
        # Check both hidden inputs (boards.greenhouse.io) and React-Select displays (job-boards.greenhouse.io)
        dropdown_sync = await page_or_frame.evaluate('''() => {
            // Strategy 1: Check hidden question inputs (older Greenhouse forms)
            const hiddenInputs = document.querySelectorAll('input[type="hidden"][id^="question_"]');
            let hiddenTotal = hiddenInputs.length;
            let hiddenFilled = 0;
            for (const inp of hiddenInputs) {
                if (inp.value && inp.value.trim() !== "") hiddenFilled++;
            }

            // Strategy 2: Check React-Select dropdowns (newer job-boards.greenhouse.io)
            const selectControls = document.querySelectorAll('.select__control, [class*="select__control"]');
            let rsTotal = 0;
            let rsFilled = 0;
            let rsEmpty = [];
            for (const ctrl of selectControls) {
                // Skip if not visible
                if (ctrl.offsetParent === null) continue;
                rsTotal++;
                const singleValue = ctrl.querySelector('.select__single-value, [class*="singleValue"]');
                const multiValue = ctrl.querySelectorAll('.select__multi-value__label');
                if ((singleValue && singleValue.textContent.trim() !== "" && singleValue.textContent.trim() !== "Select...") || multiValue.length > 0) {
                    rsFilled++;
                } else {
                    // Try to get label
                    let container = ctrl.closest('.field, .select, [class*="field"]');
                    if (!container) container = ctrl.parentElement?.parentElement?.parentElement;
                    const label = container ? container.querySelector('label') : null;
                    rsEmpty.push(label ? label.textContent.trim().substring(0, 50) : '(unknown)');
                }
            }

            return {
                hiddenTotal, hiddenFilled,
                rsTotal, rsFilled, rsEmpty: rsEmpty.slice(0, 10)
            };
        }''')

        hidden_total = dropdown_sync.get("hiddenTotal", 0)
        hidden_filled = dropdown_sync.get("hiddenFilled", 0)
        rs_total = dropdown_sync.get("rsTotal", 0)
        rs_filled = dropdown_sync.get("rsFilled", 0)
        rs_empty = dropdown_sync.get("rsEmpty", [])

        # Use whichever check found more elements
        if rs_total > hidden_total:
            sync_total = rs_total
            sync_filled = rs_filled
        else:
            sync_total = hidden_total
            sync_filled = hidden_filled
        sync_pct = round(sync_filled / sync_total * 100) if sync_total > 0 else 100

        if rs_empty:
            logger.warning(f"DRY RUN: Unfilled dropdowns: {rs_empty}")

        # ── 4. Education fields ─────────────────────────────────────────
        edu_sync = await page_or_frame.evaluate('''() => {
            const edu_selectors = [
                'input[type="hidden"][id$="--0"][id*="school"]',
                'input[type="hidden"][id$="--0"][id*="degree"]',
                'input[type="hidden"][id$="--0"][id*="discipline"]',
            ];
            let total = 0;
            let filled = 0;
            let details = {};
            for (const sel of edu_selectors) {
                const el = document.querySelector(sel);
                if (el) {
                    total++;
                    const has_val = el.value && el.value.trim() !== "";
                    if (has_val) filled++;
                    details[sel] = has_val ? "filled" : "EMPTY";
                }
            }
            return { total, filled, details };
        }''')
        edu_total = edu_sync.get("total", 0)
        edu_filled = edu_sync.get("filled", 0)
        edu_details = edu_sync.get("details", {})

        if edu_total > 0 and edu_filled < edu_total:
            logger.warning(f"DRY RUN: Incomplete education fields: {edu_details}")

        # ── 5. Validation errors ─────────────────────────────────────────
        # Check for any pre-existing validation errors (before clicking submit)
        validation_errors = await self._check_validation_errors_expanded(page_or_frame)
        # Note: We intentionally do NOT click submit in dry-run mode because:
        # 1. On job-boards.greenhouse.io (React/Remix), clicking submit triggers
        #    React's own validation which may show false positives if React-Select
        #    values are visually set but not synced to Remix form state.
        # 2. The dropdown fill check above (strategy 2) already validates that
        #    React-Select components show selected values.
        # 3. Clicking submit can disrupt the form state making debugging harder.
        if validation_errors:
            logger.warning(f"DRY RUN: Pre-existing validation errors: {validation_errors}")

        error_count = len(validation_errors)

        # ── 6. Validation summary ───────────────────────────────────────
        # Determine pass/fail: require core identity fields + resume (if field exists)
        core_pass = core_filled == core_total  # first_name, last_name, email are mandatory
        resume_pass = resume_uploaded if resume_field_exists else True
        passed = core_pass and resume_pass

        result_str = "PASS" if passed else "FAIL"

        logger.info(
            f"\n  VALIDATION SUMMARY:\n"
            f"    Core fields: {core_filled_ext}/{core_total_ext}"
            f"{(' (missing: ' + ', '.join(core_missing) + ')') if core_missing else ''}\n"
            f"    Resume: {resume_status}\n"
            f"    Dropdowns filled: {sync_filled}/{sync_total} ({sync_pct}%)\n"
            f"    Education fields: {edu_filled}/{edu_total}\n"
            f"    Validation errors: {error_count}\n"
            f"    RESULT: {result_str}"
        )

        if not core_pass:
            logger.error(f"DRY RUN: FAIL - missing core fields: {core_missing}")
        if not resume_pass:
            logger.warning("DRY RUN: FAIL - resume not uploaded despite resume field present")
        if error_count > 0:
            logger.warning(f"DRY RUN: {error_count} validation error(s) detected (non-blocking)")
        if sync_pct < 80 and sync_total > 0:
            logger.warning(f"DRY RUN: Low dropdown fill rate ({sync_pct}%) — form may not submit correctly")

        if passed:
            logger.info("DRY RUN: SUCCESS - form filled and validated")
        else:
            logger.warning("DRY RUN: FAILED - minimum criteria not met")

        return passed

    async def _check_validation_errors_expanded(self, page_or_frame) -> list:
        """Check for validation errors using targeted selectors.

        Only looks for explicit error messages, NOT generic [class*="error"] containers
        which produce false positives on React forms where error slots are always in the DOM.
        """
        # Use JavaScript to find real, visible error messages
        errors = await page_or_frame.evaluate('''() => {
            const results = [];
            const seen = new Set();

            // Specific Greenhouse error message selectors
            const selectors = [
                '.error-message',
                '.form-error',
                '.field-error-message',
                '.field-error',
                '.alert-danger',
                '[data-qa="error"]',
            ];

            for (const sel of selectors) {
                const elems = document.querySelectorAll(sel);
                for (const el of elems) {
                    if (el.offsetParent === null) continue; // not visible
                    const text = (el.textContent || "").trim();
                    // Only count real error text (not labels, not short markers)
                    if (text.length > 5 && text.includes("required") && !seen.has(text)) {
                        seen.add(text);
                        results.push(text.substring(0, 100));
                    }
                }
            }

            // Check [role="alert"] with meaningful text
            const alerts = document.querySelectorAll('[role="alert"]');
            for (const el of alerts) {
                if (el.offsetParent === null) continue;
                const text = (el.textContent || "").trim();
                if (text.length > 5 && !seen.has(text)) {
                    seen.add(text);
                    results.push(text.substring(0, 100));
                }
            }

            return results;
        }''')

        return errors or []

    async def _inject_react_select_values_before_submit(self, page) -> None:
        """LAST RESORT: Inject React-Select display values into empty type='text' inputs.

        Some Greenhouse forms use type='text' inputs for React-Select value stores.
        The dropdown shows the correct value visually, but the input is empty because
        React-Select manages its own state. Setting the input value earlier triggers
        React re-render which clears the dropdown.

        This function runs RIGHT BEFORE submit to inject values at the last possible
        moment, giving React no time to re-render.
        """
        try:
            injected = await page.evaluate('''() => {
                const results = [];
                // Find all visible React-Select controls
                const selectControls = document.querySelectorAll('.select__control, [class*="select__control"]');

                for (const ctrl of selectControls) {
                    if (ctrl.offsetParent === null) continue;

                    // Get the displayed value
                    const sv = ctrl.querySelector('.select__single-value, [class*="singleValue"]');
                    if (!sv || !sv.textContent || sv.textContent.trim() === "Select..." || sv.textContent.trim().length < 2) {
                        continue; // No display value
                    }
                    const displayValue = sv.textContent.trim();

                    // Find the container and look for empty type="text" or type="hidden" inputs
                    let container = ctrl.closest('.field, .select, [class*="field"], [class*="question"]');
                    if (!container) container = ctrl.parentElement?.parentElement?.parentElement;
                    if (!container) continue;

                    // Find all inputs in this container that are value stores (not search inputs)
                    const inputs = container.querySelectorAll('input');
                    for (const inp of inputs) {
                        const inputId = inp.id || '';
                        const inputName = inp.name || '';
                        if (!inputId && !inputName) continue;

                        // Skip React-Select's own search/filter input
                        // (has id like 'react-select-XXXXX-input' or is inside select__input)
                        if (inputId.startsWith('react-select-')) continue;
                        if (inp.getAttribute('role') === 'combobox') continue;

                        const val = inp.value ? inp.value.trim() : '';
                        if (val !== '') continue; // Already has value

                        // This is an empty value-store input — inject the display value
                        const nativeSetter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value'
                        ).set;
                        nativeSetter.call(inp, displayValue);
                        inp.dispatchEvent(new Event('input', { bubbles: true }));
                        inp.dispatchEvent(new Event('change', { bubbles: true }));
                        results.push({ inputId: inputId || inputName, value: displayValue, type: inp.type });
                    }
                }
                return results;
            }''')

            if injected:
                for item in injected:
                    logger.info(f"PRE-SUBMIT INJECT: Set {item['inputId']} (type={item['type']}) = {item['value']}")
                logger.info(f"PRE-SUBMIT INJECT: Injected {len(injected)} empty inputs from React-Select display values")
            else:
                logger.debug("PRE-SUBMIT INJECT: No empty inputs found needing injection")

        except Exception as e:
            logger.debug(f"Error in pre-submit inject: {e}")

    async def _pre_submit_validation(self, page) -> dict:
        """
        Check ALL required fields are filled BEFORE clicking submit.
        Returns dict with 'passed', 'empty_dropdowns', 'empty_required', 'dropdown_fill_pct'.
        """
        result = {"passed": True, "empty_dropdowns": [], "empty_required": [], "dropdown_fill_pct": 100}

        # 1. Check React-Select dropdowns (the main failure point)
        dropdown_info = await page.evaluate('''() => {
            const selectControls = document.querySelectorAll('.select__control, [class*="select__control"]');
            let total = 0, filled = 0;
            let emptyLabels = [];
            let emptyInputIds = [];
            // Education field labels that React-Select manages internally
            // (on type="text" forms, the value lives in React state, not in the DOM input)
            const eduKeywords = ['school', 'degree', 'discipline', 'start date', 'end date',
                                 'start month', 'end month', 'start year', 'end year'];
            for (const ctrl of selectControls) {
                if (ctrl.offsetParent === null) continue;
                total++;
                const sv = ctrl.querySelector('.select__single-value, [class*="singleValue"]');
                const mv = ctrl.querySelectorAll('.select__multi-value__label');
                if ((sv && sv.textContent.trim() !== "" && sv.textContent.trim() !== "Select...") || mv.length > 0) {
                    filled++;
                } else {
                    let container = ctrl.closest('.field, .select, [class*="field"]');
                    if (!container) container = ctrl.parentElement?.parentElement?.parentElement;
                    const label = container ? container.querySelector('label') : null;
                    const labelText = label ? label.textContent.trim() : '(unknown)';

                    // Check if this is an education dropdown — if the placeholder shows
                    // the selected value (React-Select sometimes renders it differently)
                    const labelLower = labelText.toLowerCase();
                    const isEduField = eduKeywords.some(kw => labelLower.includes(kw));

                    if (isEduField) {
                        // For education fields, check if the React-Select placeholder
                        // or input has a value (React state may be set even if single-value
                        // element is missing)
                        const placeholder = ctrl.querySelector('.select__placeholder, [class*="placeholder"]');
                        const inputEl = ctrl.querySelector('input');
                        const hasPlaceholderValue = placeholder && placeholder.textContent.trim() !== "Select..."
                            && placeholder.textContent.trim().length > 1;
                        // Check for react internal state via data attributes or aria
                        const ariaLabel = ctrl.getAttribute('aria-label') || '';
                        if (hasPlaceholderValue) {
                            filled++;
                            continue;
                        }
                    }

                    // Try to find the hidden input ID
                    const hiddenInput = container ? container.querySelector('input[type="hidden"][id^="question_"]') : null;
                    const inputId = hiddenInput ? hiddenInput.id : '';
                    emptyLabels.push(labelText.substring(0, 80));
                    emptyInputIds.push(inputId);
                }
            }
            return { total, filled, emptyLabels, emptyInputIds };
        }''')

        total = dropdown_info.get("total", 0)
        filled = dropdown_info.get("filled", 0)
        empty_labels = dropdown_info.get("emptyLabels", [])
        empty_input_ids = dropdown_info.get("emptyInputIds", [])
        pct = round(filled / total * 100) if total > 0 else 100

        result["dropdown_fill_pct"] = pct
        result["empty_dropdowns"] = empty_labels

        # 2. Check required text inputs that are empty (but skip React-Select search inputs)
        empty_required = await page.evaluate('''() => {
            const results = [];
            const inputs = document.querySelectorAll('input[type="text"], input[type="email"], input[type="tel"], textarea');
            for (const inp of inputs) {
                if (inp.offsetParent === null) continue;
                if (inp.type === 'hidden') continue;
                const val = inp.value ? inp.value.trim() : '';
                if (val !== '') continue;
                // Check if required
                const isRequired = inp.required || inp.getAttribute('aria-required') === 'true';
                let container = inp.closest('.field, [class*="field"]');
                if (!container) container = inp.parentElement;
                const label = container ? container.querySelector('label') : null;
                const labelText = label ? (label.textContent || '').trim() : '';
                const hasAsterisk = labelText.includes('*');
                if (isRequired || hasAsterisk) {
                    // Skip geosuggest/location — autocomplete value may not show in DOM
                    if (inp.id === 'candidate-location' || inp.name === 'candidate-location' ||
                        inp.className.includes('geosuggest') || inp.closest('.geosuggest')) continue;
                    // Skip inputs inside React-Select containers — those are search inputs,
                    // the actual value is tracked by the dropdown check above
                    if (inp.closest('.select__control, .select__input-container, [class*="select__"]')) continue;
                    // Skip education/country hidden-paired fields (React-Select rendered)
                    const iid = (inp.id || '').toLowerCase();
                    const iname = (inp.name || '').toLowerCase();
                    const reactSelectFieldIds = ['school--0', 'degree--0', 'discipline--0',
                        'start-month--0', 'end-month--0', 'start-year--0', 'end-year--0', 'country'];
                    if (reactSelectFieldIds.some(fid => iid === fid || iname === fid)) continue;
                    results.push(labelText.substring(0, 80) || inp.name || inp.id || '(unknown)');
                }
            }
            return results;
        }''')

        result["empty_required"] = empty_required or []
        result["empty_input_ids"] = empty_input_ids or []

        # 3. Decide pass/fail — be smart about thresholds
        total_empty = len(empty_labels) + len(empty_required)

        if total_empty > 0:
            logger.warning(f"PRE-SUBMIT CHECK: {len(empty_labels)} empty dropdowns: {empty_labels}")
            if empty_required:
                logger.warning(f"PRE-SUBMIT CHECK: {len(empty_required)} empty required fields: {empty_required}")
            logger.warning(f"PRE-SUBMIT CHECK: Dropdown fill rate: {filled}/{total} ({pct}%)")

            # Allow submission if: dropdown fill rate >= 80%, no required text fields empty,
            # and only a few (<=3) custom dropdown questions are missing
            if pct >= 80 and len(empty_required) == 0 and len(empty_labels) <= 3:
                result["passed"] = True
                logger.warning(
                    f"PRE-SUBMIT CHECK: ALLOWING with {len(empty_labels)} minor dropdowns empty "
                    f"(fill rate {pct}%, no required text fields missing)"
                )
            else:
                result["passed"] = False
        else:
            logger.info(f"PRE-SUBMIT CHECK: All fields filled ({total} dropdowns, fill rate {pct}%)")

        return result

    async def _submit_application(self, page: Page) -> bool:
        """Submit the application."""
        # Check for dry run mode
        if self.dry_run:
            return await self._run_dry_run_validation(page)

        # Review mode — pause for user to verify before submitting
        if self.review_mode:
            company = self.ai_answerer.job_context.get("company", "Unknown")
            role = self.ai_answerer.job_context.get("role", "Unknown")
            approved = await self.pause_for_review(page, company, role)
            if not approved:
                return False

        # ── PRE-SUBMIT VALIDATION: Check all fields are filled ──────────
        validation = await self._pre_submit_validation(page)
        if not validation["passed"]:
            logger.warning("PRE-SUBMIT: Empty fields detected — attempting retry fill...")

            empty_req = validation.get("empty_required", [])
            empty_dd = validation.get("empty_dropdowns", [])

            # Retry location if candidate-location is empty
            if any("location" in f.lower() or "candidate" in f.lower() for f in empty_req):
                logger.info("PRE-SUBMIT: Retrying location fill...")
                await self._fill_greenhouse_location(page)

            # Retry country if 'country' is empty
            if any("country" in f.lower() for f in empty_req):
                logger.info("PRE-SUBMIT: Retrying country fill...")
                await self._fill_greenhouse_country(page)

            # Retry education fields if school/degree/discipline are empty
            edu_fields = ["school", "degree", "discipline", "start-month", "end-month"]
            if any(any(ef in f.lower() for ef in edu_fields) for f in empty_req):
                logger.info("PRE-SUBMIT: Retrying education fields...")
                await self._fill_greenhouse_education_fields(page)

            # Retry: re-run custom questions to fill empty dropdowns
            job_data = {"company": self.ai_answerer.job_context.get("company", ""),
                        "role": self.ai_answerer.job_context.get("role", "")}
            await self._handle_custom_questions(page, job_data)
            await self._sync_all_hidden_question_inputs(page)
            await self.browser_manager.human_delay(500, 1000)

            # LAST RESORT: Force-fill hidden inputs directly if React-Select failed
            await self._force_fill_education_hidden_inputs(page)
            # Force-fill country hidden input
            country_val = self.form_filler.config.get("personal_info", {}).get("country", "United States")
            try:
                for sel in ['input[type="hidden"][id="country"]', 'input[type="hidden"][name="country"]']:
                    h = await page.query_selector(sel)
                    if h:
                        cur = await h.get_attribute("value") or ""
                        if not cur.strip():
                            await h.evaluate(f'''(el) => {{
                                const nativeSetter = Object.getOwnPropertyDescriptor(
                                    window.HTMLInputElement.prototype, 'value'
                                ).set;
                                nativeSetter.call(el, "{country_val}");
                                el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                                el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                            }}''')
                            logger.info(f"Force-filled country hidden input: {country_val}")
            except Exception:
                pass

            await self.browser_manager.human_delay(300, 500)

            # Re-validate after retry
            validation2 = await self._pre_submit_validation(page)
            if not validation2["passed"]:
                empty_count = len(validation2["empty_dropdowns"]) + len(validation2["empty_required"])
                if len(validation2["empty_required"]) > 0:
                    # Required text fields empty — this is a hard fail
                    logger.error(
                        f"PRE-SUBMIT: ABORTING — {len(validation2['empty_required'])} required text fields empty "
                        f"after retry: {validation2['empty_required']}"
                    )
                    return False
                elif validation2.get("dropdown_fill_pct", 100) < 50:
                    # Less than 50% dropdowns filled — too many missing
                    logger.error(
                        f"PRE-SUBMIT: ABORTING — dropdown fill rate too low ({validation2['dropdown_fill_pct']}%) "
                        f"after retry: {validation2['empty_dropdowns']}"
                    )
                    return False
                else:
                    # Some dropdowns empty but fill rate OK — these may be React-Select
                    # type="text" forms where the display value is set but DOM input is empty.
                    # The inject function below will handle this at submit time.
                    logger.warning(
                        f"PRE-SUBMIT: {empty_count} dropdowns still empty after retry "
                        f"(fill rate {validation2.get('dropdown_fill_pct', '?')}%), "
                        f"proceeding — inject will set values before submit: {validation2['empty_dropdowns']}"
                    )
            else:
                logger.info("PRE-SUBMIT: Retry successful — all fields now filled")

        # CRITICAL: Last-resort inject — set empty type="text" inputs from React-Select display values.
        # Must happen RIGHT before submit so React doesn't re-render and clear them.
        # This handles forms where React-Select uses type="text" instead of type="hidden".
        await self._inject_react_select_values_before_submit(page)

        # Solve invisible reCAPTCHA before clicking submit
        captcha_solved = await self.solve_invisible_recaptcha(page)
        if not captcha_solved:
            logger.error("Failed to solve reCAPTCHA - cannot submit")
            return False

        submit_selectors = [
            'button[type="submit"]',
            'input[type="submit"]',
            'button:has-text("Submit")',
            'button:has-text("Apply")',
            '#submit_app',
            '.submit-button',
        ]

        for selector in submit_selectors:
            try:
                btn = await page.query_selector(selector)
                if btn and await btn.is_visible():
                    await self.browser_manager.human_delay(500, 1000)
                    await btn.click()
                    logger.info("Clicked submit button")
                    await self.browser_manager.human_delay(2000, 3000)

                    # Check if a CAPTCHA challenge appeared after clicking
                    if await self.has_captcha(page):
                        logger.warning("CAPTCHA challenge appeared after submit")
                        solved = await self.handle_captcha(page)
                        if not solved:
                            return False
                        # Re-click submit after solving
                        await self.browser_manager.human_delay(1000, 2000)
                        await btn.click()
                        await self.browser_manager.human_delay(2000, 3000)

                    # Check for email verification code field (appears AFTER first submit)
                    verified = await self._handle_email_verification(page)
                    if verified:
                        logger.info("Verification code entered — re-submitting")
                        await self.browser_manager.human_delay(500, 1000)
                        # Re-click submit after entering verification code
                        btn2 = await page.query_selector(selector)
                        if btn2 and await btn2.is_visible():
                            await btn2.click()
                            logger.info("Re-clicked submit after verification code")
                            await self.browser_manager.human_delay(2000, 3000)

                    return True
            except Exception:
                continue

        logger.warning("Could not find submit button")
        return False

    async def _handle_email_verification(self, page) -> bool:
        """
        Detect and handle Greenhouse email verification code.
        Greenhouse shows a verification code field as part of the form, before submit.
        The code is sent to the applicant's email and must be entered to submit.
        Supports both single-input and multi-box (one input per character) layouts.
        Returns True if a code was found and entered, False if no verification was needed/available.
        """
        try:
            # Scroll to bottom first — the verification field may be at the very bottom
            await page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
            await page.wait_for_timeout(500)

            # Check page text for verification code prompt
            page_text = (await page.text_content("body") or "").lower()
            has_verification = (
                "verification code" in page_text
                or "security code" in page_text
                or ("character code" in page_text and "confirm" in page_text)
            )

            if not has_verification:
                return False

            logger.info("Greenhouse email verification code prompt detected!")

            # Check if email_verifier is available
            email_verifier = getattr(self, "email_verifier", None)
            if not email_verifier:
                logger.warning("Email verifier not configured — cannot fetch verification code")
                return False

            # Fetch the verification code from Gmail
            # Use company name in subject filter to avoid grabbing codes from previous applications
            # Greenhouse emails have subject: "Security code for your application to [Company]"
            company_name = getattr(self, "_current_company", "") or ""
            subject_kw = company_name if company_name else None
            code = email_verifier.get_verification_code(
                sender_filter="greenhouse",
                subject_filter=subject_kw,
                max_age_seconds=120,
                timeout=90,
                poll_interval=3,
            )

            if not code:
                # Try broader search without company-specific subject filter
                code = email_verifier.get_verification_code(
                    sender_filter="greenhouse",
                    max_age_seconds=120,
                    timeout=30,
                    poll_interval=3,
                )

            if not code:
                logger.warning("Could not retrieve email verification code from Gmail")
                return False

            logger.info(f"Got verification code: {code}")

            # Strategy 1: Multi-box input (one input per character, e.g. "Security code" with 6-8 boxes)
            # Look for a group of small single-char inputs near the verification text
            code_inputs = await page.query_selector_all('input[maxlength="1"][type="text"], input[maxlength="1"][type="tel"], input[maxlength="1"]:not([type="hidden"])')
            visible_code_inputs = []
            for inp in code_inputs:
                try:
                    if await inp.is_visible():
                        visible_code_inputs.append(inp)
                except Exception:
                    continue

            if len(visible_code_inputs) >= 4 and len(visible_code_inputs) <= 10:
                # Multi-box code input detected
                logger.info(f"Found {len(visible_code_inputs)} code input boxes, entering code: {code}")
                for i, char in enumerate(code):
                    if i < len(visible_code_inputs):
                        await visible_code_inputs[i].fill(char)
                        await self.browser_manager.human_delay(50, 150)
                logger.info(f"Entered verification code across {min(len(code), len(visible_code_inputs))} boxes")
                await self.browser_manager.human_delay(500, 1000)
                return True

            # Strategy 2: Single text input for the full code
            single_input_selectors = [
                'input[name*="verification"]',
                'input[name*="verify"]',
                'input[name*="security_code"]',
                'input[name*="code"]',
                'input[placeholder*="code"]',
                'input[aria-label*="verification"]',
                'input[aria-label*="code"]',
                'input[aria-label*="security"]',
            ]

            for sel in single_input_selectors:
                try:
                    inp = await page.query_selector(sel)
                    if inp and await inp.is_visible():
                        await inp.fill(code)
                        logger.info(f"Entered verification code in single input: {code}")
                        await self.browser_manager.human_delay(500, 1000)
                        return True
                except Exception:
                    continue

            logger.warning("Verification code prompt detected but could not find input field to enter code")
            return False

        except Exception as e:
            logger.debug(f"Email verification handling error: {e}")
            return False

    async def _check_filled_fields(self, page: Page) -> Dict[str, str]:
        """Check which fields have been filled (for dry run validation)."""
        filled = {}
        field_checks = [
            ('first_name', 'input[name="first_name"], input#first_name'),
            ('last_name', 'input[name="last_name"], input#last_name'),
            ('email', 'input[name="email"], input#email, input[type="email"]'),
            ('phone', 'input[name="phone"], input#phone, input[type="tel"]'),
            ('location', 'input#candidate-location, input[name*="location"]'),
        ]

        for field_name, selector in field_checks:
            try:
                elem = await page.query_selector(selector)
                if elem:
                    value = await elem.input_value()
                    if value and value.strip():
                        filled[field_name] = value[:50]  # Truncate for logging
            except Exception:
                continue

        return filled

    async def _verify_and_trigger_basic_fields(self, page: Page) -> None:
        """
        Re-verify basic fields and trigger events to ensure React picks them up.
        This fixes issues where fields are filled but React validation doesn't see them.
        """
        personal = self.form_filler.config.get("personal_info", {})

        basic_fields = [
            ('input#first_name, input[name="first_name"]', personal.get("first_name", ""), "first_name"),
            ('input#last_name, input[name="last_name"]', personal.get("last_name", ""), "last_name"),
            ('input#email, input[name="email"], input[type="email"]', personal.get("email", ""), "email"),
            ('input#phone, input[name="phone"], input[type="tel"]', personal.get("phone", ""), "phone"),
        ]

        for selectors, expected_value, field_name in basic_fields:
            if not expected_value:
                continue

            try:
                for selector in selectors.split(", "):
                    elem = await page.query_selector(selector)
                    if elem and await elem.is_visible():
                        current = await elem.input_value()

                        # If empty, re-fill
                        if not current or not current.strip():
                            logger.warning(f"Re-filling empty {field_name} field")
                            await elem.click()
                            await elem.fill(expected_value)

                        # Always trigger events to ensure React sees the value
                        await elem.evaluate('''(el) => {
                            // Trigger all common events that React might be listening for
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                            el.dispatchEvent(new Event('blur', { bubbles: true }));
                        }''')

                        # Also click out and back to trigger blur/focus
                        await page.keyboard.press("Tab")
                        await self.browser_manager.human_delay(50, 100)

                        logger.debug(f"Triggered events on {field_name}")
                        break

            except Exception as e:
                logger.debug(f"Error verifying {field_name}: {e}")

    def _resolve_file_path(self, path: str) -> str:
        """Resolve a file path - handles relative paths from project root."""
        if not path:
            return ""
        import os
        if os.path.isabs(path):
            return path if os.path.exists(path) else ""
        # Try relative to working directory
        if os.path.exists(path):
            return os.path.abspath(path)
        # Try relative to project root (src/../)
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        resolved = os.path.join(project_root, path)
        if os.path.exists(resolved):
            return resolved
        logger.warning(f"File not found: {path} (tried {resolved})")
        return ""
