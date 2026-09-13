"""
Interfaces Module
"""

from .base_interface import BaseInterface

__all__ = ["BaseInterface"]

try:
    from .telegram_interface import TelegramInterface

    __all__.append("TelegramInterface")
except ImportError:
    pass

try:
    from .wakeword_interface import WakeWordInterface

    __all__.append("WakeWordInterface")
except ImportError:
    pass
