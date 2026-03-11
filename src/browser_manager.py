"""
Browser Manager Module

Manages Playwright browser instances with stealth mode to avoid detection.
Handles multiple browser contexts for parallel processing.
"""

import asyncio
import random
from pathlib import Path
from typing import Optional
from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from playwright_stealth import Stealth
from loguru import logger


class BrowserManager:
    """Manages stealth browser instances for job applications."""

    # Common user agents for rotation
    USER_AGENTS = [
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    ]

    def __init__(
        self,
        headless: bool = False,
        slow_mo: int = 50,
        user_data_dir: Optional[str] = None,
        proxy: Optional[dict] = None,
        extension_paths: Optional[list] = None,
    ):
        """
        Initialize browser manager.

        Args:
            headless: Run browser in headless mode (more detectable)
            slow_mo: Slow down operations by X ms (more human-like)
            user_data_dir: Path to Chrome user data for session persistence
            proxy: Proxy config dict with host, port, username, password
            extension_paths: List of unpacked extension directories to load
        """
        self.headless = headless
        self.slow_mo = slow_mo
        self.user_data_dir = user_data_dir
        self.proxy = proxy
        self.extension_paths = extension_paths or []
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._persistent_context: Optional[BrowserContext] = None
        self._contexts: list[BrowserContext] = []
        self._keeper_page: Optional[Page] = None  # Blank tab to keep browser window alive

    async def start(self):
        """Start the browser."""
        # Avoid leaking resources if start() called multiple times
        if self._browser or self._persistent_context:
            return
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass

        self._playwright = await async_playwright().start()

        # Extensions require headed mode + persistent context
        headless = self.headless
        if self.extension_paths:
            headless = False
            if not self.user_data_dir:
                from pathlib import Path
                # Use copied Chrome profile (has user's cookies/logins)
                chrome_profile = Path("data/browser_profiles/chrome_profile")
                if chrome_profile.exists():
                    self.user_data_dir = str(chrome_profile)
                    logger.info(f"Using Chrome profile with user data: {self.user_data_dir}")
                else:
                    profile = Path("data/browser_profiles/extension_default")
                    profile.mkdir(parents=True, exist_ok=True)
                    self.user_data_dir = str(profile)
                    logger.info(f"Auto-created browser profile: {self.user_data_dir}")

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

        # Add extension loading args
        if self.extension_paths:
            ext_list = ",".join(self.extension_paths)
            chrome_args.append(f"--load-extension={ext_list}")
            chrome_args.append(f"--disable-extensions-except={ext_list}")
            logger.info(f"Loading extensions: {ext_list}")

        launch_args = {
            "headless": headless,
            "slow_mo": self.slow_mo,
            "args": chrome_args,
        }

        # Add proxy if configured
        if self.proxy:
            proxy_server = f"http://{self.proxy['host']}:{self.proxy['port']}"
            launch_args["proxy"] = {
                "server": proxy_server,
                "username": self.proxy.get("username", ""),
                "password": self.proxy.get("password", ""),
            }
            logger.info(f"Using proxy: {self.proxy['host']}:{self.proxy['port']}")

        # Use persistent context if user data dir provided
        if self.user_data_dir:
            # launch_persistent_context returns a BrowserContext, not Browser
            self._persistent_context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=self.user_data_dir,
                **launch_args,
            )
            self._browser = None  # Not a Browser object
            logger.info(f"Started persistent browser with profile: {self.user_data_dir}")

            # Close any extra pages that Chrome restored, except keep ONE blank tab alive
            # so the browser window never closes between jobs (one window, multiple tabs)
            pages = self._persistent_context.pages
            if len(pages) > 1:
                logger.info(f"Closing {len(pages) - 1} extra tabs from previous session")
                for extra_page in pages[1:]:
                    try:
                        await extra_page.close()
                    except Exception:
                        pass
            # Keep one blank page as the permanent keeper tab
            if pages:
                self._keeper_page = pages[0]
                try:
                    await self._keeper_page.goto("about:blank")
                except Exception:
                    pass
            else:
                self._keeper_page = await self._persistent_context.new_page()
                try:
                    await self._keeper_page.goto("about:blank")
                except Exception:
                    pass
            logger.info("Browser keeper tab ready — one window, multiple tabs mode")
        else:
            self._persistent_context = None
            self._browser = await self._playwright.chromium.launch(**launch_args)
            logger.info("Started browser")

    async def create_context(self) -> BrowserContext:
        """Create a new browser context with stealth settings."""
        if not self._browser and not self._persistent_context:
            await self.start()

        # Persistent context IS the context — reuse it directly
        if self._persistent_context:
            return self._persistent_context

        context = await self._browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=random.choice(self.USER_AGENTS),
            locale="en-US",
            timezone_id="America/Los_Angeles",
            geolocation={"latitude": 37.7749, "longitude": -122.4194},
            permissions=["geolocation"],
        )

        # Block unnecessary resources for speed (but not on all pages — some need them)
        await context.route(
            "**/*.{woff,woff2,ttf,eot}",
            lambda route: route.abort(),
        )

        self._contexts.append(context)
        return context

    async def create_stealth_page(self, context: Optional[BrowserContext] = None) -> Page:
        """Create a new page with stealth mode enabled."""
        if context is None:
            context = await self.create_context()

        try:
            page = await context.new_page()
        except Exception as e:
            if "has been closed" in str(e):
                logger.warning("Browser was closed — restarting...")
                # Force-clear stale references so start() creates a fresh browser
                self._browser = None
                self._persistent_context = None
                self._contexts.clear()
                self._keeper_page = None
                if self._playwright:
                    try:
                        await self._playwright.stop()
                    except Exception:
                        pass
                    self._playwright = None
                await self.start()
                context = await self.create_context()
                page = await context.new_page()
            else:
                raise

        # Apply stealth to page
        stealth = Stealth()
        await stealth.apply_stealth_async(page)

        # Additional stealth measures (lighter when extensions loaded)
        if self.extension_paths:
            # Skip plugins/chrome overrides — they break extension functionality
            await page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            """)
        else:
            await page.add_init_script("""
                // Override navigator properties
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });

                // Override chrome property
                window.chrome = { runtime: {} };

                // Override permissions
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications' ?
                        Promise.resolve({ state: Notification.permission }) :
                        originalQuery(parameters)
                );
            """)

        return page

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
            if random.random() < 0.1:  # 10% chance of small pause
                await self.human_delay(100, 300)

    async def human_click(self, page: Page, selector: str):
        """Click with human-like behavior."""
        element = await page.wait_for_selector(selector, timeout=10000)

        # Get element position
        box = await element.bounding_box()
        if box:
            # Add slight randomness to click position
            x = box["x"] + box["width"] / 2 + random.randint(-5, 5)
            y = box["y"] + box["height"] / 2 + random.randint(-5, 5)

            # Move mouse to element (human-like)
            await page.mouse.move(x, y, steps=random.randint(5, 15))
            await self.human_delay(100, 300)

        await element.click()
        await self.human_delay(300, 800)

    async def scroll_into_view(self, page: Page, selector: str):
        """Scroll element into view with human-like scrolling."""
        element = await page.wait_for_selector(selector, timeout=10000)
        await element.scroll_into_view_if_needed()
        await self.human_delay(200, 500)

    async def close_context(self, context: BrowserContext):
        """Close a browser context."""
        if context in self._contexts:
            self._contexts.remove(context)
        await context.close()

    async def close(self):
        """Close all contexts and the browser."""
        for context in self._contexts:
            try:
                await context.close()
            except Exception:
                pass
        self._contexts.clear()

        if self._persistent_context:
            try:
                await self._persistent_context.close()
            except Exception:
                pass
            self._persistent_context = None

        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None

        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None


async def main():
    """Test the browser manager."""
    manager = BrowserManager(headless=False, slow_mo=100)

    try:
        await manager.start()
        page = await manager.create_stealth_page()

        # Test on a bot detection site
        await page.goto("https://bot.sannysoft.com/")
        await asyncio.sleep(5)

        # Take screenshot
        await page.screenshot(path="data/stealth_test.png")
        print("Screenshot saved to data/stealth_test.png")

        await asyncio.sleep(3)

    finally:
        await manager.close()


if __name__ == "__main__":
    asyncio.run(main())
