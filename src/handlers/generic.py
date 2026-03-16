"""
Generic Handler

Fallback handler for unknown ATS platforms.
Uses intelligent form detection and filling.
"""

import re
from typing import Dict, Any
from playwright.async_api import Page
from loguru import logger

from .base import BaseHandler


def _make_apply_regex():
    """Create a regex pattern matching common apply button text (case-insensitive)."""
    return re.compile(r"(?i)\bapply\b")


class GenericHandler(BaseHandler):
    """Generic handler for unknown ATS platforms."""

    name = "generic"

    async def apply(self, page: Page, job_url: str, job_data: Dict[str, Any]) -> bool:
        """Apply to a job using generic form filling."""
        self._last_status = "failed"
        try:
            logger.info(f"Applying to job (generic): {job_data.get('company')} - {job_data.get('role')}")

            # Navigate to job URL
            await page.goto(job_url, wait_until="networkidle", timeout=30000)
            await self.browser_manager.human_delay(1000, 2000)

            # Dismiss cookie banners, location popups, overlays
            try:
                for dismiss_sel in [
                    'button:has-text("Accept")', 'button:has-text("Accept All")',
                    'button:has-text("OK")', 'button:has-text("Got it")',
                    'button:has-text("Close")', 'button:has-text("Dismiss")',
                    'button:has-text("Continue")', '[class*="cookie"] button',
                    '[id*="cookie"] button', '[class*="consent"] button',
                    '[class*="banner"] button:has-text("Accept")',
                    '[aria-label="Close"]', '[class*="modal"] button:has-text("Close")',
                ]:
                    btn = page.locator(dismiss_sel).first
                    if await btn.count() > 0 and await btn.is_visible():
                        await btn.click(timeout=2000)
                        await self.browser_manager.human_delay(500, 1000)
                        break
            except Exception:
                pass

            # Check for CAPTCHA
            if not await self.handle_captcha(page):
                return False

            # Try to find and click Apply button if not on form already
            if not await self._is_on_application_form(page):
                clicked = await self._click_apply_button(page)
                if clicked:
                    await self.browser_manager.human_delay(2000, 3000)

            # Fill the form
            max_pages = 5
            for page_num in range(max_pages):
                logger.debug(f"Processing generic form page {page_num + 1}")

                # Fill current page
                await self._fill_page(page, job_data)
                await self.browser_manager.human_delay(1000, 1500)

                # Check if done
                if await self.is_application_complete(page):
                    logger.info("Generic application submitted successfully!")
                    await self.take_screenshot(page, f"PASS_generic_{job_data.get('company', 'unknown')}")
                    self._last_status = "success"
                    return True

                # Dry run: validate but don't submit
                if self.dry_run:
                    validation = await self._run_dry_run_validation(page)
                    self._last_status = "success" if validation else "failed"
                    return validation

                # Try to advance
                action = await self.form_filler.click_next_or_submit(page)
                if action == "submit":
                    await self.browser_manager.human_delay(2000, 3000)
                    break
                elif action == "none":
                    # No button found, try to submit any form
                    await self._try_submit_form(page)
                    break

                await self.browser_manager.human_delay(1500, 2500)

            # Final check
            await self.browser_manager.human_delay(2000, 3000)
            if await self.is_application_complete(page):
                logger.info("Generic application submitted successfully!")
                await self.take_screenshot(page, f"PASS_generic_{job_data.get('company', 'unknown')}")
                self._last_status = "success"
                return True

            # Check if job is closed
            if await self.is_job_closed(page):
                self._last_status = "closed"
                return False

            error = await self.get_error_message(page)
            if error:
                logger.error(f"Generic application error: {error}")
                self._last_status = f"error: {error[:80]}"

            return False

        except Exception as e:
            logger.error(f"Generic application failed: {e}")
            self._last_status = f"exception: {str(e)[:80]}"
            await self.take_screenshot(page, f"generic_error_{job_data.get('company', 'unknown')}")
            return False

    async def detect_form_type(self, page: Page) -> str:
        """Detect form type."""
        # Check for common patterns
        if await page.query_selector('form'):
            forms = await page.query_selector_all('form')
            if len(forms) == 1:
                return "single_form"
            return "multi_form"

        return "unknown"

    async def _is_on_application_form(self, page: Page) -> bool:
        """Check if we're already on an application form."""
        form_indicators = [
            'input[name*="name"]',
            'input[name*="email"]',
            'input[type="file"]',
            'form[action*="apply"]',
            'form[action*="submit"]',
        ]

        for indicator in form_indicators:
            if await page.query_selector(indicator):
                return True

        return False

    async def _click_apply_button(self, page: Page) -> bool:
        """Try to find and click an apply button."""
        # Wait for JS-rendered buttons to appear
        await self.browser_manager.human_delay(2500, 3500)

        # Scroll down to find buttons below the fold
        try:
            await page.evaluate("window.scrollBy(0, 500)")
            await self.browser_manager.human_delay(500, 1000)
        except Exception:
            pass

        apply_patterns = [
            'a:has-text("Apply Now")',
            'button:has-text("Apply Now")',
            'a:has-text("Apply for this job")',
            'button:has-text("Apply for this job")',
            'a:has-text("Apply for this position")',
            'button:has-text("Apply for this position")',
            'a:has-text("Submit Application")',
            'button:has-text("Submit Application")',
            'a:has-text("Apply Online")',
            'button:has-text("Apply Online")',
            'a:has-text("Start Application")',
            'button:has-text("Start Application")',
            'a:has-text("I\'m interested")',
            'button:has-text("I\'m interested")',
            'a:has-text("Apply")',
            'button:has-text("Apply")',
            '.apply-button',
            '#apply-button',
            '[class*="apply"]',
        ]

        for pattern in apply_patterns:
            try:
                elem = await page.query_selector(pattern)
                if elem and await elem.is_visible():
                    await elem.click()
                    logger.debug(f"Clicked apply button: {pattern}")
                    # Wait and check if a form appeared
                    await self.browser_manager.human_delay(2500, 3500)
                    if await self._is_on_application_form(page):
                        return True
                    # Button clicked but no form yet — keep going, might still work
                    return True
            except Exception:
                continue

        # Fallback: find any visible <a> or <button> containing "apply" (case-insensitive)
        try:
            fallback_elem = page.locator('a, button').filter(has_text=_make_apply_regex())
            count = await fallback_elem.count()
            for i in range(count):
                item = fallback_elem.nth(i)
                if await item.is_visible():
                    await item.click()
                    logger.debug(f"Clicked fallback apply element (index {i})")
                    await self.browser_manager.human_delay(2500, 3500)
                    return True
        except Exception:
            pass

        return False

    async def _fill_page(self, page: Page, job_data: Dict[str, Any]) -> None:
        """Fill all fields on the current page."""
        config = self.form_filler.config

        # Use form filler for automatic field detection
        filled = await self.form_filler.fill_form(page)
        logger.debug(f"Auto-filled {len(filled)} fields")

        # Upload resume
        resume_path = config.get("files", {}).get("resume")
        if resume_path:
            await self.form_filler.upload_resume(page, resume_path)

        # Handle textareas with AI (often custom questions)
        await self._fill_textareas(page, job_data)

        # Handle any remaining unfilled required fields
        await self._fill_required_fields(page, job_data)

        # Check ALL checkboxes — terms, consent, privacy, agree
        # This fixes the "You need to agree to the terms" error (13+ failures)
        try:
            await page.evaluate("""() => {
                const checkboxes = document.querySelectorAll(
                    'input[type="checkbox"]:not(:checked)'
                );
                for (const cb of checkboxes) {
                    const label = (cb.closest('label') || cb.parentElement || {}).textContent || '';
                    const name = (cb.name || cb.id || '').toLowerCase();
                    const ll = label.toLowerCase();
                    // Check consent/terms/privacy/agree checkboxes
                    if (ll.includes('agree') || ll.includes('terms') || ll.includes('consent') ||
                        ll.includes('privacy') || ll.includes('acknowledge') || ll.includes('confirm') ||
                        ll.includes('accept') || ll.includes('certif') ||
                        name.includes('agree') || name.includes('terms') || name.includes('consent') ||
                        name.includes('privacy') || name.includes('accept')) {
                        cb.click();
                        cb.checked = true;
                        cb.dispatchEvent(new Event('change', {bubbles: true}));
                    }
                }
            }""")
            logger.debug("Checked consent/terms/privacy checkboxes")
        except Exception:
            pass

    async def _fill_textareas(self, page: Page, job_data: Dict[str, Any]) -> None:
        """Fill textarea fields using AI answerer."""
        textareas = await page.query_selector_all('textarea:not([style*="display: none"])')

        for textarea in textareas:
            try:
                # Skip if already filled
                current_value = await textarea.input_value()
                if current_value and len(current_value) > 10:
                    continue

                # Skip hidden textareas
                if not await textarea.is_visible():
                    continue

                # Get question text from label, placeholder, or aria-label
                question_text = ""

                # Try to get label
                textarea_id = await textarea.get_attribute("id")
                if textarea_id:
                    label = await page.query_selector(f'label[for="{textarea_id}"]')
                    if label:
                        question_text = (await label.text_content() or "").strip()

                # Try aria-label or placeholder
                if not question_text:
                    question_text = (
                        await textarea.get_attribute("aria-label") or
                        await textarea.get_attribute("placeholder") or
                        await textarea.get_attribute("name") or
                        ""
                    )

                # Try to find nearby label
                if not question_text:
                    parent = await textarea.evaluate_handle("el => el.closest('.field, .form-group, .question') || el.parentElement")
                    label_elem = await parent.query_selector('label, .label, .question-text')
                    if label_elem:
                        question_text = (await label_elem.text_content() or "").strip()

                if question_text:
                    logger.debug(f"Found textarea question: {question_text[:50]}")
                    answer = await self.ai_answerer.answer_question(
                        question_text, "textarea", max_length=800
                    )
                    if answer:
                        await textarea.fill(answer)
                        await self.browser_manager.human_delay(300, 600)
                        logger.info(f"AI filled textarea: {question_text[:40]}...")

            except Exception as e:
                logger.debug(f"Error filling textarea: {e}")

    async def _fill_required_fields(self, page: Page, job_data: Dict[str, Any]) -> None:
        """Find and fill any remaining required fields."""
        # Find fields marked as required but still empty
        required_inputs = await page.query_selector_all(
            'input[required]:not([type="hidden"]), '
            'textarea[required], '
            'select[required], '
            '[aria-required="true"]'
        )

        for input_elem in required_inputs:
            try:
                # Check if already filled
                value = await input_elem.input_value()
                if value:
                    continue

                # Get field info
                name = await input_elem.get_attribute("name") or ""
                id_attr = await input_elem.get_attribute("id") or ""
                placeholder = await input_elem.get_attribute("placeholder") or ""

                # Try to get label
                label_text = ""
                if id_attr:
                    label = await page.query_selector(f'label[for="{id_attr}"]')
                    if label:
                        label_text = await label.text_content() or ""

                # Use AI to answer
                question = f"{label_text} {name} {placeholder}".strip()
                if question:
                    tag = await input_elem.evaluate("el => el.tagName.toLowerCase()")

                    if tag == "select":
                        options = []
                        option_elems = await input_elem.query_selector_all("option")
                        for opt in option_elems:
                            text = await opt.text_content()
                            if text and text.strip():
                                options.append(text.strip())

                        if options:
                            answer = await self.ai_answerer.answer_question(
                                question, "select", options
                            )
                            try:
                                await input_elem.select_option(label=answer)
                            except Exception:
                                pass

                    elif tag == "textarea":
                        answer = await self.ai_answerer.answer_question(
                            question, "textarea", max_length=500
                        )
                        await input_elem.fill(answer)

                    else:
                        answer = await self.ai_answerer.answer_question(
                            question, "text", max_length=200
                        )
                        await input_elem.fill(answer)

                    await self.browser_manager.human_delay(200, 400)

            except Exception as e:
                logger.debug(f"Could not fill required field: {e}")

    async def _try_submit_form(self, page: Page) -> bool:
        """Try to submit any form on the page."""
        try:
            # Find forms
            forms = await page.query_selector_all('form')

            for form in forms:
                # Try to find submit button within form
                submit = await form.query_selector(
                    'button[type="submit"], '
                    'input[type="submit"], '
                    'button:has-text("Submit")'
                )

                if submit and await submit.is_visible():
                    await submit.click()
                    logger.debug("Submitted form")
                    return True

            # Try submitting via JavaScript
            await page.evaluate("document.forms[0]?.submit()")
            return True

        except Exception as e:
            logger.debug(f"Could not submit form: {e}")
            return False

    async def _run_dry_run_validation(self, page: Page) -> bool:
        """Validate form fill quality in dry-run mode."""
        logger.info("DRY RUN: Running generic form validation...")
        await self.take_screenshot(page, "generic_dry_run")

        filled_fields = {}
        empty_required = []

        # Check all visible inputs
        inputs = await page.query_selector_all(
            'input:not([type="hidden"]):not([type="submit"]):not([type="button"]), '
            'textarea, select'
        )

        for inp in inputs:
            try:
                if not await inp.is_visible():
                    continue

                name = (
                    await inp.get_attribute("name") or
                    await inp.get_attribute("id") or
                    await inp.get_attribute("aria-label") or
                    "unknown"
                )
                inp_type = await inp.get_attribute("type") or "text"
                tag = await inp.evaluate("el => el.tagName.toLowerCase()")

                # Get value
                if tag == "select":
                    value = await inp.evaluate("el => el.options[el.selectedIndex]?.text || ''")
                elif inp_type in ("checkbox", "radio"):
                    value = "checked" if await inp.is_checked() else ""
                else:
                    value = await inp.input_value()

                is_required = (
                    await inp.get_attribute("required") is not None or
                    await inp.get_attribute("aria-required") == "true"
                )

                if value and value.strip():
                    filled_fields[name] = value.strip()[:40]
                elif is_required:
                    empty_required.append(name)

            except Exception:
                continue

        # Log results
        logger.info(f"DRY RUN: Filled {len(filled_fields)} fields")
        for name, val in filled_fields.items():
            logger.info(f"  {name}: {val}")

        if empty_required:
            logger.warning(f"DRY RUN: Empty required fields: {empty_required}")

        # Check resume upload
        file_inputs = await page.query_selector_all('input[type="file"]')
        resume_uploaded = False
        for fi in file_inputs:
            try:
                files = await fi.evaluate("el => el.files.length")
                if files > 0:
                    resume_uploaded = True
                    break
            except Exception:
                pass

        if resume_uploaded:
            logger.info("DRY RUN: Resume uploaded")
        else:
            body_text = (await page.text_content("body") or "").lower()
            if any(ext in body_text for ext in [".pdf", ".docx", ".doc"]):
                logger.info("DRY RUN: Resume appears uploaded (file name visible)")
                resume_uploaded = True
            else:
                logger.warning("DRY RUN: No resume detected")

        # Pass if at least 2 fields filled
        passed = len(filled_fields) >= 2
        if passed:
            logger.info("DRY RUN: Generic validation PASSED")
        else:
            logger.warning("DRY RUN: Generic validation FAILED (too few fields)")

        return passed
