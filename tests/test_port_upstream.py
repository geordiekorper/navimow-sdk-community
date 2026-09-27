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

MOVED = port_upstream.encode_map(
    {
        "mower_sdk/client.py": "mower_sdk/legacy/client.py",
        "mower_sdk/navimow.py": "mower_sdk/legacy/navimow.py",
    }
)


def test_path_map_lists_the_seven_moves_and_the_three_mixed_files() -> None:
    moved, mixed = port_upstream.load_map()
    assert moved == {
        f"mower_sdk/{m}.py": f"mower_sdk/legacy/{m}.py"
        for m in ("client", "cloud", "device", "event", "navimow", "state_manager", "utils")
    }
    assert mixed == ["mower_sdk/mqtt.py", "mower_sdk/models.py", "mower_sdk/errors.py"]


# ---- the rewriter, on diff text alone -----------------------------------------------------

DIFF = b"""\
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


def hunk_counts_are_consistent(diff: bytes) -> bool:
    """Every hunk header's counts match the lines that follow it (a guard for the fixtures)."""
    old_left = new_left = 0
    in_hunk = False
    for raw in port_upstream.split_lines(diff):
        line = raw.rstrip(b"\n")
        header = port_upstream.HUNK_HEADER.match(line)
        if header:
            if in_hunk and (old_left or new_left):
                return False
            old_left = int(header.group("old") or 1)
            new_left = int(header.group("new") or 1)
            in_hunk = True
            continue
        if line.startswith((b"diff --git ", b"GIT binary patch")):
            if in_hunk and (old_left or new_left):
                return False
            in_hunk = False
            continue
        if not in_hunk or line.startswith(b"\\"):
            continue
        if line.startswith(b"+"):
            new_left -= 1
        elif line.startswith(b"-"):
            old_left -= 1
        else:
            old_left -= 1
            new_left -= 1
        if old_left < 0 or new_left < 0:
            return False
    return not (in_hunk and (old_left or new_left))


def test_the_fixture_hunk_counts_are_consistent() -> None:
    assert hunk_counts_are_consistent(DIFF)
    assert not hunk_counts_are_consistent(DIFF.replace(b"@@ -1,3 +1,3 @@", b"@@ -1,4 +1,4 @@"))


def test_rewrite_touches_only_diff_headers_of_moved_files() -> None:
    rewritten = port_upstream.rewrite_diff(DIFF, MOVED)
    expected = (
        DIFF.replace(
            b"diff --git a/mower_sdk/client.py b/mower_sdk/client.py\nindex f3b25fd..c0c3f26 100644\n"
            b"--- a/mower_sdk/client.py\n+++ b/mower_sdk/client.py\n",
            b"diff --git a/mower_sdk/legacy/client.py b/mower_sdk/legacy/client.py\n"
            b"index f3b25fd..c0c3f26 100644\n"
            b"--- a/mower_sdk/legacy/client.py\n+++ b/mower_sdk/legacy/client.py\n",
        )
        .replace(
            b"diff --git a/mower_sdk/navimow.py b/mower_sdk/navimow2.py\n",
            b"diff --git a/mower_sdk/legacy/navimow.py b/mower_sdk/navimow2.py\n",
        )
        .replace(b"rename from mower_sdk/navimow.py\n", b"rename from mower_sdk/legacy/navimow.py\n")
        .replace(
            b"diff --git a/mower_sdk/client.py b/mower_sdk/client.py\n"
            b"--- a/mower_sdk/client.py\n+++ b/mower_sdk/client.py\n@@ -1 +1,2 @@\n",
            b"diff --git a/mower_sdk/legacy/client.py b/mower_sdk/legacy/client.py\n"
            b"--- a/mower_sdk/legacy/client.py\n+++ b/mower_sdk/legacy/client.py\n@@ -1 +1,2 @@\n",
        )
    )
    assert rewritten == expected
    # The README hunk body, which looks like headers, and the binary patch are untouched.
    assert (
        b"@@ -1,3 +1,3 @@\n mower_sdk/client.py | 1 +\n--- a/mower_sdk/client.py\n"
        b"+++ b/mower_sdk/navimow.py\n diff --git a/mower_sdk/client.py b/mower_sdk/client.py\n"
        b"\\ No newline at end of file\n"
    ) in rewritten
    assert b"Tcmb<--- a/mower_sdk/client.py\n" in rewritten
    assert b"+ mower_sdk/client.py | 1 +\n" in rewritten


