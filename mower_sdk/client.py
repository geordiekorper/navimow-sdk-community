"""Deprecated import path: this module now lives at mower_sdk.legacy.client."""

from mower_sdk._deprecation import warn_legacy as _warn_legacy

_warn_legacy("client", __name__)

from mower_sdk.legacy.client import *  # noqa: F403
