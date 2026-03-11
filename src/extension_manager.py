"""
Extension Manager

Downloads and manages Chrome extensions (primarily Simplify Copilot) for
automated form filling. Extensions handle boilerplate fields (name, email,
phone, address, education) so the bot only needs to handle custom questions.

Simplify Copilot: https://simplify.jobs/copilot
Chrome Web Store ID: pbanhockgagggenencehbnadejlgchfc
"""

import asyncio
import json
import os
import shutil
import zipfile
from pathlib import Path
from typing import Optional

from loguru import logger


# Simplify Copilot extension ID from Chrome Web Store
SIMPLIFY_EXTENSION_ID = "pbanhockgagggenencehbnadejlgchfc"
EXTENSIONS_DIR = Path("data/extensions")
SIMPLIFY_DIR = EXTENSIONS_DIR / "simplify"
SIMPLIFY_PROFILE_DIR = Path("data/browser_profiles/simplify")


class ExtensionManager:
    """Manages Chrome extension downloads and profiles."""

    def __init__(self):
        EXTENSIONS_DIR.mkdir(parents=True, exist_ok=True)

    async def ensure_extension(self) -> Optional[str]:
        """Ensure Simplify extension is downloaded and ready.

        Returns:
            Path to unpacked extension directory, or None on failure
        """
        if self._is_extension_ready():
            logger.info(f"Simplify extension already available at {SIMPLIFY_DIR}")
            return str(SIMPLIFY_DIR)

        logger.info("Simplify extension not found, attempting download...")
        return await self._download_extension()

    def _is_extension_ready(self) -> bool:
        """Check if the extension is already unpacked and has a manifest."""
        manifest = SIMPLIFY_DIR / "manifest.json"
        return manifest.exists()

    async def _download_extension(self) -> Optional[str]:
        """Download Simplify Copilot from Chrome Web Store.

        Uses the CRX download URL format. Falls back to manual install instructions.

        Returns:
            Path to unpacked extension, or None
        """
        try:
            import httpx
        except ImportError:
            try:
                import requests as httpx
            except ImportError:
                logger.warning("Neither httpx nor requests installed — install manually")
                self._print_manual_instructions()
                return None

        # Chrome Web Store CRX download URL
        # Format: https://clients2.google.com/service/update2/crx?response=redirect&acceptformat=crx2,crx3&prodversion=120.0&x=id%3D{ID}%26installsource%3Dondemand%26uc
        crx_url = (
            f"https://clients2.google.com/service/update2/crx"
            f"?response=redirect&acceptformat=crx2,crx3&prodversion=120.0"
            f"&x=id%3D{SIMPLIFY_EXTENSION_ID}%26installsource%3Dondemand%26uc"
        )

        crx_path = EXTENSIONS_DIR / "simplify.crx"

        try:
            logger.info(f"Downloading Simplify Copilot extension...")

            if hasattr(httpx, 'AsyncClient'):
                # httpx
                async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
                    resp = await client.get(crx_url)
                    resp.raise_for_status()
                    crx_path.write_bytes(resp.content)
            else:
                # requests (sync fallback)
                resp = await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: httpx.get(crx_url, allow_redirects=True, timeout=30),
                )
                resp.raise_for_status()
                crx_path.write_bytes(resp.content)

            logger.info(f"Downloaded CRX ({crx_path.stat().st_size / 1024:.0f} KB)")

            # Unpack CRX (it's a zip with a CRX header)
            return self._unpack_crx(crx_path)

        except Exception as e:
            logger.warning(f"Auto-download failed: {e}")
            self._print_manual_instructions()
            return None

    def _unpack_crx(self, crx_path: Path) -> Optional[str]:
        """Unpack a CRX file into the extension directory.

        CRX3 format: magic(4) + version(4) + header_length(4) + header(N) + zip
        CRX2 format: magic(4) + version(4) + pub_key_len(4) + sig_len(4) + pub_key + sig + zip

        Returns:
            Path to unpacked extension, or None
        """
        try:
            data = crx_path.read_bytes()

            # Find the start of the ZIP archive (PK\x03\x04 signature)
            zip_start = data.find(b'PK\x03\x04')
            if zip_start == -1:
                logger.error("Not a valid CRX file — no ZIP signature found")
                return None

            # Write just the zip portion
            zip_path = EXTENSIONS_DIR / "simplify.zip"
            zip_path.write_bytes(data[zip_start:])

            # Clean and extract
            if SIMPLIFY_DIR.exists():
                shutil.rmtree(SIMPLIFY_DIR)
            SIMPLIFY_DIR.mkdir(parents=True)

            with zipfile.ZipFile(zip_path, 'r') as zf:
                zf.extractall(SIMPLIFY_DIR)

            # Verify manifest exists
            if not (SIMPLIFY_DIR / "manifest.json").exists():
                logger.error("Extracted extension has no manifest.json")
                return None

            # Cleanup
            crx_path.unlink(missing_ok=True)
            zip_path.unlink(missing_ok=True)

            logger.info(f"Extension unpacked to {SIMPLIFY_DIR}")
            return str(SIMPLIFY_DIR)

        except zipfile.BadZipFile:
            logger.error("CRX file contains invalid ZIP data")
            return None
        except Exception as e:
            logger.error(f"Failed to unpack CRX: {e}")
            return None

    def _print_manual_instructions(self):
        """Print instructions for manually installing the extension."""
        print("\n" + "=" * 60)
        print("  MANUAL EXTENSION INSTALL REQUIRED")
        print("=" * 60)
        print("  Auto-download failed. To install Simplify manually:")
        print()
        print("  1. Go to chrome://extensions/ in Chrome")
        print("  2. Enable 'Developer mode' (top right)")
        print("  3. Install 'Simplify Copilot' from Chrome Web Store")
        print("  4. Find the extension in chrome://extensions/")
        print("  5. Copy the extension's ID and directory path")
        print(f"  6. Copy the unpacked files to: {SIMPLIFY_DIR}/")
        print("     (must contain manifest.json)")
        print()
        print("  Or: download from https://simplify.jobs/copilot")
        print("=" * 60 + "\n")

    def get_extension_path(self) -> Optional[str]:
        """Get path to unpacked extension if it exists."""
        if self._is_extension_ready():
            return str(SIMPLIFY_DIR)
        return None

    def get_profile_dir(self) -> str:
        """Get the browser profile directory for Simplify.

        First time: user needs to log in to Simplify manually.
        After that: profile persists session data.
        """
        SIMPLIFY_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        return str(SIMPLIFY_PROFILE_DIR)

    async def setup_simplify_profile(self):
        """Interactive first-time setup: launch browser, let user log in to Simplify.

        This saves the Simplify profile so future runs are pre-authenticated.
        """
        ext_path = await self.ensure_extension()
        if not ext_path:
            logger.error("Cannot setup profile — extension not available")
            return

        print("\n" + "=" * 60)
        print("  SIMPLIFY COPILOT SETUP")
        print("=" * 60)
        print("  A browser will open with the Simplify extension loaded.")
        print("  Please:")
        print("    1. Click the Simplify extension icon")
        print("    2. Log in to your Simplify account")
        print("    3. Fill in your Simplify profile (name, email, etc.)")
        print("    4. Close the browser when done")
        print("=" * 60)

        from playwright.async_api import async_playwright
        profile_dir = self.get_profile_dir()

        pw = await async_playwright().start()
        try:
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=False,
                args=[
                    f"--load-extension={ext_path}",
                    f"--disable-extensions-except={ext_path}",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            page = await context.new_page()
            await page.goto("https://simplify.jobs")

            print("\n  Browser is open. Log in to Simplify, then close the browser.\n")

            # Wait for browser to close
            try:
                await context.wait_for_event("close", timeout=300_000)
            except Exception:
                pass

            logger.info(f"Simplify profile saved to {profile_dir}")
        finally:
            try:
                await pw.stop()
            except Exception:
                pass
