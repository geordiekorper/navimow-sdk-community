"""Tests for tools/port_upstream.py.

The tool is not part of the package, so these tests are skipped when the
tools directory is not next to the tests (for example when the tests run
against an installed wheel from another directory). The scan and apply tests
build a small git repository under pytest's temporary directory.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
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


# ---- the rewriter, on diff text alone -----------------------------------------------------

DIFF = """\
diff --git a/mower_sdk/client.py b/mower_sdk/client.py
index f3b25fd..c0c3f26 100644
--- a/mower_sdk/client.py
+++ b/mower_sdk/client.py
@@ -63,4 +63,5 @@ class MowerClient:
         self._token = token
         self.api.set_token(token)
+        self.token_updates = getattr(self, "token_updates", 0) + 1
         return None
     async def async_refresh_mqtt_info(self) -> dict[str, Any]:
diff --git a/README.md b/README.md
index 1111111..2222222 100644
--- a/README.md
+++ b/README.md
@@ -1,3 +1,3 @@
 mower_sdk/client.py | 1 +
--- a/mower_sdk/client.py
+++ b/mower_sdk/navimow.py
 diff --git a/mower_sdk/client.py b/mower_sdk/client.py
\\ No newline at end of file
diff --git a/mower_sdk/navimow.py b/mower_sdk/navimow2.py
similarity index 100%
rename from mower_sdk/navimow.py
rename to mower_sdk/navimow2.py
diff --git a/logo.png b/logo.png
new file mode 100644
index 0000000..3333333
GIT binary patch
literal 12
Tcmb<--- a/mower_sdk/client.py

literal 0
HcmV?d00001

diff --git a/mower_sdk/client.py b/mower_sdk/client.py
--- a/mower_sdk/client.py
+++ b/mower_sdk/client.py
@@ -1 +1,2 @@
 class MowerClient:
+ mower_sdk/client.py | 1 +
"""


def hunk_counts_are_consistent(diff: str) -> bool:
    """Every hunk header's counts match the lines that follow it (a guard for the fixtures)."""
    old_left = new_left = 0
    in_hunk = False
    for line in diff.splitlines():
        header = port_upstream.HUNK_HEADER.match(line)
        if header:
            if in_hunk and (old_left or new_left):
                return False
            old_left = int(header.group("old") or 1)
            new_left = int(header.group("new") or 1)
            in_hunk = True
            continue
        if line.startswith(("diff --git ", "GIT binary patch")):
            if in_hunk and (old_left or new_left):
                return False
            in_hunk = False
            continue
        if not in_hunk or line.startswith("\\"):
            continue
        if line.startswith("+"):
            new_left -= 1
        elif line.startswith("-"):
            old_left -= 1
        else:
            old_left -= 1
            new_left -= 1
        if old_left < 0 or new_left < 0:
            return False
    return not (in_hunk and (old_left or new_left))


def test_the_fixture_hunk_counts_are_consistent() -> None:
    assert hunk_counts_are_consistent(DIFF)
    assert not hunk_counts_are_consistent(DIFF.replace("@@ -1,3 +1,3 @@", "@@ -1,4 +1,4 @@"))


def test_rewrite_touches_only_diff_headers_of_moved_files() -> None:
    rewritten = port_upstream.rewrite_diff(DIFF, MOVED)
    expected = (
        DIFF.replace(
            "diff --git a/mower_sdk/client.py b/mower_sdk/client.py\nindex f3b25fd..c0c3f26 100644\n"
            "--- a/mower_sdk/client.py\n+++ b/mower_sdk/client.py\n",
            "diff --git a/mower_sdk/legacy/client.py b/mower_sdk/legacy/client.py\n"
            "index f3b25fd..c0c3f26 100644\n"
            "--- a/mower_sdk/legacy/client.py\n+++ b/mower_sdk/legacy/client.py\n",
        )
        .replace(
            "diff --git a/mower_sdk/navimow.py b/mower_sdk/navimow2.py\n",
            "diff --git a/mower_sdk/legacy/navimow.py b/mower_sdk/navimow2.py\n",
        )
        .replace("rename from mower_sdk/navimow.py\n", "rename from mower_sdk/legacy/navimow.py\n")
        .replace(
            "diff --git a/mower_sdk/client.py b/mower_sdk/client.py\n"
            "--- a/mower_sdk/client.py\n+++ b/mower_sdk/client.py\n@@ -1 +1,2 @@\n",
            "diff --git a/mower_sdk/legacy/client.py b/mower_sdk/legacy/client.py\n"
            "--- a/mower_sdk/legacy/client.py\n+++ b/mower_sdk/legacy/client.py\n@@ -1 +1,2 @@\n",
        )
    )
    assert rewritten == expected
    # The README hunk body, which looks like headers, and the binary patch are untouched.
    assert (
        "@@ -1,3 +1,3 @@\n mower_sdk/client.py | 1 +\n--- a/mower_sdk/client.py\n"
        "+++ b/mower_sdk/navimow.py\n diff --git a/mower_sdk/client.py b/mower_sdk/client.py\n"
        "\\ No newline at end of file\n"
    ) in rewritten
    assert "Tcmb<--- a/mower_sdk/client.py\n" in rewritten
    assert "+ mower_sdk/client.py | 1 +\n" in rewritten


