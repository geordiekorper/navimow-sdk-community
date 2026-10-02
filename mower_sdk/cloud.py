"""Deprecated import path: this module now lives at mower_sdk.legacy.cloud."""

from mower_sdk._deprecation import warn_legacy as _warn_legacy

_warn_legacy("cloud", __name__)

from mower_sdk.legacy.cloud import *  # noqa: F403
