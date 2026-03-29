"""
Email Response Tracker

Scans Gmail for responses to job applications and categorizes them into
a pipeline: follow_up → assessment → interview_invite → offer (or rejection).
Reuses Gmail IMAP credentials from EmailVerifier.

Classification uses a two-pass confidence scoring system:
  Pass 1 — Wide net: scan for ALL signal keywords, accumulate scores per category
  Pass 2 — Filter: apply negative signals, context checks, and minimum thresholds
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

# ── Confidence-scored classification rules ─────────────────────────────
# Each keyword has a weight. Subject matches get 2x weight.
# Final score must meet a minimum threshold for each category.

SCORED_RULES = {
    "offer": {
        "keywords": {
            "offer letter": 10,
            "pleased to extend": 10,
            "congratulations on your offer": 10,
            "extending an offer": 10,
            "formal offer": 8,
            "compensation package": 7,
            "accept your offer": 9,
            "offer of employment": 10,
            "start date and compensation": 8,
            "sign your offer": 9,
        },
        "threshold": 8,
        "subject_multiplier": 2.0,
    },
    "interview_invite": {
        "keywords": {
            # Scheduling-specific (high signal — someone is actively booking time)
            "schedule your interview": 8,
            "schedule an interview": 8,
            "interview invitation": 9,
            "interview confirmation": 8,
            "interview scheduled": 8,
            "interview slot": 7,
            "your interview is": 8,
            "confirm your interview": 8,
            # Format-specific
            "phone screen": 6,
            "technical interview": 7,
            "video interview": 6,
            "on-site interview": 7,
            "virtual interview": 6,
            "panel interview": 7,
            "behavioral interview": 6,
            "final round": 6,
            "superday": 8,
            # Progression signals (moderate — could be template filler)
            "next round": 4,
            "next step": 3,
            "next steps in": 3,
            "move forward": 3,
            "moved to the next": 5,
            "pleased to invite": 7,
            "like to invite you": 7,
            "meet the team": 5,
            # Booking tools (high signal if in subject or body link)
            "calendly.com": 7,
            "goodtime.io": 7,
            "greenhouse.io/interviews": 7,
            "book a time": 5,
            "pick a time": 5,
            "select a time": 5,
            "availability": 3,
        },
        "threshold": 7,
        "subject_multiplier": 2.5,
    },
    "assessment": {
        "keywords": {
            "coding challenge": 8,
            "hackerrank": 9,
            "codesignal": 9,
            "online assessment": 8,
            "technical assessment": 8,
            "take-home": 7,
            "coding test": 8,
            "skills assessment": 7,
            "oa link": 8,
            "complete the assessment": 8,
            "complete your assessment": 8,
            "leetcode": 8,
            "karat": 8,
            "codility": 8,
            "hirevue": 7,
            "assessment invitation": 8,
            "pymetrics": 7,
        },
        "threshold": 7,
        "subject_multiplier": 2.0,
    },
    "rejection": {
        "keywords": {
            "unfortunately": 4,
            "not moving forward": 8,
            "other candidates": 5,
            "position filled": 7,
            "decided not to proceed": 8,
            "will not be moving": 8,
            "unable to offer": 7,
            "not been selected": 8,
            "decided to move forward with": 7,
            "pursue other candidates": 7,
            "not a fit": 5,
            "regret to inform": 7,
            "after careful consideration": 5,
            "competitive applicant pool": 4,
            "will not be advancing": 8,
            "no longer being considered": 8,
            "not able to move forward": 8,
            "have decided to go with": 7,
        },
        "threshold": 6,
        "subject_multiplier": 2.0,
    },
    "follow_up": {
        "keywords": {
            "application received": 5,
            "thank you for applying": 6,
            "under review": 5,
            "we received your application": 6,
            "application has been submitted": 6,
            "application confirmation": 5,
            "we have received": 5,
            "reviewing your application": 5,
            "keep you updated": 4,
            "thank you for your interest": 5,
            "we will review": 4,
            "application successfully submitted": 6,
        },
        "threshold": 5,
        "subject_multiplier": 1.5,
    },
}

# Negative signals — if present, DEMOTE from interview_invite/offer to lower category
NEGATIVE_SIGNALS = {
    # These indicate the email is NOT a real interview invite
    "interview_invite": [
        # Rejection language (overrides interview keywords in templates)
        "unfortunately",
        "not moving forward",
        "not been selected",
        "will not be moving",
        "unable to offer",
        "decided not to proceed",
        "regret to inform",
        "no longer being considered",
        "we have decided to go with",
        "role has been closed",
        "position has been filled",
        "decided to close this role",
        "pausing this role",
        # Generic confirmation template filler
        "your application has been received",
        "we will review it and get back",
        "our team will review",
        "we'll be in touch if there's a match",
        "if your qualifications match",
        "should your background be a match",
        "if we feel your experience is a match",
    ],
    "offer": [
        "unfortunately",
        "not moving forward",
        "not been selected",
        "regret to inform",
    ],
}

# Auto-reply patterns — emails matching 2+ of these are confirmation emails,
# NOT interview invites or assessments
AUTO_REPLY_INDICATORS = [
    "your application has been received",
    "we will review it",
    "thank you for applying",
    "thank you for your interest",
    "application received",
    "we have received your application",
    "reviewing your application",
    "application confirmation",
    "application successfully submitted",
    "thank you for applying",
    "we've received your application",
    "we appreciate your interest",
    "our team will review your",
    "we will carefully review",
]  # All lowercase — matched against lowercased body text

# ATS noreply domains — for these, rely on subject matching instead of sender domain
ATS_NOREPLY_DOMAINS = [
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "smartrecruiters.com",
    "myworkdayjobs.com",
    "myworkday.com",
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
                confidence_score REAL DEFAULT 0.0,
                FOREIGN KEY (job_id) REFERENCES jobs(id)
            );
        """)
        # Add confidence_score column if missing (upgrade path)
        try:
            db.execute("SELECT confidence_score FROM email_responses LIMIT 0")
        except sqlite3.OperationalError:
            db.execute("ALTER TABLE email_responses ADD COLUMN confidence_score REAL DEFAULT 0.0")
            db.commit()
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
        Uses word-boundary matching to avoid substring false positives
        (e.g. "Tive" matching "Intuitive").
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
            # Single distinctive token — WORD BOUNDARY match only (4+ chars)
            # Word boundaries prevent "Tive" matching inside "Intuitive"
            elif any(
                len(t) >= 4 and re.search(r'\b' + re.escape(t) + r'\b', sender_lower)
                for t in core_tokens
            ):
                score += 5
                has_strong_match = True
            elif any(
                len(t) >= 4 and re.search(r'\b' + re.escape(t) + r'\b', subject_lower)
                for t in core_tokens
            ):
                score += 4
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

    # ── Classification (confidence-scored) ────────────────────────

    @staticmethod
    def _classify_email(subject: str, body: str) -> Tuple[str, float]:
        """Classify email into a response category using confidence scoring.

        Two-pass system:
          Pass 1 — Accumulate scores for every category from keyword hits.
          Pass 2 — Apply negative signals and context filters; pick the
                    highest-scoring category that meets its threshold.

        Returns (category, confidence_score).
        """
        subject_lower = subject.lower()

        # Strip HTML tags from body for cleaner matching, but keep raw for link detection
        body_text = re.sub(r'<[^>]+>', ' ', body.lower())
        body_text = re.sub(r'\s+', ' ', body_text).strip()

        # Reply chains: "Re:" subjects carry historical keywords from the
        # original email.  The actual new content is in the body, so we
        # reduce subject weight for replies.
        is_reply = bool(re.match(r'^re:\s', subject_lower))

        # ── Pass 1: Score accumulation ──

        category_scores: Dict[str, float] = {}

        for category, rules in SCORED_RULES.items():
            score = 0.0
            base_multiplier = rules["subject_multiplier"]
            # Replies: subject keywords get 0.5x instead of full multiplier
            # because the subject line is just the forwarded original topic
            multiplier = 0.5 if is_reply else base_multiplier

            for keyword, weight in rules["keywords"].items():
                # Subject match — stronger signal (unless reply)
                if keyword in subject_lower:
                    score += weight * multiplier
                # Body match
                if keyword in body_text:
                    score += weight

            category_scores[category] = score

        # ── Pass 2: Auto-reply detection ──
        # Count auto-reply indicators in body
        auto_reply_count = sum(
            1 for pattern in AUTO_REPLY_INDICATORS if pattern in body_text
        )
        is_auto_reply = auto_reply_count >= 2

        # If it's clearly an auto-reply, heavily penalize interview/assessment/offer
        # These categories should NOT fire on generic "thank you for applying" emails
        if is_auto_reply:
            category_scores["interview_invite"] *= 0.15  # 85% penalty
            category_scores["assessment"] *= 0.15
            category_scores["offer"] *= 0.15
            # Boost follow_up since that's what auto-replies actually are
            category_scores["follow_up"] += 5

        # ── Pass 2b: Negative signal check ──
        # If the body contains rejection language, crush interview/offer scores.
        # Also cross-boost: if we find rejection signals while checking interview,
        # boost the rejection score — the email is ABOUT a rejection.
        for category, neg_patterns in NEGATIVE_SIGNALS.items():
            if category_scores.get(category, 0) > 0:
                neg_hits = sum(1 for p in neg_patterns if p in body_text)
                if neg_hits > 0:
                    # Aggressive penalty: 1 hit = 60%, 2 hits = 85%, 3+ = 95%
                    penalty = min(0.95, 0.60 + (neg_hits - 1) * 0.25)
                    category_scores[category] *= (1 - penalty)
                    # Cross-boost: if rejection patterns found in an interview email,
                    # the email is probably a rejection
                    if category == "interview_invite":
                        category_scores["rejection"] = max(
                            category_scores.get("rejection", 0),
                            7.0 + neg_hits * 2  # ensure it clears threshold
                        )

        # ── Pass 2c: ATS noreply sender penalty ──
        # Emails from myworkday.com, greenhouse.io etc. are almost always
        # auto-generated; real interview invites come from actual recruiters
        # Exception: if the subject itself is very specific
        # (We can't check sender here, but we can check for common noreply patterns in body)
        noreply_in_body = any(
            domain in body_text for domain in ["noreply", "no-reply", "do-not-reply", "donotreply"]
        )
        if noreply_in_body:
            category_scores["interview_invite"] *= 0.5
            category_scores["offer"] *= 0.5

        # ── Pick winner ──
        # Sort by score descending, then check threshold
        sorted_cats = sorted(category_scores.items(), key=lambda x: x[1], reverse=True)

        for category, score in sorted_cats:
            threshold = SCORED_RULES[category]["threshold"]
            if score >= threshold:
                return category, round(score, 1)

        return "other", 0.0

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

                # 6. Get body and classify with confidence scoring
                body = self._get_email_body(msg)
                category, confidence = self._classify_email(subject, body)

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
                         raw_body_hash, processed_at, confidence_score)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        confidence,
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
                    "confidence": confidence,
                    "snippet": snippet,
                }
                new_responses.append(response_record)

                logger.info(
                    f"[{category.upper()}] (conf={confidence}) "
                    f"{matched_job['company']} — \"{subject}\""
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
                conf = resp.get("confidence_score", resp.get("confidence", 0))

                print(f"  {i}. {company} — \"{subject}\"")
                line = f"     Received: {received}"
                if role:
                    line += f"  |  Role: {role}"
                if conf:
                    line += f"  |  Conf: {conf}"
                print(line)

            if len(items) > 10:
                print(f"  ... and {len(items) - 10} more")

        # Pipeline summary
        total_applied = stats.get("total_applied", 0)
        total_responses = stats.get("total_responses", 0)
        interviews = stats.get("by_category", {}).get("interview_invite", 0)
        assessments = stats.get("by_category", {}).get("assessment", 0)
        offers = stats.get("by_category", {}).get("offer", 0)
        rejections = stats.get("by_category", {}).get("rejection", 0)
        response_pct = stats.get("response_rate_pct", 0)
        interview_pct = (interviews / total_applied * 100) if total_applied > 0 else 0

        print()
        print(f"Pipeline: {total_applied} applied → {total_responses} responses "
              f"({response_pct}%) → {interviews} interviews ({interview_pct:.1f}%)")
        if assessments:
            print(f"          {assessments} assessments pending")
        if rejections:
            print(f"          {rejections} rejections")
        if offers:
            print(f"          {offers} offers!")

        new_count = stats.get("new_matches", 0)
        if new_count > 0:
            print(f"\nNew this scan: {new_count} response(s)")

        print("=" * 66)
        print()

    # ── Re-classify existing entries ─────────────────────────────

    def reclassify(self) -> Dict:
        """Re-run classification on all stored emails.

        Useful after updating classification rules. Re-reads snippets
        from DB and re-classifies, updating categories and job statuses.
        """
        db = self._get_db()
        cursor = db.execute("""
            SELECT id, subject, snippet, category as old_category,
                   company_matched, job_id
            FROM email_responses
        """)
        rows = cursor.fetchall()
        logger.info(f"Re-classifying {len(rows)} stored emails...")

        changes = []
        for row in rows:
            row = dict(row)
            # Re-classify using snippet as body (we don't store full body)
            new_cat, confidence = self._classify_email(
                row["subject"], row["snippet"] or ""
            )

            if new_cat != row["old_category"]:
                changes.append({
                    "id": row["id"],
                    "company": row["company_matched"],
                    "subject": row["subject"],
                    "old": row["old_category"],
                    "new": new_cat,
                    "confidence": confidence,
                })
                db.execute(
                    "UPDATE email_responses SET category = ?, confidence_score = ? WHERE id = ?",
                    (new_cat, confidence, row["id"]),
                )

            else:
                # Just update confidence score
                db.execute(
                    "UPDATE email_responses SET confidence_score = ? WHERE id = ?",
                    (confidence, row["id"]),
                )

        db.commit()

        # Rebuild job response_status from scratch
        # Reset all to NULL, then replay all emails in chronological order
        db.execute("UPDATE jobs SET response_status = NULL WHERE status = 'applied'")
        db.commit()

        cursor = db.execute("""
            SELECT job_id, category FROM email_responses
            ORDER BY received_at ASC
        """)
        for row in cursor.fetchall():
            self._update_response_status(db, row["job_id"], row["category"])

        db.close()

        if changes:
            logger.info(f"Re-classified {len(changes)} emails:")
            for c in changes:
                logger.info(
                    f"  {c['company']}: {c['old']} → {c['new']} "
                    f"(conf={c['confidence']}) \"{c['subject'][:60]}\""
                )
        else:
            logger.info("No classification changes")

        return {"changes": changes, "total_reviewed": len(rows)}

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