def test_rewrite_is_the_identity_for_unmapped_paths() -> None:
    assert port_upstream.rewrite_diff(DIFF, {}) == DIFF


def test_hunk_counting_treats_a_stripped_context_line_as_context() -> None:
    diff = (
        b"diff --git a/mower_sdk/client.py b/mower_sdk/client.py\n"
        b"--- a/mower_sdk/client.py\n+++ b/mower_sdk/client.py\n"
        b"@@ -1,2 +1,2 @@\n"
        b"\n"  # a context line whose leading space was stripped
        b"--- a/mower_sdk/client.py\n"
        b"+++ b/mower_sdk/client.py\n"
    )
    rewritten = port_upstream.rewrite_diff(diff, MOVED)
    assert rewritten.endswith(b"@@ -1,2 +1,2 @@\n\n--- a/mower_sdk/client.py\n+++ b/mower_sdk/client.py\n")
    assert rewritten.count(b"mower_sdk/legacy/client.py") == 4


def test_lines_are_split_on_lf_only() -> None:
    """A hunk line holding a vertical tab is one line to git, and stays one line here."""
    body = (
        b"hello\x0bdiff --git a/mower_sdk/client.py b/mower_sdk/client.py"
        b"\x0b--- a/mower_sdk/client.py\x0b+++ b/mower_sdk/client.py\x0c\n"
    )
    diff = (
        b"diff --git a/x.txt b/x.txt\nnew file mode 100644\nindex 0000000..1111111\n"
        b"--- /dev/null\n+++ b/x.txt\n@@ -0,0 +1 @@\n+" + body
        + b"diff --git a/mower_sdk/client.py b/mower_sdk/client.py\n"
        b"--- a/mower_sdk/client.py\n+++ b/mower_sdk/client.py\n@@ -1 +1 @@\n-a\n+b\n"
    )
    rewritten = port_upstream.rewrite_diff(diff, MOVED)
    assert b"+" + body in rewritten
    assert rewritten.endswith(
        b"diff --git a/mower_sdk/legacy/client.py b/mower_sdk/legacy/client.py\n"
        b"--- a/mower_sdk/legacy/client.py\n+++ b/mower_sdk/legacy/client.py\n@@ -1 +1 @@\n-a\n+b\n"
    )
    assert port_upstream.split_lines(b"a\x0bb\nc") == [b"a\x0bb\n", b"c"]


def test_crlf_content_is_rewritten_byte_for_byte() -> None:
    diff = (
        b"diff --git a/mower_sdk/client.py b/mower_sdk/client.py\n"
        b"--- a/mower_sdk/client.py\n+++ b/mower_sdk/client.py\n"
        b"@@ -1,2 +1,2 @@\n class MowerClient:\r\n-    pass\r\n+    crlf = True\r\n"
    )
    assert port_upstream.rewrite_diff(diff, MOVED).endswith(
        b"@@ -1,2 +1,2 @@\n class MowerClient:\r\n-    pass\r\n+    crlf = True\r\n"
    )


# ---- the scan and the port, on a temporary repository -----------------------------------------

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Upstream",
    "GIT_AUTHOR_EMAIL": "upstream@example.invalid",
    "GIT_COMMITTER_NAME": "Upstream",
    "GIT_COMMITTER_EMAIL": "upstream@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}
DUP_BASE = """\
TCP = {
    "a": 1,
}


def make():
    pass
"""
DUP_INSERTED = """\
TCP = {
    "a": 1,
}


WSS = {
    "b": 2,
}


def make():
    pass
"""
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
       \\-- dup-base -- dup-a ---- dup-merge   (dup-a and dup-b insert the same block)
                  \\-- dup-b --/
       \\-- dup-b --/
       \\-- crlf-edit   (a CRLF file)
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

    # Both sides insert the same block, whose tail repeats the context before it,
    # so the hunk still matches (at an offset) once the block is there.
    run("checkout", "-q", "-b", "dup-a", "base")
    write("mower_sdk/client.py", DUP_BASE)
    run("commit", "-q", "-am", "base with a second block")
    run("tag", "dup-base")
    write("mower_sdk/client.py", DUP_INSERTED)
    run("commit", "-q", "-am", "shared insertion, side a")
    run("checkout", "-q", "-b", "dup-b", "dup-base")
    write("mower_sdk/client.py", DUP_INSERTED)
    run("commit", "-q", "-am", "shared insertion, side b")
    run("tag", "dup-b")
    run("checkout", "-q", "dup-a")
    run("merge", "-q", "--no-ff", "-m", "merge of the same insertion", "dup-b")
    run("tag", "dup-merge")

    run("checkout", "-q", "-b", "crlf", "base")
    (tmp_path / "mower_sdk" / "client.py").write_bytes(b"class MowerClient:\r\n    crlf = True\r\n")
    run("commit", "-q", "-am", "crlf edit")
    run("tag", "crlf-edit")

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


