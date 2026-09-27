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


# ---- the scan, on a temporary repository ----------------------------------------------------

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Upstream",
    "GIT_AUTHOR_EMAIL": "upstream@example.invalid",
    "GIT_COMMITTER_NAME": "Upstream",
    "GIT_COMMITTER_EMAIL": "upstream@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
}


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A small repository shaped like upstream's history, with the tool pointed at it.

    base -> (main) "core edit" touches mower_sdk/mqtt.py
         -> (side) "side edit" touches mower_sdk/client.py
    clean-merge: main + side, no changes of its own
    evil-merge:  a second merge of another side branch, with an extra edit in the merge
    """
    import os
    import subprocess

    def run(*args: str, cwd: Path = tmp_path) -> str:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, **GIT_ENV},
        ).stdout.strip()

    def write(path: str, text: str) -> None:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    run("init", "-q", "-b", "main")
    write("mower_sdk/client.py", "class MowerClient:\n    pass\n")
    write("mower_sdk/mqtt.py", "class NavimowMQTT:\n    pass\n")
    write("mower_sdk/utils.py", "def parse_json(data):\n    return data\n")
    run("add", "-A")
    run("commit", "-q", "-m", "base")
    run("tag", "base")

    run("checkout", "-q", "-b", "side")
    write("mower_sdk/client.py", "class MowerClient:\n    token_updates = 0\n")
    run("commit", "-q", "-am", "side edit")
    run("tag", "side-edit")

    run("checkout", "-q", "main")
    write("mower_sdk/mqtt.py", "class NavimowMQTT:\n    keepalive = 60\n")
    run("commit", "-q", "-am", "core edit")
    run("tag", "core-edit")
    run("merge", "-q", "--no-ff", "-m", "clean merge", "side")
    run("tag", "clean-merge")

    run("checkout", "-q", "-b", "side2", "base")
    write("mower_sdk/utils.py", "def parse_json(data):\n    return data or {}\n")
    run("commit", "-q", "-am", "utils edit")
    run("checkout", "-q", "main")
    run("merge", "-q", "--no-ff", "--no-commit", "side2")
    write("mower_sdk/utils.py", "def parse_json(data):\n    return data or {}  # resolved by hand\n")
    run("add", "-A")
    run("commit", "-q", "-m", "evil merge")
    run("tag", "evil-merge")

    monkeypatch.setattr(port_upstream, "REPO_ROOT", tmp_path)
    return tmp_path


@pytest.mark.usefixtures("repo")
def test_commits_in_reports_merges() -> None:
    commits = port_upstream.commits_in("base..clean-merge")
    assert [is_merge for _, is_merge in commits] == [False, False, True]


@pytest.mark.usefixtures("repo")
def test_files_touched_for_a_merge_lists_only_changes_of_its_own() -> None:
    assert port_upstream.files_touched("clean-merge", is_merge=True) == []
    assert port_upstream.files_touched("evil-merge", is_merge=True) == ["mower_sdk/utils.py"]
    assert port_upstream.files_touched("side-edit", is_merge=False) == ["mower_sdk/client.py"]


@pytest.mark.usefixtures("repo")
def test_scan_refuses_a_mixed_file_before_applying_anything(capsys: pytest.CaptureFixture[str]) -> None:
    assert port_upstream.main(["base..clean-merge"]) == 2
    captured = capsys.readouterr()
    assert "touches a mixed file: mower_sdk/mqtt.py" in captured.err
    assert "clean merge" in captured.out and "skipped" in captured.out


@pytest.mark.usefixtures("repo")
def test_scan_refuses_a_merge_with_changes_of_its_own(capsys: pytest.CaptureFixture[str]) -> None:
    assert port_upstream.main(["clean-merge..evil-merge"]) == 2
    captured = capsys.readouterr()
    assert "merge commit with changes of its own: mower_sdk/utils.py" in captured.err
    assert "utils edit" in captured.out


@pytest.mark.usefixtures("repo")
def test_dry_run_of_a_clean_merge_and_its_side_commit(capsys: pytest.CaptureFixture[str]) -> None:
    # Only the side branch, its edit and the clean merge: no mixed file in the way.
    assert port_upstream.main(["--dry-run", "core-edit..clean-merge"]) == 0
    captured = capsys.readouterr()
    assert "clean merge" in captured.out and "skipped" in captured.out
    assert captured.out.count("\nFrom ") + captured.out.startswith("From ") == 1  # one patch
    assert "diff --git a/mower_sdk/legacy/client.py b/mower_sdk/legacy/client.py" in captured.out
    assert "token_updates" in captured.out


@pytest.mark.usefixtures("repo")
def test_a_range_of_only_merges_is_nothing_to_port(capsys: pytest.CaptureFixture[str]) -> None:
    assert port_upstream.main(["--dry-run", "clean-merge^!"]) == 3
    assert "holds only merge commits" in capsys.readouterr().err
