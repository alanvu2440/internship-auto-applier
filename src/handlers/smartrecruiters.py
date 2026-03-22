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
import fcntl
import os
import re
import signal
from pathlib import Path
from typing import Dict, Any, Optional
from playwright.async_api import Page
from loguru import logger
from detection.job_status import is_job_closed as _shared_is_job_closed

from .base import BaseHandler

# File lock to ensure only ONE nodriver Chrome instance across ALL processes
_BROWSER_LOCK_PATH = Path("data/browser_profiles/nodriver.lock")
_BROWSER_PID_PATH = Path("data/browser_profiles/nodriver.pid")

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


# JS helper: recursively search all shadow roots for elements matching a CSS selector.
# Use this instead of document.querySelectorAll() when elements may be inside nested
# custom-element shadow roots (e.g. SR screening questions inside oc-screening-questions).
_DEEP_QUERY_JS = """
function deepQueryAll(root, selector) {
    var results = [];
    try {
        var d = root.querySelectorAll(selector);
        for (var _ii = 0; _ii < d.length; _ii++) results.push(d[_ii]);
        var _allNodes = root.querySelectorAll('*');
        for (var _jj = 0; _jj < _allNodes.length; _jj++) {
            if (_allNodes[_jj].shadowRoot) {
                var _sub = deepQueryAll(_allNodes[_jj].shadowRoot, selector);
                for (var _kk = 0; _kk < _sub.length; _kk++) results.push(_sub[_kk]);
            }
        }
    } catch(_e) {}
    return results;
}
"""


def _normalize_nd_result(val):
    """Normalize nodriver evaluate() results.

    nodriver sometimes returns dicts as list-of-pairs:
      [['x', {'type':'number','value':629.5}], ['y', ...]]
    instead of plain dicts: {'x': 629.5, 'y': ...}

    This converts the nested format to a plain dict.
    """
    if isinstance(val, dict):
        return val
    if isinstance(val, list) and val and isinstance(val[0], (list, tuple)):
        result = {}
        for item in val:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                key = item[0] if isinstance(item[0], str) else str(item[0])
                v = item[1]
                if isinstance(v, dict) and 'value' in v:
                    result[key] = v['value']
                elif isinstance(v, (int, float, str, bool)):
                    result[key] = v
        return result if result else None
    return val


