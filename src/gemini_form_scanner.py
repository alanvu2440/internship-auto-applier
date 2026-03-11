"""
Gemini Form Scanner

Uses Gemini AI to scan forms for unfilled fields and fill them intelligently.
Two-pass approach:
  1. DOM extraction — cheap, fast, gets field labels/types/values
  2. Vision (screenshot) — fallback when DOM pass misses fields or can't figure them out

Designed to run AFTER the existing form filler as a cleanup sweep.
"""

import asyncio
import base64
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

try:
    import google.generativeai as genai
    GENAI_AVAILABLE = True
except ImportError:
    genai = None
    GENAI_AVAILABLE = False


# JS to extract all form fields from the page
EXTRACT_FIELDS_JS = """() => {
    const fields = [];

    // Helper: walk up DOM to find label text
    function findLabel(el) {
        // Check aria-label
        if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
        // Check explicit <label for="...">
        if (el.id) {
            const label = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
            if (label) return label.textContent.trim();
        }
        // Check wrapping label
        const parent = el.closest('label');
        if (parent) return parent.textContent.trim();
        // Check preceding label/legend in parent containers
        let p = el.parentElement;
        for (let i = 0; i < 5 && p; i++) {
            const lbl = p.querySelector('label, legend, [class*="label"], [data-automation-id="richText"]');
            if (lbl && lbl.textContent.trim() && !lbl.contains(el)) {
                return lbl.textContent.trim();
            }
            p = p.parentElement;
        }
        // Check placeholder
        if (el.placeholder) return el.placeholder;
        // Check name attribute
        if (el.name) return el.name;
        return '';
    }

    // Helper: get a CSS selector that uniquely identifies this element
    function getSelector(el) {
        if (el.id) return '#' + CSS.escape(el.id);
        if (el.name) {
            const tag = el.tagName.toLowerCase();
            const sel = tag + '[name="' + CSS.escape(el.name) + '"]';
            if (document.querySelectorAll(sel).length === 1) return sel;
        }
        // Build a path using nth-child
        const parts = [];
        let current = el;
        while (current && current !== document.body) {
            let selector = current.tagName.toLowerCase();
            if (current.id) {
                selector = '#' + CSS.escape(current.id);
                parts.unshift(selector);
                break;
            }
            const parent = current.parentElement;
            if (parent) {
                const siblings = Array.from(parent.children).filter(c => c.tagName === current.tagName);
                if (siblings.length > 1) {
                    const idx = siblings.indexOf(current) + 1;
                    selector += ':nth-of-type(' + idx + ')';
                }
            }
            parts.unshift(selector);
            current = current.parentElement;
        }
        return parts.join(' > ');
    }

    // Text/email/tel/url/number inputs + textareas
    const inputs = document.querySelectorAll(
        'input:not([type="hidden"]):not([type="file"]):not([type="submit"]):not([type="button"]):not([type="image"]), textarea'
    );
    for (const inp of inputs) {
        const rect = inp.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) continue;
        if (inp.disabled || inp.readOnly) continue;
        const type = inp.type || inp.tagName.toLowerCase();
        if (type === 'radio' || type === 'checkbox') continue;

        const label = findLabel(inp);
        const isRequired = inp.required || inp.getAttribute('aria-required') === 'true' || label.includes('*');

        fields.push({
            type: type === 'TEXTAREA' ? 'textarea' : (inp.type || 'text'),
            label: label.substring(0, 200),
            value: inp.value || '',
            required: isRequired,
            selector: getSelector(inp),
            tag: inp.tagName.toLowerCase(),
            empty: !inp.value || !inp.value.trim(),
        });
    }

    // Select dropdowns
    const selects = document.querySelectorAll('select');
    for (const sel of selects) {
        const rect = sel.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) continue;
        if (sel.disabled) continue;

        const label = findLabel(sel);
        const isRequired = sel.required || sel.getAttribute('aria-required') === 'true' || label.includes('*');
        const options = Array.from(sel.options).map(o => ({
            value: o.value,
            text: o.textContent.trim(),
            selected: o.selected,
        }));
        const selectedIdx = sel.selectedIndex;
        const hasRealSelection = selectedIdx > 0 || (selectedIdx === 0 && options[0] && !options[0].text.match(/select|choose|pick|--/i));

        fields.push({
            type: 'select',
            label: label.substring(0, 200),
            value: hasRealSelection ? (options[selectedIdx]?.text || '') : '',
            required: isRequired,
            selector: getSelector(sel),
            tag: 'select',
            empty: !hasRealSelection,
            options: options.filter(o => o.text && !o.text.match(/^(select|choose|pick|--|please)/i)).map(o => o.text).slice(0, 50),
        });
    }

    // Radio button groups
    const radioGroups = {};
    for (const radio of document.querySelectorAll('input[type="radio"]')) {
        const name = radio.name;
        if (!name) continue;
        if (!radioGroups[name]) {
            radioGroups[name] = { options: [], checked: '', label: '' };
        }
        const label = radio.parentElement?.textContent?.trim() || radio.value;
        radioGroups[name].options.push(label.substring(0, 100));
        if (radio.checked) radioGroups[name].checked = label.substring(0, 100);
    }
    for (const [name, group] of Object.entries(radioGroups)) {
        const fieldset = document.querySelector('input[name="' + CSS.escape(name) + '"]')?.closest('fieldset');
        const legend = fieldset?.querySelector('legend')?.textContent?.trim() || '';
        const firstRadio = document.querySelector('input[name="' + CSS.escape(name) + '"]');
        const parentLabel = findLabel(firstRadio) || legend;
        const isRequired = firstRadio?.required || firstRadio?.getAttribute('aria-required') === 'true' || parentLabel.includes('*');

        fields.push({
            type: 'radio',
            label: (parentLabel || name).substring(0, 200),
            value: group.checked,
            required: isRequired,
            selector: 'input[name="' + CSS.escape(name) + '"]',
            tag: 'input',
            empty: !group.checked,
            options: group.options,
        });
    }

    // Checkboxes (standalone required ones)
    for (const cb of document.querySelectorAll('input[type="checkbox"]')) {
        const rect = cb.getBoundingClientRect();
        if (rect.width === 0 && rect.height === 0) continue;
        const label = findLabel(cb);
        const isRequired = cb.required || cb.getAttribute('aria-required') === 'true';
        if (!isRequired) continue;  // Only care about required checkboxes

        fields.push({
            type: 'checkbox',
            label: label.substring(0, 200),
            value: cb.checked ? 'checked' : '',
            required: true,
            selector: getSelector(cb),
            tag: 'input',
            empty: !cb.checked,
        });
    }

    // File inputs (resume/cover letter)
    for (const inp of document.querySelectorAll('input[type="file"]')) {
        const label = findLabel(inp);
        const isRequired = inp.required || label.includes('*') || /resume|cv/i.test(label);

        fields.push({
            type: 'file',
            label: label.substring(0, 200),
            value: inp.files.length > 0 ? inp.files[0].name : '',
            required: isRequired,
            selector: getSelector(inp),
            tag: 'input',
            empty: inp.files.length === 0,
        });
    }

    return fields;
}"""


