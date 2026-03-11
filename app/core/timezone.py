"""
Timezone Utilities Module

Provides timezone-aware datetime functions for the application.
Default timezone is set to Colombia (America/Bogota).
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.config import get_settings


def get_app_timezone() -> ZoneInfo:
    """
    Get the application timezone from settings.
    
    Returns:
        ZoneInfo object for the configured timezone (default: America/Bogota)
    """
    settings = get_settings()
    return ZoneInfo(settings.timezone)


def now() -> datetime:
    """
    Get current datetime in application timezone.
    
    This replaces datetime.utcnow() to use Colombia timezone.
    
    Returns:
        datetime object with timezone info set to application timezone
    """
    return datetime.now(get_app_timezone())


def utcnow() -> datetime:
    """
    Get current datetime in application timezone (for backward compatibility).
    
    Note: Despite the name, this returns time in the application timezone,
    not UTC. This is for backward compatibility with existing code.
    
    Returns:
        datetime object with timezone info set to application timezone
    """
    return now()


def to_app_timezone(dt: datetime) -> datetime:
    """
    Convert a datetime to application timezone.
    
    If the datetime is naive, it's assumed to be in application timezone.
    If it has timezone info, it's converted to application timezone.
    
    Args:
        dt: datetime to convert
        
    Returns:
        datetime in application timezone
    """
    app_tz = get_app_timezone()
    
    if dt.tzinfo is None:
        # Naive datetime, assume it's already in app timezone
        return dt.replace(tzinfo=app_tz)
    else:
        # Aware datetime, convert to app timezone
        return dt.astimezone(app_tz)


def to_naive(dt: datetime) -> datetime:
    """
    Convert a timezone-aware datetime to naive datetime in application timezone.
    
    Useful for database storage when columns don't support timezone.
    
    Args:
        dt: timezone-aware datetime
        
    Returns:
        naive datetime in application timezone
    """
    if dt.tzinfo is None:
        return dt
    
    app_tz = get_app_timezone()
    if dt.tzinfo != app_tz:
        dt = dt.astimezone(app_tz)
    
    return dt.replace(tzinfo=None)