class SmartRecruitersHandler(BaseHandler):
    """Handler for SmartRecruiters ATS applications.

    Uses nodriver (undetected-chromedriver) instead of Playwright to bypass
    DataDome bot protection on the SmartRecruiters oneclick-ui form.
    """

    name = "smartrecruiters"

    # Shared browser instance — ONE browser, never closes, new tabs per job
    _shared_nd_browser = None
    _keeper_tab = None
    _lock_fd = None  # File descriptor for exclusive lock
    _simplify_extension_path = None  # Set by main.py --with-simplify

    @staticmethod
    def _kill_existing_nodriver():
        """Kill any existing nodriver Chrome process owned by a different/dead process."""
        if _BROWSER_PID_PATH.exists():
            try:
                old_pid = int(_BROWSER_PID_PATH.read_text().strip())
                # Check if the old process is still alive
                try:
                    os.kill(old_pid, 0)  # Signal 0 = check if alive
                    # Process exists — kill it and its children
                    logger.warning(f"Killing stale nodriver Chrome (PID {old_pid})")
                    os.kill(old_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # Already dead
                except OSError:
                    pass
                _BROWSER_PID_PATH.unlink(missing_ok=True)
            except (ValueError, FileNotFoundError):
                pass

    @staticmethod
    def _acquire_browser_lock() -> bool:
        """Acquire exclusive file lock — prevents multiple Chrome instances.
        Returns True if lock acquired, False if another process holds it."""
        _BROWSER_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = open(_BROWSER_LOCK_PATH, "w")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fd.write(str(os.getpid()))
            fd.flush()
            SmartRecruitersHandler._lock_fd = fd
            return True
        except (IOError, OSError):
            # Another process holds the lock
            return False

    @staticmethod
    def _release_browser_lock():
        """Release the file lock."""
        if SmartRecruitersHandler._lock_fd:
            try:
                fcntl.flock(SmartRecruitersHandler._lock_fd, fcntl.LOCK_UN)
                SmartRecruitersHandler._lock_fd.close()
            except Exception:
                pass
            SmartRecruitersHandler._lock_fd = None

    async def _ensure_browser(self):
        """Get the shared nodriver browser from BrowserManager.

        In dual-browser mode, BrowserManager runs two separate Chrome instances:
        - nodriver Chrome for SmartRecruiters (DataDome bypass)
        - Playwright Chrome for GH/Lever/Ashby/Workday
        This handler uses the nodriver browser exclusively.

        Falls back to launching its own nodriver if BrowserManager didn't
        provide one.
        """
        # Try to get nodriver browser from BrowserManager (unified mode)
        if SmartRecruitersHandler._shared_nd_browser is not None:
            try:
                tabs = SmartRecruitersHandler._shared_nd_browser.tabs
                if tabs:
                    logger.info(f"Reusing shared nodriver browser ({len(tabs)} tabs open)")
                    return SmartRecruitersHandler._shared_nd_browser
            except Exception:
                logger.info("Shared nodriver browser died — will try to get new one")
                SmartRecruitersHandler._shared_nd_browser = None
                SmartRecruitersHandler._keeper_tab = None

        # Check if BrowserManager has a nodriver browser we can use
        if hasattr(self, 'browser_manager') and self.browser_manager and self.browser_manager.nd_browser:
            SmartRecruitersHandler._shared_nd_browser = self.browser_manager.nd_browser
            SmartRecruitersHandler._keeper_tab = self.browser_manager.nd_keeper_tab
            logger.info("Using unified nodriver browser from BrowserManager — ONE window for everything")
            return SmartRecruitersHandler._shared_nd_browser

        # Fallback: launch own nodriver browser (legacy behavior)
        logger.info("No unified browser available — launching standalone nodriver for SmartRecruiters")
        if not SmartRecruitersHandler._acquire_browser_lock():
            raise RuntimeError(
                "Another process already has nodriver Chrome open. "
                "Only ONE Chrome instance is allowed."
            )

        SmartRecruitersHandler._kill_existing_nodriver()

        uc = _get_nodriver()
        browser_args = ["--window-size=1920,1080"]

        ext_path = SmartRecruitersHandler._simplify_extension_path
        if ext_path and Path(ext_path).exists():
            browser_args.append(f"--load-extension={ext_path}")
            browser_args.append(f"--disable-extensions-except={ext_path}")

        nd_profile = Path("data/browser_profiles/nodriver_persistent")
        nd_profile.mkdir(parents=True, exist_ok=True)

        for lock_file in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
            lock_path = nd_profile / lock_file
            if lock_path.exists():
                try:
                    lock_path.unlink()
                except Exception:
                    pass

        for attempt in range(2):
            try:
                SmartRecruitersHandler._shared_nd_browser = await uc.start(
                    headless=False,
                    browser_args=browser_args,
                    user_data_dir=str(nd_profile),
                )
                break
            except Exception as e:
                logger.warning(f"Browser start attempt {attempt + 1} failed: {e}")
                SmartRecruitersHandler._shared_nd_browser = None
                if attempt == 0:
                    import shutil
                    try:
                        shutil.rmtree(nd_profile, ignore_errors=True)
                        nd_profile.mkdir(parents=True, exist_ok=True)
                        await asyncio.sleep(2)
                    except Exception:
                        pass
                else:
                    SmartRecruitersHandler._release_browser_lock()
                    raise

        SmartRecruitersHandler._keeper_tab = await SmartRecruitersHandler._shared_nd_browser.get("about:blank")
        logger.info("Started standalone nodriver browser with keeper tab")
        return SmartRecruitersHandler._shared_nd_browser

    async def apply(self, page: Page, job_url: str, job_data: Dict[str, Any]) -> bool:
        """Apply to a SmartRecruiters job using nodriver."""
        self._last_status = "failed"
        self._fields_filled = {}
        self._fields_missed = {}
        nd_browser = None
        nd_page = None

        try:
            logger.info(
                f"Applying to SmartRecruiters job: "
                f"{job_data.get('company')} - {job_data.get('role')}"
            )

            # --- Use shared nodriver browser (ONE window, new tab per job) ---
            nd_browser = await self._ensure_browser()

            nd_page = await nd_browser.get(job_url, new_tab=True)
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

            # Click Simplify autofill button if it appears
            await self._click_simplify_autofill(nd_page)

            config = self.form_filler.config

            # Handle screening/custom questions with AI (before field fill)
            await self._nd_handle_screening_questions(nd_page, job_data)

            # Upload resume FIRST — the resume parser fills name/email/phone into
            # Angular's model. If we fill fields before upload, the parser's re-render
            # wipes our DOM-only values for confirm-email, city, linkedin, etc.
            resume_path = config.get("files", {}).get("resume")
            if resume_path:
                try:
                    await asyncio.wait_for(
                        self._nd_upload_resume(nd_page, resume_path),
                        timeout=30
                    )
                    # Wait for resume parser to fill fields and Angular to re-render
                    await asyncio.sleep(5)
                    logger.info("Resume uploaded — waiting for parser to fill fields")
                except asyncio.TimeoutError:
                    logger.warning("Resume upload timed out after 30s — continuing without resume")

            # Fill the form fields AFTER resume parser has run
            filled = await self._nd_fill_form(nd_page, config, job_data)
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
                if validation:
                    # Screenshot after validation
                    try:
                        company = job_data.get("company", "unknown").replace(" ", "_")[:30]
                        ts = __import__("datetime").datetime.now().strftime("%Y%m%d_%H%M%S")
                        ss_path = f"data/screenshots/PASS_{company}_{ts}.png"
                        await nd_page.save_screenshot(ss_path)
                        logger.info(f"Screenshot saved: {ss_path}")
                    except Exception:
                        pass
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
                # Wait 10s for confirmation page to fully load, then screenshot
                logger.info("Submitted! Waiting 10s for confirmation page...")
                await asyncio.sleep(10)
                try:
                    company = job_data.get("company", "unknown").replace(" ", "_")[:30]
                    ts = __import__("datetime").datetime.now().strftime("%Y%m%d_%H%M%S")
                    ss_path = f"data/screenshots/PASS_{company}_{ts}.png"
                    await nd_page.save_screenshot(ss_path)
                    logger.info(f"Screenshot saved: {ss_path}")
                except Exception as ss_e:
                    logger.debug(f"Post-submit screenshot failed: {ss_e}")
                return True

            logger.warning("SmartRecruiters form submission failed — multi-step navigation exhausted")
            return False

        except Exception as e:
            logger.error(f"SmartRecruiters application failed: {e}")
            return False
        finally:
            # NEVER close browser or tabs — page will be reused for next job
            if nd_page:
                if self._last_status == "success":
                    logger.info("[BROWSER] SUCCESS — tab stays open for reuse")
                else:
                    logger.info("[BROWSER] SmartRecruiters tab left OPEN for manual help — browser stays open")

    async def detect_form_type(self, page: Page) -> str:
        """Detect SmartRecruiters form type."""
        return "oneclick"

    # ------------------------------------------------------------------
    # nodriver helpers
    # ------------------------------------------------------------------

    async def _nd_fill_city_autocomplete(self, nd_page, city: str, cdp) -> bool:
        """Fill the spl-autocomplete city field by typing and clicking a suggestion.

        Strategy:
        1. Focus the input inside the shadow DOM
        2. Clear existing value
        3. Type city name char-by-char via CDP dispatchKeyEvent (triggers Angular)
        4. Wait for autocomplete suggestions to appear
        5. Click the first matching suggestion via JS
        6. Verify the value was set
        """
        try:
            # Step 1: Focus the autocomplete input
            focus_result = await nd_page.evaluate("""
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
                    host.scrollIntoView({behavior: 'instant', block: 'center'});
                    input.value = '';
                    input.focus();
                    input.click();
                    input.dispatchEvent(new Event('input', {bubbles: true, composed: true}));
                    return 'FOCUSED';
                })()
            """)
            logger.info(f"City autocomplete focus: {focus_result}")
            if focus_result != 'FOCUSED':
                return False

            await asyncio.sleep(0.5)

            # Step 2: Type city via CDP Input.insertText (browser dispatches real events)
            # For autocomplete, type char-by-char to trigger suggestion dropdown
            for char in city:
                await nd_page.send(cdp.input_.insert_text(text=char))
                await asyncio.sleep(0.06)

            logger.info(f"City: typed '{city}', waiting for suggestions...")
            await asyncio.sleep(3.0)  # Wait for API/autocomplete suggestions to load

            # Step 3: Find suggestion coordinates and click via CDP mouse events
            # (JS .click() doesn't trigger Angular's change detection through shadow DOM)
            # Prefer suggestions containing "CA, US" or ", US" to avoid Costa Rica etc.
            suggestion_coords = await nd_page.evaluate("""
                (function() {
                    function collectSuggestions(root, depth) {
                        if (depth > 5) return [];
                        var results = [];
                        var selectors = [
                            'li', '[role="option"]', '[role="listbox"] > *',
                            'mat-option', '.suggestion', '[class*="suggestion"]',
                            '[class*="option"]', '[class*="result"]'
                        ];
                        for (var s = 0; s < selectors.length; s++) {
                            var items = root.querySelectorAll(selectors[s]);
                            for (var i = 0; i < items.length; i++) {
                                var r = items[i].getBoundingClientRect();
                                var txt = (items[i].textContent || '').trim();
                                if (r.width > 0 && r.height > 0 && r.height < 200 && txt.length > 2) {
                                    results.push({x: r.x + r.width/2, y: r.y + r.height/2, text: txt.substring(0, 80)});
                                }
                            }
                        }
                        var all = root.querySelectorAll('*');
                        for (var j = 0; j < all.length; j++) {
                            if (all[j].shadowRoot) {
                                results = results.concat(collectSuggestions(all[j].shadowRoot, depth + 1));
                            }
                        }
                        return results;
                    }
                    var host = document.querySelector('spl-autocomplete');
                    if (!host || !host.shadowRoot) return null;
                    var all = collectSuggestions(host.shadowRoot, 0);
                    if (all.length === 0) return null;
                    // Prefer US suggestions
                    for (var i = 0; i < all.length; i++) {
                        if (all[i].text.indexOf(', US') !== -1 || all[i].text.indexOf('United States') !== -1) {
                            return all[i];
                        }
                    }
                    return all[0];  // fallback to first
                })()
            """)
            logger.info(f"City suggestion coords: {suggestion_coords}")

            # nodriver may return dict OR list-of-pairs; normalize
            coords = _normalize_nd_result(suggestion_coords)

            if coords and isinstance(coords, dict) and 'x' in coords and 'y' in coords:
                x, y = coords['x'], coords['y']
                # CDP mouse click at exact coordinates — Angular WILL pick this up
                await nd_page.send(cdp.input_.dispatch_mouse_event(
                    type_="mousePressed", x=x, y=y,
                    button=cdp.input_.MouseButton.LEFT, click_count=1))
                await asyncio.sleep(0.05)
                await nd_page.send(cdp.input_.dispatch_mouse_event(
                    type_="mouseReleased", x=x, y=y,
                    button=cdp.input_.MouseButton.LEFT, click_count=1))
                await asyncio.sleep(1)

                city_val = await self._nd_get_city_value(nd_page)
                logger.info(f"City value after CDP mouse click: '{city_val}'")
                if city_val and len(str(city_val)) > 2:
                    # Trigger zone.js spl-change on the autocomplete host
                    # to update Angular's FormControl (DOM value alone isn't enough)
                    # Pass the actual city value in the event detail
                    escaped_city = str(city_val).replace("'", "\\'").replace('"', '\\"')
                    await nd_page.evaluate(f"""
                        (function() {{
                            var host = document.querySelector('spl-autocomplete');
                            if (!host) return;
                            var cityValue = '{escaped_city}';
                            // Trigger spl-change zone.js listeners with actual city value
                            var key = '__zone_symbol__spl-changefalse';
                            if (host[key] && Array.isArray(host[key])) {{
                                for (var i = 0; i < host[key].length; i++) {{
                                    try {{
                                        var handler = host[key][i].handler || host[key][i];
                                        if (typeof handler === 'function') {{
                                            handler(new CustomEvent('spl-change', {{
                                                detail: {{value: cityValue}}, bubbles: true
                                            }}));
                                        }}
                                    }} catch(e) {{}}
                                }}
                            }}
                            // Also dispatch native change/input events
                            host.dispatchEvent(new Event('change', {{bubbles: true, composed: true}}));
                            host.dispatchEvent(new Event('input', {{bubbles: true, composed: true}}));
                            // Try spl-touched to mark as touched
                            var touchedKey = '__zone_symbol__spl-touchedfalse';
                            if (host[touchedKey] && Array.isArray(host[touchedKey])) {{
                                for (var j = 0; j < host[touchedKey].length; j++) {{
                                    try {{
                                        var th = host[touchedKey][j].handler || host[touchedKey][j];
                                        if (typeof th === 'function') {{
                                            th(new CustomEvent('spl-touched', {{bubbles: true}}));
                                        }}
                                    }} catch(e) {{}}
                                }}
                            }}
                        }})()
                    """)
                    await asyncio.sleep(0.5)

                    # Verify Angular validity
                    ng_valid = await nd_page.evaluate("""
                        (function() {
                            var host = document.querySelector('spl-autocomplete');
                            if (!host) return 'no host';
                            return host.className.toString();
                        })()
                    """)
                    logger.info(f"City autocomplete Angular classes: {ng_valid}")
                    return True

            # Fallback: ArrowDown + Enter
            logger.info("City: CDP mouse click failed, trying ArrowDown+Enter")
            await nd_page.send(cdp.input_.dispatch_key_event(
                type_="keyDown", key="ArrowDown", code="ArrowDown"))
            await nd_page.send(cdp.input_.dispatch_key_event(
                type_="keyUp", key="ArrowDown", code="ArrowDown"))
            await asyncio.sleep(0.4)
            await nd_page.send(cdp.input_.dispatch_key_event(
                type_="keyDown", key="Enter", code="Enter"))
            await nd_page.send(cdp.input_.dispatch_key_event(
                type_="keyUp", key="Enter", code="Enter"))
            await asyncio.sleep(1)

            city_val = await self._nd_get_city_value(nd_page)
            logger.info(f"City value after ArrowDown+Enter: '{city_val}'")
            if city_val and len(str(city_val)) > 2:
                # Fire host events to sync Angular model (same as click path)
                await self._nd_commit_city_value(nd_page)
                return True

            # Last resort: press Enter to submit typed text as-is
            # Many SR city fields accept typed text without selecting from dropdown
            logger.info("City: no suggestion selected, pressing Enter to submit typed value as-is")
            await nd_page.send(cdp.input_.dispatch_key_event(
                type_="keyDown", key="Enter", code="Enter"))
            await nd_page.send(cdp.input_.dispatch_key_event(
                type_="keyUp", key="Enter", code="Enter"))
            await asyncio.sleep(1)

            city_val = await self._nd_get_city_value(nd_page)
            logger.info(f"City value after plain Enter: '{city_val}'")
            if city_val and len(str(city_val)) > 2:
                await self._nd_commit_city_value(nd_page)
                return True
            return False

        except Exception as e:
            logger.warning(f"City autocomplete fill error: {e}")
            return False

    async def _nd_commit_city_value(self, nd_page):
        """Fire host events to sync city value with Angular model after Enter/selection."""
        try:
            await nd_page.evaluate("""
                (function() {
                    var host = document.querySelector('spl-autocomplete');
                    if (!host) return;
                    // Fire change/blur/input events on host for Angular ControlValueAccessor
                    host.dispatchEvent(new Event('change', {bubbles: true, composed: true}));
                    host.dispatchEvent(new Event('input', {bubbles: true, composed: true}));
                    host.dispatchEvent(new Event('blur', {bubbles: true, composed: true}));
                    // Update Angular classes
                    host.classList.remove('ng-pristine', 'ng-untouched');
                    host.classList.add('ng-dirty', 'ng-touched');
                    // Also fire on inner input if accessible
                    if (host.shadowRoot) {
                        var inp = host.shadowRoot.querySelector('input');
                        if (inp) {
                            inp.dispatchEvent(new Event('change', {bubbles: true, composed: true}));
                            inp.dispatchEvent(new Event('blur', {bubbles: true, composed: true}));
                        }
                    }
                })()
            """)
        except Exception:
            pass

    async def _nd_get_city_value(self, nd_page) -> str:
        """Get the current value of the city autocomplete input."""
        return await nd_page.evaluate("""
            (function() {
                var host = document.querySelector('spl-autocomplete');
                if (!host || !host.shadowRoot) return '';
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
                return input ? input.value : '';
            })()
        """)

    async def _click_simplify_autofill(self, nd_page):
        """Click the Simplify Copilot autofill button if it appears on the page.

        Simplify shows a green 'Autofill' overlay button on job forms.
        We click it first and wait for it to fill, then verify/fix fields ourselves.
        """
        try:
            # Simplify's autofill button selectors (common patterns)
            autofill_selectors = [
                # Simplify overlay button
                '[class*="simplify"]',
                '#simplify-autofill',
                'button[class*="simplify"]',
                '[id*="simplify"]',
                # The extension injects elements with "simplify" in class/id
            ]
            for sel in autofill_selectors:
                try:
                    btn = await nd_page.query_selector(sel)
                    if btn:
                        logger.info(f"Found Simplify autofill element: {sel}")
                        await btn.click()
                        await asyncio.sleep(3)
                        logger.info("Clicked Simplify autofill — waiting 5s for fields to populate")
                        await asyncio.sleep(5)
                        return
                except Exception:
                    pass

            # Also try finding by text content
            for text in ["Autofill", "Fill with Simplify", "Auto-fill"]:
                try:
                    btn = await nd_page.find(text, best_match=True)
                    if btn:
                        tag = getattr(btn, 'tag_name', getattr(btn, 'tag', ''))
                        if tag and tag.lower() in ('button', 'div', 'span', 'a'):
                            logger.info(f"Found Simplify button by text: '{text}'")
                            await btn.click()
                            await asyncio.sleep(5)
                            logger.info("Clicked Simplify autofill button")
                            return
                except Exception:
                    pass

            logger.debug("No Simplify autofill button found — extension may autofill automatically")
        except Exception as e:
            logger.debug(f"Simplify autofill click attempt: {e}")

    def _is_closed_content(self, content: str) -> bool:
        """Check if page content indicates job is closed. Delegates to shared detection module."""
        return _shared_is_job_closed(content)

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

        # Wait for Simplify extension + resume parser to autofill
        # If Simplify is loaded, it fills fields through Angular (gold standard)
        # Resume parser also fills name/email/phone through Angular
        import asyncio as _asyncio
        await _asyncio.sleep(5)

        # Brief diagnostic to confirm form is loaded
        try:
            field_count = await nd_page.evaluate("""
                document.querySelectorAll('spl-input, spl-select, spl-textarea, spl-phone-field, spl-autocomplete').length
            """)
            logger.info(f"SmartRecruiters form: {field_count} spl-* components found")
        except Exception:
            pass

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
        # Normalize prefilled — nodriver may return list of pairs instead of dict
        prefilled = _normalize_nd_result(prefilled) or {}
        if isinstance(prefilled, list):
            try:
                prefilled = {k: v for k, v in prefilled}
            except (ValueError, TypeError):
                prefilled = {}
        if not isinstance(prefilled, dict):
            prefilled = {}
        logger.info(f"Pre-filled by resume parser: {list(prefilled.keys())}")

        # Fill order: Phone → Message → spl-input fields → City (ABSOLUTE LAST)
        # City MUST be last because filling spl-inputs triggers Angular re-renders
        # that clear the city autocomplete value. Phone/message go first because
        # they also trigger re-renders that clear spl-inputs.
        import nodriver.cdp as cdp
        fill_results = {}

        # CRITICAL: Activate this tab before sending any CDP Input events.
        # CDP mouse/key events go to the FOCUSED tab, not necessarily this one.
        try:
            await nd_page.activate()
            await asyncio.sleep(0.3)
        except Exception:
            pass

        # Fill phone — skip if extension already filled it
        phone = personal.get("phone", "").replace("-", "").replace(" ", "").replace("+1", "").replace("+", "")
        if phone and 'phone' not in prefilled:
            try:
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
        except Exception as e:
            logger.debug(f"Error filling message: {e}")

        # === PHASE 2: Fill spl-input fields (before city) ===
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

        # Click neutral to trigger blur (spl-change already synced Angular model)
        try:
            await nd_page.evaluate("document.querySelector('h2, h3, .section-title, body').click()")
        except Exception:
            pass
        await asyncio.sleep(0.5)

        # NOTE: Intermediate verify+refill pass removed — _nd_cdp_type_into_shadow now
        # dispatches spl-change on the host element, which syncs Angular's FormControl.
        # Values survive re-renders. Only the post-city verify (Phase 5) is needed.

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

        # === PHASE 4: Fill City LAST (autocomplete gets cleared by other field fills) ===
        city = personal.get("city", "") or personal.get("location", "")
        if city and 'city' not in prefilled:
            city_filled = await self._nd_fill_city_autocomplete(nd_page, city, cdp)
            fill_results["city"] = city_filled

        # === PHASE 5: FINAL RE-FILL — city selection triggers Angular re-render ===
        # Angular clears text fields when the city autocomplete value changes.
        # Re-check and re-fill everything one more time.
        await asyncio.sleep(0.5)
        for field_id, value in fields.items():
            if not value:
                continue
            try:
                curr = await nd_page.evaluate(f"""
                    (function() {{
                        var host = document.querySelector('#{field_id}');
                        if (!host || !host.shadowRoot) return '';
                        var inp = host.shadowRoot.querySelector('input');
                        return inp ? inp.value : '';
                    }})()
                """)
                if not curr or len(str(curr).strip()) < 2:
                    logger.info(f"Final re-fill: {field_id} (cleared by Angular after city)")
                    await self._nd_cdp_type_into_shadow(
                        nd_page, f"#{field_id}", value, input_selector='input'
                    )
                    fill_results[field_id] = True
                    await asyncio.sleep(0.2)
            except Exception:
                pass

        # Re-check phone after city fill
        if phone and fill_results.get("phone"):
            try:
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
                    logger.info("Final re-fill: phone (cleared by Angular after city)")
                    await self._nd_click_and_type_phone(nd_page, phone)
            except Exception:
                pass

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
        """Fill phone by CDP mouse click on tel input coordinates, then CDP key events.

        Uses coordinate-based approach (like city autocomplete) because CDP DOM.focus
        targets the wrong element when multiple tabs share the same persistent context.
        CDP mouse events always target the correct element at the correct coordinates.
        """
        import nodriver.cdp as cdp

        try:
            # Step 1: Get tel input coordinates via JS (works reliably through shadow DOM)
            coords_result = await nd_page.evaluate("""
                (function() {
                    var host = document.querySelector('spl-phone-field');
                    if (!host || !host.shadowRoot) return null;
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
                    if (!input) return null;
                    input.value = '';
                    input.dispatchEvent(new Event('input', {bubbles: true, composed: true}));
                    input.scrollIntoView({behavior: 'instant', block: 'center'});
                    var rect = input.getBoundingClientRect();
                    return {x: rect.x + rect.width/2, y: rect.y + rect.height/2, w: rect.width};
                })()
            """)
            coords = _normalize_nd_result(coords_result)
            if not coords or not isinstance(coords, dict) or coords.get('w', 0) <= 0:
                logger.info("Phone: no tel input coordinates found")
                return False

            # Step 2: Focus the inner tel input via JS, then CDP click for realism
            x, y = coords['x'], coords['y']

            # JS focus the inner tel input directly
            await nd_page.evaluate("""
                (function() {
                    var host = document.querySelector('spl-phone-field');
                    if (!host || !host.shadowRoot) return;
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
                    if (input) input.focus();
                })()
            """)
            await asyncio.sleep(0.1)

            # CDP mouse click for realistic interaction
            await nd_page.send(cdp.input_.dispatch_mouse_event(
                type_="mousePressed", x=x, y=y,
                button=cdp.input_.MouseButton.LEFT, click_count=1))
            await asyncio.sleep(0.05)
            await nd_page.send(cdp.input_.dispatch_mouse_event(
                type_="mouseReleased", x=x, y=y,
                button=cdp.input_.MouseButton.LEFT, click_count=1))
            await asyncio.sleep(0.2)

            # Re-focus inner input (click may have moved focus to host)
            await nd_page.evaluate("""
                (function() {
                    var host = document.querySelector('spl-phone-field');
                    if (!host || !host.shadowRoot) return;
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
                    if (input) { input.value = ''; input.focus(); }
                })()
            """)
            await asyncio.sleep(0.1)
            logger.info(f"Phone: CDP click at ({x:.0f}, {y:.0f}), inner input focused")

            # Step 3: Use Input.insertText — browser dispatches real events
            await nd_page.send(cdp.input_.insert_text(text=phone))
            await asyncio.sleep(0.3)

            # Tab out
            await nd_page.send(cdp.input_.dispatch_key_event(
                type_="keyDown", key="Tab", code="Tab"))
            await nd_page.send(cdp.input_.dispatch_key_event(
                type_="keyUp", key="Tab", code="Tab"))
            await asyncio.sleep(0.3)

            # Step 5: Verify the value was set
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
        """Type into a shadow DOM input using JS focus + CDP Input.insertText.

        Strategy:
        1. JS finds the inner input inside shadow DOM and focuses it
        2. CDP mouse click at coordinates for realistic browser interaction
        3. Verify inner input has focus (document.activeElement.shadowRoot.activeElement)
        4. CDP Select All + Delete to clear
        5. CDP Input.insertText — browser inserts text and dispatches real events
           that go through zone.js → Angular change detection
        6. Verify value was actually set
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

            # Step 2: Get coordinates, focus inner input via JS, then use CDP to type
            escaped = text.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")

            # Find inner input, scroll into view, focus it, and get coordinates.
            # If js_finder is provided (e.g. for deep shadow DOM elements), use it as the
            # complete setup evaluation — it must return {x, y, w, h, ...} or {error: '...'}.
            if js_finder:
                setup_result = await nd_page.evaluate(js_finder)
            else:
                setup_result = await nd_page.evaluate(f"""
                    (function() {{
                        function findInput(root) {{
                            if (!root) return null;
                            var inp = root.querySelector('{input_selector}');
                            if (inp) return inp;
                            var all = root.querySelectorAll('*');
                            for (var i = 0; i < all.length; i++) {{
                                if (all[i].shadowRoot) {{ var f = findInput(all[i].shadowRoot); if (f) return f; }}
                            }}
                            return null;
                        }}
                        var host = document.querySelector('{host_selector}');
                        if (!host) return {{error: 'NO_HOST'}};
                        var inp = host.shadowRoot ? findInput(host.shadowRoot) : null;
                        if (!inp) return {{error: 'NO_INPUT'}};
                        inp.scrollIntoView({{behavior: 'instant', block: 'center'}});
                        // Focus the INNER input directly — critical for CDP events to target it
                        inp.focus();
                        var rect = inp.getBoundingClientRect();
                        var focused = document.activeElement;
                        var shadowFocused = focused && focused.shadowRoot ? focused.shadowRoot.activeElement : null;
                        return {{
                            x: rect.x + rect.width/2,
                            y: rect.y + rect.height/2,
                            w: rect.width,
                            h: rect.height,
                            activeTag: focused ? focused.tagName : 'none',
                            activeId: focused ? focused.id : '',
                            shadowActiveTag: shadowFocused ? shadowFocused.tagName : 'none',
                            inputTag: inp.tagName
                        }};
                    }})()
                """)
            info = _normalize_nd_result(setup_result)

            if isinstance(info, dict) and info.get('error'):
                logger.debug(f"Cannot find {host_selector}: {info['error']} (field may not exist on this form)")
                return False

            if not info or not isinstance(info, dict) or info.get('w', 0) <= 0:
                logger.info(f"Cannot get coords for {host_selector}")
                return False

            x, y = info['x'], info['y']
            logger.debug(f"Shadow type {host_selector}: active={info.get('activeTag')}#{info.get('activeId')}, "
                        f"shadowActive={info.get('shadowActiveTag')}, input={info.get('inputTag')}")

            # CDP mouse click at input coordinates for realistic interaction
            await nd_page.send(cdp.input_.dispatch_mouse_event(
                type_="mousePressed", x=x, y=y,
                button=cdp.input_.MouseButton.LEFT, click_count=1))
            await asyncio.sleep(0.05)
            await nd_page.send(cdp.input_.dispatch_mouse_event(
                type_="mouseReleased", x=x, y=y,
                button=cdp.input_.MouseButton.LEFT, click_count=1))
            await asyncio.sleep(0.15)

            # Re-verify focus landed on inner input, fix if needed
            focus_check = await nd_page.evaluate(f"""
                (function() {{
                    function findInput(root) {{
                        if (!root) return null;
                        var inp = root.querySelector('{input_selector}');
                        if (inp) return inp;
                        var all = root.querySelectorAll('*');
                        for (var i = 0; i < all.length; i++) {{
                            if (all[i].shadowRoot) {{ var f = findInput(all[i].shadowRoot); if (f) return f; }}
                        }}
                        return null;
                    }}
                    var host = document.querySelector('{host_selector}');
                    var inp = host && host.shadowRoot ? findInput(host.shadowRoot) : null;
                    var focused = document.activeElement;
                    var shadowFocused = focused && focused.shadowRoot ? focused.shadowRoot.activeElement : null;
                    // If inner input doesn't have focus, force it
                    if (inp && shadowFocused !== inp) {{
                        inp.focus();
                        focused = document.activeElement;
                        shadowFocused = focused && focused.shadowRoot ? focused.shadowRoot.activeElement : null;
                    }}
                    return {{
                        focusOk: shadowFocused && (shadowFocused.tagName === 'INPUT' || shadowFocused.tagName === 'TEXTAREA'),
                        activeTag: focused ? focused.tagName : 'none',
                        shadowTag: shadowFocused ? shadowFocused.tagName : 'none'
                    }};
                }})()
            """)
            fc = _normalize_nd_result(focus_check) or {}
            logger.debug(f"Focus after click: ok={fc.get('focusOk')}, active={fc.get('activeTag')}, shadow={fc.get('shadowTag')}")

            # Clear existing content via JS (more reliable than Cmd+A/Delete which has timing issues)
            await nd_page.evaluate(f"""
                (function() {{
                    var host = document.querySelector('{host_selector}');
                    if (!host) return;
                    var sr = host.shadowRoot;
                    var inp = sr ? (sr.querySelector('textarea') || sr.querySelector('input')) : null;
                    if (inp) {{
                        inp.value = '';
                        inp.dispatchEvent(new Event('input', {{bubbles: true, composed: true}}));
                    }}
                }})()
            """)
            await asyncio.sleep(0.1)

            # PRIMARY: CDP Input.insertText + Angular model update via __ngContext__
            # Input.insertText sets the DOM value but Angular's model is separate.
            # We must also update Angular's internal model so values survive re-renders.
            await nd_page.send(cdp.input_.insert_text(text=text))
            await asyncio.sleep(0.15)

            # Update Angular's internal model via __ngContext__ + zone.js trigger
            angular_result = await nd_page.evaluate(f"""
                (function() {{
                    var host = document.querySelector('{host_selector}');
                    if (!host) return 'NO_HOST';

                    // Strategy 1: Find Angular component via __ngContext__ and call writeValue
                    var ctx = host.__ngContext__;
                    var componentSet = false;
                    if (ctx && Array.isArray(ctx)) {{
                        for (var i = 0; i < ctx.length; i++) {{
                            var item = ctx[i];
                            if (item && typeof item === 'object' && item !== null) {{
                                // Look for the component instance with writeValue or value setter
                                if (typeof item.writeValue === 'function') {{
                                    item.writeValue('{escaped}');
                                    if (typeof item.onChange === 'function') item.onChange('{escaped}');
                                    if (typeof item.onTouched === 'function') item.onTouched();
                                    componentSet = true;
                                    break;
                                }}
                                // Some components store value directly
                                if ('value' in item && typeof item.registerOnChange === 'function') {{
                                    item.value = '{escaped}';
                                    if (typeof item.onChange === 'function') item.onChange('{escaped}');
                                    componentSet = true;
                                    break;
                                }}
                            }}
                        }}
                    }}

                    // Strategy 2: Trigger zone.js-patched event handlers directly
                    // Zone.js stores original listeners in __zone_symbol__ properties
                    var zoneSymbols = Object.getOwnPropertyNames(host).filter(
                        function(k) {{ return k.indexOf('__zone_symbol__') === 0; }}
                    );

                    // Fire spl-change custom event (Angular listens for this on the host)
                    var splChangeKey = '__zone_symbol__spl-changefalse';
                    if (host[splChangeKey] && Array.isArray(host[splChangeKey])) {{
                        for (var i = 0; i < host[splChangeKey].length; i++) {{
                            try {{
                                var listener = host[splChangeKey][i];
                                var handler = listener.handler || listener;
                                if (typeof handler === 'function') {{
                                    handler(new CustomEvent('spl-change', {{
                                        detail: {{value: '{escaped}'}},
                                        bubbles: true
                                    }}));
                                }}
                            }} catch(e) {{}}
                        }}
                    }}

                    // Also fire spl-touched
                    var splTouchedKey = '__zone_symbol__spl-touchedfalse';
                    if (host[splTouchedKey] && Array.isArray(host[splTouchedKey])) {{
                        for (var i = 0; i < host[splTouchedKey].length; i++) {{
                            try {{
                                var listener = host[splTouchedKey][i];
                                var handler = listener.handler || listener;
                                if (typeof handler === 'function') {{
                                    handler(new CustomEvent('spl-touched', {{bubbles: true}}));
                                }}
                            }} catch(e) {{}}
                        }}
                    }}

                    // Strategy 3: Set host.value property (some Angular bindings read this)
                    try {{ host.value = '{escaped}'; }} catch(e) {{}}

                    // Verify
                    function findInput(root) {{
                        if (!root) return null;
                        var inp = root.querySelector('{input_selector}');
                        if (inp) return inp;
                        var all = root.querySelectorAll('*');
                        for (var i = 0; i < all.length; i++) {{
                            if (all[i].shadowRoot) {{ var f = findInput(all[i].shadowRoot); if (f) return f; }}
                        }}
                        return null;
                    }}
                    var inp = host.shadowRoot ? findInput(host.shadowRoot) : null;
                    var domVal = inp ? inp.value : 'NO_INPUT';

                    return {{
                        component: componentSet,
                        zoneSymbols: zoneSymbols.length,
                        domVal: domVal.substring(0, 30),
                        splChangeListeners: host[splChangeKey] ? host[splChangeKey].length : 0,
                        splTouchedListeners: host[splTouchedKey] ? host[splTouchedKey].length : 0
                    }};
                }})()
            """)
            ar = _normalize_nd_result(angular_result) or {}
            logger.info(f"Angular fill {host_selector}: component={ar.get('component')}, "
                       f"zoneSym={ar.get('zoneSymbols')}, splChange={ar.get('splChangeListeners')}, "
                       f"dom='{ar.get('domVal', '')}'")

            # Tab out to trigger blur/validation
            await nd_page.send(cdp.input_.dispatch_key_event(
                type_="keyDown", key="Tab", code="Tab"))
            await nd_page.send(cdp.input_.dispatch_key_event(
                type_="keyUp", key="Tab", code="Tab"))
            await asyncio.sleep(0.2)

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

        # Method D: Direct document.querySelector fallback for any file input on page
        try:
            result = await nd_page.send(
                cdp.runtime.evaluate(
                    expression="""
                        (function() {
                            // Try all file inputs including those in any shadow root
                            function findFileInput(root) {
                                var fi = root.querySelector('input[type="file"]');
                                if (fi) return fi;
                                var elems = root.querySelectorAll('*');
                                for (var i = 0; i < elems.length; i++) {
                                    if (elems[i].shadowRoot) {
                                        var found = findFileInput(elems[i].shadowRoot);
                                        if (found) return found;
                                    }
                                }
                                return null;
                            }
                            return findFileInput(document);
                        })()
                    """,
                    user_gesture=True,
                )
            )
            remote_obj = result[0] if isinstance(result, tuple) else result
            if remote_obj and hasattr(remote_obj, 'object_id') and remote_obj.object_id:
                await nd_page.send(
                    cdp.dom.set_file_input_files(
                        files=[abs_path],
                        object_id=remote_obj.object_id,
                    )
                )
                # Dispatch change event
                await nd_page.evaluate("""
                    (function() {
                        function findFileInput(root) {
                            var fi = root.querySelector('input[type="file"]');
                            if (fi) return fi;
                            var elems = root.querySelectorAll('*');
                            for (var i = 0; i < elems.length; i++) {
                                if (elems[i].shadowRoot) {
                                    var found = findFileInput(elems[i].shadowRoot);
                                    if (found) return found;
                                }
                            }
                            return null;
                        }
                        var fi = findFileInput(document);
                        if (fi) {
                            fi.dispatchEvent(new Event('change', {bubbles: true, composed: true}));
                            fi.dispatchEvent(new Event('input', {bubbles: true, composed: true}));
                        }
                    })()
                """)
                logger.info("Resume uploaded via Method D (deep shadow DOM traversal)")
                await asyncio.sleep(2)

                # Verify upload by checking for filename in DOM
                basename = os.path.basename(abs_path)
                verify = await nd_page.evaluate(f"""
                    document.body.innerText.includes('{basename}') ||
                    document.body.innerHTML.includes('{basename}')
                """)
                if verify:
                    logger.info(f"Resume upload verified — '{basename}' found in DOM")
                else:
                    logger.debug(f"Resume filename '{basename}' not found in DOM (may still be OK)")
                return True
        except Exception as e4:
            logger.debug(f"Method D (deep shadow traversal) failed: {e4}")

        logger.warning("All resume upload methods failed")
        return False

    async def _nd_handle_screening_questions(self, nd_page, job_data: Dict[str, Any]) -> None:
        """Handle screening/custom questions on SmartRecruiters forms.

        Directly scans spl-select, spl-input, spl-textarea, spl-radio components
        (the actual SmartRecruiters form elements) rather than relying on generic
        CSS containers.  Uses config regex patterns first, then AI answerer.
        """
        try:
            # Scroll to top to ensure all form fields are in viewport
            await nd_page.evaluate("window.scrollTo(0, 0)")
            await asyncio.sleep(0.5)

            # Pre-scan: log what form elements exist on page for debugging
            try:
                prescan = await nd_page.evaluate(_DEEP_QUERY_JS + """
                    (function() {
                        var s = deepQueryAll(document, 'spl-select').length;
                        var i = deepQueryAll(document, 'spl-input').length + deepQueryAll(document, 'spl-number-field').length;
                        var t = deepQueryAll(document, 'spl-textarea').length;
                        var cb = deepQueryAll(document, 'spl-checkbox').length;
                        var r = deepQueryAll(document, 'fieldset, [role="radiogroup"], spl-radio').length;
                        var hs = deepQueryAll(document, 'select').length;
                        var ac = deepQueryAll(document, 'spl-autocomplete').length;
                        // Sample labels using sibling-walking approach
                        function findLabel(el) {
                            var lbl = el.getAttribute('label') || el.getAttribute('aria-label') || '';
                            if (!lbl) {
                                var prev = el.previousElementSibling;
                                while (prev && !lbl) {
                                    var ptag = prev.tagName.toLowerCase();
                                    if (ptag === 'p' || ptag === 'label' || ptag === 'legend' || ptag === 'span') {
                                        var ptxt = prev.textContent.trim();
                                        if (ptxt.length > 2 && ptxt.length < 300) lbl = ptxt;
                                    }
                                    if (ptag === 'spl-input' || ptag === 'spl-select' || ptag === 'spl-textarea') break;
                                    prev = prev.previousElementSibling;
                                }
                            }
                            if (!lbl) {
                                var par = el.parentElement;
                                for (var u = 0; u < 3 && par && !lbl; u++) {
                                    var kids = par.children;
                                    for (var kk = 0; kk < kids.length; kk++) {
                                        var kt = kids[kk].tagName.toLowerCase();
                                        if ((kt === 'p' || kt === 'label' || kt === 'legend') && kids[kk] !== el) {
                                            var tx = kids[kk].textContent.trim();
                                            if (tx.length > 2 && tx.length < 300) { lbl = tx; break; }
                                        }
                                    }
                                    par = par.parentElement;
                                }
                            }
                            return lbl;
                        }
                        var labels = [];
                        var sels = deepQueryAll(document, 'spl-select');
                        for (var j = 0; j < Math.min(sels.length, 5); j++) {
                            var lbl = findLabel(sels[j]);
                            var sr = sels[j].shadowRoot;
                            var selIdx = sr ? (sr.querySelector('select') || {}).selectedIndex : -1;
                            labels.push(lbl.substring(0, 40) + '(idx=' + selIdx + ')');
                        }
                        var inputLabels = [];
                        var inps = deepQueryAll(document, 'spl-input');
                        for (var j2 = 0; j2 < Math.min(inps.length, 5); j2++) {
                            inputLabels.push(findLabel(inps[j2]).substring(0, 40));
                        }
                        return 'spl-select:' + s + ' spl-input:' + i + ' spl-textarea:' + t +
                               ' spl-checkbox:' + cb + ' spl-autocomplete:' + ac + ' fieldset/radio:' + r + ' html-select:' + hs +
                               ' select-labels=[' + labels.join(', ') + ']' +
                               ' input-labels=[' + inputLabels.join(', ') + ']';
                    })()
                """)
                logger.info(f"Screening pre-scan: {prescan}")
            except Exception as ps_e:
                logger.debug(f"Pre-scan failed: {ps_e}")

            # Detect ALL question fields on the page (spl-select, spl-input, spl-textarea, spl-radio)
            # excluding standard personal info fields.
            # SR screening questions live inside oc-screening-questions →
            # sr-screening-questions-form shadow DOM. We drill into the correct
            # shadow root first so that sibling/parent label walking works correctly.
            questions_data = await nd_page.evaluate(_DEEP_QUERY_JS + """
                (function() {
                    var questions = [];
                    var knownIds = [
                        'first-name-input', 'last-name-input', 'email-input',
                        'confirm-email-input', 'linkedin-input', 'website-input',
                        'hiring-manager-message-input'
                    ];
                    var knownLabels = [
                        'first name', 'last name', 'email', 'confirm your email',
                        'phone', 'linkedin', 'website', 'resume', 'cv',
                        'cover letter', 'message to hiring', 'city'
                    ];

                    // Find the shadow root that actually contains the form fields.
                    // SR renders screening questions inside nested custom elements:
                    //   sr-screening-questions-form > shadow root > {form content}
                    // Working INSIDE that shadow root lets sibling/parent label walking work.
                    function findFormRoot() {
                        // Return the shadow root of the OUTERMOST screening form container.
                        // Do NOT drill deeper — form fields are nested inside oc-question
                        // shadow roots within the form; deepQueryAll handles that in the scan.
                        var candidates = [
                            'sr-screening-questions-form',
                            'oc-screening-questions-form',
                            'oc-screening-questions',
                        ];
                        for (var ci = 0; ci < candidates.length; ci++) {
                            var containers = deepQueryAll(document, candidates[ci]);
                            for (var ki = 0; ki < containers.length; ki++) {
                                if (containers[ki].shadowRoot) {
                                    return containers[ki].shadowRoot;
                                }
                            }
                        }
                        return document;  // fallback: search from document
                    }
                    var searchRoot = findFormRoot();
                    var rootTag = (searchRoot === document) ? 'document' :
                        (searchRoot.host ? searchRoot.host.tagName.toLowerCase() : 'shadow');

                    function getLabel(el) {
                        var lbl = '';

                        // Strategy 1: element attributes
                        lbl = el.getAttribute('label') || el.getAttribute('aria-label') || '';

                        // Strategy 1b: shadow DOM internal label
                        if (!lbl && el.shadowRoot) {
                            var l = el.shadowRoot.querySelector('label, .label, legend');
                            if (l) lbl = l.textContent.trim();
                        }

                        // Strategy 2: walk previous siblings looking for label-like elements
                        // SR renders screening questions as <p>Question?</p> <spl-input>...
                        // OR as <div class="...">Question?</div> <spl-input>...
                        if (!lbl) {
                            var prev = el.previousElementSibling;
                            var prevDepth = 0;
                            while (prev && !lbl && prevDepth < 5) {
                                prevDepth++;
                                var tag = prev.tagName.toLowerCase();
                                if (tag === 'p' || tag === 'label' || tag === 'legend' || tag === 'span' || tag === 'div' || tag === 'h3' || tag === 'h4') {
                                    var ptxt = prev.textContent.trim();
                                    if (ptxt.length > 2 && ptxt.length < 300) {
                                        lbl = ptxt;
                                    }
                                }
                                // Stop walking if we hit another input element (belongs to previous question)
                                if (tag === 'spl-input' || tag === 'spl-select' || tag === 'spl-textarea'
                                    || tag === 'spl-radio' || tag === 'fieldset') {
                                    break;
                                }
                                prev = prev.previousElementSibling;
                            }
                        }

                        // Strategy 2b: if inside oc-question shadow root, look at the host's light DOM siblings
                        if (!lbl) {
                            var root = el.getRootNode();
                            if (root && root.host && root.host.tagName) {
                                var hostTag = root.host.tagName.toLowerCase();
                                if (hostTag === 'oc-question' || hostTag === 'sr-screening-questions-form') {
                                    // Look at siblings of the oc-question host element
                                    var hostPrev = root.host.previousElementSibling;
                                    var hDepth = 0;
                                    while (hostPrev && !lbl && hDepth < 3) {
                                        hDepth++;
                                        var htag = hostPrev.tagName.toLowerCase();
                                        if (htag === 'p' || htag === 'label' || htag === 'div' || htag === 'span') {
                                            var htxt = hostPrev.textContent.trim();
                                            if (htxt.length > 2 && htxt.length < 300) lbl = htxt;
                                        }
                                        if (htag === 'oc-question' || htag === 'spl-input' || htag === 'spl-select') break;
                                        hostPrev = hostPrev.previousElementSibling;
                                    }
                                    // Also check inside oc-question's OWN shadow root for label text
                                    if (!lbl && root.host.shadowRoot) {
                                        var labelEl = root.host.shadowRoot.querySelector('label, .label, [class*="label"], p, legend');
                                        if (labelEl && labelEl.textContent.trim().length > 2) lbl = labelEl.textContent.trim();
                                    }
                                }
                            }
                        }

                        // Strategy 2c: SR-specific — check sr-question-field-* host element text
                        // SR forms wrap each question in <sr-question-field-text> or <sr-question-field-select>
                        // These hold the label text as their overall textContent (minus the input value)
                        if (!lbl) {
                            var sqfEl = el;
                            var sqfDepth = 0;
                            while (sqfEl && sqfDepth < 8 && !lbl) {
                                sqfDepth++;
                                var sqfTag = (sqfEl.tagName || '').toLowerCase();
                                if (sqfTag.indexOf('sr-question-field') === 0 ||
                                    sqfTag.indexOf('oc-question-field') === 0 ||
                                    sqfTag.indexOf('sr-question') === 0) {
                                    // The textContent of this element IS the label (extract without child input text)
                                    // Use the label attribute if present, else try innerHTML text nodes
                                    var sqfLbl = sqfEl.getAttribute('label') || sqfEl.getAttribute('aria-label') || '';
                                    if (!sqfLbl) {
                                        // Walk light DOM children looking for label/p/div before inputs
                                        var sqfKids = sqfEl.children;
                                        for (var sq = 0; sq < sqfKids.length; sq++) {
                                            var sqt = sqfKids[sq].tagName.toLowerCase();
                                            if (sqt === 'label' || sqt === 'p' || sqt === 'div' || sqt === 'span') {
                                                var sqtxt = sqfKids[sq].textContent.trim();
                                                if (sqtxt.length > 2 && sqtxt.length < 300) { sqfLbl = sqtxt; break; }
                                            }
                                            if (sqt.indexOf('spl-') === 0 || sqt.indexOf('sr-') === 0) break;
                                        }
                                    }
                                    // Also try the shadowRoot label element
                                    if (!sqfLbl && sqfEl.shadowRoot) {
                                        var sqfShadowLbl = sqfEl.shadowRoot.querySelector('label, .label, legend, [class*="label"]');
                                        if (sqfShadowLbl) sqfLbl = sqfShadowLbl.textContent.trim();
                                    }
                                    if (sqfLbl && sqfLbl.length > 2) lbl = sqfLbl;
                                }
                                // Walk up through shadow DOM boundaries too
                                var nextP = sqfEl.parentElement;
                                if (!nextP && sqfEl.getRootNode) {
                                    var rn3 = sqfEl.getRootNode();
                                    nextP = (rn3 && rn3.host) ? rn3.host : null;
                                }
                                sqfEl = nextP;
                            }
                        }

                        // Strategy 3: walk up parents (up to 6 levels), look for child label-like elements
                        if (!lbl) {
                            var parentW = el;
                            for (var up = 0; up < 6 && parentW && !lbl; up++) {
                                // Cross shadow DOM boundary if needed
                                var parentEl = parentW.parentElement;
                                if (!parentEl && parentW.getRootNode) {
                                    var rn3b = parentW.getRootNode();
                                    parentEl = (rn3b && rn3b.host) ? rn3b.host : null;
                                }
                                parentW = parentEl;
                                if (!parentW) break;
                                var kids = parentW.children;
                                for (var k = 0; k < kids.length; k++) {
                                    var ktag = kids[k].tagName.toLowerCase();
                                    if ((ktag === 'p' || ktag === 'label' || ktag === 'legend' || ktag === 'span' || ktag === 'div' || ktag === 'h3' || ktag === 'h4')
                                        && kids[k] !== el) {
                                        var txt = kids[k].textContent.trim();
                                        if (txt.length > 2 && txt.length < 300) {
                                            lbl = txt;
                                            break;
                                        }
                                    }
                                }
                            }
                        }

                        // Strategy 4: closest question/field container
                        if (!lbl) {
                            var container = el.closest('.field, .form-group, [class*="field"], [class*="question"], [class*="screening"], oc-question, fieldset, .spl-mb-1, .spl-flex-col, sr-question-field-text, sr-question-field-select');
                            if (container) {
                                var l2 = container.querySelector('label, legend, .label, p, span.question-text, [class*="question"], [class*="label"]');
                                if (l2 && l2 !== el) lbl = l2.textContent.trim();
                            }
                        }

                        // Strategy 5: parent's direct text nodes and non-input children
                        if (!lbl && el.parentElement) {
                            var ptext = '';
                            var pChildren = el.parentElement.childNodes;
                            for (var ci = 0; ci < pChildren.length; ci++) {
                                if (pChildren[ci].nodeType === 3) {
                                    ptext += pChildren[ci].textContent.trim() + ' ';
                                } else if (pChildren[ci] !== el && pChildren[ci].tagName !== 'SPL-SELECT' &&
                                           pChildren[ci].tagName !== 'SPL-INPUT' && pChildren[ci].tagName !== 'INPUT') {
                                    var ctxt = (pChildren[ci].textContent || '').trim();
                                    if (ctxt.length > 2 && ctxt.length < 500) ptext += ctxt + ' ';
                                }
                            }
                            ptext = ptext.trim();
                            if (ptext.length > 2) lbl = ptext.substring(0, 300);
                        }

                        // Strategy 6: shadow DOM placeholder as last resort
                        if (!lbl && el.shadowRoot) {
                            var inp = el.shadowRoot.querySelector('input, textarea, select');
                            if (inp) lbl = inp.getAttribute('placeholder') || '';
                        }

                        return lbl;
                    }

                    function isKnown(id, label) {
                        if (knownIds.indexOf(id) >= 0) return true;
                        var ll = label.toLowerCase();
                        for (var i = 0; i < knownLabels.length; i++) {
                            if (ll.indexOf(knownLabels[i]) >= 0) return true;
                        }
                        return false;
                    }

                    // === spl-select dropdowns (screening questions like work auth, education) ===
                    // deepQueryAll from searchRoot pierces oc-question shadow roots within the form
                    var selects = deepQueryAll(searchRoot, 'spl-select');
                    for (var i = 0; i < selects.length; i++) {
                        var id = selects[i].id || 'spl-select-' + i;
                        var label = getLabel(selects[i]);
                        if (!label || label.length < 3) continue;
                        if (isKnown(id, label)) continue;
                        // Get current value and options
                        var val = '', opts = [];
                        var inner = null;
                        if (selects[i].shadowRoot) {
                            inner = selects[i].shadowRoot.querySelector('select');
                            if (inner) {
                                val = inner.value || '';
                                for (var o = 0; o < inner.options.length; o++) {
                                    var ot = (inner.options[o].text || '').trim();
                                    if (ot && ot !== 'Select...' && ot !== 'Choose...' && ot !== '--'
                                        && ot.indexOf('Select') !== 0) {
                                        opts.push(ot);
                                    }
                                }
                            }
                        }
                        // Skip if already has non-default selection
                        if (inner && inner.selectedIndex > 0) continue;
                        var req = selects[i].hasAttribute('required');
                        questions.push({id: id, label: label, type: 'select', options: opts,
                                        required: req, tagName: 'spl-select', idx: i, deep_idx: i});
                    }

                    // === spl-input / spl-number-field text fields (screening questions) ===
                    // spl-number-field is used for numeric inputs (e.g. salary)
                    var inputs = deepQueryAll(searchRoot, 'spl-input, spl-number-field');
                    for (var j = 0; j < inputs.length; j++) {
                        var jid = inputs[j].id || 'spl-input-' + j;
                        var jlabel = getLabel(inputs[j]);
                        if (!jlabel || jlabel.length < 3) continue;
                        if (isKnown(jid, jlabel)) continue;
                        // Check if already filled
                        var jval = '';
                        if (inputs[j].shadowRoot) {
                            var jinp = inputs[j].shadowRoot.querySelector('input');
                            jval = jinp ? jinp.value : '';
                        }
                        if (jval && jval.trim().length > 1) continue;
                        var jreq = inputs[j].hasAttribute('required');
                        // Use actual tag name so fill knows whether it's spl-input or spl-number-field
                        var jTagName = inputs[j].tagName.toLowerCase();
                        questions.push({id: jid, label: jlabel, type: 'text', options: [],
                                        required: jreq, tagName: jTagName, idx: j, deep_idx: j});
                    }

                    // === spl-textarea (longer answers) ===
                    var textareas = deepQueryAll(searchRoot, 'spl-textarea');
                    for (var t = 0; t < textareas.length; t++) {
                        var tid = textareas[t].id || 'spl-textarea-' + t;
                        var tlabel = getLabel(textareas[t]);
                        if (!tlabel || tlabel.length < 3) continue;
                        if (isKnown(tid, tlabel)) continue;
                        var tval = '';
                        if (textareas[t].shadowRoot) {
                            var ta = textareas[t].shadowRoot.querySelector('textarea');
                            tval = ta ? ta.value : '';
                        }
                        if (tval && tval.trim().length > 1) continue;
                        var treq = textareas[t].hasAttribute('required');
                        questions.push({id: tid, label: tlabel, type: 'textarea', options: [],
                                        required: treq, tagName: 'spl-textarea', idx: t, deep_idx: t});
                    }

                    // === Radio groups (Yes/No screening, EEO) ===
                    // Look for fieldsets, spl-radio, or [role="radiogroup"] with labels
                    var radioGroups = deepQueryAll(searchRoot,
                        'fieldset, [role="radiogroup"], spl-radio'
                    );
                    for (var r = 0; r < radioGroups.length; r++) {
                        var rEl = radioGroups[r];
                        var rlabel = '';
                        var legend = rEl.querySelector('legend');
                        if (legend) rlabel = legend.textContent.trim();
                        if (!rlabel) rlabel = getLabel(rEl);
                        if (!rlabel || rlabel.length < 3) continue;
                        if (isKnown(rEl.id || '', rlabel)) continue;

                        // Find radio inputs
                        var radios = rEl.querySelectorAll('input[type="radio"]');
                        if (radios.length === 0 && rEl.shadowRoot) {
                            radios = rEl.shadowRoot.querySelectorAll('input[type="radio"]');
                        }
                        if (radios.length === 0) continue;

                        // Check if already selected
                        var anyChecked = false;
                        var rOpts = [];
                        for (var ri = 0; ri < radios.length; ri++) {
                            if (radios[ri].checked) anyChecked = true;
                            var rl = radios[ri].closest('label');
                            if (!rl && radios[ri].id) {
                                rl = rEl.querySelector('label[for="' + radios[ri].id + '"]');
                            }
                            if (rl) rOpts.push(rl.textContent.trim());
                            else rOpts.push(radios[ri].value || 'Option ' + (ri+1));
                        }
                        if (anyChecked) continue;

                        questions.push({id: rEl.id || 'radio-' + r, label: rlabel, type: 'radio',
                                        options: rOpts, required: rEl.hasAttribute('required'),
                                        tagName: 'fieldset', idx: r, deep_idx: r});
                    }

                    // === Fallback: radio inputs NOT in fieldset/radiogroup (e.g. SR oc-question shadow roots) ===
                    // Collect names already handled by the fieldset loop above
                    var _seenRadioNames = {};
                    for (var _rg = 0; _rg < radioGroups.length; _rg++) {
                        var _rInputs = radioGroups[_rg].querySelectorAll('input[type="radio"]');
                        if (_rInputs.length === 0 && radioGroups[_rg].shadowRoot)
                            _rInputs = radioGroups[_rg].shadowRoot.querySelectorAll('input[type="radio"]');
                        for (var _ri = 0; _ri < _rInputs.length; _ri++) {
                            if (_rInputs[_ri].name) _seenRadioNames[_rInputs[_ri].name] = true;
                        }
                    }
                    var _allRadios = deepQueryAll(searchRoot, 'input[type="radio"]');
                    var _radioByName = {};
                    for (var _rri = 0; _rri < _allRadios.length; _rri++) {
                        var _rInp = _allRadios[_rri];
                        var _rn = _rInp.name || ('radio-anon-' + _rri);
                        if (_seenRadioNames[_rn]) continue;
                        if (!_radioByName[_rn]) _radioByName[_rn] = {inputs: [], label: '', anyChecked: false};
                        _radioByName[_rn].inputs.push(_rInp);
                        if (_rInp.checked) _radioByName[_rn].anyChecked = true;
                    }
                    var _rnKeys = Object.keys(_radioByName);
                    for (var _rnk = 0; _rnk < _rnKeys.length; _rnk++) {
                        var _rgrp = _radioByName[_rnKeys[_rnk]];
                        if (_rgrp.anyChecked) continue;
                        // Walk up DOM from first radio to find question label text
                        var _walkEl = _rgrp.inputs[0].parentElement;
                        var _rlabel2 = '';
                        for (var _d = 0; _d < 8 && _walkEl && !_rlabel2; _d++) {
                            var _children = _walkEl.childNodes;
                            for (var _ci = 0; _ci < _children.length; _ci++) {
                                var _cn = _children[_ci];
                                var _ct = (_cn.textContent || '').trim();
                                if (_cn.nodeType === 3 && _ct.length > 5) { _rlabel2 = _ct; break; }
                                if (_cn.nodeType === 1 && !['INPUT','LABEL','BUTTON'].includes(_cn.tagName)
                                    && _ct.length > 5 && _ct.length < 250) { _rlabel2 = _ct; break; }
                            }
                            var _parentEl = _walkEl.parentElement;
                            if (!_parentEl && _walkEl.getRootNode) {
                                var _rn2 = _walkEl.getRootNode();
                                _parentEl = (_rn2 && _rn2.host) ? _rn2.host.parentElement : null;
                            }
                            _walkEl = _parentEl;
                        }
                        if (!_rlabel2 || _rlabel2.length < 3) continue;
                        if (isKnown(_rnKeys[_rnk], _rlabel2)) continue;
                        var _rOpts2 = _rgrp.inputs.map(function(inp) {
                            var lbl = inp.closest('label');
                            return lbl ? lbl.textContent.trim() : (inp.value || 'Option');
                        });
                        questions.push({id: _rnKeys[_rnk], label: _rlabel2, type: 'radio',
                                        options: _rOpts2, required: true,
                                        tagName: 'input-radio', idx: _rnk, deep_idx: _rnk});
                    }

                    // === Standard HTML selects not inside spl-* ===
                    var htmlSelects = deepQueryAll(searchRoot, 'select');
                    for (var h = 0; h < htmlSelects.length; h++) {
                        // Skip if inside an spl-select (already handled)
                        if (htmlSelects[h].closest('spl-select')) continue;
                        var hid = htmlSelects[h].id || htmlSelects[h].name || 'select-' + h;
                        var hlabel = '';
                        var hLblEl = htmlSelects[h].closest('label') ||
                                     searchRoot.querySelector('label[for="' + hid + '"]');
                        if (hLblEl) hlabel = hLblEl.textContent.trim();
                        if (!hlabel) hlabel = htmlSelects[h].getAttribute('aria-label') || hid;
                        if (isKnown(hid, hlabel)) continue;
                        if (htmlSelects[h].selectedIndex > 0) continue;
                        var hOpts = [];
                        for (var ho = 0; ho < htmlSelects[h].options.length; ho++) {
                            var hot = (htmlSelects[h].options[ho].text || '').trim();
                            if (hot && hot !== 'Select...' && hot !== '--') hOpts.push(hot);
                        }
                        questions.push({id: hid, label: hlabel, type: 'select', options: hOpts,
                                        required: htmlSelects[h].required, tagName: 'select', idx: h});
                    }

                    return JSON.stringify({questions: questions, root: rootTag});
                })()
            """)

            import json as _json
            raw = _json.loads(questions_data) if isinstance(questions_data, str) else {}
            if isinstance(raw, dict):
                questions = raw.get('questions', [])
                logger.info(f"SR scan root: {raw.get('root', '?')} | found {len(questions)} questions")
            else:
                questions = raw if isinstance(raw, list) else []

            if not questions:
                logger.debug("No screening questions found on this page")
                # Debug: dump DOM structure around spl-inputs to diagnose label extraction failure
                if raw and isinstance(raw, dict) and raw.get('root') == 'sr-screening-questions-form':
                    try:
                        dom_dump = await nd_page.evaluate(_DEEP_QUERY_JS + """
                            (function() {
                                var sr = deepQueryAll(document, 'sr-screening-questions-form')[0];
                                if (!sr || !sr.shadowRoot) return 'NO_SR_FORM';
                                var inputs = deepQueryAll(sr.shadowRoot, 'spl-input, spl-number-field');
                                if (inputs.length === 0) return 'NO_INPUTS';
                                var result = [];
                                for (var i = 0; i < Math.min(inputs.length, 3); i++) {
                                    var el = inputs[i];
                                    var info = {id: el.id, tag: el.tagName, attrs: {}};
                                    // Collect all attributes
                                    for (var a = 0; a < el.attributes.length; a++) {
                                        info.attrs[el.attributes[a].name] = el.attributes[a].value;
                                    }
                                    // Parent chain
                                    var parents = [];
                                    var p = el.parentElement;
                                    for (var d = 0; d < 6 && p; d++) {
                                        var ptxt = p.textContent ? p.textContent.trim().substring(0, 80) : '';
                                        parents.push({tag: p.tagName, id: p.id || '', cls: (p.className || '').substring(0, 40), txt: ptxt});
                                        var nextP = p.parentElement;
                                        if (!nextP && p.getRootNode) {
                                            var rn = p.getRootNode();
                                            nextP = (rn && rn.host) ? rn.host : null;
                                        }
                                        p = nextP;
                                    }
                                    info.parents = parents;
                                    // Previous siblings
                                    var sibs = [];
                                    var prev = el.previousElementSibling;
                                    for (var s = 0; s < 3 && prev; s++) {
                                        sibs.push({tag: prev.tagName, id: prev.id || '', txt: (prev.textContent||'').trim().substring(0,80)});
                                        prev = prev.previousElementSibling;
                                    }
                                    info.prevSibs = sibs;
                                    result.push(info);
                                }
                                return JSON.stringify(result);
                            })()
                        """)
                        logger.info(f"SR DOM structure debug (for label fix): {dom_dump}")
                    except Exception as dbg_e:
                        logger.debug(f"DOM debug failed: {dbg_e}")
                return

            logger.info(f"Found {len(questions)} screening questions: {[q['label'][:40] for q in questions]}")

            for q in questions:
                question_text = q["label"]
                field_type = q["type"]
                options = q.get("options", [])
                tag_name = q.get("tagName", "")
                q_idx = q.get("idx", 0)

                logger.info(f"Screening Q: '{question_text[:60]}' type={field_type} opts={options[:5]}")

                # Skip social media fields entirely
                q_lower = question_text.lower().strip()
                if any(x in q_lower for x in ['facebook', 'twitter', 'instagram', 'tiktok', 'snapchat', 'x (fka']):
                    logger.info(f"Skipping social media field: {question_text[:50]}")
                    continue

                # Check if required — skip optional unless it's LinkedIn/GitHub
                is_required = '*' in question_text or q.get('required', False)
                is_linkedin_github = any(x in q_lower for x in ['linkedin', 'github', 'portfolio'])
                if not is_required and not is_linkedin_github:
                    logger.info(f"Skipping optional screening Q: {question_text[:50]}")
                    continue

                try:
                    # Single call to answer_question — handles template bank → config →
                    # option matching → cache → AI in the correct priority order
                    answer = None
                    if hasattr(self, 'ai_answerer') and self.ai_answerer:
                        effective_type = field_type
                        if field_type == "radio":
                            effective_type = "select"
                        max_len = 500 if field_type == "textarea" else 200
                        answer = await self.ai_answerer.answer_question(
                            question_text, effective_type,
                            options=options if options else None,
                            max_length=max_len
                        )

                    if not answer:
                        logger.warning(f"SR FILL: No answer for '{question_text[:50]}' (type={field_type}, options={options[:3] if options else 'none'})")
                        continue

                    logger.info(f"SR FILL: '{question_text[:50]}' -> '{answer[:40]}' (type={field_type})")
                    # Fill the answer based on field type
                    escaped_answer = answer.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")

                    if field_type == "select":
                        # Fill spl-select or standard select.
                        # deepQueryAll pierces nested shadow roots for SR screening page.
                        q_actual_id = q.get('id', '')
                        q_deep_idx = q.get('deep_idx', q_idx)
                        fill_result = await nd_page.evaluate(_DEEP_QUERY_JS + f"""
                            (function() {{
                                var answer = '{escaped_answer}'.toLowerCase();
                                var tagName = '{tag_name}';
                                // Use deepQueryAll for spl-select; plain querySelectorAll for HTML select
                                var allEls = (tagName === 'select')
                                    ? Array.from(document.querySelectorAll('select'))
                                    : deepQueryAll(document, tagName);
                                // Find by actual ID first, then by deep index
                                var qId = '{q_actual_id}';
                                var el = null;
                                if (qId && qId.indexOf('spl-') !== 0 && qId.indexOf('select-') !== 0) {{
                                    for (var fi = 0; fi < allEls.length; fi++) {{
                                        if (allEls[fi].id === qId) {{ el = allEls[fi]; break; }}
                                    }}
                                }}
                                if (!el) el = allEls[{q_deep_idx}];
                                if (!el) return 'NOT_FOUND';

                                var select = (el.tagName === 'SELECT') ? el :
                                    (el.shadowRoot ? el.shadowRoot.querySelector('select') : null);
                                if (!select) return 'NO_SELECT';

                                // Try exact match first, then partial match
                                var bestIdx = -1, bestScore = 0;
                                for (var i = 0; i < select.options.length; i++) {{
                                    var optText = (select.options[i].text || '').trim().toLowerCase();
                                    if (!optText || optText === 'select...' || optText === '--') continue;
                                    // Exact match
                                    if (optText === answer) {{ bestIdx = i; bestScore = 100; break; }}
                                    // Contains match
                                    if (optText.indexOf(answer) >= 0 || answer.indexOf(optText) >= 0) {{
                                        var score = optText.length;
                                        if (score > bestScore) {{ bestIdx = i; bestScore = score; }}
                                    }}
                                }}
                                function setSelectAndTriggerAngular(select, idx, el) {{
                                    select.selectedIndex = idx;
                                    select.dispatchEvent(new Event('change', {{bubbles: true, composed: true}}));
                                    select.dispatchEvent(new Event('input', {{bubbles: true, composed: true}}));
                                    // Trigger zone.js spl-change on host for Angular model
                                    if (el !== select) {{
                                        try {{ el.value = select.options[idx].value; }} catch(e) {{}}
                                        var splKey = '__zone_symbol__spl-changefalse';
                                        if (el[splKey] && Array.isArray(el[splKey])) {{
                                            for (var z = 0; z < el[splKey].length; z++) {{
                                                try {{
                                                    var h = el[splKey][z].handler || el[splKey][z];
                                                    if (typeof h === 'function') {{
                                                        h(new CustomEvent('spl-change', {{
                                                            detail: {{value: select.options[idx].value}}, bubbles: true
                                                        }}));
                                                    }}
                                                }} catch(e) {{}}
                                            }}
                                        }}
                                        el.dispatchEvent(new Event('change', {{bubbles: true}}));
                                    }}
                                }}

                                if (bestIdx >= 0) {{
                                    setSelectAndTriggerAngular(select, bestIdx, el);
                                    return 'OK:' + select.options[bestIdx].text;
                                }}
                                // Normalize true/false → yes/no for matching
                                var normAnswer = answer;
                                if (normAnswer === 'true') normAnswer = 'yes';
                                if (normAnswer === 'false') normAnswer = 'no';
                                // Fallback: match on yes/no
                                for (var j = 0; j < select.options.length; j++) {{
                                    var ot = select.options[j].text.toLowerCase().trim();
                                    if (!ot || ot === 'select...' || ot === '--') continue;
                                    if (ot === normAnswer || ot === answer) {{
                                        setSelectAndTriggerAngular(select, j, el);
                                        return 'OK_YESNO:' + select.options[j].text;
                                    }}
                                }}
                                // Fallback: partial word match (e.g. answer "linkedin" matches "LinkedIn/Social")
                                for (var j = 0; j < select.options.length; j++) {{
                                    var ot = select.options[j].text.toLowerCase().trim();
                                    if (!ot || ot === 'select...' || ot === '--') continue;
                                    if (normAnswer.length > 2 && (ot.indexOf(normAnswer) >= 0 || normAnswer.indexOf(ot) >= 0)) {{
                                        setSelectAndTriggerAngular(select, j, el);
                                        return 'OK_PARTIAL:' + select.options[j].text;
                                    }}
                                }}
                                return 'NO_MATCH:opts=' + select.options.length + ',answer=' + answer;
                            }})()
                        """)
                        logger.info(f"Select fill: '{question_text[:30]}' -> {fill_result}")

                    elif field_type == "radio":
                        # Click the correct radio button
                        q_deep_idx_radio = q.get('deep_idx', q_idx)
                        q_radio_tag = q.get('tagName', 'fieldset')
                        q_radio_name = q.get('id', '')  # for input-radio, id = name attribute
                        fill_result = await nd_page.evaluate(_DEEP_QUERY_JS + f"""
                            (function() {{
                                var answer = '{escaped_answer}'.toLowerCase();

                                // For radio inputs found by name (not wrapped in fieldset/radiogroup)
                                if ('{q_radio_tag}' === 'input-radio') {{
                                    var radioName = '{q_radio_name}';
                                    var allRadios = deepQueryAll(document, 'input[type="radio"]');
                                    var namedRadios = allRadios.filter(function(r) {{
                                        return r.name === radioName;
                                    }});
                                    if (namedRadios.length === 0) return 'NOT_FOUND_BY_NAME';
                                    for (var ni = 0; ni < namedRadios.length; ni++) {{
                                        var lbl = namedRadios[ni].closest('label');
                                        var lt = lbl ? lbl.textContent.trim().toLowerCase() : '';
                                        var val = (namedRadios[ni].value || '').toLowerCase();
                                        if (lt === answer || val === answer ||
                                            lt.indexOf(answer) >= 0 || answer.indexOf(lt) >= 0 ||
                                            (answer === 'no' && (val === 'false' || lt === 'no')) ||
                                            (answer === 'yes' && (val === 'true' || lt === 'yes'))) {{
                                            namedRadios[ni].click();
                                            namedRadios[ni].dispatchEvent(new Event('change', {{bubbles:true, composed:true}}));
                                            return 'OK_NAME:' + (lbl ? lbl.textContent.trim() : val);
                                        }}
                                    }}
                                    // Fallback: pick yes/no by position (first=yes for yes/no questions)
                                    if ((answer === 'no' || answer === 'false') && namedRadios.length >= 2) {{
                                        namedRadios[1].click();
                                        namedRadios[1].dispatchEvent(new Event('change', {{bubbles:true, composed:true}}));
                                        return 'OK_NAME_POS1';
                                    }}
                                    if ((answer === 'yes' || answer === 'true') && namedRadios.length >= 1) {{
                                        namedRadios[0].click();
                                        namedRadios[0].dispatchEvent(new Event('change', {{bubbles:true, composed:true}}));
                                        return 'OK_NAME_POS0';
                                    }}
                                    return 'NO_MATCH_NAME';
                                }}

                                // Original path: find by fieldset/radiogroup index
                                var groups = deepQueryAll(document, 'fieldset, [role="radiogroup"], spl-radio');
                                var group = groups[{q_deep_idx_radio}];
                                if (!group) return 'NOT_FOUND';

                                var radios = group.querySelectorAll('input[type="radio"]');
                                if (radios.length === 0 && group.shadowRoot) {{
                                    radios = group.shadowRoot.querySelectorAll('input[type="radio"]');
                                }}

                                for (var i = 0; i < radios.length; i++) {{
                                    var label = radios[i].closest('label');
                                    if (!label && radios[i].id) {{
                                        label = group.querySelector('label[for="' + radios[i].id + '"]');
                                    }}
                                    var labelText = label ? label.textContent.trim().toLowerCase() : '';
                                    var val = (radios[i].value || '').toLowerCase();

                                    if (labelText === answer || val === answer ||
                                        labelText.indexOf(answer) >= 0 || answer.indexOf(labelText) >= 0) {{
                                        radios[i].click();
                                        radios[i].dispatchEvent(new Event('change', {{bubbles: true, composed: true}}));
                                        return 'OK:' + (label ? label.textContent.trim() : val);
                                    }}
                                }}
                                // Fallback: "Yes" for affirmative answers
                                if (answer === 'yes' || answer === 'true') {{
                                    for (var j = 0; j < radios.length; j++) {{
                                        var rl = radios[j].closest('label');
                                        var rlt = rl ? rl.textContent.trim().toLowerCase() : radios[j].value.toLowerCase();
                                        if (rlt === 'yes' || rlt === 'true') {{
                                            radios[j].click();
                                            return 'OK_YES';
                                        }}
                                    }}
                                }}
                                if (answer === 'no' || answer === 'false') {{
                                    for (var j = 0; j < radios.length; j++) {{
                                        var rl = radios[j].closest('label');
                                        var rlt = rl ? rl.textContent.trim().toLowerCase() : radios[j].value.toLowerCase();
                                        if (rlt === 'no' || rlt === 'false') {{
                                            radios[j].click();
                                            return 'OK_NO';
                                        }}
                                    }}
                                }}
                                return 'NO_MATCH';
                            }})()
                        """)
                        logger.info(f"Radio fill: '{question_text[:30]}' -> {fill_result}")

                    elif field_type == "textarea":
                        # deepQueryAll js_finder for spl-textarea inside nested shadow DOM
                        ta_deep_idx = q.get('deep_idx', q_idx)
                        ta_actual_id = q.get('id', '')
                        ta_js_finder = (
                            _DEEP_QUERY_JS + f"""
                            (function() {{
                                var allTA = deepQueryAll(document, 'spl-textarea');
                                var host = null;
                                var qId = '{ta_actual_id}';
                                if (qId && qId.indexOf('spl-textarea-') !== 0) {{
                                    for (var fi = 0; fi < allTA.length; fi++) {{
                                        if (allTA[fi].id === qId) {{ host = allTA[fi]; break; }}
                                    }}
                                }}
                                if (!host) host = allTA[{ta_deep_idx}];
                                if (!host || !host.shadowRoot) return {{error: 'NO_HOST'}};
                                var inp = host.shadowRoot.querySelector('textarea');
                                if (!inp) return {{error: 'NO_INPUT'}};
                                inp.scrollIntoView({{behavior: 'instant', block: 'center'}});
                                inp.focus();
                                var rect = inp.getBoundingClientRect();
                                var focused = document.activeElement;
                                var sf = focused && focused.shadowRoot ? focused.shadowRoot.activeElement : null;
                                return {{x: rect.x + rect.width/2, y: rect.y + rect.height/2,
                                         w: rect.width, h: rect.height,
                                         activeTag: focused ? focused.tagName : 'none',
                                         activeId: focused ? focused.id : '',
                                         shadowActiveTag: sf ? sf.tagName : 'none',
                                         inputTag: 'TEXTAREA'}};
                            }})()"""
                        )
                        filled = await self._nd_cdp_type_into_shadow(
                            nd_page, 'spl-textarea', answer,
                            input_selector='textarea', js_finder=ta_js_finder
                        )
                        logger.info(f"Textarea fill: '{question_text[:30]}' -> {filled}")

                    else:  # text input (spl-input or spl-number-field)
                        # deepQueryAll js_finder for spl-input/spl-number-field inside nested shadow DOM
                        txt_deep_idx = q.get('deep_idx', q_idx)
                        txt_actual_id = q.get('id', '')
                        txt_tag_name = q.get('tagName', 'spl-input')  # may be spl-number-field
                        txt_selector = f'{txt_tag_name}, spl-input, spl-number-field'
                        # Escape label for JS string (strip * and lowercase)
                        txt_q_label = question_text.rstrip('*').strip()[:60].lower().replace("'", "\\'").replace('"', '\\"')
                        txt_js_finder = (
                            _DEEP_QUERY_JS + f"""
                            (function() {{
                                // Search document scope (not formRoot) so spl-number-field
                                // inside closed shadow DOMs are still reachable via deepQueryAll
                                var allInp = deepQueryAll(document, '{txt_selector}');
                                var host = null;
                                var qId = '{txt_actual_id}';
                                var qLabel = '{txt_q_label}';

                                // 1. ID match (skip auto-generated spl-input-XXXX ids)
                                if (qId && qId.indexOf('spl-input-') !== 0) {{
                                    for (var fi = 0; fi < allInp.length; fi++) {{
                                        if (allInp[fi].id === qId) {{ host = allInp[fi]; break; }}
                                    }}
                                }}

                                // 2. Label attribute match (works for spl-number-field which has label attr)
                                if (!host && qLabel.length > 2) {{
                                    var qLabelShort = qLabel.substring(0, 20);
                                    for (var fi = 0; fi < allInp.length; fi++) {{
                                        var lbl = (allInp[fi].getAttribute('label') ||
                                                   allInp[fi].getAttribute('aria-label') ||
                                                   allInp[fi].getAttribute('placeholder') || '').toLowerCase();
                                        if (lbl && (lbl.indexOf(qLabelShort) >= 0 ||
                                                    qLabel.indexOf(lbl.substring(0, 20)) >= 0)) {{
                                            host = allInp[fi]; break;
                                        }}
                                    }}
                                }}

                                // 3. Fallback: index within screening form, else document index
                                if (!host) {{
                                    var formInp = null;
                                    var _cands = ['sr-screening-questions-form',
                                                  'oc-screening-questions-form',
                                                  'oc-screening-questions'];
                                    for (var _ci = 0; _ci < _cands.length; _ci++) {{
                                        var _els = deepQueryAll(document, _cands[_ci]);
                                        for (var _ki = 0; _ki < _els.length; _ki++) {{
                                            if (_els[_ki].shadowRoot) {{
                                                formInp = deepQueryAll(_els[_ki].shadowRoot, '{txt_selector}');
                                                break;
                                            }}
                                        }}
                                        if (formInp) break;
                                    }}
                                    host = (formInp && formInp[{txt_deep_idx}]) || allInp[{txt_deep_idx}] || null;
                                }}

                                if (!host) return {{error: 'NO_HOST'}};

                                // Get inner input: try shadowRoot first, then light DOM (for closed/null shadow)
                                var inp = null;
                                if (host.shadowRoot) {{
                                    inp = host.shadowRoot.querySelector('input');
                                }} else {{
                                    // spl-number-field may use closed shadow DOM — try light DOM
                                    inp = host.querySelector('input') || host.querySelector('input[type="number"]');
                                }}
                                if (!inp) return {{error: 'NO_INPUT', hostTag: host.tagName, hasShadow: !!host.shadowRoot}};

                                inp.scrollIntoView({{behavior: 'instant', block: 'center'}});
                                inp.focus();
                                var rect = inp.getBoundingClientRect();
                                var focused = document.activeElement;
                                var sf = focused && focused.shadowRoot ? focused.shadowRoot.activeElement : null;
                                return {{x: rect.x + rect.width/2, y: rect.y + rect.height/2,
                                         w: rect.width, h: rect.height,
                                         activeTag: focused ? focused.tagName : 'none',
                                         activeId: focused ? focused.id : '',
                                         shadowActiveTag: sf ? sf.tagName : 'none',
                                         inputTag: 'INPUT'}};
                            }})()"""
                        )
                        filled = await self._nd_cdp_type_into_shadow(
                            nd_page, 'spl-input', answer,
                            input_selector='input', js_finder=txt_js_finder
                        )
                        logger.info(f"Text fill: '{question_text[:30]}' -> {filled}")

                    await asyncio.sleep(0.3)

                except Exception as e:
                    logger.warning(f"Error answering screening Q '{question_text[:40]}': {e}")

        except Exception as e:
            logger.warning(f"Error detecting screening questions: {e}", exc_info=True)

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

    async def _nd_fill_plain_form_inputs(self, nd_page, job_data: Dict[str, Any]) -> None:
        """Fill plain HTML <input>/<select> fields on Preliminary questions pages.

        SR multi-step forms sometimes have address fields (Street Address, City, State,
        Zip Code, Country) and 'How did you hear?' using plain HTML elements — NOT spl-*
        components — that _nd_handle_screening_questions misses entirely.
        """
        try:
            config = self.form_filler.config
            personal = config.get("personal_info", {})
            address   = personal.get("address", "")
            city      = personal.get("city", "")
            state     = personal.get("state", "")
            zip_code  = personal.get("zip_code", "")
            country   = personal.get("country", "United States")

            # Map label patterns → fill value
            label_map = {
                "street address": address,
                "address line 1": address,
                "address": address,
                "city": city,
                "state": state,
                "zip": zip_code,
                "postal": zip_code,
                "country": country,
                "how did you hear": "LinkedIn",
                "hear about us": "LinkedIn",
                "referred by": "",  # leave blank
            }

            result = await nd_page.evaluate("""
                (function() {
                    var filled = [];
                    var labelMap = %s;
                    // Find all visible text inputs / selects
                    var inputs = Array.from(document.querySelectorAll('input[type="text"],input[type=""],input:not([type]),select'));
                    inputs = inputs.filter(function(el) {
                        var s = window.getComputedStyle(el);
                        return s.display !== 'none' && s.visibility !== 'hidden' && el.offsetParent !== null;
                    });
                    inputs.forEach(function(inp) {
                        // Get label text
                        var labelText = '';
                        if (inp.id) {
                            var lbl = document.querySelector('label[for="' + inp.id + '"]');
                            if (lbl) labelText = lbl.textContent.trim().toLowerCase();
                        }
                        if (!labelText) {
                            var parent = inp.parentElement;
                            for (var i = 0; i < 4 && parent; i++) {
                                var lbl2 = parent.querySelector('label');
                                if (lbl2) { labelText = lbl2.textContent.trim().toLowerCase(); break; }
                                parent = parent.parentElement;
                            }
                        }
                        if (!labelText && inp.placeholder) labelText = inp.placeholder.toLowerCase();
                        if (!labelText) return;

                        // Find matching value
                        var fillVal = null;
                        for (var key in labelMap) {
                            if (labelText.indexOf(key) >= 0) {
                                fillVal = labelMap[key];
                                break;
                            }
                        }
                        if (fillVal === null || fillVal === undefined) return;
                        if (inp.value && inp.value.trim().length > 0) return;  // already filled

                        if (inp.tagName === 'SELECT') {
                            // Try to select matching option
                            var opts = inp.options;
                            for (var j = 0; j < opts.length; j++) {
                                if (opts[j].text.toLowerCase().indexOf(fillVal.toLowerCase()) >= 0 ||
                                    fillVal.toLowerCase().indexOf(opts[j].text.toLowerCase()) >= 0) {
                                    inp.selectedIndex = j;
                                    inp.dispatchEvent(new Event('change', {bubbles: true}));
                                    filled.push(labelText + '=' + opts[j].text);
                                    break;
                                }
                            }
                        } else {
                            inp.value = fillVal;
                            inp.dispatchEvent(new Event('input', {bubbles: true}));
                            inp.dispatchEvent(new Event('change', {bubbles: true}));
                            filled.push(labelText + '=' + fillVal);
                        }
                    });
                    return filled;
                })()
            """ % str(label_map).replace("'", '"').replace("True", "true").replace("False", "false").replace("None", "null"))

            if result and isinstance(result, list) and len(result) > 0:
                logger.info(f"Filled {len(result)} plain form inputs: {result}")
            else:
                logger.debug("No plain form inputs filled (none matched or all pre-filled)")

        except Exception as e:
            logger.debug(f"_nd_fill_plain_form_inputs failed: {e}")

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
                "thank you for applying",
                "thanks for applying",
                "thank you for your interest",
                "application has been submitted",
                "application received",
                "application submitted",
                "successfully applied",
                "application complete",
                "we have received your application",
                "we've received your application",
                "your application has been received",
            ]):
                logger.info(f"Multi-step form submitted successfully at step {step + 1}!")
                return True

            # Check URL for success
            curr_url = str(nd_page.url) if hasattr(nd_page, 'url') else ""
            if any(x in curr_url.lower() for x in ["confirmation", "success", "thank", "complete", "submitted"]):
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
                        # New page — fill any screening/custom questions visible on this step
                        try:
                            logger.info(f"Step {step + 1}: filling screening questions on new page")
                            await self._nd_handle_screening_questions(nd_page, job_data)
                        except Exception as sq_e:
                            logger.debug(f"Screening question fill on step {step + 1} failed: {sq_e}")
                        # Also fill plain HTML inputs (address fields on "Preliminary questions" pages)
                        try:
                            await self._nd_fill_plain_form_inputs(nd_page, job_data)
                        except Exception as pq_e:
                            logger.debug(f"Plain form input fill on step {step + 1} failed: {pq_e}")
                    prev_sections = sections

                    # If stuck on privacy/consent checkbox error, re-click checkboxes
                    has_privacy_error = any('you declare' in s.lower() or 'privacy notice' in s.lower() or 'consent' in s.lower() for s in sections if s.startswith('ERROR:'))
                    if has_privacy_error and stall_count <= 3:
                        logger.info(f"Privacy/consent checkbox error detected — re-clicking checkboxes (attempt {stall_count})")
                        try:
                            await nd_page.activate()
                            await asyncio.sleep(0.3)
                            import nodriver.cdp as cdp_priv
                            # Force-check ALL spl-checkbox elements via multiple strategies
                            force_result = await nd_page.evaluate("""
                                (function() {
                                    var fixed = [];
                                    var splChecks = document.querySelectorAll('spl-checkbox');
                                    for (var i = 0; i < splChecks.length; i++) {
                                        var sr = splChecks[i].shadowRoot;
                                        if (!sr) continue;
                                        var inner = sr.querySelector('input[type="checkbox"]');
                                        if (!inner) continue;
                                        // Force checked state
                                        if (!inner.checked) inner.checked = true;
                                        inner.setAttribute('aria-checked', 'true');
                                        // Dispatch all necessary events for Angular
                                        inner.dispatchEvent(new Event('change', {bubbles: true, composed: true}));
                                        inner.dispatchEvent(new Event('input', {bubbles: true, composed: true}));
                                        // Trigger zone.js spl-change on the host element
                                        splChecks[i].dispatchEvent(new CustomEvent('spl-change', {
                                            detail: {value: true, checked: true}, bubbles: true, composed: true
                                        }));
                                        // Also try direct zone.js handler invocation
                                        for (var key in splChecks[i]) {
                                            if (key.indexOf('__zone_symbol__spl-change') === 0) {
                                                var handlers = splChecks[i][key];
                                                if (Array.isArray(handlers)) {
                                                    for (var j = 0; j < handlers.length; j++) {
                                                        try {
                                                            var h = handlers[j].handler || handlers[j];
                                                            if (typeof h === 'function') {
                                                                h(new CustomEvent('spl-change', {
                                                                    detail: {value: true, checked: true}, bubbles: true
                                                                }));
                                                            }
                                                        } catch(e) {}
                                                    }
                                                }
                                            }
                                        }
                                        fixed.push(splChecks[i].id || 'spl-cb-' + i);
                                    }
                                    // Also check regular checkboxes
                                    var htmlChecks = document.querySelectorAll('input[type="checkbox"]');
                                    for (var k = 0; k < htmlChecks.length; k++) {
                                        if (!htmlChecks[k].checked && !htmlChecks[k].closest('spl-checkbox')) {
                                            htmlChecks[k].checked = true;
                                            htmlChecks[k].dispatchEvent(new Event('change', {bubbles: true}));
                                            fixed.push(htmlChecks[k].id || 'html-cb-' + k);
                                        }
                                    }
                                    return fixed.join(',') || 'NONE';
                                })()
                            """)
                            logger.info(f"Privacy checkbox force-check result: {force_result}")
                            # Also CDP-click each checkbox label for real mouse events
                            cb_coords_priv = await nd_page.evaluate("""
                                (function() {
                                    var results = [];
                                    var splChecks = document.querySelectorAll('spl-checkbox');
                                    for (var i = 0; i < splChecks.length; i++) {
                                        var sr = splChecks[i].shadowRoot;
                                        if (!sr) continue;
                                        splChecks[i].scrollIntoView({behavior: 'instant', block: 'center'});
                                        var label = sr.querySelector('label');
                                        var target = label || splChecks[i];
                                        var rect = target.getBoundingClientRect();
                                        if (rect.width > 0 && rect.height > 0) {
                                            results.push({x: rect.x + 12, y: rect.y + rect.height/2, id: splChecks[i].id || 'cb-'+i});
                                        }
                                    }
                                    return results;
                                })()
                            """)
                            if cb_coords_priv and isinstance(cb_coords_priv, list):
                                for cb in cb_coords_priv:
                                    if isinstance(cb, dict) and 'x' in cb:
                                        x, y = float(cb['x']), float(cb['y'])
                                        await nd_page.send(cdp_priv.input_.dispatch_mouse_event(
                                            type_="mouseMoved", x=x, y=y))
                                        await asyncio.sleep(0.05)
                                        await nd_page.send(cdp_priv.input_.dispatch_mouse_event(
                                            type_="mousePressed", x=x, y=y,
                                            button=cdp_priv.input_.MouseButton.LEFT, click_count=1))
                                        await asyncio.sleep(0.05)
                                        await nd_page.send(cdp_priv.input_.dispatch_mouse_event(
                                            type_="mouseReleased", x=x, y=y,
                                            button=cdp_priv.input_.MouseButton.LEFT, click_count=1))
                                        await asyncio.sleep(0.3)
                                        logger.info(f"CDP-clicked privacy checkbox '{cb.get('id')}' at ({x:.0f},{y:.0f})")
                            await asyncio.sleep(2)  # Wait for Angular to process
                        except Exception as priv_e:
                            logger.debug(f"Privacy checkbox re-click failed: {priv_e}")

                    # If stuck on "Fields marked with * are required", re-fill missing fields
                    has_required_error = any('fields marked with' in s.lower() for s in sections)
                    if has_required_error and stall_count <= 2:
                        logger.info(f"Re-filling missing required fields at step {step + 1}")
                        # Activate tab before CDP input events
                        try:
                            await nd_page.activate()
                            await asyncio.sleep(0.2)
                        except Exception:
                            pass
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
                                    click_r = _normalize_nd_result(click_r)
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

                        # Re-fill city LAST (after all other fields, so nothing clears it)
                        city = personal.get("city", "") or personal.get("location", "")
                        if city:
                            city_val = await self._nd_get_city_value(nd_page)
                            if not city_val or len(str(city_val).strip()) < 2:
                                logger.info("Re-filling city (LAST — after all other fields)")
                                import nodriver.cdp as cdp_mod
                                await self._nd_fill_city_autocomplete(nd_page, city, cdp_mod)

                        await asyncio.sleep(1)

                    # If stalled 3+ times on same page, give up on this step
                    if stall_count >= 3:
                        # Enable CDP network logging to see if Submit triggers any API calls
                        try:
                            import nodriver.cdp.network as cdp_net
                            await nd_page.send(cdp_net.enable())
                            # Click Submit one more time — try EVERY approach including
                            # directly invoking zone.js registered handlers
                            await nd_page.evaluate("""
                                (function() {
                                    var splBtns = document.querySelectorAll('spl-button');
                                    for (var i = 0; i < splBtns.length; i++) {
                                        var text = (splBtns[i].textContent || '').trim().toLowerCase();
                                        if (text.indexOf('submit') >= 0) {
                                            // Approach 1: host click
                                            splBtns[i].click();
                                            // Approach 2: inner button click
                                            if (splBtns[i].shadowRoot) {
                                                var inner = splBtns[i].shadowRoot.querySelector('button');
                                                if (inner) {
                                                    inner.click();
                                                    // Approach 3: directly invoke zone.js registered click handlers
                                                    var zkey = '__zone_symbol__clickfalse';
                                                    if (inner[zkey] && Array.isArray(inner[zkey])) {
                                                        for (var j = 0; j < inner[zkey].length; j++) {
                                                            try {
                                                                var handler = inner[zkey][j].handler || inner[zkey][j];
                                                                if (typeof handler === 'function') {
                                                                    handler(new MouseEvent('click', {
                                                                        bubbles: true, cancelable: true, composed: true, view: window
                                                                    }));
                                                                }
                                                            } catch(e) {}
                                                        }
                                                    }
                                                }
                                            }
                                            // Approach 4: dispatch composed MouseEvent
                                            splBtns[i].dispatchEvent(new MouseEvent('click', {
                                                bubbles: true, cancelable: true, composed: true, view: window
                                            }));
                                            return 'CLICKED:' + text;
                                        }
                                    }
                                    return 'NOT_FOUND';
                                })()
                            """)
                            await asyncio.sleep(5)
                            # Check if URL changed (successful submission redirects)
                            new_url = await nd_page.evaluate("window.location.href")
                            logger.info(f"URL after final Submit attempts: {new_url}")
                            # Check page content for thank you / success
                            page_text = await nd_page.evaluate("(document.body.innerText || '').substring(0, 500)")
                            logger.info(f"Page text after final Submit: {str(page_text)[:200]}")
                        except Exception as net_e:
                            logger.debug(f"Network logging failed: {net_e}")

                        # Try one more approach: use zone.js event listeners directly on the Submit button
                        try:
                            submit_probe = await nd_page.evaluate("""
                                (function() {
                                    var splBtns = document.querySelectorAll('spl-button');
                                    for (var i = 0; i < splBtns.length; i++) {
                                        var text = (splBtns[i].textContent || '').trim().toLowerCase();
                                        if (text.indexOf('submit') >= 0) {
                                            var zoneKeys = [];
                                            // Check for event listeners registered via zone.js
                                            var elKey = '__zone_symbol__eventListeners';
                                            if (splBtns[i][elKey]) {
                                                var listeners = splBtns[i][elKey];
                                                for (var evtName in listeners) {
                                                    zoneKeys.push(evtName + ':' + listeners[evtName].length);
                                                }
                                            }
                                            // Check __zone_symbol__ keys
                                            var allKeys = [];
                                            for (var key in splBtns[i]) {
                                                if (key.indexOf('__zone_symbol__') === 0 && key.indexOf('click') >= 0) {
                                                    allKeys.push(key + '=' + typeof splBtns[i][key]);
                                                }
                                            }
                                            // Check inner button too
                                            var innerKeys = [];
                                            if (splBtns[i].shadowRoot) {
                                                var inner = splBtns[i].shadowRoot.querySelector('button');
                                                if (inner) {
                                                    for (var k in inner) {
                                                        if (k.indexOf('__zone_symbol__') === 0 && k.indexOf('click') >= 0) {
                                                            innerKeys.push(k + '=' + typeof inner[k]);
                                                        }
                                                    }
                                                    if (inner[elKey]) {
                                                        for (var en in inner[elKey]) {
                                                            innerKeys.push('listener:' + en + ':' + inner[elKey][en].length);
                                                        }
                                                    }
                                                }
                                            }
                                            return JSON.stringify({
                                                text: text,
                                                hostZoneClick: allKeys,
                                                hostListeners: zoneKeys,
                                                innerKeys: innerKeys
                                            });
                                        }
                                    }
                                    return 'NO_SUBMIT';
                                })()
                            """)
                            logger.info(f"Submit button zone probe: {submit_probe}")
                        except Exception:
                            pass

                        # Before giving up, dump Angular form state for debugging
                        try:
                            full_dump = await nd_page.evaluate("""
                                (function() {
                                    var info = {};
                                    info.url = window.location.href;

                                    // Check Angular classes on form-related elements
                                    var ngClasses = [];
                                    var els = document.querySelectorAll('.ng-invalid, .ng-valid, .ng-dirty, .ng-pristine, .ng-touched, .ng-untouched');
                                    for (var i = 0; i < Math.min(els.length, 20); i++) {
                                        ngClasses.push({
                                            tag: els[i].tagName,
                                            id: els[i].id || '',
                                            classes: (els[i].className || '').toString().substring(0, 100)
                                        });
                                    }
                                    info.ngClasses = ngClasses;

                                    // Check spl-checkbox Angular state
                                    var cbState = [];
                                    var splChecks = document.querySelectorAll('spl-checkbox');
                                    for (var c = 0; c < splChecks.length; c++) {
                                        var host = splChecks[c];
                                        var hostClasses = (host.className || '').toString();
                                        var sr = host.shadowRoot;
                                        var inner = sr ? sr.querySelector('input[type="checkbox"]') : null;
                                        var ngCtx = host.__ngContext__;
                                        cbState.push({
                                            id: host.id,
                                            hostClasses: hostClasses.substring(0, 80),
                                            checked: inner ? inner.checked : 'no-inner',
                                            hasNgCtx: !!ngCtx,
                                            ngCtxType: ngCtx ? typeof ngCtx : 'none',
                                            ngCtxLen: Array.isArray(ngCtx) ? ngCtx.length : 'n/a'
                                        });
                                    }
                                    info.cbState = cbState;

                                    // Check spl-button state
                                    var btnState = [];
                                    var splBtns = document.querySelectorAll('spl-button');
                                    for (var b = 0; b < splBtns.length; b++) {
                                        var btn = splBtns[b];
                                        var btnClasses = (btn.className || '').toString();
                                        var innerBtn = btn.shadowRoot ? btn.shadowRoot.querySelector('button') : null;
                                        btnState.push({
                                            text: (btn.textContent || '').trim().substring(0, 20),
                                            hostClasses: btnClasses.substring(0, 80),
                                            disabled: btn.hasAttribute('disabled'),
                                            innerDisabled: innerBtn ? innerBtn.disabled : 'no-inner',
                                            innerType: innerBtn ? innerBtn.type : 'none'
                                        });
                                    }
                                    info.btnState = btnState;

                                    // Check body text
                                    info.bodyText = (document.body.innerText || '').substring(0, 300);

                                    return JSON.stringify(info);
                                })()
                            """)
                            logger.info(f"STALL DEBUG dump: {full_dump}")
                        except Exception as dump_e:
                            logger.debug(f"Dump failed: {dump_e}")
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
                            var bDisabled = splBtns[b].hasAttribute('disabled') ? ',DIS' : '';
                            // Check inner button disabled too
                            if (splBtns[b].shadowRoot) {
                                var innerBtn = splBtns[b].shadowRoot.querySelector('button');
                                if (innerBtn && innerBtn.disabled) bDisabled = ',DIS';
                            }
                            info.buttons.push(bText + '(' + bVis + bDisabled + ')');
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

            # Check ALL consent/privacy/agree checkboxes
            # Strategy: probe spl-checkbox shadow DOM structure, then use the most Angular-compatible click
            try:
                import nodriver.cdp as cdp_cb
                # First, probe the shadow DOM to understand checkbox structure
                cb_probe = await nd_page.evaluate("""
                    (function() {
                        var splChecks = document.querySelectorAll('spl-checkbox');
                        if (splChecks.length === 0) return 'NO_CHECKBOXES';
                        var probe = [];
                        var first = splChecks[0];
                        var sr = first.shadowRoot;
                        if (!sr) return 'NO_SHADOW';
                        // List all elements and their tags inside shadow DOM
                        var children = sr.querySelectorAll('*');
                        for (var i = 0; i < children.length; i++) {
                            var el = children[i];
                            var rect = el.getBoundingClientRect();
                            probe.push({
                                tag: el.tagName.toLowerCase(),
                                id: el.id || '',
                                className: (el.className || '').toString().substring(0, 50),
                                type: el.type || '',
                                w: Math.round(rect.width),
                                h: Math.round(rect.height),
                                x: Math.round(rect.x),
                                y: Math.round(rect.y)
                            });
                        }
                        // Also check zone.js symbols on host
                        var zoneKeys = [];
                        for (var key in first) {
                            if (key.indexOf('__zone_symbol__') === 0 || key.indexOf('__ng') === 0) {
                                zoneKeys.push(key);
                            }
                        }
                        return JSON.stringify({shadow: probe, zoneKeys: zoneKeys, hostTag: first.tagName});
                    })()
                """)
                logger.info(f"Checkbox shadow DOM probe: {cb_probe}")

                # Get coordinates — use the <label> element inside shadow DOM
                # which is the proper clickable target that toggles the checkbox
                cb_coords = await nd_page.evaluate("""
                    (function() {
                        var results = [];
                        var splChecks = document.querySelectorAll('spl-checkbox');
                        for (var i = 0; i < splChecks.length; i++) {
                            var sr = splChecks[i].shadowRoot;
                            if (!sr) continue;
                            var inner = sr.querySelector('input[type="checkbox"]');
                            if (inner && !inner.checked) {
                                splChecks[i].scrollIntoView({behavior: 'instant', block: 'center'});
                                // Try label first (proper way to toggle checkbox)
                                var label = sr.querySelector('label');
                                var hostRect = splChecks[i].getBoundingClientRect();
                                if (label) {
                                    var labelRect = label.getBoundingClientRect();
                                    // Click at the LEFT part of the label (where checkbox icon is)
                                    results.push({
                                        x: labelRect.x + 10,
                                        y: labelRect.y + labelRect.height/2,
                                        id: splChecks[i].id || ('spl-cb-' + i),
                                        type: 'spl-label',
                                        labelW: Math.round(labelRect.width),
                                        labelH: Math.round(labelRect.height)
                                    });
                                } else {
                                    results.push({
                                        x: hostRect.x + 10,
                                        y: hostRect.y + hostRect.height/2,
                                        id: splChecks[i].id || ('spl-cb-' + i),
                                        type: 'spl-host'
                                    });
                                }
                            }
                        }
                        // Also check regular HTML checkboxes
                        var htmlChecks = document.querySelectorAll('input[type="checkbox"]:not([hidden])');
                        for (var j = 0; j < htmlChecks.length; j++) {
                            if (!htmlChecks[j].checked && !htmlChecks[j].closest('spl-checkbox')) {
                                var r = htmlChecks[j].getBoundingClientRect();
                                if (r.width > 0 && r.height > 0) {
                                    results.push({x: r.x + r.width/2, y: r.y + r.height/2, id: htmlChecks[j].id || 'html-cb-'+j, type: 'html'});
                                }
                            }
                        }
                        return results;
                    })()
                """)
                # Deep unwrap nodriver results
                def _unwrap_nd_list(raw):
                    if not raw:
                        return []
                    result = []
                    items = raw if isinstance(raw, list) else [raw]
                    for item in items:
                        if isinstance(item, dict) and 'type' in item and item.get('type') == 'object' and 'value' in item:
                            pairs = item['value']
                            obj = {}
                            if isinstance(pairs, list):
                                for pair in pairs:
                                    if isinstance(pair, list) and len(pair) == 2:
                                        k, v = pair
                                        obj[k] = v['value'] if isinstance(v, dict) and 'value' in v else v
                            if obj:
                                result.append(obj)
                        elif isinstance(item, dict) and 'x' in item:
                            result.append(item)
                    return result

                cb_list = _unwrap_nd_list(cb_coords)
                logger.info(f"Checkboxes to click: {cb_list}")

                # Strategy: Click "Select all" first if present (most reliable),
                # then click individual required ones if "Select all" didn't work
                select_all_cb = None
                individual_cbs = []
                for cb in cb_list:
                    if not isinstance(cb, dict) or 'x' not in cb:
                        continue
                    if cb.get('id', '').lower() in ('consent-select-all', 'select-all', 'selectall'):
                        select_all_cb = cb
                    else:
                        individual_cbs.append(cb)

                # Click checkboxes with full CDP mouse event sequence
                async def _cdp_click_checkbox(x, y, cb_id):
                    # Full mouse event sequence: move → down → up (mimics real user)
                    await nd_page.send(cdp_cb.input_.dispatch_mouse_event(
                        type_="mouseMoved", x=x, y=y))
                    await asyncio.sleep(0.05)
                    await nd_page.send(cdp_cb.input_.dispatch_mouse_event(
                        type_="mousePressed", x=x, y=y,
                        button=cdp_cb.input_.MouseButton.LEFT, click_count=1))
                    await asyncio.sleep(0.05)
                    await nd_page.send(cdp_cb.input_.dispatch_mouse_event(
                        type_="mouseReleased", x=x, y=y,
                        button=cdp_cb.input_.MouseButton.LEFT, click_count=1))
                    await asyncio.sleep(0.5)
                    logger.info(f"CDP-clicked checkbox '{cb_id}' at ({x:.0f},{y:.0f})")

                # Click select-all first, then individuals
                if select_all_cb:
                    await _cdp_click_checkbox(
                        float(select_all_cb['x']), float(select_all_cb['y']),
                        select_all_cb.get('id', 'select-all'))
                    await asyncio.sleep(0.5)

                for cb in individual_cbs:
                    await _cdp_click_checkbox(
                        float(cb['x']), float(cb['y']),
                        cb.get('id', 'unknown'))

                # Verify checkboxes are checked
                await asyncio.sleep(0.5)
                verify_cb = await nd_page.evaluate("""
                    (function() {
                        var results = [];
                        var splChecks = document.querySelectorAll('spl-checkbox');
                        for (var i = 0; i < splChecks.length; i++) {
                            var sr = splChecks[i].shadowRoot;
                            if (!sr) continue;
                            var inner = sr.querySelector('input[type="checkbox"]');
                            results.push((splChecks[i].id || 'spl-cb-' + i) + ':' + (inner ? (inner.checked ? 'CHECKED' : 'UNCHECKED') : 'no-inner'));
                        }
                        return results.join(', ');
                    })()
                """)
                logger.info(f"Checkbox status after CDP click: {verify_cb}")

                # If still unchecked, click the HOST element itself
                # Angular's spl-checkbox has a click handler on the HOST (zone.js patches onclick)
                # HOST.click() → zone.js → Angular change detection → checkbox toggled properly
                still_unchecked = await nd_page.evaluate("""
                    (function() {
                        var fixed = [];
                        var splChecks = document.querySelectorAll('spl-checkbox');
                        for (var i = 0; i < splChecks.length; i++) {
                            var sr = splChecks[i].shadowRoot;
                            if (!sr) continue;
                            var inner = sr.querySelector('input[type="checkbox"]');
                            if (inner && !inner.checked) {
                                // Strategy 1: Click the HOST element (Angular listens here)
                                splChecks[i].click();
                                fixed.push(splChecks[i].id || 'spl-cb-' + i);
                            }
                        }
                        return fixed.length > 0 ? fixed.join(',') : 'ALL_CHECKED';
                    })()
                """)
                if still_unchecked != 'ALL_CHECKED':
                    logger.info(f"Host-clicked checkboxes: {still_unchecked}")
                    await asyncio.sleep(0.5)
                    # Verify after host click
                    verify2 = await nd_page.evaluate("""
                        (function() {
                            var results = [];
                            var splChecks = document.querySelectorAll('spl-checkbox');
                            for (var i = 0; i < splChecks.length; i++) {
                                var sr = splChecks[i].shadowRoot;
                                if (!sr) continue;
                                var inner = sr.querySelector('input[type="checkbox"]');
                                results.push((splChecks[i].id || 'spl-cb-' + i) + ':' + (inner ? (inner.checked ? 'CHECKED' : 'UNCHECKED') : 'no-inner'));
                            }
                            return results.join(', ');
                        })()
                    """)
                    logger.info(f"After host.click(): {verify2}")
                    # If STILL unchecked, try the wrapper div click + zone.js spl-change
                    still_unchecked2 = await nd_page.evaluate("""
                        (function() {
                            var remaining = [];
                            var splChecks = document.querySelectorAll('spl-checkbox');
                            for (var i = 0; i < splChecks.length; i++) {
                                var sr = splChecks[i].shadowRoot;
                                if (!sr) continue;
                                var inner = sr.querySelector('input[type="checkbox"]');
                                if (inner && !inner.checked) {
                                    // Try wrapper div click
                                    var wrapper = sr.querySelector('.c-spl-checkbox-wrapper, .c-spl-checkbox');
                                    if (wrapper) wrapper.click();
                                    // Force DOM state
                                    if (!inner.checked) inner.checked = true;
                                    // Trigger zone.js spl-change on host
                                    var key = '__zone_symbol__spl-changefalse';
                                    if (splChecks[i][key] && Array.isArray(splChecks[i][key])) {
                                        for (var j = 0; j < splChecks[i][key].length; j++) {
                                            try {
                                                var h = splChecks[i][key][j].handler || splChecks[i][key][j];
                                                if (typeof h === 'function') {
                                                    h(new CustomEvent('spl-change', {
                                                        detail: {value: true, checked: true}, bubbles: true
                                                    }));
                                                }
                                            } catch(e) {}
                                        }
                                    }
                                    remaining.push(splChecks[i].id || 'spl-cb-' + i);
                                }
                            }
                            return remaining.length > 0 ? remaining.join(',') : 'ALL_CHECKED';
                        })()
                    """)
                    if still_unchecked2 != 'ALL_CHECKED':
                        logger.warning(f"Force-checked remaining: {still_unchecked2}")

                await asyncio.sleep(1)
            except Exception as cb_e:
                logger.debug(f"Checkbox check failed: {cb_e}")

            # Handle screening questions on the current step (e.g., "Preliminary questions")
            # These can appear on any step, not just the first page
            try:
                logger.info(f"Step {step + 1}: scanning for screening questions...")
                await self._nd_handle_screening_questions(nd_page, job_data)
            except Exception as sq_e:
                logger.warning(f"Screening questions on step {step + 1} FAILED: {sq_e}")
            # Fill plain HTML inputs (address, etc.) which spl-* scanner misses
            try:
                await self._nd_fill_plain_form_inputs(nd_page, job_data)
            except Exception as pq_e:
                logger.debug(f"Plain form input fill (every-step) failed: {pq_e}")

            # Handle spl-autocomplete fields on page 2+ (e.g., disability self-ID)
            # On page 1, spl-autocomplete is the city field. On page 2+, it's screening questions.
            try:
                ac_count = await nd_page.evaluate(_DEEP_QUERY_JS + """
                    (function() {
                        var acs = deepQueryAll(document, 'spl-autocomplete');
                        var total = acs.length;
                        var unfilled = [];
                        for (var i = 0; i < acs.length; i++) {
                            var val = '';
                            if (acs[i].shadowRoot) {
                                var inp = acs[i].shadowRoot.querySelector('input');
                                if (!inp) {
                                    // Nested shadow: spl-autocomplete > spl-input > input
                                    var nested = acs[i].shadowRoot.querySelector('spl-input');
                                    if (nested && nested.shadowRoot) inp = nested.shadowRoot.querySelector('input');
                                }
                                val = inp ? inp.value : '';
                            }
                            if (!val || !val.trim()) {
                                var label = acs[i].getAttribute('label') || '';
                                // Get parent text for label
                                if (!label) {
                                    var prev = acs[i].previousElementSibling;
                                    if (prev) label = (prev.textContent || '').trim().substring(0, 100);
                                }
                                if (!label) {
                                    var parent = acs[i].parentElement;
                                    if (parent) {
                                        var lbl = parent.querySelector('label, .label, p');
                                        if (lbl && lbl !== acs[i]) label = lbl.textContent.trim().substring(0, 100);
                                    }
                                }
                                unfilled.push({idx: i, label: label, id: acs[i].id || ''});
                            }
                        }
                        return JSON.stringify({total: total, unfilled: unfilled});
                    })()
                """)
                import json as _ac_json
                ac_data = _ac_json.loads(ac_count) if isinstance(ac_count, str) else {}
                ac_total = ac_data.get('total', 0) if isinstance(ac_data, dict) else 0
                ac_fields = ac_data.get('unfilled', []) if isinstance(ac_data, dict) else []
                logger.info(f"Step {step + 1} spl-autocomplete scan: {ac_total} total, {len(ac_fields)} unfilled")
                # Broad page scan for debugging: check iframes, ng-invalid, all custom elements
                if step >= 1:
                    page_debug = await nd_page.evaluate("""
                        (function() {
                            var info = {};
                            // 1. Check for iframes
                            info.iframes = document.querySelectorAll('iframe').length;
                            // 2. Check for ng-invalid elements (form validation failures)
                            var invalid = document.querySelectorAll('.ng-invalid, [class*="ng-invalid"]');
                            info.ngInvalid = [];
                            for (var i = 0; i < invalid.length; i++) {
                                info.ngInvalid.push({
                                    tag: invalid[i].tagName, id: invalid[i].id || '',
                                    cls: invalid[i].className.toString().substring(0, 60)
                                });
                            }
                            // 3. ALL custom elements (non-standard HTML tags)
                            var customs = [];
                            var allEls = document.querySelectorAll('*');
                            for (var j = 0; j < allEls.length; j++) {
                                var tag = allEls[j].tagName.toLowerCase();
                                if (tag.indexOf('-') > 0 && !tag.startsWith('spl-checkbox') && !tag.startsWith('spl-button')) {
                                    var r = allEls[j].getBoundingClientRect();
                                    if (r.width > 50 && r.height > 10) {
                                        customs.push(tag + '#' + (allEls[j].id || '') + '(' + Math.round(r.width) + 'x' + Math.round(r.height) + ')');
                                    }
                                }
                            }
                            info.customElements = customs.slice(0, 30);
                            // 4. ALL visible inputs/selects (even in shadow DOM we've already checked)
                            var allInputs = [];
                            function scanInputs(root, prefix) {
                                var inputs = root.querySelectorAll('input:not([type="hidden"]), select, textarea');
                                for (var k = 0; k < inputs.length; k++) {
                                    var el = inputs[k];
                                    var r = el.getBoundingClientRect();
                                    if (r.width > 20 && r.height > 5) {
                                        allInputs.push(prefix + el.tagName + '#' + (el.id || '') + '[' + (el.type || '') + ']=' +
                                            (el.value || '').substring(0, 20) + '(' + Math.round(r.x) + ',' + Math.round(r.y) + ')');
                                    }
                                }
                            }
                            scanInputs(document, '');
                            // Also scan shadow roots of ALL custom elements
                            for (var m = 0; m < allEls.length; m++) {
                                if (allEls[m].shadowRoot) {
                                    scanInputs(allEls[m].shadowRoot, allEls[m].tagName + '>');
                                    // One more level deep
                                    var inner = allEls[m].shadowRoot.querySelectorAll('*');
                                    for (var n = 0; n < inner.length; n++) {
                                        if (inner[n].shadowRoot) scanInputs(inner[n].shadowRoot, allEls[m].tagName + '>' + inner[n].tagName + '>');
                                    }
                                }
                            }
                            info.allVisibleInputs = allInputs.slice(0, 30);
                            return JSON.stringify(info);
                        })()
                    """)
                    logger.info(f"Step {step + 1} page debug: {page_debug}")
                if ac_fields:
                    logger.info(f"Step {step + 1} unfilled spl-autocomplete fields: {ac_fields}")
                    import nodriver.cdp as cdp_ac
                    for acf in ac_fields:
                        ac_label = acf.get('label', '').lower()
                        ac_idx = acf.get('idx', 0)
                        # Skip city field (handled separately)
                        if 'city' in ac_label:
                            continue
                        # Determine answer based on label
                        if 'disab' in ac_label or 'voluntary' in ac_label or 'self-identification' in ac_label:
                            answer = "I do not wish to answer"
                        elif 'gender' in ac_label:
                            answer = "Prefer not to say"
                        elif 'veteran' in ac_label:
                            answer = "I am not a protected veteran"
                        elif 'race' in ac_label or 'ethnic' in ac_label:
                            answer = "Decline to self identify"
                        else:
                            answer = "I do not wish to answer"
                        logger.info(f"Filling spl-autocomplete #{ac_idx} '{ac_label[:40]}' with '{answer}'")
                        # Get input coords from shadow DOM
                        ac_coords = await nd_page.evaluate(f"""
                            (function() {{
                                var acs = document.querySelectorAll('spl-autocomplete');
                                var ac = acs[{ac_idx}];
                                if (!ac || !ac.shadowRoot) return null;
                                ac.scrollIntoView({{behavior: 'instant', block: 'center'}});
                                var inp = ac.shadowRoot.querySelector('input');
                                if (!inp) {{
                                    var nested = ac.shadowRoot.querySelector('spl-input');
                                    if (nested && nested.shadowRoot) inp = nested.shadowRoot.querySelector('input');
                                }}
                                if (!inp) return null;
                                inp.value = '';
                                inp.focus();
                                var r = inp.getBoundingClientRect();
                                return {{x: r.x + r.width/2, y: r.y + r.height/2, w: r.width}};
                            }})()
                        """)
                        if ac_coords and isinstance(ac_coords, dict) and ac_coords.get('w', 0) > 0:
                            ax, ay = float(ac_coords['x']), float(ac_coords['y'])
                            # CDP click to focus
                            await nd_page.send(cdp_ac.input_.dispatch_mouse_event(
                                type_="mousePressed", x=ax, y=ay,
                                button=cdp_ac.input_.MouseButton.LEFT, click_count=1))
                            await nd_page.send(cdp_ac.input_.dispatch_mouse_event(
                                type_="mouseReleased", x=ax, y=ay,
                                button=cdp_ac.input_.MouseButton.LEFT, click_count=1))
                            await asyncio.sleep(0.5)
                            # Type answer char by char
                            for char in answer:
                                await nd_page.send(cdp_ac.input_.dispatch_key_event(type_="keyDown", key=char))
                                await nd_page.send(cdp_ac.input_.dispatch_key_event(type_="char", text=char, key=char))
                                await nd_page.send(cdp_ac.input_.dispatch_key_event(type_="keyUp", key=char))
                                await asyncio.sleep(0.04)
                            await asyncio.sleep(2)
                            # Click first matching suggestion
                            suggestion_result = await nd_page.evaluate(f"""
                                (function() {{
                                    var ac = document.querySelectorAll('spl-autocomplete')[{ac_idx}];
                                    if (!ac || !ac.shadowRoot) return 'NO_AC';
                                    // Search for suggestions in shadow DOM at all levels
                                    function findSuggestions(root, depth) {{
                                        if (depth > 5) return [];
                                        var results = [];
                                        var items = root.querySelectorAll('li, [role="option"], [class*="option"], [class*="suggestion"], [class*="item"]');
                                        for (var i = 0; i < items.length; i++) {{
                                            var r = items[i].getBoundingClientRect();
                                            var t = (items[i].textContent || '').trim();
                                            if (r.width > 0 && r.height > 0 && r.height < 200 && t.length > 2) {{
                                                results.push({{el: items[i], text: t, x: r.x + r.width/2, y: r.y + r.height/2}});
                                            }}
                                        }}
                                        var all = root.querySelectorAll('*');
                                        for (var j = 0; j < all.length; j++) {{
                                            if (all[j].shadowRoot) results = results.concat(findSuggestions(all[j].shadowRoot, depth+1));
                                        }}
                                        return results;
                                    }}
                                    var suggestions = findSuggestions(ac.shadowRoot, 0);
                                    // Also check document level (some dropdowns render outside shadow DOM)
                                    var globalItems = document.querySelectorAll('[role="listbox"] li, [role="listbox"] [role="option"], [class*="cdk-overlay"] li');
                                    for (var k = 0; k < globalItems.length; k++) {{
                                        var r = globalItems[k].getBoundingClientRect();
                                        var t = (globalItems[k].textContent || '').trim();
                                        if (r.width > 0 && r.height > 0 && r.height < 200 && t.length > 2) {{
                                            suggestions.push({{el: globalItems[k], text: t, x: r.x + r.width/2, y: r.y + r.height/2}});
                                        }}
                                    }}
                                    if (suggestions.length === 0) return 'NO_SUGGESTIONS';
                                    // Find best match: prefer "do not wish", "prefer not", "decline"
                                    var answer = '{answer}'.toLowerCase();
                                    for (var s = 0; s < suggestions.length; s++) {{
                                        var st = suggestions[s].text.toLowerCase();
                                        if (st.indexOf('do not wish') >= 0 || st.indexOf('prefer not') >= 0 ||
                                            st.indexOf('decline') >= 0 || st.indexOf(answer) >= 0 || answer.indexOf(st) >= 0) {{
                                            suggestions[s].el.click();
                                            return 'CLICKED:' + suggestions[s].text.substring(0, 60);
                                        }}
                                    }}
                                    // Click first suggestion as fallback
                                    suggestions[0].el.click();
                                    return 'CLICKED_FIRST:' + suggestions[0].text.substring(0, 60);
                                }})()
                            """)
                            logger.info(f"Autocomplete suggestion result: {suggestion_result}")
                            # If JS click didn't work, try CDP click at suggestion coordinates
                            if suggestion_result and 'NO_SUGGESTIONS' not in str(suggestion_result):
                                await asyncio.sleep(0.5)
                            else:
                                # Try typing just a shorter query to trigger suggestions
                                logger.info("No suggestions found, trying shorter query...")
                                # Clear and type just "do not"
                                await nd_page.send(cdp_ac.input_.dispatch_key_event(
                                    type_="keyDown", key="a",
                                    windows_virtual_key_code=65, native_virtual_key_code=65,
                                    modifiers=2))  # Cmd+A
                                await nd_page.send(cdp_ac.input_.dispatch_key_event(type_="keyUp", key="a"))
                                await nd_page.send(cdp_ac.input_.dispatch_key_event(
                                    type_="keyDown", key="Backspace"))
                                await nd_page.send(cdp_ac.input_.dispatch_key_event(type_="keyUp", key="Backspace"))
                                await asyncio.sleep(0.3)
                                for char in "do not":
                                    await nd_page.send(cdp_ac.input_.dispatch_key_event(type_="keyDown", key=char))
                                    await nd_page.send(cdp_ac.input_.dispatch_key_event(type_="char", text=char, key=char))
                                    await nd_page.send(cdp_ac.input_.dispatch_key_event(type_="keyUp", key=char))
                                    await asyncio.sleep(0.04)
                                await asyncio.sleep(2)
                        else:
                            logger.info(f"Could not get coords for spl-autocomplete #{ac_idx}")
            except Exception as ac_e:
                logger.warning(f"spl-autocomplete scan failed: {ac_e}", exc_info=True)

            # Handle special fields that _nd_handle_screening_questions might miss:
            # - oc-autocomplete-question (disability self-ID, etc.)
            # - oc-select-question (custom SR question wrappers)
            # These use different components than spl-select/spl-input
            try:
                special_result = await nd_page.evaluate("""
                    (function() {
                        var filled = [];

                        // Find ALL unfilled form elements the screening scanner missed
                        // Look for oc-* question wrappers, custom autocomplete fields, etc.
                        var allInputs = document.querySelectorAll(
                            'input:not([type="hidden"]):not([type="checkbox"]):not([type="radio"]):not([type="file"]),' +
                            'select, textarea'
                        );
                        for (var i = 0; i < allInputs.length; i++) {
                            var el = allInputs[i];
                            // Skip if inside spl-* components (already handled)
                            if (el.closest('spl-input') || el.closest('spl-select') || el.closest('spl-textarea') ||
                                el.closest('spl-phone-field') || el.closest('spl-autocomplete') || el.closest('spl-dropzone')) continue;
                            // Skip if already filled
                            if (el.value && el.value.trim().length > 0) continue;
                            // Skip if hidden
                            var rect = el.getBoundingClientRect();
                            if (rect.width === 0 || rect.height === 0) continue;

                            // Find label
                            var label = '';
                            var lbl = el.closest('label') || document.querySelector('label[for="' + el.id + '"]');
                            if (lbl) label = lbl.textContent.trim();
                            if (!label) {
                                // Check parent oc-* components
                                var ocParent = el.closest('oc-autocomplete-question, oc-select-question, oc-input-question, oc-question, [class*="question"]');
                                if (ocParent) {
                                    var ocLabel = ocParent.querySelector('label, legend, .label, [class*="label"], p');
                                    if (ocLabel) label = ocLabel.textContent.trim();
                                }
                            }
                            if (!label) {
                                // Check previous sibling or parent text
                                var prev = el.previousElementSibling;
                                if (prev) label = (prev.textContent || '').trim().substring(0, 200);
                                if (!label && el.parentElement) {
                                    label = (el.parentElement.textContent || '').trim().substring(0, 200);
                                }
                            }
                            if (!label) label = el.getAttribute('placeholder') || el.getAttribute('aria-label') || '';

                            filled.push({
                                tag: el.tagName, id: el.id || '', name: el.name || '',
                                type: el.type || '', label: label.substring(0, 100),
                                x: Math.round(rect.x + rect.width/2),
                                y: Math.round(rect.y + rect.height/2)
                            });
                        }

                        // Also check shadow DOM of any non-spl custom elements
                        var customEls = document.querySelectorAll('oc-autocomplete-question, oc-select-question');
                        for (var j = 0; j < customEls.length; j++) {
                            var ce = customEls[j];
                            var ceLabel = '';
                            var ceLbl = ce.querySelector('label, .label');
                            if (ceLbl) ceLabel = ceLbl.textContent.trim();
                            // Check shadow root
                            if (ce.shadowRoot) {
                                var ceInputs = ce.shadowRoot.querySelectorAll('input, select');
                                for (var k = 0; k < ceInputs.length; k++) {
                                    if (!ceInputs[k].value || !ceInputs[k].value.trim()) {
                                        var cr = ceInputs[k].getBoundingClientRect();
                                        if (cr.width > 0) {
                                            filled.push({
                                                tag: 'SHADOW:' + ceInputs[k].tagName, id: ceInputs[k].id || '',
                                                parent: ce.tagName, label: ceLabel.substring(0, 100),
                                                x: Math.round(cr.x + cr.width/2),
                                                y: Math.round(cr.y + cr.height/2)
                                            });
                                        }
                                    }
                                }
                            }
                        }
                        return filled.length > 0 ? JSON.stringify(filled) : 'NONE';
                    })()
                """)
                if special_result and special_result != 'NONE':
                    import json as _spec_json
                    special_fields = _spec_json.loads(special_result) if isinstance(special_result, str) else []
                    logger.info(f"Step {step + 1} special unfilled fields: {special_fields}")

                    # Try to fill disability/EEO fields automatically
                    for sf in special_fields:
                        sf_label = sf.get('label', '').lower()
                        if 'disab' in sf_label or 'self-identification' in sf_label or 'voluntary' in sf_label:
                            logger.info(f"Found disability field: {sf}")
                            # Type "I do not wish to answer" into the field
                            sx, sy = float(sf.get('x', 0)), float(sf.get('y', 0))
                            if sx > 0 and sy > 0:
                                import nodriver.cdp as cdp_spec
                                # Click to focus
                                await nd_page.send(cdp_spec.input_.dispatch_mouse_event(
                                    type_="mousePressed", x=sx, y=sy,
                                    button=cdp_spec.input_.MouseButton.LEFT, click_count=1))
                                await nd_page.send(cdp_spec.input_.dispatch_mouse_event(
                                    type_="mouseReleased", x=sx, y=sy,
                                    button=cdp_spec.input_.MouseButton.LEFT, click_count=1))
                                await asyncio.sleep(0.5)
                                # Type text
                                answer = "I do not wish to answer"
                                for char in answer:
                                    await nd_page.send(cdp_spec.input_.dispatch_key_event(type_="keyDown", key=char))
                                    await nd_page.send(cdp_spec.input_.dispatch_key_event(type_="char", text=char, key=char))
                                    await nd_page.send(cdp_spec.input_.dispatch_key_event(type_="keyUp", key=char))
                                    await asyncio.sleep(0.03)
                                await asyncio.sleep(1.5)
                                # Try clicking first suggestion
                                suggestion_clicked = await nd_page.evaluate("""
                                    (function() {
                                        var items = document.querySelectorAll(
                                            '[role="option"], [role="listbox"] li, li[class*="option"], ' +
                                            '[class*="suggestion"], [class*="item"]'
                                        );
                                        for (var i = 0; i < items.length; i++) {
                                            var r = items[i].getBoundingClientRect();
                                            var t = (items[i].textContent || '').trim().toLowerCase();
                                            if (r.width > 0 && r.height > 0 && r.height < 100 &&
                                                (t.indexOf('do not wish') >= 0 || t.indexOf('prefer not') >= 0 ||
                                                 t.indexOf('decline') >= 0 || t.indexOf('not to answer') >= 0)) {
                                                items[i].click();
                                                return 'CLICKED:' + t.substring(0, 50);
                                            }
                                        }
                                        // If no match, click first visible item
                                        for (var j = 0; j < items.length; j++) {
                                            var r2 = items[j].getBoundingClientRect();
                                            if (r2.width > 0 && r2.height > 0 && r2.height < 100) {
                                                items[j].click();
                                                return 'CLICKED_FIRST:' + (items[j].textContent || '').trim().substring(0, 50);
                                            }
                                        }
                                        return 'NO_SUGGESTION';
                                    })()
                                """)
                                logger.info(f"Disability field suggestion: {suggestion_clicked}")
                                await asyncio.sleep(0.5)

                        elif 'gender' in sf_label or 'race' in sf_label or 'ethnic' in sf_label or 'veteran' in sf_label:
                            logger.info(f"Found EEO field: {sf}")
                            # Similar autocomplete fill for EEO fields
                            sx, sy = float(sf.get('x', 0)), float(sf.get('y', 0))
                            if sx > 0 and sy > 0:
                                import nodriver.cdp as cdp_spec
                                await nd_page.send(cdp_spec.input_.dispatch_mouse_event(
                                    type_="mousePressed", x=sx, y=sy,
                                    button=cdp_spec.input_.MouseButton.LEFT, click_count=1))
                                await nd_page.send(cdp_spec.input_.dispatch_mouse_event(
                                    type_="mouseReleased", x=sx, y=sy,
                                    button=cdp_spec.input_.MouseButton.LEFT, click_count=1))
                                await asyncio.sleep(0.5)
                                answer = "Prefer not to say" if 'gender' in sf_label else "Decline"
                                for char in answer:
                                    await nd_page.send(cdp_spec.input_.dispatch_key_event(type_="keyDown", key=char))
                                    await nd_page.send(cdp_spec.input_.dispatch_key_event(type_="char", text=char, key=char))
                                    await nd_page.send(cdp_spec.input_.dispatch_key_event(type_="keyUp", key=char))
                                    await asyncio.sleep(0.03)
                                await asyncio.sleep(1.5)
                                # Click first matching suggestion
                                await nd_page.evaluate("""
                                    (function() {
                                        var items = document.querySelectorAll(
                                            '[role="option"], [role="listbox"] li, li[class*="option"]'
                                        );
                                        for (var i = 0; i < items.length; i++) {
                                            var r = items[i].getBoundingClientRect();
                                            if (r.width > 0 && r.height > 0 && r.height < 100) {
                                                items[i].click();
                                                return;
                                            }
                                        }
                                    })()
                                """)
                                await asyncio.sleep(0.5)
            except Exception as spec_e:
                logger.warning(f"Special field scan failed: {spec_e}", exc_info=True)

            # ====================================================================
            # EEOC / Demographics: fill radio buttons and search-autocomplete fields
            # These appear on the screening/consent page of many SR forms.
            # Radio groups: disability, veteran status
            # Search fields: gender, race/ethnicity
            # ====================================================================
            try:
                eeoc_result = await nd_page.evaluate("""
                    (function() {
                        var results = [];
                        var bodyText = (document.body.innerText || '').toLowerCase();

                        // === RADIO BUTTONS: disability, veteran ===
                        // Find all radio groups (visible radio inputs grouped by name)
                        var radioNames = {};
                        var allRadios = document.querySelectorAll('input[type="radio"]');
                        for (var i = 0; i < allRadios.length; i++) {
                            var r = allRadios[i];
                            if (r.name && !r.checked) {
                                if (!radioNames[r.name]) radioNames[r.name] = [];
                                radioNames[r.name].push(r);
                            }
                        }
                        // Also check shadow DOM radio inputs
                        var splRadios = document.querySelectorAll('spl-radio, oc-radio-question');
                        for (var sr = 0; sr < splRadios.length; sr++) {
                            var root = splRadios[sr].shadowRoot || splRadios[sr];
                            var radios = root.querySelectorAll('input[type="radio"]');
                            for (var ri = 0; ri < radios.length; ri++) {
                                if (radios[ri].name && !radios[ri].checked) {
                                    if (!radioNames[radios[ri].name]) radioNames[radios[ri].name] = [];
                                    radioNames[radios[ri].name].push(radios[ri]);
                                }
                            }
                        }

                        for (var name in radioNames) {
                            var group = radioNames[name];
                            if (group.length === 0) continue;
                            // Get the group's label text from parent/fieldset
                            var parent = group[0].closest('fieldset, [role="radiogroup"], div, oc-radio-question');
                            var labelText = '';
                            if (parent) {
                                var lbl = parent.querySelector('legend, label, p, [class*="label"], [class*="question"]');
                                if (lbl) labelText = lbl.textContent.trim().toLowerCase();
                                if (!labelText) labelText = (parent.textContent || '').substring(0, 200).toLowerCase();
                            }

                            // Match disability question
                            if (labelText.indexOf('disab') >= 0 || labelText.indexOf('history/record') >= 0) {
                                // Select "I do not want to answer" or "No, I do not have a disability"
                                for (var d = 0; d < group.length; d++) {
                                    var optLabel = (group[d].closest('label') || group[d].parentElement || {}).textContent || '';
                                    optLabel = optLabel.trim().toLowerCase();
                                    if (optLabel.indexOf('do not want to answer') >= 0 || optLabel.indexOf('do not wish') >= 0) {
                                        group[d].click();
                                        group[d].checked = true;
                                        group[d].dispatchEvent(new Event('change', {bubbles: true, composed: true}));
                                        results.push('disability:' + optLabel.substring(0, 40));
                                        break;
                                    }
                                }
                            }
                            // Match veteran question
                            else if (labelText.indexOf('veteran') >= 0 || labelText.indexOf('protected veteran') >= 0) {
                                // Select "No" or "Prefer not to answer"
                                for (var v = 0; v < group.length; v++) {
                                    var vLabel = (group[v].closest('label') || group[v].parentElement || {}).textContent || '';
                                    vLabel = vLabel.trim().toLowerCase();
                                    if (vLabel === 'no' || vLabel.indexOf('prefer not') >= 0 || vLabel.indexOf('not a protected') >= 0) {
                                        group[v].click();
                                        group[v].checked = true;
                                        group[v].dispatchEvent(new Event('change', {bubbles: true, composed: true}));
                                        results.push('veteran:' + vLabel.substring(0, 40));
                                        break;
                                    }
                                }
                            }
                        }

                        // === SEARCH/AUTOCOMPLETE FIELDS: gender, race/ethnicity ===
                        // These render as <input> inside <spl-input> inside <sr-screening-questions-form>
                        // They have a search icon (magnifying glass) and a label before them
                        var allSplInputs = document.querySelectorAll('spl-input');
                        for (var si = 0; si < allSplInputs.length; si++) {
                            var host = allSplInputs[si];
                            var inp = host.shadowRoot ? host.shadowRoot.querySelector('input') : null;
                            if (!inp) continue;
                            // Skip if already filled
                            if (inp.value && inp.value.trim()) continue;
                            var rect = inp.getBoundingClientRect();
                            if (rect.width < 20 || rect.height < 5) continue;

                            // Get label: check parent's previous sibling, parent text, or attribute
                            var inputLabel = host.getAttribute('label') || host.getAttribute('aria-label') || '';
                            if (!inputLabel) {
                                var prevSib = host.previousElementSibling;
                                if (prevSib) inputLabel = (prevSib.textContent || '').trim();
                            }
                            if (!inputLabel) {
                                var par = host.parentElement;
                                if (par) {
                                    var pLabel = par.querySelector('label, p, span');
                                    if (pLabel && pLabel !== host) inputLabel = pLabel.textContent.trim();
                                }
                            }
                            var il = inputLabel.toLowerCase();

                            if (il.indexOf('gender') >= 0) {
                                results.push('GENDER_FIELD:' + Math.round(rect.x + rect.width/2) + ',' + Math.round(rect.y + rect.height/2));
                            } else if (il.indexOf('race') >= 0 || il.indexOf('ethnic') >= 0) {
                                results.push('RACE_FIELD:' + Math.round(rect.x + rect.width/2) + ',' + Math.round(rect.y + rect.height/2));
                            }
                        }

                        return results.length > 0 ? results.join('|') : 'NONE';
                    })()
                """)
                if eeoc_result and eeoc_result != 'NONE':
                    logger.info(f"EEOC fill result: {eeoc_result}")
                    import nodriver.cdp as cdp_eeoc
                    # Fill gender/race autocomplete fields
                    for part in str(eeoc_result).split('|'):
                        if part.startswith('GENDER_FIELD:') or part.startswith('RACE_FIELD:'):
                            coords = part.split(':')[1].split(',')
                            fx, fy = float(coords[0]), float(coords[1])
                            answer = "Male" if 'GENDER' in part else "Decline To Self Identify"
                            logger.info(f"Filling EEOC {'gender' if 'GENDER' in part else 'race'} at ({fx},{fy}) with '{answer}'")
                            # CDP click to focus
                            await nd_page.send(cdp_eeoc.input_.dispatch_mouse_event(
                                type_="mousePressed", x=fx, y=fy,
                                button=cdp_eeoc.input_.MouseButton.LEFT, click_count=1))
                            await nd_page.send(cdp_eeoc.input_.dispatch_mouse_event(
                                type_="mouseReleased", x=fx, y=fy,
                                button=cdp_eeoc.input_.MouseButton.LEFT, click_count=1))
                            await asyncio.sleep(0.5)
                            # Type answer
                            for char in answer:
                                await nd_page.send(cdp_eeoc.input_.dispatch_key_event(type_="keyDown", key=char))
                                await nd_page.send(cdp_eeoc.input_.dispatch_key_event(type_="char", text=char, key=char))
                                await nd_page.send(cdp_eeoc.input_.dispatch_key_event(type_="keyUp", key=char))
                                await asyncio.sleep(0.03)
                            await asyncio.sleep(2)
                            # Click first matching suggestion
                            click_res = await nd_page.evaluate("""
                                (function() {
                                    // Check for suggestions: role=option, listbox items, cdk-overlay items
                                    var selectors = [
                                        '[role="option"]', '[role="listbox"] li',
                                        'li[class*="option"]', '[class*="cdk-overlay"] li',
                                        '[class*="suggestion"]', 'mat-option'
                                    ];
                                    var items = document.querySelectorAll(selectors.join(','));
                                    for (var i = 0; i < items.length; i++) {
                                        var r = items[i].getBoundingClientRect();
                                        if (r.width > 0 && r.height > 0 && r.height < 200) {
                                            items[i].click();
                                            return 'CLICKED:' + (items[i].textContent || '').trim().substring(0, 60);
                                        }
                                    }
                                    return 'NO_SUGGESTIONS';
                                })()
                            """)
                            logger.info(f"EEOC suggestion click: {click_res}")
                            await asyncio.sleep(0.5)
            except Exception as eeoc_e:
                logger.debug(f"EEOC fill failed: {eeoc_e}")

            # Handle spl-select dropdowns that need answers (screening questions as dropdowns)
            # This is a FALLBACK for any selects that _nd_handle_screening_questions missed
            # MUST trigger zone.js spl-change on the host for Angular to see the change
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
                            if (inner.options.length > 1) {
                                var picked = -1;
                                for (var j = 1; j < inner.options.length; j++) {
                                    var optText = (inner.options[j].text || '').toLowerCase();
                                    // Prefer "yes" or positive options
                                    if (optText.indexOf('yes') >= 0 || optText === 'true') {
                                        picked = j;
                                        break;
                                    }
                                }
                                // If no "yes" found, just select first option
                                if (picked < 0) picked = 1;

                                inner.selectedIndex = picked;
                                inner.dispatchEvent(new Event('change', {bubbles: true, composed: true}));
                                inner.dispatchEvent(new Event('input', {bubbles: true, composed: true}));

                                // Trigger zone.js spl-change on host for Angular model update
                                try { selects[i].value = inner.options[picked].value; } catch(e) {}
                                var splKey = '__zone_symbol__spl-changefalse';
                                if (selects[i][splKey] && Array.isArray(selects[i][splKey])) {
                                    for (var z = 0; z < selects[i][splKey].length; z++) {
                                        try {
                                            var h = selects[i][splKey][z].handler || selects[i][splKey][z];
                                            if (typeof h === 'function') {
                                                h(new CustomEvent('spl-change', {
                                                    detail: {value: inner.options[picked].value}, bubbles: true
                                                }));
                                            }
                                        } catch(e) {}
                                    }
                                }
                                selects[i].dispatchEvent(new Event('change', {bubbles: true}));
                                filled.push(label + '=' + inner.options[picked].text);
                            }
                        }
                        return filled.length > 0 ? JSON.stringify(filled) : 'NONE';
                    })()
                """)
                if select_result and select_result != 'NONE':
                    logger.info(f"Fallback filled spl-select dropdowns on step {step + 1}: {select_result}")
            except Exception:
                pass

            # Screenshot each step for debugging
            try:
                ss_path = f"data/screenshots/SR_STEP{step+1}_{__import__('datetime').datetime.now().strftime('%H%M%S')}.png"
                await nd_page.save_screenshot(ss_path)
                logger.info(f"Step {step + 1} screenshot: {ss_path}")
            except Exception:
                pass

            # Click navigation buttons — try multiple strategies
            try:
                # Install console error catcher (no network interceptor — causes infinite recursion with zone.js)
                await nd_page.evaluate("""
                    if (!window.__sr_console_patched) {
                        window.__sr_console_errors = [];
                        var origError = console.error;
                        console.error = function() {
                            window.__sr_console_errors.push(Array.from(arguments).join(' ').substring(0, 200));
                            origError.apply(console, arguments);
                        };
                        window.__sr_console_patched = true;
                    } else {
                        window.__sr_console_errors = [];
                    }
                """)

                nav_result = await nd_page.evaluate("""
                    (function() {
                        var allButtons = [];

                        // Collect ALL spl-button components
                        // CRITICAL: use the INNER button coordinates from shadow DOM
                        // not the host coordinates — click events on host SPAN don't reach
                        // the inner button's zone.js click handler
                        var splBtns = document.querySelectorAll('spl-button');
                        for (var i = 0; i < splBtns.length; i++) {
                            var text = (splBtns[i].textContent || '').trim().toLowerCase();
                            var rect = splBtns[i].getBoundingClientRect();
                            if (rect.width === 0 || rect.height === 0) continue;
                            // Get inner button coordinates from shadow DOM
                            var innerRect = rect;
                            if (splBtns[i].shadowRoot) {
                                var inner = splBtns[i].shadowRoot.querySelector('button');
                                if (inner) {
                                    var ir = inner.getBoundingClientRect();
                                    if (ir.width > 0 && ir.height > 0) innerRect = ir;
                                }
                            }
                            allButtons.push({text: text, host: splBtns[i], type: 'spl',
                                x: innerRect.x + innerRect.width/2, y: innerRect.y + innerRect.height/2});
                        }

                        // Collect regular buttons
                        var btns = document.querySelectorAll('button, a[role="button"], input[type="submit"]');
                        for (var j = 0; j < btns.length; j++) {
                            var t = (btns[j].textContent || '').trim().toLowerCase();
                            if (btns[j].closest('spl-button')) continue;
                            var r = btns[j].getBoundingClientRect();
                            if (r.width === 0 || r.height === 0) continue;
                            allButtons.push({text: t, host: btns[j], type: 'html',
                                x: r.x + r.width/2, y: r.y + r.height/2});
                        }

                        function findButton(priority) {
                            for (var k = 0; k < allButtons.length; k++) {
                                var txt = allButtons[k].text;
                                if (priority === 'next' && (txt === 'next' || txt === 'continue' || txt === 'next step' ||
                                    txt === 'save & next' || txt === 'save and next' || txt.indexOf('next') === 0)) {
                                    return allButtons[k];
                                }
                                if (priority === 'submit' && (txt.indexOf('submit') >= 0 || txt.indexOf('apply now') >= 0)) {
                                    return allButtons[k];
                                }
                            }
                            return null;
                        }

                        var btn = findButton('next') || findButton('submit');
                        if (!btn) {
                            var dbg = allButtons.map(function(b) { return b.text; }).slice(0, 8);
                            return 'NO_NAV:visible=' + JSON.stringify(dbg);
                        }

                        var action = btn.text.indexOf('next') >= 0 || btn.text === 'continue' ? 'NEXT' : 'SUBMIT';

                        // Return button info — DON'T click here, let CDP handle it
                        return JSON.stringify({action: action, text: btn.text, type: btn.type,
                            x: btn.x, y: btn.y});
                    })()
                """)
                nav_str = str(nav_result) if nav_result else 'NO_RESULT'

                clicked = False
                if nav_str.startswith('{') or ('{' in nav_str and '"action"' in nav_str):
                    import json as _json
                    btn_data = _json.loads(nav_str)
                    action = btn_data['action']
                    btn_text = btn_data['text']
                    logger.info(f"Step {step + 1} nav result: {action}:{btn_text}:{btn_data['type']}")

                    # Check what element is at the button coordinates
                    import nodriver.cdp.input_ as cdp_nav
                    bx, by = float(btn_data['x']), float(btn_data['y'])
                    try:
                        elem_at_point = await nd_page.evaluate(f"""
                            (function() {{
                                var el = document.elementFromPoint({bx}, {by});
                                if (!el) return 'null';
                                return el.tagName + '#' + el.id + '.' + (el.className || '').toString().substring(0,50) +
                                    ' text=' + (el.textContent || '').trim().substring(0,30) +
                                    ' parent=' + (el.parentElement ? el.parentElement.tagName : 'none');
                            }})()
                        """)
                        logger.info(f"Element at ({bx:.0f},{by:.0f}): {elem_at_point}")
                    except Exception:
                        pass

                    # Use CDP mouse events at the INNER button coordinates
                    # nodriver's find+click hits the SPAN (projected light DOM content)
                    # which doesn't trigger the shadow DOM inner button's zone.js handler.
                    # CDP mouse events at the inner button coords send a trusted click.
                    logger.info(f"CDP clicking {action} button '{btn_text}' at ({bx:.0f}, {by:.0f})")
                    try:
                        await nd_page.send(cdp_nav.dispatch_mouse_event(
                            type_="mouseMoved", x=bx, y=by))
                        await asyncio.sleep(0.05)
                        await nd_page.send(cdp_nav.dispatch_mouse_event(
                            type_="mousePressed", x=bx, y=by,
                            button=cdp_nav.MouseButton.LEFT, click_count=1))
                        await asyncio.sleep(0.05)
                        await nd_page.send(cdp_nav.dispatch_mouse_event(
                            type_="mouseReleased", x=bx, y=by,
                            button=cdp_nav.MouseButton.LEFT, click_count=1))
                        logger.info(f"CDP clicked '{btn_text}' at ({bx:.0f}, {by:.0f})")
                        # CDP mouse at coords hits SPAN (light DOM projected via slot),
                        # NOT the inner shadow DOM button. The click bubbles through light DOM
                        # and never reaches inner button's zone.js handler.
                        # Fix: focus the inner button via JS, then send CDP Enter key (trusted).
                        await asyncio.sleep(0.3)

                        # CRITICAL: Before clicking submit, ensure Angular sees checkbox changes.
                        # Angular's spl-checkbox uses NgZone-patched event listeners. We need to:
                        # 1. Toggle checkbox via the inner input (so DOM state is correct)
                        # 2. Fire 'change' event on inner input (triggers component's ControlValueAccessor)
                        # 3. Mark form control as dirty/touched via Angular's __ngContext__
                        btn_target = 'submit' if action == 'SUBMIT' else 'next'
                        click_result = await nd_page.evaluate("""
                            (function() {
                                var results = [];

                                // First: ensure ALL spl-checkbox are properly marked as touched/dirty
                                // Angular requires ng-touched + ng-dirty for form validation to pass submit
                                var splChecks = document.querySelectorAll('spl-checkbox');
                                for (var i = 0; i < splChecks.length; i++) {
                                    var host = splChecks[i];
                                    var sr = host.shadowRoot;
                                    if (!sr) continue;
                                    var inner = sr.querySelector('input[type="checkbox"]');
                                    if (!inner) continue;

                                    // Ensure checked
                                    if (!inner.checked) inner.checked = true;

                                    // Update Angular classes on host (remove pristine/untouched, add dirty/touched)
                                    host.classList.remove('ng-pristine', 'ng-untouched');
                                    host.classList.add('ng-dirty', 'ng-touched');

                                    // Dispatch events that Angular's ControlValueAccessor listens for
                                    inner.dispatchEvent(new Event('change', {bubbles: true, composed: true}));
                                    inner.dispatchEvent(new Event('input', {bubbles: true, composed: true}));

                                    // Try Angular's internal change detection via component ref
                                    // The __ngContext__ on the host contains the LView
                                    try {
                                        if (host.__ngContext__ && typeof host.__ngContext__ === 'number') {
                                            // Angular Ivy: __ngContext__ is the LView index
                                            // We need to trigger change detection on the parent form
                                        }
                                    } catch(e) {}

                                    results.push(host.id + ':dirty');
                                }

                                // Now update the parent form's Angular classes too
                                var formEls = document.querySelectorAll('.ng-pristine');
                                for (var f = 0; f < formEls.length; f++) {
                                    formEls[f].classList.remove('ng-pristine', 'ng-untouched');
                                    formEls[f].classList.add('ng-dirty', 'ng-touched');
                                }

                                // Finally: click the inner button directly (JS click within same execution
                                // context runs inside NgZone automatically)
                                var splBtns = document.querySelectorAll('spl-button');
                                for (var b = 0; b < splBtns.length; b++) {
                                    var text = (splBtns[b].textContent || '').trim().toLowerCase();
                                    if (text.indexOf('""" + btn_target + """') >= 0) {
                                        if (splBtns[b].shadowRoot) {
                                            var innerBtn = splBtns[b].shadowRoot.querySelector('button');
                                            if (innerBtn) {
                                                innerBtn.click();
                                                results.push('INNER_CLICK:' + text);
                                            }
                                        }
                                        // Also click host for good measure
                                        splBtns[b].click();
                                        results.push('HOST_CLICK:' + text);
                                    }
                                }
                                return results.join(',');
                            })()
                        """)
                        logger.info(f"Angular-aware click result: {click_result}")

                        # Check if Angular click already navigated the page
                        await asyncio.sleep(1)
                        current_url_before = await nd_page.evaluate("window.location.href")
                        post_angular_url = current_url_before  # Will be compared after each fallback

                        # PRIMARY: use nodriver's native find+click (triggers real browser click
                        # that Zone.js intercepts — most reliable for Angular forms)
                        if 'INNER_CLICK' not in str(click_result) and 'HOST_CLICK' not in str(click_result):
                            # Angular click didn't fire — try nodriver
                            pass  # fall through to nodriver click below
                        else:
                            # Angular click fired — check if page navigated
                            post_angular_url = await nd_page.evaluate("window.location.href")

                        if post_angular_url != current_url_before:
                            logger.info(f"Page navigated after Angular click — skipping fallbacks")
                        else:
                            await asyncio.sleep(0.5)
                            try:
                                nd_btn_text = "Submit" if action == 'SUBMIT' else "Next"
                                nd_btn = await nd_page.find(nd_btn_text, best_match=True, timeout=3)
                                if nd_btn:
                                    await nd_btn.click()
                                    logger.info(f"nodriver native click on '{nd_btn_text}' succeeded")
                            except Exception as nd_click_e:
                                logger.debug(f"nodriver find+click failed: {nd_click_e}")

                            # Check if nodriver click navigated the page
                            await asyncio.sleep(1)
                            post_nodriver_url = await nd_page.evaluate("window.location.href")
                            if post_nodriver_url != current_url_before:
                                logger.info(f"Page navigated after nodriver click — skipping Space key")
                            else:
                                await asyncio.sleep(0.1)
                                # Send Space key as last resort — trusted CDP event on focused inner button
                                await nd_page.evaluate("""
                                    (function() {
                                        var splBtns = document.querySelectorAll('spl-button');
                                        for (var i = 0; i < splBtns.length; i++) {
                                            var text = (splBtns[i].textContent || '').trim().toLowerCase();
                                            if (text.indexOf('""" + btn_target + """') >= 0) {
                                                if (splBtns[i].shadowRoot) {
                                                    var inner = splBtns[i].shadowRoot.querySelector('button');
                                                    if (inner) inner.focus();
                                                }
                                                return;
                                            }
                                        }
                                    })()
                                """)
                                await asyncio.sleep(0.1)
                                await nd_page.send(cdp_nav.dispatch_key_event(
                                    type_="keyDown", key=" ",
                                    code="Space", windows_virtual_key_code=32, native_virtual_key_code=32))
                                await nd_page.send(cdp_nav.dispatch_key_event(
                                    type_="keyUp", key=" ",
                                    code="Space", windows_virtual_key_code=32, native_virtual_key_code=32))
                                logger.info(f"Sent Space key to focused inner button for '{btn_text}'")
                    except Exception as cdp_click_e:
                        logger.info(f"CDP click failed ({cdp_click_e}), trying nodriver find")
                        try:
                            nd_btn = await nd_page.find(btn_text, best_match=True)
                            if nd_btn:
                                await nd_btn.click()
                                logger.info(f"nodriver clicked '{btn_text}' as fallback")
                        except Exception:
                            pass
                    clicked = True
                else:
                    logger.info(f"Step {step + 1} nav result: {nav_str}")

                if clicked:
                    await asyncio.sleep(3)
                    # Check for console errors and network calls after click
                    try:
                        errors = await nd_page.evaluate("JSON.stringify(window.__sr_console_errors || [])")
                        if errors and errors != '[]':
                            logger.info(f"Console errors after click: {errors}")
                        # Reset for next iteration
                        await nd_page.evaluate("window.__sr_console_errors = [];")
                    except Exception:
                        pass
                    continue
            except Exception as e:
                logger.debug(f"Navigation click failed: {e}")

            # Fallback: use nodriver's native find() + click() for button
            # nodriver handles shadow DOM traversal internally
            for btn_text in ["Next", "Continue", "Submit Application", "Submit"]:
                try:
                    btn = await nd_page.find(btn_text, best_match=True)
                    if btn:
                        found_text = str(getattr(btn, 'text', '')).strip().lower()
                        if btn_text.lower() in found_text or found_text in btn_text.lower():
                            await btn.click()
                            logger.info(f"nodriver click '{btn_text}' at step {step + 1}")
                            await asyncio.sleep(3)
                            break
                except Exception:
                    continue

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
