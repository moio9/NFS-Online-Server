"""Need for Speed Carbon online services."""

from common import API_VERSION as COMMON_API_VERSION

EXPECTED_COMMON_API_VERSION = 8
if COMMON_API_VERSION != EXPECTED_COMMON_API_VERSION:
    raise RuntimeError(
        f"incompatible common API {COMMON_API_VERSION}; "
        f"this build requires {EXPECTED_COMMON_API_VERSION}"
    )

BUILD_VERSION = "V875"
BUILD_PROFILE = "dlc-store-toml-kick"
