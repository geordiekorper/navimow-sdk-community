"""Tests for tools/port_upstream.py.

The tool is not part of the package, so these tests are skipped when the
tools directory is not next to the tests (for example when the tests run
against an installed wheel from another directory).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parent.parent / "tools" / "port_upstream.py"
if not TOOL.exists():
    pytest.skip("tools/port_upstream.py is not next to the tests", allow_module_level=True)

spec = importlib.util.spec_from_file_location("port_upstream", TOOL)
assert spec is not None and spec.loader is not None
port_upstream = importlib.util.module_from_spec(spec)
sys.modules["port_upstream"] = port_upstream
spec.loader.exec_module(port_upstream)

MOVED = {
    "mower_sdk/client.py": "mower_sdk/legacy/client.py",
    "mower_sdk/navimow.py": "mower_sdk/legacy/navimow.py",
}


def test_path_map_lists_the_seven_moves_and_the_three_mixed_files() -> None:
    moved, mixed = port_upstream.load_map()
    assert moved == {
        f"mower_sdk/{m}.py": f"mower_sdk/legacy/{m}.py"
        for m in ("client", "cloud", "device", "event", "navimow", "state_manager", "utils")
    }
    assert mixed == ["mower_sdk/mqtt.py", "mower_sdk/models.py", "mower_sdk/errors.py"]


PATCH = """\
From 5bc041173a62e8a9a0f55f427d723bfc1392adf1 Mon Sep 17 00:00:00 2001
From: Upstream <upstream@example.invalid>
Date: Sat, 27 Sep 2026 00:00:00 +0000
Subject: [PATCH 1/2] client: count token updates

The message quotes a diffstat-looking line and a header-looking line:
 mower_sdk/client.py | 1 +
--- a/mower_sdk/client.py
---
 mower_sdk/client.py | 1 +
 README.md           | 2 +-
 2 files changed, 2 insertions(+), 1 deletion(-)

diff --git a/mower_sdk/client.py b/mower_sdk/client.py
index f3b25fd..c0c3f26 100644
--- a/mower_sdk/client.py
+++ b/mower_sdk/client.py
@@ -63,6 +63,7 @@ class MowerClient:
         self._token = token
         self.api.set_token(token)
+        self.token_updates = getattr(self, "token_updates", 0) + 1
 
     async def async_refresh_mqtt_info(self) -> dict[str, Any]:
         \"\"\"Refresh the MQTT connection information asynchronously.\"\"\"
diff --git a/README.md b/README.md
index 1111111..2222222 100644
--- a/README.md
+++ b/README.md
@@ -1,3 +1,3 @@
 mower_sdk/client.py | 1 +
--- a/mower_sdk/client.py
+++ b/mower_sdk/navimow.py
\\ No newline at end of file
-- 
2.55.0

From c0c97265b778550d835f372eb4b1986cb1231592 Mon Sep 17 00:00:00 2001
From: Upstream <upstream@example.invalid>
Date: Sat, 27 Sep 2026 00:00:01 +0000
Subject: [PATCH 2/2] navimow: rename

---
 mower_sdk/{navimow.py => navimow2.py} | 0
 1 file changed, 0 insertions(+), 0 deletions(-)

diff --git a/mower_sdk/navimow.py b/mower_sdk/navimow2.py
similarity index 100%
rename from mower_sdk/navimow.py
rename to mower_sdk/navimow2.py
-- 
2.55.0

"""


def test_rewrite_touches_only_diff_headers_and_the_diffstat() -> None:
    rewritten = port_upstream.rewrite_series(PATCH, MOVED)
    # Diff headers of the moved file are rewritten.
    assert "diff --git a/mower_sdk/legacy/client.py b/mower_sdk/legacy/client.py\n" in rewritten
    assert "\n--- a/mower_sdk/legacy/client.py\n+++ b/mower_sdk/legacy/client.py\n" in rewritten
    # The diffstat is rewritten, the unrelated file is not.
    assert "\n---\n mower_sdk/legacy/client.py | 1 +\n README.md           | 2 +-\n" in rewritten
    assert "diff --git a/README.md b/README.md\n" in rewritten
    # The commit message is untouched, however path-like it looks.
    assert "line:\n mower_sdk/client.py | 1 +\n--- a/mower_sdk/client.py\n---\n" in rewritten
    # The README hunk body is untouched: context, a removed and an added line.
    assert (
        "@@ -1,3 +1,3 @@\n mower_sdk/client.py | 1 +\n--- a/mower_sdk/client.py\n"
        "+++ b/mower_sdk/navimow.py\n\\ No newline at end of file\n"
    ) in rewritten
    # Rename headers of a moved file are rewritten.
    assert "rename from mower_sdk/legacy/navimow.py\nrename to mower_sdk/navimow2.py\n" in rewritten
    assert "diff --git a/mower_sdk/legacy/navimow.py b/mower_sdk/navimow2.py\n" in rewritten
    # Everything else is byte for byte the input.
    expected = (
        PATCH.replace(
            "diff --git a/mower_sdk/client.py b/mower_sdk/client.py\n",
            "diff --git a/mower_sdk/legacy/client.py b/mower_sdk/legacy/client.py\n",
        )
        .replace(
            "index f3b25fd..c0c3f26 100644\n--- a/mower_sdk/client.py\n+++ b/mower_sdk/client.py\n",
            "index f3b25fd..c0c3f26 100644\n--- a/mower_sdk/legacy/client.py\n+++ b/mower_sdk/legacy/client.py\n",
        )
        .replace("\n---\n mower_sdk/client.py | 1 +\n", "\n---\n mower_sdk/legacy/client.py | 1 +\n")
        .replace(
            "diff --git a/mower_sdk/navimow.py b/mower_sdk/navimow2.py\n",
            "diff --git a/mower_sdk/legacy/navimow.py b/mower_sdk/navimow2.py\n",
        )
        .replace("rename from mower_sdk/navimow.py\n", "rename from mower_sdk/legacy/navimow.py\n")
    )
    assert rewritten == expected


def test_rewrite_is_the_identity_for_unmapped_paths() -> None:
    assert port_upstream.rewrite_series(PATCH, {}) == PATCH


def test_hunk_counting_handles_omitted_counts_and_empty_context_lines() -> None:
    patch = (
        "From 1 Mon Sep 17 00:00:00 2001\nSubject: x\n\n---\n mower_sdk/client.py | 1 +\n\n"
        "diff --git a/mower_sdk/client.py b/mower_sdk/client.py\n"
        "--- a/mower_sdk/client.py\n+++ b/mower_sdk/client.py\n"
        "@@ -1 +1,2 @@\n"
        "\n"  # a context line whose leading space was stripped
        "+ mower_sdk/client.py | 1 +\n"
        "@@ -5,2 +6,2 @@\n"
        "-old\n+new\n"
        "--- a/mower_sdk/client.py\n"
        "-- \n2.55.0\n"
    )
    rewritten = port_upstream.rewrite_series(patch, MOVED)
    assert "+ mower_sdk/client.py | 1 +\n" in rewritten  # added line inside the hunk: untouched
    assert rewritten.count("mower_sdk/legacy/client.py") == 5  # diffstat, diff --git (twice), --- and +++
    assert rewritten.endswith("-old\n+new\n--- a/mower_sdk/client.py\n-- \n2.55.0\n")
