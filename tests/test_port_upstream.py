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
            b"diff --git a/mower_sdk/client.py b/mower_sdk/client.py\n"
            b"index f3b25fd..c0c3f26 100644\n"
            b"--- a/mower_sdk/client.py\n+++ b/mower_sdk/client.py\n",
            b"diff --git a/mower_sdk/legacy/client.py b/mower_sdk/legacy/client.py\n"
            b"index f3b25fd..c0c3f26 100644\n"
            b"--- a/mower_sdk/legacy/client.py\n+++ b/mower_sdk/legacy/client.py\n",
        )
        .replace(
            b"diff --git a/mower_sdk/navimow.py b/mower_sdk/navimow2.py\n",
            b"diff --git a/mower_sdk/legacy/navimow.py b/mower_sdk/navimow2.py\n",
        )
        .replace(
            b"rename from mower_sdk/navimow.py\n", b"rename from mower_sdk/legacy/navimow.py\n"
        )
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
    assert rewritten.endswith(
        b"@@ -1,2 +1,2 @@\n\n--- a/mower_sdk/client.py\n+++ b/mower_sdk/client.py\n"
    )
    assert rewritten.count(b"mower_sdk/legacy/client.py") == 4


def test_lines_are_split_on_lf_only() -> None:
    """A hunk line holding a vertical tab is one line to git, and stays one line here."""
    body = (
        b"hello\x0bdiff --git a/mower_sdk/client.py b/mower_sdk/client.py"
        b"\x0b--- a/mower_sdk/client.py\x0b+++ b/mower_sdk/client.py\x0c\n"
    )
    diff = (
        b"diff --git a/x.txt b/x.txt\nnew file mode 100644\nindex 0000000..1111111\n"
        b"--- /dev/null\n+++ b/x.txt\n@@ -0,0 +1 @@\n+"
        + body
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


# ---- the trailer, on a message alone -------------------------------------------------------------


def test_the_trailer_is_a_paragraph_of_its_own_after_a_body() -> None:
    message = b"s\n\nWhy.\nAnd: how.\n"
    assert port_upstream.with_trailer(message, "abc") == message + b"\nUpstream-commit: abc\n"


def test_the_trailer_joins_a_trailer_block() -> None:
    message = (
        b"s\n\nWhy.\n\nSigned-off-by: U <u@example.invalid>\nReviewed-by: V <v@example.invalid>\n"
    )
    assert port_upstream.with_trailer(message, "abc") == message + b"Upstream-commit: abc\n"


def test_the_trailer_follows_a_subject_with_no_body() -> None:
    assert port_upstream.with_trailer(b"s\n", "abc") == b"s\n\nUpstream-commit: abc\n"


def test_a_title_in_the_shape_of_a_trailer_is_not_a_trailer_block() -> None:
    """Git never reads trailers from the first paragraph, so the trailer gets its own."""
    assert port_upstream.with_trailer(b"fix: repair the client\n", "abc") == (
        b"fix: repair the client\n\nUpstream-commit: abc\n"
    )
    assert port_upstream.with_trailer(b"fix: repair\n\nSigned-off-by: U <u@x>\n", "abc") == (
        b"fix: repair\n\nSigned-off-by: U <u@x>\nUpstream-commit: abc\n"
    )


def test_the_trailer_is_the_whole_of_an_empty_message() -> None:
    assert port_upstream.with_trailer(b"", "abc") == b"Upstream-commit: abc\n"


def test_blank_lines_at_the_end_of_a_message_do_not_separate_the_trailer_twice() -> None:
    assert port_upstream.with_trailer(b"s\n\nWhy.\n\n\n", "abc") == (
        b"s\n\nWhy.\n\nUpstream-commit: abc\n"
    )


# ---- the scan and the port, on a temporary repository -----------------------------------------

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Upstream",
    "GIT_AUTHOR_EMAIL": "upstream@example.invalid",
    "GIT_COMMITTER_NAME": "Porter",
    "GIT_COMMITTER_EMAIL": "porter@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}
# The author date of the side edit, so the port's copy of it can be checked.
SIDE_EDIT_DATE = "1700000000 +0200"
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
# Every shape git am reshapes or cuts: a bracketed subject prefix, a first paragraph
# of two lines, a body starting with header-like lines, trailing whitespace, a
# quoted patch with its separator, a run of blank lines, a CR, a "From " line
# and a blank line at the end.
PATHOLOGICAL_MESSAGE = (
    b"[WIP] quoted edit\n"
    b"on a second subject line\n"
    b"\n"
    b"From: not a header\n"
    b"Date: not a header either\n"
    b"The message quotes a patch, a separator and a From line:  \n"
    b" mower_sdk/client.py | 1 +\n"
    b"---\n"
    b"diff --git a/mower_sdk/client.py b/mower_sdk/client.py\n"
    b"--- a/mower_sdk/client.py\n"
    b"\n"
    b"\n"
    b"From here on, nothing.\r\n"
    b"\n"
)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    r"""A small repository shaped like upstream's history, with the tool pointed at it.

    base ---- core-edit ---- clean-merge ---- evil-merge ---- ours-merge   (main)
       \-- side-edit --/           /                /
       \-- utils-edit ------------/                /
       \-- discarded-edit -------------------------/
       \-- dup-base -- dup-a ---- dup-merge   (dup-a and dup-b insert the same block)
                  \-- dup-b --/
       \-- dup-b --/
       \-- crlf-edit   (a CRLF file)
       \-- quoted-edit  (a commit whose message holds every shape git am would alter)
       \-- latin-edit   (a commit with an encoding header)
       \-- fix-edit     (a commit whose whole message is one trailer-shaped line)
       \-- fork: client.py moved to legacy/client.py with a shim, as this repository did

    side-edit has a second commit, side-edit-2, which adds a file.
    """
    # A git hook or alias exports GIT_DIR, GIT_INDEX_FILE and the like; left in
    # place they would point every command below at the caller's repository.
    for name in os.environ:
        if name.startswith("GIT_") and name != "GIT_EXEC_PATH":
            monkeypatch.delenv(name)
    for name, value in GIT_ENV.items():
        monkeypatch.setenv(name, value)

    def run(*args: str, data: bytes | None = None) -> str:
        return subprocess.run(
            ["git", "-C", str(tmp_path), *args],
            input=data,
            capture_output=True,
            check=True,
            text=data is None,
        ).stdout

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
    monkeypatch.setenv("GIT_AUTHOR_DATE", SIDE_EDIT_DATE)
    run("commit", "-q", "-am", "side edit")
    monkeypatch.delenv("GIT_AUTHOR_DATE")
    run("tag", "side-edit")
    write("notes.txt", "added upstream\n")
    run("add", "-A")
    run("commit", "-q", "-m", "side edit two")
    run("tag", "side-edit-2")

    run("checkout", "-q", "main")
    write("mower_sdk/mqtt.py", "class NavimowMQTT:\n    keepalive = 60\n")
    run("commit", "-q", "-am", "core edit")
    run("tag", "core-edit")
    run("merge", "-q", "--no-ff", "-m", "clean merge", "side-edit")
    run("tag", "clean-merge")

    run("checkout", "-q", "-b", "side2", "base")
    write("mower_sdk/utils.py", "def parse_json(data):\n    return data or {}\n")
    run("commit", "-q", "-am", "utils edit")
    run("checkout", "-q", "main")
    run("merge", "-q", "--no-ff", "--no-commit", "side2")
    write(
        "mower_sdk/utils.py", "def parse_json(data):\n    return data or {}  # resolved by hand\n"
    )
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
    run("commit", "-q", "-a", "--cleanup=verbatim", "-F", "-", data=PATHOLOGICAL_MESSAGE)
    run("tag", "quoted-edit")

    run("checkout", "-q", "-b", "latin", "base")
    write("mower_sdk/client.py", "class MowerClient:\n    latin = True\n")
    run("-c", "i18n.commitEncoding=ISO-8859-1", "commit", "-q", "-a", "-F", "-", data=b"caf\xe9\n")
    run("tag", "latin-edit")

    run("checkout", "-q", "-b", "fix", "base")
    write("mower_sdk/client.py", "class MowerClient:\n    repaired = True\n")
    run("commit", "-q", "-am", "fix: repair the client")
    run("tag", "fix-edit")

    run("checkout", "-q", "-b", "fork", "base")
    (tmp_path / "mower_sdk" / "legacy").mkdir()
    run("mv", "mower_sdk/client.py", "mower_sdk/legacy/client.py")
    write("mower_sdk/client.py", "from mower_sdk.legacy.client import *\n")
    run("add", "-A")
    run("commit", "-q", "-m", "fork: move client.py to legacy/")
    run("tag", "fork-move")

    monkeypatch.setattr(port_upstream, "REPO_ROOT", tmp_path)
    return tmp_path


@pytest.fixture
def decoy(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, bytes]:
    """Another repository, exported through GIT_* the way a git hook exports its own.

    Returns the repository and its configuration as it was before any other
    fixture ran.
    """
    decoy = tmp_path_factory.mktemp("decoy")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")} | GIT_ENV
    subprocess.run(["git", "-C", str(decoy), "init", "-q"], check=True, env=env)
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))
    monkeypatch.setenv("GIT_INDEX_FILE", str(decoy / ".git" / "index"))
    return decoy, (decoy / ".git" / "config").read_bytes()


