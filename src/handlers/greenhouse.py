"""
Greenhouse Handler

Handles job applications on Greenhouse ATS.
URLs: boards.greenhouse.io, job-boards.greenhouse.io
"""

import asyncio
import random
from typing import Dict, Any
from playwright.async_api import Page, Frame
from loguru import logger

from .base import BaseHandler


class GreenhouseHandler(BaseHandler):
    """Handler for Greenhouse ATS applications."""

    name = "greenhouse"

    @staticmethod
    def _get_keyboard(page_or_frame):
        """Get the keyboard from a Page or Frame object.

        Playwright Frame objects don't have .keyboard directly —
        keyboard events must go through frame.page.keyboard.
        """
        if isinstance(page_or_frame, Frame):
            return page_or_frame.page.keyboard
        return page_or_frame.keyboard

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

            # Click Simplify autofill button NOW (on listing page — extension is visible here)
            await self.wait_for_extension_autofill(page)

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

                # Re-trigger Simplify on the form page (it was only triggered on the listing page)
                await self.wait_for_extension_autofill(page)

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

            # Reset reCAPTCHA so user can manually submit this tab later
            try:
                await page.evaluate('''() => {
                    // Reset grecaptcha so user gets a fresh token on manual submit
                    if (window.grecaptcha && window.grecaptcha.reset) {
                        try { window.grecaptcha.reset(); } catch(e) {}
                    }
                    // Also clear any stale response tokens
                    const textarea = document.querySelector('#g-recaptcha-response, [name="g-recaptcha-response"]');
                    if (textarea) textarea.value = '';
                }''')
                logger.info("Reset reCAPTCHA for manual submission")
            except Exception:
                pass

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

    async def _fill_form_fields(self, target, job_data: Dict[str, Any]) -> None:
        """Shared form-filling pipeline used by both standard and embedded forms.

        Args:
            target: Either a Page (standard) or Frame (embedded iframe).
            job_data: Job metadata dict with company/role.
        """
        # Fill basic fields using form filler
        await self.form_filler.fill_form(target)
        await self.browser_manager.human_delay(500, 1000)

        # CRITICAL: Re-verify and trigger events on basic fields (fixes React validation)
        await self._verify_and_trigger_basic_fields(target)
        await self.browser_manager.human_delay(300, 500)

        # Fill Greenhouse-specific education fields (hidden inputs)
        await self._fill_greenhouse_education_fields(target)
        await self.browser_manager.human_delay(300, 600)
        await self._force_fill_education_hidden_inputs(target)

        # Fill work experience fields (company, title, dates)
        await self._fill_greenhouse_work_experience_fields(target)
        await self.browser_manager.human_delay(300, 600)

        # Fill country field (React-Select dropdown)
        await self._fill_greenhouse_country(target)
        await self.browser_manager.human_delay(200, 400)

        # Fill phone country code dropdown (React-Select)
        await self._fill_greenhouse_phone_country_code(target)
        await self.browser_manager.human_delay(200, 400)

        # Fill source/referral dropdown
        await self._fill_greenhouse_source_dropdown(target)
        await self.browser_manager.human_delay(200, 400)

        # Fill location field (handles hidden input sync)
        await self._fill_greenhouse_location(target)
        await self.browser_manager.human_delay(300, 600)

        # Upload resume
        resume_path = self._resolve_file_path(self.form_filler.config.get("files", {}).get("resume"))
        if resume_path:
            uploaded = await self._upload_resume(target, resume_path)
            if uploaded:
                await self._verify_resume_upload(target)

        # Upload cover letter ONLY if there's a clearly labeled cover letter field
        cover_letter_path = self._resolve_file_path(self.form_filler.config.get("files", {}).get("cover_letter"))
        if cover_letter_path:
            has_cl_field = await target.query_selector(
                'input[type="file"][data-field*="cover"], input[type="file"][name*="cover"], '
                'input[type="file"]#cover_letter, input[type="file"][id*="cover"]'
            )
            if has_cl_field:
                await self._upload_cover_letter(target, cover_letter_path)
            else:
                logger.debug("No cover letter field found — skipping upload")

        # Upload transcript if available
        transcript_path = self._resolve_file_path(self.form_filler.config.get("files", {}).get("transcript"))
        if transcript_path:
            await self._upload_transcript(target, transcript_path)

        # Handle custom questions
        await self._handle_custom_questions(target, job_data)

        # Fill demographic questions if present
        await self._fill_demographics(target)

        # CRITICAL: Sync all hidden question_XXXXXXXX inputs that might still be empty
        await self._sync_all_hidden_question_inputs(target)

    async def _fill_standard_form(self, page: Page, job_data: Dict[str, Any]) -> bool:
        """Fill standard Greenhouse application form."""
        try:
            await self._fill_form_fields(page, job_data)

            # Handle email verification code if present (must be before submit)
            await self._handle_email_verification(page)

            # Submit
            return await self._submit_application(page)

        except Exception as e:
            logger.error(f"Error filling Greenhouse form: {e}")
            return False

    async def _fill_embedded_form(self, page: Page, job_data: Dict[str, Any]) -> bool:
        """Fill embedded Greenhouse iframe form.

        Finds the iframe, fills form fields inside it using the shared pipeline,
        then handles CAPTCHA and submit on the parent page.
        """
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

            # Wait for Simplify to auto-fill (operates on parent page)
            await self.wait_for_extension_autofill(page)

            # Fill form inside iframe using shared pipeline
            logger.info("Filling embedded form fields")
            await self._fill_form_fields(frame, job_data)

            # Handle email verification code (check parent page — code field is outside iframe)
            await self._handle_email_verification(page)

            # Submit (check for dry run)
            if self.dry_run:
                logger.info("DRY RUN: Embedded form filled, running validation on iframe")
                return await self._run_dry_run_validation(frame)

            # Solve invisible reCAPTCHA on parent page (reCAPTCHA is usually outside the iframe)
            captcha_solved = await self.solve_invisible_recaptcha(page)
            if not captcha_solved:
                logger.error("Failed to solve reCAPTCHA for embedded form - cannot submit")
                return False

            # Check required fields before submit
            empty_fields = await self._check_required_fields_before_submit(frame)
            if empty_fields:
                logger.warning(
                    f"IFRAME PRE-SUBMIT: {len(empty_fields)} required fields still empty: {empty_fields} — "
                    f"leaving tab open for manual completion"
                )
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
            logger.info("Falling back to standard form after iframe error")
            return await self._fill_standard_form(page, job_data)

    async def _upload_resume(self, page_or_frame, resume_path: str) -> bool:
        """Upload resume file. Works with both Page and Frame objects."""
        try:
            # Greenhouse typically uses data-field="resume" or similar
            file_input = await page_or_frame.query_selector(
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
            upload_btn = await page_or_frame.query_selector(
                'button:has-text("Upload"), '
                'button:has-text("Attach"), '
                '[data-field="resume"] button'
            )
            if upload_btn:
                # Get Page from Frame if needed — Frame objects don't have expect_file_chooser()
                page_obj = page_or_frame.page if hasattr(page_or_frame, 'page') else page_or_frame
                async with page_obj.expect_file_chooser() as fc_info:
                    await upload_btn.click()
                file_chooser = await fc_info.value
                await file_chooser.set_files(resume_path)
                logger.info("Resume uploaded via button")
                return True

        except Exception as e:
            logger.warning(f"Could not upload resume: {e}")

        return False

    async def _upload_cover_letter(self, page_or_frame, cover_letter_path: str) -> bool:
        """Upload cover letter file if there's a field for it. Works with both Page and Frame objects."""
        try:
            # Strategy 1: Look for cover letter specific file inputs
            file_input = await page_or_frame.query_selector(
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

            # Strategy 2: Find by label text containing "cover letter"
            labels = await page_or_frame.query_selector_all(
                '.upload-label, label[class*="upload"], label, .field-label, '
                '[class*="label"], legend, .attachment-label'
            )
            for label in labels:
                label_text = ((await label.text_content()) or "").lower()
                if "cover letter" in label_text or "cover_letter" in label_text:
                    parent = await label.evaluate_handle(
                        'el => el.closest(".field, .upload-field, .form-field, '
                        '[class*=\\"attachment\\"], [class*=\\"upload\\"], div")'
                    )
                    if parent:
                        fi = await parent.query_selector('input[type="file"]')
                        if fi:
                            await fi.set_input_files(cover_letter_path)
                            logger.info("Cover letter uploaded via label match")
                            await self.browser_manager.human_delay(500, 1000)
                            return True

            # Strategy 3: Check for a labeled cover letter section by attributes
            cover_sections = await page_or_frame.query_selector_all('[data-field*="cover"], [class*="cover-letter"], [id*="cover"]')
            for section in cover_sections:
                file_input = await section.query_selector('input[type="file"]')
                if file_input:
                    await file_input.set_input_files(cover_letter_path)
                    logger.info("Cover letter uploaded via labeled section")
                    return True

            # Strategy 4: Second file input is often cover letter (first is resume)
            all_file_inputs = await page_or_frame.query_selector_all('input[type="file"]')
            if len(all_file_inputs) >= 2:
                await all_file_inputs[1].set_input_files(cover_letter_path)
                logger.info("Cover letter uploaded to second file input")
                return True

            logger.debug("No cover letter field found")
            return False

        except Exception as e:
            logger.debug(f"Could not upload cover letter: {e}")
        return False

    async def _upload_transcript(self, page_or_frame, transcript_path: str) -> bool:
        """Upload transcript file if there's a field for it. Works with both Page and Frame objects."""
        try:
            # Strategy 1: Find by transcript-specific attributes
            file_input = await page_or_frame.query_selector(
                'input[type="file"][data-field*="transcript"], '
                'input[type="file"][name*="transcript"], '
                'input[type="file"][id*="transcript"], '
                'input[type="file"][aria-label*="transcript" i]'
            )
            if file_input:
                await file_input.set_input_files(transcript_path)
                logger.info("Transcript uploaded via attribute match")
                await self.browser_manager.human_delay(500, 1000)
                return True

            # Strategy 2: Find by label text containing "transcript" (broad selector set)
            labels = await page_or_frame.query_selector_all(
                '.upload-label, label[class*="upload"], label, .field-label, '
                '[class*="label"], legend, .attachment-label, '
                '[class*="upload-label"], [class*="error"]'
            )
            for label in labels:
                label_text = ((await label.text_content()) or "").lower()
                if "transcript" in label_text:
                    parent = await label.evaluate_handle(
                        'el => el.closest(".field, .upload-field, .form-field, '
                        '[class*=\\"attachment\\"], [class*=\\"upload\\"], div")'
                    )
                    if parent:
                        fi = await parent.query_selector('input[type="file"]')
                        if fi:
                            await fi.set_input_files(transcript_path)
                            logger.info("Transcript uploaded via label match")
                            await self.browser_manager.human_delay(500, 1000)
                            return True

            # Strategy 3: Find file inputs that are NOT resume and NOT cover letter
            # and check their nearby text for transcript keywords
            all_file_inputs = await page_or_frame.query_selector_all('input[type="file"]')
            for fi in all_file_inputs:
                nearby_text = await fi.evaluate('''(el) => {
                    let container = el.closest(".field, .upload-field, .form-field, div");
                    return container ? container.textContent.toLowerCase() : "";
                }''')
                if "transcript" in nearby_text:
                    await fi.set_input_files(transcript_path)
                    logger.info("Transcript uploaded via nearby text match")
                    await self.browser_manager.human_delay(500, 1000)
                    return True

            # Strategy 4: Third file input fallback (resume=1st, cover letter=2nd, transcript=3rd)
            if len(all_file_inputs) >= 3:
                await all_file_inputs[2].set_input_files(transcript_path)
                logger.info("Transcript uploaded to third file input")
                await self.browser_manager.human_delay(500, 1000)
                return True

            logger.debug("No transcript field found")
            return False

        except Exception as e:
            logger.debug(f"Could not upload transcript: {e}")
            return False

    # _upload_cover_letter_in_frame and _upload_transcript_in_frame removed:
    # _upload_cover_letter() and _upload_transcript() now accept page_or_frame directly.

    async def _fill_greenhouse_phone_country_code(self, page_or_frame) -> None:
        """Fill the phone country code React-Select dropdown in Greenhouse forms.

        Handles field IDs like 'phoneNumber--countryPhoneCode' which is a separate
        React-Select dropdown from the phone number input.
        """
        try:
            # Check if the country code field exists (hidden input)
            hidden = await page_or_frame.query_selector(
                'input[type="hidden"][id*="countryPhoneCode"], '
                'input[type="hidden"][name*="countryPhoneCode"]'
            )

            # Check if already filled
            if hidden:
                current = await hidden.get_attribute("value") or ""
                if current and current.strip():
                    logger.debug(f"Phone country code already filled: {current}")
                    return

            # Find the React-Select dropdown near a phone country code label
            dropdowns = await page_or_frame.query_selector_all('.select__control, [role="combobox"]')
            for dropdown in dropdowns:
                if not await dropdown.is_visible():
                    continue

                # Check if this dropdown is associated with a phone country code field
                is_phone_country = await dropdown.evaluate('''(el) => {
                    // Check for countryPhoneCode in nearby hidden inputs or container IDs
                    let container = el.closest('.field, .select, [class*="field"], [data-field]');
                    if (!container) container = el.parentElement?.parentElement?.parentElement;
                    if (!container) return false;

                    // Check data-field attribute
                    const dataField = container.getAttribute("data-field") || "";
                    if (dataField.includes("countryPhoneCode") || dataField.includes("country_phone")) return true;

                    // Check for hidden input with countryPhoneCode ID
                    const hiddenInput = container.querySelector('input[type="hidden"][id*="countryPhoneCode"]');
                    if (hiddenInput) return true;

                    // Check label text
                    const label = container.querySelector("label, .select__label, [class*='label']");
                    const labelText = label ? label.textContent.toLowerCase().trim() : "";
                    if (labelText.includes("country") && labelText.includes("phone")) return true;
                    if (labelText.includes("phone") && labelText.includes("code")) return true;
                    if (labelText.includes("country code")) return true;

                    // Check if nearby a phone field
                    const phoneField = container.querySelector('input[type="tel"], input[name="phone"], input#phone');
                    if (phoneField && (labelText.includes("country") || labelText.includes("code"))) return true;

                    return false;
                }''')

                if not is_phone_country:
                    continue

                # Check if already has a value selected
                current_text = (await dropdown.text_content() or "").strip()
                if current_text and "select" not in current_text.lower() and len(current_text) > 2:
                    logger.debug(f"Phone country code dropdown already filled: {current_text}")
                    return

                logger.info("Filling phone country code dropdown with 'United States +1'")
                await dropdown.click()
                await self.browser_manager.human_delay(400, 600)

                # Type to search for United States
                await page_or_frame.keyboard.type("United States", delay=50)
                await self.browser_manager.human_delay(600, 900)

                # Try to find and click the +1 option
                options = await page_or_frame.query_selector_all(
                    '.select__option, [id*="option"], [role="option"]'
                )
                for option in options:
                    text = (await option.text_content() or "").strip()
                    if "united states" in text.lower() and "+1" in text:
                        await option.click()
                        logger.info(f"Selected phone country code: {text}")
                        break
                else:
                    # Just press Enter to select the first search result
                    await page_or_frame.keyboard.press("Enter")
                    logger.info("Selected first phone country code search result")

                await self.browser_manager.human_delay(200, 400)

                # Sync hidden input
                if hidden:
                    await hidden.evaluate('''(el) => {
                        el.value = "US";
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                    }''')
                    logger.info("Synced phone country code hidden input")

                return

            # Fallback: force-set hidden input directly if it exists
            if hidden:
                await hidden.evaluate('''(el) => {
                    el.value = "US";
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                }''')
                logger.info("Force-set phone country code hidden input to US")

        except Exception as e:
            logger.debug(f"Error filling phone country code: {e}")

    async def _fill_greenhouse_source_dropdown(self, page_or_frame) -> None:
        """Fill the 'How did you hear about us?' source dropdown (id='source--source').

        This is a standard Greenhouse field, not a custom question, so it needs
        dedicated handling separate from the question_* inputs.
        """
        how_heard = self.form_filler._flat_config.get(
            "common_answers.how_did_you_hear", "Online Job Board"
        )

        try:
            # Check if hidden input for source exists
            hidden = await page_or_frame.query_selector(
                'input[type="hidden"][id="source--source"], '
                'input[type="hidden"][name*="source"]'
            )

            if hidden:
                current = await hidden.get_attribute("value") or ""
                if current and current.strip():
                    logger.debug(f"Source dropdown already filled: {current}")
                    return
            else:
                # No hidden source field — this form probably doesn't have one
                return

            # Find the React-Select dropdown for source
            dropdowns = await page_or_frame.query_selector_all('.select__control, [role="combobox"]')
            for dropdown in dropdowns:
                if not await dropdown.is_visible():
                    continue

                is_source = await dropdown.evaluate('''(el) => {
                    let container = el.closest('.field, .select, [class*="field"]');
                    if (!container) container = el.parentElement?.parentElement?.parentElement;
                    if (!container) return false;

                    // Check hidden input ID
                    const hiddenInput = container.querySelector('input[type="hidden"][id*="source"]');
                    if (hiddenInput) return true;

                    // Check label text
                    const label = container.querySelector("label, .select__label, [class*='label']");
                    const labelText = label ? label.textContent.toLowerCase().trim() : "";
                    if (labelText.includes("how did you hear") || labelText.includes("source") ||
                        labelText.includes("where did you") || labelText.includes("find out about") ||
                        labelText.includes("learn about")) return true;

                    return false;
                }''')

                if not is_source:
                    continue

                # Check if already has a value
                current_text = (await dropdown.text_content() or "").strip()
                if current_text and "select" not in current_text.lower() and len(current_text) > 2:
                    logger.debug(f"Source dropdown already filled: {current_text}")
                    return

                logger.info(f"Filling source dropdown with '{how_heard}'")
                await dropdown.click()
                await self.browser_manager.human_delay(400, 600)

                # Type to search
                await page_or_frame.keyboard.type(how_heard[:20], delay=50)
                await self.browser_manager.human_delay(600, 900)

                # Try to select matching option
                found = await self.form_filler._select_dropdown_option(page_or_frame, how_heard)
                if not found:
                    # Try alternatives: LinkedIn, Internet, Other, Job Board
                    alternatives = ["LinkedIn", "Internet", "Job Board", "Other"]
                    for alt in alternatives:
                        # Clear and try alternative
                        await page_or_frame.keyboard.press("Escape")
                        await self.browser_manager.human_delay(200, 300)
                        await dropdown.click()
                        await self.browser_manager.human_delay(300, 500)
                        await page_or_frame.keyboard.type(alt, delay=50)
                        await self.browser_manager.human_delay(500, 700)
                        found = await self.form_filler._select_dropdown_option(page_or_frame, alt)
                        if found:
                            logger.info(f"Selected source alternative: {alt}")
                            break

                if not found:
                    # Last resort: press Enter for first option
                    await page_or_frame.keyboard.press("Enter")
                    logger.info("Selected first available source option")

                await self.browser_manager.human_delay(200, 400)

                # Sync hidden input
                if hidden:
                    selected_text = how_heard
                    await hidden.evaluate(f'''(el) => {{
                        if (!el.value || el.value.trim() === "") {{
                            el.value = "{selected_text}";
                            el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                            el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        }}
                    }}''')

                return

            # Fallback: force-set hidden input
            if hidden:
                await hidden.evaluate(f'''(el) => {{
                    el.value = "{how_heard}";
                    el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                }}''')
                logger.info(f"Force-set source hidden input: {how_heard}")

        except Exception as e:
            logger.debug(f"Error filling source dropdown: {e}")

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

                    # Skip checkbox inputs (handled separately)
                    if inp_type == "checkbox":
                        logger.debug(f"Skipping {inp_id} - checkbox handled separately")
                        continue

                    # Hidden inputs may be React-Select value stores — check for associated dropdown
                    if inp_type == "hidden":
                        has_react_select = await inp.evaluate('''(el) => {
                            let current = el.parentElement;
                            for (let i = 0; i < 8 && current; i++) {
                                if (current.querySelector('.select__control, [role="combobox"]')) return true;
                                current = current.parentElement;
                            }
                            return false;
                        }''')
                        if has_react_select:
                            # Check if already has a value
                            hidden_val = await inp.evaluate('(el) => el.value')
                            if hidden_val and len(str(hidden_val).strip()) > 0:
                                logger.debug(f"Skipping {inp_id} - hidden React-Select already has value: {hidden_val}")
                                continue
                            logger.info(f"Found hidden React-Select question {inp_id} — will fill dropdown")
                            # Get question text and fill via React-Select
                            question_text = await self._get_question_label(page, inp, inp_id)
                            if not question_text:
                                question_text = f"Additional information required for {inp_id}"
                            react_filled = await self._fill_associated_react_select(page, inp, inp_id, question_text)
                            if react_filled:
                                logger.info(f"Filled hidden React-Select {inp_id} via dropdown")
                            else:
                                # Try click-type-select on the container
                                answer = await self.ai_answerer.answer_question(question_text, "select", max_length=200)
                                if answer:
                                    try:
                                        select_ctrl = await page.evaluate_handle(f'''(function() {{
                                            var inp = document.getElementById("{inp_id}");
                                            if (!inp) return null;
                                            var current = inp.parentElement;
                                            for (var i = 0; i < 8 && current; i++) {{
                                                var ctrl = current.querySelector('.select__control');
                                                if (ctrl) return ctrl;
                                                current = current.parentElement;
                                            }}
                                            return null;
                                        }})()''')
                                        ctrl_elem = select_ctrl.as_element() if select_ctrl else None
                                        if ctrl_elem and await ctrl_elem.is_visible():
                                            await ctrl_elem.click()
                                            await self.browser_manager.human_delay(400, 600)
                                            # Type answer into search input inside React-Select
                                            search_input = await page.query_selector(f'.select__input input, [id^="react-select-"]')
                                            if search_input:
                                                await search_input.fill(answer)
                                            else:
                                                await self._get_keyboard(page).type(answer)
                                            await self.browser_manager.human_delay(500, 800)
                                            option = await page.query_selector('.select__option:first-child, [role="option"]:first-child')
                                            if option and await option.is_visible():
                                                await option.click()
                                                logger.info(f"Click-type-select filled hidden React-Select {inp_id}: {answer}")
                                            else:
                                                await self._get_keyboard(page).press("Enter")
                                                logger.info(f"Enter-selected hidden React-Select {inp_id}: {answer}")
                                    except Exception as e:
                                        logger.debug(f"Hidden React-Select fill failed for {inp_id}: {e}")
                            await self.browser_manager.human_delay(200, 400)
                            continue
                        else:
                            logger.debug(f"Skipping {inp_id} - hidden with no React-Select")
                            continue

                    if not is_visible:
                        logger.debug(f"Skipping {inp_id} - not visible")
                        continue

                    # Handle file inputs - upload transcript/portfolio if label matches
                    if inp_type == "file":
                        question_text_file = await self._get_question_label(page, inp, inp_id)
                        if question_text_file and "transcript" in question_text_file.lower():
                            transcript_path = self._resolve_file_path(
                                self.form_filler.config.get("files", {}).get("transcript")
                            )
                            if transcript_path:
                                await inp.set_input_files(transcript_path)
                                logger.info(f"Transcript uploaded via question file input {inp_id}")
                        elif question_text_file and any(x in question_text_file.lower() for x in ["portfolio", "work sample", "writing sample"]):
                            # Try uploading resume as portfolio fallback
                            resume_path = self._resolve_file_path(
                                self.form_filler.config.get("files", {}).get("resume")
                            )
                            if resume_path:
                                await inp.set_input_files(resume_path)
                                logger.info(f"Resume uploaded as portfolio via question file input {inp_id}")
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

                    # Check if field is REQUIRED — skip optional fields (except LinkedIn/GitHub)
                    is_required = '*' in question_text
                    if not is_required:
                        # Check for aria-required or required attribute
                        is_required = await inp.evaluate('(el) => el.required || el.getAttribute("aria-required") === "true"')
                    if not is_required:
                        # Check parent/label for required indicator
                        is_required = await inp.evaluate('''(el) => {
                            let p = el.closest('.field, .form-field, [class*="field"]');
                            if (p) {
                                let txt = p.textContent || '';
                                if (txt.includes('*') || p.querySelector('.required, [class*="required"]')) return true;
                            }
                            return false;
                        }''')

                    q_lower = question_text.lower()
                    # Always fill LinkedIn and GitHub (helpful for app)
                    is_linkedin_github = any(x in q_lower for x in ['linkedin', 'github', 'portfolio'])
                    # Never fill social media
                    is_social_media = any(x in q_lower for x in ['facebook', 'twitter', 'instagram', 'tiktok', 'snapchat', 'x (fka'])

                    if is_social_media:
                        logger.info(f"Skipping social media field: {question_text[:50]}")
                        continue

                    if not is_required and not is_linkedin_github:
                        logger.info(f"Skipping optional field: {question_text[:50]}")
                        continue

                    logger.info(f"{'[REQ]' if is_required else '[OPT]'} Greenhouse question: {question_text[:50]}...")

                    # Get answer based on field type
                    if tag_name == "select":
                        options = await inp.query_selector_all("option")
                        option_texts = []
                        option_values = []
                        for opt in options:
                            text = (await opt.text_content() or "").strip()
                            val = (await opt.get_attribute("value")) or ""
                            if text and text != "Select..." and text != "Choose...":
                                option_texts.append(text)
                                option_values.append(val)

                        if option_texts:
                            answer = await self.ai_answerer.answer_question(
                                question_text, "select", options=option_texts
                            )
                            if answer:
                                filled_select = False
                                # Try 1: exact label match
                                try:
                                    await inp.select_option(label=answer)
                                    logger.info(f"AI filled select {inp_id}: {answer}")
                                    filled_select = True
                                except Exception:
                                    pass

                                # Try 2: case-insensitive / partial match
                                if not filled_select:
                                    answer_lower = answer.lower().strip()
                                    for i, opt_text in enumerate(option_texts):
                                        opt_lower = opt_text.lower().strip()
                                        if answer_lower == opt_lower or answer_lower in opt_lower or opt_lower in answer_lower:
                                            try:
                                                await inp.select_option(value=option_values[i])
                                                logger.info(f"Fuzzy matched select {inp_id}: '{answer}' → '{opt_text}'")
                                                filled_select = True
                                                break
                                            except Exception:
                                                pass

                                # Try 3: for Yes/No answers, find Yes/No in options
                                if not filled_select and answer_lower in ("yes", "no"):
                                    for i, opt_text in enumerate(option_texts):
                                        if opt_text.lower().strip() == answer_lower:
                                            try:
                                                await inp.select_option(value=option_values[i])
                                                logger.info(f"Yes/No matched select {inp_id}: {answer}")
                                                filled_select = True
                                                break
                                            except Exception:
                                                pass

                                # Try 4: just select the best matching option by index
                                if not filled_select:
                                    try:
                                        await inp.select_option(index=1)  # First non-placeholder option
                                        logger.warning(f"Fallback: selected first option for {inp_id}")
                                    except Exception:
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
                        is_in_react_select = await inp.evaluate('''(el) => {
                            let current = el.parentElement;
                            for (let i = 0; i < 8 && current; i++) {
                                if (current.querySelector('.select__control, [role="combobox"], .select__value-container')) return true;
                                if (current.classList && (current.classList.contains('select__input-container') || current.classList.contains('select'))) return true;
                                current = current.parentElement;
                            }
                            return false;
                        }''')

                        if is_in_react_select:
                            # This is a React-Select dropdown — use the proper dropdown fill
                            logger.info(f"Detected React-Select for question {inp_id}")
                            react_select_filled = await self._fill_associated_react_select(page, inp, inp_id, question_text)
                            if not react_select_filled:
                                # Try harder: find the .select__control by walking up from the input
                                logger.info(f"React-Select fill failed for {inp_id}, trying click-type-select approach")
                                answer = await self.ai_answerer.answer_question(
                                    question_text, "select", max_length=200
                                )
                                if answer:
                                    try:
                                        # Find the select__control container
                                        select_ctrl = await page.evaluate_handle(f'''(function() {{
                                            var inp = document.getElementById("{inp_id}");
                                            if (!inp) return null;
                                            var current = inp.parentElement;
                                            for (var i = 0; i < 8 && current; i++) {{
                                                var ctrl = current.querySelector('.select__control');
                                                if (ctrl) return ctrl;
                                                current = current.parentElement;
                                            }}
                                            return null;
                                        }})()''')
                                        ctrl_elem = select_ctrl.as_element() if select_ctrl else None
                                        if ctrl_elem and await ctrl_elem.is_visible():
                                            # Click to open, type to filter, select option
                                            await ctrl_elem.click()
                                            await self.browser_manager.human_delay(400, 600)
                                            # Type into the search input
                                            await inp.fill(answer)
                                            await self.browser_manager.human_delay(500, 800)
                                            # Try to click first matching option
                                            option = await page.query_selector('.select__option:first-child, [role="option"]:first-child')
                                            if option and await option.is_visible():
                                                await option.click()
                                                logger.info(f"Click-type-select filled React-Select {inp_id}: {answer}")
                                            else:
                                                # Press Enter to select first match
                                                await self._get_keyboard(page).press("Enter")
                                                logger.info(f"Enter-selected React-Select {inp_id}: {answer}")
                                        else:
                                            logger.warning(f"Could not find .select__control for {inp_id}")
                                    except Exception as rs_e:
                                        logger.debug(f"React-Select click-type-select failed for {inp_id}: {rs_e}")
                        else:
                            # Regular text input — fill directly
                            logger.info(f"Filling text input {inp_id}")
                            answer = await self.ai_answerer.answer_question(
                                question_text, "text", max_length=200
                            )
                            logger.info(f"AI answer for {inp_id}: {answer[:50] if answer else 'None'}...")
                            if answer:
                                await inp.fill(answer)
                                logger.info(f"AI filled text input {inp_id}")
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
                    await self._get_keyboard(page).press("Escape")
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
                await self._get_keyboard(page).type(answer[:30], delay=30)
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
                            await self._get_keyboard(page).press("Enter")
                            await page.wait_for_timeout(400)
                            found = True
                    else:
                        # No good match with typed text — clear and try alternatives
                        await self._get_keyboard(page).press("Escape")
                        await page.wait_for_timeout(200)

                        # Try semester alternatives (e.g., "Spring 2026" for "May 2026")
                        for alt_answer in answer_alternatives[1:]:
                            await react_select_elem.click()
                            await self.browser_manager.human_delay(300, 500)
                            await self._get_keyboard(page).type(alt_answer[:30], delay=30)
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
                                        await self._get_keyboard(page).press("Enter")
                                        await page.wait_for_timeout(400)
                                        found = True
                                    break

                            await self._get_keyboard(page).press("Escape")
                            await page.wait_for_timeout(200)
                else:
                    # Clear typed text and try direct option click
                    await self._get_keyboard(page).press("Escape")
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

            # Strategy 3: Acknowledgment/disclosure/policy/export controls fallback
            if not found and any(x in question_text.lower() for x in ["california", "ccpa", "disclosure", "additional information", "acknowledgment", "policy", "usage policy", "employment history", "export control", "export controls", "itar", "u.s. citizen", "authorized to work", "legally authorized", "work authorization", "sponsorship", "require sponsorship"]):
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
                        # Export controls / work authorization — pick affirmative US person/citizen option
                        q_lower_s3 = question_text.lower()
                        if any(x in q_lower_s3 for x in ["export control", "itar", "u.s. citizen", "authorized to work", "legally authorized", "work authorization"]):
                            # Pick options that affirm US citizenship/authorization
                            affirmative_keywords = ["u.s. citizen", "us citizen", "u.s. person", "us person",
                                                     "green card", "permanent resident", "authorized", "i am a u.s.",
                                                     "yes", "i meet", "i qualify", "i can"]
                            if any(x in opt_lower for x in affirmative_keywords):
                                await opt.click()
                                found = True
                                logger.info(f"Selected export/auth option: {opt_text[:50]}...")
                                break
                        # Sponsorship questions — answer No (don't require sponsorship)
                        if any(x in q_lower_s3 for x in ["sponsorship", "require sponsorship"]):
                            negative_keywords = ["no", "do not require", "don't require", "will not require"]
                            if any(x in opt_lower for x in negative_keywords):
                                await opt.click()
                                found = True
                                logger.info(f"Selected sponsorship option: {opt_text[:50]}...")
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

            # Strategy 4: When answer is Yes/No but options are descriptive — pick best affirmative/negative
            if not found and answer.lower() in ("yes", "no"):
                try:
                    await react_select_elem.click()
                    await self.browser_manager.human_delay(300, 500)
                    options = await page.query_selector_all('.select__option, [role="option"]')
                    visible_opts = []
                    for opt in options:
                        if await opt.is_visible():
                            t = (await opt.text_content() or "").strip()
                            if t:
                                visible_opts.append((opt, t))

                    if visible_opts and not any(t.lower() in ("yes", "no") for _, t in visible_opts):
                        # Options are descriptive — pick the most affirmative/negative one
                        if answer.lower() == "yes":
                            affirm = ["i am", "i have", "i do", "i can", "i meet", "i qualify",
                                       "i acknowledge", "i agree", "yes", "authorized", "citizen",
                                       "permanent resident", "u.s. person"]
                            for opt, t in visible_opts:
                                if any(x in t.lower() for x in affirm):
                                    await opt.click()
                                    found = True
                                    logger.info(f"Strategy 4: Selected affirmative option: {t[:50]}")
                                    break
                        else:  # "No"
                            negate = ["i am not", "i do not", "i don't", "no", "none",
                                       "not authorized", "will require"]
                            for opt, t in visible_opts:
                                if any(x in t.lower() for x in negate):
                                    await opt.click()
                                    found = True
                                    logger.info(f"Strategy 4: Selected negative option: {t[:50]}")
                                    break

                        # Last resort — just pick the first option
                        if not found and visible_opts:
                            await visible_opts[0][0].click()
                            found = True
                            logger.info(f"Strategy 4: Selected first option as fallback: {visible_opts[0][1][:50]}")
                except Exception:
                    pass

            if not found:
                await self._get_keyboard(page).press("Escape")
                return False

            await self.browser_manager.human_delay(300, 500)

            # Verify the value was captured by React
            # React-Select stores the REAL value in a sibling hidden input, not the search box
            try:
                inp_type = await inp.get_attribute("type") or ""
                if inp_type == "text":
                    # This is the search box — find the SIBLING hidden input that stores the value
                    hidden_synced = await inp.evaluate(f'''(el) => {{
                        // Walk up to the React-Select container, find the hidden input
                        let container = el.closest('[class*="select"]') || el.parentElement?.parentElement?.parentElement;
                        if (!container) return 'no_container';
                        let hidden = container.querySelector('input[type="hidden"][id="{inp_id}"], input[type="hidden"]');
                        if (hidden && (!hidden.value || hidden.value.trim() === '')) {{
                            // The selected option's value - get from React-Select's internal state
                            let singleValue = container.querySelector('.select__single-value');
                            if (singleValue) {{
                                let displayText = singleValue.textContent.trim();
                                // Set the hidden input value to the display text
                                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                                setter.call(hidden, displayText);
                                hidden.dispatchEvent(new Event('input', {{bubbles: true}}));
                                hidden.dispatchEvent(new Event('change', {{bubbles: true}}));
                                return 'synced:' + displayText;
                            }}
                            return 'no_single_value';
                        }}
                        return hidden ? ('has_value:' + hidden.value.substring(0, 30)) : 'no_hidden';
                    }}''')
                    logger.debug(f"React-Select hidden input sync for {inp_id}: {hidden_synced}")
                else:
                    # type="hidden" — sync directly
                    inp_value = await inp.evaluate('(el) => el.value')
                    if not inp_value or str(inp_value).strip() == "":
                        await inp.evaluate('''(el, val) => {
                            const setter = Object.getOwnPropertyDescriptor(
                                window.HTMLInputElement.prototype, 'value'
                            ).set;
                            setter.call(el, val);
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        }''', answer)
                        logger.info(f"Force-synced hidden input {inp_id}: {answer[:30]}")
            except Exception as sync_err:
                logger.debug(f"Could not sync input {inp_id}: {sync_err}")

            return True

        except Exception as e:
            logger.debug(f"Error in _fill_associated_react_select for {inp_id}: {e}")
            try:
                await self._get_keyboard(page).press("Escape")
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

                    # Visa sponsorship / work authorization checkboxes
                    # "Do you now OR in the future require visa sponsorship" — check based on config
                    elif "sponsor" in label_lower or "visa" in label_lower:
                        work_auth = self.form_filler.config.get("work_authorization", {})
                        need_sponsor = work_auth.get("require_sponsorship", False)
                        need_sponsor_now = work_auth.get("require_sponsorship_now", False)
                        need_sponsor_future = work_auth.get("require_sponsorship_future", False)
                        # If the checkbox says "require sponsorship" — check it only if we need it
                        if need_sponsor or need_sponsor_now or need_sponsor_future:
                            should_check = True
                        else:
                            should_check = False  # Explicitly don't check
                        reason = "sponsorship"

                    # Work authorization checkboxes ("authorized to work in US")
                    elif any(x in label_lower for x in ["authorized to work", "legally authorized", "eligible to work", "right to work"]):
                        should_check = True
                        reason = "work authorization"

                    # Availability/willingness checkboxes
                    elif any(x in label_lower for x in ["available to work", "willing to", "able to work", "can you commit", "full-time", "full time"]):
                        should_check = True
                        reason = "availability"

                    # Relocation / in-office checkboxes
                    elif any(x in label_lower for x in ["relocate", "relocation", "onsite", "on-site", "in person", "in-person", "office"]):
                        should_check = True
                        reason = "relocation/office"

                    # Programming languages / technologies / skills checkbox groups
                    elif not should_check:
                        # Get the parent fieldset label to see if this is a skill question
                        skill_parent_label = ""
                        try:
                            skill_fieldset = await checkbox.evaluate_handle('el => el.closest("fieldset, .field, .checkbox-grouping, [class*=\\"question\\"]")')
                            if skill_fieldset:
                                skill_legend = await skill_fieldset.query_selector("legend, label, .checkbox__description, [class*='label']")
                                if skill_legend:
                                    skill_parent_label = ((await skill_legend.text_content()) or "").lower()
                        except Exception:
                            pass

                        if skill_parent_label and any(x in skill_parent_label for x in [
                            "programming language", "coding language", "technologies",
                            "technical skill", "which language", "proficient in",
                            "comfortable using", "experience with", "familiar with",
                            "what tools", "what technologies", "skills do you have",
                        ]):
                            # Match against configured skills
                            skills_config = self.form_filler.config.get("skills", {})
                            raw_langs = skills_config.get("programming_languages", [])
                            # Handle both dict format ({name: "Java"}) and plain string format
                            languages = []
                            for l in raw_langs:
                                if isinstance(l, dict):
                                    languages.append(l.get("name", "").lower())
                                else:
                                    languages.append(str(l).lower())
                            frameworks = [f.lower() for f in skills_config.get("frameworks", [])]
                            tools = [t.lower() for t in skills_config.get("tools", [])]
                            all_skills = languages + frameworks + tools

                            if any(skill in label_lower for skill in all_skills):
                                should_check = True
                                reason = f"skill ({label_text[:20]})"
                            # Common languages fallback if no config
                            elif not all_skills and any(x in label_lower for x in [
                                "python", "java", "javascript", "typescript", "c++",
                                "sql", "html", "css", "react", "node",
                            ]):
                                should_check = True
                                reason = f"common skill ({label_text[:20]})"

                    # Checkbox groups with "[]" in name — demographics or "how did you hear"
                    if not should_check and "[]" in checkbox_name:
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

                        if parent_label and any(x in parent_label for x in [
                            "how did you hear", "where did you hear", "how did you learn",
                            "how did you find", "source", "learn about us", "find out about",
                            "hear about", "discover us", "referred",
                        ]):
                            how_heard = self.form_filler._flat_config.get("common_answers.how_did_you_hear", "LinkedIn").lower()
                            if any(x in label_lower for x in [
                                how_heard, "linkedin", "online", "job board", "internet",
                                "website", "social media", "search engine", "google",
                                "indeed", "glassdoor", "career", "other",
                            ]):
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
                                "asian": ["east asian", "asian"],
                                "east asian": ["east asian", "asian"],
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

                        # Catch-all for checkbox groups: if parent label contains "select all"
                        # or looks like a multi-select question, check any generic safe option
                        if not should_check and parent_label:
                            if any(x in parent_label for x in ["select all", "check all", "choose all"]):
                                # For "select all that apply" questions, check the safest generic option
                                if any(x in label_lower for x in [
                                    "linkedin", "online", "job board", "other", "google",
                                    "career site", "website", "search engine", "social media",
                                ]):
                                    should_check = True
                                    reason = f"multi-select safe ({label_text[:20]})"

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
                    await self._get_keyboard(page).press("Escape")
                    return False

        except Exception as e:
            logger.debug(f"Error filling React-select for '{label_keyword}': {e}")
            try:
                await self._get_keyboard(page).press("Escape")
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
                    // Handles both standard IDs (school--0) and non-standard (education-7--school)
                    const selectors = [
                        'input[type="hidden"][id="{field_name}--0"]',
                        'input[type="hidden"][id$="--0"][id*="{field_name}"]',
                        'input[type="hidden"][id*="--{field_name}"]',
                        'input[type="hidden"][id*="education"][id*="{field_name}"]',
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

                    # For school, type to search (typeahead) — try progressively shorter terms
                    if field_name == "school":
                        # Try multiple search terms: full name → first 3 words → first 2 words → abbreviation
                        school_name = value.split(",")[0].strip()
                        words = school_name.split()
                        search_terms = [school_name[:20]]
                        if len(words) >= 3:
                            search_terms.append(" ".join(words[:3]))
                        if len(words) >= 2:
                            search_terms.append(" ".join(words[:2]))
                        # Add common abbreviation (e.g., "San Jose State University" → "SJSU")
                        if len(words) >= 2:
                            abbrev = "".join(w[0] for w in words if w[0].isupper())
                            if len(abbrev) >= 2:
                                search_terms.append(abbrev)

                        found_match = False
                        for search_term in search_terms:
                            await dropdown.click()
                            await self.browser_manager.human_delay(500, 800)
                            await self._get_keyboard(page).type(search_term, delay=50)
                            await self.browser_manager.human_delay(800, 1200)

                            found_match = await self.form_filler._select_dropdown_option(page, value)
                            if found_match:
                                logger.info(f"Selected school via search '{search_term}'")
                                break
                            await self._get_keyboard(page).press("Escape")
                            await self.browser_manager.human_delay(200, 300)

                        if not found_match:
                            # Last resort: type first term and press Enter for first result
                            await dropdown.click()
                            await self.browser_manager.human_delay(300, 500)
                            await self._get_keyboard(page).type(search_terms[0], delay=50)
                            await self.browser_manager.human_delay(500, 800)
                            await self._get_keyboard(page).press("Enter")
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
                                await self._get_keyboard(page).press("Escape")
                                await self.browser_manager.human_delay(200, 300)

                        if not found_match:
                            # Last resort: type and Enter for first match
                            await dropdown.click()
                            await self.browser_manager.human_delay(300, 500)
                            search_term = value[:15]
                            await self._get_keyboard(page).type(search_term, delay=50)
                            await self.browser_manager.human_delay(500, 800)
                            await self._get_keyboard(page).press("Enter")
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
                    await self._get_keyboard(page).press("Escape")
                except Exception:
                    pass

        # Fill graduation date fields (start/end month/year)
        await self._fill_graduation_date_fields(page, edu)

        # Final sync pass: ensure all education hidden inputs are populated
        for field_name, value, keywords, alternatives in education_fields:
            if value:
                await self._sync_education_hidden_input_robust(page, field_name, value)

    async def _fill_greenhouse_work_experience_fields(self, page: Page) -> None:
        """Fill Greenhouse work experience fields (company-name, title, dates).

        Greenhouse employment sections use IDs like:
        - company-name-0 (text input or hidden + React-Select)
        - title-0 (text input or hidden + React-Select)
        - start-date-month-0, start-date-year-0 (React-Select dropdowns)
        - end-date-month-0, end-date-year-0 (React-Select dropdowns)

        These are NOT question fields — they're standard employment section fields.
        """
        import re as _re_work

        experience = self.form_filler.config.get("experience", [])
        if isinstance(experience, list) and experience:
            exp = experience[0]  # Use first (most recent) work experience
        elif isinstance(experience, dict):
            exp = experience
        else:
            logger.debug("No work experience data configured, skipping work experience fields")
            return

        if not exp:
            logger.debug("No work experience data configured, skipping work experience fields")
            return

        company = exp.get("company", "")
        title = exp.get("title", "")
        start_date_str = exp.get("start_date", "")
        end_date_str = exp.get("end_date", "")
        is_current = exp.get("current", False)

        if not company and not title:
            logger.debug("Work experience has no company or title, skipping")
            return

        logger.info(f"Filling Greenhouse work experience: {company} - {title}")

        # Parse dates
        month_names = ["January", "February", "March", "April", "May", "June",
                       "July", "August", "September", "October", "November", "December"]

        def parse_date(date_str):
            if not date_str:
                return "", ""
            m = _re_work.search(r'(January|February|March|April|May|June|July|August|September|October|November|December)', date_str, _re_work.IGNORECASE)
            y = _re_work.search(r'20\d{2}', date_str)
            return (m.group(1).capitalize() if m else ""), (y.group() if y else "")

        start_month, start_year = parse_date(start_date_str)
        end_month, end_year = parse_date(end_date_str)

        # If current job and no end date, use "Present" or current date
        if is_current and not end_month:
            import datetime
            now = datetime.datetime.now()
            end_month = month_names[now.month - 1]
            end_year = str(now.year)

        # Ensure work experience section is expanded — click "Add Work Experience" if needed
        company_field = await page.query_selector('input#company-name-0, input[id*="company-name"]')
        if not company_field or not await company_field.is_visible():
            for add_sel in [
                'button:has-text("Add Work Experience")',
                'button:has-text("Add Employment")',
                'a:has-text("Add Work Experience")',
                'button:has-text("Add Another Employment")',
                '[data-qa="add-employment"]',
                'button[aria-label*="employment" i]',
                'button[aria-label*="work experience" i]',
            ]:
                try:
                    btn = await page.query_selector(add_sel)
                    if btn and await btn.is_visible():
                        await btn.click()
                        await asyncio.sleep(0.8)
                        logger.info(f"Clicked '{add_sel}' to expand work experience section")
                        break
                except Exception as e:
                    logger.debug(f"Work experience expand: '{add_sel}' failed: {e}")
                    continue

        # ── TEXT FIELDS: company-name-0, title-0 ──────────────────────
        text_fields = {
            "company-name": company,
            "title": title,
        }

        for field_key, value in text_fields.items():
            if not value:
                continue
            try:
                # Try multiple selector patterns for employment text fields
                selectors = [
                    f'input#{field_key}-0',
                    f'input[id="{field_key}-0"]',
                    f'input[id*="employment"][id*="{field_key}"]',
                    f'input[name*="{field_key}"]',
                    f'input[id*="{field_key}"][id*="-0"]',
                ]

                inp = None
                for sel in selectors:
                    try:
                        inp = await page.query_selector(sel)
                        if inp:
                            break
                    except Exception:
                        continue

                if not inp:
                    logger.debug(f"Work experience field '{field_key}' not found on page")
                    continue

                inp_type = await inp.get_attribute("type") or ""

                if inp_type == "hidden":
                    # Hidden input — may be React-Select, fill via JS
                    current_val = await inp.evaluate('(el) => el.value')
                    if current_val and current_val.strip():
                        logger.debug(f"Work experience hidden '{field_key}' already has value: {current_val}")
                        continue

                    # Try to find and fill associated React-Select dropdown
                    container = await inp.evaluate_handle('''(el) => {
                        let c = el.closest('.field, .select, [class*="field"]');
                        if (!c) c = el.parentElement?.parentElement?.parentElement;
                        return c;
                    }''')
                    container_elem = container.as_element() if container else None
                    if container_elem:
                        dropdown = await container_elem.query_selector('.select__control, [role="combobox"]')
                        if dropdown and await dropdown.is_visible():
                            await dropdown.click()
                            await self.browser_manager.human_delay(400, 600)
                            await self._get_keyboard(page).type(value[:20], delay=50)
                            await self.browser_manager.human_delay(500, 800)
                            option = await page.query_selector('.select__option:first-child, [role="option"]:first-child')
                            if option and await option.is_visible():
                                await option.click()
                                logger.info(f"Filled work experience React-Select '{field_key}' = {value}")
                            else:
                                await self._get_keyboard(page).press("Enter")
                                logger.info(f"Filled work experience React-Select '{field_key}' via Enter = {value}")
                            await self.browser_manager.human_delay(300, 500)
                            continue

                    # Force-set hidden input as last resort
                    safe_val = value.replace("\\", "\\\\").replace('"', '\\"').replace('\n', ' ')
                    await inp.evaluate(f'''(el) => {{
                        const nativeSetter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value'
                        ).set;
                        nativeSetter.call(el, "{safe_val}");
                        el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    }}''')
                    logger.info(f"Force-set work experience hidden '{field_key}' = {value}")

                else:
                    # Visible text input — fill normally
                    current_val = await inp.input_value()
                    if current_val and current_val.strip():
                        logger.debug(f"Work experience text '{field_key}' already has value: {current_val}")
                        continue

                    await inp.click()
                    await self.browser_manager.human_delay(100, 200)
                    await inp.fill(value)
                    # Trigger React events
                    await inp.evaluate('''(el) => {
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        el.dispatchEvent(new Event('blur', { bubbles: true }));
                    }''')
                    logger.info(f"Filled work experience text '{field_key}' = {value}")
                    await self.browser_manager.human_delay(200, 400)

            except Exception as e:
                logger.debug(f"Error filling work experience field '{field_key}': {e}")

        # ── DATE FIELDS: start-date-month-0, end-date-month-0, etc. ───
        date_fields = []
        if start_month:
            date_fields.append(("start-date-month", start_month))
        if start_year:
            date_fields.append(("start-date-year", start_year))
        if end_month:
            date_fields.append(("end-date-month", end_month))
        if end_year:
            date_fields.append(("end-date-year", end_year))

        for field_id_base, value in date_fields:
            try:
                # Try to find the hidden input first
                selectors = [
                    f'input#{field_id_base}-0',
                    f'input[id="{field_id_base}-0"]',
                    f'input[id*="employment"][id*="{field_id_base}"]',
                    f'input[id*="{field_id_base}"][id*="-0"]',
                ]

                inp = None
                for sel in selectors:
                    try:
                        inp = await page.query_selector(sel)
                        if inp:
                            break
                    except Exception:
                        continue

                if inp:
                    current_val = await inp.evaluate('(el) => el.value')
                    if current_val and current_val.strip():
                        logger.debug(f"Work experience date '{field_id_base}' already has value: {current_val}")
                        continue

                    # Force-set hidden input value
                    safe_val = value.replace("\\", "\\\\").replace('"', '\\"')
                    await inp.evaluate(f'''(el) => {{
                        const nativeSetter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value'
                        ).set;
                        nativeSetter.call(el, "{safe_val}");
                        el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    }}''')
                    logger.info(f"Set work experience date '{field_id_base}' = {value}")
                    continue

                # If no hidden input, try React-Select dropdowns by label
                dropdowns = await page.query_selector_all('.select__control, [role="combobox"]')
                # Determine what label keywords to match
                # e.g., "start-date-month" → look for "start" + "month" in label
                parts = field_id_base.split("-")  # ["start", "date", "month"] or ["end", "date", "year"]
                date_direction = parts[0]  # "start" or "end"
                date_unit = parts[-1]  # "month" or "year"

                for dropdown in dropdowns:
                    if not await dropdown.is_visible():
                        continue

                    label_text = await dropdown.evaluate('''(el) => {
                        let container = el.closest('.field, .select, [class*="field"]');
                        if (!container) container = el.parentElement?.parentElement?.parentElement;
                        if (!container) return "";
                        let label = container.querySelector("label, .select__label, [class*='label']");
                        let text = label ? label.textContent.toLowerCase().trim() : "";
                        // Also check the container's ID for employment context
                        let cid = container.id || container.getAttribute("data-field") || "";
                        return text + " " + cid.toLowerCase();
                    }''')

                    # Must match both direction (start/end) and unit (month/year)
                    # AND should be in employment context (not education)
                    is_employment = any(x in label_text for x in ["employ", "company", "work", "job", "experience"])
                    is_direction = date_direction in label_text
                    is_unit = date_unit in label_text

                    # Also check by container ID matching employment section
                    container_id = await dropdown.evaluate('''(el) => {
                        let c = el.closest('[id*="employment"], [id*="work"], [data-field*="employment"]');
                        return c ? c.id || "" : "";
                    }''')
                    if container_id:
                        is_employment = True

                    if is_direction and is_unit and is_employment:
                        # Check if already filled
                        display_value = await dropdown.evaluate('''(el) => {
                            const sv = el.querySelector('.select__single-value, [class*="singleValue"]');
                            if (sv && sv.textContent && sv.textContent.trim() !== "Select..." && sv.textContent.trim().length > 1) {
                                return sv.textContent.trim();
                            }
                            return "";
                        }''')

                        if display_value:
                            logger.debug(f"Work experience date dropdown '{field_id_base}' already shows: {display_value}")
                            break

                        await dropdown.click()
                        await self.browser_manager.human_delay(400, 600)
                        found = await self.form_filler._select_dropdown_option(page, value)
                        if found:
                            logger.info(f"Selected work experience date '{field_id_base}' = {value}")
                        else:
                            await self._get_keyboard(page).type(value[:10], delay=50)
                            await self.browser_manager.human_delay(300, 500)
                            await self._get_keyboard(page).press("Enter")
                            logger.info(f"Typed work experience date '{field_id_base}' = {value}")
                        await self.browser_manager.human_delay(300, 500)
                        break

            except Exception as e:
                logger.debug(f"Error filling work experience date '{field_id_base}': {e}")

        # ── CURRENT EMPLOYMENT CHECKBOX ───────────────────────────────
        if is_current:
            try:
                current_cb = await page.query_selector(
                    'input[type="checkbox"][id*="current"], '
                    'input[type="checkbox"][name*="current"], '
                    'input[type="checkbox"][id*="currently"]'
                )
                if current_cb and not await current_cb.is_checked():
                    await current_cb.click()
                    logger.info("Checked 'currently work here' checkbox")
                    await self.browser_manager.human_delay(200, 400)
            except Exception as e:
                logger.debug(f"Error checking current employment checkbox: {e}")

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
                await self._get_keyboard(page).type(country[:15], delay=50)
                await self.browser_manager.human_delay(600, 900)

                found = await self.form_filler._select_dropdown_option(page, country)
                if not found:
                    await self._get_keyboard(page).press("Enter")
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
                                                await self._get_keyboard(page).press("Escape")
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
                        await self._get_keyboard(page).type(value[:10], delay=50)
                        await page.wait_for_timeout(300)
                        await self._get_keyboard(page).press("Enter")
                        found = True
                        logger.info(f"Selected {field_name} via typed search: {value}")

                    if not found:
                        await self._get_keyboard(page).press("Escape")
                    else:
                        logger.info(f"Selected {field_name}: {value}")

                    await self.browser_manager.human_delay(300, 500)
                    break

            except Exception as e:
                logger.debug(f"Error filling date dropdown: {e}")
                try:
                    await self._get_keyboard(page).press("Escape")
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
                await self._get_keyboard(page).press("Control+a")
                await self._get_keyboard(page).press("Backspace")

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
                    await self._get_keyboard(page).press("ArrowDown")
                    await self.browser_manager.human_delay(200, 400)
                    await self._get_keyboard(page).press("Enter")
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

                # Check if this is a location field (strict matching to avoid false positives)
                import re as _re_loc
                is_location_label = False
                if _re_loc.search(r'\blocation\b', label_text):
                    is_location_label = True
                elif _re_loc.search(r'\bwhere\b.{0,20}\b(located|based|live|reside)\b', label_text):
                    is_location_label = True
                elif _re_loc.search(r'\bcity\b', label_text) and 'ethni' not in label_text:
                    is_location_label = True
                # Exclude known non-location fields
                if any(x in label_text for x in ["ethnicity", "gender", "race", "disability", "veteran",
                                                   "degree", "education", "sponsor", "authorized", "right to work"]):
                    is_location_label = False
                if not is_location_label:
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
                await self._get_keyboard(page).type(search_term, delay=50)
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
                        await self._get_keyboard(page).press("ArrowDown")
                        await page.wait_for_timeout(50)
                    await self._get_keyboard(page).press("Enter")
                    await self.browser_manager.human_delay(300, 500)
                    logger.info(f"Selected location from dropdown: {visible_options[best_idx][1]}")
                    return True
                else:
                    # No results with city, try state
                    # Clear and retype
                    await self._get_keyboard(page).press("Control+a")
                    await self._get_keyboard(page).press("Backspace")
                    await page.wait_for_timeout(200)

                    personal = self.form_filler.config.get("personal_info", {})
                    state = personal.get("state", "California")
                    await self._get_keyboard(page).type(state, delay=50)
                    await self.browser_manager.human_delay(800, 1200)

                    # Try first result
                    await self._get_keyboard(page).press("ArrowDown")
                    await page.wait_for_timeout(100)
                    await self._get_keyboard(page).press("Enter")
                    await self.browser_manager.human_delay(300, 500)

                    # Verify
                    new_text = (await dropdown.text_content() or "").strip()
                    if new_text and "select" not in new_text.lower():
                        logger.info(f"Selected location from dropdown with state search: {new_text}")
                        return True

                # Close dropdown
                await self._get_keyboard(page).press("Escape")

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
            # PHASE 1: Force-fill education fields by ID (school--0, degree--0, etc.)
            # Also handles non-standard IDs like education-7--school, education-9--degree
            # education is a list in config — grab first entry
            edu_raw = self.form_filler.config.get("education", {})
            edu = edu_raw[0] if isinstance(edu_raw, list) and edu_raw else (edu_raw if isinstance(edu_raw, dict) else {})
            edu_fields = {
                "school": edu.get("school", "San Jose State University"),
                "degree": edu.get("degree", "Bachelor's Degree"),
                "discipline": edu.get("major", edu.get("field_of_study", "Software Engineering")),
            }
            edu_injected = 0
            for field_key, value in edu_fields.items():
                try:
                    # Try standard ID first, then wildcard for non-standard IDs
                    inp = await page.query_selector(f'input#{field_key}--0')
                    if not inp:
                        inp = await page.query_selector(f'input[id*="--{field_key}"]')
                    if not inp:
                        inp = await page.query_selector(f'input[id*="education"][id*="{field_key}"]')
                    if inp:
                        current_val = await inp.input_value()
                        if not current_val or current_val.strip() == "":
                            # Also check if React-Select shows a display value
                            display_val = await inp.evaluate('''(el) => {
                                let container = el.closest('.field, .select, [class*="field"], [class*="education"]');
                                if (!container) container = el.parentElement?.parentElement?.parentElement;
                                if (!container) return null;
                                const sv = container.querySelector('.select__single-value, [class*="singleValue"]');
                                return sv ? sv.textContent.trim() : null;
                            }''')
                            inject_val = display_val if display_val and len(display_val) > 2 else value
                            actual_id = await inp.get_attribute("id") or field_key
                            await inp.evaluate('''(el, val) => {
                                const nativeSetter = Object.getOwnPropertyDescriptor(
                                    window.HTMLInputElement.prototype, 'value'
                                ).set;
                                nativeSetter.call(el, val);
                                el.dispatchEvent(new Event('input', { bubbles: true }));
                                el.dispatchEvent(new Event('change', { bubbles: true }));
                                // Also trigger React's onChange via fiber if available
                                const reactKey = Object.keys(el).find(k => k.startsWith('__reactFiber$') || k.startsWith('__reactInternalInstance$'));
                                if (reactKey) {
                                    let fiber = el[reactKey];
                                    while (fiber) {
                                        if (fiber.memoizedProps && fiber.memoizedProps.onChange) {
                                            fiber.memoizedProps.onChange({ target: { value: val } });
                                            break;
                                        }
                                        fiber = fiber.return;
                                    }
                                }
                            }''', inject_val)
                            logger.info(f"PRE-SUBMIT INJECT: Force-filled {actual_id} = {inject_val[:50]}")
                            edu_injected += 1
                except Exception as e:
                    logger.debug(f"PRE-SUBMIT INJECT: Failed to force-fill {field_key}: {e}")

            # Also handle date fields
            grad_date = edu.get("graduation_date", "May 2026")
            import re as _re_inject
            year_match = _re_inject.search(r'20\d{2}', grad_date)
            month_match = _re_inject.search(r'(January|February|March|April|May|June|July|August|September|October|November|December)', grad_date, _re_inject.IGNORECASE)
            date_fields = {
                "end-year--0": year_match.group() if year_match else "2026",
                "end-month--0": month_match.group(1).capitalize() if month_match else "May",
                "start-year--0": str(int((year_match.group() if year_match else "2026")) - 4),
                "start-month--0": "August",
            }
            for field_id, value in date_fields.items():
                try:
                    inp = await page.query_selector(f'input#{field_id}')
                    # Also try non-standard IDs (e.g., education-7--end-year)
                    if not inp:
                        base_name = field_id.replace("--0", "")
                        inp = await page.query_selector(f'input[id*="--{base_name}"]')
                    if not inp:
                        base_name = field_id.replace("--0", "")
                        inp = await page.query_selector(f'input[id*="education"][id*="{base_name}"]')
                    if inp:
                        current_val = await inp.input_value()
                        if not current_val or current_val.strip() == "":
                            await inp.evaluate('''(el, val) => {
                                const nativeSetter = Object.getOwnPropertyDescriptor(
                                    window.HTMLInputElement.prototype, 'value'
                                ).set;
                                nativeSetter.call(el, val);
                                el.dispatchEvent(new Event('input', { bubbles: true }));
                                el.dispatchEvent(new Event('change', { bubbles: true }));
                            }''', value)
                            actual_id = await inp.get_attribute("id") or field_id
                            logger.info(f"PRE-SUBMIT INJECT: Force-filled date {actual_id} = {value}")
                            edu_injected += 1
                except Exception as e:
                    logger.debug(f"PRE-SUBMIT INJECT: Failed to force-fill date {field_id}: {e}")

            if edu_injected:
                logger.info(f"PRE-SUBMIT INJECT: Force-filled {edu_injected} education/date fields")

            # PHASE 2: Generic React-Select injection for other dropdowns
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

                    // Find the container — try wide first (application-question) then narrow
                    let container = ctrl.closest('.application-question, [class*="question"], .field, .select, [class*="field"]');
                    if (!container) container = ctrl.parentElement?.parentElement?.parentElement?.parentElement;
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

            # PHASE 3: Direct scan for question_* empty text inputs — find nearest React-Select display value
            # Catches inputs Phase 2 missed (different DOM structure / no .select__control ancestor)
            phase3_injected = await page.evaluate('''() => {
                const results = [];
                const desc = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value");
                if (!desc || !desc.set) return results;
                const nativeSetter = desc.set;
                const questionInputs = document.querySelectorAll('input[id^="question_"]');
                for (const inp of questionInputs) {
                    if (inp.type === "hidden") continue;
                    if (inp.getAttribute("role") === "combobox") continue;
                    if (inp.id.includes("react-select")) continue;
                    const val = inp.value ? inp.value.trim() : "";
                    if (val !== "") continue; // already filled (possibly by Phase 2)
                    let el = inp.parentElement;
                    for (let k = 0; k < 6 && el; k++) {
                        const sv = el.querySelector(".select__single-value, [class*=singleValue]");
                        if (sv) {
                            const trimmed = sv.textContent.trim();
                            if (trimmed && trimmed !== "Select...") {
                                nativeSetter.call(inp, trimmed);
                                inp.dispatchEvent(new Event("input", { bubbles: true }));
                                inp.dispatchEvent(new Event("change", { bubbles: true }));
                                results.push({ inputId: inp.id, value: trimmed });
                                break;
                            }
                        }
                        el = el.parentElement;
                    }
                }
                return results;
            }''')
            for item in (phase3_injected or []):
                logger.info(f"PRE-SUBMIT INJECT P3: Set {item['inputId']} = {item['value']}")

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

            # Retry work experience fields if company/title/dates are empty
            work_fields = ["company-name", "title-0", "start-date", "end-date"]
            if any(any(wf in f.lower() for wf in work_fields) for f in empty_req):
                logger.info("PRE-SUBMIT: Retrying work experience fields...")
                await self._fill_greenhouse_work_experience_fields(page)

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

        # ── GOLDEN PATH GUARD: final required-field check before submit ──
        empty_required = await self._check_required_fields_before_submit(page)
        if empty_required:
            logger.warning(
                f"GOLDEN PATH GUARD: {len(empty_required)} required field(s) still empty — "
                f"NOT submitting, leaving tab open: {empty_required}"
            )
            return False

        submit_selectors = [
            'button[type="submit"]',
            'input[type="submit"]',
            'button:has-text("Submit")',
            'button:has-text("Submit Application")',
            'button:has-text("Submit application")',
            'button:has-text("Apply")',
            '#submit_app',
            '.submit-button',
            'a:has-text("Submit")',
            'button:has-text("Send Application")',
            'button:has-text("Send application")',
            '[data-action="submit"]',
            'button[id*="submit"]',
            'button[class*="submit"]',
        ]

        for selector in submit_selectors:
            try:
                btn = await page.query_selector(selector)
                if btn and await btn.is_visible():
                    # Human-like delay before submit to avoid bot detection
                    await asyncio.sleep(random.uniform(2.0, 5.0))
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
                        logger.info("Verification code entered — re-solving reCAPTCHA before re-submitting")
                        await self.browser_manager.human_delay(500, 1000)
                        # Re-solve reCAPTCHA (token consumed by first submit)
                        # solve_invisible_recaptcha already no-ops when no CAPTCHA is present
                        captcha_ok = await self.solve_invisible_recaptcha(page)
                        if captcha_ok:
                            logger.info("Re-solved reCAPTCHA after verification code")
                        else:
                            logger.debug("reCAPTCHA re-solve returned False (may not be needed)")
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

        # Fallback: find ANY visible button at the bottom that looks like submit
        try:
            all_buttons = await page.query_selector_all('button, input[type="submit"], a[role="button"]')
            for btn in reversed(all_buttons):  # Start from bottom
                if not await btn.is_visible():
                    continue
                text = (await btn.text_content() or "").strip().lower()
                if any(w in text for w in ["submit", "apply", "send", "continue"]):
                    # Human-like delay before fallback submit
                    await asyncio.sleep(random.uniform(2.0, 5.0))
                    await self.browser_manager.human_delay(500, 1000)
                    await btn.scroll_into_view_if_needed()
                    await btn.click()
                    logger.info(f"Clicked fallback submit button: '{text}'")
                    await self.browser_manager.human_delay(2000, 3000)
                    # Click again (user requested double-click submit)
                    try:
                        if await btn.is_visible():
                            await btn.click()
                            logger.info("Double-clicked submit")
                            await self.browser_manager.human_delay(2000, 3000)
                    except Exception:
                        pass
                    return True
        except Exception as e:
            logger.debug(f"Fallback submit search failed: {e}")

        # Check if Simplify already submitted (page shows "Thank you for applying")
        try:
            body_text = await page.text_content("body") or ""
            body_lower = body_text.lower()
            _context_keywords = {"application", "submitted", "applied", "received", "candidacy"}
            _thank_you_match = (
                "thank you for applying" in body_lower
                and any(kw in body_lower for kw in _context_keywords)
            )
            if _thank_you_match or "application has been received" in body_lower:
                logger.info("Simplify already submitted the application! (detected 'Thank you for applying')")
                self._last_status = "success"
                return True
        except Exception:
            pass

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

            # Strategy 3: Find any visible text input in a modal/dialog/overlay
            modal_selectors = [
                '[role="dialog"] input[type="text"]',
                '.modal input[type="text"]',
                '[class*="modal"] input[type="text"]',
                '[class*="overlay"] input[type="text"]',
                '[class*="verification"] input',
                '[class*="confirm"] input[type="text"]',
            ]
            for sel in modal_selectors:
                try:
                    inp = await page.query_selector(sel)
                    if inp and await inp.is_visible():
                        await inp.fill(code)
                        logger.info(f"Entered verification code in modal input: {code}")
                        await self.browser_manager.human_delay(500, 1000)
                        return True
                except Exception:
                    continue

            # Strategy 4: Find ANY visible empty text input (last resort — the modal may focus it)
            all_text_inputs = await page.query_selector_all('input[type="text"]:not([type="hidden"])')
            for inp in all_text_inputs:
                try:
                    if await inp.is_visible():
                        val = await inp.input_value()
                        if not val or not val.strip():
                            await inp.fill(code)
                            logger.info(f"Entered verification code in empty text input: {code}")
                            await self.browser_manager.human_delay(500, 1000)
                            return True
                except Exception:
                    continue

            # Strategy 5: Just type it — the page might have focused the input already
            try:
                await self._get_keyboard(page).type(code, delay=100)
                logger.info(f"Typed verification code via keyboard: {code}")
                await self.browser_manager.human_delay(500, 1000)
                # Press Enter to submit
                await self._get_keyboard(page).press("Enter")
                await self.browser_manager.human_delay(2000, 3000)
                return True
            except Exception:
                pass

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
                        await self._get_keyboard(page).press("Tab")
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