def test_rewrite_is_the_identity_for_unmapped_paths() -> None:
    assert port_upstream.rewrite_diff(DIFF, {}) == DIFF


def test_hunk_counting_treats_a_stripped_context_line_as_context() -> None:
    diff = (
        "diff --git a/mower_sdk/client.py b/mower_sdk/client.py\n"
        "--- a/mower_sdk/client.py\n+++ b/mower_sdk/client.py\n"
        "@@ -1,2 +1,2 @@\n"
        "\n"  # a context line whose leading space was stripped
        "--- a/mower_sdk/client.py\n"
        "+++ b/mower_sdk/client.py\n"
    )
    rewritten = port_upstream.rewrite_diff(diff, MOVED)
    assert rewritten.endswith("@@ -1,2 +1,2 @@\n\n--- a/mower_sdk/client.py\n+++ b/mower_sdk/client.py\n")
    assert rewritten.count("mower_sdk/legacy/client.py") == 4


# ---- the scan and the port, on a temporary repository -----------------------------------------

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Upstream",
    "GIT_AUTHOR_EMAIL": "upstream@example.invalid",
    "GIT_COMMITTER_NAME": "Upstream",
    "GIT_COMMITTER_EMAIL": "upstream@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}
PATHOLOGICAL_MESSAGE = """\
quoted edit

The message quotes a patch, a separator and a From line:
 mower_sdk/client.py | 1 +
---
diff --git a/mower_sdk/client.py b/mower_sdk/client.py
--- a/mower_sdk/client.py
From here on, nothing.
"""


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A small repository shaped like upstream's history, with the tool pointed at it.

    base ---- core-edit ---- clean-merge ---- evil-merge ---- ours-merge   (main)
       \\-- side-edit --/           /                /
       \\-- utils-edit ------------/                /
       \\-- discarded-edit -------------------------/
       \\-- quoted-edit  (a commit whose message looks like a patch)
       \\-- fork: client.py moved to legacy/client.py with a shim, as Phase 1 did
    """
    for name, value in GIT_ENV.items():
        monkeypatch.setenv(name, value)

    def run(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(tmp_path), *args], capture_output=True, text=True, check=True
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

    run("checkout", "-q", "-b", "side", "base")
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

    run("checkout", "-q", "-b", "side3", "base")
    write("mower_sdk/client.py", "class MowerClient:\n    discarded = True\n")
    run("commit", "-q", "-am", "discarded edit")
    run("checkout", "-q", "main")
    run("merge", "-q", "-s", "ours", "--no-ff", "-m", "ours merge", "side3")
    run("tag", "ours-merge")

    run("checkout", "-q", "-b", "quoted", "base")
    write("mower_sdk/client.py", "class MowerClient:\n    quoted = True\n")
    run("commit", "-q", "-am", PATHOLOGICAL_MESSAGE)
    run("tag", "quoted-edit")

    run("checkout", "-q", "-b", "fork", "base")
    (tmp_path / "mower_sdk" / "legacy").mkdir()
    run("mv", "mower_sdk/client.py", "mower_sdk/legacy/client.py")
    write("mower_sdk/client.py", "from mower_sdk.legacy.client import *\n")
    run("add", "-A")
    run("commit", "-q", "-m", "fork: move client.py to legacy/")
    run("tag", "fork-move")

    monkeypatch.setattr(port_upstream, "REPO_ROOT", tmp_path)
    return tmp_path


def git_out(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def test_commits_in_reports_parents(repo: Path) -> None:
    commits = port_upstream.commits_in("base..clean-merge")
    assert [len(parents) for _, parents in commits] == [1, 1, 2]
    sha, parents = commits[-1]
    assert sha == git_out(repo, "rev-parse", "clean-merge").strip()
    assert parents == [git_out(repo, "rev-parse", "core-edit").strip(), git_out(repo, "rev-parse", "side-edit").strip()]


@pytest.mark.usefixtures("repo")
def test_files_touched_lists_a_commits_changes() -> None:
    assert port_upstream.files_touched("side-edit") == ["mower_sdk/client.py"]
    assert port_upstream.files_touched("base") == [
        "mower_sdk/client.py", "mower_sdk/mqtt.py", "mower_sdk/utils.py"
    ]


def parents_of(repo: Path, commit: str) -> list[str]:
    return git_out(repo, "rev-list", "--parents", "-n1", commit).split()[1:]


def test_merge_verdicts(repo: Path) -> None:
    # Exactly git's clean merge of its parents: the side-branch commit carries the change.
    assert port_upstream.merge_verdict("clean-merge", parents_of(repo, "clean-merge")) is None
    # A clean merge was possible, but the commit's tree differs: a resolution by hand.
    assert "not the clean merge" in port_upstream.merge_verdict("evil-merge", parents_of(repo, "evil-merge"))
    # -s ours discarded a change to a file both sides touched: the parents conflict.
    assert "conflict" in port_upstream.merge_verdict("ours-merge", parents_of(repo, "ours-merge"))
    assert "octopus" in port_upstream.merge_verdict("clean-merge", ["a", "b", "c"])


def test_an_ours_merge_that_discards_an_unconflicting_change_is_refused(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The merge is clean for git, yet its tree keeps the first parent's version."""
    git_out(repo, "checkout", "-q", "-b", "side4", "ours-merge")
    (repo / "mower_sdk" / "newfile.py").write_text("x = 1\n")
    git_out(repo, "add", "-A")
    git_out(repo, "commit", "-q", "-m", "new file on a side branch")
    git_out(repo, "checkout", "-q", "main")
    git_out(repo, "merge", "-q", "-s", "ours", "--no-ff", "-m", "ours merge of an unconflicting change", "side4")
    assert "not the clean merge" in port_upstream.merge_verdict("HEAD", parents_of(repo, "HEAD"))
    assert port_upstream.main(["--dry-run", "ours-merge..HEAD"]) == 2
    assert "merge commit: its tree is not the clean merge" in capsys.readouterr().err


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
    assert "merge commit: its tree is not the clean merge of its parents" in captured.err
    assert "utils edit" in captured.out


