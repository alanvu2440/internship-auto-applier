"""
Form Filler Engine

Intelligent form filling that maps config values to form fields.
Handles text inputs, dropdowns, checkboxes, radio buttons, and file uploads.
"""

import re
from typing import Any, Dict, List, Optional
from playwright.async_api import Page, ElementHandle
from loguru import logger


class FormFiller:
    """Fills job application forms using config data."""

    # Field name patterns mapped to config keys
    FIELD_MAPPINGS = {
        # Personal Info
        "first_name": [
            r"first.?name", r"given.?name", r"fname", r"name.*first",
        ],
        "last_name": [
            r"last.?name", r"surname", r"family.?name", r"lname", r"name.*last",
        ],
        "full_name": [
            r"full.?name", r"^name$", r"your.?name", r"legal.?name",
        ],
        "email": [
            r"e?-?mail", r"email.?address",
        ],
        "phone": [
            r"phone", r"mobile", r"cell", r"telephone", r"contact.?number",
        ],
        "address": [
            r"street", r"address.?1", r"address.?line", r"^address$",
        ],
        "city": [
            r"^city$", r"town",
        ],
        "state": [
            r"^state$", r"province", r"region",
        ],
        "zip_code": [
            r"zip", r"postal", r"post.?code",
        ],
        "country": [
            r"^country$",
            r"^country\s*of\s*(current\s*)?residence",
            r"country.*residence",
            r"^country\s*\*?$",
        ],

        # Online Profiles
        "linkedin": [
            r"linkedin", r"li.?profile",
        ],
        "github": [
            r"github", r"git.?hub",
        ],
        "portfolio": [
            r"portfolio", r"website", r"personal.?site", r"url",
        ],

        # Education
        "school": [
            r"school", r"university", r"college", r"institution",
        ],
        "degree": [
            r"degree", r"education.?level",
        ],
        "field_of_study": [
            r"major", r"field.?of.?study", r"concentration", r"discipline",
        ],
        "graduation_date": [
            r"graduation", r"grad.?date", r"expected.?grad", r"completion",
        ],
        "gpa": [
            r"^gpa$", r"grade.?point", r"cumulative.?gpa",
        ],

        # Work Authorization
        "us_work_authorized": [
            r"authorized.?to.?work", r"legally.?authorized", r"work.?authorization",
            r"eligible.?to.?work", r"permitted.?to.?work",
        ],
        "require_sponsorship": [
            r"sponsor", r"visa.?sponsor", r"require.*sponsor", r"need.*sponsor",
        ],

        # Demographics
        "gender": [
            r"^gender$", r"sex",
        ],
        "ethnicity": [
            r"ethnic", r"race", r"background",
        ],
        "veteran_status": [
            r"veteran", r"military",
        ],
        "disability_status": [
            r"disab", r"accommodation",
        ],

        # Availability
        "start_date": [
            r"start.?date", r"available.?from", r"availability",
        ],

        # Experience
        "years_of_experience": [
            r"years?.?of.?experience", r"experience.?level", r"how.?many.?years",
        ],

        # Common Questions
        "how_did_you_hear": [
            r"how.?did.?you.?(hear|find|learn)", r"source", r"referral.?source",
        ],
        "salary_expectations": [
            r"salary", r"compensation", r"pay.?expectation", r"desired.?pay",
        ],
    }

    # Boolean field patterns (checkboxes/radio)
    BOOLEAN_MAPPINGS = {
        "is_18_or_older": [r"18.?years", r"over.?18", r"at.?least.?18"],
        "can_pass_background_check": [r"background.?check"],
        "agree_to_terms": [r"terms", r"agree", r"accept", r"consent"],
        "us_citizen": [r"citizen", r"citizenship"],
        "require_sponsorship": [r"sponsor", r"visa"],
    }

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize form filler with user config.

        Args:
            config: Parsed master_config.yaml
        """
        self.config = config
        self._flat_config = self._flatten_config(config)

    def _flatten_config(self, config: Dict, prefix: str = "") -> Dict[str, Any]:
        """Flatten nested config into single-level dict."""
        flat = {}
        for key, value in config.items():
            full_key = f"{prefix}{key}" if prefix else key
            if isinstance(value, dict):
                flat.update(self._flatten_config(value, f"{full_key}."))
            elif isinstance(value, list):
                # Handle lists (e.g., skills)
                if value and isinstance(value[0], dict):
                    # List of dicts - take first item's values
                    flat[full_key] = value
                else:
                    # Simple list - join as comma-separated
                    flat[full_key] = ", ".join(str(v) for v in value)
            else:
                flat[full_key] = value
        return flat

    def get_value_for_field(self, field_name: str, field_label: str = "") -> Optional[Any]:
        """
        Get the config value for a form field.

        Args:
            field_name: HTML name/id attribute
            field_label: Label text associated with field

        Returns:
            Value to fill or None if not found
        """
        search_text = f"{field_name} {field_label}".lower()

        # Special handling for phone - construct full number
        if re.search(r"phone|mobile|cell|telephone", search_text, re.IGNORECASE):
            prefix = self._flat_config.get("personal_info.phone_prefix", "+1")
            number = self._flat_config.get("personal_info.phone", "")
            if number:
                # Clean up the number - remove dashes, spaces
                clean_number = re.sub(r'[\s\-\(\)]', '', number)
                # If number already has country code, use as-is
                if clean_number.startswith('+') or clean_number.startswith('1'):
                    return clean_number
                return f"{prefix}{clean_number}"
            logger.warning("No phone number configured — leaving phone field empty")
            return ""  # Don't fill fake phone numbers

        # State & Country combined field
        if re.search(r"state.*country|state\s*&\s*country", search_text, re.IGNORECASE):
            state = self._flat_config.get("personal_info.state", "California")
            country = self._flat_config.get("personal_info.country", "United States")
            return f"{state}, {country}"

        # Internship field/area question (text input version)
        if re.search(r"what field.*internship|field.*complete.*internship", search_text, re.IGNORECASE):
            return self._flat_config.get("education.field_of_study", "Software Engineering")

        # Try each mapping
        for config_key, patterns in self.FIELD_MAPPINGS.items():
            for pattern in patterns:
                if re.search(pattern, search_text, re.IGNORECASE):
                    # Find value in flattened config - but skip prefix fields
                    for flat_key, value in self._flat_config.items():
                        if config_key in flat_key.lower() and "prefix" not in flat_key.lower():
                            return value

        return None

    def get_boolean_for_field(self, field_name: str, field_label: str = "") -> Optional[bool]:
        """Get boolean value for checkbox/radio field."""
        search_text = f"{field_name} {field_label}".lower()

        for config_key, patterns in self.BOOLEAN_MAPPINGS.items():
            for pattern in patterns:
                if re.search(pattern, search_text, re.IGNORECASE):
                    for flat_key, value in self._flat_config.items():
                        if config_key in flat_key.lower():
                            if isinstance(value, bool):
                                return value
                            return str(value).lower() in ("true", "yes", "1")

        return None

    async def fill_form(self, page: Page) -> Dict[str, Any]:
        """
        Fill all detected form fields on the page.

        Returns:
            Dict of filled fields and their values
        """
        filled = {}
        missed = {}

        # Debug: scan and log all visible inputs on the page
        await self._debug_scan_inputs(page)

        # First, fill critical fields directly by common selectors (Greenhouse specific)
        critical_filled = await self._fill_critical_fields(page)
        filled.update(critical_filled)

        # Fill text inputs
        text_filled = await self._fill_text_inputs(page)
        filled.update(text_filled)

        # Fill standard HTML dropdowns (<select>)
        dropdown_filled = await self._fill_dropdowns(page)
        filled.update(dropdown_filled)

        # Fill custom dropdowns (Greenhouse/React style)
        custom_dropdown_filled = await self._fill_custom_dropdowns(page)
        filled.update(custom_dropdown_filled)

        # Fill checkboxes
        checkbox_filled = await self._fill_checkboxes(page)
        filled.update(checkbox_filled)

        # Fill radio buttons
        radio_filled = await self._fill_radio_buttons(page)
        filled.update(radio_filled)

        # Check for unfilled required fields
        missed = await self._find_unfilled_required_fields(page, filled)

        logger.info(f"Filled {len(filled)} form fields")
        if missed:
            logger.warning(f"Missed {len(missed)} required fields: {list(missed.keys())}")

        # Store filled and missed for reporting
        self._last_fill_result = {
            "filled": filled,
            "missed": missed
        }

        return filled

    async def _fill_critical_fields(self, page: Page) -> Dict[str, str]:
        """Fill critical fields directly using common Greenhouse selectors."""
        filled = {}
        personal = self.config.get("personal_info", {})

        # First, wait for form to be ready (any input visible)
        try:
            await page.wait_for_selector('input:not([type="hidden"])', timeout=5000)
        except Exception:
            logger.warning("No visible inputs found after waiting")
            # Continue anyway in case dynamic loading

        # Direct field mappings for Greenhouse - common name/id patterns
        # These are the most critical fields that often fail with generic detection
        critical_mappings = [
            # First name - many possible selectors
            (
                "first_name",
                [
                    'input[name="first_name"]',
                    'input[name="firstName"]',
                    'input#first_name',
                    'input#firstName',
                    'input[autocomplete="given-name"]',
                    'input[data-qa="first-name"]',
                    # Greenhouse job board specific
                    '#first_name',
                    '[name="job_application[first_name]"]',
                    'input[aria-label*="First Name" i]',
                    'input[aria-label*="First name" i]',
                    'input[placeholder*="First Name" i]',
                    'input[placeholder*="First name" i]',
                    # More flexible - find by nearby label
                    'label:has-text("First Name") + input',
                    'label:has-text("First Name") ~ input',
                    'div:has(> label:has-text("First Name")) input',
                ],
                personal.get("first_name", "")
            ),
            # Last name
            (
                "last_name",
                [
                    'input[name="last_name"]',
                    'input[name="lastName"]',
                    'input#last_name',
                    'input#lastName',
                    'input[autocomplete="family-name"]',
                    'input[data-qa="last-name"]',
                    '#last_name',
                    '[name="job_application[last_name]"]',
                    'input[aria-label*="Last Name" i]',
                    'input[aria-label*="Last name" i]',
                    'input[placeholder*="Last Name" i]',
                    'input[placeholder*="Last name" i]',
                    'label:has-text("Last Name") + input',
                    'label:has-text("Last Name") ~ input',
                    'div:has(> label:has-text("Last Name")) input',
                ],
                personal.get("last_name", "")
            ),
            # Email
            (
                "email",
                [
                    'input[name="email"]',
                    'input[type="email"]',
                    'input#email',
                    'input[autocomplete="email"]',
                    'input[data-qa="email"]',
                    '[name="job_application[email]"]',
                    'input[aria-label*="Email" i]',
                    'input[placeholder*="Email" i]',
                    'label:has-text("Email") + input',
                    'label:has-text("Email") ~ input',
                ],
                personal.get("email", "")
            ),
            # Phone
            (
                "phone",
                [
                    'input[name="phone"]',
                    'input[name="phone_number"]',
                    'input[type="tel"]',
                    'input#phone',
                    'input[autocomplete="tel"]',
                    'input[data-qa="phone"]',
                    '[name="job_application[phone]"]',
                    'input[aria-label*="Phone" i]',
                    'input[placeholder*="Phone" i]',
                    'label:has-text("Phone") + input',
                    'label:has-text("Phone") ~ input',
                ],
                self._get_full_phone()
            ),
            # Preferred name (same as first name for most people)
            (
                "preferred_name",
                [
                    'input[name="preferred_name"]',
                    'input[name="preferredName"]',
                    'input#preferred_name',
                    'input#preferredName',
                    'input[aria-label*="Preferred name" i]',
                    'input[aria-label*="Preferred Name" i]',
                    'input[placeholder*="Preferred name" i]',
                    'input[placeholder*="Preferred Name" i]',
                    'label:has-text("Preferred name") ~ input',
                    'label:has-text("Preferred Name") ~ input',
                ],
                personal.get("first_name", "")  # Use first name as preferred name
            ),
            # Country
            (
                "country",
                [
                    'input#country',
                    'input[name="country"]',
                    'input[autocomplete="country"]',
                    'input[aria-label*="Country" i]',
                ],
                personal.get("country", "United States")
            ),
            # Location/City - handled separately due to autocomplete
            # (
            #     "location",
            #     [
            #         'input#candidate-location',
            #         ...
            #     ],
            #     personal.get("city", "") + ", " + personal.get("state", "") if personal.get("city") else ""
            # ),
        ]

        for field_name, selectors, value in critical_mappings:
            if not value:
                logger.debug(f"No value configured for {field_name}, skipping")
                continue

            field_filled = False
            for selector in selectors:
                try:
                    element = await page.query_selector(selector)
                    if element and await element.is_visible():
                        # Check if already filled
                        current_value = await element.input_value()
                        if current_value and len(current_value.strip()) > 0:
                            logger.debug(f"Field {field_name} ({selector}) already filled with '{current_value}'")
                            filled[field_name] = current_value
                            field_filled = True
                            break

                        await element.click()
                        await element.fill(str(value))
                        filled[field_name] = value
                        logger.info(f"Filled critical field {field_name} = '{value}'")
                        field_filled = True
                        break
                except Exception as e:
                    logger.debug(f"Could not fill {field_name} via {selector}: {e}")

            if not field_filled:
                logger.warning(f"Could not fill critical field: {field_name}")

        # Handle location separately - it's a typeahead field that requires dropdown selection
        location_value = personal.get("city", "")
        if personal.get("state"):
            location_value = f"{location_value}, {personal.get('state')}" if location_value else personal.get("state", "")

        if location_value and "location" not in filled:
            location_selectors = [
                # Greenhouse geosuggest (most common pattern)
                '.geosuggest input',
                '.geosuggest__input',
                'input[class*="geosuggest"]',
                'input[id*="location"][class*="geosuggest"]',
                # Greenhouse-specific
                'input#candidate-location',
                'input[name="candidate-location"]',
                '#candidate-location input',  # Wrapped in div
                '[data-qa="candidate-location"] input',
                'input#job_application_location',
                # By field class/container
                '.field--location input',
                '[data-field="location"] input',
                '.candidate-location input',
                # Generic location selectors
                'input[name="location"]',
                'input[name="city"]',
                'input[autocomplete="address-level2"]',
                'input[aria-label*="Location" i]',
                'input[aria-label*="City" i]',
                'input[placeholder*="Location" i]',
                'input[placeholder*="City" i]',
                'input[placeholder*="Enter a location" i]',
                'input[placeholder*="Enter your location" i]',
                'input[placeholder*="Enter location" i]',
                # Label-based selectors for Greenhouse
                'label:has-text("Location") ~ input',
                'label:has-text("City") ~ input',
                'div:has(> label:has-text("Location")) input',
            ]
            success = await self._fill_typeahead_field(page, filled, "location", location_selectors, location_value)

            # If typeahead didn't work, try finding by label and just typing the value
            if not success:
                await self._fill_location_by_label(page, filled, location_value)

        # Also try filling by finding inputs near labels (Greenhouse-specific approach)
        if "first_name" not in filled or "last_name" not in filled:
            await self._fill_by_label_proximity(page, filled, personal)

        return filled

    async def _fill_location_by_label(self, page: Page, filled: Dict, location_value: str) -> bool:
        """Fallback: fill location by finding input near Location/City label."""
        try:
            labels = await page.query_selector_all('label')
            for label in labels:
                label_text = (await label.text_content() or "").strip().lower()
                if "location" in label_text or "city" in label_text:
                    # Get the for attribute or find nearby input
                    for_attr = await label.get_attribute("for")
                    if for_attr:
                        input_elem = await page.query_selector(f'#{for_attr}')
                    else:
                        # Look for input as sibling or in parent
                        input_elem = await label.evaluate_handle('''(label) => {
                            let sibling = label.nextElementSibling;
                            if (sibling && sibling.tagName === 'INPUT') return sibling;
                            let parent = label.parentElement;
                            if (parent) {
                                let input = parent.querySelector('input:not([type="hidden"])');
                                if (input) return input;
                            }
                            let grandparent = parent?.parentElement;
                            if (grandparent) {
                                let input = grandparent.querySelector('input:not([type="hidden"])');
                                if (input) return input;
                            }
                            return null;
                        }''')
                        input_elem = input_elem.as_element() if input_elem else None

                    if input_elem and await input_elem.is_visible():
                        current = await input_elem.input_value()
                        if not current or not current.strip():
                            # Just type the city name
                            city = location_value.split(",")[0].strip()
                            await input_elem.click()
                            await input_elem.fill(city)
                            # Try to select from autocomplete
                            await page.wait_for_timeout(800)
                            await page.keyboard.press("ArrowDown")
                            await page.wait_for_timeout(200)
                            await page.keyboard.press("Enter")
                            await page.wait_for_timeout(300)

                            new_value = await input_elem.input_value()
                            filled["location"] = new_value if new_value else city
                            logger.info(f"Filled location via label fallback = '{filled['location']}'")
                            return True
        except Exception as e:
            logger.debug(f"Location label fallback failed: {e}")
        return False

    async def _fill_by_label_proximity(self, page: Page, filled: Dict, personal: Dict) -> None:
        """Fill fields by finding inputs near their labels (Greenhouse fallback)."""
        label_mappings = [
            ("First Name", "first_name", personal.get("first_name", "")),
            ("Last Name", "last_name", personal.get("last_name", "")),
            ("Email", "email", personal.get("email", "")),
            ("Phone", "phone", self._get_full_phone()),
        ]

        for label_text, field_name, value in label_mappings:
            if field_name in filled or not value:
                continue

            try:
                # Find label containing the text
                labels = await page.query_selector_all(f'label')
                for label in labels:
                    label_content = (await label.text_content() or "").strip()
                    if label_text.lower() in label_content.lower():
                        # Get the for attribute or find nearby input
                        for_attr = await label.get_attribute("for")
                        if for_attr:
                            input_elem = await page.query_selector(f'#{for_attr}')
                        else:
                            # Look for input as sibling or in parent
                            input_elem = await label.evaluate_handle('''(label) => {
                                // Check next sibling
                                let sibling = label.nextElementSibling;
                                if (sibling && sibling.tagName === 'INPUT') return sibling;

                                // Check parent for input
                                let parent = label.parentElement;
                                if (parent) {
                                    let input = parent.querySelector('input:not([type="hidden"])');
                                    if (input) return input;
                                }

                                // Check grandparent
                                let grandparent = parent?.parentElement;
                                if (grandparent) {
                                    let input = grandparent.querySelector('input:not([type="hidden"])');
                                    if (input) return input;
                                }

                                return null;
                            }''')
                            input_elem = input_elem.as_element() if input_elem else None

                        if input_elem and await input_elem.is_visible():
                            current = await input_elem.input_value()
                            if not current or not current.strip():
                                await input_elem.click()
                                await input_elem.fill(str(value))
                                filled[field_name] = value
                                logger.info(f"Filled {field_name} via label proximity = '{value}'")
                                break

            except Exception as e:
                logger.debug(f"Label proximity fill failed for {field_name}: {e}")

        # Ultra-aggressive fallback: scan ALL visible text inputs and identify by context
        if "first_name" not in filled or "last_name" not in filled or "email" not in filled:
            await self._fill_by_context_scanning(page, filled, personal)

    async def _fill_by_context_scanning(self, page: Page, filled: Dict, personal: Dict) -> None:
        """Ultra-aggressive fallback: find inputs by scanning all visible inputs and checking context."""
        try:
            # Get all visible text inputs
            all_inputs = await page.query_selector_all('input[type="text"], input[type="email"], input:not([type])')
            visible_inputs = []

            for inp in all_inputs:
                try:
                    if await inp.is_visible():
                        visible_inputs.append(inp)
                except Exception:
                    continue

            logger.debug(f"Context scanning found {len(visible_inputs)} visible text inputs")

            for inp in visible_inputs:
                try:
                    # Skip if already has value
                    current = await inp.input_value()
                    if current and len(current.strip()) > 0:
                        continue

                    # Get all identifying info about this input
                    inp_id = (await inp.get_attribute("id") or "").lower()
                    inp_name = (await inp.get_attribute("name") or "").lower()
                    inp_placeholder = (await inp.get_attribute("placeholder") or "").lower()
                    inp_aria = (await inp.get_attribute("aria-label") or "").lower()
                    inp_autocomplete = (await inp.get_attribute("autocomplete") or "").lower()

                    # Get text from nearby elements (label, parent text content)
                    context_text = await inp.evaluate('''(el) => {
                        let text = "";
                        // Check for label with for attribute
                        if (el.id) {
                            let label = document.querySelector('label[for="' + el.id + '"]');
                            if (label) text += " " + label.textContent;
                        }
                        // Check parent and siblings
                        let parent = el.parentElement;
                        for (let i = 0; i < 3 && parent; i++) {
                            let labels = parent.querySelectorAll('label, span, div');
                            labels.forEach(l => {
                                if (l.textContent && l.textContent.length < 100) {
                                    text += " " + l.textContent;
                                }
                            });
                            parent = parent.parentElement;
                        }
                        return text.toLowerCase();
                    }''')

                    # Combine all context
                    all_context = f"{inp_id} {inp_name} {inp_placeholder} {inp_aria} {inp_autocomplete} {context_text}"

                    # Identify field type
                    if "first_name" not in filled:
                        if any(x in all_context for x in ["first name", "first_name", "firstname", "given name", "given-name"]):
                            await inp.fill(personal.get("first_name", ""))
                            filled["first_name"] = personal.get("first_name", "")
                            logger.info(f"Context scan filled first_name = '{personal.get('first_name')}'")
                            continue

                    if "last_name" not in filled:
                        if any(x in all_context for x in ["last name", "last_name", "lastname", "family name", "family-name", "surname"]):
                            await inp.fill(personal.get("last_name", ""))
                            filled["last_name"] = personal.get("last_name", "")
                            logger.info(f"Context scan filled last_name = '{personal.get('last_name')}'")
                            continue

                    if "email" not in filled:
                        if any(x in all_context for x in ["email", "e-mail", "mail"]):
                            await inp.fill(personal.get("email", ""))
                            filled["email"] = personal.get("email", "")
                            logger.info(f"Context scan filled email = '{personal.get('email')}'")
                            continue

                    if "phone" not in filled:
                        if any(x in all_context for x in ["phone", "mobile", "cell", "tel"]):
                            await inp.fill(self._get_full_phone())
                            filled["phone"] = self._get_full_phone()
                            logger.info(f"Context scan filled phone")
                            continue

                except Exception as e:
                    logger.debug(f"Context scan error for input: {e}")

        except Exception as e:
            logger.debug(f"Context scanning failed: {e}")

    def _get_full_phone(self) -> str:
        """Get full phone number with prefix."""
        prefix = self._flat_config.get("personal_info.phone_prefix", "+1")
        number = self._flat_config.get("personal_info.phone", "")
        if number:
            clean_number = re.sub(r'[\s\-\(\)]', '', number)
            if clean_number.startswith('+') or clean_number.startswith('1'):
                return clean_number
            return f"{prefix}{clean_number}"
        return ""

    async def _fill_typeahead_field(self, page: Page, filled: Dict, field_name: str, selectors: List[str], value: str) -> bool:
        """Fill a typeahead/autocomplete field by typing and selecting from dropdown."""
        for selector in selectors:
            try:
                element = await page.query_selector(selector)
                if not element or not await element.is_visible():
                    continue

                logger.debug(f"Found typeahead field {field_name} with selector {selector}")

                # Check if already filled with a valid value
                current_value = await element.input_value()
                if current_value and len(current_value.strip()) > 2:
                    logger.debug(f"Typeahead {field_name} already has value: '{current_value}'")
                    filled[field_name] = current_value
                    return True

                # Click to focus
                await element.click()
                await page.wait_for_timeout(300)

                # Clear existing value with keyboard (more reliable than fill(""))
                await page.keyboard.press("Control+a")
                await page.keyboard.press("Backspace")
                await page.wait_for_timeout(200)

                # For location, search for just the city (more likely to match)
                search_term = value.split(",")[0].strip() if "," in value else value
                logger.debug(f"Typing '{search_term}' into typeahead field {field_name}")

                # Type slowly to trigger autocomplete
                await element.type(search_term, delay=80)
                await page.wait_for_timeout(1200)  # Longer wait for API call to complete

                # Greenhouse location autocomplete uses various structures
                # Try multiple approaches to find and click suggestions
                autocomplete_selectors = [
                    # Greenhouse-specific patterns
                    '[class*="geosuggest__suggests"] [class*="geosuggest__item"]',
                    '[class*="location-autocomplete"] li',
                    '[class*="autocomplete-dropdown"] [class*="item"]',
                    '[class*="typeahead"] [class*="option"]',
                    '.geosuggest__suggests .geosuggest__item',
                    '.location-suggestions li',
                    # React-select style (used by some Greenhouse forms)
                    '.select__menu [class*="option"]',
                    '[class*="menu-list"] [class*="option"]',
                    # Generic patterns
                    '[role="listbox"] [role="option"]',
                    '[class*="suggestion"]',
                    '[class*="autocomplete"] [class*="option"]',
                    '[class*="dropdown"] li:not([class*="no-results"])',
                    '.pac-container .pac-item',  # Google Places
                    'ul[class*="result"] li',
                    'div[class*="results"] div[class*="result"]',
                ]

                option_found = False
                for ac_selector in autocomplete_selectors:
                    try:
                        options = await page.query_selector_all(ac_selector)
                        visible_options = []
                        for opt in options:
                            if await opt.is_visible():
                                visible_options.append(opt)

                        if visible_options:
                            logger.debug(f"Found {len(visible_options)} visible autocomplete options with {ac_selector}")

                            # Collect option texts and find best match index
                            option_texts = []
                            best_match_index = -1
                            for i, opt in enumerate(visible_options):
                                opt_text = (await opt.text_content() or "").strip()
                                option_texts.append(opt_text)
                                logger.debug(f"  Autocomplete option [{i}]: '{opt_text}'")

                            # Find best match - prefer exact country match for locations
                            for i, opt_text in enumerate(option_texts):
                                if not opt_text:
                                    continue
                                opt_lower = opt_text.lower()
                                # For locations: prefer US/country match
                                if search_term.lower() in opt_lower:
                                    if best_match_index == -1:
                                        best_match_index = i
                                    # Prefer "United States" or country-specific match
                                    if "united states" in opt_lower or "usa" in opt_lower or ", ca" in opt_lower:
                                        best_match_index = i
                                        break

                            if best_match_index == -1 and option_texts:
                                best_match_index = 0  # Fall back to first option

                            if best_match_index >= 0:
                                # Use keyboard to navigate to the right option
                                # First option is already highlighted, use ArrowDown to reach target
                                for _ in range(best_match_index):
                                    await page.keyboard.press("ArrowDown")
                                    await page.wait_for_timeout(100)

                                # Press Enter to select (more reliable than clicking)
                                await page.keyboard.press("Enter")
                                await page.wait_for_timeout(500)

                                # Verify the field got filled
                                new_value = await element.input_value()
                                if new_value and len(new_value.strip()) > len(search_term) // 2:
                                    logger.info(f"Selected typeahead option via keyboard: '{option_texts[best_match_index]}' -> '{new_value}'")
                                    filled[field_name] = new_value
                                    return True

                            option_found = True
                            break
                    except Exception as e:
                        logger.debug(f"Autocomplete selector {ac_selector} failed: {e}")

                # If no dropdown options found, try keyboard navigation
                if not option_found:
                    logger.debug(f"No autocomplete dropdown found, trying keyboard selection")

                    # Wait a bit more for lazy-loaded suggestions (API calls can be slow)
                    await page.wait_for_timeout(800)

                    # Try ArrowDown to select first suggestion
                    await page.keyboard.press("ArrowDown")
                    await page.wait_for_timeout(300)
                    await page.keyboard.press("Enter")
                    await page.wait_for_timeout(500)

                    # Check if value changed
                    new_value = await element.input_value()
                    if new_value and new_value != search_term and len(new_value.strip()) > 2:
                        logger.info(f"Filled typeahead {field_name} via keyboard = '{new_value}'")
                        filled[field_name] = new_value
                        return True

                # Last resort: just accept the typed value
                # Some forms may accept free-form text without autocomplete
                final_value = await element.input_value()
                if final_value and len(final_value.strip()) > 2:
                    filled[field_name] = final_value
                    logger.info(f"Typeahead {field_name} filled with typed value: '{final_value}'")
                    # Click elsewhere to deselect and trigger any validation
                    await page.click('body', position={'x': 10, 'y': 10})
                    await page.wait_for_timeout(200)
                    return True

            except Exception as e:
                logger.debug(f"Typeahead selector {selector} failed: {e}")

        # Extra fallback: try to find location field by scanning all inputs
        if field_name == "location":
            await self._fill_location_by_scanning(page, filled, value)
            if "location" in filled:
                return True

        logger.warning(f"Could not fill typeahead field: {field_name}")
        return False

    async def _fill_location_by_scanning(self, page: Page, filled: Dict, value: str) -> None:
        """Fallback: find location input by scanning all inputs and checking context."""
        try:
            all_inputs = await page.query_selector_all('input[type="text"], input:not([type])')

            for inp in all_inputs:
                try:
                    if not await inp.is_visible():
                        continue

                    # Get context
                    inp_id = (await inp.get_attribute("id") or "").lower()
                    inp_name = (await inp.get_attribute("name") or "").lower()
                    inp_placeholder = (await inp.get_attribute("placeholder") or "").lower()

                    # Get label text
                    context_text = await inp.evaluate('''(el) => {
                        let text = "";
                        if (el.id) {
                            let label = document.querySelector('label[for="' + el.id + '"]');
                            if (label) text += " " + label.textContent;
                        }
                        let parent = el.parentElement;
                        for (let i = 0; i < 3 && parent; i++) {
                            text += " " + (parent.textContent || "").slice(0, 200);
                            parent = parent.parentElement;
                        }
                        return text.toLowerCase();
                    }''')

                    all_context = f"{inp_id} {inp_name} {inp_placeholder} {context_text}"

                    # Check if this looks like a location field
                    if any(x in all_context for x in ["location", "city", "address", "where are you", "candidate-location"]):
                        current = await inp.input_value()
                        if current and len(current.strip()) > 2:
                            filled["location"] = current
                            return

                        # Fill with just the city part
                        city = value.split(",")[0].strip() if "," in value else value
                        await inp.click()
                        await inp.fill(city)

                        # Try to trigger autocomplete and select best option
                        await page.wait_for_timeout(1000)

                        # Check for autocomplete options and pick the best one
                        ac_selectors = [
                            '.select__menu [class*="option"]',
                            '[class*="autocomplete"] [class*="option"]',
                            '[role="listbox"] [role="option"]',
                            '[class*="suggestion"]',
                        ]
                        best_found = False
                        state = self.config.get("personal_info", {}).get("state", "CA")
                        for ac_sel in ac_selectors:
                            options = await page.query_selector_all(ac_sel)
                            visible_opts = []
                            for opt in options:
                                if await opt.is_visible():
                                    opt_text = (await opt.text_content() or "").strip().lower()
                                    if opt_text:
                                        visible_opts.append((opt, opt_text))

                            if visible_opts:
                                # Prefer US/state match
                                for opt, opt_text in visible_opts:
                                    if "united states" in opt_text or "usa" in opt_text or (state and f", {state.lower()}" in opt_text.lower()):
                                        await opt.click()
                                        best_found = True
                                        break
                                if not best_found and visible_opts:
                                    # Click first option
                                    await visible_opts[0][0].click()
                                    best_found = True
                                break

                        if not best_found:
                            # Fallback: keyboard Enter on first option
                            await page.keyboard.press("Enter")

                        await page.wait_for_timeout(300)

                        new_value = await inp.input_value()
                        filled["location"] = new_value if new_value else city
                        logger.info(f"Location field filled via scanning: '{filled['location']}'")
                        return

                except Exception:
                    continue

        except Exception as e:
            logger.debug(f"Location scanning failed: {e}")

    async def _find_unfilled_required_fields(self, page: Page, filled: Dict) -> Dict[str, str]:
        """Find required fields that weren't filled."""
        missed = {}

        # Look for required fields with error states or empty values
        required_selectors = [
            'input[required]:not([type="hidden"])',
            'input[aria-required="true"]:not([type="hidden"])',
            'textarea[required]',
            'select[required]',
            '[class*="required"] input:not([type="hidden"])',
            '[class*="required"] textarea',
            '[class*="required"] select',
        ]

        for selector in required_selectors:
            try:
                elements = await page.query_selector_all(selector)
                for element in elements:
                    if not await element.is_visible():
                        continue

                    name = await element.get_attribute("name") or ""
                    id_attr = await element.get_attribute("id") or ""
                    field_id = name or id_attr

                    # Skip if we already filled it
                    if any(field_id in str(k) for k in filled.keys()):
                        continue

                    # Check if empty
                    tag = await element.evaluate("el => el.tagName.toLowerCase()")
                    if tag == "select":
                        value = await element.input_value()
                        if not value or value == "":
                            label = await self._get_label_for_element(page, element, id_attr)
                            missed[field_id or label] = "dropdown (empty)"
                    else:
                        value = await element.input_value()
                        if not value or value.strip() == "":
                            label = await self._get_label_for_element(page, element, id_attr)
                            missed[field_id or label] = f"text input (empty) - label: {label}"

            except Exception as e:
                logger.debug(f"Error checking required fields: {e}")

        return missed

    def get_last_fill_result(self) -> Dict[str, Any]:
        """Get the result of the last fill_form call."""
        return getattr(self, '_last_fill_result', {"filled": {}, "missed": {}})

    async def _debug_scan_inputs(self, page: Page) -> None:
        """Debug: scan and log all visible inputs on the page."""
        try:
            inputs = await page.query_selector_all('input:not([type="hidden"]), textarea, select')
            logger.debug(f"Found {len(inputs)} input elements on page")

            for i, inp in enumerate(inputs[:20]):  # Log first 20
                try:
                    if not await inp.is_visible():
                        continue

                    tag = await inp.evaluate("el => el.tagName")
                    name = await inp.get_attribute("name") or ""
                    id_attr = await inp.get_attribute("id") or ""
                    inp_type = await inp.get_attribute("type") or ""
                    placeholder = await inp.get_attribute("placeholder") or ""
                    value = await inp.input_value() if tag.lower() != "select" else ""

                    logger.debug(f"  [{i}] {tag} name='{name}' id='{id_attr}' type='{inp_type}' placeholder='{placeholder}' value='{value[:20] if value else ''}'")
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"Debug scan failed: {e}")

    async def _fill_text_inputs(self, page: Page) -> Dict[str, str]:
        """Fill all text input fields."""
        filled = {}

        # Find all text-like inputs
        selectors = [
            'input[type="text"]',
            'input[type="email"]',
            'input[type="tel"]',
            'input[type="url"]',
            'input[type="number"]',
            'input:not([type])',
            'textarea',
        ]

        for selector in selectors:
            elements = await page.query_selector_all(selector)
            for element in elements:
                try:
                    # Skip hidden/disabled fields
                    if not await element.is_visible():
                        continue
                    if await element.is_disabled():
                        continue

                    # Get field identifiers
                    name = await element.get_attribute("name") or ""
                    id_attr = await element.get_attribute("id") or ""
                    placeholder = await element.get_attribute("placeholder") or ""

                    # Try to find associated label
                    label = await self._get_label_for_element(page, element, id_attr)

                    # Get value from config
                    value = self.get_value_for_field(
                        f"{name} {id_attr} {placeholder}",
                        label
                    )

                    if value:
                        # Check if already filled (e.g. by Simplify extension) — don't override
                        current_value = await element.input_value()
                        if current_value and len(current_value.strip()) > 2:
                            logger.debug(f"Text field '{name or id_attr}' already filled with '{current_value}' — preserving")
                            filled[name or id_attr or placeholder] = current_value
                            continue

                        # Only fill if empty
                        await element.click()
                        await element.fill("")
                        await element.fill(str(value))
                        filled[name or id_attr or placeholder] = value
                        logger.debug(f"Filled text field '{name or id_attr}' with '{value}'")

                except Exception as e:
                    logger.debug(f"Error filling text input: {e}")

        return filled

    async def _fill_dropdowns(self, page: Page) -> Dict[str, str]:
        """Fill dropdown/select fields."""
        filled = {}

        elements = await page.query_selector_all("select")
        for element in elements:
            try:
                if not await element.is_visible():
                    continue

                name = await element.get_attribute("name") or ""
                id_attr = await element.get_attribute("id") or ""
                label = await self._get_label_for_element(page, element, id_attr)

                value = self.get_value_for_field(f"{name} {id_attr}", label)
                if value:
                    # Check if already selected (e.g. by Simplify) — don't override
                    current_val = await element.input_value()
                    if current_val and current_val.strip() and current_val != "0" and current_val != "":
                        logger.debug(f"Dropdown '{name or id_attr}' already has value '{current_val}' — preserving")
                        filled[name or id_attr] = current_val
                        continue

                    selected = False

                    # Get all options first for quick matching
                    options = await element.query_selector_all("option")
                    option_map = {}
                    for option in options:
                        option_text = (await option.text_content() or "").strip()
                        option_value = await option.get_attribute("value") or ""
                        option_map[option_text.lower()] = option_value
                        option_map[option_value.lower()] = option_value

                    # Try exact match first
                    search_val = str(value).lower()
                    if search_val in option_map:
                        await element.select_option(value=option_map[search_val], timeout=2000)
                        selected = True
                    else:
                        # Try partial match
                        for opt_text, opt_val in option_map.items():
                            if search_val in opt_text or opt_text in search_val:
                                await element.select_option(value=opt_val, timeout=2000)
                                selected = True
                                break

                    if selected:
                        filled[name or id_attr] = value
                        logger.debug(f"Filled dropdown '{name or id_attr}' with '{value}'")

            except Exception as e:
                logger.debug(f"Error filling dropdown: {e}")

        return filled

    async def _fill_custom_dropdowns(self, page: Page) -> Dict[str, str]:
        """Fill custom dropdown components (Greenhouse/React style)."""
        filled = {}

        # Also find by looking for aria attributes
        dropdown_containers = await page.query_selector_all('[aria-haspopup="listbox"], [role="combobox"]')

        for container in dropdown_containers:
            try:
                if not await container.is_visible():
                    continue

                # Get the label for this dropdown using JavaScript evaluation
                label_text = await container.evaluate('''(el) => {
                    // Go up to find the field container
                    let container = el.closest('div[class*="field"], div[class*="question"], label');
                    if (!container) container = el.parentElement?.parentElement?.parentElement;
                    if (!container) return "";

                    // Find label within container
                    let label = container.querySelector("label, span[class*='label'], div[class*='label']");
                    return label ? label.textContent.trim() : "";
                }''')

                if not label_text:
                    # Try aria-label
                    label_text = await container.get_attribute("aria-label") or ""

                # Determine what value we should select
                value = self._get_dropdown_value_for_label(label_text)
                if not value:
                    continue

                logger.debug(f"Attempting custom dropdown '{label_text}' with value '{value}'")

                # Click to open the dropdown
                await container.click()
                await page.wait_for_timeout(800)  # Increased wait time

                # Find and click the matching option
                option_selected = await self._select_dropdown_option(page, value)

                if option_selected:
                    filled[label_text or "custom_dropdown"] = value
                    logger.debug(f"Selected custom dropdown '{label_text}' = '{value}'")
                else:
                    # Click elsewhere to close dropdown
                    await page.keyboard.press("Escape")

            except Exception as e:
                logger.debug(f"Error filling custom dropdown: {e}")
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass

        # Also handle Greenhouse-specific question dropdowns by looking at field structure
        await self._fill_greenhouse_dropdowns(page, filled)

        # FINAL PASS: Catch any remaining unfilled dropdowns
        await self._fill_all_remaining_dropdowns(page, filled)

        return filled

    async def _fill_all_remaining_dropdowns(self, page: Page, filled: Dict) -> None:
        """
        Final pass to catch ANY unfilled dropdown on the page.
        This includes React-Select, standard selects, and custom dropdowns.
        """
        try:
            # Find all dropdown-like elements that might still be unfilled
            all_dropdowns = await page.evaluate('''() => {
                const dropdowns = [];

                // 1. Standard HTML selects
                document.querySelectorAll('select').forEach((el, i) => {
                    if (el.offsetParent !== null) { // visible
                        const label = el.closest('.field, .question, label, [class*="field"]')
                            ?.querySelector('label, [class*="label"]')?.textContent?.trim() || '';
                        const value = el.value;
                        const hasPlaceholder = !value || value === '' || el.options[el.selectedIndex]?.text?.toLowerCase().includes('select');
                        if (hasPlaceholder || !value) {
                            dropdowns.push({
                                type: 'select',
                                id: el.id || el.name || `select_${i}`,
                                label: label,
                                selector: el.id ? `#${el.id}` : `select[name="${el.name}"]`
                            });
                        }
                    }
                });

                // 2. React-Select dropdowns that show placeholder
                document.querySelectorAll('.select__control, [class*="select__control"]').forEach((el, i) => {
                    if (el.offsetParent !== null) {
                        const text = el.textContent?.trim()?.toLowerCase() || '';
                        const hasPlaceholder = text.includes('select') || text.includes('choose') || text.length < 3;
                        if (hasPlaceholder) {
                            const label = el.closest('.field, .question, [class*="field"]')
                                ?.querySelector('label, [class*="label"]')?.textContent?.trim() || '';
                            dropdowns.push({
                                type: 'react-select',
                                id: `react_select_${i}`,
                                label: label,
                                index: i
                            });
                        }
                    }
                });

                return dropdowns;
            }''')

            logger.info(f"Final pass: found {len(all_dropdowns)} potentially unfilled dropdowns")

            for dd in all_dropdowns:
                try:
                    label = dd.get('label', '')
                    dd_type = dd.get('type', '')
                    dd_id = dd.get('id', '')

                    # Skip if already filled
                    if label in filled or dd_id in filled:
                        continue

                    # Get value for this dropdown
                    value = self._get_dropdown_value_for_label(label)
                    if not value:
                        # Try AI answerer fallback
                        value = self._get_ai_answer_for_dropdown(label)

                    if not value:
                        logger.debug(f"No value found for unfilled dropdown: {label}")
                        continue

                    logger.info(f"Final pass filling {dd_type} dropdown '{label}' with '{value}'")

                    if dd_type == 'select':
                        # Standard HTML select
                        selector = dd.get('selector', '')
                        if selector:
                            select_elem = await page.query_selector(selector)
                            if select_elem:
                                await self._fill_standard_select(page, select_elem, value, label, filled)

                    elif dd_type == 'react-select':
                        # React-Select
                        index = dd.get('index', 0)
                        react_selects = await page.query_selector_all('.select__control, [class*="select__control"]')
                        if index < len(react_selects):
                            await self._fill_react_select_by_element(page, react_selects[index], value, label, filled)

                except Exception as e:
                    logger.debug(f"Error in final pass for dropdown: {e}")

        except Exception as e:
            logger.debug(f"Error in _fill_all_remaining_dropdowns: {e}")

    def _get_ai_answer_for_dropdown(self, label: str) -> Optional[str]:
        """Get a simple answer for common dropdown questions."""
        label_lower = label.lower()

        # How did you hear about us/this job
        if any(x in label_lower for x in ["hear", "source", "referral", "find out", "learn about", "discover"]):
            return self._flat_config.get("common_answers.how_did_you_hear", "LinkedIn")

        # Yes/No questions with common patterns
        yes_patterns = [
            "authorized", "eligible", "legally", "willing", "able", "comfortable",
            "relocate", "background check", "drug test", "consent", "agree",
            "18", "21", "legal age", "unrestricted", "right to work",
            "onsite", "office", "hybrid", "in-person", "in person"
        ]
        no_patterns = [
            "sponsor", "visa", "current employee", "previously employed",
            "relative", "family", "convicted", "felony", "criminal"
        ]

        for pattern in yes_patterns:
            if pattern in label_lower:
                return "Yes"
        for pattern in no_patterns:
            if pattern in label_lower:
                return "No"

        # Demographics
        if any(x in label_lower for x in ["gender", "race", "ethnic", "disability", "transgender", "lgbtq"]):
            return "Prefer not to say"
        if "veteran" in label_lower:
            return "No"

        # Low-code automation platforms (Zapier, Make, Workato, etc.)
        if any(x in label_lower for x in ["zapier", "workato", "power automate", "make.com", "low-code", "low code", "no-code", "automation platform"]):
            return "No"

        # Built/deployed project with Python/JavaScript + AI/APIs
        if ("built" in label_lower or "deployed" in label_lower) and ("project" in label_lower or "app" in label_lower) and any(x in label_lower for x in ["python", "javascript", "ai model", "integrat"]):
            return "Yes"

        # GPA threshold questions (e.g. "Is your GPA 3.0 or higher?")
        import re as _re
        gpa_match = _re.search(r"gpa.*?(\d+\.\d+)\s*or\s*higher", label_lower)
        if gpa_match:
            threshold = float(gpa_match.group(1))
            actual_gpa = float(self._flat_config.get("education.gpa", "3.6"))
            return "Yes" if actual_gpa >= threshold else "No"

        return None

    async def _fill_standard_select(self, page: Page, select_elem, value: str, label: str, filled: Dict) -> None:
        """Fill a standard HTML select element."""
        try:
            options = await select_elem.query_selector_all("option")
            value_lower = value.lower()

            for opt in options:
                opt_text = (await opt.text_content() or "").strip()
                opt_value = await opt.get_attribute("value") or ""
                opt_lower = opt_text.lower()

                # Match logic
                if value_lower == opt_lower or value_lower in opt_lower or opt_lower in value_lower:
                    await select_elem.select_option(value=opt_value if opt_value else opt_text)
                    filled[label] = value
                    logger.info(f"Filled standard select '{label}' = '{opt_text}'")
                    return

                # LinkedIn matching
                if "linkedin" in value_lower and any(x in opt_lower for x in ["linkedin", "social", "online", "job board"]):
                    await select_elem.select_option(value=opt_value if opt_value else opt_text)
                    filled[label] = opt_text
                    logger.info(f"Filled standard select '{label}' = '{opt_text}' (matched LinkedIn)")
                    return

                # Yes/No matching
                if value_lower == "yes" and "yes" in opt_lower:
                    await select_elem.select_option(value=opt_value if opt_value else opt_text)
                    filled[label] = opt_text
                    return
                if value_lower == "no" and "no" in opt_lower:
                    await select_elem.select_option(value=opt_value if opt_value else opt_text)
                    filled[label] = opt_text
                    return

        except Exception as e:
            logger.debug(f"Error filling standard select: {e}")

    async def _fill_react_select_by_element(self, page: Page, react_select, value: str, label: str, filled: Dict) -> None:
        """Fill a React-Select dropdown by element reference."""
        try:
            # Click to open
            await react_select.click()
            await page.wait_for_timeout(600)

            # Find and click option
            if await self._select_dropdown_option(page, value):
                filled[label] = value
                logger.info(f"Filled React-Select '{label}' = '{value}'")
            else:
                await page.keyboard.press("Escape")

        except Exception as e:
            logger.debug(f"Error filling React-Select: {e}")
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass

    def _get_dropdown_value_for_label(self, label: str) -> Optional[str]:
        """Determine what value to select based on dropdown label."""
        label_lower = label.lower()

        # Special case: "authorized...without...sponsorship" is an authorization question, not sponsorship
        # e.g., "Are you legally authorized to work without employer support or sponsorship?"
        # Answer: Yes (we ARE authorized without needing sponsorship)
        if "sponsor" in label_lower and "without" in label_lower and any(x in label_lower for x in ["authorized", "authorization", "legally"]):
            auth = self._flat_config.get("work_authorization.us_work_authorized", True)
            return "Yes" if auth else "No"

        # IMPORTANT: Check sponsorship BEFORE work authorization (sponsorship questions often contain "authorization")
        if "sponsor" in label_lower:
            # Check both require_sponsorship_now and require_sponsorship_future
            need_sponsor_now = self._flat_config.get("work_authorization.require_sponsorship_now", False)
            need_sponsor_future = self._flat_config.get("work_authorization.require_sponsorship_future", False)
            need_sponsor = self._flat_config.get("work_authorization.require_sponsorship", False)

            # Any sponsorship need means Yes
            if need_sponsor_now or need_sponsor_future or need_sponsor:
                return "Yes"
            return "No"  # Default - no sponsorship needed

        # Work authorization (checked after sponsorship to avoid false positive)
        if any(x in label_lower for x in ["authorized", "authorization", "legally", "eligible to work"]):
            # Try multiple key variations
            auth = (
                self._flat_config.get("work_authorization.us_work_authorized") or
                self._flat_config.get("work_authorization.us_authorized") or
                self._flat_config.get("work_authorization.work_authorized")
            )
            if auth is not None:
                return "Yes" if auth else "No"
            return "Yes"  # Default for interns

        # School/University (but NOT yes/no questions like "are you in your junior year of school?")
        if any(x in label_lower for x in ["school", "university", "college", "institution"]):
            # Skip if this is a yes/no question (e.g., "are you currently in your junior year of school")
            if any(x in label_lower for x in ["are you", "do you", "will you", "junior year", "senior year",
                                                "on track to graduate", "currently in"]):
                pass  # Let yes/no patterns handle it below
            # Skip if asking about STATE/LOCATION of the school (e.g., "In what state is the university you attend located")
            elif any(x in label_lower for x in ["what state", "which state", "state is", "located in"]):
                state = self._flat_config.get("personal_info.state", "California")
                state_abbrev_map = {
                    "CA": "California", "NY": "New York", "TX": "Texas", "FL": "Florida",
                    "WA": "Washington", "IL": "Illinois", "PA": "Pennsylvania", "OH": "Ohio",
                    "GA": "Georgia", "NC": "North Carolina", "MA": "Massachusetts",
                    "NJ": "New Jersey", "VA": "Virginia", "CO": "Colorado", "AZ": "Arizona",
                    "OR": "Oregon", "MN": "Minnesota", "WI": "Wisconsin", "MD": "Maryland",
                    "CT": "Connecticut", "UT": "Utah", "IN": "Indiana", "TN": "Tennessee",
                    "MO": "Missouri", "MI": "Michigan", "SC": "South Carolina",
                }
                return state_abbrev_map.get(state.upper(), state)
            else:
                return self._flat_config.get("education.school") or self.config.get("education", [{}])[0].get("school")

        # Degree - normalize to common format
        # Skip if the question is asking about YEAR of completion (not degree type)
        if "degree" in label_lower and "discipline" not in label_lower and \
           not ("what year" in label_lower or "which year" in label_lower or ("year" in label_lower and any(x in label_lower for x in ["expect", "complet", "graduat", "finish"]))):
            degree = self._flat_config.get("education.degree") or self.config.get("education", [{}])[0].get("degree", "")
            # Map common degree formats
            if degree:
                degree_lower = degree.lower()
                if "bachelor" in degree_lower:
                    return "Bachelor's degree"  # Common Greenhouse format
                elif "master" in degree_lower:
                    return "Master's degree"
                elif "phd" in degree_lower or "doctor" in degree_lower:
                    return "Doctor of Philosophy (Ph.D.)"
            return degree

        # Discipline / Field of Study / Major (but NOT disability/chronic questions)
        if any(x in label_lower for x in ["discipline", "field of study", "major", "area of study", "concentration"]):
            # Exclude disability-related questions that might contain "condition"
            if not any(x in label_lower for x in ["disab", "chronic", "health", "medical", "physical", "visual", "auditory"]):
                field = self._flat_config.get("education.field_of_study") or self.config.get("education", [{}])[0].get("field_of_study", "")
                return field if field else "Computer Science"  # Default for tech roles

        # Year of school / class standing
        if ("year" in label_lower and "school" in label_lower) or "class standing" in label_lower or "year of study" in label_lower:
            # Determine class standing from graduation date
            grad = self._flat_config.get("education.graduation_date", "May 2026")
            import re as _re_yr
            yr_match = _re_yr.search(r'20(\d{2})', str(grad))
            if yr_match:
                grad_year = int("20" + yr_match.group(1))
                # Current year is 2026
                diff = grad_year - 2026
                if diff <= 0:
                    return "Senior"
                elif diff == 1:
                    return "Junior"
                elif diff == 2:
                    return "Sophomore"
                else:
                    return "Freshman"
            return "Senior"

        # GPA dropdown — if question asks "X.X or higher?" answer Yes/No, else return raw GPA
        if "gpa" in label_lower or "grade point" in label_lower:
            import re as _re
            threshold_match = _re.search(r"(\d+\.\d+)\s*or\s*higher", label_lower)
            if threshold_match:
                threshold = float(threshold_match.group(1))
                actual_gpa = float(self._flat_config.get("education.gpa", "3.6"))
                return "Yes" if actual_gpa >= threshold else "No"
            return self._flat_config.get("education.gpa", "3.6")

        # "Would you like to be considered for other openings/positions?"
        if "considered" in label_lower and any(x in label_lower for x in ["other", "opening", "position", "role"]):
            return "Yes"

        # "How many prior internships/co-ops?"
        if "how many" in label_lower and any(x in label_lower for x in ["internship", "co-op", "co op"]):
            return self._flat_config.get("skills.num_prior_internships", "1")

        # "Most recently completed form of education"
        if "most recent" in label_lower and ("education" in label_lower or "form" in label_lower):
            degree = self._flat_config.get("education.degree", "Bachelor's")
            if "bachelor" in degree.lower():
                return "Bachelor's degree"
            return degree

        # Graduation month/date
        if "month" in label_lower and any(x in label_lower for x in ["end", "grad", "completion"]):
            grad = self._flat_config.get("education.graduation_date", "May 2026")
            if "May" in str(grad) or "05" in str(grad):
                return "May"
            return grad.split()[0] if " " in str(grad) else "May"

        # Graduation/End year - handle multiple patterns
        if "year" in label_lower:
            if any(x in label_lower for x in ["end", "grad", "completion", "expected", "expect", "complet"]):
                grad = self._flat_config.get("education.graduation_date", "May 2026")
                # Extract year
                import re
                year_match = re.search(r'20\d{2}', str(grad))
                return year_match.group() if year_match else "2026"
            # Generic "year" field in education context
            if "date" in label_lower or "education" in label_lower:
                return "2026"

        # Start date month/year (for internships)
        if "start" in label_lower and "month" in label_lower:
            return "May"  # Common summer internship start
        if "start" in label_lower and "year" in label_lower:
            return "2026"  # Assuming 2026 internships

        # Remote/state eligibility confirmation (e.g., "hire within states listed, please confirm")
        if "confirm" in label_lower and any(x in label_lower for x in ["state", "location", "reside", "eligible"]):
            return "Yes"

        # Standalone State dropdown (e.g., "Please select your State", "which state would you work remotely from")
        if "state" in label_lower and "country" not in label_lower and "united" not in label_lower:
            # Only match when it's clearly asking for US state, not "state of mind" etc.
            if any(x in label_lower for x in ["select", "your state", "home state", "state of residence",
                                                "remotely from", "work from", "reside", "located"]):
                state = self._flat_config.get("personal_info.state", "California")
                # Expand abbreviation to full name for dropdown matching
                state_abbrev_map = {
                    "CA": "California", "NY": "New York", "TX": "Texas", "FL": "Florida",
                    "WA": "Washington", "IL": "Illinois", "PA": "Pennsylvania", "OH": "Ohio",
                    "GA": "Georgia", "NC": "North Carolina", "MA": "Massachusetts",
                    "NJ": "New Jersey", "VA": "Virginia", "CO": "Colorado", "AZ": "Arizona",
                    "OR": "Oregon", "MN": "Minnesota", "WI": "Wisconsin", "MD": "Maryland",
                    "CT": "Connecticut", "UT": "Utah", "IN": "Indiana", "TN": "Tennessee",
                    "MO": "Missouri", "MI": "Michigan", "SC": "South Carolina",
                }
                return state_abbrev_map.get(state.upper(), state)

        # Phone country code / dialing code dropdown
        if any(x in label_lower for x in ["country code", "dialing code", "phone country", "phone prefix",
                                            "countryphonecode", "country_phone_code"]):
            return "United States +1"

        # Security clearance eligibility
        if "security clearance" in label_lower or "clearance" in label_lower:
            us_citizen = self._flat_config.get("work_authorization.us_citizen", True)
            return "Yes" if us_citizen else "No"

        # State and Country combined field - try various formats
        if ("state" in label_lower and "country" in label_lower) or "state & country" in label_lower or "state and country" in label_lower:
            state = self._flat_config.get("personal_info.state", "California")
            country = self._flat_config.get("personal_info.country", "United States")
            city = self._flat_config.get("personal_info.city", "")
            # Return city + state format that's likely to match typeahead options
            if city:
                return f"{city}, {state}"
            return f"{state}, {country}"

        # Internship field/area question
        if "what field" in label_lower and ("internship" in label_lower or "complete" in label_lower):
            return self._flat_config.get("education.field_of_study", "Computer Science")

        # Looking for / interested in field
        if "looking" in label_lower and "field" in label_lower:
            return self._flat_config.get("education.field_of_study", "Software Engineering")

        # Currently enrolled in program
        if "currently enrolled" in label_lower or ("enrolled" in label_lower and "program" in label_lower):
            return "Yes"  # Assume user is a student for internship applications

        # Undergraduate/graduate status
        if "undergraduate" in label_lower or "graduate" in label_lower:
            if "program" in label_lower or "student" in label_lower:
                return "Yes"

        # Background check consent (check BEFORE race/ethnicity to avoid "background" false match)
        if "background check" in label_lower or "background screening" in label_lower:
            return "Yes"  # Consent to background check

        # Pronouns
        if "pronoun" in label_lower:
            pronouns = self._flat_config.get("demographics.pronouns", "")
            return pronouns if pronouns else "They/Them"

        # Transgender (must be before gender — "transgender" contains "gender")
        if "transgender" in label_lower:
            return "No"

        # Gender
        if "gender" in label_lower:
            return self._flat_config.get("demographics.gender", "Prefer not to say")

        # Race/Ethnicity (removed "background" to avoid false match with background check)
        if any(x in label_lower for x in ["race", "ethnic"]):
            return self._flat_config.get("demographics.ethnicity", "Prefer not to say")

        # Veteran
        if "veteran" in label_lower:
            return self._flat_config.get("demographics.veteran_status", "No")

        # Disability / Chronic condition
        if "disab" in label_lower or "chronic condition" in label_lower:
            return self._flat_config.get("demographics.disability_status", "Prefer not to say")

        # Relocation / Willing to relocate
        if "relocat" in label_lower or "willing to move" in label_lower:
            return "Yes"  # Default to willing to relocate for internships

        # Sexual orientation
        if "sexual orientation" in label_lower:
            return "Prefer not to say"

        # LGBTQ+
        if "lgbtq" in label_lower:
            return "Prefer not to say"

        # Internship length / duration
        if any(x in label_lower for x in ["length of internship", "internship duration", "internship length",
                                            "how long", "available for"]):
            if "internship" in label_lower or "intern" in label_lower:
                return "12 weeks"

        # Academic year / class standing (as dropdown label)
        if any(x in label_lower for x in ["academic year", "class standing", "year in school",
                                            "what year are you", "year of study"]):
            return "Senior"

        # Referred by / who referred you
        if "referred" in label_lower and ("who" in label_lower or "by" in label_lower or "name" in label_lower):
            return "N/A"

        # How did you hear - must match actual "how did you hear" questions
        # NOT "heartflow" (contains "hear"), NOT "source of your right to work"
        import re as _re_hear
        if (_re_hear.search(r'\bhear\b', label_lower) and any(x in label_lower for x in ["how", "where", "about"])) or \
           ("referral" in label_lower and "source" in label_lower) or \
           ("find out" in label_lower) or ("learn about" in label_lower) or \
           (_re_hear.search(r'\bhow did you\b', label_lower) and any(x in label_lower for x in ["find", "learn", "hear"])):
            return self._flat_config.get("common_answers.how_did_you_hear", "LinkedIn")

        # Previously worked at / employed at the company
        if any(x in label_lower for x in ["previously worked", "previously employed", "have you worked",
                                            "have you been employed", "worked for this", "employed with"]):
            return "No"

        # Family member at company
        if "family member" in label_lower or "relative" in label_lower:
            if any(x in label_lower for x in ["work", "employ", "company", "organization"]):
                return "No"

        # Referral questions (did someone refer you)
        if "refer" in label_lower and any(x in label_lower for x in ["someone", "did", "were you", "employee"]):
            return "No"

        # Certifications / licenses
        if any(x in label_lower for x in ["certification", "license", "certified"]):
            if any(x in label_lower for x in ["do you", "have you", "any"]):
                return "No"

        # Employment history at THIS company (e.g., "Impinj Employment History*")
        if "employment history" in label_lower:
            return "No"

        # AI Usage Policy / acknowledgments
        if "ai usage" in label_lower or "ai policy" in label_lower:
            return "Yes"

        # Debarred by FDA / excluded by OIG
        if ("debarred" in label_lower or "excluded" in label_lower) and any(x in label_lower for x in ["fda", "oig"]):
            return "No"

        # "Which university are you currently attending?" — return school name
        if ("which university" in label_lower or "what university" in label_lower or
            "university" in label_lower and ("attending" in label_lower or "enrolled" in label_lower or "currently" in label_lower)):
            education = self.config.get("education", [{}])
            if isinstance(education, list) and education:
                return education[0].get("school", "San Jose State University")
            return self._flat_config.get("education.school", "San Jose State University")

        # "Which team would you be interested in" / team preference
        if "which team" in label_lower or "team preference" in label_lower or "interested in" in label_lower and "team" in label_lower:
            return "Software"

        # Export compliance (national origin Cuba/Iran/North Korea/Syria)
        if "export" in label_lower and ("compliance" in label_lower or "national origin" in label_lower or "cuba" in label_lower or "iran" in label_lower):
            return "No"

        # "If answered Yes" follow-up questions (provide name if yes, N/A if no)
        if "if answered yes" in label_lower or "if yes" in label_lower:
            if any(x in label_lower for x in ["name", "provide", "please"]):
                return "N/A"

        # "Do you currently reside within the continental United States?"
        if "reside" in label_lower and any(x in label_lower for x in ["continental", "united states", "u.s"]):
            return "Yes"

        # "Are you actively completing your Ph.D.?"
        if "ph.d" in label_lower or "phd" in label_lower:
            if any(x in label_lower for x in ["completing", "pursuing", "enrolled", "currently"]):
                return "No"

        # "Please confirm the season you are applying for"
        if "season" in label_lower and any(x in label_lower for x in ["applying", "confirm", "interested"]):
            return "Summer 2026"

        # "I have read and understand [privacy notice/policy]" — acknowledgment
        if "i have read" in label_lower or "read and understand" in label_lower:
            return "Yes"

        # Transportation / living accommodations / housing for internship
        if "transportation" in label_lower or "living accommodations" in label_lower or "housing" in label_lower:
            if any(x in label_lower for x in ["do you have", "can you", "will you", "able to", "duration"]):
                return "Yes"

        # Work location/office/onsite questions
        # If asking "are you ok/open/comfortable with requirement" → Yes/No answer
        # If asking "which office/location" → city answer
        if any(x in label_lower for x in ["office", "onsite", "on-site", "in-person", "open to working"]):
            # Check if it's a yes/no question (are you ok/open/comfortable/willing)
            if any(x in label_lower for x in ["are you ok", "are you open", "are you comfortable",
                                                "are you willing", "requirement", "required to be"]):
                return "Yes"
            # Otherwise try to match a city
            personal = self.config.get("personal_info", {})
            city = personal.get("city", "").lower()
            city_abbreviations = {
                "san francisco": "SF",
                "new york": "NYC",
                "los angeles": "LA",
                "chicago": "Chicago",
                "seattle": "Seattle",
                "boston": "Boston",
                "austin": "Austin",
                "denver": "Denver",
            }
            for city_name, abbrev in city_abbreviations.items():
                if city_name in city or city_name in label_lower:
                    return abbrev
            return "Yes"

        # Work arrangement comfort questions (4 days a week, 5 days, hybrid, etc.)
        if "comfortable" in label_lower or "comfortable with" in label_lower:
            if any(x in label_lower for x in ["days", "week", "schedule", "arrangement", "onsite", "office", "hybrid"]):
                return "Yes"

        # This job requires X days a week
        if "requires" in label_lower and any(x in label_lower for x in ["days", "onsite", "office", "in-person"]):
            return "Yes"

        # Internship term (Summer 2026, etc.)
        if "internship" in label_lower and any(x in label_lower for x in ["term", "session", "period", "quarter"]):
            return "Summer 2026"

        # Expected graduation date
        if "expected" in label_lower and any(x in label_lower for x in ["graduat", "degree", "completion"]):
            education = self.config.get("education", [{}])
            if isinstance(education, list) and education:
                return education[0].get("graduation_date", "May 2026")
            return "May 2026"

        # Hispanic/Latino question
        if "hispanic" in label_lower or "latino" in label_lower:
            demo = self.config.get("demographics", {})
            ethnicity = demo.get("ethnicity", "Prefer not to say")
            if "hispanic" in ethnicity.lower() or "latino" in ethnicity.lower():
                return "Yes"
            return "No"

        # Transgender identity question
        if "transgender" in label_lower:
            return "Prefer not to say"

        # Privacy policy acknowledgement
        if "acknowledge" in label_lower and any(x in label_lower for x in ["privacy", "policy", "agree", "accept"]):
            return "Yes"

        # Relatives/family members at the company
        if any(x in label_lower for x in ["relative", "family member", "immediate family", "close personal"]):
            if any(x in label_lower for x in ["employed", "work", "practic", "use", "purchase"]):
                return "No"

        # Standalone "Source" dropdown (e.g., source--source, "How did you find this job?")
        # Must come BEFORE "source of right to work" to avoid false match
        import re as _re_source
        if (("source" in label_lower and not any(x in label_lower for x in ["right to work", "income", "funding"])) and
            _re_source.search(r'\b(source|where did you find|how did you find|job source)\b', label_lower)):
            return self._flat_config.get("common_answers.how_did_you_hear", "LinkedIn")

        # "Source of your right to work" — this is a work authorization question, not "how did you hear"
        if "source" in label_lower and "right to work" in label_lower:
            us_citizen = self._flat_config.get("work_authorization.us_citizen", True)
            return "Citizen" if us_citizen else "Work Visa"

        # Non-competition/non-solicitation agreements
        if any(x in label_lower for x in ["non-competition", "non-solicitation", "restrictive covenant"]):
            return "No"

        # Medical practice / product usage (e.g., Modernizing Medicine specific)
        if "medical practice" in label_lower or "ema" in label_lower or "ehr" in label_lower:
            return "No"

        # Highest level of education
        if "highest" in label_lower and ("education" in label_lower or "level" in label_lower):
            degree = self._flat_config.get("education.degree", "Bachelor's")
            if "bachelor" in degree.lower():
                return "Bachelor's degree"
            elif "master" in degree.lower():
                return "Master's degree"
            return degree

        # 18+ / Age verification
        if "18" in label_lower and ("older" in label_lower or "above" in label_lower or "age" in label_lower):
            return "Yes"

        # Consent to contact / be contacted
        if "consent" in label_lower and ("contact" in label_lower or "email" in label_lower or "communicate" in label_lower):
            return "Yes"

        # Criminal/felony background
        if any(x in label_lower for x in ["convicted", "felony", "criminal", "misdemeanor"]):
            return "No"

        # Drug test consent
        if "drug" in label_lower and ("test" in label_lower or "screen" in label_lower):
            return "Yes"

        # Internship availability / commitment
        if "available" in label_lower and any(x in label_lower for x in ["start", "summer", "internship", "full-time"]):
            return "Yes"

        # Travel percentage (e.g., "What percentage of time willing to travel?")
        if "percentage" in label_lower and "travel" in label_lower:
            return "25%"  # Reasonable travel willingness for internships

        # Willing to work specific schedule
        if "willing" in label_lower and any(x in label_lower for x in ["relocate", "travel", "work", "commute"]):
            return "Yes"

        return None

    async def _select_dropdown_option(self, page: Page, value: str) -> bool:
        """Select an option from an open React-Select dropdown menu.

        Uses keyboard navigation (ArrowDown + Enter) instead of direct clicks
        to ensure React-Select properly updates its internal state and hidden inputs.
        """
        value_lower = value.lower()

        # Wait for dropdown menu to fully render
        await page.wait_for_timeout(600)

        try:
            # Find individual option elements
            option_selectors = [
                '.select__option',
                '[id^="react-select-"][id*="-option-"]',
                '[role="option"]',
            ]

            options = []
            for sel in option_selectors:
                found = await page.query_selector_all(sel)
                if found:
                    options = found
                    break

            if not options:
                options = await page.query_selector_all('[class*="option"]:not([class*="menu"]):not([class*="container"])')

            logger.debug(f"Found {len(options)} dropdown options")

            # Collect all visible option texts
            option_data = []
            for i, opt in enumerate(options):
                if not await opt.is_visible():
                    continue
                text = (await opt.text_content() or "").strip()
                if not text or len(text) > 200:
                    continue
                option_data.append((i, opt, text, text.lower()))

            if option_data:
                logger.debug(f"  Options: {[t for _, _, t, _ in option_data]}")

            # Find the best matching option index
            match_index = self._find_best_option_match(value_lower, option_data)

            if match_index is not None:
                matched_text = option_data[match_index][2] if match_index < len(option_data) else value
                target_opt = option_data[match_index][1]

                # Try clicking the option element directly first — this triggers
                # React-Select's mouseDown handler which properly fires onChange
                try:
                    if await target_opt.is_visible():
                        await target_opt.click()
                        await page.wait_for_timeout(400)
                        logger.debug(f"Selected option via click: '{matched_text}'")
                        return True
                except Exception:
                    pass

                # Fallback: keyboard navigation (ArrowDown from first option + Enter)
                target_pos = match_index
                for _ in range(target_pos):
                    await page.keyboard.press("ArrowDown")
                    await page.wait_for_timeout(50)
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(400)

                logger.debug(f"Selected option via keyboard: '{matched_text}'")
                return True

        except Exception as e:
            logger.debug(f"Option selection failed: {e}")

        return False

    def _find_best_option_match(self, value_lower: str, option_data: list) -> int:
        """Find the index of the best matching option. Returns None if no match."""

        # First pass: exact match
        for idx, (i, opt, text, text_lower) in enumerate(option_data):
            if text_lower == value_lower:
                return idx

        # Second pass: partial match (value in option text, with contraction normalization)
        # Prefer longer/more specific matches (e.g., "Software Engineering" over "Engineering")
        value_normalized = value_lower.replace("don't", "do not").replace("doesn't", "does not").replace("won't", "will not").replace("can't", "cannot")
        partial_matches = []
        for idx, (i, opt, text, text_lower) in enumerate(option_data):
            text_normalized = text_lower.replace("don't", "do not").replace("doesn't", "does not").replace("won't", "will not").replace("can't", "cannot")
            if value_normalized in text_normalized and len(text_lower) < 100:
                partial_matches.append((idx, len(text_lower), 0))  # score 0 = value in option (broader option)
            elif text_normalized in value_normalized and len(text_lower) < 100:
                partial_matches.append((idx, len(text_lower), 1))  # score 1 = option in value (narrower option)
        if partial_matches:
            # Prefer: value-in-option (score 0) first, then longer text (more specific)
            partial_matches.sort(key=lambda x: (x[2], -x[1]))
            return partial_matches[0][0]

        # Discipline/major matching — "Software Engineering" should match "Engineering"
        # and vice versa when exact/partial matching above didn't find it
        discipline_synonyms = {
            "software engineering": ["engineering", "computer science", "computer engineering", "information technology", "cs", "se"],
            "computer science": ["engineering", "software engineering", "computer engineering", "information technology", "cs"],
            "computer engineering": ["engineering", "computer science", "software engineering", "electrical engineering"],
            "engineering": ["software engineering", "computer science", "computer engineering"],
            "information technology": ["computer science", "software engineering", "information systems"],
        }
        if value_lower in discipline_synonyms:
            for idx, (i, opt, text, text_lower) in enumerate(option_data):
                if text_lower in discipline_synonyms[value_lower]:
                    return idx
                # Also check if option contains any synonym
                for syn in discipline_synonyms[value_lower]:
                    if syn in text_lower and len(text_lower) < 50:
                        return idx

        # Third pass: smart matching
        for idx, (i, opt, text, text_lower) in enumerate(option_data):
            # Yes matching
            if value_lower == "yes":
                if any(x in text_lower for x in ["authorized", "eligible", "legally", "permitted", "citizen", "permanent"]):
                    return idx
                if any(x in text_lower for x in ["consent", "agree", "accept", "confirm", "acknowledge"]):
                    return idx
                if text_lower == "yes" or text_lower.startswith("yes,") or text_lower.startswith("yes -") or text_lower.startswith("yes.") or text_lower.startswith("yes "):
                    return idx

            # No matching — check "no" answers including sponsorship-style long options
            if value_lower == "no":
                if text_lower == "no" or text_lower.startswith("no,") or text_lower.startswith("no -") or text_lower.startswith("no."):
                    return idx
                if text_lower.startswith("no ") or "do not require" in text_lower or "i don't require" in text_lower:
                    return idx

            # Prefer not to say / Do not wish to answer
            if "prefer" in value_lower or "not to say" in value_lower or "not wish" in value_lower or "do not want" in value_lower:
                if any(x in text_lower for x in ["prefer not", "decline", "do not want", "don't want", "not to respond", "i do not want", "i don't wish", "do not wish", "don't wish"]):
                    return idx

            # Degree matching — "Bachelor of Science" -> "BS currently pursuing"
            if "bachelor" in value_lower and "bachelor" in text_lower:
                return idx
            if "master" in value_lower and ("master" in text_lower or text_lower.startswith("ms")):
                return idx

            # School matching
            if any(x in value_lower for x in ["university", "college", "institute"]):
                norm_value = value_lower.replace(",", "").replace("-", " ").replace("  ", " ")
                norm_text = text_lower.replace(",", "").replace("-", " ").replace("  ", " ")
                value_words = set(norm_value.split())
                text_words = set(norm_text.split())
                overlap = value_words & text_words
                significant = {w for w in overlap if len(w) > 2}
                if len(significant) >= 3:
                    return idx

            # Veteran status
            if value_lower == "no" and "veteran" in text_lower and "not" in text_lower:
                return idx

            # Pronouns matching — "he/him" -> "he/him/his", "she/her" -> "she/her/hers"
            if "/" in value_lower and any(x in value_lower for x in ["he", "she", "they", "ze"]):
                # Extract base pronoun (e.g., "he" from "he/him")
                base = value_lower.split("/")[0].strip()
                if base in text_lower:
                    return idx

            # LinkedIn source matching
            if "linkedin" in value_lower:
                if any(x in text_lower for x in ["linkedin", "online", "job board", "social media"]):
                    return idx

            # "Online Job Board" source matching — common alternatives
            if "online job board" in value_lower or "job board" in value_lower:
                if any(x in text_lower for x in ["linkedin", "online", "internet", "website", "job board", "job site", "web"]):
                    return idx

        # Degree-specific matching: "Bachelor of Science" -> "BS currently pursuing"
        if "bachelor" in value_lower:
            is_science = "science" in value_lower
            # First: try exact degree type match with "pursuing" (current student)
            for idx, (i, opt, text, text_lower) in enumerate(option_data):
                if is_science and text_lower.startswith("bs") and "pursuing" in text_lower:
                    return idx
                if not is_science and text_lower.startswith("ba") and "pursuing" in text_lower:
                    return idx
            # Second: try exact degree type match without "pursuing"
            for idx, (i, opt, text, text_lower) in enumerate(option_data):
                if is_science and text_lower.startswith("bs"):
                    return idx
                if not is_science and text_lower.startswith("ba"):
                    return idx
            # Third: any bachelor/BS/BA match
            for idx, (i, opt, text, text_lower) in enumerate(option_data):
                if text_lower.startswith("bs") or text_lower.startswith("ba") or "bachelor" in text_lower:
                    return idx

        # Final fallback for "no"
        if value_lower == "no":
            for idx, (i, opt, text, text_lower) in enumerate(option_data):
                if "not" in text_lower or "no" in text_lower:
                    return idx

        # GPA range matching — "3.6" should match "3.5 - 4.0" or "3.6 - 4.0" etc.
        import re as _re_gpa
        gpa_match = _re_gpa.match(r'^(\d+\.\d+)$', value_lower.strip())
        if gpa_match:
            gpa_val = float(gpa_match.group(1))
            for idx, (i, opt, text, text_lower) in enumerate(option_data):
                # Match ranges like "3.5 - 4.0", "3.5-4.0", "3.50-4.00"
                range_match = _re_gpa.search(r'(\d+\.?\d*)\s*[-–]\s*(\d+\.?\d*)', text_lower)
                if range_match:
                    low = float(range_match.group(1))
                    high = float(range_match.group(2))
                    if low <= gpa_val <= high:
                        return idx

        # Gender synonym matching — "Male"↔"Man", "Female"↔"Woman"
        gender_synonyms = {
            "male": ["man", "cis male", "cisgender male", "cis man", "cisgender man"],
            "female": ["woman", "cis female", "cisgender female", "cis woman", "cisgender woman"],
            "man": ["male", "cis male", "cisgender male"],
            "woman": ["female", "cis female", "cisgender female"],
        }
        if value_lower in gender_synonyms:
            for idx, (i, opt, text, text_lower) in enumerate(option_data):
                for syn in gender_synonyms[value_lower]:
                    if text_lower == syn or text_lower.startswith(syn + " ") or text_lower.startswith(syn + ","):
                        return idx

        # Graduation date → semester mapping
        # Maps "May 2026" to "Spring 2026", "August 2026" to "Fall 2026", etc.
        import re as _re_match
        month_to_semester = {
            "january": "spring", "february": "spring", "march": "spring",
            "april": "spring", "may": "spring",
            "june": "summer", "july": "summer", "august": "summer",
            "september": "fall", "october": "fall", "november": "fall", "december": "fall",
        }
        year_match = _re_match.search(r'20\d{2}', value_lower)
        if year_match:
            year = year_match.group()
            for month, semester in month_to_semester.items():
                if month in value_lower:
                    # Try semester + year (e.g., "Spring 2026")
                    for idx, (i, opt, text, text_lower) in enumerate(option_data):
                        if semester in text_lower and year in text_lower:
                            logger.debug(f"Graduation date mapped '{value_lower}' -> semester match '{text}'")
                            return idx
                    # Try just the year (e.g., "2026") excluding alumni/graduated options
                    for idx, (i, opt, text, text_lower) in enumerate(option_data):
                        if year in text_lower and "alumni" not in text_lower and "already" not in text_lower and "graduated" not in text_lower:
                            logger.debug(f"Graduation date mapped '{value_lower}' -> year match '{text}'")
                            return idx
                    break
            # If value is just a year with no month, try matching year directly
            if value_lower.strip() == year:
                for idx, (i, opt, text, text_lower) in enumerate(option_data):
                    if year in text_lower and "alumni" not in text_lower and "already" not in text_lower:
                        return idx

        return None

    async def _fill_greenhouse_dropdowns(self, page: Page, filled: Dict) -> None:
        """Handle Greenhouse-specific dropdown fields."""
        # Find all select__control elements directly
        dropdowns = await page.query_selector_all('.select__control, [role="combobox"], .css-1s2u09g-control, .css-13cymwt-control')

        # Also look for standard HTML selects with question_ IDs (these are Greenhouse custom questions)
        html_selects = await page.query_selector_all('select[id^="question_"]')
        await self._fill_greenhouse_html_selects(page, html_selects, filled)

        logger.debug(f"Found {len(dropdowns)} Greenhouse dropdowns")

        for dropdown in dropdowns:
            try:
                if not await dropdown.is_visible():
                    continue

                # Check if already filled - look for actual value vs placeholder
                text = (await dropdown.text_content() or "").strip()
                has_placeholder = (
                    "select" in text.lower() or
                    "choose" in text.lower() or
                    text == "" or
                    text == "--" or
                    len(text) < 2
                )

                # Find the label by going up to the field container
                label_text = await dropdown.evaluate('''(el) => {
                    // Go up to find the field container
                    let container = el.closest('.field, .select, [class*="field"], [class*="question"]');
                    if (!container) container = el.parentElement?.parentElement?.parentElement;
                    if (!container) return "";

                    // Find label within container
                    let label = container.querySelector("label, .select__label, [class*='label']");
                    return label ? label.textContent.trim() : "";
                }''')

                # Skip if no label found
                if not label_text:
                    continue

                # Skip if already handled
                if label_text in filled:
                    logger.debug(f"Dropdown '{label_text}' already handled, skipping")
                    continue

                # Skip if dropdown already has a real value (not placeholder)
                if not has_placeholder and text and len(text) >= 2:
                    logger.debug(f"Dropdown '{label_text}' appears pre-filled with '{text}'")
                    filled[label_text] = text  # Record as filled
                    continue

                # Get value to select
                value = self._get_dropdown_value_for_label(label_text)
                if not value:
                    logger.debug(f"No value mapped for dropdown: '{label_text}'")
                    continue

                logger.info(f"Filling Greenhouse dropdown: '{label_text}' -> '{value}'")

                # Click to open
                await dropdown.click()
                await page.wait_for_timeout(800)  # Increased wait time

                # Determine if this dropdown needs typeahead search (type to filter)
                label_lower = label_text.lower()
                needs_typeahead = any(x in label_lower for x in [
                    "school", "university", "college", "institution",
                    "location", "city", "state", "country", "where",
                ])

                if needs_typeahead:
                    # Type to search in the dropdown
                    search_term = value.split(",")[0][:20]  # First 20 chars before comma
                    await page.keyboard.type(search_term, delay=50)
                    await page.wait_for_timeout(1000)  # Wait for search results / API

                    # Try to select from filtered results
                    if await self._select_dropdown_option(page, value):
                        filled[label_text] = value
                        logger.info(f"Filled Greenhouse dropdown '{label_text}' = '{value}'")
                        await page.wait_for_timeout(300)
                        continue
                    else:
                        # If exact match not found, press Enter to select first result
                        await page.keyboard.press("Enter")
                        await page.wait_for_timeout(300)
                        filled[label_text] = value
                        logger.info(f"Filled Greenhouse dropdown '{label_text}' with first search result")
                        continue

                # Select option (non-typeahead dropdowns)
                if await self._select_dropdown_option(page, value):
                    filled[label_text] = value
                    logger.info(f"Filled Greenhouse dropdown '{label_text}' = '{value}'")
                    await page.wait_for_timeout(300)

                    # CRITICAL: Sync hidden question_XXXXXXXX input if present
                    await self._sync_dropdown_hidden_input(page, dropdown, value)
                else:
                    logger.warning(f"Could not find option '{value}' for '{label_text}'")
                    await page.keyboard.press("Escape")

            except Exception as e:
                logger.debug(f"Error in Greenhouse dropdown: {e}")
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass

    async def _fill_greenhouse_html_selects(self, page: Page, html_selects: list, filled: Dict) -> None:
        """Fill standard HTML select elements with question_ IDs."""
        for select in html_selects:
            try:
                if not await select.is_visible():
                    continue

                select_id = await select.get_attribute("id") or ""

                # Check if already filled
                current = await select.input_value()
                if current and current.strip():
                    logger.debug(f"HTML select {select_id} already has value: {current}")
                    continue

                # Get label for this select
                label_text = await self._get_label_for_element(page, select, select_id)
                if not label_text:
                    # Try to find label from parent container
                    label_text = await select.evaluate('''(el) => {
                        let container = el.closest('.field, .question, div[class*="field"]');
                        if (!container) container = el.parentElement?.parentElement;
                        if (!container) return "";
                        let label = container.querySelector('label, .field-label');
                        return label ? label.textContent.trim() : "";
                    }''')

                if not label_text:
                    continue

                # Get value for this dropdown
                value = self._get_dropdown_value_for_label(label_text)
                if not value:
                    logger.debug(f"No config value for HTML select '{label_text}'")
                    continue

                logger.info(f"Filling HTML select '{label_text}' with '{value}'")

                # Get all options
                options = await select.query_selector_all("option")
                option_texts = []
                for opt in options:
                    text = (await opt.text_content() or "").strip()
                    opt_value = await opt.get_attribute("value") or ""
                    if text and text.lower() != "select...":
                        option_texts.append((text, opt_value))

                # Try to match the option
                matched = False
                value_lower = value.lower()

                for opt_text, opt_value in option_texts:
                    opt_text_lower = opt_text.lower()

                    # Exact match
                    if value_lower == opt_text_lower:
                        await select.select_option(value=opt_value if opt_value else opt_text)
                        matched = True
                        break

                    # Partial match
                    if value_lower in opt_text_lower or opt_text_lower in value_lower:
                        await select.select_option(value=opt_value if opt_value else opt_text)
                        matched = True
                        break

                    # LinkedIn matching for "How did you hear" questions
                    if "linkedin" in value_lower:
                        if "linkedin" in opt_text_lower or "online" in opt_text_lower or "job board" in opt_text_lower:
                            await select.select_option(value=opt_value if opt_value else opt_text)
                            matched = True
                            break

                if matched:
                    filled[select_id] = value
                    logger.info(f"Filled HTML select {select_id} = '{value}'")
                else:
                    logger.warning(f"Could not match option for HTML select '{label_text}' with value '{value}'")
                    logger.debug(f"Available options: {[t for t, v in option_texts]}")

            except Exception as e:
                logger.debug(f"Error filling HTML select: {e}")

    async def _sync_dropdown_hidden_input(self, page: Page, dropdown, value: str) -> None:
        """Sync hidden question_XXXXXXXX input after dropdown selection."""
        try:
            # Find the hidden input in the same field container
            hidden_input = await dropdown.evaluate('''(el) => {
                // Look for the field container
                let container = el.closest('.field, .select, [class*="field"], [class*="question"]');
                if (!container) container = el.parentElement?.parentElement?.parentElement;
                if (!container) return null;

                // Find hidden input with question_ ID
                let hidden = container.querySelector('input[type="hidden"][id^="question_"]');
                if (hidden) return hidden.id;

                // Also check for hidden inputs with name containing question
                hidden = container.querySelector('input[type="hidden"][name^="question_"]');
                if (hidden) return hidden.name;

                return null;
            }''')

            if hidden_input:
                # Get the current value of the hidden input
                hidden_elem = await page.query_selector(f'input[id="{hidden_input}"], input[name="{hidden_input}"]')
                if hidden_elem:
                    current = await hidden_elem.get_attribute("value") or ""
                    if not current:
                        # Set the value via JavaScript
                        await page.evaluate(f'''() => {{
                            let el = document.querySelector('input[id="{hidden_input}"], input[name="{hidden_input}"]');
                            if (el) {{
                                el.value = "{value}";
                                el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                            }}
                        }}''')
                        logger.debug(f"Synced hidden input {hidden_input} with value: {value}")
                    else:
                        logger.debug(f"Hidden input {hidden_input} already has value: {current}")

        except Exception as e:
            logger.debug(f"Error syncing hidden input: {e}")

    async def _fill_checkboxes(self, page: Page) -> Dict[str, bool]:
        """Fill checkbox fields."""
        filled = {}

        elements = await page.query_selector_all('input[type="checkbox"]')
        for element in elements:
            try:
                if not await element.is_visible():
                    continue

                name = await element.get_attribute("name") or ""
                id_attr = await element.get_attribute("id") or ""
                label = await self._get_label_for_element(page, element, id_attr)

                should_check = self.get_boolean_for_field(f"{name} {id_attr}", label)

                # Default to checking "agree" type checkboxes
                if should_check is None:
                    if any(word in f"{name} {label}".lower() for word in ["agree", "terms", "consent", "accept"]):
                        should_check = True

                if should_check is not None:
                    is_checked = await element.is_checked()
                    if should_check and not is_checked:
                        await element.check()
                    elif not should_check and is_checked:
                        await element.uncheck()

                    filled[name or id_attr] = should_check
                    logger.debug(f"Set checkbox '{name or id_attr}' to {should_check}")

            except Exception as e:
                logger.debug(f"Error filling checkbox: {e}")

        return filled

    async def _fill_radio_buttons(self, page: Page) -> Dict[str, str]:
        """Fill radio button groups."""
        filled = {}

        # Group radio buttons by name
        elements = await page.query_selector_all('input[type="radio"]')
        radio_groups: Dict[str, List[ElementHandle]] = {}

        for element in elements:
            name = await element.get_attribute("name")
            if name:
                if name not in radio_groups:
                    radio_groups[name] = []
                radio_groups[name].append(element)

        # Process each group
        for name, radios in radio_groups.items():
            try:
                # Get labels for all options
                for radio in radios:
                    if not await radio.is_visible():
                        continue

                    id_attr = await radio.get_attribute("id") or ""
                    value = await radio.get_attribute("value") or ""
                    label = await self._get_label_for_element(page, radio, id_attr)

                    # Check if this option should be selected
                    should_select = self.get_boolean_for_field(f"{name} {value}", label)

                    # Handle Yes/No type radios
                    if should_select is None:
                        config_value = self.get_value_for_field(name, "")
                        if config_value:
                            if str(config_value).lower() == value.lower():
                                should_select = True
                            elif str(config_value).lower() in label.lower():
                                should_select = True

                    if should_select:
                        await radio.check()
                        filled[name] = value or label
                        logger.debug(f"Selected radio '{name}' = '{value or label}'")
                        break

            except Exception as e:
                logger.debug(f"Error filling radio group {name}: {e}")

        return filled

    async def _get_label_for_element(
        self, page: Page, element: ElementHandle, id_attr: str
    ) -> str:
        """Find the label text for a form element."""
        try:
            # Try finding label by 'for' attribute
            if id_attr:
                label = await page.query_selector(f'label[for="{id_attr}"]')
                if label:
                    return await label.text_content() or ""

            # Try finding parent label
            parent = await element.evaluate_handle("el => el.closest('label')")
            if parent:
                return await parent.text_content() or ""

            # Try finding nearby label (previous sibling)
            prev_text = await element.evaluate("""
                el => {
                    let prev = el.previousElementSibling;
                    if (prev && prev.tagName === 'LABEL') return prev.textContent;
                    return '';
                }
            """)
            if prev_text:
                return prev_text

        except Exception:
            pass

        return ""

    async def upload_resume(self, page: Page, resume_path: str) -> bool:
        """Upload resume file to file input."""
        try:
            # Find file inputs
            file_inputs = await page.query_selector_all('input[type="file"]')

            for file_input in file_inputs:
                try:
                    # Check if it's for resume
                    name = await file_input.get_attribute("name") or ""
                    id_attr = await file_input.get_attribute("id") or ""
                    accept = await file_input.get_attribute("accept") or ""

                    # Look for resume-related attributes
                    if any(word in f"{name} {id_attr}".lower() for word in ["resume", "cv", "file"]):
                        await file_input.set_input_files(resume_path)
                        logger.info(f"Uploaded resume to '{name or id_attr}'")
                        return True

                    # If accept includes pdf, likely resume
                    if ".pdf" in accept.lower() or "application/pdf" in accept.lower():
                        await file_input.set_input_files(resume_path)
                        logger.info(f"Uploaded resume to file input")
                        return True

                except Exception as e:
                    logger.debug(f"Error uploading to file input: {e}")

            # If only one file input, use it
            if len(file_inputs) == 1:
                await file_inputs[0].set_input_files(resume_path)
                logger.info("Uploaded resume to single file input")
                return True

        except Exception as e:
            logger.error(f"Error uploading resume: {e}")

        return False

    async def click_next_or_submit(self, page: Page) -> str:
        """
        Find and click next/continue/submit button.

        Returns:
            "next", "submit", or "none"
        """
        # Button patterns in priority order
        button_patterns = [
            # Submit patterns (higher priority)
            ('submit', ['submit', 'apply now', 'apply for job', 'send application']),
            # Next patterns
            ('next', ['next', 'continue', 'proceed', 'save and continue', 'next step']),
        ]

        for action, patterns in button_patterns:
            for pattern in patterns:
                # Try button elements
                for selector in [
                    f'button:has-text("{pattern}")',
                    f'input[type="submit"][value*="{pattern}" i]',
                    f'a:has-text("{pattern}")',
                    f'[role="button"]:has-text("{pattern}")',
                ]:
                    try:
                        element = await page.query_selector(selector)
                        if element and await element.is_visible():
                            await element.click()
                            logger.info(f"Clicked {action} button: {pattern}")
                            return action
                    except Exception:
                        continue

        return "none"


# Test
async def main():
    """Test form filler."""
    import yaml

    # Load config
    with open("config/master_config.yaml", "r") as f:
        config = yaml.safe_load(f)

    filler = FormFiller(config)

    # Test value lookup
    test_fields = [
        ("firstName", "First Name"),
        ("lastName", "Last Name"),
        ("email", "Email Address"),
        ("phone", "Phone Number"),
        ("linkedIn", "LinkedIn Profile"),
        ("school", "School/University"),
        ("sponsorship", "Do you require visa sponsorship?"),
    ]

    print("Field Mapping Test:")
    print("-" * 50)
    for name, label in test_fields:
        value = filler.get_value_for_field(name, label)
        print(f"{label}: {value}")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
