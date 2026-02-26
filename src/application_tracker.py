"""
Application Tracker

Tracks detailed information about each job application for reporting.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, asdict, field
from loguru import logger


@dataclass
class ApplicationRecord:
    """Record of a single job application."""
    job_id: int
    company: str
    role: str
    url: str
    ats_type: str
    timestamp: str
    status: str  # "submitted", "failed", "skipped"
    fields_filled: Dict[str, str] = field(default_factory=dict)
    fields_missed: Dict[str, str] = field(default_factory=dict)
    questions_answered: Dict[str, str] = field(default_factory=dict)  # question -> answer
    validation_errors: List[str] = field(default_factory=list)
    error_message: Optional[str] = None
    screenshot_path: Optional[str] = None


class ApplicationTracker:
    """Tracks and reports on job applications."""

    def __init__(self, report_dir: str = "logs"):
        self.report_dir = Path(report_dir)
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.session_records: List[ApplicationRecord] = []
        self.session_start = datetime.now()

    def record_application(
        self,
        job_data: Dict[str, Any],
        status: str,
        fields_filled: Dict[str, str] = None,
        fields_missed: Dict[str, str] = None,
        questions_answered: Dict[str, str] = None,
        validation_errors: List[str] = None,
        error_message: str = None,
        screenshot_path: str = None
    ) -> ApplicationRecord:
        """Record a single application attempt."""
        record = ApplicationRecord(
            job_id=job_data.get("id", 0),
            company=job_data.get("company", "Unknown"),
            role=job_data.get("role", "Unknown"),
            url=job_data.get("url", ""),
            ats_type=job_data.get("ats_type", "unknown"),
            timestamp=datetime.now().isoformat(),
            status=status,
            fields_filled=fields_filled or {},
            fields_missed=fields_missed or {},
            questions_answered=questions_answered or {},
            validation_errors=validation_errors or [],
            error_message=error_message,
            screenshot_path=screenshot_path
        )
        self.session_records.append(record)

        # Also save immediately to a running log file (in case of crash)
        self._append_to_running_log(record)

        return record

    def _append_to_running_log(self, record: ApplicationRecord):
        """Append record to running log file for crash recovery."""
        try:
            log_file = self.report_dir / "running_application_log.jsonl"
            with open(log_file, "a") as f:
                f.write(json.dumps(asdict(record)) + "\n")
        except Exception as e:
            logger.warning(f"Could not write to running log: {e}")

    def get_session_summary(self) -> Dict[str, Any]:
        """Get summary of current session."""
        submitted = [r for r in self.session_records if r.status == "submitted"]
        failed = [r for r in self.session_records if r.status == "failed"]
        skipped = [r for r in self.session_records if r.status == "skipped"]

        # Calculate common missed fields
        missed_field_counts = {}
        for record in failed:
            for field_name in record.fields_missed.keys():
                missed_field_counts[field_name] = missed_field_counts.get(field_name, 0) + 1

        return {
            "session_start": self.session_start.isoformat(),
            "session_duration_minutes": (datetime.now() - self.session_start).total_seconds() / 60,
            "total_attempts": len(self.session_records),
            "submitted": len(submitted),
            "failed": len(failed),
            "skipped": len(skipped),
            "success_rate": f"{len(submitted) / len(self.session_records) * 100:.1f}%" if self.session_records else "N/A",
            "common_missed_fields": dict(sorted(missed_field_counts.items(), key=lambda x: -x[1])[:10]),
            "submitted_applications": [
                {
                    "company": r.company,
                    "role": r.role,
                    "fields_filled_count": len(r.fields_filled),
                    "fields_filled": r.fields_filled
                }
                for r in submitted
            ],
            "failed_applications": [
                {
                    "company": r.company,
                    "role": r.role,
                    "url": r.url,
                    "error": r.error_message,
                    "fields_missed": r.fields_missed,
                    "questions_answered": r.questions_answered,
                    "validation_errors": r.validation_errors
                }
                for r in failed
            ]
        }

    def print_session_report(self) -> None:
        """Print a formatted session report to console."""
        summary = self.get_session_summary()

        print("\n" + "=" * 70)
        print("                    APPLICATION SESSION REPORT")
        print("=" * 70)
        print(f"Session Duration: {summary['session_duration_minutes']:.1f} minutes")
        print(f"Total Attempts:   {summary['total_attempts']}")
        print(f"Submitted:        {summary['submitted']} ({summary['success_rate']})")
        print(f"Failed:           {summary['failed']}")
        print(f"Skipped:          {summary['skipped']}")
        print("=" * 70)

        if summary['submitted_applications']:
            print("\n" + "-" * 70)
            print("SUCCESSFUL SUBMISSIONS:")
            print("-" * 70)
            for i, app in enumerate(summary['submitted_applications'], 1):
                print(f"\n{i}. {app['company']} - {app['role']}")
                print(f"   Fields filled: {app['fields_filled_count']}")
                if app['fields_filled']:
                    for field, value in list(app['fields_filled'].items())[:8]:
                        # Truncate long values
                        display_value = str(value)[:40] + "..." if len(str(value)) > 40 else str(value)
                        print(f"      - {field}: {display_value}")
                    if len(app['fields_filled']) > 8:
                        print(f"      ... and {len(app['fields_filled']) - 8} more fields")

        if summary['failed_applications']:
            print("\n" + "-" * 70)
            print("FAILED APPLICATIONS:")
            print("-" * 70)
            for i, app in enumerate(summary['failed_applications'], 1):
                print(f"\n{i}. {app['company']} - {app['role']}")
                print(f"   Error: {app['error'] or 'Unknown error'}")
                if app['fields_missed']:
                    print(f"   Missed fields: {list(app['fields_missed'].keys())}")

        if summary['common_missed_fields']:
            print("\n" + "-" * 70)
            print("COMMONLY MISSED FIELDS (potential issues):")
            print("-" * 70)
            for field, count in summary['common_missed_fields'].items():
                print(f"   - {field}: missed {count} times")

        print("\n" + "=" * 70 + "\n")

    def save_session_report(self, filename: str = None) -> str:
        """Save detailed session report to JSON file."""
        if not filename:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"application_report_{timestamp}.json"

        filepath = self.report_dir / filename

        report = {
            "summary": self.get_session_summary(),
            "all_records": [asdict(r) for r in self.session_records]
        }

        with open(filepath, "w") as f:
            json.dump(report, f, indent=2)

        logger.info(f"Session report saved to {filepath}")
        return str(filepath)

    def get_record(self, job_id: int) -> Optional[ApplicationRecord]:
        """Get record for a specific job."""
        for record in self.session_records:
            if record.job_id == job_id:
                return record
        return None
