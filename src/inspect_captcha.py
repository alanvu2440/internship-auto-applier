#!/usr/bin/env python3
"""Inspect a Greenhouse form to find the reCAPTCHA sitekey and type."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from browser_manager import BrowserManager
from loguru import logger

logger.remove()
logger.add(sys.stderr, format="{time:HH:mm:ss} | {level: <8} | {message}", level="DEBUG")

TEST_URLS = [
    "https://job-boards.greenhouse.io/xometry/jobs/5007635007",
    "https://boards.greenhouse.io/embed/job_app?for=brex&token=6394894003",
]


async def inspect_captcha(page, url):
    logger.info(f"\n{'='*60}")
    logger.info(f"Inspecting: {url}")
    logger.info(f"{'='*60}")

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        logger.warning(f"Page load issue: {e}")
        return

    await asyncio.sleep(3)

    # Click Apply if needed
    apply_btn = await page.query_selector('button:has-text("Apply"), a:has-text("Apply")')
    if apply_btn:
        try:
            await apply_btn.click()
            await asyncio.sleep(3)
        except Exception:
            pass

    # 1. Check for reCAPTCHA script tags
    scripts_info = await page.evaluate('''() => {
        const scripts = document.querySelectorAll('script[src*="recaptcha"], script[src*="captcha"]');
        return Array.from(scripts).map(s => ({
            src: s.src,
            async: s.async,
            defer: s.defer,
        }));
    }''')
    logger.info(f"\nreCAPTCHA scripts found: {len(scripts_info)}")
    for s in scripts_info:
        logger.info(f"  src: {s['src']}")

    # 2. Check for data-sitekey attributes
    sitekeys = await page.evaluate('''() => {
        const els = document.querySelectorAll('[data-sitekey]');
        return Array.from(els).map(el => ({
            tag: el.tagName,
            sitekey: el.getAttribute('data-sitekey'),
            size: el.getAttribute('data-size'),
            callback: el.getAttribute('data-callback'),
            action: el.getAttribute('data-action'),
            className: el.className.substring(0, 60),
            id: el.id,
        }));
    }''')
    logger.info(f"\nElements with data-sitekey: {len(sitekeys)}")
    for sk in sitekeys:
        logger.info(f"  tag={sk['tag']}, sitekey={sk['sitekey']}, size={sk['size']}, "
                     f"callback={sk['callback']}, action={sk['action']}, "
                     f"id={sk['id']}, class={sk['className']}")

    # 3. Check for g-recaptcha-response textarea
    response_fields = await page.evaluate('''() => {
        const fields = document.querySelectorAll(
            '#g-recaptcha-response, [name="g-recaptcha-response"], textarea[name*="recaptcha"]'
        );
        return Array.from(fields).map(f => ({
            tag: f.tagName,
            id: f.id,
            name: f.name,
            value: f.value ? f.value.substring(0, 30) : '(empty)',
            hidden: f.style.display === 'none' || f.type === 'hidden',
        }));
    }''')
    logger.info(f"\nreCAPTCHA response fields: {len(response_fields)}")
    for rf in response_fields:
        logger.info(f"  tag={rf['tag']}, id={rf['id']}, name={rf['name']}, "
                     f"value={rf['value']}, hidden={rf['hidden']}")

    # 4. Check for grecaptcha object
    grecaptcha_info = await page.evaluate('''() => {
        if (typeof grecaptcha === 'undefined') return { exists: false };
        const info = { exists: true, methods: [] };
        try {
            info.methods = Object.keys(grecaptcha).filter(k => typeof grecaptcha[k] === 'function');
        } catch(e) { info.error = e.message; }
        return info;
    }''')
    logger.info(f"\ngrecaptcha object: {grecaptcha_info}")

    # 5. Search page source for reCAPTCHA key pattern
    key_search = await page.evaluate('''() => {
        const html = document.documentElement.innerHTML;
        // reCAPTCHA keys start with 6L and are ~40 chars
        const matches = html.match(/6L[a-zA-Z0-9_-]{38,42}/g);
        return matches ? [...new Set(matches)] : [];
    }''')
    logger.info(f"\nreCAPTCHA keys found in HTML: {key_search}")

    # 6. Check for Enterprise reCAPTCHA
    enterprise_check = await page.evaluate('''() => {
        const scripts = document.querySelectorAll('script[src*="enterprise"]');
        const hasEnterprise = scripts.length > 0;
        const hasGrecaptchaEnterprise = typeof grecaptcha !== 'undefined' &&
            typeof grecaptcha.enterprise !== 'undefined';
        return {
            enterpriseScript: hasEnterprise,
            enterpriseObject: hasGrecaptchaEnterprise,
            scriptSrcs: Array.from(scripts).map(s => s.src),
        };
    }''')
    logger.info(f"\nEnterprise reCAPTCHA: {enterprise_check}")

    # 7. Check ___grecaptcha_cfg
    cfg_info = await page.evaluate('''() => {
        if (typeof ___grecaptcha_cfg === 'undefined') return { exists: false };
        try {
            const cfg = ___grecaptcha_cfg;
            return {
                exists: true,
                clientCount: cfg.clients ? Object.keys(cfg.clients).length : 0,
                clients: cfg.clients ? Object.keys(cfg.clients).map(k => {
                    const c = cfg.clients[k];
                    return { id: k, keys: Object.keys(c).slice(0, 10) };
                }) : [],
            };
        } catch(e) { return { exists: true, error: e.message }; }
    }''')
    logger.info(f"\n___grecaptcha_cfg: {cfg_info}")

    # 8. Check form submit button attributes
    submit_info = await page.evaluate('''() => {
        const btns = document.querySelectorAll(
            'button[type="submit"], input[type="submit"], button:has-text("Submit")'
        );
        return Array.from(btns).map(b => ({
            tag: b.tagName,
            type: b.type,
            text: b.textContent.trim().substring(0, 30),
            className: b.className.substring(0, 60),
            dataSitekey: b.getAttribute('data-sitekey'),
            dataCallback: b.getAttribute('data-callback'),
            dataAction: b.getAttribute('data-action'),
        }));
    }''')
    logger.info(f"\nSubmit buttons: {len(submit_info)}")
    for si in submit_info:
        logger.info(f"  tag={si['tag']}, text='{si['text']}', sitekey={si['dataSitekey']}, "
                     f"callback={si['dataCallback']}, action={si['dataAction']}")


async def main():
    browser = BrowserManager(headless=False, slow_mo=50)
    await browser.start()
    page = await browser.create_stealth_page()

    for url in TEST_URLS:
        await inspect_captcha(page, url)

    await page.close()
    await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
