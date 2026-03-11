"""
Email Response Tracker

Scans Gmail for responses to job applications and categorizes them into
a pipeline: follow_up → assessment → interview_invite → offer (or rejection).
Reuses Gmail IMAP credentials from EmailVerifier.
"""

from __future__ import annotations

import hashlib
import imaplib
import email
import email.message
import email.utils
import json
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from loguru import logger


# Response status priority — higher index = more important, only upgrades
RESPONSE_PRIORITY = {
    "rejection": 0,
    "follow_up": 1,
    "other": 2,
    "assessment": 3,
    "interview_invite": 4,
    "offer": 5,
}

# Classification keywords — checked against subject + body
CLASSIFICATION_RULES = {
    "offer": {
        "strong": [
            "offer letter",
            "pleased to extend",
            "congratulations on your offer",
            "extending an offer",
            "formal offer",
            "compensation package",
            "accept your offer",
            "offer of employment",
        ],
    },
    "interview_invite": {
        "strong": [
            "schedule an interview",
            "schedule your interview",
            "interview invitation",
            "phone screen",
            "technical interview",
            "next round",
            "interview with",
            "video interview",
            "on-site interview",
            "final round",
            "meet the team",
            "panel interview",
            "behavioral interview",
            "superday",
            "hiring manager",
            "calendly",
            "book a time",
            "schedule a call",
            "interview slot",
        ],
    },
    "assessment": {
        "strong": [
            "coding challenge",
            "hackerrank",
            "codesignal",
            "online assessment",
            "technical assessment",
            "take-home",
            "coding test",
            "skills assessment",
            "oa link",
            "complete the assessment",
            "leetcode",
            "karat",
            "codility",
            "complete your assessment",
        ],
    },
    "rejection": {
        "strong": [
            "unfortunately",
            "not moving forward",
            "other candidates",
            "position filled",
            "decided not to proceed",
            "will not be moving",
            "unable to offer",
            "not been selected",
            "decided to move forward with",
            "pursue other candidates",
            "not a fit",
            "regret to inform",
            "after careful consideration",
            "competitive applicant pool",
            "will not be advancing",
        ],
    },
    "follow_up": {
        "strong": [
            "application received",
            "thank you for applying",
            "under review",
            "we received your application",
            "application has been submitted",
            "application confirmation",
            "we have received",
            "reviewing your application",
            "keep you updated",
        ],
    },
}

# ATS noreply domains — for these, rely on subject matching instead of sender domain
ATS_NOREPLY_DOMAINS = [
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "smartrecruiters.com",
    "myworkdayjobs.com",
    "icims.com",
    "applytojob.com",
    "jobvite.com",
    "taleo.net",
]

# Tokens to strip from company names during normalization
COMPANY_SUFFIXES = [
    "inc", "inc.", "llc", "llc.", "corp", "corp.", "corporation",
    "co", "co.", "company", "ltd", "ltd.", "limited", "group",
    "holdings", "technologies", "technology", "tech", "labs",
    "software", "solutions", "services", "systems",
]


