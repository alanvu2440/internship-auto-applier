#!/usr/bin/env python3
"""
Single Job Test - Focus on getting ONE job to work 100%.
Runs with detailed logging to identify all issues.
"""

import asyncio
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

import yaml
from loguru import logger

from browser_manager import BrowserManager
from form_filler import FormFiller
from handlers.greenhouse import GreenhouseHandler
from ai_answerer import AIAnswerer

# Test a specific Greenhouse job URL
# Using a real job from SimplifyJobs that we know is accepting applications
TEST_URL = "https://job-boards.greenhouse.io/sigmacomputing/jobs/7614004003"
TEST_COMPANY = "Sigma Computing"
TEST_ROLE = "Software Engineering Intern (Summer 2026)"


async def run_single_test():
    """Run a single focused test on one job."""

    # Setup detailed logging
    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        level="DEBUG",
    )

    # Also log to file
    log_file = Path("logs/single_job_test.log")
    log_file.parent.mkdir(exist_ok=True)
    logger.add(log_file, level="DEBUG", rotation="1 MB")

    logger.info("=" * 60)
    logger.info("SINGLE JOB TEST - Focusing on 100% success")
    logger.info(f"Test URL: {TEST_URL}")
    logger.info("=" * 60)

    # Load mock config
    config_path = Path(__file__).parent.parent / "config" / "mock_config.yaml"
    with open(config_path) as f:
        config = yaml.safe_load(f)

    logger.info(f"Test user: {config['personal_info']['first_name']} {config['personal_info']['last_name']}")
    logger.info(f"Test email: {config['personal_info']['email']}")

    # Check resume exists
    resume_path = Path(__file__).parent.parent / config.get("files", {}).get("resume", "")
    if not resume_path.exists():
        # Create a dummy resume for testing
        dummy_resume = Path(__file__).parent.parent / "config" / "resume.pdf"
        if not dummy_resume.exists():
            logger.warning(f"Resume not found at {resume_path}, creating dummy")
            # Create minimal PDF
            dummy_resume.parent.mkdir(exist_ok=True)
            # Write a minimal valid PDF
            with open(dummy_resume, "wb") as f:
                f.write(b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Count 1/Kids[3 0 R]>>endobj\n3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R>>endobj\nxref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n0000000052 00000 n \n0000000102 00000 n \ntrailer<</Size 4/Root 1 0 R>>\nstartxref\n178\n%%EOF")
            logger.info(f"Created dummy resume at {dummy_resume}")

    # Setup browser and handler
    browser = BrowserManager(headless=False, slow_mo=100)
    form_filler = FormFiller(config)
    ai_answerer = AIAnswerer(config)

    # Set job context
    ai_answerer.set_profile(config)
    ai_answerer.set_job_context(TEST_COMPANY, TEST_ROLE)

    # Create handler in dry_run mode
    handler = GreenhouseHandler(form_filler, ai_answerer, browser, dry_run=True)

    try:
        await browser.start()
        page = await browser.create_stealth_page()

        logger.info("Browser started, navigating to job...")

        # Navigate to job
        await page.goto(TEST_URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)

        # Take initial screenshot
        await page.screenshot(path="logs/01_initial_page.png")
        logger.info("Screenshot: logs/01_initial_page.png")

        # Check page title
        title = await page.title()
        logger.info(f"Page title: {title}")

        # Check if there's an Apply button we need to click
        apply_btns = await page.query_selector_all('a:has-text("Apply"), button:has-text("Apply")')
        for btn in apply_btns:
            if await btn.is_visible():
                btn_text = await btn.text_content()
                logger.info(f"Found Apply button: {btn_text}")
                await btn.click()
                await asyncio.sleep(2)
                await page.screenshot(path="logs/02_after_apply_click.png")
                break

        # Wait for form to load
        try:
            await page.wait_for_selector('input[name="first_name"], input#first_name, form#application_form', timeout=10000)
            logger.info("Form inputs detected")
        except Exception as e:
            logger.warning(f"Form inputs not detected: {e}")

        await page.screenshot(path="logs/03_form_loaded.png")

        # Detect all form fields
        logger.info("=== FORM FIELD DETECTION ===")

        # Text inputs
        text_inputs = await page.query_selector_all('input[type="text"], input[type="email"], input[type="tel"], input:not([type])')
        for inp in text_inputs:
            if await inp.is_visible():
                inp_id = await inp.get_attribute("id") or ""
                inp_name = await inp.get_attribute("name") or ""
                inp_placeholder = await inp.get_attribute("placeholder") or ""
                logger.debug(f"Text input: id={inp_id}, name={inp_name}, placeholder={inp_placeholder}")

        # Dropdowns (select and React-Select)
        selects = await page.query_selector_all('select')
        logger.debug(f"Found {len(selects)} HTML select elements")

        react_selects = await page.query_selector_all('.select__control, [role="combobox"]')
        logger.debug(f"Found {len(react_selects)} React-Select dropdowns")

        # File inputs
        file_inputs = await page.query_selector_all('input[type="file"]')
        logger.debug(f"Found {len(file_inputs)} file input elements")

        # Hidden question inputs
        hidden_questions = await page.query_selector_all('input[type="hidden"][id^="question_"]')
        logger.debug(f"Found {len(hidden_questions)} hidden question_* inputs")

        # Checkboxes
        checkboxes = await page.query_selector_all('input[type="checkbox"]')
        logger.debug(f"Found {len(checkboxes)} checkboxes")

        # Now run the actual handler
        logger.info("=== RUNNING GREENHOUSE HANDLER ===")

        job_data = {"company": TEST_COMPANY, "role": TEST_ROLE}
        result = await handler.apply(page, TEST_URL, job_data)

        logger.info(f"Handler result: {result}")

        # Take final screenshot
        await page.screenshot(path="logs/04_final_state.png")

        # Check for any error messages on page
        error_msg = await handler.get_error_message(page)
        if error_msg:
            logger.error(f"Error on page: {error_msg}")

        # Check all filled fields
        logger.info("=== CHECKING FILLED FIELDS ===")

        filled_fields = await handler._check_filled_fields(page)
        for field, value in filled_fields.items():
            logger.info(f"  {field}: {value}")

        # Check hidden inputs that are still empty
        logger.info("=== CHECKING EMPTY HIDDEN INPUTS ===")
        empty_hidden = []
        for hidden in hidden_questions:
            hidden_id = await hidden.get_attribute("id") or ""
            hidden_value = await hidden.get_attribute("value") or ""
            if not hidden_value:
                empty_hidden.append(hidden_id)
                logger.warning(f"Empty hidden input: {hidden_id}")

        if empty_hidden:
            logger.warning(f"Total empty hidden inputs: {len(empty_hidden)}")
        else:
            logger.info("All hidden inputs have values!")

        # Check React-Select dropdowns
        logger.info("=== CHECKING REACT-SELECT DROPDOWNS ===")
        for dropdown in react_selects:
            if await dropdown.is_visible():
                current_text = (await dropdown.text_content() or "").strip()
                label = await dropdown.evaluate('''(el) => {
                    let container = el.closest('.field, .select, [class*="field"]');
                    if (!container) container = el.parentElement?.parentElement?.parentElement;
                    if (!container) return "";
                    let label = container.querySelector("label, .select__label, [class*='label']");
                    return label ? label.textContent.trim() : "";
                }''')
                if "select" in current_text.lower() or not current_text:
                    logger.warning(f"Unfilled dropdown: {label}")
                else:
                    logger.info(f"  Dropdown '{label}': {current_text[:50]}")

        # Keep browser open for manual inspection
        logger.info("=== TEST COMPLETE ===")
        logger.info(f"Result: {'SUCCESS' if result else 'FAILED'}")
        logger.info("Keeping browser open for 10 seconds for inspection...")

        await asyncio.sleep(10)

        return result

    except Exception as e:
        logger.error(f"Test failed with exception: {e}")
        import traceback
        traceback.print_exc()
        return False

    finally:
        await browser.close()


if __name__ == "__main__":
    result = asyncio.run(run_single_test())
    sys.exit(0 if result else 1)