@pytest.mark.usefixtures("repo")
def test_a_range_of_only_merges_is_nothing_to_port(capsys: pytest.CaptureFixture[str]) -> None:
    assert port_upstream.main(["--dry-run", "clean-merge^!"]) == 3
    assert "holds only merge commits" in capsys.readouterr().err


def test_dry_run_is_the_message_then_the_rewritten_diff(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert port_upstream.main(["--dry-run", "core-edit..clean-merge"]) == 0
    out = capsys.readouterr().out
    assert "clean merge" in out and "skipped" in out
    mail = git_out(repo, "log", "-1", "--pretty=mboxrd", "side-edit")
    diff = git_out(repo, "diff-tree", "--no-commit-id", "-p", "-M", "--binary", "--root", "side-edit")
    assert out.endswith(mail + "---\n\n" + diff.replace("mower_sdk/client.py", "mower_sdk/legacy/client.py"))
    assert out.count("\nFrom ") + out.startswith("From ") == 1  # one mbox entry


def test_a_message_that_looks_like_a_patch_is_copied_byte_for_byte(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert port_upstream.main(["--dry-run", "base..quoted-edit"]) == 0
    out = capsys.readouterr().out
    mail = git_out(repo, "log", "-1", "--pretty=mboxrd", "quoted-edit")
    assert mail in out
    body = PATHOLOGICAL_MESSAGE.split("\n", 2)[2].replace("From here", ">From here")
    assert body in mail  # git's mboxrd quoting is the only change to the message
    assert out.count("diff --git a/mower_sdk/legacy/client.py b/mower_sdk/legacy/client.py") == 1


def test_port_applies_the_series_into_legacy_with_upstream_authorship(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    git_out(repo, "checkout", "-q", "fork")
    assert port_upstream.main(["base..side-edit"]) == 0
    assert "applied 1 commit(s)" in capsys.readouterr().out
    assert git_out(repo, "log", "-1", "--format=%an <%ae>: %s") == "Upstream <upstream@example.invalid>: side edit\n"
    assert git_out(repo, "diff", "--name-only", "fork-move", "HEAD") == "mower_sdk/legacy/client.py\n"
    assert (repo / "mower_sdk/legacy/client.py").read_text() == "class MowerClient:\n    token_updates = 0\n"
    assert (repo / "mower_sdk/client.py").read_text() == "from mower_sdk.legacy.client import *\n"
    assert git_out(repo, "status", "--porcelain") == ""


def test_a_conflict_stops_the_port_and_abort_restores_the_branch(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    git_out(repo, "checkout", "-q", "fork")
    (repo / "mower_sdk/legacy/client.py").write_text("class MowerClient:\n    forked = True\n")
    git_out(repo, "commit", "-q", "-am", "fork: conflicting edit")
    head = git_out(repo, "rev-parse", "HEAD")
    assert port_upstream.main(["base..side-edit"]) == 1
    assert "git am --continue" in capsys.readouterr().err
    assert (repo / ".git" / "rebase-apply").is_dir()
    assert "UU mower_sdk/legacy/client.py" in git_out(repo, "status", "--porcelain")
    git_out(repo, "am", "--abort")
    assert git_out(repo, "rev-parse", "HEAD") == head
    assert git_out(repo, "status", "--porcelain") == ""


@pytest.mark.usefixtures("repo")
def test_a_dirty_tree_is_refused_before_git_am(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    git_out(repo, "checkout", "-q", "fork")
    (repo / "mower_sdk/client.py").write_text("# dirty\n")
    assert port_upstream.main(["base..side-edit"]) == 3
    assert "uncommitted changes" in capsys.readouterr().err
