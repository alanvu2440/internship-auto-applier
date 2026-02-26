"""
Question Verification System

Human-in-the-loop verification for job application answers.
Uncertain answers get queued for review instead of being submitted blindly.

Schema: verified_answers table in data/verified_answers.db
  - question_text: The question as seen on the form
  - answer: The approved answer
  - confidence: 0-100 confidence score
  - source: Where the answer came from (config/ai/manual/generic_fallback)
  - verified_by: "human" or "auto"
  - verified_at: ISO timestamp
  - field_type: text/textarea/select/radio/checkbox
  - options: JSON list of options if dropdown/radio
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any
from loguru import logger


class QuestionVerifier:
    """Manages verified answers and human review queue."""

    CONFIDENCE_CONFIG = 100       # Config pattern match — always trusted
    CONFIDENCE_VERIFIED = 100     # Human-verified — always trusted
    CONFIDENCE_CACHE = 90         # Previously cached AI answer — high trust
    CONFIDENCE_AI = 80            # Fresh AI answer — good but reviewable
    CONFIDENCE_GENERIC = 0        # Generic fallback — MUST be reviewed

    def __init__(self, db_path: str = "data/verified_answers.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        """Create the verified_answers table if it doesn't exist."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS verified_answers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    question_text TEXT NOT NULL,
                    question_normalized TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    confidence INTEGER NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT 'unknown',
                    verified_by TEXT NOT NULL DEFAULT 'pending',
                    verified_at TEXT,
                    field_type TEXT DEFAULT 'text',
                    options TEXT DEFAULT '[]',
                    company TEXT DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_question_normalized
                ON verified_answers(question_normalized)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS review_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    question_text TEXT NOT NULL,
                    question_normalized TEXT NOT NULL,
                    proposed_answer TEXT NOT NULL,
                    source TEXT NOT NULL,
                    confidence INTEGER NOT NULL DEFAULT 0,
                    field_type TEXT DEFAULT 'text',
                    options TEXT DEFAULT '[]',
                    company TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    reviewed_at TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            conn.commit()

    @staticmethod
    def _normalize(question: str) -> str:
        """Normalize question text for matching."""
        import re
        q = question.lower().strip()
        q = re.sub(r'\s+', ' ', q)
        q = re.sub(r'[*:?\.]$', '', q).strip()
        return q

    def get_verified_answer(
        self, question: str, field_type: str = "text", options: Optional[List[str]] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Look up a verified answer for a question.

        Returns dict with 'answer', 'confidence', 'source' if found, else None.
        Only returns answers verified by human or auto-verified config answers.
        """
        normalized = self._normalize(question)
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            # Exact match on normalized question
            row = conn.execute(
                """SELECT answer, confidence, source, verified_by
                   FROM verified_answers
                   WHERE question_normalized = ?
                     AND verified_by != 'pending'
                   ORDER BY confidence DESC, verified_at DESC
                   LIMIT 1""",
                (normalized,)
            ).fetchone()

            if row:
                logger.debug(f"Verified answer found: '{question[:40]}...' -> '{row['answer'][:40]}...' (confidence={row['confidence']})")
                return {
                    "answer": row["answer"],
                    "confidence": row["confidence"],
                    "source": f"verified_{row['source']}",
                    "verified_by": row["verified_by"],
                }
        return None

    def store_verified_answer(
        self,
        question: str,
        answer: str,
        confidence: int,
        source: str,
        verified_by: str = "auto",
        field_type: str = "text",
        options: Optional[List[str]] = None,
        company: str = "",
    ):
        """Store a verified answer in the database."""
        normalized = self._normalize(question)
        with sqlite3.connect(self.db_path) as conn:
            # Check if we already have this exact question+answer verified
            existing = conn.execute(
                """SELECT id FROM verified_answers
                   WHERE question_normalized = ? AND answer = ? AND verified_by != 'pending'""",
                (normalized, answer)
            ).fetchone()
            if existing:
                return  # Already stored

            conn.execute(
                """INSERT INTO verified_answers
                   (question_text, question_normalized, answer, confidence, source,
                    verified_by, verified_at, field_type, options, company)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    question.strip(),
                    normalized,
                    answer,
                    confidence,
                    source,
                    verified_by,
                    datetime.now().isoformat() if verified_by != "pending" else None,
                    field_type,
                    json.dumps(options or []),
                    company,
                )
            )
            conn.commit()

    def queue_for_review(
        self,
        question: str,
        proposed_answer: str,
        source: str,
        confidence: int = 0,
        field_type: str = "text",
        options: Optional[List[str]] = None,
        company: str = "",
    ):
        """Add a question to the human review queue."""
        normalized = self._normalize(question)
        with sqlite3.connect(self.db_path) as conn:
            # Don't queue duplicates
            existing = conn.execute(
                """SELECT id FROM review_queue
                   WHERE question_normalized = ? AND status = 'pending'""",
                (normalized,)
            ).fetchone()
            if existing:
                return

            # Also don't queue if already verified
            verified = conn.execute(
                """SELECT id FROM verified_answers
                   WHERE question_normalized = ? AND verified_by != 'pending'""",
                (normalized,)
            ).fetchone()
            if verified:
                return

            conn.execute(
                """INSERT INTO review_queue
                   (question_text, question_normalized, proposed_answer, source,
                    confidence, field_type, options, company)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    question.strip(),
                    normalized,
                    proposed_answer,
                    source,
                    confidence,
                    field_type,
                    json.dumps(options or []),
                    company,
                )
            )
            conn.commit()
            logger.info(f"Queued for review: '{question[:50]}...' (source={source})")

    def get_pending_reviews(self) -> List[Dict[str, Any]]:
        """Get all questions pending human review."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT id, question_text, proposed_answer, source, confidence,
                          field_type, options, company, created_at
                   FROM review_queue
                   WHERE status = 'pending'
                   ORDER BY created_at ASC"""
            ).fetchall()
            return [dict(row) for row in rows]

    def approve_answer(self, review_id: int, answer: Optional[str] = None):
        """
        Approve a queued question. Optionally override the proposed answer.

        Moves from review_queue to verified_answers with verified_by='human'.
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM review_queue WHERE id = ?", (review_id,)
            ).fetchone()
            if not row:
                logger.warning(f"Review ID {review_id} not found")
                return False

            final_answer = answer if answer is not None else row["proposed_answer"]

            # Store as verified
            self.store_verified_answer(
                question=row["question_text"],
                answer=final_answer,
                confidence=self.CONFIDENCE_VERIFIED,
                source=row["source"],
                verified_by="human",
                field_type=row["field_type"],
                options=json.loads(row["options"]) if row["options"] else [],
                company=row["company"],
            )

            # Mark review as done
            conn.execute(
                """UPDATE review_queue
                   SET status = 'approved', reviewed_at = ?
                   WHERE id = ?""",
                (datetime.now().isoformat(), review_id)
            )
            conn.commit()
            logger.info(f"Approved review #{review_id}: '{row['question_text'][:50]}...'")
            return True

    def reject_answer(self, review_id: int):
        """Reject a queued question — it won't be used."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """UPDATE review_queue
                   SET status = 'rejected', reviewed_at = ?
                   WHERE id = ?""",
                (datetime.now().isoformat(), review_id)
            )
            conn.commit()
            logger.info(f"Rejected review #{review_id}")
            return True

    def get_stats(self) -> Dict[str, int]:
        """Get verification system stats."""
        with sqlite3.connect(self.db_path) as conn:
            verified_count = conn.execute(
                "SELECT COUNT(*) FROM verified_answers WHERE verified_by != 'pending'"
            ).fetchone()[0]
            pending_count = conn.execute(
                "SELECT COUNT(*) FROM review_queue WHERE status = 'pending'"
            ).fetchone()[0]
            approved_count = conn.execute(
                "SELECT COUNT(*) FROM review_queue WHERE status = 'approved'"
            ).fetchone()[0]
            rejected_count = conn.execute(
                "SELECT COUNT(*) FROM review_queue WHERE status = 'rejected'"
            ).fetchone()[0]
            return {
                "verified_answers": verified_count,
                "pending_review": pending_count,
                "approved": approved_count,
                "rejected": rejected_count,
            }

    def auto_verify_config_answers(self, ai_answerer):
        """
        Bulk-verify all config-pattern answers as trusted.
        Call this once after init to seed the verified DB with config answers.
        """
        count = 0
        for entry in ai_answerer.session_answers:
            if entry.get("source") in ("config", "config_option"):
                self.store_verified_answer(
                    question=entry["question"],
                    answer=entry["answer"],
                    confidence=self.CONFIDENCE_CONFIG,
                    source=entry["source"],
                    verified_by="auto",
                )
                count += 1
        if count:
            logger.info(f"Auto-verified {count} config-based answers")
