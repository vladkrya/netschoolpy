"""myschoolpy — асинхронный клиент «Госуслуги Моя школа»."""

from .client import MySchool, interval_id, week_intervals
from .exceptions import (
    AccessDenied,
    AccountLocked,
    DataNotReady,
    ESIAAccountLocked,
    ESIAError,
    ESIAInvalidCredentials,
    InvalidCredentials,
    LoginError,
    MFAError,
    MySchoolError,
    NotFound,
    ServerUnavailable,
    SessionExpired,
)
from .models import Homework, Lesson, Mark, Student

__version__ = "0.1.0"

__all__ = [
    "MySchool",
    "interval_id",
    "week_intervals",
    "Student",
    "Lesson",
    "Mark",
    "Homework",
    "MySchoolError",
    "SessionExpired",
    "AccessDenied",
    "NotFound",
    "DataNotReady",
    "ServerUnavailable",
    "LoginError",
    "MFAError",
    "ESIAError",
    "InvalidCredentials",
    "AccountLocked",
    "ESIAInvalidCredentials",
    "ESIAAccountLocked",
]
