#!/usr/bin/env python3
"""Inspect a Greenhouse form's DOM structure to understand hidden inputs."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from browser_manager import BrowserManager
from loguru import logger

logger.remove()
logger.add(sys.stderr, format="{time:HH:mm:ss} | {level: <8} | {message}", level="INFO")

TEST_URL = "https://job-boards.greenhouse.io/xometry/jobs/5007635007"

async def main():
    browser = BrowserManager(headless=False, slow_mo=50)
    await browser.start()
    page = await browser.create_stealth_page()

    await page.goto(TEST_URL, wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(3)

    # Click Apply button
    apply_btn = await page.query_selector('button:has-text("Apply")')
    if apply_btn:
        await apply_btn.click()
        await asyncio.sleep(3)

    # Check for all input types
    all_inputs = await page.evaluate('''() => {
        const inputs = document.querySelectorAll('input, select, textarea');
        const result = [];
        for (const inp of inputs) {
            result.push({
                tag: inp.tagName,
                type: inp.type || '',
                id: inp.id || '',
                name: inp.name || '',
                value: inp.value || '',
                hidden: inp.type === 'hidden',
                visible: inp.offsetParent !== null,
                className: inp.className.substring(0, 80)
            });
        }
        return result;
    }''')

    logger.info(f"Total form elements: {len(all_inputs)}")

    # Count by type
    hidden_inputs = [i for i in all_inputs if i['hidden']]
    question_inputs = [i for i in all_inputs if i['id'].startswith('question_')]
    hidden_question_inputs = [i for i in all_inputs if i['hidden'] and i['id'].startswith('question_')]
    visible_question_inputs = [i for i in all_inputs if not i['hidden'] and i['id'].startswith('question_')]

    logger.info(f"Hidden inputs: {len(hidden_inputs)}")
    logger.info(f"Question inputs (any): {len(question_inputs)}")
    logger.info(f"Hidden question inputs: {len(hidden_question_inputs)}")
    logger.info(f"Visible question inputs: {len(visible_question_inputs)}")

    logger.info("\n--- ALL HIDDEN INPUTS ---")
    for inp in hidden_inputs:
        logger.info(f"  id={inp['id']}, name={inp['name']}, value={inp['value'][:30]}")

    logger.info("\n--- QUESTION INPUTS ---")
    for inp in question_inputs:
        logger.info(f"  id={inp['id']}, type={inp['type']}, hidden={inp['hidden']}, visible={inp['visible']}, tag={inp['tag']}")

    # Check for React-Select containers
    react_selects = await page.evaluate('''() => {
        const controls = document.querySelectorAll('.select__control, [role="combobox"]');
        const result = [];
        for (const ctrl of controls) {
            let container = ctrl.closest('.field, .select, [class*="field"]');
            if (!container) container = ctrl.parentElement?.parentElement?.parentElement;
            const label = container ? container.querySelector('label') : null;
            const hidden = container ? container.querySelector('input[type="hidden"]') : null;
            result.push({
                label: label ? label.textContent.trim() : '(no label)',
                hiddenId: hidden ? hidden.id : '(no hidden)',
                hiddenName: hidden ? hidden.name : '',
                hiddenValue: hidden ? hidden.value : '',
            });
        }
        return result;
    }''')

    logger.info(f"\n--- REACT-SELECT DROPDOWNS ({len(react_selects)}) ---")
    for rs in react_selects:
        logger.info(f"  label='{rs['label'][:50]}', hiddenId={rs['hiddenId']}, value={rs['hiddenValue'][:30]}")

    # Check education hidden inputs
    edu_hidden = await page.evaluate('''() => {
        const inputs = document.querySelectorAll('input[type="hidden"][id$="--0"]');
        return Array.from(inputs).map(i => ({ id: i.id, name: i.name, value: i.value }));
    }''')

    logger.info(f"\n--- EDUCATION HIDDEN INPUTS ({len(edu_hidden)}) ---")
    for inp in edu_hidden:
        logger.info(f"  id={inp['id']}, name={inp['name']}, value={inp['value'][:30]}")

    await page.close()
    await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