def test_the_fixture_ignores_git_variables_exported_by_a_caller(
    decoy: tuple[Path, bytes], repo: Path
) -> None:
    decoy_git = decoy[0] / ".git"
    assert (
        git_out(repo, "log", "-1", "--format=%s", "fork-move").strip()
        == "fork: move client.py to legacy/"
    )
    assert (decoy_git / "config").read_bytes() == decoy[1]
    assert not (decoy_git / "index").exists()
    assert not list((decoy_git / "refs" / "heads").iterdir())


def git_out(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def git_raw(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True).stdout


def sha_of(repo: Path, rev: str) -> bytes:
    return git_out(repo, "rev-parse", rev).strip().encode()


def stored_message(repo: Path, rev: str) -> bytes:
    """The message bytes of a commit object, after the blank line that ends its headers."""
    return git_raw(repo, "cat-file", "commit", rev).partition(b"\n\n")[2]


def author_line(repo: Path, rev: str) -> bytes:
    """The value of a commit object's author header."""
    headers = git_raw(repo, "cat-file", "commit", rev).partition(b"\n\n")[0]
    return next(line[7:] for line in headers.split(b"\n") if line.startswith(b"author "))


def rewritten_diff(repo: Path, *trees: str) -> bytes:
    diff = git_raw(
        repo, "diff-tree", "--no-commit-id", "-p", "-M", "--binary", "--full-index", *trees
    )
    return diff.replace(b"mower_sdk/client.py", b"mower_sdk/legacy/client.py")


def state_dir(repo: Path) -> Path:
    return repo / ".git" / "port-upstream"


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
        "mower_sdk/client.py",
        "mower_sdk/mqtt.py",
        "mower_sdk/utils.py",
    ]
    assert port_upstream.files_touched("clean-merge", parents("clean-merge")) == [
        "mower_sdk/client.py"
    ]
    assert port_upstream.files_touched("evil-merge", parents("evil-merge")) == [
        "mower_sdk/utils.py"
    ]
    assert port_upstream.files_touched("ours-merge", parents("ours-merge")) == []
    assert port_upstream.files_touched("dup-merge", parents("dup-merge")) == []
    (squashed,) = port_upstream.merged_commits(parents("clean-merge"))
    assert port_upstream.subject(squashed).endswith(" side edit")
    assert port_upstream.merged_commits(parents("ours-merge")) == [
        port_upstream.git("rev-parse", "side3").stdout.strip()
    ]


