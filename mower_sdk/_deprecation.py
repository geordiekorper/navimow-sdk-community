"""Deprecation policy for the legacy package.

One DeprecationWarning per legacy module per process, on the first deprecated
access to it through any old path: a shim import at a vacated module path, or
a lazily served name in ``mower_sdk``, ``mower_sdk.mqtt``, ``mower_sdk.models``
or ``mower_sdk.errors``. Later accesses to the same legacy module, through any
path, are silent. Importing ``mower_sdk.legacy.<module>`` directly never warns.

This module imports only ``threading`` and ``warnings``, nothing from
``legacy/``, so core can use it without loading legacy code.
"""

from __future__ import annotations

import threading
import warnings

_LOCK = threading.RLock()
_WARNED: set[str] = set()


def warn_legacy(legacy_module: str, via: str) -> None:
    """Warn, once per process, that ``legacy_module`` was reached through ``via``.

    ``legacy_module`` is the module's name under ``mower_sdk.legacy`` (for
    example ``"client"``) and ``via`` the deprecated path the caller used (for
    example ``"mower_sdk.client"`` or ``"mower_sdk.MowerClient"``).

    The warning says that every name in the legacy module is deprecated,
    because later names from the same module are silent. It is emitted with
    ``stacklevel=3``: one frame for this helper and one for the shim body or
    ``__getattr__`` that called it, so it is attributed to the caller's line;
    Python skips importlib's bootstrap frames, so a shim executed during an
    import is attributed to the importing line as well.

    The check, the warning and the record run under an RLock, so concurrent
    first accesses produce one warning and a warning handler that itself
    touches a legacy name does not deadlock. The module is recorded only after
    ``warnings.warn`` returns: under an error filter every deprecated access
    raises, rather than raising once and passing silently afterwards.
    """
    with _LOCK:
        if legacy_module in _WARNED:
            return
        warnings.warn(
            f"{via} is deprecated: all names in mower_sdk.legacy.{legacy_module} "
            "are legacy code kept only for compatibility",
            DeprecationWarning,
            stacklevel=3,
        )
        _WARNED.add(legacy_module)
