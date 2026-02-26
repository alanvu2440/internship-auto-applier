"""
GitHub Watcher Module

Monitors the SimplifyJobs/Summer2026-Internships repository for new job postings.
Polls the repository at regular intervals and detects changes.
"""

import asyncio
import hashlib
from datetime import datetime
from typing import Optional, Callable
import httpx
from loguru import logger


class GitHubWatcher:
    """Watches a GitHub repository for changes."""

    SIMPLIFY_JOBS_URL = "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/README.md"
    SIMPLIFY_JOBS_API = "https://api.github.com/repos/SimplifyJobs/Summer2026-Internships/commits?path=README.md&per_page=1"

    def __init__(
        self,
        poll_interval: int = 300,  # 5 minutes
        on_change: Optional[Callable] = None,
    ):
        """
        Initialize the GitHub watcher.

        Args:
            poll_interval: Seconds between polls (default 5 minutes)
            on_change: Callback function when changes detected
        """
        self.poll_interval = poll_interval
        self.on_change = on_change
        self.last_content_hash: Optional[str] = None
        self.last_commit_sha: Optional[str] = None
        self.running = False
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=30.0,
                headers={
                    "User-Agent": "InternshipAutoApplier/1.0",
                    "Accept": "application/vnd.github.v3+json",
                },
            )
        return self._client

    async def fetch_readme(self) -> str:
        """Fetch the current README content."""
        client = await self._get_client()
        response = await client.get(self.SIMPLIFY_JOBS_URL)
        response.raise_for_status()
        return response.text

    async def get_latest_commit(self) -> Optional[str]:
        """Get the SHA of the latest commit affecting README.md."""
        client = await self._get_client()
        try:
            response = await client.get(self.SIMPLIFY_JOBS_API)
            response.raise_for_status()
            commits = response.json()
            if commits:
                return commits[0]["sha"]
        except Exception as e:
            logger.warning(f"Failed to get latest commit: {e}")
        return None

    def _hash_content(self, content: str) -> str:
        """Generate hash of content for comparison."""
        return hashlib.sha256(content.encode()).hexdigest()

    async def check_for_changes(self) -> tuple[bool, Optional[str]]:
        """
        Check if the repository has been updated.

        Returns:
            Tuple of (has_changes, new_content)
        """
        try:
            # First try commit-based detection (faster)
            latest_commit = await self.get_latest_commit()
            if latest_commit and latest_commit != self.last_commit_sha:
                content = await self.fetch_readme()
                content_hash = self._hash_content(content)

                # Verify actual content changed (not just commit metadata)
                if content_hash != self.last_content_hash:
                    self.last_commit_sha = latest_commit
                    self.last_content_hash = content_hash
                    logger.info(f"New changes detected! Commit: {latest_commit[:8]}")
                    return True, content

            # Fallback to content-based detection
            content = await self.fetch_readme()
            content_hash = self._hash_content(content)

            if self.last_content_hash is None:
                # First run - initialize
                self.last_content_hash = content_hash
                self.last_commit_sha = latest_commit
                logger.info("Initialized watcher with current content")
                return True, content  # Return content for initial processing

            if content_hash != self.last_content_hash:
                self.last_content_hash = content_hash
                self.last_commit_sha = latest_commit
                logger.info("Content changes detected!")
                return True, content

            return False, None

        except Exception as e:
            logger.error(f"Error checking for changes: {e}")
            return False, None

    async def watch(self):
        """Start watching for changes."""
        self.running = True
        logger.info(f"Starting GitHub watcher (poll interval: {self.poll_interval}s)")

        while self.running:
            has_changes, content = await self.check_for_changes()

            if has_changes and content and self.on_change:
                try:
                    await self.on_change(content)
                except Exception as e:
                    logger.error(f"Error in change callback: {e}")

            await asyncio.sleep(self.poll_interval)

    def stop(self):
        """Stop watching."""
        self.running = False
        logger.info("GitHub watcher stopped")

    async def close(self):
        """Clean up resources."""
        self.stop()
        if self._client:
            await self._client.aclose()
            self._client = None


async def main():
    """Test the GitHub watcher."""
    async def on_change(content: str):
        logger.info(f"Received {len(content)} bytes of content")
        # Count job entries (rough estimate)
        job_count = content.count("| **")
        logger.info(f"Approximately {job_count} job entries found")

    watcher = GitHubWatcher(poll_interval=60, on_change=on_change)

    try:
        # Single check
        has_changes, content = await watcher.check_for_changes()
        if content:
            await on_change(content)
    finally:
        await watcher.close()


if __name__ == "__main__":
    asyncio.run(main())