def test_commit_object_reads_the_author_the_encoding_and_the_message_as_stored(repo: Path) -> None:
    author, encoding, message = port_upstream.commit_object("quoted-edit")
    assert author == author_line(repo, "quoted-edit")
    assert author.startswith(b"Upstream <upstream@example.invalid> ")
    assert encoding is None
    assert message == PATHOLOGICAL_MESSAGE
    author, encoding, message = port_upstream.commit_object("latin-edit")
    assert (encoding, message) == (b"ISO-8859-1", b"caf\xe9\n")
    assert port_upstream.commit_object("side-edit")[0].endswith(SIDE_EDIT_DATE.encode())


@pytest.mark.usefixtures("repo")
def test_scan_refuses_a_mixed_file_before_applying_anything(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert port_upstream.main(["base..clean-merge"]) == 2
    captured = capsys.readouterr()
    assert "core edit: mower_sdk/mqtt.py" in captured.err
    assert (
        "clean merge: merge commit, ported as its net change against its first parent, "
        "squashing 1 commit(s):" in captured.out
    )
    assert "    " in captured.out and "side edit" in captured.out


@pytest.mark.usefixtures("repo")
def test_a_merge_resolved_by_hand_is_ported_as_its_net_change(
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    assert port_upstream.main(["--dry-run", "clean-merge..evil-merge"]) == 0
    out = capsysbinary.readouterr().out
    assert b"squashing 1 commit(s):" in out and b"utils edit" in out
    assert out.count(b"\ncommit ") + out.startswith(b"commit ") == 1
    assert b"diff --git a/mower_sdk/legacy/utils.py b/mower_sdk/legacy/utils.py\n" in out
    assert b"+    return data or {}  # resolved by hand\n" in out


@pytest.mark.usefixtures("repo")
def test_a_merge_that_kept_its_first_parent_is_skipped(capsys: pytest.CaptureFixture[str]) -> None:
    assert port_upstream.main(["--dry-run", "evil-merge..ours-merge"]) == 3
    captured = capsys.readouterr()
    assert "ours merge: no change against its first parent, skipped" in captured.out
    assert "discarded edit" not in captured.out  # never visited, so never ported
    assert "nothing to port" in captured.err


def test_dry_run_is_the_commit_its_trailer_and_the_rewritten_diff(
    repo: Path, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    assert port_upstream.main(["--dry-run", "base..side-edit"]) == 0
    out = capsysbinary.readouterr().out
    sha = sha_of(repo, "side-edit")
    assert out.endswith(
        b"commit "
        + sha
        + b"\nauthor "
        + author_line(repo, "side-edit")
        + b"\n\nside edit\n\nUpstream-commit: "
        + sha
        + b"\n\n"
        + rewritten_diff(repo, "--root", "side-edit")
    )
    assert out.count(b"\ncommit ") + out.startswith(b"commit ") == 1  # one step
    assert git_out(repo, "status", "--porcelain") == ""
    assert not state_dir(repo).exists()


def test_a_merge_is_ported_as_one_step_with_its_net_change(
    repo: Path, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    assert port_upstream.main(["--dry-run", "core-edit..clean-merge"]) == 0
    out = capsysbinary.readouterr().out
    sha = sha_of(repo, "clean-merge")
    assert out.endswith(
        b"\n\nclean merge\n\nUpstream-commit: "
        + sha
        + b"\n\n"
        + rewritten_diff(repo, "core-edit", "clean-merge")
    )
    assert out.count(b"\ncommit ") + out.startswith(b"commit ") == 1
    assert b"squashing 1 commit(s):" in out and b"side edit" in out

    git_out(repo, "checkout", "-q", "fork")
    assert port_upstream.main(["core-edit..clean-merge"]) == 0
    assert git_out(repo, "log", "-1", "--format=%an: %s") == "Upstream: clean merge\n"
    assert stored_message(repo, "HEAD") == b"clean merge\n\nUpstream-commit: " + sha + b"\n"
    assert (
        git_out(repo, "diff", "--name-only", "fork-move", "HEAD") == "mower_sdk/legacy/client.py\n"
    )
    assert (
        repo / "mower_sdk/legacy/client.py"
    ).read_text() == "class MowerClient:\n    token_updates = 0\n"


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
    assert (
        "merge of the same insertion: no change against its first parent, skipped" in captured.out
    )
    assert "applied base..dup-merge" in captured.out
    assert git_out(repo, "log", "--format=%s", "fork-move..HEAD") == (
        "shared insertion, side a\nbase with a second block\n"
    )
    content = (repo / "mower_sdk/legacy/client.py").read_bytes()
    assert content == DUP_INSERTED.encode()
    assert content.count(b"WSS = {") == 1


def test_a_message_git_am_would_alter_is_ported_byte_for_byte(repo: Path) -> None:
    assert stored_message(repo, "quoted-edit") == PATHOLOGICAL_MESSAGE  # stored as written
    git_out(repo, "checkout", "-q", "fork")
    assert port_upstream.main(["base..quoted-edit"]) == 0
    sha = sha_of(repo, "quoted-edit")
    assert stored_message(repo, "HEAD") == (
        PATHOLOGICAL_MESSAGE.rstrip(b"\n") + b"\n\nUpstream-commit: " + sha + b"\n"
    )
    assert (
        repo / "mower_sdk/legacy/client.py"
    ).read_text() == "class MowerClient:\n    quoted = True\n"
    assert git_out(repo, "status", "--porcelain") == ""


def test_a_ported_one_line_message_carries_a_trailer_git_reads(repo: Path) -> None:
    git_out(repo, "checkout", "-q", "fork")
    assert port_upstream.main(["base..fix-edit"]) == 0
    sha = sha_of(repo, "fix-edit")
    assert (
        stored_message(repo, "HEAD") == b"fix: repair the client\n\nUpstream-commit: " + sha + b"\n"
    )
    trailers = git_out(repo, "log", "-1", "--format=%(trailers:key=Upstream-commit,valueonly)")
    assert trailers.strip() == sha.decode()


def test_a_commits_encoding_header_is_carried_over(repo: Path) -> None:
    git_out(repo, "checkout", "-q", "fork")
    assert port_upstream.main(["base..latin-edit"]) == 0
    headers, _, message = git_raw(repo, "cat-file", "commit", "HEAD").partition(b"\n\n")
    assert b"\nencoding ISO-8859-1\n" in headers + b"\n"
    assert message == b"caf\xe9\n\nUpstream-commit: " + sha_of(repo, "latin-edit") + b"\n"


def test_a_message_without_an_encoding_header_is_not_labelled_with_the_local_one(
    repo: Path,
) -> None:
    git_out(repo, "config", "i18n.commitEncoding", "ISO-8859-1")
    git_out(repo, "checkout", "-q", "fork")
    assert port_upstream.main(["base..side-edit"]) == 0
    headers = git_raw(repo, "cat-file", "commit", "HEAD").partition(b"\n\n")[0]
    assert b"\nencoding " not in headers


FAKE_GPG = """\
#!/bin/sh
# Stands in for gpg: reads the buffer, reports a signature made, prints one.
cat >/dev/null
printf '\\n[GNUPG:] SIG_CREATED D 1 8 00 0 0\\n' >&2
printf -- '-----BEGIN PGP SIGNATURE-----\\n\\nfake\\n-----END PGP SIGNATURE-----\\n'
"""


def test_a_ported_commit_is_signed_when_commit_gpgsign_is_set(repo: Path) -> None:
    fake = repo / ".git" / "fake-gpg.sh"  # under .git, so git status does not list it
    fake.write_text(FAKE_GPG)
    fake.chmod(0o755)
    git_out(repo, "config", "gpg.program", str(fake))
    git_out(repo, "config", "commit.gpgsign", "true")
    git_out(repo, "checkout", "-q", "fork")
    assert port_upstream.main(["base..side-edit"]) == 0
    headers = git_raw(repo, "cat-file", "commit", "HEAD").partition(b"\n\n")[0]
    assert b"\ngpgsig -----BEGIN PGP SIGNATURE-----\n" in headers
    assert git_out(repo, "log", "-1", "--format=%an: %s") == "Upstream: side edit\n"


def failing_signature(repo: Path) -> None:
    """Make every commit's signature fail: a gpg stand-in that exits 1, with signing on."""
    fake = repo / ".git" / "failing-gpg.sh"  # under .git, so git status does not list it
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)
    git_out(repo, "config", "gpg.program", str(fake))
    git_out(repo, "config", "commit.gpgsign", "true")


def test_a_commit_git_refuses_stops_the_port_with_the_step_staged_and_continue_retries(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    failing_signature(repo)
    git_out(repo, "checkout", "-q", "fork")
    head = git_out(repo, "rev-parse", "HEAD")
    assert port_upstream.main(["base..side-edit-2"]) == 1
    err = capsys.readouterr().err
    assert "gpg failed to sign" in err
    assert "the port stopped at" in err and "side edit:" in err
    assert "git could not make the commit; the step is applied and staged" in err
    assert git_out(repo, "status", "--porcelain") == "M  mower_sdk/legacy/client.py\n"
    assert git_out(repo, "rev-parse", "HEAD") == head
    assert port_upstream.main(["base..side-edit-2"]) == 3  # stopped, so refused
    capsys.readouterr()

    git_out(repo, "config", "commit.gpgsign", "false")
    assert port_upstream.main(["--continue"]) == 0
    out = capsys.readouterr().out
    assert "side edit: ported as" in out and "side edit two: ported as" in out
    assert git_out(repo, "log", "--format=%an: %s", f"{head.strip()}..HEAD") == (
        "Upstream: side edit two\nUpstream: side edit\n"
    )
    assert git_out(repo, "status", "--porcelain") == ""
    assert not state_dir(repo).exists()


def test_a_commit_git_refuses_can_be_skipped_or_aborted(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    failing_signature(repo)
    git_out(repo, "checkout", "-q", "fork")
    head = git_out(repo, "rev-parse", "HEAD")
    assert port_upstream.main(["base..side-edit"]) == 1
    assert port_upstream.main(["--abort"]) == 0
    assert git_out(repo, "rev-parse", "HEAD") == head
    assert git_out(repo, "status", "--porcelain") == ""
    assert port_upstream.main(["base..side-edit-2"]) == 1
    capsys.readouterr()
    git_out(repo, "config", "commit.gpgsign", "false")
    assert port_upstream.main(["--skip"]) == 0
    out = capsys.readouterr().out
    assert "side edit: skipped" in out and "side edit two: ported as" in out
    assert git_out(repo, "log", "--format=%s", f"{head.strip()}..HEAD") == "side edit two\n"
    assert (repo / "mower_sdk/legacy/client.py").read_text() == "class MowerClient:\n    pass\n"


def test_no_commit_hook_runs_and_the_reference_transaction_hook_does(repo: Path) -> None:
    git_out(repo, "checkout", "-q", "fork")
    hooks = repo / ".git" / "hooks"
    for name in ("pre-commit", "commit-msg", "applypatch-msg", "pre-applypatch", "post-applypatch"):
        hook = hooks / name
        hook.write_text(f"#!/bin/sh\necho {name} >> {repo / 'hooks.log'}\nexit 1\n")
        hook.chmod(0o755)
    ref_hook = hooks / "reference-transaction"
    ref_hook.write_text(f"#!/bin/sh\necho $1 >> {repo / 'refs.log'}\n")
    ref_hook.chmod(0o755)
    assert port_upstream.main(["base..side-edit"]) == 0
    assert git_out(repo, "log", "-1", "--format=%s") == "side edit\n"
    assert not (repo / "hooks.log").exists()
    assert "committed" in (repo / "refs.log").read_text()


def test_crlf_content_is_ported_byte_for_byte(
    repo: Path, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    assert port_upstream.main(["--dry-run", "base..crlf-edit"]) == 0
    assert b"+    crlf = True\r\n" in capsysbinary.readouterr().out
    git_out(repo, "checkout", "-q", "fork")
    assert port_upstream.main(["base..crlf-edit"]) == 0
    assert (
        repo / "mower_sdk/legacy/client.py"
    ).read_bytes() == b"class MowerClient:\r\n    crlf = True\r\n"


def test_port_applies_the_series_into_legacy_with_upstream_authorship(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    git_out(repo, "checkout", "-q", "fork")
    assert port_upstream.main(["base..side-edit"]) == 0
    out = capsys.readouterr().out
    assert "side edit: ported as " + git_out(repo, "rev-parse", "--short=7", "HEAD").strip() in out
    assert "applied base..side-edit" in out
    assert git_out(repo, "log", "-1", "--format=%an <%ae>|%ad|%cn <%ce>: %s", "--date=raw") == (
        f"Upstream <upstream@example.invalid>|{SIDE_EDIT_DATE}|Porter <porter@example.invalid>: "
        "side edit\n"
    )
    assert stored_message(repo, "HEAD") == (
        b"side edit\n\nUpstream-commit: " + sha_of(repo, "side-edit") + b"\n"
    )
    headers = git_raw(repo, "cat-file", "commit", "HEAD").partition(b"\n\n")[0]
    assert b"gpgsig" not in headers and b"encoding" not in headers  # unsigned, UTF-8
    assert (
        git_out(repo, "diff", "--name-only", "fork-move", "HEAD") == "mower_sdk/legacy/client.py\n"
    )
    assert (
        repo / "mower_sdk/legacy/client.py"
    ).read_text() == "class MowerClient:\n    token_updates = 0\n"
    assert (repo / "mower_sdk/client.py").read_text() == "from mower_sdk.legacy.client import *\n"
    assert git_out(repo, "status", "--porcelain") == ""
    assert not state_dir(repo).exists()
    assert "port_upstream: " in git_out(repo, "reflog", "-1")


def test_a_step_whose_change_is_already_in_the_tree_makes_no_commit(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    git_out(repo, "checkout", "-q", "fork")
    (repo / "mower_sdk/legacy/client.py").write_text("class MowerClient:\n    token_updates = 0\n")
    git_out(repo, "commit", "-q", "-am", "fork: the same edit")
    head = git_out(repo, "rev-parse", "HEAD")
    assert port_upstream.main(["base..side-edit-2"]) == 0
    out = capsys.readouterr().out
    assert "side edit: already in the tree, no commit made" in out
    assert "side edit two: ported as" in out
    assert git_out(repo, "log", "--format=%s", f"{head.strip()}..HEAD") == "side edit two\n"
    assert git_out(repo, "status", "--porcelain") == ""


def conflicting_fork(repo: Path) -> str:
    """Check out the fork with an edit that conflicts with the side edit; return HEAD."""
    git_out(repo, "checkout", "-q", "fork")
    (repo / "mower_sdk/legacy/client.py").write_text("class MowerClient:\n    forked = True\n")
    git_out(repo, "commit", "-q", "-am", "fork: conflicting edit")
    return git_out(repo, "rev-parse", "HEAD")


def test_a_conflict_stops_the_port_and_abort_restores_the_branch(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    head = conflicting_fork(repo)
    assert port_upstream.main(["base..side-edit-2"]) == 1
    err = capsys.readouterr().err
    assert "the port stopped at" in err and "side edit:" in err
    assert "resolve the conflicts, git add each file, and run the tool with --continue" in err
    assert "UU mower_sdk/legacy/client.py" in git_out(repo, "status", "--porcelain")
    assert git_out(repo, "rev-parse", "HEAD") == head
    assert (state_dir(repo) / "patch").read_bytes() == rewritten_diff(repo, "base", "side-edit")

    assert port_upstream.main(["base..side-edit-2"]) == 3  # a stopped port is not run over
    assert "a port is stopped" in capsys.readouterr().err

    assert port_upstream.main(["--abort"]) == 0
    assert "is aborted" in capsys.readouterr().out
    assert git_out(repo, "rev-parse", "HEAD") == head
    assert git_out(repo, "status", "--porcelain") == ""
    assert not state_dir(repo).exists()


def test_a_resolved_step_is_committed_by_continue_and_the_rest_is_ported(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    head = conflicting_fork(repo)
    assert port_upstream.main(["base..side-edit-2"]) == 1
    capsys.readouterr()

    assert port_upstream.main(["--continue"]) == 1  # still unmerged
    assert "still unmerged" in capsys.readouterr().err
    assert git_out(repo, "rev-parse", "HEAD") == head

    (repo / "mower_sdk/legacy/client.py").write_text("class MowerClient:\n    token_updates = 0\n")
    git_out(repo, "add", "mower_sdk/legacy/client.py")
    assert port_upstream.main(["--continue"]) == 0
    out = capsys.readouterr().out
    assert "side edit: ported as" in out and "side edit two: ported as" in out
    assert "applied base..side-edit-2" in out
    assert git_out(repo, "log", "--format=%an: %s", f"{head.strip()}..HEAD") == (
        "Upstream: side edit two\nUpstream: side edit\n"
    )
    assert stored_message(repo, "HEAD~1") == (
        b"side edit\n\nUpstream-commit: " + sha_of(repo, "side-edit") + b"\n"
    )
    assert (repo / "notes.txt").read_text() == "added upstream\n"
    assert git_out(repo, "status", "--porcelain") == ""
    assert not state_dir(repo).exists()


def test_skip_drops_the_stopped_step_and_ports_the_rest(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    head = conflicting_fork(repo)
    assert port_upstream.main(["base..side-edit-2"]) == 1
    capsys.readouterr()
    assert port_upstream.main(["--skip"]) == 0
    out = capsys.readouterr().out
    assert "side edit: skipped" in out and "side edit two: ported as" in out
    assert git_out(repo, "log", "--format=%s", f"{head.strip()}..HEAD") == "side edit two\n"
    assert (
        repo / "mower_sdk/legacy/client.py"
    ).read_text() == "class MowerClient:\n    forked = True\n"
    assert git_out(repo, "status", "--porcelain") == ""
    assert not state_dir(repo).exists()


def test_continue_refuses_a_resolution_that_stages_nothing(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    head = conflicting_fork(repo)
    assert port_upstream.main(["base..side-edit"]) == 1
    git_out(repo, "checkout", "HEAD", "--", "mower_sdk/legacy/client.py")  # resolved to ours
    assert port_upstream.main(["--continue"]) == 1
    assert "nothing is staged for" in capsys.readouterr().err
    assert git_out(repo, "rev-parse", "HEAD") == head
    assert state_dir(repo).exists()


def test_abort_after_head_moved_forgets_the_port_and_resets_nothing(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conflicting_fork(repo)
    assert port_upstream.main(["base..side-edit"]) == 1
    git_out(repo, "checkout", "HEAD", "--", "mower_sdk/legacy/client.py")
    (repo / "notes.txt").write_text("meanwhile\n")
    git_out(repo, "add", "-A")
    git_out(repo, "commit", "-q", "-m", "fork: a commit made meanwhile")
    moved = git_out(repo, "rev-parse", "HEAD")
    assert port_upstream.main(["--abort"]) == 1
    assert "HEAD has moved" in capsys.readouterr().err
    assert git_out(repo, "rev-parse", "HEAD") == moved
    assert not state_dir(repo).exists()


@pytest.mark.usefixtures("repo")
def test_continue_skip_and_abort_need_a_stopped_port(capsys: pytest.CaptureFixture[str]) -> None:
    for flag in ("--continue", "--skip", "--abort"):
        assert port_upstream.main([flag]) == 3
        assert "no port is stopped" in capsys.readouterr().err


@pytest.mark.usefixtures("repo")
def test_a_range_is_required_and_excluded_by_the_resume_flags() -> None:
    with pytest.raises(SystemExit) as excinfo:
        port_upstream.main([])
    assert excinfo.value.code == 2
    with pytest.raises(SystemExit) as excinfo:
        port_upstream.main(["--continue", "base..side-edit"])
    assert excinfo.value.code == 2
    with pytest.raises(SystemExit) as excinfo:
        port_upstream.main(["--abort", "--dry-run"])
    assert excinfo.value.code == 2


def test_a_dirty_tree_is_refused_before_anything_is_applied(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    git_out(repo, "checkout", "-q", "fork")
    (repo / "mower_sdk/client.py").write_text("# dirty\n")
    assert port_upstream.main(["base..side-edit"]) == 3
    assert "uncommitted changes" in capsys.readouterr().err
    assert not state_dir(repo).exists()
