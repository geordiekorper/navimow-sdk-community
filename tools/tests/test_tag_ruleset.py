"""The ruleset for release tags: only a repository admin creates, moves or deletes a v* tag."""

from __future__ import annotations

import json

from conftest import ROOT

RULESET = json.loads(
    (ROOT / ".github" / "rulesets" / "release-tags.json").read_text(encoding="utf-8")
)


def test_the_ruleset_holds_every_v_tag_and_nothing_else() -> None:
    assert RULESET["target"] == "tag"
    assert RULESET["enforcement"] == "active"
    assert RULESET["conditions"] == {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}}


def test_a_v_tag_is_not_created_moved_or_deleted_except_by_an_admin() -> None:
    assert [rule["type"] for rule in RULESET["rules"]] == ["creation", "update", "deletion"]
    # Repository role 5 is admin; in a repository owned by a user, that is the owner.
    assert RULESET["bypass_actors"] == [
        {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}
    ]