def git_raw(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True).stdout


def test_commits_in_follows_the_first_parent_line(repo: Path) -> None:
    commits = port_upstream.commits_in("base..clean-merge")
    assert [git_out(repo, "log", "-1", "--format=%s", sha).strip() for sha, _ in commits] == [
        "core edit",
        "clean merge",
    ]
    assert [len(parents) for _, parents in commits] == [1, 2]
    assert commits[1][1][0] == commits[0][0]  # the merge's first parent is the previous step


@pytest.mark.usefixtures("repo")
def test_files_touched_and_merged_commits_are_against_the_first_parent() -> None:
    def parents(commit: str) -> list[str]:
        return port_upstream.git("rev-list", "--parents", "-n1", commit).stdout.split()[1:]

    assert port_upstream.files_touched("side-edit", parents("side-edit")) == ["mower_sdk/client.py"]
    assert port_upstream.files_touched("base", []) == [
        "mower_sdk/client.py", "mower_sdk/mqtt.py", "mower_sdk/utils.py"
    ]
    assert port_upstream.files_touched("clean-merge", parents("clean-merge")) == ["mower_sdk/client.py"]
    assert port_upstream.files_touched("evil-merge", parents("evil-merge")) == ["mower_sdk/utils.py"]
    assert port_upstream.files_touched("ours-merge", parents("ours-merge")) == []
    assert port_upstream.files_touched("dup-merge", parents("dup-merge")) == []
    (squashed,) = port_upstream.merged_commits(parents("clean-merge"))
    assert port_upstream.subject(squashed).endswith(" side edit")
    assert port_upstream.merged_commits(parents("ours-merge")) == [
        port_upstream.git("rev-parse", "side3").stdout.strip()
    ]


@pytest.mark.usefixtures("repo")
def test_scan_refuses_a_mixed_file_before_applying_anything(capsys: pytest.CaptureFixture[str]) -> None:
    assert port_upstream.main(["base..clean-merge"]) == 2
    captured = capsys.readouterr()
    assert "core edit: mower_sdk/mqtt.py" in captured.err
    assert "clean merge: merge commit, ported as its net change against its first parent, squashing 1 commit(s):" in captured.out
    assert "    " in captured.out and "side edit" in captured.out


