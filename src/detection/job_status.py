"""
Job Status Detection

Shared constants and functions for detecting whether a job posting is
closed/unavailable or whether an application was submitted successfully.

Used by ATS handlers (base, greenhouse, smartrecruiters, etc.) to avoid
duplicating long lists of text indicators.
"""

from typing import List


# ---------------------------------------------------------------------------
# Closed / unavailable job indicators
# Merged from base.py is_job_closed() and smartrecruiters.py _is_closed_content()
# ---------------------------------------------------------------------------
CLOSED_JOB_INDICATORS: List[str] = [
    "position has been filled",
    "no longer accepting",
    "job has been closed",
    "this position is closed",
    "this job is no longer available",
    "job posting has expired",
    "requisition has been closed",
    "role has been filled",
    "sorry, we couldn't find",
    "page not found",
    "job not found",
    "this posting has closed",
    "no longer available",
    "this job has been removed",
    "application is no longer active",
    "oops, you've gone too far",                       # SmartRecruiters 404
    "sorry, this job has expired",                      # SmartRecruiters expired
    "this job has expired",                             # SmartRecruiters generic
    "the page you are looking for doesn't exist",       # Workday 404
    "page you are looking for doesn",                   # Workday 404 variant
    "something went wrong",                             # Workday error state
    "this position is no longer",
    "this role is no longer",
    "job is no longer posted",
    "posting has been removed",
    "this opening has been filled",
    "you have already submitted",
    "already applied to this",
]


# ---------------------------------------------------------------------------
# Failure indicators checked BEFORE success (prevent false positives)
# From base.py is_application_complete()
# ---------------------------------------------------------------------------
FAILURE_INDICATORS: List[str] = [
    "no longer accepting",
    "position is closed",
    "position has been filled",
    "no longer available",
    "this job has expired",
    "this job is no longer",
    "flagged as possible spam",
    "flagged as spam",
    "suspicious activity",
    "already applied",
    "already submitted",
    "you have already submitted an application",
    "duplicate application",
    "previously applied",
    "application already exists",
    "page not found",
    "page you are looking for doesn't exist",
    "page you are looking for doesn",
    "job is no longer posted",
    "this position is no longer",
    "this role has been filled",
    "this requisition has been closed",
    "posting has been removed",
    # Workday auth pages — never mark as success
    "password requirements:",
    "verify new password",
    "create your candidate home account",
]


# ---------------------------------------------------------------------------
# Success indicators (application submitted)
# From base.py is_application_complete()
# ---------------------------------------------------------------------------
SUCCESS_INDICATORS: List[str] = [
    "thank you for applying",
    "thanks for applying",
    "application received",
    "application submitted",
    "successfully applied",
    "we've received your application",
    "application complete",
    "thank you for your interest in",
    "thank you for submitting",
]


def is_job_closed(text: str) -> bool:
    """Check if page text indicates the job is closed or unavailable.

    Args:
        text: Page body text (will be lowercased internally).

    Returns:
        True if any closed-job indicator is found in the text.
    """
    text_lower = text.lower()
    return any(indicator in text_lower for indicator in CLOSED_JOB_INDICATORS)


def is_application_complete(text: str) -> bool:
    """Check if page text indicates a successful application submission.

    Checks failure indicators first to prevent false positives (e.g. a page
    that says "thank you for your interest" but also "position is closed").

    Args:
        text: Page body text (will be lowercased internally).

    Returns:
        True if success indicators are found and no failure indicators match.
    """
    text_lower = text.lower()

    # Check failure indicators first — these override success text
    for indicator in FAILURE_INDICATORS:
        if indicator in text_lower:
            return False

    # Check success indicators
    for indicator in SUCCESS_INDICATORS:
        if indicator in text_lower:
            return True

    # Fallback: bare "thank you" only on short pages (likely a confirmation page)
    if "thank you" in text_lower and len(text_lower.split()) < 200:
        return True

    return False
