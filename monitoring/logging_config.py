"""
Centralized Logging Configuration

Supports both local development and Google Cloud Logging
Enhanced with structured JSON logging and Cloud integration
"""

import logging
import sys
import os
from typing import Optional
from datetime import datetime, timezone


class StructuredFormatter(logging.Formatter):
    """
    Custom formatter for structured JSON logging
    Compatible with Google Cloud Logging
    """

    def format(self, record):
        """Format log record as structured JSON"""
        import json

        # Base log data
        log_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Add exception info if present
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
            log_data["error_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None

        # Extract and add extra fields from the LogRecord
        # Cloud Logging expects these at the root level
        extra_fields = {}
        for key, value in record.__dict__.items():
            # Skip standard LogRecord attributes
            if key not in ['name', 'msg', 'args', 'created', 'filename', 'funcName',
                          'levelname', 'levelno', 'lineno', 'module', 'msecs',
                          'message', 'pathname', 'process', 'processName',
                          'relativeCreated', 'thread', 'threadName', 'exc_info',
                          'exc_text', 'stack_info', 'getMessage']:
                extra_fields[key] = value

        # Add extra fields to log data
        log_data.update(extra_fields)

        return json.dumps(log_data)


class CloudLoggingAdapter(logging.LoggerAdapter):
    """
    Logger adapter for adding contextual information to all log entries
    Useful for session tracking, request tracing, etc.
    """

    def process(self, msg, kwargs):
        """Add context to all log messages"""
        # Ensure 'extra' exists in kwargs
        if 'extra' not in kwargs:
            kwargs['extra'] = {}

        # Merge adapter's extra context with call-specific extra
        kwargs['extra'].update(self.extra)

        return msg, kwargs


class FunctionCallWarningFilter(logging.Filter):
    """
    Filter to suppress benign warnings from google_genai.types
    about non-text parts in responses.

    This warning occurs when ADK agents use tools (function calls) - the response
    contains both text and function_call/function_response parts. Our code
    properly processes all parts, so this warning is not actionable.
    """

    def filter(self, record):
        """Filter out specific warning about non-text parts"""
        # Filter google_genai.types warnings about non-text parts
        if record.name == 'google_genai.types':
            if 'non-text parts in the response' in record.getMessage():
                return False  # Suppress this warning
        return True  # Allow all other log records


def setup_logging(
    level: str = "INFO",
    use_cloud_logging: bool = False,
    project_id: Optional[str] = None,
    enable_json_stdout: bool = False
):
    """
    Setup centralized logging configuration

    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        use_cloud_logging: Use Google Cloud Logging (auto-detects if not specified)
        project_id: Google Cloud project ID (auto-detects from env if not specified)
        enable_json_stdout: Force JSON output to stdout (useful for log aggregation)
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    # Root logger configuration
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Auto-detect Cloud Logging if project_id is available
    if not use_cloud_logging and project_id:
        # Try to enable Cloud Logging automatically if project is configured
        use_cloud_logging = True

    cloud_logging_enabled = False

    if use_cloud_logging:
        # Google Cloud Logging
        try:
            import google.cloud.logging
            from google.cloud.logging.handlers import CloudLoggingHandler

            client = google.cloud.logging.Client(project=project_id)

            # Use CloudLoggingHandler for better integration
            cloud_handler = CloudLoggingHandler(client, name="adk-agent-system")
            cloud_handler.setLevel(log_level)

            # Add function call warning filter to cloud handler
            cloud_handler.addFilter(FunctionCallWarningFilter())

            root_logger.addHandler(cloud_handler)

            cloud_logging_enabled = True
            print("[OK] Google Cloud Logging enabled")
            print(f"   Project: {project_id}")
            print(f"   Log name: adk-agent-system")

        except ImportError:
            print("[WARNING] google-cloud-logging not installed")
            print("   Install: pip install google-cloud-logging")
            print("   Falling back to stdout logging")
        except Exception as e:
            print(f"[WARNING] Failed to setup Cloud Logging: {e}")
            print("   Falling back to stdout logging")

    # Always add stdout handler for local visibility
    # (Cloud Logging handler doesn't print to console)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)

    # Use JSON formatter if:
    # - Explicitly requested
    # - Production environment
    # - Cloud Logging is disabled (for log aggregation tools)
    use_json_format = (
        enable_json_stdout or
        os.getenv('ENVIRONMENT') == 'production' or
        os.getenv('LOG_FORMAT', '').lower() == 'json'
    )

    if use_json_format:
        formatter = StructuredFormatter()
        print("📋 JSON structured logging enabled for stdout")
    else:
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )

    console_handler.setFormatter(formatter)

    # Add function call warning filter to console handler
    console_handler.addFilter(FunctionCallWarningFilter())

    root_logger.addHandler(console_handler)

    # Suppress noisy loggers
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('google').setLevel(logging.WARNING)
    logging.getLogger('googleapiclient').setLevel(logging.WARNING)
    logging.getLogger('google.auth').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)

    # Suppress ADK internal LLM call logs (ReAct loop remains functional)
    # Summary logging still available via runner_utils
    logging.getLogger('google_adk').setLevel(logging.WARNING)
    logging.getLogger('google_adk.google.adk.models.google_llm').setLevel(logging.WARNING)

    # Suppress HTTP request logs (keep errors visible)
    logging.getLogger('httpx').setLevel(logging.WARNING)

    # Add filter to google_genai.types logger specifically
    genai_types_logger = logging.getLogger('google_genai.types')
    genai_types_logger.addFilter(FunctionCallWarningFilter())

    # Log configuration summary
    config_msg = f"Logging configured: level={level}, cloud_logging={cloud_logging_enabled}, json_stdout={use_json_format}"
    logging.info(config_msg)

    return cloud_logging_enabled


def get_logger_with_context(name: str, **context) -> CloudLoggingAdapter:
    """
    Get a logger with contextual information

    Args:
        name: Logger name (usually __name__)
        **context: Additional context to add to all log messages

    Returns:
        CloudLoggingAdapter with context

    Example:
        logger = get_logger_with_context(__name__, session_id="abc-123", user_id="user-456")
        logger.info("Processing request")  # Will include session_id and user_id
    """
    base_logger = logging.getLogger(name)
    return CloudLoggingAdapter(base_logger, context)
