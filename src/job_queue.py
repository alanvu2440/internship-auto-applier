"""
Job Queue Module

Manages the queue of jobs to apply to using SQLite.
Handles priority (new jobs first), tracking, and deduplication.
"""

import asyncio
import aiosqlite
from dataclasses import asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import List, Optional
from loguru import logger

from job_parser import Job, ATSType


class JobStatus(Enum):
    """Job application status."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    APPLIED = "applied"
    FAILED = "failed"
    SKIPPED = "skipped"


class JobQueue:
    """SQLite-based job queue for managing applications."""

    def __init__(self, db_path: str = "data/jobs.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db: Optional[aiosqlite.Connection] = None

    async def initialize(self):
        """Initialize the database and create tables."""
        self._db = await aiosqlite.connect(str(self.db_path))
        self._db.row_factory = aiosqlite.Row

        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE NOT NULL,
                company TEXT NOT NULL,
                role TEXT NOT NULL,
                location TEXT,
                ats_type TEXT,
                status TEXT DEFAULT 'pending',
                priority INTEGER DEFAULT 0,
                attempts INTEGER DEFAULT 0,
                last_attempt TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                applied_at TIMESTAMP,
                error_message TEXT,
                raw_text TEXT,
                verification_tier TEXT DEFAULT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
            CREATE INDEX IF NOT EXISTS idx_jobs_priority ON jobs(priority DESC);
            CREATE INDEX IF NOT EXISTS idx_jobs_ats ON jobs(ats_type);

            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                success INTEGER DEFAULT 1,
                notes TEXT,
                FOREIGN KEY (job_id) REFERENCES jobs(id)
            );

            CREATE TABLE IF NOT EXISTS email_responses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER,
                message_id TEXT UNIQUE NOT NULL,
                sender TEXT NOT NULL,
                sender_email TEXT NOT NULL,
                subject TEXT NOT NULL,
                received_at TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'other',
                company_matched TEXT,
                snippet TEXT,
                raw_body_hash TEXT,
                processed_at TEXT NOT NULL DEFAULT (datetime('now')),
                notified INTEGER DEFAULT 0,
                FOREIGN KEY (job_id) REFERENCES jobs(id)
            );
        """)
        await self._db.commit()

        # Add response_status column if missing (safe for existing DBs)
        try:
            await self._db.execute("SELECT response_status FROM jobs LIMIT 0")
        except Exception:
            await self._db.execute(
                "ALTER TABLE jobs ADD COLUMN response_status TEXT DEFAULT NULL"
            )
            await self._db.commit()
        logger.info(f"Database initialized at {self.db_path}")

    async def close(self):
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    async def add_job(self, job: Job, priority: int = 0) -> bool:
        """
        Add a job to the queue.

        Args:
            job: Job object to add
            priority: Higher = more urgent (new jobs get higher priority)

        Returns:
            True if added, False if already exists or already applied
        """
        try:
            # DUPLICATE PROTECTION: Check if we already applied to same company+role
            cursor = await self._db.execute(
                """SELECT id, status, verification_tier FROM jobs
                   WHERE company = ? AND role = ? AND status IN ('applied', 'skipped')""",
                (job.company, job.role),
            )
            existing = await cursor.fetchone()
            if existing:
                logger.debug(f"Skipping duplicate: {job.company} — {job.role} (already {existing[1]}, tier={existing[2]})")
                return False

            await self._db.execute(
                """
                INSERT OR IGNORE INTO jobs
                (url, company, role, location, ats_type, priority, raw_text)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.url,
                    job.company,
                    job.role,
                    job.location,
                    job.ats_type.value,
                    priority,
                    job.raw_text,
                ),
            )
            await self._db.commit()

            # Check if it was actually inserted
            cursor = await self._db.execute(
                "SELECT changes()"
            )
            row = await cursor.fetchone()
            return row[0] > 0

        except Exception as e:
            logger.error(f"Failed to add job: {e}")
            return False

    async def add_jobs(self, jobs: List[Job], priority: int = 0) -> int:
        """
        Add multiple jobs to the queue.

        Returns:
            Number of jobs actually added (excluding duplicates)
        """
        added = 0
        for job in jobs:
            if await self.add_job(job, priority):
                added += 1
        logger.info(f"Added {added}/{len(jobs)} jobs to queue")
        return added

    async def get_job_by_url(self, url: str) -> Optional[dict]:
        """Look up a job by its URL. Returns the job dict or None."""
        cursor = await self._db.execute(
            "SELECT * FROM jobs WHERE url = ?", (url,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def add_job_url(self, url: str, ats_type: str) -> int:
        """Insert a minimal job entry by URL and return its ID."""
        cursor = await self._db.execute(
            "INSERT INTO jobs (url, company, role, ats_type, status) VALUES (?, ?, ?, ?, 'pending')",
            (url, "Unknown", "Unknown", ats_type),
        )
        await self._db.commit()
        return cursor.lastrowid

    async def get_next_job(self, ats_type: Optional[ATSType] = None, max_attempts: int = 2, url_patterns: list = None) -> Optional[dict]:
        """
        Get the next job to process.

        Args:
            ats_type: Optionally filter by ATS type
            max_attempts: Skip jobs that have failed this many times
            url_patterns: List of SQL LIKE patterns to filter URLs (e.g. ['%amat.wd1%', '%parsons.wd5%'])

        Returns:
            Job dict or None if queue is empty
        """
        # Prioritize: SmartRecruiters > Lever > Greenhouse > Ashby > Workday > Others
        ats_priority = """
            CASE ats_type
                WHEN 'ashby' THEN 10
                WHEN 'lever' THEN 9
                WHEN 'greenhouse' THEN 8
                WHEN 'smartrecruiters' THEN 7
                WHEN 'bamboohr' THEN 5
                WHEN 'jobvite' THEN 4
                WHEN 'workday' THEN 2
                WHEN 'icims' THEN 1
                ELSE 3
            END
        """

        # Never re-apply to a company+role that's already triple_verified
        query = f"""
            SELECT * FROM jobs
            WHERE status = 'pending' AND attempts < ?
            AND NOT EXISTS (
                SELECT 1 FROM jobs j2
                WHERE j2.company = jobs.company AND j2.role = jobs.role
                AND j2.verification_tier = 'triple_verified'
            )
        """
        params = [max_attempts]

        # Skip Workday/iCIMS by default (login walls, low success rate)
        # unless explicitly filtering for them
        if ats_type:
            query += " AND ats_type = ?"
            params.append(ats_type.value)
        else:
            query += " AND ats_type NOT IN ('workday', 'icims')"

        if url_patterns:
            or_clauses = " OR ".join(["url LIKE ?" for _ in url_patterns])
            query += f" AND ({or_clauses})"
            params.extend(url_patterns)

        software_role_boost = """
            CASE
                WHEN lower(role) LIKE '%software%' OR lower(role) LIKE '%swe %'
                     OR lower(role) LIKE '%developer%' OR lower(role) LIKE '%data engineer%'
                     OR lower(role) LIKE '%backend%' OR lower(role) LIKE '%frontend%'
                     OR lower(role) LIKE '%full stack%' OR lower(role) LIKE '%ml %'
                     OR lower(role) LIKE '%machine learning%'
                THEN 0
                ELSE 1
            END
        """
        query += f" ORDER BY priority DESC, {software_role_boost}, {ats_priority} DESC, attempts ASC, created_at DESC LIMIT 1"

        cursor = await self._db.execute(query, params)
        row = await cursor.fetchone()

        if row:
            # Mark as in progress
            await self._db.execute(
                "UPDATE jobs SET status = 'in_progress', last_attempt = ? WHERE id = ?",
                (datetime.now().isoformat(), row["id"]),
            )
            await self._db.commit()
            return dict(row)

        return None

    async def mark_applied(self, job_id: int, notes: str = ""):
        """Mark a job as successfully applied with triple_verified tier."""
        await self._db.execute(
            """
            UPDATE jobs
            SET status = 'applied', applied_at = ?, attempts = attempts + 1,
                verification_tier = 'triple_verified'
            WHERE id = ?
            """,
            (datetime.now().isoformat(), job_id),
        )

        await self._db.execute(
            "INSERT INTO applications (job_id, success, notes) VALUES (?, 1, ?)",
            (job_id, notes),
        )

        await self._db.commit()
        logger.info(f"Marked job {job_id} as applied")

    async def mark_failed(self, job_id: int, error: str = "", retry: bool = True, max_attempts: int = 2):
        """Mark a job as failed. Permanently fails after max_attempts."""
        # Get current attempts
        cursor = await self._db.execute("SELECT attempts FROM jobs WHERE id = ?", (job_id,))
        row = await cursor.fetchone()
        current_attempts = row[0] if row else 0

        # Don't retry if we've hit the limit (aligned with get_next_job default)
        if current_attempts >= max_attempts - 1:
            retry = False

        status = "pending" if retry else "failed"
        await self._db.execute(
            """
            UPDATE jobs
            SET status = ?, error_message = ?, attempts = attempts + 1, last_attempt = ?
            WHERE id = ?
            """,
            (status, error, datetime.now().isoformat(), job_id),
        )
        await self._db.commit()
        if retry:
            logger.warning(f"Marked job {job_id} as failed (will retry): {error}")
        else:
            logger.warning(f"Marked job {job_id} as permanently failed: {error}")

    async def mark_skipped(self, job_id: int, reason: str = ""):
        """Mark a job as skipped (won't retry)."""
        await self._db.execute(
            "UPDATE jobs SET status = 'skipped', error_message = ? WHERE id = ?",
            (reason, job_id),
        )
        await self._db.commit()
        logger.info(f"Marked job {job_id} as skipped: {reason}")

    async def reset_job(self, job_id: int):
        """Reset a job back to pending (used after dry runs)."""
        await self._db.execute(
            "UPDATE jobs SET status = 'pending' WHERE id = ?",
            (job_id,),
        )
        await self._db.commit()

    async def get_applied_urls(self) -> set:
        """Get set of all applied job URLs."""
        cursor = await self._db.execute(
            "SELECT url FROM jobs WHERE status IN ('applied', 'skipped')"
        )
        rows = await cursor.fetchall()
        return {row["url"] for row in rows}

    async def get_all_urls(self) -> set:
        """Get set of all job URLs in database."""
        cursor = await self._db.execute("SELECT url FROM jobs")
        rows = await cursor.fetchall()
        return {row["url"] for row in rows}

    async def get_stats(self) -> dict:
        """Get queue statistics."""
        cursor = await self._db.execute("""
            SELECT
                status,
                COUNT(*) as count
            FROM jobs
            GROUP BY status
        """)
        rows = await cursor.fetchall()

        stats = {row["status"]: row["count"] for row in rows}

        # Get total
        cursor = await self._db.execute("SELECT COUNT(*) FROM jobs")
        total = (await cursor.fetchone())[0]

        stats["total"] = total
        return stats

    async def get_pending_count(self) -> int:
        """Get number of pending jobs."""
        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM jobs WHERE status = 'pending'"
        )
        return (await cursor.fetchone())[0]

    async def reset_stuck_jobs(self, timeout_minutes: int = 30):
        """Reset jobs that have been in_progress too long."""
        await self._db.execute(
            """
            UPDATE jobs
            SET status = 'pending'
            WHERE status = 'in_progress'
            AND datetime(last_attempt) < datetime('now', ? || ' minutes')
            """,
            (-timeout_minutes,),
        )
        await self._db.commit()

    async def export_to_csv(self, filepath: str):
        """Export all applications to CSV."""
        import csv

        cursor = await self._db.execute("""
            SELECT
                company, role, location, url, status,
                ats_type, applied_at, created_at, error_message
            FROM jobs
            ORDER BY applied_at DESC NULLS LAST, created_at DESC
        """)
        rows = await cursor.fetchall()

        with open(filepath, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "Company", "Role", "Location", "URL", "Status",
                "ATS", "Applied At", "Added At", "Notes"
            ])
            for row in rows:
                writer.writerow(dict(row).values())

        logger.info(f"Exported {len(rows)} jobs to {filepath}")


async def main():
    """Test the job queue."""
    from job_parser import Job, ATSType

    queue = JobQueue("data/test_jobs.db")
    await queue.initialize()

    # Add test jobs
    test_jobs = [
        Job(
            company="Google",
            role="Software Engineering Intern",
            location="Mountain View, CA",
            url="https://boards.greenhouse.io/google/123",
            ats_type=ATSType.GREENHOUSE,
        ),
        Job(
            company="Meta",
            role="Software Engineer Intern",
            location="Menlo Park, CA",
            url="https://jobs.lever.co/meta/456",
            ats_type=ATSType.LEVER,
        ),
    ]

    added = await queue.add_jobs(test_jobs, priority=10)
    print(f"Added {added} jobs")

    # Get stats
    stats = await queue.get_stats()
    print(f"Queue stats: {stats}")

    # Get next job
    job = await queue.get_next_job()
    if job:
        print(f"Next job: {job['company']} - {job['role']}")
        await queue.mark_applied(job["id"], "Test application")

    # Final stats
    stats = await queue.get_stats()
    print(f"Final stats: {stats}")

    await queue.close()


if __name__ == "__main__":
    asyncio.run(main())