@pytest.mark.usefixtures("repo")
def test_a_merge_resolved_by_hand_is_ported_as_its_net_change(
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    assert port_upstream.main(["--dry-run", "clean-merge..evil-merge"]) == 0
    out = capsysbinary.readouterr().out
    assert b"squashing 1 commit(s):" in out and b"utils edit" in out
    assert out.count(b"\nFrom ") + out.startswith(b"From ") == 1
    assert b"diff --git a/mower_sdk/legacy/utils.py b/mower_sdk/legacy/utils.py\n" in out
    assert b"+    return data or {}  # resolved by hand\n" in out


@pytest.mark.usefixtures("repo")
def test_a_merge_that_kept_its_first_parent_is_skipped(capsys: pytest.CaptureFixture[str]) -> None:
    assert port_upstream.main(["--dry-run", "evil-merge..ours-merge"]) == 3
    captured = capsys.readouterr()
    assert "ours merge: no change against its first parent, skipped" in captured.out
    assert "discarded edit" not in captured.out  # never visited, so never ported
    assert "nothing to port" in captured.err


def test_dry_run_is_the_message_then_the_rewritten_diff(
    repo: Path, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    assert port_upstream.main(["--dry-run", "base..side-edit"]) == 0
    out = capsysbinary.readouterr().out
    mail = git_raw(repo, "log", "-1", "--pretty=mboxrd", "side-edit")
    diff = git_raw(repo, "diff-tree", "--no-commit-id", "-p", "-M", "--binary", "--root", "side-edit")
    assert out.endswith(mail + b"---\n\n" + diff.replace(b"mower_sdk/client.py", b"mower_sdk/legacy/client.py"))
    assert out.count(b"\nFrom ") + out.startswith(b"From ") == 1  # one mbox entry


def test_a_merge_is_ported_as_one_step_with_its_net_change(
    repo: Path, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    assert port_upstream.main(["--dry-run", "core-edit..clean-merge"]) == 0
    out = capsysbinary.readouterr().out
    mail = git_raw(repo, "log", "-1", "--pretty=mboxrd", "clean-merge")
    diff = git_raw(repo, "diff-tree", "--no-commit-id", "-p", "-M", "--binary", "core-edit", "clean-merge")
    assert out.endswith(mail + b"---\n\n" + diff.replace(b"mower_sdk/client.py", b"mower_sdk/legacy/client.py"))
    assert out.count(b"\nFrom ") + out.startswith(b"From ") == 1
    assert b"squashing 1 commit(s):" in out and b"side edit" in out

    git_out(repo, "checkout", "-q", "fork")
    assert port_upstream.main(["core-edit..clean-merge"]) == 0
    assert git_out(repo, "log", "-1", "--format=%an: %s") == "Upstream: clean merge\n"
    assert git_out(repo, "diff", "--name-only", "fork-move", "HEAD") == "mower_sdk/legacy/client.py\n"
    assert (repo / "mower_sdk/legacy/client.py").read_text() == "class MowerClient:\n    token_updates = 0\n"


def test_the_same_insertion_on_both_sides_of_a_merge_is_ported_once(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A clean merge of two identical insertions holds one copy; so does the port.

    The inserted block's tail repeats the context before it, so side b's patch
    still applies, at an offset, once side a's is in; replaying both would
    insert the block twice. Only the first-parent walk keeps the second copy
    out.
    """
    side_b = git_raw(repo, "format-patch", "--stdout", "-1", "dup-b")
    git_out(repo, "checkout", "-q", "dup-a")
    check = subprocess.run(
        ["git", "-C", str(repo), "apply", "--check", "--verbose"], input=side_b, capture_output=True
    )
    assert check.returncode == 0 and b"offset" in check.stderr  # the duplicate would go in

    git_out(repo, "checkout", "-q", "fork")
    assert port_upstream.main(["base..dup-merge"]) == 0
    captured = capsys.readouterr()
    assert "merge of the same insertion: no change against its first parent, skipped" in captured.out
    assert "applied 2 commit(s)" in captured.out
    assert git_out(repo, "log", "--format=%s", "fork-move..HEAD") == (
        "shared insertion, side a\nbase with a second block\n"
    )
    content = (repo / "mower_sdk/legacy/client.py").read_bytes()
    assert content == DUP_INSERTED.encode()
    assert content.count(b"WSS = {") == 1


def test_a_message_that_looks_like_a_patch_is_copied_byte_for_byte(
    repo: Path, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    assert port_upstream.main(["--dry-run", "base..quoted-edit"]) == 0
    out = capsysbinary.readouterr().out
    mail = git_raw(repo, "log", "-1", "--pretty=mboxrd", "quoted-edit")
    assert mail in out
    body = PATHOLOGICAL_MESSAGE.split("\n", 2)[2].replace("From here", ">From here").encode()
    assert body in mail  # git's mboxrd quoting is the only change to the message
    assert out.count(b"diff --git a/mower_sdk/legacy/client.py b/mower_sdk/legacy/client.py") == 1


def test_crlf_content_is_ported_byte_for_byte(
    repo: Path, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    assert port_upstream.main(["--dry-run", "base..crlf-edit"]) == 0
    assert b"+    crlf = True\r\n" in capsysbinary.readouterr().out
    git_out(repo, "checkout", "-q", "fork")
    assert port_upstream.main(["base..crlf-edit"]) == 0
    assert (repo / "mower_sdk/legacy/client.py").read_bytes() == b"class MowerClient:\r\n    crlf = True\r\n"


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
