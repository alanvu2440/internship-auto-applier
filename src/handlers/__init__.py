"""ATS Handlers for different job application platforms."""

from .base import BaseHandler
from .greenhouse import GreenhouseHandler
from .lever import LeverHandler
from .workday import WorkdayHandler
from .smartrecruiters import SmartRecruitersHandler
from .ashby import AshbyHandler
from .icims import ICIMSHandler
from .generic import GenericHandler

__all__ = [
    "BaseHandler",
    "GreenhouseHandler",
    "LeverHandler",
    "WorkdayHandler",
    "SmartRecruitersHandler",
    "AshbyHandler",
    "ICIMSHandler",
    "GenericHandler",
]
