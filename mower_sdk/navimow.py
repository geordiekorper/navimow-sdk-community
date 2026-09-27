"""Deprecated import path: this module now lives at mower_sdk.legacy.navimow."""

from mower_sdk._deprecation import warn_legacy as _warn_legacy

_warn_legacy("navimow", __name__)

from mower_sdk.legacy.navimow import *  # noqa: E402, F403
