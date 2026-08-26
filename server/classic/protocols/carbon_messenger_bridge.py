"""Deprecated compatibility names for the V900 in-memory IPC state."""

from .carbon_messenger_ipc import (
    CarbonIPCIdentity as CarbonBridgeIdentity,
    CarbonMessengerIPCState as CarbonMessengerBridge,
)

__all__ = ["CarbonBridgeIdentity", "CarbonMessengerBridge"]
