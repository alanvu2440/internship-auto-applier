"""
SmartRecruiters Handler

Handles job applications on SmartRecruiters ATS.
URLs: jobs.smartrecruiters.com

SmartRecruiters oneclick-ui is protected by DataDome CAPTCHA which blocks
Playwright's Chromium.  This handler uses nodriver (undetected-chromedriver)
to bypass DataDome and fills the Shadow DOM form fields via JavaScript.

Flow:
  1. Launch nodriver (bypasses DataDome bot detection)
  2. Navigate to job page
  3. Click "I'm interested" button
  4. Wait for oneclick-ui Shadow DOM form to load
  5. Fill fields by piercing shadow roots (spl-input, spl-phone-field, etc.)
  6. Upload resume via the spl-dropzone file input
  7. Submit or validate (dry run)
"""

import asyncio
import os
import re
from pathlib import Path
from typing import Dict, Any, Optional
from playwright.async_api import Page
from loguru import logger

from .base import BaseHandler

# Lazy-import nodriver to avoid ImportError if not installed
_nodriver = None


def _get_nodriver():
    global _nodriver
    if _nodriver is None:
        try:
            import nodriver
            _nodriver = nodriver
        except ImportError:
            raise ImportError(
                "nodriver is required for SmartRecruiters. "
                "Install it with: pip install nodriver"
            )
    return _nodriver


