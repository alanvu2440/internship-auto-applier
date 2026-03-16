"""
Dual Browser Manager

TWO independent browsers, each with its own driver:
  Browser 1: nodriver Chrome — for SmartRecruiters (DataDome bypass)
  Browser 2: Playwright Chrome — for Greenhouse, Lever, Ashby, Workday, etc.

WHY: nodriver and Playwright both use CDP (Chrome DevTools Protocol).
Connecting both to the SAME Chrome causes CDP collisions and crashes.
Keeping them separate = each driver has exclusive control = no crashes.

Both browsers:
  - Load Simplify extension via --load-extension
  - Use persistent profiles (cookies/sessions preserved)
  - Stay alive for the entire session (never auto-close)
  - Have keeper tabs to prevent auto-shutdown
"""

import asyncio
import random
import subprocess
from pathlib import Path
from typing import Optional
from loguru import logger

# Playwright imports
from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from playwright_stealth import Stealth


class BrowserManager:
    """Dual browser manager — nodriver for SR, Playwright for everything else."""

    def __init__(
        self,
        headless: bool = False,
        slow_mo: int = 50,
        user_data_dir: Optional[str] = None,
        proxy: Optional[dict] = None,
        extension_paths: Optional[list] = None,
    ):
        self.headless = headless
        self.slow_mo = slow_mo
        self.proxy = proxy
        self.extension_paths = extension_paths or []

        # Profile directories (SEPARATE to avoid lock conflicts between browsers)
        # Playwright gets the old extension_default profile (has Simplify login data)
        # nodriver gets its own profile
        self._nodriver_profile = str(Path("data/browser_profiles/nodriver_profile"))
        self._playwright_profile = str(Path("data/browser_profiles/extension_default"))

        # Playwright state (for GH/Lever/Ashby/Workday/Generic)
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._persistent_context: Optional[BrowserContext] = None
        self._contexts: list[BrowserContext] = []
        self._keeper_page: Optional[Page] = None
        self._pw_started = False

        # nodriver state (for SmartRecruiters)
        self._nd_browser = None
        self._nd_keeper_tab = None
        self._nd_started = False

        # Shared stealth instance (reuse instead of creating per-page)
        self._stealth = Stealth()

    @property
    def nd_browser(self):
        """Get the nodriver browser (used by SmartRecruiters handler)."""
        return self._nd_browser

    @property
    def nd_keeper_tab(self):
        """Get the nodriver keeper tab."""
        return self._nd_keeper_tab

    # ── NODRIVER BROWSER (SmartRecruiters) ────────────────────────────

    async def start_nodriver(self):
        """Start nodriver Chrome for SmartRecruiters.

        Uses real Chrome with stealth patches — bypasses DataDome.
        Loads Simplify extension. Stays alive for entire session.
        """
        if self._nd_started and self._nd_browser:
            return

        profile = Path(self._nodriver_profile)
        profile.mkdir(parents=True, exist_ok=True)

        # Clean stale locks and kill orphaned Chrome
        self._clean_stale_locks(str(profile))
        self._kill_orphaned_chrome(str(profile))

        try:
            import nodriver as uc

            browser_args = [
                "--window-size=1920,1080",
                "--disable-infobars",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-session-crashed-bubble",
                "--disable-features=TranslateUI",
                "--noerrdialogs",
            ]

            if self.extension_paths:
                ext_list = ",".join(self.extension_paths)
                browser_args.append(f"--load-extension={ext_list}")
                browser_args.append(f"--disable-extensions-except={ext_list}")
                logger.info(f"[nodriver] Loading extensions: {ext_list}")

            self._nd_browser = await uc.start(
                headless=self.headless,
                browser_args=browser_args,
                user_data_dir=str(profile),
            )

            self._nd_keeper_tab = await self._nd_browser.get("about:blank")
            self._nd_started = True
            logger.info("nodriver Chrome started — keeper tab ready (for SmartRecruiters)")

        except Exception as e:
            logger.error(f"nodriver launch failed: {e}")
            self._nd_browser = None
            self._nd_started = False
            raise

    # ── PLAYWRIGHT BROWSER (GH/Lever/Ashby/Workday/Generic) ──────────

    async def start_playwright(self):
        """Start Playwright Chrome for non-SR handlers.

        Uses persistent context with Simplify extension loaded.
        Stays alive for entire session.
        """
        if self._pw_started and (self._persistent_context or self._browser):
            return

        profile = Path(self._playwright_profile)
        profile.mkdir(parents=True, exist_ok=True)

        # Clean stale locks
        self._clean_stale_locks(str(profile))

        # Kill any orphaned Chrome using this profile
        self._kill_orphaned_chrome(str(profile))

        self._playwright = await async_playwright().start()

        chrome_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-infobars",
            "--window-size=1920,1080",
            "--start-maximized",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-session-crashed-bubble",
            "--disable-features=TranslateUI",
            "--noerrdialogs",
        ]

        if self.extension_paths:
            ext_list = ",".join(self.extension_paths)
            chrome_args.append(f"--load-extension={ext_list}")
            chrome_args.append(f"--disable-extensions-except={ext_list}")
            logger.info(f"[Playwright] Loading extensions: {ext_list}")

        # Use persistent context (preserves cookies, extensions, profile)
        try:
            self._persistent_context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                headless=self.headless,  # Extensions require headed mode (headless=False by default)
                slow_mo=self.slow_mo,
                args=chrome_args,
                viewport={"width": 1920, "height": 1080},
            )
        except Exception:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
            self._pw_started = False
            raise
        self._context = self._persistent_context
        self._browser = None  # Not used with persistent context

        # Set up keeper page
        pages = self._persistent_context.pages
        if len(pages) > 1:
            for extra in pages[1:]:
                try:
                    await extra.close()
                except Exception:
                    pass
        if pages:
            self._keeper_page = pages[0]
            try:
                await self._keeper_page.goto("about:blank")
            except Exception:
                pass
        else:
            self._keeper_page = await self._persistent_context.new_page()

        self._pw_started = True
        logger.info("Playwright Chrome started — keeper tab ready (for GH/Lever/Ashby)")

    # ── UNIFIED START (backward compat) ──────────────────────────────

    async def start(self):
        """Start the Playwright browser (default for non-SR handlers).

        nodriver is started on-demand by SmartRecruiters handler.
        """
        await self.start_playwright()

    # ── PAGE CREATION ────────────────────────────────────────────────

    async def create_context(self) -> BrowserContext:
        """Get the Playwright browser context for creating pages."""
        if not self._pw_started:
            await self.start_playwright()

        if self._context:
            return self._context
        if self._persistent_context:
            return self._persistent_context

        raise RuntimeError("No Playwright browser context available — call start_playwright() first")

    async def create_stealth_page(self, context: Optional[BrowserContext] = None) -> Page:
        """Get a reusable Playwright page with stealth mode enabled.

        CRITICAL: Reuses the SAME page across all jobs. NEVER creates new Chrome.
        The browser starts ONCE and stays alive for the entire session.
        """
        # Reuse existing work page if alive — this is the NORMAL path
        if hasattr(self, '_work_page') and self._work_page:
            try:
                if not self._work_page.is_closed():
                    await self._work_page.goto("about:blank", timeout=5000)
                    return self._work_page
            except Exception:
                logger.info("Work page died — creating new tab in same browser")
                self._work_page = None

        # Work page is dead — try creating a new tab in existing context
        if context is None:
            try:
                context = await self.create_context()
            except Exception:
                # Context is also dead — this is the ONLY case where we restart
                if not hasattr(self, '_restart_count'):
                    self._restart_count = 0
                self._restart_count += 1
                if self._restart_count > 3:
                    logger.error("Browser restarted 3 times — something is fundamentally broken. Stopping.")
                    raise RuntimeError("Too many browser restarts")
                logger.warning(f"Browser context dead — restarting Chrome (restart #{self._restart_count})")
                self._persistent_context = None
                self._context = None
                self._contexts.clear()
                self._keeper_page = None
                self._pw_started = False
                self._browser = None
                self._work_page = None
                if self._playwright:
                    try:
                        await self._playwright.stop()
                    except Exception:
                        pass
                    self._playwright = None
                await self.start_playwright()
                context = await self.create_context()

        try:
            page = await context.new_page()
        except Exception as e:
            if "has been closed" in str(e):
                # Context died during new_page — one more restart attempt
                if not hasattr(self, '_restart_count'):
                    self._restart_count = 0
                self._restart_count += 1
                if self._restart_count > 3:
                    raise RuntimeError("Too many browser restarts — check browser_manager.py")
                logger.warning(f"Context died on new_page — restarting Chrome (restart #{self._restart_count})")
                self._persistent_context = None
                self._context = None
                self._contexts.clear()
                self._keeper_page = None
                self._pw_started = False
                self._browser = None
                self._work_page = None
                if self._playwright:
                    try:
                        await self._playwright.stop()
                    except Exception:
                        pass
                    self._playwright = None
                await self.start_playwright()
                context = await self.create_context()
                page = await context.new_page()
            else:
                raise

        # Apply stealth
        await self._stealth.apply_stealth_async(page)
        self._work_page = page

        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
        """)

        return page

    # ── HUMAN-LIKE INTERACTIONS ───────────────────────────────────────

    async def human_delay(self, min_ms: int = 500, max_ms: int = 2000):
        """Add a random human-like delay."""
        delay = random.randint(min_ms, max_ms) / 1000
        await asyncio.sleep(delay)

    async def human_type(self, page: Page, selector: str, text: str):
        """Type text with human-like delays between keystrokes."""
        element = await page.wait_for_selector(selector, timeout=10000)
        await element.click()
        await self.human_delay(200, 500)

        for char in text:
            await page.keyboard.type(char, delay=random.randint(50, 150))
            if random.random() < 0.1:
                await self.human_delay(100, 300)

    async def human_click(self, page: Page, selector: str):
        """Click with human-like behavior."""
        element = await page.wait_for_selector(selector, timeout=10000)
        box = await element.bounding_box()
        if box:
            x = box["x"] + box["width"] / 2 + random.randint(-5, 5)
            y = box["y"] + box["height"] / 2 + random.randint(-5, 5)
            await page.mouse.move(x, y, steps=random.randint(5, 15))
            await self.human_delay(100, 300)
        await element.click()
        await self.human_delay(300, 800)

    async def scroll_into_view(self, page: Page, selector: str):
        """Scroll element into view with human-like scrolling."""
        element = await page.wait_for_selector(selector, timeout=10000)
        await element.scroll_into_view_if_needed()
        await self.human_delay(200, 500)

    # ── CLEANUP HELPERS ──────────────────────────────────────────────

    def _clean_stale_locks(self, profile_dir: str):
        """Remove stale Chrome lock files from a profile directory."""
        for lock_file in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
            lock_path = Path(profile_dir) / lock_file
            if lock_path.exists():
                try:
                    lock_path.unlink()
                except Exception:
                    pass

    def _kill_orphaned_chrome(self, profile_dir: str):
        """Kill orphaned Chrome processes using a specific profile."""
        try:
            result = subprocess.run(
                ["pgrep", "-f", profile_dir],
                capture_output=True, text=True, timeout=5
            )
            if result.stdout.strip():
                logger.warning(f"Killing orphaned Chrome from previous run ({profile_dir})")
                subprocess.run(
                    ["pkill", "-9", "-f", profile_dir],
                    capture_output=True, timeout=5
                )
                # Brief sync sleep OK here — only runs once at startup before event loop is hot
                import time as _time
                _time.sleep(1)
        except Exception:
            pass

    async def close_context(self, context: BrowserContext):
        """Close a browser context."""
        if context in self._contexts:
            self._contexts.remove(context)
        await context.close()

    async def close(self):
        """Close everything — both browsers."""
        # Close Playwright contexts
        for context in self._contexts:
            try:
                await context.close()
            except Exception:
                pass
        self._contexts.clear()

        # Close Playwright browser
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None

        if self._persistent_context:
            try:
                await self._persistent_context.close()
            except Exception:
                pass
            self._persistent_context = None
            self._context = None

        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

        self._pw_started = False
        self._keeper_page = None

        # Close nodriver Chrome
        if self._nd_browser:
            try:
                self._nd_browser.stop()
            except Exception:
                pass
            self._nd_browser = None

        self._nd_started = False
        self._nd_keeper_tab = None

        logger.info("Both browsers closed")
