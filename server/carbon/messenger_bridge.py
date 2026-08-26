"""Deprecated V845 JSON bridge compatibility import.

V846 uses :class:`CarbonMessengerIPCPublisher` and writes no bridge file.
"""

from .messenger_ipc import CarbonMessengerIPCPublisher

__all__ = ["CarbonMessengerIPCPublisher"]
