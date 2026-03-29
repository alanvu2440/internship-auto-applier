"""
Lever Handler

Handles job applications on Lever ATS.
URLs: jobs.lever.co
"""

import asyncio
import re
from typing import Dict, Any
from playwright.async_api import Page
from loguru import logger

from .base import BaseHandler


class LeverHandler(BaseHandler):
    """Handler for Lever ATS applications."""

    name = "lever"

    async def apply(self, page: Page, job_url: str, job_data: Dict[str, Any]) -> bool:
        """Apply to a Lever job."""
        self._last_status = "failed"
        try:
            logger.info(f"Applying to Lever job: {job_data.get('company')} - {job_data.get('role')}")

            # Always navigate directly to /apply URL (skips needing to click Apply button)
            apply_url = job_url if job_url.rstrip('/').endswith('/apply') else job_url.rstrip('/') + '/apply'

            # Navigate to application form
            try:
                await page.goto(apply_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                logger.warning(f"Page load issue: {e}")
                await page.goto(apply_url, wait_until="commit", timeout=15000)

            await self.browser_manager.human_delay(1000, 2000)

            # Check if job is closed
            if await self.is_job_closed(page):
                logger.info("Job is closed/unavailable")
                self._last_status = "closed"
                return False

            # Dismiss any popups
            await self.dismiss_popups(page)

            # We navigated directly to /apply — check form is present
            form_present = await page.query_selector(
                'input[name="name"], input[name="email"], '
                '.application-form, form[action*="lever"], '
                '#application-form, .postings-application-form, '
                'input[placeholder*="name"], input[placeholder*="email"], '
                '.application-page, .lever-application-page'
            )
            if not form_present:
                # Wait a bit more and retry
                await page.wait_for_timeout(3000)
                form_present = await page.query_selector('input, textarea, select')
            if not form_present:
                # Last resort: try clicking Apply button if page redirected to job listing
                apply_clicked = await self._click_apply_button(page)
                if not apply_clicked:
                    logger.warning("Could not find Lever application form")
                    return False

            await self.browser_manager.human_delay(1500, 2500)

            # Let Simplify extension autofill boilerplate if loaded
            ext_filled = await self.wait_for_extension_autofill(page)
            if ext_filled:
                logger.info("Simplify extension pre-filled fields — handler will fill remaining gaps")

            # Check for CAPTCHA
            if not await self.handle_captcha(page):
                return False

            # Guard: check page still open after Simplify/CAPTCHA
            if page.is_closed():
                logger.warning("Page closed after Simplify/CAPTCHA — job may be expired")
                return False

            # Fill the application form
            success = await self._fill_application(page, job_data)
            if not success:
                return False

            # Dry-run mode: validate without submitting
            if self.dry_run:
                logger.info("DRY RUN: Lever form filled, running validation")
                validation_result = await self._run_dry_run_validation(page)
                self._last_status = "success" if validation_result else "failed"
                return validation_result

            # Submit
            await self._submit_application(page)

            # Check if successful
            await self.browser_manager.human_delay(2000, 3000)
            if await self.is_application_complete(page):
                logger.info("Lever application submitted successfully!")
                self._last_status = "success"
                return True

            error = await self.get_error_message(page)
            if error:
                logger.error(f"Lever application error: {error}")

            return False

        except Exception as e:
            logger.error(f"Lever application failed: {e}")
            await self.take_screenshot(page, f"lever_error_{job_data.get('company', 'unknown')}")
            return False

    async def detect_form_type(self, page: Page) -> str:
        """Detect Lever form type."""
        # Lever typically has a modal or separate page for applications
        modal = await page.query_selector('.application-modal, .postings-btn-wrapper')
        if modal:
            return "modal"

        # Check for direct application form
        form = await page.query_selector('form.application-form, form[action*="lever"]')
        if form:
            return "direct"

        return "standard"

    async def _click_apply_button(self, page: Page) -> bool:
        """Click the Apply button to open application form."""
        apply_selectors = [
            'a.postings-btn[href*="apply"]',
            'a:has-text("Apply for this job")',
            'a:has-text("Apply now")',
            'button:has-text("Apply")',
            '.apply-button',
            'a.apply',
        ]

        for selector in apply_selectors:
            try:
                btn = await page.query_selector(selector)
                if btn and await btn.is_visible():
                    await btn.click()
                    return True
            except Exception:
                continue

        # Try clicking any prominent apply button
        try:
            await page.click('text=Apply', timeout=5000)
            return True
        except Exception:
            pass

        return False

    async def _fill_application(self, page: Page, job_data: Dict[str, Any]) -> bool:
        """Fill the Lever application form."""
        try:
            config = self.form_filler.config
            personal = config.get("personal_info", {})

            # Wait for form to load + Simplify extension to autofill
            await page.wait_for_selector('input[name="name"], input[name="email"]', timeout=10000)
            await self.browser_manager.human_delay(2000, 3000)  # Let extension fill first

            # Helper: only fill if empty (don't overwrite extension autofill)
            async def _fill_if_empty(element, value, label="field"):
                if not element or not value:
                    return
                try:
                    current = await element.input_value()
                    if current and current.strip():
                        logger.info(f"Skipping {label} — already filled: '{current[:30]}'")
                        return
                    await element.fill(str(value))
                    await self.browser_manager.human_delay(200, 400)
                except Exception:
                    await element.fill(str(value))

            # Full name (Lever often uses single name field)
            name_input = await page.query_selector('input[name="name"]')
            full_name = f"{personal.get('first_name', '')} {personal.get('last_name', '')}".strip()
            await _fill_if_empty(name_input, full_name, "name")

            # Email
            email_input = await page.query_selector('input[name="email"]')
            await _fill_if_empty(email_input, personal.get("email", ""), "email")

            # Phone
            phone_input = await page.query_selector('input[name="phone"]')
            phone = re.sub(r'[\s\-\(\)]', '', f"{personal.get('phone_prefix', '')}{personal.get('phone', '')}").replace("+", "")
            await _fill_if_empty(phone_input, phone, "phone")

            # Current company (optional)
            company_input = await page.query_selector('input[name="org"]')
            if company_input:
                exp = config.get("experience", [])
                if exp:
                    await company_input.fill(exp[0].get("company", ""))

            # LinkedIn
            linkedin_selectors = [
                'input[name*="linkedin"]',
                'input[name*="urls[LinkedIn]"]',
                'input[name*="LinkedIn"]',
                'input[placeholder*="linkedin.com"]',
            ]
            for selector in linkedin_selectors:
                linkedin_input = await page.query_selector(selector)
                if linkedin_input and await linkedin_input.is_visible():
                    await linkedin_input.fill(personal.get("linkedin", ""))
                    await self.browser_manager.human_delay(200, 400)
                    break

            # GitHub
            github_selectors = [
                'input[name*="github"]',
                'input[name*="urls[GitHub]"]',
                'input[name*="GitHub"]',
                'input[placeholder*="github.com"]',
            ]
            for selector in github_selectors:
                github_input = await page.query_selector(selector)
                if github_input and await github_input.is_visible():
                    await github_input.fill(personal.get("github", ""))
                    await self.browser_manager.human_delay(200, 400)
                    break

            # Portfolio/Website
            portfolio_selectors = [
                'input[name*="portfolio"]',
                'input[name*="urls[Portfolio]"]',
                'input[name*="website"]',
                'input[name*="urls[Website]"]',
                'input[placeholder*="website"]',
            ]
            for selector in portfolio_selectors:
                portfolio_input = await page.query_selector(selector)
                if portfolio_input and await portfolio_input.is_visible():
                    await portfolio_input.fill(personal.get("portfolio", ""))
                    await self.browser_manager.human_delay(200, 400)
                    break

            # Upload resume
            resume_path = config.get("files", {}).get("resume")
            if resume_path:
                await self._upload_resume(page, resume_path)

            # Fill education fields
            await self._fill_education_fields(page, config)

            # Handle additional questions
            await self._handle_additional_questions(page, job_data)

            # Handle EEO questions
            await self._handle_eeo_questions(page)

            return True

        except Exception as e:
            logger.error(f"Error filling Lever form: {e}")
            return False

    async def _upload_resume(self, page: Page, resume_path: str) -> bool:
        """Upload resume to Lever."""
        try:
            # Lever resume upload input
            file_input = await page.query_selector(
                'input[type="file"][name="resume"], '
                'input[type="file"].resume-upload, '
                'input[type="file"]'
            )

            if file_input:
                await file_input.set_input_files(resume_path)
                logger.info("Resume uploaded to Lever")
                await self.browser_manager.human_delay(1500, 2500)
                return True

            # Try the upload button approach
            upload_area = await page.query_selector('.resume-upload-area, .upload-area')
            if upload_area:
                async with page.expect_file_chooser() as fc_info:
                    await upload_area.click()
                file_chooser = await fc_info.value
                await file_chooser.set_files(resume_path)
                logger.info("Resume uploaded via upload area")
                return True

        except Exception as e:
            logger.warning(f"Could not upload resume to Lever: {e}")

        return False

    async def _handle_additional_questions(self, page: Page, job_data: Dict[str, Any]) -> None:
        """Handle Lever's additional/custom questions."""
        # Find all question cards
        question_cards = await page.query_selector_all(
            '.application-question, '
            '.custom-question, '
            'div[class*="question"]'
        )

        # Patterns to skip — these are handled by dedicated methods
        skip_patterns = [
            "name", "email", "phone", "resume",
            # EEO / demographic fields — handled by _handle_eeo_questions
            "gender", "race", "ethnicity", "veteran", "disability",
            "demographic", "equal employment", "eeo",
        ]

        for card in question_cards:
            try:
                # Get question text — use only the direct text of the label,
                # not descendant text (which includes select option labels)
                question_elem = await card.query_selector('label, .question-label, .question-text')
                if not question_elem:
                    continue

                # Extract only the direct text nodes of the label element,
                # excluding text from child elements like <select>, <input>, etc.
                question_text = await question_elem.evaluate("""el => {
                    let text = '';
                    for (const node of el.childNodes) {
                        if (node.nodeType === Node.TEXT_NODE) {
                            text += node.textContent;
                        }
                    }
                    return text.trim();
                }""")

                # Fallback: if direct text is empty, use full text_content but
                # strip out anything after "Select" which is typically option text
                if not question_text:
                    full_text = (await question_elem.text_content() or "").strip()
                    # Remove option text that gets concatenated (e.g. "GenderSelect ...MaleFemale...")
                    question_text = re.split(r'Select(?:\.\.\.|…)', full_text)[0].strip()

                if not question_text:
                    continue

                # Skip if already handled (name, email, etc.) or is an EEO field
                if any(skip in question_text.lower() for skip in skip_patterns):
                    continue

                # Find text/select/textarea inputs
                input_elem = await card.query_selector(
                    'input:not([type="hidden"]):not([type="file"]):not([type="checkbox"]):not([type="radio"]), '
                    'textarea, select'
                )

                # Find radio buttons
                radio_elems = await card.query_selector_all('input[type="radio"]')

                # Find checkboxes (non-consent)
                checkbox_elems = await card.query_selector_all('input[type="checkbox"]')

                if input_elem:
                    tag = await input_elem.evaluate("el => el.tagName.toLowerCase()")
                    input_type = await input_elem.get_attribute("type") or "text"

                    if tag == "select":
                        options = []
                        option_elems = await input_elem.query_selector_all("option")
                        for opt in option_elems:
                            text = await opt.text_content()
                            if text and text.strip() and text.strip() not in ("Select...", "Select", "Choose..."):
                                options.append(text.strip())

                        if options:
                            answer = await self.ai_answerer.answer_question(
                                question_text, "select", options
                            )
                            try:
                                await asyncio.wait_for(
                                    input_elem.select_option(label=answer),
                                    timeout=5
                                )
                            except (asyncio.TimeoutError, Exception):
                                # Try partial match
                                for opt in options:
                                    if answer.lower() in opt.lower():
                                        try:
                                            await asyncio.wait_for(
                                                input_elem.select_option(label=opt),
                                                timeout=5
                                            )
                                        except (asyncio.TimeoutError, Exception):
                                            pass
                                        break

                    elif tag == "textarea":
                        answer = await self.ai_answerer.answer_question(
                            question_text, "textarea", max_length=1000
                        )
                        await input_elem.fill(answer)

                    else:
                        # Text input
                        answer = await self.ai_answerer.answer_question(
                            question_text, "text", max_length=200
                        )
                        await input_elem.fill(answer)

                elif radio_elems:
                    # Handle radio button questions
                    options = []
                    for radio in radio_elems:
                        radio_id = await radio.get_attribute("id")
                        label = None
                        if radio_id:
                            label = await card.query_selector(f'label[for="{radio_id}"]')
                        if not label:
                            label = await radio.evaluate_handle(
                                "el => el.closest('label') || el.parentElement"
                            )
                            label_text = await label.evaluate("el => el.textContent")
                        else:
                            label_text = await label.text_content()
                        text = (label_text or "").strip()
                        if text:
                            options.append((text, radio))

                    if options:
                        option_texts = [o[0] for o in options]
                        answer = await self.ai_answerer.answer_question(
                            question_text, "select", option_texts
                        )
                        clicked = False
                        for text, radio in options:
                            if answer.lower() in text.lower() or text.lower() in answer.lower():
                                await radio.click()
                                clicked = True
                                break
                        if not clicked and options:
                            await options[0][1].click()

                elif checkbox_elems:
                    # Handle checkbox questions
                    for checkbox in checkbox_elems:
                        checkbox_id = await checkbox.get_attribute("id")
                        label = None
                        if checkbox_id:
                            label = await card.query_selector(f'label[for="{checkbox_id}"]')
                        if not label:
                            label = await checkbox.evaluate_handle(
                                "el => el.closest('label') || el.parentElement"
                            )
                        label_text = ""
                        if label:
                            label_text = (await label.evaluate("el => el.textContent") or "").strip()

                        full_question = f"{question_text}: {label_text}" if label_text else question_text
                        answer = await self.ai_answerer.answer_question(
                            full_question, "select", ["Yes", "No"]
                        )
                        if answer.lower() in ("yes", "true"):
                            if not await checkbox.is_checked():
                                await checkbox.click()
                else:
                    continue

                await self.browser_manager.human_delay(200, 500)

            except Exception as e:
                logger.debug(f"Error handling Lever question: {e}")

    async def _handle_eeo_questions(self, page: Page) -> None:
        """Handle EEO/demographic questions."""
        demographics = self.form_filler.config.get("demographics", {})

        async def safe_select(selector: str, value: str):
            """Select an option with a 5s timeout to avoid hanging."""
            if not value:
                return
            try:
                elem = await page.query_selector(selector)
                if elem and await elem.is_visible():
                    await asyncio.wait_for(
                        elem.select_option(label=value),
                        timeout=5
                    )
            except (asyncio.TimeoutError, Exception):
                pass

        await safe_select('select[name*="gender"]', demographics.get("gender", ""))
        await safe_select('select[name*="race"], select[name*="ethnicity"]', demographics.get("ethnicity", ""))
        await safe_select('select[name*="veteran"]', demographics.get("veteran_status", ""))
        await safe_select('select[name*="disability"]', demographics.get("disability_status", ""))

    async def _fill_education_fields(self, page: Page, config: Dict[str, Any]) -> None:
        """Fill education-related fields (school, degree, field of study, GPA)."""
        education = config.get("education", [])
        if not education:
            return

        edu = education[0]  # Use primary education entry

        # School / University
        school_selectors = [
            'input[name*="school"]', 'input[name*="university"]',
            'input[name*="institution"]', 'input[name*="college"]',
            'input[placeholder*="School"]', 'input[placeholder*="University"]',
            'select[name*="school"]', 'select[name*="university"]',
        ]
        for selector in school_selectors:
            try:
                elem = await page.query_selector(selector)
                if elem and await elem.is_visible():
                    tag = await elem.evaluate("el => el.tagName.toLowerCase()")
                    if tag == "select":
                        try:
                            await elem.select_option(label=edu.get("school", ""))
                        except Exception:
                            pass
                    else:
                        await elem.fill(edu.get("school", ""))
                    await self.browser_manager.human_delay(200, 400)
                    break
            except Exception:
                continue

        # Degree
        degree_selectors = [
            'input[name*="degree"]', 'select[name*="degree"]',
            'input[placeholder*="Degree"]', 'select[id*="degree"]',
        ]
        for selector in degree_selectors:
            try:
                elem = await page.query_selector(selector)
                if elem and await elem.is_visible():
                    tag = await elem.evaluate("el => el.tagName.toLowerCase()")
                    if tag == "select":
                        try:
                            await elem.select_option(label=edu.get("degree", ""))
                        except Exception:
                            pass
                    else:
                        await elem.fill(edu.get("degree", ""))
                    await self.browser_manager.human_delay(200, 400)
                    break
            except Exception:
                continue

        # Major / Field of study
        major_selectors = [
            'input[name*="major"]', 'input[name*="field_of_study"]',
            'input[name*="fieldOfStudy"]', 'input[name*="discipline"]',
            'input[placeholder*="Major"]', 'input[placeholder*="Field"]',
            'select[name*="major"]', 'select[name*="field_of_study"]',
        ]
        for selector in major_selectors:
            try:
                elem = await page.query_selector(selector)
                if elem and await elem.is_visible():
                    tag = await elem.evaluate("el => el.tagName.toLowerCase()")
                    if tag == "select":
                        try:
                            await elem.select_option(label=edu.get("field_of_study", ""))
                        except Exception:
                            pass
                    else:
                        await elem.fill(edu.get("field_of_study", ""))
                    await self.browser_manager.human_delay(200, 400)
                    break
            except Exception:
                continue

        # GPA
        gpa_selectors = [
            'input[name*="gpa"]', 'input[id*="gpa"]',
            'input[placeholder*="GPA"]',
        ]
        for selector in gpa_selectors:
            try:
                elem = await page.query_selector(selector)
                if elem and await elem.is_visible():
                    await elem.fill(str(edu.get("gpa", "")))
                    await self.browser_manager.human_delay(200, 400)
                    break
            except Exception:
                continue

        # Graduation date
        grad_selectors = [
            'input[name*="graduation"]', 'input[name*="gradDate"]',
            'input[name*="grad_date"]', 'input[placeholder*="Graduation"]',
            'select[name*="graduation"]', 'select[name*="gradDate"]',
        ]
        for selector in grad_selectors:
            try:
                elem = await page.query_selector(selector)
                if elem and await elem.is_visible():
                    tag = await elem.evaluate("el => el.tagName.toLowerCase()")
                    if tag == "select":
                        try:
                            await elem.select_option(label=edu.get("graduation_date", ""))
                        except Exception:
                            pass
                    else:
                        await elem.fill(edu.get("graduation_date", ""))
                    await self.browser_manager.human_delay(200, 400)
                    break
            except Exception:
                continue

    async def _submit_application(self, page: Page) -> bool:
        """Submit the Lever application."""
        # Review mode — pause for user to verify
        if self.review_mode:
            company = self.ai_answerer.job_context.get("company", "Unknown")
            role = self.ai_answerer.job_context.get("role", "Unknown")
            approved = await self.pause_for_review(page, company, role)
            if not approved:
                return False

        submit_selectors = [
            'button[type="submit"]',
            'button:has-text("Submit application")',
            'button:has-text("Submit")',
            'input[type="submit"]',
            '.postings-btn[type="submit"]',
        ]

        for selector in submit_selectors:
            try:
                btn = await page.query_selector(selector)
                if btn and await btn.is_visible():
                    # Solve CAPTCHA (handles both reCAPTCHA and hCaptcha via base handler)
                    captcha_solved = await self.solve_invisible_recaptcha(page)
                    if not captcha_solved:
                        # Check if hCaptcha overlay is actively blocking the submit button
                        hcaptcha_blocking = await page.query_selector('.h-captcha iframe[src*="hcaptcha"]')
                        if hcaptcha_blocking and not getattr(self, 'assist_mode', False):
                            logger.warning("hCaptcha blocking submit and solve failed — skipping job")
                            self._last_status = "captcha"
                            return False
                        logger.warning("CAPTCHA solve failed — submitting anyway, tab stays open for manual")
                    await self.browser_manager.human_delay(500, 1000)
                    await btn.click()
                    logger.info("Clicked Lever submit button")
                    return True
            except Exception as e:
                logger.debug(f"Error clicking submit {selector}: {e}")
                continue

        return False

    async def _run_dry_run_validation(self, page: Page) -> bool:
        """Validate form fill quality in dry-run mode."""
        logger.info("DRY RUN: Running Lever validation checks...")
        # Guard: check if page is still open (Simplify or redirect may close it)
        if page.is_closed():
            logger.warning("DRY RUN: Page was closed before validation — likely Simplify redirect or expired job")
            return False
        await self.take_screenshot(page, "lever_dry_run")

        filled_fields = {}

        # Check core fields
        field_checks = {
            "name": 'input[name="name"]',
            "email": 'input[name="email"]',
            "phone": 'input[name="phone"]',
        }

        for field_name, selector in field_checks.items():
            try:
                elem = await page.query_selector(selector)
                if elem:
                    value = await elem.input_value()
                    if value and value.strip():
                        filled_fields[field_name] = value.strip()
            except Exception:
                continue

        # Check resume upload
        resume_uploaded = False
        body_text = (await page.text_content("body") or "").lower()
        resume_uploaded = any(ext in body_text for ext in [".pdf", ".docx", ".doc", ".rtf"])

        # Log results
        core_fields = ["name", "email"]
        core_filled = sum(1 for f in core_fields if f in filled_fields)
        core_missing = [f for f in core_fields if f not in filled_fields]

        logger.info(f"DRY RUN: Core fields filled: {core_filled}/{len(core_fields)}")
        if core_missing:
            logger.warning(f"DRY RUN: Missing core fields: {core_missing}")
        logger.info(f"DRY RUN: All filled fields: {list(filled_fields.keys())}")
        logger.info(f"DRY RUN: Resume uploaded: {resume_uploaded}")

        passed = core_filled >= 2
        if passed:
            logger.info("DRY RUN: Lever validation PASSED")
        else:
            logger.warning("DRY RUN: Lever validation FAILED — too few core fields")

        return passed