class EmailResponseTracker:
    """Scans Gmail for application responses and categorizes them."""

    IMAP_SERVER = "imap.gmail.com"
    IMAP_PORT = 993

    def __init__(self, gmail_email: str, app_password: str, db_path: str = "data/jobs.db"):
        self.email_address = gmail_email
        self.app_password = app_password.replace(" ", "")
        self.db_path = db_path
        self._conn: Optional[imaplib.IMAP4_SSL] = None

    # ── IMAP connection ──────────────────────────────────────────

    def _connect(self) -> imaplib.IMAP4_SSL:
        """Connect to Gmail IMAP."""
        if self._conn:
            try:
                self._conn.noop()
                return self._conn
            except Exception:
                self._conn = None

        conn = imaplib.IMAP4_SSL(self.IMAP_SERVER, self.IMAP_PORT)
        conn.login(self.email_address, self.app_password)
        self._conn = conn
        logger.debug("Connected to Gmail IMAP")
        return conn

    def _disconnect(self):
        """Disconnect from Gmail IMAP."""
        if self._conn:
            try:
                self._conn.logout()
            except Exception:
                pass
            self._conn = None

    # ── DB helpers ───────────────────────────────────────────────

    def _get_db(self) -> sqlite3.Connection:
        """Get a synchronous SQLite connection."""
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        # Ensure email_responses table exists
        db.executescript("""
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
        # Add response_status column if missing
        try:
            db.execute("SELECT response_status FROM jobs LIMIT 0")
        except sqlite3.OperationalError:
            db.execute("ALTER TABLE jobs ADD COLUMN response_status TEXT DEFAULT NULL")
            db.commit()
        return db

    def _get_applied_companies(self, db: sqlite3.Connection) -> List[dict]:
        """Load applied companies from jobs table."""
        cursor = db.execute("""
            SELECT id, company, role, url, applied_at
            FROM jobs
            WHERE status = 'applied'
            ORDER BY applied_at DESC
        """)
        return [dict(row) for row in cursor.fetchall()]

    def _get_processed_message_ids(self, db: sqlite3.Connection) -> set:
        """Get already-processed message IDs for dedup."""
        cursor = db.execute("SELECT message_id FROM email_responses")
        return {row["message_id"] for row in cursor.fetchall()}

    # ── Company matching ─────────────────────────────────────────

    @staticmethod
    def _normalize_company(name: str) -> List[str]:
        """Normalize company name into matchable tokens."""
        name = name.lower().strip()
        # Remove common suffixes
        for suffix in COMPANY_SUFFIXES:
            name = re.sub(r'\b' + re.escape(suffix) + r'\b', '', name)
        # Tokenize
        tokens = re.findall(r'[a-z0-9]+', name)
        return [t for t in tokens if len(t) > 1]

    def _match_company(
        self,
        sender_name: str,
        sender_email: str,
        subject: str,
        applied_companies: List[dict],
    ) -> Optional[dict]:
        """Match an email against applied companies.

        Returns the best-matching job dict, or None.
        Uses strict matching: company name must appear in sender OR subject
        as a recognizable phrase, not just overlapping common tokens.
        """
        sender_lower = sender_name.lower()
        email_lower = sender_email.lower()
        subject_lower = subject.lower()
        email_domain = email_lower.split("@")[-1] if "@" in email_lower else ""

        # Check if this is an ATS noreply — if so, skip domain matching
        is_ats_noreply = any(d in email_domain for d in ATS_NOREPLY_DOMAINS)

        best_match = None
        best_score = 0

        for job in applied_companies:
            company_name = job["company"].lower().strip()
            company_tokens = self._normalize_company(job["company"])
            if not company_tokens:
                continue

            score = 0
            has_strong_match = False

            # Strong match: full company name (or major portion) in sender or subject
            # Build a "core name" — first 2-3 meaningful tokens joined
            core_tokens = company_tokens[:3]
            core_name = " ".join(core_tokens)

            # Check exact company name match (strongest signal)
            if company_name in sender_lower or company_name in subject_lower:
                score += 10
                has_strong_match = True
            # Check core name match
            elif len(core_tokens) >= 2 and core_name in sender_lower:
                score += 8
                has_strong_match = True
            elif len(core_tokens) >= 2 and core_name in subject_lower:
                score += 6
                has_strong_match = True
            # Single distinctive token (4+ chars) in sender name
            elif any(len(t) >= 4 and t in sender_lower for t in core_tokens):
                score += 5
                has_strong_match = True
            # Check email domain for company name (not ATS noreply)
            elif not is_ats_noreply and any(
                len(t) >= 4 and t in email_domain for t in core_tokens
            ):
                score += 4
                has_strong_match = True

            # Without a strong match, skip this company
            if not has_strong_match:
                continue

            # Bonus: role keywords in subject
            role_tokens = re.findall(r'[a-z]+', job["role"].lower())
            role_hits = sum(1 for t in role_tokens if len(t) > 3 and t in subject_lower)
            if role_hits > 0:
                score += role_hits

            if score > best_score:
                best_score = score
                best_match = job

        return best_match

    # ── Classification ───────────────────────────────────────────

    @staticmethod
    def _classify_email(subject: str, body: str) -> str:
        """Classify email into a response category using keyword matching.

        Uses subject-weighted matching: keywords in subject are stronger signals
        than keywords buried in body template text.
        """
        subject_lower = subject.lower()
        body_lower = body.lower()
        text = subject_lower + " " + body_lower

        # If body is clearly an auto-reply confirmation, classify as follow_up
        # BEFORE checking interview/assessment keywords (which may appear in templates)
        follow_up_signals = [
            "your application has been received",
            "we will review it",
            "thank you for applying",
            "application received",
            "we have received your application",
            "reviewing your application",
        ]
        is_auto_reply = sum(1 for s in follow_up_signals if s in body_lower) >= 2

        # Check categories in priority order (offer first, follow_up last)
        for category in ["offer", "interview_invite", "assessment", "rejection", "follow_up"]:
            rules = CLASSIFICATION_RULES[category]
            for keyword in rules["strong"]:
                if keyword in subject_lower:
                    # Subject match is always strong
                    return category
                if keyword in body_lower:
                    # Body match: if this is an auto-reply, don't promote to
                    # interview/assessment just because the template mentions it
                    if is_auto_reply and category in ("interview_invite", "assessment"):
                        continue
                    return category

        return "other"

    # ── Email parsing ────────────────────────────────────────────

    @staticmethod
    def _decode_header_value(value: str) -> str:
        """Decode an email header value."""
        decoded_parts = decode_header(value)
        result = []
        for part, charset in decoded_parts:
            if isinstance(part, bytes):
                result.append(part.decode(charset or "utf-8", errors="replace"))
            else:
                result.append(part)
        return " ".join(result)

    @staticmethod
    def _extract_email_address(from_header: str) -> str:
        """Extract bare email address from From header."""
        match = re.search(r'<([^>]+)>', from_header)
        if match:
            return match.group(1).lower()
        # No angle brackets — the whole thing might be an email
        if "@" in from_header:
            return from_header.strip().lower()
        return from_header.lower()

    @staticmethod
    def _extract_sender_name(from_header: str) -> str:
        """Extract display name from From header."""
        match = re.match(r'^"?([^"<]+)"?\s*<', from_header)
        if match:
            return match.group(1).strip()
        return from_header.split("@")[0] if "@" in from_header else from_header

    @staticmethod
    def _get_email_body(msg: email.message.Message) -> str:
        """Extract text body from email message."""
        body_parts = []

        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                if content_type in ("text/plain", "text/html"):
                    try:
                        payload = part.get_payload(decode=True)
                        charset = part.get_content_charset() or "utf-8"
                        text = payload.decode(charset, errors="replace")
                        body_parts.append(text)
                    except Exception:
                        continue
        else:
            try:
                payload = msg.get_payload(decode=True)
                charset = msg.get_content_charset() or "utf-8"
                body_parts.append(payload.decode(charset, errors="replace"))
            except Exception:
                pass

        return "\n".join(body_parts)

    # ── Core scan ────────────────────────────────────────────────

    def scan(
        self,
        days: int = 30,
        limit: int = 500,
        category_filter: Optional[str] = None,
    ) -> Dict:
        """Scan Gmail for application responses.

        Args:
            days: How many days back to search.
            limit: Max emails to fetch.
            category_filter: Only return results of this category.

        Returns:
            Summary dict with categorized responses.
        """
        db = self._get_db()

        # 1. Load applied companies
        applied_companies = self._get_applied_companies(db)
        if not applied_companies:
            logger.warning("No applied jobs found in database")
            db.close()
            return {"error": "No applied jobs", "responses": [], "stats": {}}

        logger.info(f"Loaded {len(applied_companies)} applied companies")

        # 2. Load already-processed message IDs
        processed_ids = self._get_processed_message_ids(db)
        logger.info(f"Already processed {len(processed_ids)} emails")

        # 3. Connect to Gmail and search
        try:
            conn = self._connect()
            conn.select("INBOX")
        except Exception as e:
            logger.error(f"Failed to connect to Gmail: {e}")
            db.close()
            return {"error": str(e), "responses": [], "stats": {}}

        since_date = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
        status, message_ids = conn.search(None, f'SINCE {since_date}')

        if status != "OK" or not message_ids[0]:
            logger.info("No emails found in date range")
            self._disconnect()
            db.close()
            return {"responses": [], "stats": {"total_scanned": 0, "matched": 0}}

        ids = message_ids[0].split()
        # Most recent first, cap at limit
        ids = ids[-limit:]
        ids.reverse()

        logger.info(f"Scanning {len(ids)} emails from last {days} days...")

        # 4. Process each email
        new_responses = []
        scanned = 0

        for msg_id in ids:
            try:
                status, msg_data = conn.fetch(msg_id, "(RFC822)")
                if status != "OK":
                    continue

                msg = email.message_from_bytes(msg_data[0][1])
                scanned += 1

                # Get Message-ID for dedup
                message_id = msg.get("Message-ID", "").strip()
                if not message_id:
                    # Generate a fallback ID from headers
                    date_str = msg.get("Date", "")
                    from_str = msg.get("From", "")
                    message_id = hashlib.md5(
                        f"{date_str}{from_str}{msg.get('Subject', '')}".encode()
                    ).hexdigest()

                if message_id in processed_ids:
                    continue

                # Parse headers
                raw_from = self._decode_header_value(msg.get("From", ""))
                subject = self._decode_header_value(msg.get("Subject", ""))
                date_str = msg.get("Date", "")

                sender_name = self._extract_sender_name(raw_from)
                sender_email = self._extract_email_address(raw_from)

                # Parse date
                try:
                    received_dt = email.utils.parsedate_to_datetime(date_str)
                    received_at = received_dt.isoformat()
                except Exception:
                    received_at = datetime.now().isoformat()

                # 5. Match against applied companies
                matched_job = self._match_company(
                    sender_name, sender_email, subject, applied_companies
                )

                if not matched_job:
                    continue

                # 6. Get body and classify
                body = self._get_email_body(msg)
                category = self._classify_email(subject, body)

                # Apply category filter
                if category_filter and category != category_filter:
                    continue

                # Create snippet (first 300 chars of plaintext)
                snippet = re.sub(r'<[^>]+>', '', body)  # strip HTML tags
                snippet = re.sub(r'\s+', ' ', snippet).strip()[:300]

                body_hash = hashlib.md5(body.encode()).hexdigest()

                # 7. Save to email_responses table
                try:
                    db.execute("""
                        INSERT OR IGNORE INTO email_responses
                        (job_id, message_id, sender, sender_email, subject,
                         received_at, category, company_matched, snippet,
                         raw_body_hash, processed_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        matched_job["id"],
                        message_id,
                        sender_name,
                        sender_email,
                        subject,
                        received_at,
                        category,
                        matched_job["company"],
                        snippet,
                        body_hash,
                        datetime.now().isoformat(),
                    ))
                    db.commit()
                except sqlite3.IntegrityError:
                    # Already processed (race condition)
                    continue

                # 8. Update jobs.response_status (only upgrade)
                self._update_response_status(db, matched_job["id"], category)

                response_record = {
                    "job_id": matched_job["id"],
                    "company": matched_job["company"],
                    "role": matched_job["role"],
                    "sender": sender_name,
                    "sender_email": sender_email,
                    "subject": subject,
                    "received_at": received_at,
                    "category": category,
                    "snippet": snippet,
                }
                new_responses.append(response_record)

                logger.info(
                    f"[{category.upper()}] {matched_job['company']} — \"{subject}\""
                )

            except Exception as e:
                logger.debug(f"Error processing email {msg_id}: {e}")
                continue

        self._disconnect()

        # 9. Build summary
        summary = self._build_summary(db, new_responses, scanned, applied_companies, category_filter)

        # 10. Save summary to file
        summary_path = Path("data/response_summary.json")
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        with open(summary_path, "w") as f:
            json.dump(summary, f, indent=2, default=str)
        logger.info(f"Summary saved to {summary_path}")

        # 11. Print terminal summary
        self._print_summary(summary)

        db.close()
        return summary

    def _update_response_status(self, db: sqlite3.Connection, job_id: int, category: str):
        """Update jobs.response_status — only upgrades (higher priority wins)."""
        cursor = db.execute(
            "SELECT response_status FROM jobs WHERE id = ?", (job_id,)
        )
        row = cursor.fetchone()
        if not row:
            return

        current = row["response_status"]
        current_priority = RESPONSE_PRIORITY.get(current, -1)
        new_priority = RESPONSE_PRIORITY.get(category, -1)

        if new_priority > current_priority:
            db.execute(
                "UPDATE jobs SET response_status = ? WHERE id = ?",
                (category, job_id),
            )
            db.commit()
            logger.debug(f"Job {job_id} response_status: {current} → {category}")

    def _build_summary(
        self,
        db: sqlite3.Connection,
        new_responses: List[dict],
        scanned: int,
        applied_companies: List[dict],
        category_filter: Optional[str],
    ) -> Dict:
        """Build the full summary dict."""
        # Get all responses from DB (not just new ones)
        query = "SELECT * FROM email_responses ORDER BY received_at DESC"
        cursor = db.execute(query)
        all_responses = [dict(row) for row in cursor.fetchall()]

        # Categorize
        by_category: Dict[str, list] = {}
        for resp in all_responses:
            cat = resp["category"]
            if category_filter and cat != category_filter:
                continue
            by_category.setdefault(cat, []).append(resp)

        # Stats
        total_applied = len(applied_companies)
        total_responses = len(all_responses)
        response_rate = (total_responses / total_applied * 100) if total_applied > 0 else 0

        stats = {
            "scan_date": datetime.now().isoformat(),
            "emails_scanned": scanned,
            "new_matches": len(new_responses),
            "total_applied": total_applied,
            "total_responses": total_responses,
            "response_rate_pct": round(response_rate, 1),
            "by_category": {cat: len(items) for cat, items in by_category.items()},
        }

        return {
            "stats": stats,
            "responses": by_category,
            "new_responses": new_responses,
        }

    def _print_summary(self, summary: Dict):
        """Print formatted terminal summary."""
        stats = summary["stats"]
        by_category = summary.get("responses", {})

        print()
        print("=" * 66)
        print("          EMAIL RESPONSE TRACKER — SUMMARY")
        print("=" * 66)

        # Display order
        display_order = [
            ("offer", "OFFERS"),
            ("interview_invite", "INTERVIEWS"),
            ("assessment", "ASSESSMENTS"),
            ("follow_up", "FOLLOW-UPS"),
            ("rejection", "REJECTIONS"),
            ("other", "OTHER"),
        ]

        for category, label in display_order:
            items = by_category.get(category, [])
            if not items:
                continue

            print(f"\n--- {label} ({len(items)}) ---")
            for i, resp in enumerate(items[:10], 1):  # Show max 10 per category
                company = resp.get("company_matched", resp.get("company", "Unknown"))
                subject = resp.get("subject", "")
                received = resp.get("received_at", "")[:10]
                role = resp.get("role", "")

                print(f"  {i}. {company} — \"{subject}\"")
                if received:
                    line = f"     Received: {received}"
                    if role:
                        line += f"  |  Role: {role}"
                    print(line)

            if len(items) > 10:
                print(f"  ... and {len(items) - 10} more")

        # Pipeline summary
        total_applied = stats.get("total_applied", 0)
        total_responses = stats.get("total_responses", 0)
        interviews = stats.get("by_category", {}).get("interview_invite", 0)
        assessments = stats.get("by_category", {}).get("assessment", 0)
        offers = stats.get("by_category", {}).get("offer", 0)
        response_pct = stats.get("response_rate_pct", 0)
        interview_pct = (interviews / total_applied * 100) if total_applied > 0 else 0

        print()
        print(f"Pipeline: {total_applied} applied → {total_responses} responses "
              f"({response_pct}%) → {interviews} interviews ({interview_pct:.1f}%)")
        if assessments:
            print(f"          {assessments} assessments pending")
        if offers:
            print(f"          {offers} offers!")

        new_count = stats.get("new_matches", 0)
        if new_count > 0:
            print(f"\nNew this scan: {new_count} response(s)")

        print("=" * 66)
        print()

    # ── Continuous tracking ──────────────────────────────────────

    def track(self, interval_hours: int = 48, days: int = 7):
        """Run continuous tracking at the given interval.

        Args:
            interval_hours: Hours between scans.
            days: Lookback window per scan cycle.
        """
        logger.info(f"Starting continuous tracking — every {interval_hours}h, {days}d lookback")
        print(f"\nTracking email responses every {interval_hours} hours...")
        print("Press Ctrl+C to stop.\n")

        try:
            while True:
                logger.info("Running scheduled scan...")
                self.scan(days=days)
                logger.info(f"Next scan in {interval_hours} hours")
                time.sleep(interval_hours * 3600)
        except KeyboardInterrupt:
            logger.info("Tracking stopped by user")
            print("\nTracking stopped.")