class GeminiFormScanner:
    """Scans forms with Gemini AI to find and fill empty fields."""

    DIAGNOSIS_PROMPT = """You are a job application form filling assistant. Analyze these form fields and determine what value should go in each EMPTY field.

CANDIDATE PROFILE:
{profile}

FORM FIELDS (JSON):
{fields_json}

For each empty field, determine the correct value from the candidate profile.

Rules:
- Only fill fields that are currently EMPTY
- For dropdowns/radios, pick from the available options EXACTLY as listed
- For yes/no questions about work authorization: Yes (US citizen)
- For sponsorship questions: No (doesn't need sponsorship)
- For text fields, use the candidate's actual info
- For "How did you hear about us": say "Job Board"
- For salary/compensation: leave empty or pick lowest range
- Skip file upload fields (type=file)
- For checkboxes that are required (like terms/privacy), set to "check"

Return a JSON array of objects, each with:
  {{"selector": "the CSS selector", "value": "the value to fill", "action": "type|select|click"}}

Only include fields you can confidently fill. Return empty array [] if nothing to fill."""

    VISION_PROMPT = """You are looking at a job application form screenshot. Some fields appear to be empty or not properly filled.

CANDIDATE PROFILE:
{profile}

Look at the form and identify ALL empty/unfilled required fields. For each one, describe:
1. The field label you see
2. What value should go there based on the candidate profile
3. The approximate location on the page (top/middle/bottom, left/right)

Return a JSON array:
[{{"label": "field label text", "value": "what to fill", "location": "description of where it is"}}]

Focus on REQUIRED fields (usually marked with * or in red). Return [] if the form looks fully filled."""

    def __init__(self, ai_answerer):
        """Initialize with an existing AIAnswerer (shares Gemini model + cost tracking).

        Args:
            ai_answerer: AIAnswerer instance with configured Gemini model
        """
        self.ai = ai_answerer
        self._session_cost = 0.0
        self._session_cap = 1.0  # $1 max per session for scanner calls
        self._fill_log: List[Dict] = []

    def _build_profile_summary(self) -> str:
        """Build a concise profile summary from config."""
        config = self.ai.config
        parts = []

        pi = config.get("personal_info", {})
        if pi:
            parts.append(f"Name: {pi.get('first_name', '')} {pi.get('last_name', '')}")
            parts.append(f"Email: {pi.get('email', '')}")
            parts.append(f"Phone: {pi.get('phone', '')}")
            parts.append(f"Location: {pi.get('city', '')}, {pi.get('state', '')} {pi.get('zip_code', '')}")
            if pi.get("linkedin"):
                parts.append(f"LinkedIn: {pi['linkedin']}")
            if pi.get("github"):
                parts.append(f"GitHub: {pi['github']}")
            if pi.get("portfolio") or pi.get("website"):
                parts.append(f"Website: {pi.get('portfolio') or pi.get('website')}")
            parts.append(f"Address: {pi.get('address', '')}")
            parts.append(f"Country: {pi.get('country', 'United States')}")

        edu = config.get("education", [{}])
        if edu and isinstance(edu, list) and edu[0]:
            e = edu[0]
            parts.append(f"School: {e.get('school', '')}")
            parts.append(f"Degree: {e.get('degree', '')} in {e.get('field_of_study', '')}")
            parts.append(f"GPA: {e.get('gpa', '')}")
            parts.append(f"Graduation: {e.get('graduation_date', '')}")

        wa = config.get("work_authorization", {})
        if wa:
            parts.append(f"US Citizen: {wa.get('us_citizen', True)}")
            parts.append(f"Work Authorized: {wa.get('us_work_authorized', True)}")
            parts.append(f"Needs Sponsorship: {wa.get('require_sponsorship_now', False)}")

        demo = config.get("demographics", {})
        if demo:
            parts.append(f"Gender: {demo.get('gender', '')}")
            parts.append(f"Race: {demo.get('race', '')}")
            parts.append(f"Veteran: {demo.get('veteran_status', 'Not a veteran')}")
            parts.append(f"Disability: {demo.get('disability_status', 'Prefer not to answer')}")

        avail = config.get("availability", {})
        if avail:
            parts.append(f"Start Date: {avail.get('earliest_start', '')}")

        return "\n".join(parts)

    async def scan_and_fill(self, page, max_retries: int = 1) -> Dict[str, Any]:
        """Main entry point: scan the page for empty fields and fill them.

        Args:
            page: Playwright page object
            max_retries: How many times to re-scan after filling (catches conditional fields)

        Returns:
            Dict with 'filled' (fields we filled), 'still_empty' (fields we couldn't fill),
            'cost' (estimated USD cost for this scan)
        """
        total_filled = {}
        still_empty = []

        for attempt in range(1 + max_retries):
            if attempt > 0:
                logger.info(f"Scanner re-scan attempt {attempt + 1}...")
                await asyncio.sleep(1)  # Let page react to fills

            # Pass 1: DOM extraction
            dom_filled, dom_empty = await self._dom_pass(page)
            total_filled.update(dom_filled)

            if not dom_empty:
                logger.info("All fields filled after DOM pass")
                break

            # Pass 2: Vision (screenshot) for remaining empty fields
            vision_filled, vision_empty = await self._vision_pass(page, dom_empty)
            total_filled.update(vision_filled)
            still_empty = vision_empty

            if not still_empty:
                logger.info("All fields filled after vision pass")
                break

        result = {
            "filled": total_filled,
            "still_empty": still_empty,
            "cost": self._session_cost,
            "passes": attempt + 1,
        }

        # Log results
        logger.info(
            f"Scanner complete: filled {len(total_filled)} fields, "
            f"{len(still_empty)} still empty, cost=${self._session_cost:.4f}"
        )
        self._fill_log.append(result)
        return result

    async def _dom_pass(self, page) -> tuple:
        """Pass 1: Extract fields from DOM, ask Gemini what to fill, execute fills.

        Returns:
            (filled_dict, list_of_still_empty_fields)
        """
        filled = {}

        # Extract all form fields
        try:
            fields = await page.evaluate(EXTRACT_FIELDS_JS)
        except Exception as e:
            logger.error(f"Failed to extract form fields: {e}")
            return filled, []

        if not fields:
            logger.debug("No form fields found on page")
            return filled, []

        # Filter to empty fields only
        empty_fields = [f for f in fields if f.get("empty")]
        all_required_empty = [f for f in empty_fields if f.get("required")]

        logger.info(
            f"Scanner DOM pass: {len(fields)} total fields, "
            f"{len(empty_fields)} empty, {len(all_required_empty)} required+empty"
        )

        if not empty_fields:
            return filled, []

        # Check session cost cap
        if self._session_cost >= self._session_cap:
            logger.warning(f"Scanner session cost cap reached (${self._session_cost:.4f})")
            return filled, empty_fields

        # Ask Gemini what to fill
        profile = self._build_profile_summary()
        # Only send empty fields to Gemini (cheaper)
        fields_for_gemini = []
        for f in empty_fields:
            entry = {
                "label": f["label"],
                "type": f["type"],
                "selector": f["selector"],
                "required": f["required"],
            }
            if f.get("options"):
                entry["options"] = f["options"]
            fields_for_gemini.append(entry)

        prompt = self.DIAGNOSIS_PROMPT.format(
            profile=profile,
            fields_json=json.dumps(fields_for_gemini, indent=2),
        )

        diagnosis = await self.ai.diagnose_with_gemini(prompt, max_output_tokens=2000)
        if not diagnosis:
            logger.warning("Gemini returned no diagnosis for DOM pass")
            return filled, empty_fields

        # Track cost estimate
        self._session_cost += 0.0004  # ~$0.0004 per call

        # diagnosis should be a list of {selector, value, action}
        fills = diagnosis if isinstance(diagnosis, list) else diagnosis.get("fills", diagnosis.get("fields", []))
        if not isinstance(fills, list):
            logger.warning(f"Unexpected diagnosis format: {type(fills)}")
            return filled, empty_fields

        # Execute fills
        for fill in fills:
            selector = fill.get("selector", "")
            value = fill.get("value", "")
            action = fill.get("action", "type")

            if not selector or not value:
                continue

            try:
                success = await self._execute_fill(page, selector, value, action)
                if success:
                    filled[selector] = value
                    logger.debug(f"Scanner filled: {selector} = {value[:50]}")
            except Exception as e:
                logger.debug(f"Scanner fill failed for {selector}: {e}")

        # Re-check what's still empty
        try:
            fields_after = await page.evaluate(EXTRACT_FIELDS_JS)
            still_empty = [f for f in fields_after if f.get("empty") and f.get("required")]
        except Exception:
            still_empty = []

        return filled, still_empty

    async def _vision_pass(self, page, empty_fields: list) -> tuple:
        """Pass 2: Take screenshot, ask Gemini vision what fields are still empty.

        Args:
            page: Playwright page
            empty_fields: Fields that DOM pass couldn't fill

        Returns:
            (filled_dict, list_of_still_empty_fields)
        """
        filled = {}

        if not GENAI_AVAILABLE:
            return filled, empty_fields

        # Check cost cap
        if self._session_cost >= self._session_cap:
            logger.warning("Scanner session cost cap reached, skipping vision pass")
            return filled, empty_fields

        # Take screenshot
        try:
            screenshot_bytes = await page.screenshot(full_page=True)
        except Exception as e:
            logger.error(f"Failed to take screenshot for vision pass: {e}")
            return filled, empty_fields

        # Build vision prompt
        profile = self._build_profile_summary()
        prompt = self.VISION_PROMPT.format(profile=profile)

        # Call Gemini with image
        try:
            model = self.ai._get_model()
            if not model:
                return filled, empty_fields

            # Create image part for Gemini
            image_part = {
                "mime_type": "image/png",
                "data": base64.b64encode(screenshot_bytes).decode("utf-8"),
            }

            response = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: model.generate_content(
                        [prompt, image_part],
                        generation_config=genai.types.GenerationConfig(
                            max_output_tokens=2000,
                            temperature=0.2,
                            response_mime_type="application/json",
                        ),
                    )
                ),
                timeout=45,
            )

            text = response.text.strip()
            self.ai._track_ai_call(
                is_backup=self.ai._using_backup,
                input_len=len(prompt) + len(screenshot_bytes),
                output_len=len(text),
            )
            self._session_cost += 0.002  # Vision calls cost more

            vision_fills = self._parse_json_response(text)
            if vision_fills is None:
                logger.warning("Vision pass returned unparseable response")
                return filled, empty_fields
            if not isinstance(vision_fills, list):
                vision_fills = vision_fills.get("fields", []) if isinstance(vision_fills, dict) else []

            logger.info(f"Vision pass identified {len(vision_fills)} fields to fill")

        except asyncio.TimeoutError:
            logger.warning("Vision pass timed out")
            return filled, empty_fields
        except Exception as e:
            logger.error(f"Vision pass failed: {e}")
            return filled, empty_fields

        # Try to match vision results to DOM fields and fill them
        for vf in vision_fills:
            label = vf.get("label", "")
            value = vf.get("value", "")
            if not label or not value:
                continue

            # Find matching DOM field by label text
            matched = await self._find_field_by_label(page, label)
            if matched:
                try:
                    success = await self._execute_fill(page, matched["selector"], value, matched.get("action", "type"))
                    if success:
                        filled[label] = value
                        logger.debug(f"Vision filled: {label} = {value[:50]}")
                except Exception as e:
                    logger.debug(f"Vision fill failed for {label}: {e}")

        # Final check
        try:
            fields_after = await page.evaluate(EXTRACT_FIELDS_JS)
            still_empty = [f for f in fields_after if f.get("empty") and f.get("required")]
        except Exception:
            still_empty = empty_fields

        return filled, still_empty

    async def _find_field_by_label(self, page, label_text: str) -> Optional[Dict]:
        """Find a form field on the page by its label text.

        Returns:
            Dict with 'selector' and 'action' if found, None otherwise
        """
        label_lower = label_text.lower().strip()

        try:
            fields = await page.evaluate(EXTRACT_FIELDS_JS)
            for f in fields:
                if not f.get("empty"):
                    continue
                field_label = f.get("label", "").lower().strip()
                # Fuzzy match: check if most words overlap
                label_words = set(re.findall(r'\w+', label_lower))
                field_words = set(re.findall(r'\w+', field_label))
                if not label_words:
                    continue
                overlap = len(label_words & field_words) / len(label_words)
                if overlap >= 0.6:
                    action = "type"
                    if f["type"] == "select":
                        action = "select"
                    elif f["type"] in ("radio", "checkbox"):
                        action = "click"
                    return {"selector": f["selector"], "action": action, "field": f}
        except Exception:
            pass
        return None

    @staticmethod
    def _parse_json_response(text: str) -> Optional[Any]:
        """Parse JSON from Gemini response, handling common issues."""
        text = text.strip()
        # Strip markdown fences
        if text.startswith("```"):
            text = re.sub(r'^```(?:json)?\s*', '', text)
            text = re.sub(r'\s*```$', '', text)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # Try closing truncated JSON
        for fix in [text + "]", text + "}]", text + '"}]', text + '"}']:
            try:
                return json.loads(fix)
            except json.JSONDecodeError:
                continue
        # Extract any JSON array
        arr = re.search(r'\[.*\]', text, re.DOTALL)
        if arr:
            try:
                return json.loads(arr.group())
            except json.JSONDecodeError:
                pass
        return None

    async def _execute_fill(self, page, selector: str, value: str, action: str = "type") -> bool:
        """Execute a single field fill operation.

        Args:
            page: Playwright page
            selector: CSS selector for the field
            value: Value to fill
            action: 'type', 'select', or 'click'

        Returns:
            True if successful
        """
        try:
            element = await page.query_selector(selector)
            if not element:
                logger.debug(f"Element not found: {selector}")
                return False

            # Scroll into view
            await element.scroll_into_view_if_needed()
            await asyncio.sleep(0.2)

            if action == "select":
                # For <select> dropdowns
                tag = await element.evaluate("el => el.tagName.toLowerCase()")
                if tag == "select":
                    # Try to match the value to an option
                    options = await element.evaluate("""el => {
                        return Array.from(el.options).map(o => ({value: o.value, text: o.textContent.trim()}));
                    }""")
                    matched_value = None
                    value_lower = value.lower().strip()
                    for opt in options:
                        if opt["text"].lower().strip() == value_lower:
                            matched_value = opt["value"]
                            break
                    if not matched_value:
                        # Fuzzy: check if value is contained in option text
                        for opt in options:
                            if value_lower in opt["text"].lower() or opt["text"].lower() in value_lower:
                                matched_value = opt["value"]
                                break
                    if matched_value:
                        await element.select_option(value=matched_value)
                        return True
                    # Last resort: select by label text
                    try:
                        await element.select_option(label=value)
                        return True
                    except Exception:
                        pass
                return False

            elif action == "click":
                # For radio buttons and checkboxes
                if value.lower() in ("check", "checked", "true", "yes"):
                    checked = await element.is_checked()
                    if not checked:
                        await element.click()
                    return True
                # For radio: find the option with matching text
                # Get all radios with same name
                name = await element.get_attribute("name")
                if name:
                    radios = await page.query_selector_all(f'input[name="{name}"]')
                    for radio in radios:
                        parent_text = await radio.evaluate("el => el.parentElement?.textContent?.trim() || ''")
                        if value.lower() in parent_text.lower():
                            await radio.click()
                            return True
                return False

            else:
                # type action — clear and type
                await element.click(click_count=3)  # Select all
                await asyncio.sleep(0.1)
                await element.fill(value)
                # Trigger change/input events
                await element.evaluate("el => { el.dispatchEvent(new Event('change', {bubbles: true})); el.dispatchEvent(new Event('input', {bubbles: true})); }")
                return True

        except Exception as e:
            logger.debug(f"Execute fill error for {selector}: {e}")
            return False

    async def quick_scan(self, page) -> Dict[str, Any]:
        """Quick scan — just extract fields and return status. No Gemini calls.

        Useful for checking form state without spending API tokens.
        """
        try:
            fields = await page.evaluate(EXTRACT_FIELDS_JS)
        except Exception as e:
            return {"error": str(e), "fields": [], "empty_count": 0, "required_empty_count": 0}

        empty = [f for f in fields if f.get("empty")]
        required_empty = [f for f in empty if f.get("required")]

        return {
            "total_fields": len(fields),
            "empty_count": len(empty),
            "required_empty_count": len(required_empty),
            "fields": fields,
            "empty_required": [
                {"label": f["label"], "type": f["type"], "selector": f["selector"]}
                for f in required_empty
            ],
        }

    def get_session_log(self) -> List[Dict]:
        """Get all scan results from this session."""
        return self._fill_log
