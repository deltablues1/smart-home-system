"""
Error Handler

Globalna obrada grešaka s retry logikom i strukturiranim porukama
"""

import logging
import time
from typing import Callable, Any, Optional, Dict
from functools import wraps

logger = logging.getLogger(__name__)


class ErrorHandler:
    """
    Globalni error handler za ADK sustav

    Funkcionalnosti:
    - Retry logika s exponential backoff
    - Strukturirane error poruke za LLM
    - Dead Letter Queue za kritične greške
    """

    def __init__(self):
        self.error_count: Dict[str, int] = {}
        self.dead_letter_queue: list = []

    def retry_with_backoff(
        self,
        func: Callable,
        max_retries: int = 3,
        initial_delay: float = 1.0,
        backoff_factor: float = 2.0,
        exceptions: tuple = (Exception,)
    ) -> Callable:
        """
        Decorator za retry s exponential backoff

        Args:
            func: Funkcija za wrappe-anje
            max_retries: Maksimalan broj pokušaja
            initial_delay: Početno čekanje (sekunde)
            backoff_factor: Faktor za povećanje delay-a
            exceptions: Tuple exceptiona koje treba retry-ati

        Returns:
            Wrapped funkcija
        """
        @wraps(func)
        def wrapper(*args, **kwargs):
            delay = initial_delay

            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    if attempt == max_retries - 1:
                        logger.error(f"Function {func.__name__} failed after {max_retries} attempts: {e}")
                        raise

                    logger.warning(
                        f"Attempt {attempt + 1}/{max_retries} failed for {func.__name__}: {e}. "
                        f"Retrying in {delay}s..."
                    )

                    time.sleep(delay)
                    delay *= backoff_factor

        return wrapper

    def format_error_for_llm(
        self,
        error: Exception,
        context: str = "",
        tool_name: str = ""
    ) -> str:
        """
        Formatira error poruku za LLM razumljivo

        Args:
            error: Exception objekt
            context: Kontekst greške
            tool_name: Naziv alata koji je bacio grešku

        Returns:
            Formatirana poruka
        """
        error_type = type(error).__name__
        error_msg = str(error)

        message = f"❌ Error: {error_type}\n"

        if tool_name:
            message += f"Tool: {tool_name}\n"

        if context:
            message += f"Context: {context}\n"

        message += f"Details: {error_msg}\n"

        # Dodaj actionable savjete
        suggestions = self._get_error_suggestions(error_type, error_msg)
        if suggestions:
            message += f"\n💡 Suggestions:\n"
            for suggestion in suggestions:
                message += f"  • {suggestion}\n"

        return message

    def _get_error_suggestions(
        self,
        error_type: str,
        error_msg: str
    ) -> list[str]:
        """
        Vraća sugestije za rješavanje greške

        Args:
            error_type: Tip greške
            error_msg: Poruka greške

        Returns:
            Lista sugestija
        """
        suggestions = []
        error_msg_lower = error_msg.lower()

        # Quota errors
        if "quota" in error_msg_lower or "rate limit" in error_msg_lower:
            suggestions.extend([
                "API quota exceeded. Wait a few minutes and try again.",
                "Consider implementing request throttling.",
            ])

        # Permission errors
        elif "permission" in error_msg_lower or "forbidden" in error_msg_lower:
            suggestions.extend([
                "Check if you have necessary permissions for this resource.",
                "Verify OAuth scopes include required permissions.",
                "Ensure Service Account has domain-wide delegation enabled.",
            ])

        # Not found errors
        elif "not found" in error_msg_lower or error_type == "FileNotFoundError":
            suggestions.extend([
                "Verify the resource ID or name is correct.",
                "Resource may have been deleted or moved.",
                "Try searching for the resource first.",
            ])

        # Authentication errors
        elif "auth" in error_msg_lower or "credential" in error_msg_lower:
            suggestions.extend([
                "Check if authentication credentials are valid.",
                "Try refreshing OAuth token.",
                "Verify environment variables are set correctly.",
            ])

        # Network errors
        elif "timeout" in error_msg_lower or "connection" in error_msg_lower:
            suggestions.extend([
                "Network connectivity issue. Check internet connection.",
                "Try again in a few moments.",
                "Consider increasing timeout value.",
            ])

        # Default suggestion
        if not suggestions:
            suggestions.append("Review the error details and try a different approach.")

        return suggestions

    def log_to_dead_letter_queue(
        self,
        error: Exception,
        context: Dict[str, Any]
    ) -> None:
        """
        Logira kritičnu grešku u Dead Letter Queue

        Args:
            error: Exception objekt
            context: Kontekstualne informacije
        """
        entry = {
            'timestamp': time.time(),
            'error_type': type(error).__name__,
            'error_message': str(error),
            'context': context
        }

        self.dead_letter_queue.append(entry)
        logger.critical(f"Added to DLQ: {entry}")

        # Ograniči DLQ na 100 zapisa
        if len(self.dead_letter_queue) > 100:
            self.dead_letter_queue.pop(0)

    def get_error_count(self, error_type: str) -> int:
        """
        Dohvaća broj grešaka određenog tipa

        Args:
            error_type: Tip greške

        Returns:
            Broj grešaka
        """
        return self.error_count.get(error_type, 0)

    def increment_error_count(self, error_type: str) -> None:
        """
        Povećava brojač grešaka

        Args:
            error_type: Tip greške
        """
        self.error_count[error_type] = self.error_count.get(error_type, 0) + 1

    def reset_error_count(self, error_type: Optional[str] = None) -> None:
        """
        Resetira brojač grešaka

        Args:
            error_type: Tip greške (ako None, resetira sve)
        """
        if error_type:
            self.error_count[error_type] = 0
        else:
            self.error_count.clear()


# Singleton instance
_error_handler: Optional[ErrorHandler] = None


def get_error_handler() -> ErrorHandler:
    """
    Dohvaća singleton instancu ErrorHandler-a

    Returns:
        ErrorHandler instance
    """
    global _error_handler
    if _error_handler is None:
        _error_handler = ErrorHandler()
    return _error_handler
