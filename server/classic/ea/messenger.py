"""Compatibility exports for the shared Messenger and Classic profiles."""
from common.messenger import (
    HEADER_SIZE,
    EAMessengerAdapter,
    EAMessengerFrame,
    EAMessengerHub,
    EAMessengerStreamDecoder,
    WireSender,
    decode_messenger_fields,
)
from classic.ea.messenger_profiles import EAMessengerProfile, MESSENGER_PROFILES, messenger_profile

__all__ = ["HEADER_SIZE", "EAMessengerAdapter", "EAMessengerFrame", "EAMessengerHub",
           "EAMessengerStreamDecoder", "WireSender", "decode_messenger_fields",
           "EAMessengerProfile", "MESSENGER_PROFILES", "messenger_profile"]
