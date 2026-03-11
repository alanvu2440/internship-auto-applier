"""
Email Verifier

Reads verification codes from Gmail via IMAP.
Used when ATS systems send email confirmation codes during application.
"""

import imaplib
import email
import email.message
import email.utils
import re
import time
from email.header import decode_header
from typing import Optional
from loguru import logger


class EmailVerifier:
    """Reads verification/confirmation codes from Gmail inbox."""

    IMAP_SERVER = "imap.gmail.com"
    IMAP_PORT = 993

    # Patterns to extract verification codes from email bodies
    CODE_PATTERNS = [
        # Alphanumeric codes in bold/prominent HTML formatting (Greenhouse uses <h1>CODE</h1>)
        r"<(?:b|strong|h[1-6])>\s*([A-Za-z0-9]{4,10})\s*</(?:b|strong|h[1-6])>",
        # 4-8 digit numeric codes after keywords
        r"(?:verification|confirm|security|login|access|one.?time|otp|passcode|auth)\s*(?:code|number|pin|key)[:\s]*(\d{4,8})",
        r"(?:your|the|enter|use)\s+(?:code|verification|otp|pin)\s+(?:is|:)\s*(\d{4,8})",
        r"(\d{4,8})\s+is your\s+(?:verification|confirmation|security|login|one.?time)",
        # Alphanumeric codes after keywords (broader)
        r"(?:code|passcode)\s*(?:is|:)\s*([A-Za-z0-9]{4,10})",
        r"(?:paste|enter)\s+(?:this|the)\s+code.*?:\s*([A-Za-z0-9]{4,10})",
        # Generic standalone 4-8 digit codes (last resort)
        r"\b(\d{4,8})\b",
    ]

    # Subject patterns that indicate verification emails
    SUBJECT_PATTERNS = [
        r"verif",
        r"confirm",
        r"security\s*code",
        r"one.?time",
        r"otp",
        r"login\s*code",
        r"access\s*code",
        r"auth",
        r"sign.?in",
        r"email\s*verification",
    ]

    def __init__(self, gmail_email: str, app_password: str):
        self.email_address = gmail_email
        self.app_password = app_password.replace(" ", "")  # Remove spaces from app password
        self._conn: Optional[imaplib.IMAP4_SSL] = None

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

    def _decode_header_value(self, value: str) -> str:
        """Decode an email header value."""
        decoded_parts = decode_header(value)
        result = []
        for part, charset in decoded_parts:
            if isinstance(part, bytes):
                result.append(part.decode(charset or "utf-8", errors="replace"))
            else:
                result.append(part)
        return " ".join(result)

    def _extract_code_from_text(self, text: str) -> Optional[str]:
        """Extract a verification code from email text."""
        for pattern in self.CODE_PATTERNS[:-1]:  # Try specific patterns first
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                code = match.group(1)
                if 4 <= len(code) <= 8:
                    return code

        # Last resort: find any standalone 4-8 digit number
        # But only if the email is clearly a verification email
        numbers = re.findall(r"\b(\d{4,8})\b", text)
        if len(numbers) == 1:
            return numbers[0]

        return None

    def _is_verification_email(self, subject: str, sender: str) -> bool:
        """Check if an email is likely a verification code email."""
        subject_lower = subject.lower()
        for pattern in self.SUBJECT_PATTERNS:
            if re.search(pattern, subject_lower):
                return True

        # Also check sender for noreply/automated senders
        sender_lower = sender.lower()
        if any(x in sender_lower for x in ["noreply", "no-reply", "donotreply", "verify", "confirm", "security"]):
            return True

        return False

    def get_verification_code(
        self,
        sender_filter: Optional[str] = None,
        subject_filter: Optional[str] = None,
        max_age_seconds: int = 300,
        timeout: int = 120,
        poll_interval: int = 5,
    ) -> Optional[str]:
        """
        Wait for and extract a verification code from a recent email.

        Args:
            sender_filter: Only check emails from this sender (substring match)
            subject_filter: Only check emails with this in the subject (substring match)
            max_age_seconds: Only check emails received within this many seconds
            timeout: Max seconds to wait for the email
            poll_interval: Seconds between IMAP checks

        Returns:
            The verification code string, or None if not found within timeout
        """
        start_time = time.time()
        logger.info(f"Waiting for verification email (timeout={timeout}s, sender={sender_filter})")

        while time.time() - start_time < timeout:
            try:
                code = self._check_for_code(sender_filter, subject_filter, max_age_seconds)
                if code:
                    logger.info(f"Found verification code: {code}")
                    self._disconnect()
                    return code
            except Exception as e:
                logger.warning(f"Error checking email: {e}")
                self._conn = None  # Force reconnect

            time.sleep(poll_interval)

        logger.warning(f"No verification code found within {timeout}s")
        self._disconnect()
        return None

    def _check_for_code(
        self,
        sender_filter: Optional[str],
        subject_filter: Optional[str],
        max_age_seconds: int,
    ) -> Optional[str]:
        """Check inbox for a verification code email."""
        conn = self._connect()
        conn.select("INBOX")

        # Always search ALL recent emails (not just UNSEEN) because
        # verification code emails may be auto-marked as read by IMAP
        search_criteria = []

        if sender_filter:
            search_criteria.append(f'FROM "{sender_filter}"')

        if subject_filter:
            search_criteria.append(f'SUBJECT "{subject_filter}"')

        # Build IMAP search string
        if not search_criteria:
            search_str = "ALL"
        elif len(search_criteria) == 1:
            search_str = search_criteria[0]
        else:
            search_str = "(" + " ".join(search_criteria) + ")"

        status, message_ids = conn.search(None, search_str)
        if status != "OK" or not message_ids[0]:
            # Broadest fallback
            status, message_ids = conn.search(None, "ALL")
            if status != "OK" or not message_ids[0]:
                return None

        ids = message_ids[0].split()
        # Check most recent emails first (last 15 to catch recent verification codes)
        ids = ids[-15:]
        ids.reverse()

        cutoff_time = time.time() - max_age_seconds

        for msg_id in ids:
            try:
                status, msg_data = conn.fetch(msg_id, "(RFC822)")
                if status != "OK":
                    continue

                msg = email.message_from_bytes(msg_data[0][1])

                # Check date
                date_str = msg.get("Date", "")
                msg_date = email.utils.parsedate_to_datetime(date_str)
                if msg_date.timestamp() < cutoff_time:
                    continue  # Too old

                subject = self._decode_header_value(msg.get("Subject", ""))
                sender = self._decode_header_value(msg.get("From", ""))

                # Apply filters
                if sender_filter and sender_filter.lower() not in sender.lower():
                    continue
                if subject_filter and subject_filter.lower() not in subject.lower():
                    continue

                # Check if it looks like a verification email
                if not self._is_verification_email(subject, sender):
                    continue

                # Extract body text
                body = self._get_email_body(msg)
                if not body:
                    continue

                code = self._extract_code_from_text(body)
                if code:
                    logger.debug(f"Found code '{code}' in email from {sender}: {subject}")
                    return code

            except Exception as e:
                logger.debug(f"Error processing email {msg_id}: {e}")
                continue

        return None

    def _get_email_body(self, msg: email.message.Message) -> str:
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

    def get_verification_link(
        self,
        sender_filter: Optional[str] = None,
        subject_filter: Optional[str] = None,
        link_pattern: Optional[str] = None,
        max_age_seconds: int = 300,
        timeout: int = 90,
        poll_interval: int = 5,
    ) -> Optional[str]:
        """
        Wait for and extract a verification link from a recent email.

        Args:
            sender_filter: Only check emails from this sender (substring match)
            subject_filter: Only check emails with this in the subject
            link_pattern: Regex pattern for the verification link (if None, matches common patterns)
            max_age_seconds: Only check emails received within this many seconds
            timeout: Max seconds to wait for the email
            poll_interval: Seconds between IMAP checks

        Returns:
            The verification link URL, or None if not found within timeout
        """
        start_time = time.time()
        logger.info(f"Waiting for verification link email (timeout={timeout}s, sender={sender_filter})")

        while time.time() - start_time < timeout:
            try:
                link = self._check_for_link(sender_filter, subject_filter, link_pattern, max_age_seconds)
                if link:
                    logger.info(f"Found verification link: {link[:80]}...")
                    self._disconnect()
                    return link
            except Exception as e:
                logger.warning(f"Error checking email for link: {e}")
                self._conn = None

            time.sleep(poll_interval)

        logger.warning(f"No verification link found within {timeout}s")
        self._disconnect()
        return None

    def _check_for_link(
        self,
        sender_filter: Optional[str],
        subject_filter: Optional[str],
        link_pattern: Optional[str],
        max_age_seconds: int,
    ) -> Optional[str]:
        """Check inbox for a verification link email."""
        conn = self._connect()
        conn.select("INBOX")

        search_criteria = []
        if sender_filter:
            search_criteria.append(f'FROM "{sender_filter}"')
        if subject_filter:
            search_criteria.append(f'SUBJECT "{subject_filter}"')

        if not search_criteria:
            search_str = "ALL"
        elif len(search_criteria) == 1:
            search_str = search_criteria[0]
        else:
            search_str = "(" + " ".join(search_criteria) + ")"

        status, message_ids = conn.search(None, search_str)
        if status != "OK" or not message_ids[0]:
            status, message_ids = conn.search(None, "ALL")
            if status != "OK" or not message_ids[0]:
                return None

        ids = message_ids[0].split()
        ids = ids[-15:]
        ids.reverse()

        cutoff_time = time.time() - max_age_seconds

        # Default link patterns for verification emails
        default_patterns = [
            r'href=["\']?(https?://[^\s"\'<>]*(?:verif|confirm|activate|validate)[^\s"\'<>]*)',
            r'(https?://[^\s"\'<>]*(?:verif|confirm|activate|validate)[^\s"\'<>]*)',
        ]

        for msg_id in ids:
            try:
                status, msg_data = conn.fetch(msg_id, "(RFC822)")
                if status != "OK":
                    continue

                msg = email.message_from_bytes(msg_data[0][1])

                date_str = msg.get("Date", "")
                msg_date = email.utils.parsedate_to_datetime(date_str)
                if msg_date.timestamp() < cutoff_time:
                    continue

                subject = self._decode_header_value(msg.get("Subject", ""))
                sender = self._decode_header_value(msg.get("From", ""))

                if sender_filter and sender_filter.lower() not in sender.lower():
                    continue
                if subject_filter and subject_filter.lower() not in subject.lower():
                    continue

                if not self._is_verification_email(subject, sender):
                    continue

                body = self._get_email_body(msg)
                if not body:
                    continue

                # Try custom pattern first
                if link_pattern:
                    match = re.search(link_pattern, body, re.IGNORECASE)
                    if match:
                        return match.group(1) if match.lastindex else match.group(0)

                # Try default patterns
                for pattern in default_patterns:
                    match = re.search(pattern, body, re.IGNORECASE)
                    if match:
                        url = match.group(1) if match.lastindex else match.group(0)
                        # Clean up trailing punctuation
                        url = url.rstrip('.,;:)"\'')
                        return url

            except Exception as e:
                logger.debug(f"Error processing email {msg_id} for link: {e}")
                continue

        return None

    def test_connection(self) -> bool:
        """Test that Gmail IMAP connection works."""
        try:
            conn = self._connect()
            conn.select("INBOX")
            status, messages = conn.search(None, "ALL")
            total = len(messages[0].split()) if messages[0] else 0
            logger.info(f"Gmail connection OK — {total} emails in inbox")
            self._disconnect()
            return True
        except Exception as e:
            logger.error(f"Gmail connection failed: {e}")
            self._disconnect()
            return False