class SmartRecruitersHandler(BaseHandler):
    """Handler for SmartRecruiters ATS applications.

    Uses nodriver (undetected-chromedriver) instead of Playwright to bypass
    DataDome bot protection on the SmartRecruiters oneclick-ui form.
    """

    name = "smartrecruiters"

    async def apply(self, page: Page, job_url: str, job_data: Dict[str, Any]) -> bool:
        """Apply to a SmartRecruiters job using nodriver."""
        self._last_status = "failed"
        self._fields_filled = {}
        self._fields_missed = {}
        nd_browser = None

        try:
            logger.info(
                f"Applying to SmartRecruiters job: "
                f"{job_data.get('company')} - {job_data.get('role')}"
            )

            # --- Use nodriver for the entire SR flow ---
            uc = _get_nodriver()
            nd_browser = await uc.start(
                headless=False,
                browser_args=[
                    "--window-size=1920,1080",
                    "--window-position=10000,10000",
                ],
            )

            nd_page = await nd_browser.get(job_url)
            await asyncio.sleep(4)

            # Check if job is closed
            content = await nd_page.get_content()
            if self._is_closed_content(content):
                logger.info("Job is closed/unavailable")
                self._last_status = "closed"
                return False

            # Click "I'm interested" / Apply button
            apply_clicked = await self._nd_click_apply(nd_page)
            if not apply_clicked:
                logger.warning("Could not find Apply button on SmartRecruiters page")
                return False

            # Wait for oneclick-ui form to load
            await asyncio.sleep(8)

            # Verify the form loaded (check for Shadow DOM components)
            content = await nd_page.get_content()
            if "first-name-input" not in content:
                # Try waiting a bit more
                await asyncio.sleep(5)
                content = await nd_page.get_content()

            if "first-name-input" not in content:
                logger.error(
                    "SmartRecruiters oneclick-ui form did not load. "
                    f"URL: {nd_page.url}"
                )
                if "ddjskey" in content[:500]:
                    logger.error("DataDome is blocking — form never loaded")
                    self._last_status = "captcha_blocked"
                return False

            logger.info("SmartRecruiters oneclick-ui form loaded")

            # Fill the form via Shadow DOM JavaScript
            config = self.form_filler.config
            filled = await self._nd_fill_form(nd_page, config, job_data)
            if not filled:
                return False

            # Handle screening/custom questions with AI
            await self._nd_handle_screening_questions(nd_page, job_data)

            # Upload resume
            resume_path = config.get("files", {}).get("resume")
            if resume_path:
                await self._nd_upload_resume(nd_page, resume_path)

            # Dry run: validate and return
            if self.dry_run:
                logger.info("DRY RUN: Form filled, running validation")
                validation = await self._nd_validate(nd_page)
                self._last_status = "success" if validation else "failed"
                return validation

            # Submit
            await self._nd_submit(nd_page)
            await asyncio.sleep(3)

            # Check success
            content_after = await nd_page.get_content()
            success_indicators = [
                "thank you", "application received", "application submitted",
                "successfully applied", "application complete",
            ]
            content_lower = content_after.lower()
            for indicator in success_indicators:
                if indicator in content_lower:
                    logger.info("SmartRecruiters application submitted!")
                    self._last_status = "success"
                    return True

            logger.warning("Submit completed but no success confirmation found")
            return False

        except Exception as e:
            logger.error(f"SmartRecruiters application failed: {e}")
            return False
        finally:
            if nd_browser:
                try:
                    nd_browser.stop()
                except Exception as e:
                    logger.debug(f"Error closing nodriver browser: {e}")

    async def detect_form_type(self, page: Page) -> str:
        """Detect SmartRecruiters form type."""
        return "oneclick"

    # ------------------------------------------------------------------
    # nodriver helpers
    # ------------------------------------------------------------------

    def _is_closed_content(self, content: str) -> bool:
        """Check if page content indicates the job is closed."""
        content_lower = content.lower()
        closed_indicators = [
            "this job has expired",
            "sorry, this job has expired",
            "oops, you've gone too far",
            "position has been filled",
            "no longer accepting",
            "job has been closed",
            "this position is closed",
            "this job is no longer available",
            "page not found",
        ]
        return any(ind in content_lower for ind in closed_indicators)

    async def _nd_click_apply(self, nd_page) -> bool:
        """Click the Apply / 'I'm interested' button using nodriver."""
        # Try finding the button by text
        for text in ["I'm interested", "Apply now", "Apply for this job", "Apply"]:
            try:
                btn = await nd_page.find(text, best_match=True)
                if btn:
                    await btn.click()
                    logger.info(f"Clicked apply button: '{text}'")
                    return True
            except Exception:
                continue

        # Try CSS selectors
        for selector in [
            "a[data-sr-track='apply']",
            "a.js-oneclick",
            "button[data-sr-track='apply']",
        ]:
            try:
                elem = await nd_page.select(selector)
                if elem:
                    await elem.click()
                    logger.info(f"Clicked apply via selector: {selector}")
                    return True
            except Exception:
                continue

        return False

    async def _nd_fill_form(
        self, nd_page, config: Dict[str, Any], job_data: Dict[str, Any]
    ) -> bool:
        """Fill the SmartRecruiters oneclick-ui form via Shadow DOM JavaScript.

        The oneclick-ui uses Web Components (spl-input, spl-textarea, etc.)
        with Shadow DOM.  We must pierce the shadow root to access the actual
        <input>/<textarea> elements and set their values with native setters
        so Angular change detection picks up the changes.
        """
        personal = config.get("personal_info", {})

        fields = {
            "first-name-input": personal.get("first_name", ""),
            "last-name-input": personal.get("last_name", ""),
            "email-input": personal.get("email", ""),
            "confirm-email-input": personal.get("email", ""),
            "linkedin-input": personal.get("linkedin", ""),
            "website-input": personal.get("portfolio", "") or personal.get("github", ""),
        }

        # Build JS to fill all spl-input fields
        fill_results = {}
        for element_id, value in fields.items():
            if not value:
                continue
            try:
                escaped_value = value.replace("\\", "\\\\").replace("'", "\\'")
                result = await nd_page.evaluate(f"""
                    (function() {{
                        var host = document.querySelector('#{element_id}');
                        if (!host || !host.shadowRoot) return 'HOST_NOT_FOUND';
                        var input = host.shadowRoot.querySelector('input');
                        if (!input) return 'INPUT_NOT_FOUND';
                        var nativeSetter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value'
                        ).set;
                        nativeSetter.call(input, '{escaped_value}');
                        input.dispatchEvent(new Event('input', {{bubbles: true, composed: true}}));
                        input.dispatchEvent(new Event('change', {{bubbles: true, composed: true}}));
                        input.dispatchEvent(new Event('blur', {{bubbles: true, composed: true}}));
                        return 'OK';
                    }})()
                """)
                if result == "OK":
                    fill_results[element_id] = True
                    logger.debug(f"Filled {element_id}")
                else:
                    fill_results[element_id] = False
                    logger.debug(f"Could not fill {element_id}: {result}")
            except Exception as e:
                logger.debug(f"Error filling {element_id}: {e}")
                fill_results[element_id] = False

        # Fill phone (spl-phone-field — nested shadow DOM)
        phone = f"{personal.get('phone_prefix', '')}{personal.get('phone', '')}".replace("+", "")
        if phone:
            try:
                phone_result = await nd_page.evaluate(f"""
                    (function() {{
                        var phoneHost = document.querySelector('spl-phone-field');
                        if (!phoneHost || !phoneHost.shadowRoot) return 'NO_PHONE_HOST';

                        // spl-phone-field > shadowRoot > spl-internal-form-field > ... > input
                        // Try multiple levels of shadow DOM piercing
                        var input = phoneHost.shadowRoot.querySelector('input[type="tel"]');
                        if (!input) {{
                            // Try deeper: spl-internal-form-field also has shadow
                            var inner = phoneHost.shadowRoot.querySelector('spl-internal-form-field');
                            if (inner && inner.shadowRoot) {{
                                input = inner.shadowRoot.querySelector('input[type="tel"]');
                            }}
                        }}
                        if (!input) {{
                            // Last resort: search all shadow roots recursively
                            function findInput(root) {{
                                var inp = root.querySelector('input[type="tel"], input:not([type="hidden"])');
                                if (inp) return inp;
                                var hosts = root.querySelectorAll('*');
                                for (var i = 0; i < hosts.length; i++) {{
                                    if (hosts[i].shadowRoot) {{
                                        var found = findInput(hosts[i].shadowRoot);
                                        if (found) return found;
                                    }}
                                }}
                                return null;
                            }}
                            input = findInput(phoneHost.shadowRoot);
                        }}
                        if (!input) return 'NO_INPUT';

                        var nativeSetter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value'
                        ).set;
                        nativeSetter.call(input, '{phone}');
                        input.dispatchEvent(new Event('input', {{bubbles: true, composed: true}}));
                        input.dispatchEvent(new Event('change', {{bubbles: true, composed: true}}));
                        return 'OK';
                    }})()
                """)
                if phone_result == "OK":
                    fill_results["phone"] = True
                    logger.debug("Filled phone")
                else:
                    logger.debug(f"Phone fill result: {phone_result}")
            except Exception as e:
                logger.debug(f"Error filling phone: {e}")

        # Fill hiring manager message (spl-textarea)
        company = job_data.get("company", "your company")
        message = (
            f"I am excited to apply for this position at {company}. "
            f"I believe my experience and skills make me a strong candidate."
        )
        try:
            escaped_msg = message.replace("\\", "\\\\").replace("'", "\\'")
            msg_result = await nd_page.evaluate(f"""
                (function() {{
                    var host = document.querySelector('spl-textarea#hiring-manager-message-input');
                    if (!host) host = document.querySelector('spl-textarea');
                    if (!host || !host.shadowRoot) return 'NO_HOST';
                    var ta = host.shadowRoot.querySelector('textarea');
                    if (!ta) return 'NO_TEXTAREA';
                    var nativeSetter = Object.getOwnPropertyDescriptor(
                        window.HTMLTextAreaElement.prototype, 'value'
                    ).set;
                    nativeSetter.call(ta, '{escaped_msg}');
                    ta.dispatchEvent(new Event('input', {{bubbles: true, composed: true}}));
                    ta.dispatchEvent(new Event('change', {{bubbles: true, composed: true}}));
                    return 'OK';
                }})()
            """)
            if msg_result == "OK":
                fill_results["message"] = True
                logger.debug("Filled hiring manager message")
        except Exception as e:
            logger.debug(f"Error filling message: {e}")

        # Fill location/autocomplete if present (spl-autocomplete)
        city = personal.get("city", "")
        if city:
            try:
                await nd_page.evaluate(f"""
                    (function() {{
                        var host = document.querySelector('spl-autocomplete');
                        if (!host || !host.shadowRoot) return;
                        var input = host.shadowRoot.querySelector('input');
                        if (!input) return;
                        var nativeSetter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value'
                        ).set;
                        nativeSetter.call(input, '{city}');
                        input.dispatchEvent(new Event('input', {{bubbles: true, composed: true}}));
                        input.dispatchEvent(new Event('change', {{bubbles: true, composed: true}}));
                    }})()
                """)
            except Exception as e:
                logger.debug(f"Error filling location autocomplete: {e}")

        # Log summary and track fields
        filled_count = sum(1 for v in fill_results.values() if v)
        total = len(fields) + 2  # +phone +message
        logger.info(f"SmartRecruiters form: filled {filled_count}/{total} fields")

        # Populate fields_filled / fields_missed for tracking
        field_labels = {
            "first-name-input": "First Name",
            "last-name-input": "Last Name",
            "email-input": "Email",
            "confirm-email-input": "Confirm Email",
            "linkedin-input": "LinkedIn",
            "website-input": "Website/GitHub",
            "phone": "Phone",
            "message": "Hiring Manager Message",
        }
        # Build a lookup that includes phone/message values
        all_values = dict(fields)
        all_values["phone"] = phone
        all_values["message"] = message

        for field_id, success in fill_results.items():
            label = field_labels.get(field_id, field_id)
            if success:
                val = all_values.get(field_id, "(filled)")
                self._fields_filled[label] = str(val)[:40] if val else "(filled)"
            else:
                self._fields_missed[label] = "Could not fill"

        # Check minimum required fields
        required = ["first-name-input", "last-name-input", "email-input"]
        required_filled = sum(1 for r in required if fill_results.get(r))
        if required_filled < 2:
            logger.error(f"Too few required fields filled: {required_filled}/3")
            return False

        return True

    async def _nd_upload_resume(self, nd_page, resume_path: str) -> bool:
        """Upload resume via the spl-dropzone Shadow DOM file input."""
        abs_path = str(Path(resume_path).resolve())
        if not os.path.exists(abs_path):
            logger.warning(f"Resume file not found: {abs_path}")
            return False

        try:
            # Find the file input inside spl-dropzone shadow root
            result = await nd_page.evaluate(f"""
                (function() {{
                    var dropzone = document.querySelector('spl-dropzone');
                    if (!dropzone || !dropzone.shadowRoot) return 'NO_DROPZONE';
                    var fileInput = dropzone.shadowRoot.querySelector('input[type="file"]');
                    if (!fileInput) return 'NO_FILE_INPUT';
                    return 'FOUND';
                }})()
            """)

            if result != "FOUND":
                logger.warning(f"Dropzone file input: {result}")
                return False

            # nodriver can set files on file inputs using CDP
            # We need to get the DOM node ID and use Input.setFiles
            # Alternative: use the nodriver file upload API
            try:
                file_input = await nd_page.select("input[type='file']")
                if file_input:
                    # Use CDP to set the file
                    await file_input.send_file(abs_path)
                    logger.info("Resume uploaded via nodriver")
                    await asyncio.sleep(2)
                    return True
            except Exception as e1:
                logger.debug(f"nodriver send_file failed: {e1}")

            # Fallback: try to use CDP DOM.setFileInputFiles directly
            try:
                # Find the actual file input element in the shadow DOM
                node_info = await nd_page.send(
                    "Runtime.evaluate",
                    expression="""
                        (function() {
                            var dz = document.querySelector('spl-dropzone');
                            if (!dz || !dz.shadowRoot) return null;
                            return dz.shadowRoot.querySelector('input[type="file"]');
                        })()
                    """,
                    returnByValue=False,
                )
                if node_info and node_info.get("result", {}).get("objectId"):
                    object_id = node_info["result"]["objectId"]
                    # Get the DOM node
                    node = await nd_page.send(
                        "DOM.describeNode",
                        objectId=object_id,
                    )
                    backend_node_id = node.get("node", {}).get("backendNodeId")
                    if backend_node_id:
                        await nd_page.send(
                            "DOM.setFileInputFiles",
                            files=[abs_path],
                            backendNodeId=backend_node_id,
                        )
                        logger.info("Resume uploaded via CDP")
                        await asyncio.sleep(2)
                        return True
            except Exception as e2:
                logger.debug(f"CDP file upload failed: {e2}")

        except Exception as e:
            logger.warning(f"Resume upload error: {e}")

        return False

    async def _nd_handle_screening_questions(self, nd_page, job_data: Dict[str, Any]) -> None:
        """Handle screening/custom questions on SmartRecruiters forms using AI.

        SmartRecruiters screening questions appear as additional form fields
        beyond the standard name/email/phone. They can be text inputs, selects,
        textareas, or radio buttons, often inside spl-* shadow DOM components
        or standard HTML elements in the form.
        """
        try:
            # Detect all screening question containers
            questions_data = await nd_page.evaluate("""
                (function() {
                    var questions = [];
                    // Look for screening question containers
                    // SmartRecruiters uses div.field or div.question-item
                    var containers = document.querySelectorAll(
                        '.field, .question-item, [class*="screening"], [class*="question"]'
                    );
                    // Also check for standard form groups outside the known fields
                    if (containers.length === 0) {
                        containers = document.querySelectorAll('.form-group, .form-field');
                    }

                    var knownIds = [
                        'first-name-input', 'last-name-input', 'email-input',
                        'confirm-email-input', 'linkedin-input', 'website-input',
                        'hiring-manager-message-input'
                    ];

                    for (var i = 0; i < containers.length; i++) {
                        var container = containers[i];
                        var label = container.querySelector('label, .label, legend');
                        if (!label) continue;
                        var labelText = (label.textContent || '').trim();
                        if (!labelText || labelText.length < 3) continue;

                        // Skip known standard fields
                        var skipPatterns = [
                            'first name', 'last name', 'email', 'phone',
                            'linkedin', 'website', 'resume', 'cv', 'cover letter',
                            'message to hiring'
                        ];
                        var skip = false;
                        for (var s = 0; s < skipPatterns.length; s++) {
                            if (labelText.toLowerCase().indexOf(skipPatterns[s]) >= 0) {
                                skip = true; break;
                            }
                        }
                        if (skip) continue;

                        // Find the input element
                        var input = container.querySelector(
                            'input:not([type="hidden"]):not([type="file"]):not([type="checkbox"]):not([type="radio"]), ' +
                            'textarea, select'
                        );

                        // Check if it's inside a shadow DOM component
                        if (!input) {
                            var splInput = container.querySelector('spl-input, spl-textarea, spl-select');
                            if (splInput && splInput.shadowRoot) {
                                input = splInput.shadowRoot.querySelector('input, textarea, select');
                            }
                        }

                        if (!input) continue;

                        // Check if already filled
                        if (input.value && input.value.length > 2) continue;

                        // Also check for known IDs to skip
                        var parentId = (input.id || '');
                        if (knownIds.indexOf(parentId) >= 0) continue;
                        var parentHost = input.getRootNode().host;
                        if (parentHost && knownIds.indexOf(parentHost.id || '') >= 0) continue;

                        var type = input.tagName.toLowerCase();
                        var options = [];
                        if (type === 'select') {
                            var opts = input.querySelectorAll('option');
                            for (var o = 0; o < opts.length; o++) {
                                var optText = (opts[o].textContent || '').trim();
                                if (optText && optText !== 'Select...' && optText !== '' && optText !== 'Choose...') {
                                    options.push(optText);
                                }
                            }
                        }

                        questions.push({
                            label: labelText,
                            type: type,
                            options: options,
                            index: i
                        });
                    }

                    // Also check for radio button groups
                    var radios = document.querySelectorAll('[class*="radio-group"], fieldset');
                    for (var r = 0; r < radios.length; r++) {
                        var legend = radios[r].querySelector('legend, label, .label');
                        if (!legend) continue;
                        var legendText = (legend.textContent || '').trim();
                        if (!legendText || legendText.length < 3) continue;

                        var radioInputs = radios[r].querySelectorAll('input[type="radio"]');
                        if (radioInputs.length === 0) continue;

                        var radioOpts = [];
                        for (var ri = 0; ri < radioInputs.length; ri++) {
                            var radioLabel = radios[r].querySelector('label[for="' + radioInputs[ri].id + '"]');
                            if (radioLabel) {
                                radioOpts.push((radioLabel.textContent || '').trim());
                            }
                        }
                        if (radioOpts.length > 0) {
                            questions.push({
                                label: legendText,
                                type: 'radio',
                                options: radioOpts,
                                index: containers.length + r
                            });
                        }
                    }

                    return JSON.stringify(questions);
                })()
            """)

            import json as _json
            questions = _json.loads(questions_data) if isinstance(questions_data, str) else []

            if not questions:
                logger.debug("No screening questions found on SmartRecruiters form")
                return

            logger.info(f"Found {len(questions)} screening questions on SmartRecruiters")

            for q in questions:
                question_text = q["label"]
                field_type = q["type"]
                options = q.get("options", [])
                index = q["index"]

                logger.debug(f"Screening Q: {question_text[:60]} (type={field_type})")

                try:
                    if field_type == "select" and options:
                        answer = await self.ai_answerer.answer_question(
                            question_text, "select", options
                        )
                    elif field_type == "radio" and options:
                        answer = await self.ai_answerer.answer_question(
                            question_text, "select", options
                        )
                    elif field_type == "textarea":
                        answer = await self.ai_answerer.answer_question(
                            question_text, "textarea", max_length=500
                        )
                    else:
                        answer = await self.ai_answerer.answer_question(
                            question_text, "text", max_length=200
                        )

                    if not answer:
                        continue

                    # Fill the answer via JavaScript
                    escaped_answer = answer.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
                    fill_result = await nd_page.evaluate(f"""
                        (function() {{
                            var containers = document.querySelectorAll(
                                '.field, .question-item, [class*="screening"], [class*="question"], ' +
                                '.form-group, .form-field, [class*="radio-group"], fieldset'
                            );
                            var container = containers[{index}];
                            if (!container) return 'CONTAINER_NOT_FOUND';

                            // For select
                            var select = container.querySelector('select');
                            if (select) {{
                                for (var i = 0; i < select.options.length; i++) {{
                                    if (select.options[i].text.indexOf('{escaped_answer}') >= 0 ||
                                        '{escaped_answer}'.indexOf(select.options[i].text) >= 0) {{
                                        select.selectedIndex = i;
                                        select.dispatchEvent(new Event('change', {{bubbles: true}}));
                                        return 'OK_SELECT';
                                    }}
                                }}
                                return 'SELECT_NO_MATCH';
                            }}

                            // For radio
                            var radios = container.querySelectorAll('input[type="radio"]');
                            if (radios.length > 0) {{
                                for (var r = 0; r < radios.length; r++) {{
                                    var radioLabel = container.querySelector('label[for="' + radios[r].id + '"]');
                                    if (radioLabel && radioLabel.textContent.indexOf('{escaped_answer}') >= 0) {{
                                        radios[r].click();
                                        return 'OK_RADIO';
                                    }}
                                }}
                                // Default: click first matching or first option
                                radios[0].click();
                                return 'OK_RADIO_DEFAULT';
                            }}

                            // For text/textarea (including shadow DOM)
                            var input = container.querySelector('input:not([type="hidden"]):not([type="file"]), textarea');
                            if (!input) {{
                                var splComp = container.querySelector('spl-input, spl-textarea');
                                if (splComp && splComp.shadowRoot) {{
                                    input = splComp.shadowRoot.querySelector('input, textarea');
                                }}
                            }}
                            if (input) {{
                                var proto = input.tagName === 'TEXTAREA'
                                    ? window.HTMLTextAreaElement.prototype
                                    : window.HTMLInputElement.prototype;
                                var setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                                setter.call(input, '{escaped_answer}');
                                input.dispatchEvent(new Event('input', {{bubbles: true, composed: true}}));
                                input.dispatchEvent(new Event('change', {{bubbles: true, composed: true}}));
                                return 'OK_TEXT';
                            }}

                            return 'NO_INPUT_FOUND';
                        }})()
                    """)

                    if fill_result and fill_result.startswith("OK"):
                        logger.info(f"Answered screening Q: {question_text[:40]}... -> {answer[:30]}")
                    else:
                        logger.debug(f"Screening Q fill result: {fill_result}")

                except Exception as e:
                    logger.debug(f"Error answering screening question '{question_text[:40]}': {e}")

        except Exception as e:
            logger.debug(f"Error detecting screening questions: {e}")

    async def _nd_validate(self, nd_page) -> bool:
        """Validate form fill quality in dry-run mode (nodriver)."""
        logger.info("DRY RUN: Validating SmartRecruiters form fill...")

        try:
            result = await nd_page.evaluate("""
                (function() {
                    var fields = {};
                    var ids = [
                        'first-name-input', 'last-name-input',
                        'email-input', 'confirm-email-input',
                        'linkedin-input', 'website-input'
                    ];
                    for (var i = 0; i < ids.length; i++) {
                        var host = document.querySelector('#' + ids[i]);
                        if (host && host.shadowRoot) {
                            var input = host.shadowRoot.querySelector('input');
                            if (input && input.value) {
                                fields[ids[i]] = input.value;
                            }
                        }
                    }

                    // Check phone
                    var phone = document.querySelector('spl-phone-field');
                    if (phone && phone.shadowRoot) {
                        function findTel(root) {
                            var inp = root.querySelector('input[type="tel"]');
                            if (inp) return inp;
                            var hosts = root.querySelectorAll('*');
                            for (var j = 0; j < hosts.length; j++) {
                                if (hosts[j].shadowRoot) {
                                    var found = findTel(hosts[j].shadowRoot);
                                    if (found) return found;
                                }
                            }
                            return null;
                        }
                        var telInput = findTel(phone.shadowRoot);
                        if (telInput && telInput.value) {
                            fields['phone'] = telInput.value;
                        }
                    }

                    // Check message
                    var msg = document.querySelector('spl-textarea');
                    if (msg && msg.shadowRoot) {
                        var ta = msg.shadowRoot.querySelector('textarea');
                        if (ta && ta.value) {
                            fields['message'] = ta.value.substring(0, 50);
                        }
                    }

                    // Check file
                    var dz = document.querySelector('spl-dropzone');
                    if (dz && dz.shadowRoot) {
                        var fileInput = dz.shadowRoot.querySelector('input[type="file"]');
                        if (fileInput && fileInput.files && fileInput.files.length > 0) {
                            fields['resume'] = fileInput.files[0].name;
                        }
                    }

                    return JSON.stringify(fields);
                })()
            """)

            import json
            filled = json.loads(result) if isinstance(result, str) else {}

            logger.info(f"DRY RUN: Filled fields: {list(filled.keys())}")
            for k, v in filled.items():
                logger.info(f"  {k}: {v[:40] if isinstance(v, str) else v}")

            core = ["first-name-input", "last-name-input", "email-input"]
            core_filled = sum(1 for c in core if c in filled)
            logger.info(f"DRY RUN: Core fields: {core_filled}/3")

            passed = core_filled >= 2
            if passed:
                logger.info("DRY RUN: SmartRecruiters validation PASSED")
            else:
                logger.warning("DRY RUN: SmartRecruiters validation FAILED")

            return passed

        except Exception as e:
            logger.error(f"Validation error: {e}")
            return False

    async def _nd_submit(self, nd_page) -> bool:
        """Click the submit button in the oneclick-ui form."""
        try:
            # Find submit button text
            btn = await nd_page.find("Submit", best_match=True)
            if btn:
                await btn.click()
                logger.info("Clicked submit button")
                return True
        except Exception as e:
            logger.debug(f"Submit button find failed: {e}")

        # Fallback: click the spl-button that contains the submit
        try:
            result = await nd_page.evaluate("""
                (function() {
                    var buttons = document.querySelectorAll('spl-button');
                    for (var i = 0; i < buttons.length; i++) {
                        var text = buttons[i].textContent || '';
                        if (text.toLowerCase().indexOf('submit') >= 0 ||
                            text.toLowerCase().indexOf('apply') >= 0) {
                            if (buttons[i].shadowRoot) {
                                var btn = buttons[i].shadowRoot.querySelector('button');
                                if (btn) {
                                    btn.click();
                                    return 'CLICKED';
                                }
                            }
                            buttons[i].click();
                            return 'CLICKED';
                        }
                    }
                    return 'NOT_FOUND';
                })()
            """)
            if result == "CLICKED":
                logger.info("Clicked submit via Shadow DOM")
                return True
        except Exception as e:
            logger.warning(f"Submit error: {e}")

        return False
