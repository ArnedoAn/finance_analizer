"""
Structured Logging Configuration

Provides consistent, structured logging across the application using structlog.
Includes context processors for request tracking and environment info.
Logs are output to both console and file.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import structlog
from structlog.types import EventDict, Processor

from app.core.config import get_settings


def add_app_context(
    logger: logging.Logger, method_name: str, event_dict: EventDict
) -> EventDict:
    """Add application context to all log entries."""
    settings = get_settings()
    event_dict["app"] = settings.app_name
    event_dict["env"] = settings.app_env
    return event_dict


def drop_color_message_key(
    logger: logging.Logger, method_name: str, event_dict: EventDict
) -> EventDict:
    """Remove color_message key added by uvicorn."""
    event_dict.pop("color_message", None)
    return event_dict


def setup_logging() -> None:
    """
    Configure structured logging for the application.
    
    Sets up structlog with appropriate processors for development
    and production environments.
    """
    settings = get_settings()
    
    # Common processors for all environments
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        add_app_context,
        drop_color_message_key,
    ]
    
    if settings.is_production:
        # Production: JSON output for log aggregation
        shared_processors.append(structlog.processors.format_exc_info)
        renderer: Processor = structlog.processors.JSONRenderer()
    else:
        # Development: Colored console output
        renderer = structlog.dev.ConsoleRenderer(colors=True)
    
    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    
    # Configure standard library logging
    log_level = getattr(logging, settings.log_level)
    
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )
    
    # Console handler (colored for dev, plain for prod)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    
    # File handler (JSON format for easy parsing)
    log_dir = Path(settings.log_dir)
    log_file = log_dir / settings.log_file
    log_dir.mkdir(parents=True, exist_ok=True)
    
    # Create JSON formatter for file output
    file_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )
    
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(file_formatter)
    
    # Root logger configuration
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)
    root_logger.setLevel(log_level)
    
    # Suppress noisy loggers
    for logger_name in ["httpx", "httpcore", "urllib3", "googleapiclient"]:
        logging.getLogger(logger_name).setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """
    Get a structured logger instance.
    
    Args:
        name: Optional logger name. Defaults to caller's module.
        
    Returns:
        Configured structlog BoundLogger instance.
    """
    return structlog.get_logger(name)


class LogContext:
    """Context manager for adding temporary context to logs."""
    
    def __init__(self, **kwargs: Any) -> None:
        self.context = kwargs
        self._tokens: list[Any] = []
    
    def __enter__(self) -> "LogContext":
        for key, value in self.context.items():
            token = structlog.contextvars.bind_contextvars(**{key: value})
            self._tokens.append(token)
        return self
    
    def __exit__(self, *args: Any) -> None:
        structlog.contextvars.unbind_contextvars(*self.context.keys())
