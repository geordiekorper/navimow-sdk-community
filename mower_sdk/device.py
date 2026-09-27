"""Deprecated import path: this module now lives at mower_sdk.legacy.device."""

from mower_sdk._deprecation import warn_legacy as _warn_legacy

_warn_legacy("device", __name__)

from mower_sdk.legacy.device import *  # noqa: E402, F403
