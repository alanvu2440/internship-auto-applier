#!/usr/bin/env python3
"""Inspect form structure after filling some fields to understand React state."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import yaml
from browser_manager import BrowserManager
from form_filler import FormFiller
from handlers.greenhouse import GreenhouseHandler
from ai_answerer import AIAnswerer
from loguru import logger

logger.remove()
logger.add(sys.stderr, format="{time:HH:mm:ss} | {level: <8} | {message}", level="INFO")

TEST_URL = "https://job-boards.greenhouse.io/xometry/jobs/5007635007"

async def main():
    config_path = Path(__file__).parent.parent / "config" / "mock_config.yaml"
    with open(config_path) as f:
        config = yaml.safe_load(f)

    browser = BrowserManager(headless=False, slow_mo=50)
    form_filler = FormFiller(config)
    ai_answerer = AIAnswerer(config)
    handler = GreenhouseHandler(form_filler, ai_answerer, browser, dry_run=True)

    await browser.start()
    page = await browser.create_stealth_page()

    await page.goto(TEST_URL, wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(3)

    # Click Apply
    apply_btn = await page.query_selector('button:has-text("Apply")')
    if apply_btn:
        await apply_btn.click()
        await asyncio.sleep(3)

    # Fill the form using our handler
    logger.info("Filling form...")
    await form_filler.fill_form(page)
    await asyncio.sleep(1)

    # Now inspect what the form looks like
    logger.info("\n=== AFTER FILLING ===")

    # Check question inputs and their values
    question_data = await page.evaluate('''() => {
        const inputs = document.querySelectorAll('input[id^="question_"]');
        return Array.from(inputs).map(i => ({
            id: i.id,
            type: i.type,
            value: i.value,
            ariaExpanded: i.getAttribute('aria-expanded'),
            role: i.getAttribute('role'),
        }));
    }''')

    logger.info(f"\nQuestion inputs after fill ({len(question_data)}):")
    for q in question_data:
        logger.info(f"  {q['id']}: type={q['type']}, value='{q['value'][:40]}', role={q['role']}")

    # Check React-Select single values (selected options)
    selected_values = await page.evaluate('''() => {
        const fields = document.querySelectorAll('.field, [class*="field"]');
        const result = [];
        for (const field of fields) {
            const label = field.querySelector('label');
            const singleValue = field.querySelector('.select__single-value');
            const input = field.querySelector('input[id^="question_"]');
            if (singleValue || input) {
                result.push({
                    label: label ? label.textContent.trim().substring(0, 50) : '(no label)',
                    selectedValue: singleValue ? singleValue.textContent.trim() : '(none)',
                    inputId: input ? input.id : '(no input)',
                    inputValue: input ? input.value : '',
                });
            }
        }
        return result;
    }''')

    logger.info(f"\nField states ({len(selected_values)}):")
    for sv in selected_values:
        logger.info(f"  {sv['label']}: selected='{sv['selectedValue']}', input={sv['inputId']}, inputVal='{sv['inputValue'][:30]}'")

    # Check what happens when we try to intercept form submission
    submit_data = await page.evaluate('''() => {
        const form = document.querySelector('form');
        if (!form) return { error: 'no form found' };
        return {
            action: form.action,
            method: form.method,
            enctype: form.encType,
            id: form.id,
            className: form.className.substring(0, 50),
        };
    }''')
    logger.info(f"\nForm: {submit_data}")

    # Try clicking submit and intercept
    logger.info("\nClicking submit to trigger validation...")
    await page.evaluate('''() => {
        const form = document.querySelector('form');
        if (form) {
            form.addEventListener('submit', (e) => {
                e.preventDefault();
                window.__formData = new FormData(form);
                window.__formEntries = Array.from(window.__formData.entries()).map(([k, v]) => ({
                    key: k,
                    value: typeof v === 'string' ? v.substring(0, 50) : v.name || 'file'
                }));
            }, { once: true });
        }
    }''')

    submit_btn = await page.query_selector('button[type="submit"], input[type="submit"], button:has-text("Submit")')
    if submit_btn:
        await submit_btn.click()
        await asyncio.sleep(2)

    # Get the intercepted form data
    form_entries = await page.evaluate('() => window.__formEntries || []')
    logger.info(f"\nForm data entries ({len(form_entries)}):")
    for entry in form_entries:
        logger.info(f"  {entry['key']}: {entry['value']}")

    # Check for validation errors
    errors = await page.evaluate('''() => {
        const errorElements = document.querySelectorAll(
            '.error-message, .field-error, [role="alert"], [class*="error"], [aria-invalid="true"]'
        );
        return Array.from(errorElements)
            .filter(el => el.offsetParent !== null)
            .map(el => el.textContent.trim())
            .filter(t => t.length > 2);
    }''')

    logger.info(f"\nValidation errors ({len(errors)}):")
    for err in errors[:20]:
        logger.info(f"  - {err[:80]}")

    await page.close()
    await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
