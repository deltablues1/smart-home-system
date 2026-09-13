"""
Alerting Configuration

Setup alerts for critical system events
"""

import logging
from typing import Dict, Any, Optional
from enum import Enum

logger = logging.getLogger(__name__)


class AlertSeverity(Enum):
    """Alert severity levels"""
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class AlertManager:
    """Manages system alerts"""

    def __init__(self):
        self.handlers = []

    def add_handler(self, handler):
        """Add alert handler"""
        self.handlers.append(handler)

    def send_alert(
        self,
        title: str,
        message: str,
        severity: AlertSeverity = AlertSeverity.INFO,
        metadata: Optional[Dict[str, Any]] = None
    ):
        """Send alert through configured handlers"""
        alert = {
            "title": title,
            "message": message,
            "severity": severity.value,
            "metadata": metadata or {},
            "timestamp": datetime.now(timezone.utc).isoformat()
        }

        logger.log(
            self._severity_to_log_level(severity),
            f"Alert: {title} - {message}"
        )

        for handler in self.handlers:
            try:
                handler.send(alert)
            except Exception as e:
                logger.error(f"Failed to send alert via {handler}: {e}")

    def _severity_to_log_level(self, severity: AlertSeverity) -> int:
        """Convert alert severity to log level"""
        mapping = {
            AlertSeverity.INFO: logging.INFO,
            AlertSeverity.WARNING: logging.WARNING,
            AlertSeverity.ERROR: logging.ERROR,
            AlertSeverity.CRITICAL: logging.CRITICAL
        }
        return mapping.get(severity, logging.INFO)


class CloudLoggingAlertHandler:
    """Send alerts to Google Cloud Logging"""

    def __init__(self, project_id: Optional[str] = None):
        try:
            import google.cloud.logging
            self.client = google.cloud.logging.Client(project=project_id)
            self.logger = self.client.logger("adk-alerts")
        except ImportError:
            logger.warning("google-cloud-logging not installed")
            self.client = None

    def send(self, alert: Dict[str, Any]):
        """Send alert to Cloud Logging"""
        if self.client:
            self.logger.log_struct(alert, severity=alert["severity"].upper())


# Global alert manager
_alert_manager = AlertManager()


def get_alert_manager() -> AlertManager:
    """Get global alert manager"""
    return _alert_manager


def alert(title: str, message: str, severity: AlertSeverity = AlertSeverity.INFO, **metadata):
    """Convenience function to send alert"""
    _alert_manager.send_alert(title, message, severity, metadata)


from datetime import datetime, timezone
