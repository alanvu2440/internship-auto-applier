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

            # Check if redirected to expired/closed page
            current_url = str(nd_page.url) if hasattr(nd_page, 'url') else ""
            if "expired" in current_url.lower():
                logger.info("Job expired (URL contains 'expired')")
                self._last_status = "closed"
                return False

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

            config = self.form_filler.config

            # Handle screening/custom questions with AI (before field fill)
            await self._nd_handle_screening_questions(nd_page, job_data)

            # Fill the form fields
            filled = await self._nd_fill_form(nd_page, config, job_data)

            # Upload resume LAST — after form fill so Angular re-renders don't clear the dropzone
            resume_path = config.get("files", {}).get("resume")
            if resume_path:
                try:
                    await asyncio.wait_for(
                        self._nd_upload_resume(nd_page, resume_path),
                        timeout=30
                    )
                except asyncio.TimeoutError:
                    logger.warning("Resume upload timed out after 30s — continuing without resume")
            if not filled:
                return False

            # Debug: screenshot after fill
            try:
                company = job_data.get("company", "unknown").replace(" ", "_")[:30]
                ts = __import__("datetime").datetime.now().strftime("%H%M%S")
                await nd_page.save_screenshot(f"data/screenshots/SR_FILLED_{company}_{ts}.png")
            except Exception:
                pass

            # Dry run: validate and return
            if self.dry_run:
                logger.info("DRY RUN: Form filled, running validation")
                validation = await self._nd_validate(nd_page)
                self._last_status = "success" if validation else "failed"
                return validation

            # Log all visible buttons before submit for debugging
            try:
                buttons_info = await nd_page.evaluate("""
                    (function() {
                        var info = [];
                        var all = document.querySelectorAll('button, a, spl-button, [role="button"], input[type="submit"]');
                        for (var i = 0; i < all.length; i++) {
                            var el = all[i];
                            var text = (el.textContent || '').trim().substring(0, 50);
                            var rect = el.getBoundingClientRect();
                            var visible = rect.width > 0 && rect.height > 0;
                            if (text && visible) info.push(text);
                        }
                        // Also check for consent/privacy checkboxes
                        var checks = document.querySelectorAll('input[type="checkbox"], spl-checkbox');
                        for (var j = 0; j < checks.length; j++) {
                            var label = (checks[j].closest('label') || checks[j].parentElement || {}).textContent || '';
                            info.push('CHECKBOX: ' + label.trim().substring(0, 80));
                        }
                        return JSON.stringify(info);
                    })()
                """)
                if buttons_info and isinstance(buttons_info, str):
                    import json as _btn_json
                    btns = _btn_json.loads(buttons_info)
                    logger.info(f"Visible buttons/elements before submit: {btns[:15]}")
            except Exception as btn_e:
                logger.debug(f"Button scan failed: {btn_e}")

            # Dump all field values right before submit for debugging
            try:
                field_dump = await nd_page.evaluate("""
                    (function() {
                        var dump = {};
                        // Check spl-input fields
                        var splInputs = document.querySelectorAll('spl-input');
                        for (var i = 0; i < splInputs.length; i++) {
                            var id = splInputs[i].id || 'spl-input-' + i;
                            var req = splInputs[i].hasAttribute('required') ? '*' : '';
                            if (splInputs[i].shadowRoot) {
                                var inp = splInputs[i].shadowRoot.querySelector('input');
                                dump[id + req] = inp ? inp.value : '(no inner input)';
                            } else {
                                dump[id + req] = '(no shadow)';
                            }
                        }
                        // Check spl-phone-field
                        var phone = document.querySelector('spl-phone-field');
                        if (phone && phone.shadowRoot) {
                            function findTel(root) {
                                var inp = root.querySelector('input[type="tel"]');
                                if (inp) return inp;
                                var all = root.querySelectorAll('*');
                                for (var j = 0; j < all.length; j++) {
                                    if (all[j].shadowRoot) { var f = findTel(all[j].shadowRoot); if (f) return f; }
                                }
                                return null;
                            }
                            var tel = findTel(phone.shadowRoot);
                            dump['phone' + (phone.hasAttribute('required') ? '*' : '')] = tel ? tel.value : '(no tel)';
                        }
                        // Check spl-autocomplete (city)
                        var ac = document.querySelector('spl-autocomplete');
                        if (ac && ac.shadowRoot) {
                            var acInp = ac.shadowRoot.querySelector('input');
                            dump['city' + (ac.hasAttribute('required') ? '*' : '')] = acInp ? acInp.value : '(no input)';
                        }
                        // Check spl-dropzone (resume)
                        var dz = document.querySelector('spl-dropzone');
                        if (dz) {
                            var req = dz.hasAttribute('required') ? '*' : '';
                            var fileName = dz.getAttribute('file-name') || '';
                            if (dz.shadowRoot) {
                                var fi = dz.shadowRoot.querySelector('input[type="file"]');
                                var hasFile = fi && fi.files && fi.files.length > 0;
                                dump['resume' + req] = hasFile ? fi.files[0].name : ('no-file, attr=' + fileName);
                            }
                        }
                        // Check spl-textarea
                        var ta = document.querySelector('spl-textarea');
                        if (ta && ta.shadowRoot) {
                            var taInner = ta.shadowRoot.querySelector('textarea');
                            dump['message'] = taInner ? taInner.value.substring(0, 30) : '(no textarea)';
                        }
                        // Check for Angular validation errors
                        var errors = document.querySelectorAll('[class*="error"], [class*="invalid"], .ng-invalid');
                        var errTexts = [];
                        for (var e = 0; e < errors.length; e++) {
                            var t = (errors[e].textContent || '').trim();
                            if (t && t.length < 100 && t.length > 2) errTexts.push(t.substring(0, 60));
                        }
                        if (errTexts.length > 0) dump['ERRORS'] = errTexts.slice(0, 5).join(' | ');
                        return JSON.stringify(dump);
                    })()
                """)
                if field_dump:
                    import json as _dump_json
                    logger.info(f"PRE-SUBMIT field dump: {_dump_json.loads(field_dump) if isinstance(field_dump, str) else field_dump}")
            except Exception as dump_e:
                logger.info(f"Field dump failed: {dump_e}")

            # Re-fill any spl-input fields that got cleared by phone/city/resume fills
            # Angular re-renders shadow DOM components, clearing typed values
            config = self.form_filler.config
            personal = config.get("personal_info", {})
            refill_fields = {
                "first-name-input": personal.get("first_name", ""),
                "last-name-input": personal.get("last_name", ""),
                "email-input": personal.get("email", ""),
                "confirm-email-input": personal.get("email", ""),
                "linkedin-input": personal.get("linkedin", ""),
                "website-input": personal.get("portfolio", "") or personal.get("github", ""),
            }
            import nodriver.cdp as cdp
            for element_id, value in refill_fields.items():
                if not value:
                    continue
                try:
                    current_val = await nd_page.evaluate(f"""
                        (function() {{
                            var host = document.querySelector('#{element_id}');
                            if (!host || !host.shadowRoot) return '';
                            var input = host.shadowRoot.querySelector('input');
                            return input ? input.value : '';
                        }})()
                    """)
                    if not current_val or len(str(current_val).strip()) < 2:
                        logger.info(f"Re-filling {element_id} (cleared by Angular)")
                        await self._nd_cdp_type_into_shadow(
                            nd_page, f"#{element_id}", value, input_selector='input'
                        )
                        await asyncio.sleep(0.5)
                except Exception:
                    pass

            # Navigate multi-step form: click Next through steps, then Submit
            submitted = await self._nd_handle_multistep_submit(nd_page, job_data)
            if submitted:
                self._last_status = "success"
                return True

            logger.warning("SmartRecruiters form submission failed — multi-step navigation exhausted")
            return False

        except Exception as e:
            logger.error(f"SmartRecruiters application failed: {e}")
            return False
        finally:
            # Only close nodriver browser on SUCCESS — on failure, leave open for manual help
            if nd_browser and self._last_status == "success":
                try:
                    nd_browser.stop()
                except Exception as e:
                    logger.debug(f"Error closing nodriver browser: {e}")
            elif nd_browser:
                logger.info("[BROWSER] SmartRecruiters nodriver browser left OPEN for manual help")

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

        # Wait for Simplify extension to autofill first (if loaded)
        import asyncio as _asyncio
        await _asyncio.sleep(3)

        # Check what's already filled by the extension — DON'T overwrite those
        prefilled = await nd_page.evaluate("""
            (function() {
                var filled = {};
                // Check spl-input fields
                document.querySelectorAll('spl-input').forEach(function(host) {
                    var inp = host.shadowRoot ? host.shadowRoot.querySelector('input') : null;
                    if (inp && inp.value && inp.value.trim()) {
                        filled[host.id || host.getAttribute('name') || 'unknown'] = inp.value;
                    }
                });
                // Check phone
                var phone = document.querySelector('spl-phone-field');
                if (phone && phone.shadowRoot) {
                    var telInp = phone.shadowRoot.querySelector('input[type="tel"]');
                    if (!telInp) {
                        var inner = phone.shadowRoot.querySelector('spl-input');
                        if (inner && inner.shadowRoot) telInp = inner.shadowRoot.querySelector('input');
                    }
                    if (telInp && telInp.value && telInp.value.trim()) filled['phone'] = telInp.value;
                }
                // Check city autocomplete
                var ac = document.querySelector('spl-autocomplete');
                if (ac && ac.shadowRoot) {
                    var acInp = ac.shadowRoot.querySelector('input');
                    if (!acInp) {
                        var inner = ac.shadowRoot.querySelector('spl-input');
                        if (inner && inner.shadowRoot) acInp = inner.shadowRoot.querySelector('input');
                    }
                    if (acInp && acInp.value && acInp.value.trim()) filled['city'] = acInp.value;
                }
                // Check resume/dropzone
                var dz = document.querySelector('spl-dropzone');
                if (dz && dz.shadowRoot) {
                    var fileLabel = dz.shadowRoot.querySelector('.file-name, [class*="file"]');
                    if (fileLabel && fileLabel.textContent.trim()) filled['resume'] = fileLabel.textContent.trim();
                }
                return filled;
            })()
        """)
        logger.info(f"Extension pre-filled: {prefilled}")

        # Fill order: Phone → City → Message → spl-input fields (LAST)
        # Phone/city use JS focus + CDP insertText which triggers Angular re-renders
        # that clear spl-input values. So spl-inputs must be filled AFTER phone/city.
        import nodriver.cdp as cdp
        fill_results = {}

        # Fill phone — skip if extension already filled it
        phone = personal.get("phone", "").replace("-", "").replace(" ", "").replace("+1", "").replace("+", "")
        if phone and 'phone' not in prefilled:
            try:
                # JS focus + CDP insertText is the ONLY approach that works for spl-phone-field.
                # CDP char-by-char key events and mouse-click approaches don't persist values
                # because Angular's ControlValueAccessor on the nested component doesn't process them.
                phone_filled = await self._nd_click_and_type_phone(nd_page, phone)

                if phone_filled:
                    fill_results["phone"] = True
                    logger.info("Filled phone via CDP")
                else:
                    logger.warning("Phone fill failed via all methods")
            except Exception as e:
                logger.debug(f"Error filling phone: {e}")

        # Fill hiring manager message (spl-textarea) via CDP typing
        company = job_data.get("company", "your company")
        message = (
            f"I am excited to apply for this position at {company}. "
            f"I believe my experience and skills make me a strong candidate."
        )
        try:
            msg_filled = await self._nd_cdp_type_into_shadow(
                nd_page, "spl-textarea", message, input_selector='textarea'
            )
            if msg_filled:
                fill_results["message"] = True
                logger.debug("Filled hiring manager message via CDP typing")
            else:
                logger.debug("Message CDP typing failed")
        except Exception as e:
            logger.debug(f"Error filling message: {e}")

        # Fill City/location autocomplete — skip if extension already filled it
        city = personal.get("city", "") or personal.get("location", "")
        if city and 'city' not in prefilled:
            try:
                # Step 1: JS focus + insertText into the autocomplete input
                city_focus = await nd_page.evaluate("""
                    (function() {
                        var host = document.querySelector('spl-autocomplete');
                        if (!host || !host.shadowRoot) return 'NO_HOST';
                        function findInput(root) {
                            var inp = root.querySelector('input');
                            if (inp) return inp;
                            var all = root.querySelectorAll('*');
                            for (var i = 0; i < all.length; i++) {
                                if (all[i].shadowRoot) {
                                    var f = findInput(all[i].shadowRoot);
                                    if (f) return f;
                                }
                            }
                            return null;
                        }
                        var input = findInput(host.shadowRoot);
                        if (!input) return 'NO_INPUT';
                        // Scroll into view
                        host.scrollIntoView({behavior: 'instant', block: 'center'});
                        // Clear and focus
                        input.value = '';
                        input.focus();
                        return 'FOCUSED';
                    })()
                """)
                logger.info(f"City JS focus: {city_focus}")

                if city_focus == 'FOCUSED':
                    await asyncio.sleep(0.3)
                    # Type city char-by-char with CDP dispatchKeyEvent
                    # (insertText doesn't trigger Angular change detection)
                    for char in city:
                        await nd_page.send(cdp.input_.dispatch_key_event(
                            type_="keyDown", key=char, code="Key" + char.upper() if char.isalpha() else "Space"))
                        await nd_page.send(cdp.input_.dispatch_key_event(
                            type_="char", text=char, key=char))
                        await nd_page.send(cdp.input_.dispatch_key_event(
                            type_="keyUp", key=char, code="Key" + char.upper() if char.isalpha() else "Space"))
                        await asyncio.sleep(0.05)
                    await asyncio.sleep(2)  # Wait for autocomplete suggestions

                    # ArrowDown + Enter to select first suggestion
                    await nd_page.send(cdp.input_.dispatch_key_event(
                        type_="keyDown", key="ArrowDown", code="ArrowDown"))
                    await nd_page.send(cdp.input_.dispatch_key_event(
                        type_="keyUp", key="ArrowDown", code="ArrowDown"))
                    await asyncio.sleep(0.3)
                    await nd_page.send(cdp.input_.dispatch_key_event(
                        type_="keyDown", key="Enter", code="Enter"))
                    await nd_page.send(cdp.input_.dispatch_key_event(
                        type_="keyUp", key="Enter", code="Enter"))
                    await asyncio.sleep(0.5)

                    # Verify
                    city_verify = await nd_page.evaluate("""
                        (function() {
                            var host = document.querySelector('spl-autocomplete');
                            if (host && host.shadowRoot) {
                                var input = host.shadowRoot.querySelector('input');
                                if (input) return input.value;
                                // Try nested
                                function findInput(root) {
                                    var inp = root.querySelector('input');
                                    if (inp) return inp;
                                    var all = root.querySelectorAll('*');
                                    for (var i = 0; i < all.length; i++) {
                                        if (all[i].shadowRoot) {
                                            var f = findInput(all[i].shadowRoot);
                                            if (f) return f;
                                        }
                                    }
                                    return null;
                                }
                                var inp2 = findInput(host.shadowRoot);
                                if (inp2) return inp2.value;
                            }
                            return '';
                        })()
                    """)
                    logger.info(f"City value after insertText+ArrowDown+Enter: '{city_verify}'")

                    # Dispatch events so Angular picks up the city value
                    if city_verify and len(str(city_verify)) > 2:
                        await nd_page.evaluate("""
                            (function() {
                                var host = document.querySelector('spl-autocomplete');
                                if (!host || !host.shadowRoot) return;
                                function findInput(root) {
                                    var inp = root.querySelector('input');
                                    if (inp) return inp;
                                    var all = root.querySelectorAll('*');
                                    for (var i = 0; i < all.length; i++) {
                                        if (all[i].shadowRoot) {
                                            var f = findInput(all[i].shadowRoot);
                                            if (f) return f;
                                        }
                                    }
                                    return null;
                                }
                                var input = findInput(host.shadowRoot);
                                if (input) {
                                    input.dispatchEvent(new Event('input', {bubbles: true, composed: true}));
                                    input.dispatchEvent(new Event('change', {bubbles: true, composed: true}));
                                    input.dispatchEvent(new Event('blur', {bubbles: true, composed: true}));
                                }
                            })()
                        """)
                        await asyncio.sleep(0.2)

                    if city_verify and len(str(city_verify)) > 2:
                        fill_results["city"] = True
                    else:
                        # Fallback: click suggestion elements inside spl-autocomplete shadow
                        select_result = await nd_page.evaluate("""
                            (function() {
                                function findSuggestions(root) {
                                    var items = root.querySelectorAll(
                                        'li, [role="option"], [role="listbox"] > *'
                                    );
                                    for (var i = 0; i < items.length; i++) {
                                        var r = items[i].getBoundingClientRect();
                                        if (r.width > 0 && r.height > 0 && r.height < 100) {
                                            items[i].click();
                                            return 'CLICKED:' + (items[i].textContent || '').trim().substring(0, 40);
                                        }
                                    }
                                    var hosts = root.querySelectorAll('*');
                                    for (var j = 0; j < hosts.length; j++) {
                                        if (hosts[j].shadowRoot) {
                                            var result = findSuggestions(hosts[j].shadowRoot);
                                            if (result !== 'NONE') return result;
                                        }
                                    }
                                    return 'NONE';
                                }
                                var host = document.querySelector('spl-autocomplete');
                                if (host && host.shadowRoot) {
                                    return findSuggestions(host.shadowRoot);
                                }
                                return 'NO_HOST';
                            })()
                        """)
                        logger.info(f"City fallback click: {select_result}")
                        fill_results["city"] = select_result and 'CLICKED' in str(select_result)
            except Exception as e:
                logger.info(f"Error filling city: {e}")

        # === PHASE 2: Fill spl-input fields LAST (after phone/city/message) ===
        # Skip fields already filled by the Simplify extension
        for element_id, value in fields.items():
            if not value:
                continue
            # Check if extension already filled this field
            if element_id in prefilled:
                logger.info(f"Skipping {element_id} — already filled by extension: '{prefilled[element_id][:30]}'")
                fill_results[element_id] = True
                continue
            try:
                filled = await self._nd_cdp_type_into_shadow(
                    nd_page, f"#{element_id}", value, input_selector='input'
                )
                fill_results[element_id] = filled
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.debug(f"Error filling {element_id}: {e}")
                fill_results[element_id] = False

        # Click neutral to trigger blur, then verify
        try:
            await nd_page.evaluate("document.querySelector('h2, h3, .section-title, body').click()")
        except Exception:
            pass
        await asyncio.sleep(1)

        # Verify and re-fill any spl-input fields that got cleared
        for element_id, value in fields.items():
            if not value:
                continue
            try:
                current_val = await nd_page.evaluate(f"""
                    (function() {{
                        var host = document.querySelector('#{element_id}');
                        if (!host || !host.shadowRoot) return '';
                        var input = host.shadowRoot.querySelector('input');
                        return input ? input.value : '';
                    }})()
                """)
                if not current_val or len(str(current_val).strip()) < 2:
                    logger.info(f"Re-filling {element_id} (was cleared by Angular)")
                    filled = await self._nd_cdp_type_into_shadow(
                        nd_page, f"#{element_id}", value, input_selector='input'
                    )
                    fill_results[element_id] = filled
                    await asyncio.sleep(0.5)
                else:
                    fill_results[element_id] = True
            except Exception as e:
                logger.debug(f"Error verifying {element_id}: {e}")

        # === PHASE 3: Detect and fill unknown spl-input fields (spl-form-element_* IDs) ===
        # These are custom company-specific fields not in our standard fields dict
        known_ids = set(fields.keys()) | {"phone", "city", "message", "resume"}
        try:
            unknown_fields = await nd_page.evaluate("""
                (function() {
                    var result = [];
                    var splInputs = document.querySelectorAll('spl-input');
                    for (var i = 0; i < splInputs.length; i++) {
                        var id = splInputs[i].id || '';
                        if (!id) continue;
                        // Get label from multiple sources
                        var label = splInputs[i].getAttribute('label') || splInputs[i].getAttribute('aria-label') || '';
                        if (!label && splInputs[i].shadowRoot) {
                            var lbl = splInputs[i].shadowRoot.querySelector('label, .label, [class*="label"]');
                            if (lbl) label = lbl.textContent.trim();
                            if (!label) {
                                var ph = splInputs[i].shadowRoot.querySelector('input');
                                if (ph) label = ph.getAttribute('placeholder') || '';
                            }
                        }
                        // Get current value
                        var val = '';
                        if (splInputs[i].shadowRoot) {
                            var inp = splInputs[i].shadowRoot.querySelector('input');
                            val = inp ? inp.value.trim() : '';
                        }
                        var req = splInputs[i].hasAttribute('required');
                        result.push({id: id, label: label, value: val, required: req});
                    }
                    return result;
                })()
            """)
            if unknown_fields:
                for field_info in unknown_fields:
                    fid = field_info.get('id', '')
                    if fid in known_ids:
                        continue
                    if field_info.get('value'):
                        continue  # Already filled
                    label = field_info.get('label', '').lower()
                    required = field_info.get('required', False)
                    # Map common label patterns to config values
                    answer = None
                    personal = config.get("personal_info", {})
                    if any(x in label for x in ["preferred name", "nickname", "preferred first"]):
                        answer = personal.get("preferred_name") or personal.get("first_name", "")
                    elif any(x in label for x in ["middle name", "middle initial"]):
                        answer = personal.get("middle_name", "")
                    elif any(x in label for x in ["pronouns", "pronoun"]):
                        answer = config.get("demographics", {}).get("pronouns", "He/Him")
                    elif any(x in label for x in ["hear about", "referral", "source", "how did you"]):
                        answer = config.get("common_answers", {}).get("how_did_you_hear", "Online Job Board")
                    elif any(x in label for x in ["salary", "compensation", "pay"]):
                        answer = config.get("common_answers", {}).get("salary_expectations", "Open to discuss")
                    elif any(x in label for x in ["portfolio", "personal site", "personal website"]):
                        answer = personal.get("portfolio") or personal.get("github", "")
                    elif required:
                        # Unknown required field — use first_name as safe fallback for text fields
                        logger.warning(f"Unknown required spl-input field '{fid}' (label='{label}') — skipping")
                        continue
                    if answer:
                        try:
                            filled = await self._nd_cdp_type_into_shadow(
                                nd_page, f"#{fid}", answer, input_selector='input'
                            )
                            logger.info(f"Filled unknown field '{fid}' (label='{label}') = '{answer[:30]}': {filled}")
                            fill_results[fid] = filled
                            await asyncio.sleep(0.3)
                        except Exception as e:
                            logger.debug(f"Error filling unknown field {fid}: {e}")
        except Exception as e:
            logger.debug(f"Error detecting unknown spl-input fields: {e}")

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

    async def _nd_click_and_type_phone(self, nd_page, phone: str) -> bool:
        """Fill phone by JS-focusing the tel input, then CDP Input.insertText.

        The spl-phone-field has nested shadow DOM:
          spl-phone-field → shadow → [country-code-selector (has input[type="text"])]
                                      [spl-input or similar → shadow → input[type="tel"]]
        We must search ONLY for input[type="tel"] to avoid hitting the country code text input.
        """
        import nodriver.cdp as cdp

        try:
            # Use JS to find and focus ONLY the tel input (not the country code text input)
            focus_result = await nd_page.evaluate("""
                (function() {
                    var host = document.querySelector('spl-phone-field');
                    if (!host || !host.shadowRoot) return 'NO_HOST';
                    // ONLY search for input[type="tel"] — do NOT fall back to generic inputs
                    function findTelInput(root) {
                        if (!root) return null;
                        var inp = root.querySelector('input[type="tel"]');
                        if (inp) return inp;
                        // Recurse into nested shadow roots
                        var all = root.querySelectorAll('*');
                        for (var i = 0; i < all.length; i++) {
                            if (all[i].shadowRoot) {
                                var found = findTelInput(all[i].shadowRoot);
                                if (found) return found;
                            }
                        }
                        return null;
                    }
                    var input = findTelInput(host.shadowRoot);
                    if (!input) return 'NO_TEL_INPUT';
                    // Clear any existing value
                    input.value = '';
                    // Focus it via JS
                    input.focus();
                    return 'FOCUSED:' + input.type + ':' + input.tagName;
                })()
            """)

            logger.info(f"Phone JS focus result: {focus_result}")

            if not focus_result or 'FOCUSED' not in str(focus_result):
                return False

            await asyncio.sleep(0.3)

            # Type char-by-char with CDP dispatchKeyEvent — triggers real browser keyboard
            # events that Angular's zone.js picks up (insertText does NOT trigger these)
            for char in phone:
                await nd_page.send(cdp.input_.dispatch_key_event(
                    type_="keyDown", key=char, code=f"Digit{char}" if char.isdigit() else "Space"))
                await nd_page.send(cdp.input_.dispatch_key_event(
                    type_="char", text=char, key=char))
                await nd_page.send(cdp.input_.dispatch_key_event(
                    type_="keyUp", key=char, code=f"Digit{char}" if char.isdigit() else "Space"))
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.5)

            # Tab out to trigger blur/validation on the Angular component
            await nd_page.send(cdp.input_.dispatch_key_event(
                type_="keyDown", key="Tab", code="Tab"))
            await nd_page.send(cdp.input_.dispatch_key_event(
                type_="keyUp", key="Tab", code="Tab"))
            await asyncio.sleep(0.3)

            # Verify the value was set on the tel input specifically
            verify = await nd_page.evaluate("""
                (function() {
                    var host = document.querySelector('spl-phone-field');
                    if (!host || !host.shadowRoot) return 'NO_HOST';
                    function findTelInput(root) {
                        if (!root) return null;
                        var inp = root.querySelector('input[type="tel"]');
                        if (inp) return inp;
                        var all = root.querySelectorAll('*');
                        for (var i = 0; i < all.length; i++) {
                            if (all[i].shadowRoot) {
                                var found = findTelInput(all[i].shadowRoot);
                                if (found) return found;
                            }
                        }
                        return null;
                    }
                    var input = findTelInput(host.shadowRoot);
                    return input ? ('TEL_VALUE:' + input.value) : 'NOT_FOUND';
                })()
            """)
            logger.info(f"Phone value after typing: '{verify}'")

            return bool(verify and 'TEL_VALUE:' in str(verify) and len(str(verify)) > 15)

        except Exception as e:
            logger.info(f"Click-and-type phone failed: {e}")
            return False

    async def _nd_cdp_type_into_shadow(
        self, nd_page, host_selector: str, text: str,
        input_selector: str = 'input', js_finder: str = None
    ) -> bool:
        """Type into a shadow DOM input using hybrid JS-find + CDP-focus + CDP-type.

        Strategy:
        1. JS runtime.evaluate finds the input (JS is great at recursive shadow traversal)
        2. CDP DOM.requestNode converts RemoteObject → nodeId
        3. CDP DOM.focus gives REAL browser-level focus (not just JS focus)
        4. CDP Input.insertText sends real keyboard input

        This triggers Angular's event listeners naturally.
        """
        import nodriver.cdp as cdp

        try:
            # Step 1: Use JS to find the shadow DOM input element
            # JS can recursively traverse shadow roots easily
            if js_finder:
                js_code = js_finder
            else:
                js_code = f"""
                    (function() {{
                        var host = document.querySelector('{host_selector}');
                        if (!host) return null;
                        // Recursive shadow DOM search — first pass: exact selector only
                        function findExact(root) {{
                            if (!root) return null;
                            var inp = root.querySelector('{input_selector}');
                            if (inp) return inp;
                            var all = root.querySelectorAll('*');
                            for (var i = 0; i < all.length; i++) {{
                                if (all[i].shadowRoot) {{
                                    var found = findExact(all[i].shadowRoot);
                                    if (found) return found;
                                }}
                            }}
                            return null;
                        }}
                        var result = host.shadowRoot ? findExact(host.shadowRoot) : null;
                        if (result) return result;
                        // Second pass: any visible input (fallback)
                        function findAny(root) {{
                            if (!root) return null;
                            var inp = root.querySelector('input:not([type="hidden"]):not([type="checkbox"]):not([type="radio"])');
                            if (inp) return inp;
                            var all = root.querySelectorAll('*');
                            for (var i = 0; i < all.length; i++) {{
                                if (all[i].shadowRoot) {{
                                    var found = findAny(all[i].shadowRoot);
                                    if (found) return found;
                                }}
                            }}
                            return null;
                        }}
                        return host.shadowRoot ? findAny(host.shadowRoot) : null;
                    }})()
                """

            # Get RemoteObject reference to the input element
            result = await nd_page.send(
                cdp.runtime.evaluate(
                    expression=js_code,
                    return_by_value=False,
                )
            )
            remote_obj = result[0] if isinstance(result, tuple) else result

            if not remote_obj or not hasattr(remote_obj, 'object_id') or not remote_obj.object_id:
                logger.info(f"CDP type: element not found via JS for '{host_selector}' (type={type(remote_obj).__name__})")
                return False

            logger.info(f"CDP type: found element for '{host_selector}', object_id={remote_obj.object_id[:30]}...")

            # Step 2: Convert RemoteObject → DOM nodeId
            # Need DOM.enable() first so DOM.requestNode works
            try:
                await nd_page.send(cdp.dom.enable())
            except Exception:
                pass  # Already enabled

            node_id = await nd_page.send(
                cdp.dom.request_node(object_id=remote_obj.object_id)
            )
            if not node_id:
                logger.debug(f"CDP type: could not get nodeId for '{host_selector}'")
                return False

            logger.info(f"CDP type: got nodeId={node_id} for '{host_selector}'")

            # Step 3: Focus via CDP DOM.focus (gives REAL browser-level focus)
            await nd_page.send(cdp.dom.focus(node_id=node_id))
            await asyncio.sleep(0.2)
            logger.info(f"CDP type: focused nodeId={node_id}")

            # Step 4: Clear any existing value via JS (Cmd+A doesn't work with delegatesFocus)
            await nd_page.evaluate(f"""
                (function() {{
                    var host = document.querySelector('{host_selector}');
                    if (host && host.shadowRoot) {{
                        var input = host.shadowRoot.querySelector('{input_selector}');
                        if (input) {{
                            input.value = '';
                            input.dispatchEvent(new Event('input', {{bubbles: true}}));
                        }}
                    }}
                    // Also try findExact for nested shadow DOM
                    function findExact(root) {{
                        if (!root) return null;
                        var inp = root.querySelector('{input_selector}');
                        if (inp) return inp;
                        var all = root.querySelectorAll('*');
                        for (var i = 0; i < all.length; i++) {{
                            if (all[i].shadowRoot) {{ var f = findExact(all[i].shadowRoot); if (f) return f; }}
                        }}
                        return null;
                    }}
                    if (host && host.shadowRoot) {{
                        var inp = findExact(host.shadowRoot);
                        if (inp) {{ inp.value = ''; inp.dispatchEvent(new Event('input', {{bubbles: true}})); }}
                    }}
                }})()
            """)
            await asyncio.sleep(0.1)

            # Step 5: Type text character by character using CDP key events
            # keyDown (no text!) → char (with text) → keyUp
            for char in text:
                await nd_page.send(cdp.input_.dispatch_key_event(
                    type_="keyDown", key=char,
                ))
                await nd_page.send(cdp.input_.dispatch_key_event(
                    type_="char", text=char, key=char,
                ))
                await nd_page.send(cdp.input_.dispatch_key_event(
                    type_="keyUp", key=char,
                ))
            await asyncio.sleep(0.3)

            logger.info(f"CDP typed '{text[:30]}' char-by-char into {host_selector} > {input_selector}")
            return True

        except Exception as e:
            logger.info(f"CDP type into shadow failed ({host_selector}): {e}")
            return False

    async def _nd_upload_resume(self, nd_page, resume_path: str) -> bool:
        """Upload resume via the spl-dropzone Shadow DOM file input.

        Uses nodriver CDP API (cdp.runtime.evaluate + cdp.dom.set_file_input_files)
        to pierce shadow DOM and set file on the hidden input.
        """
        abs_path = str(Path(resume_path).resolve())
        if not os.path.exists(abs_path):
            logger.warning(f"Resume file not found: {abs_path}")
            return False

        import nodriver.cdp as cdp

        # Method A: CDP Runtime.evaluate to get remote object reference to shadow input,
        # then use DOM.setFileInputFiles with the object_id
        try:
            result = await nd_page.send(
                cdp.runtime.evaluate(
                    expression="""
                        (function() {
                            var dz = document.querySelector('spl-dropzone');
                            if (dz && dz.shadowRoot) {
                                var fi = dz.shadowRoot.querySelector('input[type="file"]');
                                if (fi) return fi;
                            }
                            // Fallback: any file input on page
                            var all = document.querySelectorAll('input[type="file"]');
                            return all.length > 0 ? all[0] : null;
                        })()
                    """,
                    user_gesture=True,
                )
            )
            # runtime.evaluate returns (RemoteObject, Optional[ExceptionDetails])
            remote_obj = result[0] if isinstance(result, tuple) else result
            if remote_obj and hasattr(remote_obj, 'object_id') and remote_obj.object_id:
                await nd_page.send(
                    cdp.dom.set_file_input_files(
                        files=[abs_path],
                        object_id=remote_obj.object_id,
                    )
                )
                logger.info("Resume uploaded via CDP Runtime.evaluate + setFileInputFiles")
                # Dispatch change event so spl-dropzone component detects the file
                await nd_page.evaluate("""
                    (function() {
                        var dz = document.querySelector('spl-dropzone');
                        if (dz && dz.shadowRoot) {
                            var fi = dz.shadowRoot.querySelector('input[type="file"]');
                            if (fi) {
                                fi.dispatchEvent(new Event('change', {bubbles: true, composed: true}));
                                fi.dispatchEvent(new Event('input', {bubbles: true, composed: true}));
                            }
                        }
                    })()
                """)
                await asyncio.sleep(2)
                return True
            else:
                logger.debug(f"Runtime.evaluate returned no object_id: {remote_obj}")
        except Exception as e1:
            logger.debug(f"Method A (CDP evaluate) failed: {e1}")

        # Method B: Use nodriver query_selector + update() to resolve object_id, then send_file
        try:
            file_input = await nd_page.query_selector("input[type='file']")
            if file_input:
                await file_input.update()  # resolves object_id via cdp.dom.resolve_node
                await file_input.send_file(abs_path)
                logger.info("Resume uploaded via query_selector + update + send_file")
                await asyncio.sleep(2)
                return True
        except Exception as e2:
            logger.debug(f"Method B (query_selector+update) failed: {e2}")

        # Method C: CDP DOM.getDocument(pierce=True) to traverse shadow DOM
        try:
            doc = await nd_page.send(cdp.dom.get_document(depth=-1, pierce=True))
            host_nid = await nd_page.send(
                cdp.dom.query_selector(node_id=doc.node_id, selector="spl-dropzone")
            )
            if host_nid:
                described_host = await nd_page.send(
                    cdp.dom.describe_node(node_id=host_nid, depth=1, pierce=True)
                )
                if described_host and described_host.shadow_roots:
                    sr_nid = described_host.shadow_roots[0].node_id
                    fi_nid = await nd_page.send(
                        cdp.dom.query_selector(
                            node_id=sr_nid, selector='input[type="file"]'
                        )
                    )
                    if fi_nid:
                        described_fi = await nd_page.send(
                            cdp.dom.describe_node(node_id=fi_nid)
                        )
                        await nd_page.send(
                            cdp.dom.set_file_input_files(
                                files=[abs_path],
                                backend_node_id=described_fi.backend_node_id,
                            )
                        )
                        logger.info("Resume uploaded via CDP DOM pierce + setFileInputFiles")
                        await asyncio.sleep(2)
                        return True
        except Exception as e3:
            logger.debug(f"Method C (DOM pierce) failed: {e3}")

        logger.warning("All resume upload methods failed")
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
            # Uses shadow-DOM-piercing search to find questions inside spl-* components
            questions_data = await nd_page.evaluate("""
                (function() {
                    // Recursively search through shadow roots
                    function findInShadow(root, selector) {
                        var results = Array.from(root.querySelectorAll(selector));
                        var allEls = root.querySelectorAll('*');
                        for (var i = 0; i < allEls.length; i++) {
                            if (allEls[i].shadowRoot) {
                                results = results.concat(findInShadow(allEls[i].shadowRoot, selector));
                            }
                        }
                        return results;
                    }

                    var questions = [];

                    // Look for screening question containers — pierce shadow DOM
                    var containers = findInShadow(document,
                        '.field, .question-item, [class*="screening"], [class*="question"]'
                    );
                    // Also check for standard form groups outside the known fields
                    if (containers.length === 0) {
                        containers = findInShadow(document, '.form-group, .form-field');
                    }

                    var knownIds = [
                        'first-name-input', 'last-name-input', 'email-input',
                        'confirm-email-input', 'linkedin-input', 'website-input',
                        'hiring-manager-message-input'
                    ];

                    for (var i = 0; i < containers.length; i++) {
                        var container = containers[i];
                        // Find label — also search inside shadow roots within this container
                        var label = container.querySelector('label, .label, legend');
                        if (!label) {
                            var shadowLabels = findInShadow(container, 'label, .label, legend');
                            if (shadowLabels.length > 0) label = shadowLabels[0];
                        }
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

                        // Find input — search through shadow roots of spl-* components
                        var input = container.querySelector(
                            'input:not([type="hidden"]):not([type="file"]):not([type="checkbox"]):not([type="radio"]), ' +
                            'textarea, select'
                        );

                        if (!input) {
                            // Deep search through all shadow DOMs in this container
                            var shadowInputs = findInShadow(container,
                                'input:not([type="hidden"]):not([type="file"]):not([type="checkbox"]):not([type="radio"]), ' +
                                'textarea, select'
                            );
                            if (shadowInputs.length > 0) input = shadowInputs[0];
                        }

                        if (!input) continue;

                        // Check if already filled
                        if (input.value && input.value.length > 2) continue;

                        // Also check for known IDs to skip
                        var parentId = (input.id || '');
                        if (knownIds.indexOf(parentId) >= 0) continue;
                        try {
                            var parentHost = input.getRootNode().host;
                            if (parentHost && knownIds.indexOf(parentHost.id || '') >= 0) continue;
                        } catch(e) {}

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

                    // Also check for radio button groups — pierce shadow DOM
                    var radios = findInShadow(document, '[class*="radio-group"], fieldset, spl-radio, spl-checkbox');
                    for (var r = 0; r < radios.length; r++) {
                        var legend = radios[r].querySelector('legend, label, .label');
                        if (!legend) {
                            var shadowLegends = findInShadow(radios[r], 'legend, label, .label');
                            if (shadowLegends.length > 0) legend = shadowLegends[0];
                        }
                        if (!legend) continue;
                        var legendText = (legend.textContent || '').trim();
                        if (!legendText || legendText.length < 3) continue;

                        // Find radio inputs — including inside shadow roots
                        var radioInputs = radios[r].querySelectorAll('input[type="radio"]');
                        if (radioInputs.length === 0) {
                            radioInputs = findInShadow(radios[r], 'input[type="radio"]');
                        }
                        if (radioInputs.length === 0) continue;

                        var radioOpts = [];
                        for (var ri = 0; ri < radioInputs.length; ri++) {
                            var radioLabel = null;
                            if (radioInputs[ri].id) {
                                radioLabel = radios[r].querySelector('label[for="' + radioInputs[ri].id + '"]');
                                if (!radioLabel) {
                                    var shadowRL = findInShadow(radios[r], 'label[for="' + radioInputs[ri].id + '"]');
                                    if (shadowRL.length > 0) radioLabel = shadowRL[0];
                                }
                            }
                            // Fallback: find closest label or parent text
                            if (!radioLabel) {
                                radioLabel = radioInputs[ri].closest('label');
                            }
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
                            // Shadow-DOM-piercing search
                            function findInShadow(root, selector) {{
                                var results = Array.from(root.querySelectorAll(selector));
                                var allEls = root.querySelectorAll('*');
                                for (var i = 0; i < allEls.length; i++) {{
                                    if (allEls[i].shadowRoot) {{
                                        results = results.concat(findInShadow(allEls[i].shadowRoot, selector));
                                    }}
                                }}
                                return results;
                            }}

                            var containers = findInShadow(document,
                                '.field, .question-item, [class*="screening"], [class*="question"], ' +
                                '.form-group, .form-field, [class*="radio-group"], fieldset, spl-radio, spl-checkbox'
                            );
                            var container = containers[{index}];
                            if (!container) return 'CONTAINER_NOT_FOUND';

                            // For select — also check shadow DOM
                            var select = container.querySelector('select');
                            if (!select) {{
                                var shadowSelects = findInShadow(container, 'select');
                                if (shadowSelects.length > 0) select = shadowSelects[0];
                            }}
                            if (select) {{
                                for (var i = 0; i < select.options.length; i++) {{
                                    if (select.options[i].text.indexOf('{escaped_answer}') >= 0 ||
                                        '{escaped_answer}'.indexOf(select.options[i].text) >= 0) {{
                                        select.selectedIndex = i;
                                        select.dispatchEvent(new Event('change', {{bubbles: true, composed: true}}));
                                        return 'OK_SELECT';
                                    }}
                                }}
                                return 'SELECT_NO_MATCH';
                            }}

                            // For radio — also check shadow DOM
                            var radios = Array.from(container.querySelectorAll('input[type="radio"]'));
                            if (radios.length === 0) {{
                                radios = findInShadow(container, 'input[type="radio"]');
                            }}
                            if (radios.length > 0) {{
                                for (var r = 0; r < radios.length; r++) {{
                                    var radioLabel = null;
                                    if (radios[r].id) {{
                                        radioLabel = container.querySelector('label[for="' + radios[r].id + '"]');
                                        if (!radioLabel) {{
                                            var srl = findInShadow(container, 'label[for="' + radios[r].id + '"]');
                                            if (srl.length > 0) radioLabel = srl[0];
                                        }}
                                    }}
                                    if (!radioLabel) radioLabel = radios[r].closest('label');
                                    if (radioLabel && radioLabel.textContent.indexOf('{escaped_answer}') >= 0) {{
                                        radios[r].click();
                                        return 'OK_RADIO';
                                    }}
                                }}
                                // Default: click first option
                                radios[0].click();
                                return 'OK_RADIO_DEFAULT';
                            }}

                            // For text/textarea — deep shadow search
                            var input = container.querySelector('input:not([type="hidden"]):not([type="file"]), textarea');
                            if (!input) {{
                                var shadowInputs = findInShadow(container,
                                    'input:not([type="hidden"]):not([type="file"]):not([type="radio"]):not([type="checkbox"]), textarea'
                                );
                                if (shadowInputs.length > 0) input = shadowInputs[0];
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

    async def _nd_detect_multistep(self, nd_page) -> bool:
        """Detect if SmartRecruiters is showing a multi-step form (resume parsed)."""
        try:
            result = await nd_page.evaluate("""
                (function() {
                    var text = document.body.innerText || '';
                    // Multi-step indicators: resume parsed, experience/education sections
                    if (text.indexOf('prefilled using data from resume') >= 0) return 'MULTISTEP';
                    if (text.indexOf('Experience') >= 0 && text.indexOf('+ Add') >= 0) return 'MULTISTEP';
                    // Check for step indicators / progress bar
                    var steps = document.querySelectorAll('[class*="step"], [class*="progress"], [class*="wizard"]');
                    if (steps.length > 0) return 'MULTISTEP';
                    // Check for "Next" button
                    var buttons = document.querySelectorAll('button, a');
                    for (var i = 0; i < buttons.length; i++) {
                        var t = (buttons[i].textContent || '').trim().toLowerCase();
                        if (t === 'next' || t === 'continue' || t === 'next step') return 'MULTISTEP';
                    }
                    return 'SIMPLE';
                })()
            """)
            is_multi = (result == "MULTISTEP")
            if is_multi:
                logger.info("Detected multi-step SmartRecruiters form")
            return is_multi
        except Exception as e:
            logger.debug(f"Multi-step detection failed: {e}")
            return False

    async def _nd_handle_multistep_submit(self, nd_page, job_data) -> bool:
        """Navigate through multi-step SmartRecruiters form and submit.

        Multi-step forms: Personal Info → Experience → Education → Additional → Review → Submit
        Uses nodriver's find() for robust button clicking through web components.
        """
        max_steps = 10
        prev_sections = None
        stall_count = 0

        for step in range(max_steps):
            await asyncio.sleep(3)

            # Check for success indicators in page content
            try:
                page_text = await nd_page.evaluate("document.body.innerText || ''")
                page_text_lower = page_text.lower() if isinstance(page_text, str) else ""
            except Exception:
                page_text_lower = ""

            if any(x in page_text_lower for x in [
                "thank you for your application",
                "application received",
                "application submitted",
                "successfully applied",
                "application complete",
                "we have received your application",
            ]):
                logger.info(f"Multi-step form submitted successfully at step {step + 1}!")
                return True

            # Check URL for success
            curr_url = str(nd_page.url) if hasattr(nd_page, 'url') else ""
            if any(x in curr_url.lower() for x in ["confirmation", "success", "thank"]):
                logger.info(f"Multi-step success via URL: {curr_url}")
                return True

            # Log current page section for progress tracking
            try:
                section_info = await nd_page.evaluate("""
                    (function() {
                        // Get all section headings / form labels
                        var headings = document.querySelectorAll('h1, h2, h3, h4, [class*="section-title"]');
                        var texts = [];
                        for (var i = 0; i < headings.length; i++) {
                            var t = headings[i].textContent.trim();
                            if (t && t.length < 60) texts.push(t);
                        }
                        // Check for error messages
                        var errors = document.querySelectorAll('[class*="error"], [class*="invalid"], .error-message');
                        for (var j = 0; j < errors.length; j++) {
                            var e = errors[j].textContent.trim();
                            if (e && e.length < 100) texts.push('ERROR: ' + e);
                        }
                        // Check for required field indicators
                        var required = document.querySelectorAll('[class*="required"]');
                        if (required.length > 0) texts.push('Required fields: ' + required.length);
                        return JSON.stringify(texts.slice(0, 10));
                    })()
                """)
                if section_info and isinstance(section_info, str):
                    import json as _sec_json
                    sections = _sec_json.loads(section_info)
                    logger.info(f"Step {step + 1} sections: {sections}")

                    # Stall detection: if same sections/errors as previous step, we're stuck
                    if sections == prev_sections:
                        stall_count += 1
                        logger.info(f"Step {step + 1} stall #{stall_count} (same page)")
                    else:
                        stall_count = 0
                    prev_sections = sections

                    # If stuck on "Fields marked with * are required", re-fill missing fields
                    has_required_error = any('fields marked with' in s.lower() for s in sections)
                    if has_required_error and stall_count <= 2:
                        logger.info(f"Re-filling missing required fields at step {step + 1}")
                        config = self.form_filler.config
                        personal = config.get("personal_info", {})
                        import nodriver.cdp as cdp

                        # Re-fill phone if empty
                        phone = personal.get("phone", "").replace("-", "").replace(" ", "").replace("+1", "").replace("+", "")
                        if phone:
                            phone_val = await nd_page.evaluate("""
                                (function() {
                                    var host = document.querySelector('spl-phone-field');
                                    if (!host || !host.shadowRoot) return '';
                                    function findTel(root) {
                                        var inp = root.querySelector('input[type="tel"]');
                                        if (inp) return inp;
                                        var all = root.querySelectorAll('*');
                                        for (var i = 0; i < all.length; i++) {
                                            if (all[i].shadowRoot) { var f = findTel(all[i].shadowRoot); if (f) return f; }
                                        }
                                        return null;
                                    }
                                    var tel = findTel(host.shadowRoot);
                                    return tel ? tel.value : '';
                                })()
                            """)
                            if not phone_val or len(str(phone_val).strip()) < 5:
                                logger.info("Re-filling phone (was empty)")
                                # Use mouse-click + char-by-char approach (same as _nd_fill_form)
                                try:
                                    click_r = await nd_page.evaluate("""
                                        (function() {
                                            var host = document.querySelector('spl-phone-field');
                                            if (!host || !host.shadowRoot) return null;
                                            function findTel(root) {
                                                var inp = root.querySelector('input[type="tel"]');
                                                if (inp) return inp;
                                                var all = root.querySelectorAll('*');
                                                for (var i = 0; i < all.length; i++) {
                                                    if (all[i].shadowRoot) { var f = findTel(all[i].shadowRoot); if (f) return f; }
                                                }
                                                return null;
                                            }
                                            var input = findTel(host.shadowRoot);
                                            if (!input) return null;
                                            input.value = '';
                                            input.dispatchEvent(new Event('input', {bubbles: true, composed: true}));
                                            var rect = input.getBoundingClientRect();
                                            return {x: rect.x + rect.width/2, y: rect.y + rect.height/2, w: rect.width};
                                        })()
                                    """)
                                    if click_r and isinstance(click_r, dict) and click_r.get('w', 0) > 0:
                                        x, y = click_r['x'], click_r['y']
                                        await nd_page.send(cdp.input_.dispatch_mouse_event(
                                            type_="mousePressed", x=x, y=y,
                                            button=cdp.input_.MouseButton.LEFT, click_count=1))
                                        await nd_page.send(cdp.input_.dispatch_mouse_event(
                                            type_="mouseReleased", x=x, y=y,
                                            button=cdp.input_.MouseButton.LEFT, click_count=1))
                                        await asyncio.sleep(0.2)
                                        for char in phone:
                                            await nd_page.send(cdp.input_.dispatch_key_event(type_="keyDown", key=char))
                                            await nd_page.send(cdp.input_.dispatch_key_event(type_="char", text=char, key=char))
                                            await nd_page.send(cdp.input_.dispatch_key_event(type_="keyUp", key=char))
                                        await asyncio.sleep(0.3)
                                except Exception as pe:
                                    logger.debug(f"Phone re-fill mouse method failed: {pe}")
                                    # Fallback
                                    await self._nd_click_and_type_phone(nd_page, phone)
                                await asyncio.sleep(0.5)

                        # Re-fill city if empty
                        city = personal.get("city", "") or personal.get("location", "")
                        if city:
                            city_val = await nd_page.evaluate("""
                                (function() {
                                    var host = document.querySelector('spl-autocomplete');
                                    if (host && host.shadowRoot) {
                                        var inp = host.shadowRoot.querySelector('input');
                                        return inp ? inp.value : '';
                                    }
                                    return '';
                                })()
                            """)
                            if not city_val or len(str(city_val).strip()) < 2:
                                logger.info("Re-filling city (was empty)")
                                city_typed = await self._nd_cdp_type_into_shadow(
                                    nd_page, "spl-autocomplete", city, input_selector='input'
                                )
                                if city_typed:
                                    await asyncio.sleep(2)
                                    # Select from autocomplete dropdown
                                    await nd_page.send(cdp.input_.dispatch_key_event(
                                        type_="keyDown", key="ArrowDown", code="ArrowDown"))
                                    await nd_page.send(cdp.input_.dispatch_key_event(
                                        type_="keyUp", key="ArrowDown", code="ArrowDown"))
                                    await asyncio.sleep(0.3)
                                    await nd_page.send(cdp.input_.dispatch_key_event(
                                        type_="keyDown", key="Enter", code="Enter"))
                                    await nd_page.send(cdp.input_.dispatch_key_event(
                                        type_="keyUp", key="Enter", code="Enter"))
                                    await asyncio.sleep(0.5)
                                    # Fallback: click suggestion
                                    city_verify = await nd_page.evaluate("""
                                        (function() {
                                            var host = document.querySelector('spl-autocomplete');
                                            if (host && host.shadowRoot) {
                                                var inp = host.shadowRoot.querySelector('input');
                                                return inp ? inp.value : '';
                                            }
                                            return '';
                                        })()
                                    """)
                                    if not city_verify or len(str(city_verify).strip()) < 2:
                                        await nd_page.evaluate("""
                                            (function() {
                                                function findSuggestions(root) {
                                                    var items = root.querySelectorAll('li, [role="option"]');
                                                    for (var i = 0; i < items.length; i++) {
                                                        var r = items[i].getBoundingClientRect();
                                                        if (r.width > 0 && r.height > 0 && r.height < 100) {
                                                            items[i].click(); return 'CLICKED';
                                                        }
                                                    }
                                                    var hosts = root.querySelectorAll('*');
                                                    for (var j = 0; j < hosts.length; j++) {
                                                        if (hosts[j].shadowRoot) {
                                                            var r2 = findSuggestions(hosts[j].shadowRoot);
                                                            if (r2 !== 'NONE') return r2;
                                                        }
                                                    }
                                                    return 'NONE';
                                                }
                                                var host = document.querySelector('spl-autocomplete');
                                                if (host && host.shadowRoot) findSuggestions(host.shadowRoot);
                                            })()
                                        """)

                        # Re-upload resume if missing
                        resume_path = config.get("files", {}).get("resume")
                        if resume_path:
                            resume_val = await nd_page.evaluate("""
                                (function() {
                                    var dz = document.querySelector('spl-dropzone');
                                    if (!dz) return 'no-dz';
                                    var fileName = dz.getAttribute('file-name') || '';
                                    if (fileName) return fileName;
                                    if (dz.shadowRoot) {
                                        var fi = dz.shadowRoot.querySelector('input[type="file"]');
                                        if (fi && fi.files && fi.files.length > 0) return fi.files[0].name;
                                    }
                                    return '';
                                })()
                            """)
                            if not resume_val or resume_val == 'no-dz' or len(str(resume_val).strip()) < 2:
                                logger.info("Re-uploading resume (was empty)")
                                try:
                                    await asyncio.wait_for(
                                        self._nd_upload_resume(nd_page, resume_path),
                                        timeout=15
                                    )
                                except Exception:
                                    pass

                        # Re-fill text fields that got cleared
                        refill_fields = {
                            "first-name-input": personal.get("first_name", ""),
                            "last-name-input": personal.get("last_name", ""),
                            "email-input": personal.get("email", ""),
                            "confirm-email-input": personal.get("email", ""),
                        }
                        for element_id, value in refill_fields.items():
                            if not value:
                                continue
                            try:
                                curr = await nd_page.evaluate(f"""
                                    (function() {{
                                        var host = document.querySelector('#{element_id}');
                                        if (!host || !host.shadowRoot) return '';
                                        var inp = host.shadowRoot.querySelector('input');
                                        return inp ? inp.value : '';
                                    }})()
                                """)
                                if not curr or len(str(curr).strip()) < 2:
                                    await self._nd_cdp_type_into_shadow(
                                        nd_page, f"#{element_id}", value, input_selector='input'
                                    )
                                    await asyncio.sleep(0.3)
                            except Exception:
                                pass

                        await asyncio.sleep(1)

                    # If stalled 3+ times on same page, give up on this step
                    if stall_count >= 3:
                        logger.warning(f"Stalled {stall_count} times on step {step + 1} — giving up")
                        break
            except Exception:
                pass

            # Scroll to bottom first so all buttons are visible
            try:
                await nd_page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(0.5)
            except Exception:
                pass

            # Debug: dump form fields, questions, checkboxes, and buttons for diagnostics
            try:
                dom_debug = await nd_page.evaluate("""
                    (function() {
                        var info = {fields: [], buttons: [], checkboxes: []};

                        // Find all form-related elements and their values
                        // Check spl-input, spl-select, spl-textarea, spl-checkbox, spl-radio, spl-phone-field
                        var splTypes = ['spl-input', 'spl-select', 'spl-textarea', 'spl-checkbox',
                                        'spl-radio', 'spl-phone-field', 'spl-autocomplete', 'spl-dropzone'];
                        for (var t = 0; t < splTypes.length; t++) {
                            var els = document.querySelectorAll(splTypes[t]);
                            for (var i = 0; i < els.length; i++) {
                                var id = els[i].id || '';
                                var label = els[i].getAttribute('label') || els[i].getAttribute('aria-label') || '';
                                var req = els[i].hasAttribute('required') ? '*' : '';
                                var val = '';
                                if (els[i].shadowRoot) {
                                    var inner = els[i].shadowRoot.querySelector('input, textarea, select');
                                    val = inner ? (inner.value || '').substring(0, 30) : '';
                                    if (splTypes[t] === 'spl-checkbox') {
                                        var cb = els[i].shadowRoot.querySelector('input[type="checkbox"]');
                                        val = cb ? (cb.checked ? 'CHECKED' : 'UNCHECKED') : 'no-cb';
                                    }
                                }
                                info.fields.push(splTypes[t] + (id ? '#' + id : '') + req +
                                    (label ? '(' + label.substring(0, 30) + ')' : '') + '=' + val);
                            }
                        }

                        // Also check regular form elements
                        var selects = document.querySelectorAll('select');
                        for (var s = 0; s < selects.length; s++) {
                            var sId = selects[s].id || selects[s].name || 'select-' + s;
                            var sReq = selects[s].hasAttribute('required') ? '*' : '';
                            var opts = [];
                            for (var o = 0; o < selects[s].options.length; o++) {
                                opts.push(selects[s].options[o].text.substring(0, 20));
                            }
                            info.fields.push('select#' + sId + sReq + '=' +
                                selects[s].value + '(opts:' + opts.slice(0, 4).join('|') + ')');
                        }
                        var radios = document.querySelectorAll('input[type="radio"]');
                        if (radios.length > 0) {
                            info.fields.push('radios:' + radios.length);
                        }

                        // Buttons
                        var splBtns = document.querySelectorAll('spl-button');
                        for (var b = 0; b < splBtns.length; b++) {
                            var bText = (splBtns[b].textContent || '').trim().substring(0, 30);
                            var bRect = splBtns[b].getBoundingClientRect();
                            var bVis = bRect.width > 0 && bRect.height > 0 ? 'vis' : 'hid';
                            info.buttons.push(bText + '(' + bVis + ')');
                        }

                        // spl-checkbox status
                        var splCb = document.querySelectorAll('spl-checkbox');
                        for (var c = 0; c < splCb.length; c++) {
                            var cbId = splCb[c].id || 'spl-cb-' + c;
                            var cbReq = splCb[c].hasAttribute('required') ? '*' : '';
                            var cbLabel = (splCb[c].textContent || '').trim().substring(0, 50);
                            var cbChecked = 'unknown';
                            if (splCb[c].shadowRoot) {
                                var cbInner = splCb[c].shadowRoot.querySelector('input[type="checkbox"]');
                                cbChecked = cbInner ? (cbInner.checked ? 'CHECKED' : 'UNCHECKED') : 'no-inner';
                            }
                            info.checkboxes.push(cbId + cbReq + ':' + cbChecked + '(' + cbLabel + ')');
                        }

                        return JSON.stringify(info);
                    })()
                """)
                if dom_debug:
                    import json as _dbg_json
                    logger.info(f"Step {step + 1} DOM debug: {_dbg_json.loads(dom_debug) if isinstance(dom_debug, str) else dom_debug}")
            except Exception as dbg_e:
                logger.debug(f"DOM debug failed: {dbg_e}")

            # Check ALL consent/privacy/agree checkboxes — aggressively check every checkbox on the page
            # SmartRecruiters uses spl-checkbox with shadow DOM; the real input is inside the shadow root
            try:
                checkbox_result = await nd_page.evaluate("""
                    (function() {
                        var checked = [];

                        // Strategy 1: spl-checkbox components (most common in SmartRecruiters)
                        var splChecks = document.querySelectorAll('spl-checkbox');
                        for (var i = 0; i < splChecks.length; i++) {
                            var sr = splChecks[i].shadowRoot;
                            if (!sr) continue;
                            var inner = sr.querySelector('input[type="checkbox"]');
                            if (inner && !inner.checked) {
                                // Click the outer spl-checkbox element (Angular listens here)
                                splChecks[i].click();
                                // Also click inner input to ensure DOM state change
                                if (!inner.checked) inner.click();
                                // Dispatch Angular-friendly events on both inner and outer
                                var evts = ['click', 'change', 'input'];
                                evts.forEach(function(evtName) {
                                    inner.dispatchEvent(new Event(evtName, {bubbles: true, composed: true}));
                                    splChecks[i].dispatchEvent(new Event(evtName, {bubbles: true, composed: true}));
                                });
                                checked.push('spl-checkbox-' + i);
                            }
                            // Also try clicking the label/container inside shadow
                            if (inner && !inner.checked) {
                                var label = sr.querySelector('label, .checkbox-label, .checkmark');
                                if (label) label.click();
                            }
                        }

                        // Strategy 2: regular HTML checkboxes
                        var htmlChecks = document.querySelectorAll('input[type="checkbox"]');
                        for (var j = 0; j < htmlChecks.length; j++) {
                            if (!htmlChecks[j].checked) {
                                htmlChecks[j].click();
                                checked.push('html-checkbox-' + j);
                            }
                        }

                        // Strategy 3: elements with checkbox role
                        var roleChecks = document.querySelectorAll('[role="checkbox"]');
                        for (var k = 0; k < roleChecks.length; k++) {
                            var ariaChecked = roleChecks[k].getAttribute('aria-checked');
                            if (ariaChecked !== 'true') {
                                roleChecks[k].click();
                                checked.push('role-checkbox-' + k);
                            }
                        }

                        // Strategy 4: Deep shadow DOM search — some checkboxes are inside
                        // nested shadow roots (e.g., inside consent sections)
                        function findAndCheckInShadow(root) {
                            var all = root.querySelectorAll('*');
                            for (var m = 0; m < all.length; m++) {
                                if (all[m].shadowRoot) {
                                    var innerChecks = all[m].shadowRoot.querySelectorAll('input[type="checkbox"]');
                                    for (var n = 0; n < innerChecks.length; n++) {
                                        if (!innerChecks[n].checked) {
                                            innerChecks[n].click();
                                            checked.push('deep-shadow-' + m + '-' + n);
                                        }
                                    }
                                    findAndCheckInShadow(all[m].shadowRoot);
                                }
                            }
                        }
                        findAndCheckInShadow(document);

                        return checked.length > 0 ? ('CHECKED:' + checked.join(',')) : 'NONE_FOUND';
                    })()
                """)
                if checkbox_result and 'CHECKED' in str(checkbox_result):
                    logger.info(f"Checked consent checkboxes: {checkbox_result}")
                    await asyncio.sleep(1)  # Wait for validation to clear after checking
            except Exception as cb_e:
                logger.debug(f"Checkbox check failed: {cb_e}")

            # Handle screening questions on the current step (e.g., "Preliminary questions")
            # These can appear on any step, not just the first page
            try:
                await self._nd_handle_screening_questions(nd_page, job_data)
            except Exception as sq_e:
                logger.debug(f"Screening questions on step {step + 1}: {sq_e}")

            # Handle spl-select dropdowns that need answers (screening questions as dropdowns)
            try:
                select_result = await nd_page.evaluate("""
                    (function() {
                        var filled = [];
                        var selects = document.querySelectorAll('spl-select');
                        for (var i = 0; i < selects.length; i++) {
                            if (!selects[i].shadowRoot) continue;
                            var inner = selects[i].shadowRoot.querySelector('select');
                            if (!inner) continue;
                            // Skip if already has a non-default selection
                            if (inner.selectedIndex > 0) continue;
                            var label = selects[i].getAttribute('label') || '';
                            // For required selects without a value, try selecting first non-empty option
                            if (selects[i].hasAttribute('required') && inner.options.length > 1) {
                                for (var j = 1; j < inner.options.length; j++) {
                                    var optText = (inner.options[j].text || '').toLowerCase();
                                    // Prefer "yes" or positive options
                                    if (optText.indexOf('yes') >= 0 || optText === 'true') {
                                        inner.selectedIndex = j;
                                        inner.dispatchEvent(new Event('change', {bubbles: true, composed: true}));
                                        filled.push(label + '=' + inner.options[j].text);
                                        break;
                                    }
                                }
                                // If no "yes" found, just select first option
                                if (inner.selectedIndex === 0 && inner.options.length > 1) {
                                    inner.selectedIndex = 1;
                                    inner.dispatchEvent(new Event('change', {bubbles: true, composed: true}));
                                    filled.push(label + '=' + inner.options[1].text);
                                }
                            }
                        }
                        return filled.length > 0 ? JSON.stringify(filled) : 'NONE';
                    })()
                """)
                if select_result and select_result != 'NONE':
                    logger.info(f"Filled spl-select dropdowns on step {step + 1}: {select_result}")
            except Exception:
                pass

            # Try clicking navigation buttons via JS — handles both spl-button shadow DOM and regular buttons
            # Prioritize "Next"/"Continue" over "Submit" since we navigate step by step
            try:
                nav_result = await nd_page.evaluate("""
                    (function() {
                        var allButtons = [];

                        // Collect ALL spl-button components with their text and click target
                        var splBtns = document.querySelectorAll('spl-button');
                        for (var i = 0; i < splBtns.length; i++) {
                            var text = (splBtns[i].textContent || '').trim().toLowerCase();
                            var rect = splBtns[i].getBoundingClientRect();
                            if (rect.width === 0 || rect.height === 0) continue;
                            var clickTarget = splBtns[i];
                            if (splBtns[i].shadowRoot) {
                                var inner = splBtns[i].shadowRoot.querySelector('button');
                                if (inner) clickTarget = inner;
                            }
                            allButtons.push({text: text, el: clickTarget, type: 'spl'});
                        }

                        // Collect regular buttons
                        var btns = document.querySelectorAll('button, a[role="button"], input[type="submit"]');
                        for (var j = 0; j < btns.length; j++) {
                            var t = (btns[j].textContent || '').trim().toLowerCase();
                            var r = btns[j].getBoundingClientRect();
                            if (r.width === 0 || r.height === 0) continue;
                            // Skip if already captured as spl-button inner button
                            if (btns[j].closest('spl-button')) continue;
                            allButtons.push({text: t, el: btns[j], type: 'html'});
                        }

                        // Priority 1: "Next" / "Continue" / "Save & Next"
                        for (var k = 0; k < allButtons.length; k++) {
                            var txt = allButtons[k].text;
                            if (txt === 'next' || txt === 'continue' || txt === 'next step' ||
                                txt === 'save & next' || txt === 'save and next' ||
                                txt.indexOf('next') === 0) {
                                allButtons[k].el.click();
                                return 'NEXT:' + txt + ':' + allButtons[k].type;
                            }
                        }

                        // Priority 2: "Submit Application" / "Submit" / "Apply"
                        for (var m = 0; m < allButtons.length; m++) {
                            var stxt = allButtons[m].text;
                            if (stxt.indexOf('submit') >= 0 || stxt.indexOf('apply now') >= 0) {
                                allButtons[m].el.click();
                                return 'SUBMIT:' + stxt + ':' + allButtons[m].type;
                            }
                        }

                        // Priority 3: Any prominent action button (not "back" or "cancel")
                        for (var n = 0; n < allButtons.length; n++) {
                            var ptxt = allButtons[n].text;
                            if (ptxt && ptxt.length > 0 && ptxt.length < 30 &&
                                ptxt !== 'back' && ptxt !== 'cancel' && ptxt !== 'previous' &&
                                ptxt !== 'sign in' && ptxt !== 'log in' && ptxt !== 'close' &&
                                ptxt.indexOf('upload') < 0 && ptxt.indexOf('add') < 0 &&
                                ptxt.indexOf('remove') < 0 && ptxt.indexOf('delete') < 0 &&
                                ptxt.indexOf('edit') < 0) {
                                // Only click primary/action-style buttons
                                var el = allButtons[n].el;
                                var classes = (el.className || '').toLowerCase();
                                if (classes.indexOf('primary') >= 0 || classes.indexOf('action') >= 0 ||
                                    classes.indexOf('cta') >= 0 || classes.indexOf('submit') >= 0) {
                                    el.click();
                                    return 'ACTION:' + ptxt + ':' + allButtons[n].type;
                                }
                            }
                        }

                        // Debug: return what buttons are visible
                        var dbg = allButtons.map(function(b) { return b.text; }).slice(0, 8);
                        return 'NO_NAV:visible=' + JSON.stringify(dbg);
                    })()
                """)
                nav_str = str(nav_result) if nav_result else 'NO_RESULT'
                logger.info(f"Step {step + 1} nav result: {nav_str}")

                if 'NEXT:' in nav_str or 'SUBMIT:' in nav_str or 'ACTION:' in nav_str:
                    await asyncio.sleep(3)
                    continue
            except Exception as e:
                logger.debug(f"Navigation JS click failed: {e}")

            # Fallback: try nodriver's find() for "Next" button (handles shadow DOM better sometimes)
            for btn_text in ["Next", "Continue", "Submit Application", "Submit"]:
                try:
                    btn = await nd_page.find(btn_text, best_match=True)
                    if btn:
                        found_text = str(getattr(btn, 'text', '')).strip().lower()
                        if btn_text.lower() in found_text or found_text in btn_text.lower():
                            await btn.click()
                            logger.info(f"Clicked '{btn_text}' via nodriver find at step {step + 1}")
                            await asyncio.sleep(3)
                            break
                except Exception:
                    continue
            else:
                # No button found at all — form is stuck
                logger.warning(f"Multi-step form stuck at step {step + 1} — no navigation button found")
                break

        logger.warning("Multi-step form navigation exhausted without success")
        return False

    async def _nd_check_required_fields(self, nd_page) -> list:
        """Check all required fields are filled in SmartRecruiters Shadow DOM form.
        Returns list of empty required field labels."""
        try:
            import json as _json
            raw = await nd_page.evaluate("""
                (function() {
                    var empty = [];

                    // Check standard inputs (including Shadow DOM components)
                    var inputs = document.querySelectorAll(
                        'input:not([type="hidden"]):not([type="file"]):not([type="submit"]), textarea, select'
                    );
                    for (var i = 0; i < inputs.length; i++) {
                        var inp = inputs[i];
                        var isReq = inp.required || inp.getAttribute('aria-required') === 'true';
                        // Also check parent for asterisk
                        if (!isReq) {
                            var parent = inp.closest('.field, .form-group, [class*="field"]');
                            if (parent && parent.textContent.indexOf('*') >= 0) {
                                isReq = true;
                            }
                        }
                        if (isReq && (!inp.value || !inp.value.trim())) {
                            var label = inp.getAttribute('aria-label') || inp.name || inp.id || '?';
                            empty.push(label);
                        }
                    }

                    // Check Shadow DOM spl-input components
                    var splInputs = document.querySelectorAll('spl-input, spl-textarea, spl-select');
                    for (var j = 0; j < splInputs.length; j++) {
                        var spl = splInputs[j];
                        var req = spl.hasAttribute('required') || spl.getAttribute('aria-required') === 'true';
                        if (req && spl.shadowRoot) {
                            var inner = spl.shadowRoot.querySelector('input, textarea, select');
                            if (inner && (!inner.value || !inner.value.trim())) {
                                var lbl = spl.getAttribute('label') || spl.getAttribute('name') || '?';
                                empty.push(lbl);
                            }
                        }
                    }

                    // Check file upload (resume) - spl-dropzone
                    var dropzone = document.querySelector('spl-dropzone');
                    if (dropzone) {
                        var hasFile = dropzone.getAttribute('file-name') ||
                                      (dropzone.shadowRoot && dropzone.shadowRoot.querySelector('.file-name'));
                        if (!hasFile) {
                            var dzReq = dropzone.hasAttribute('required');
                            if (dzReq) empty.push('Resume/CV upload');
                        }
                    }

                    return JSON.stringify(empty);
                })()
            """)
            # nodriver evaluate returns deep-serialized value; parse JSON string
            if isinstance(raw, str):
                return _json.loads(raw)
            elif isinstance(raw, list):
                # Already a list, ensure items are strings
                return [str(x) if not isinstance(x, str) else x for x in raw]
            return []
        except Exception as e:
            logger.warning(f"SmartRecruiters required fields check failed: {e}")
            return []

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
