"""Fail-closed merge eligibility (docs/evaluation.md rubric R5).

Fixtures build a mini gold/verdicts tree under tmp_path and monkeypatch
merge_verdicts' module paths so promote() runs against the fixture.
"""
import json

import pytest

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "training"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import merge_verdicts as mv  # noqa: E402
from eval.gold import gold_doc_scorable, load_gold  # noqa: E402

SUGG = {
    "item_id": "pure-0001",
    "suggested_requirements": [
        {"id": "FR-1", "kind": "FR", "text": "The system shall log in."},
        {"id": "NFR-1", "kind": "security", "text": "Data is encrypted."},
        {"id": "NFR-2", "kind": "performance", "text": "Responds in 2s."},
    ],
    "suggested_implied_categories": ["security"],
}


def _write_tree(tmp_path, verdicts):
    frozen = tmp_path / "data" / "frozen"
    gold_dir = frozen / "gold" / "dev"
    gold_dir.mkdir(parents=True)
    (gold_dir / "pure-0001.gold.json").write_text(json.dumps({
        "item_id": "pure-0001", "prd_path": "x.txt",
        "implied_categories": [], "gold_requirements": [],
        "provenance": {"status": "SKELETON - awaiting human verification"},
    }))
    sugg_dir = frozen / "gold_llm_suggestions" / "dev"
    sugg_dir.mkdir(parents=True)
    (sugg_dir / "pure-0001.llm_suggestions.json").write_text(
        json.dumps(SUGG))
    ver_dir = frozen / "gold_verdicts" / "dev"
    ver_dir.mkdir(parents=True)
    for v in verdicts:
        (ver_dir / f"pure-0001.{v['annotator']}.verdict.json").write_text(
            json.dumps(v))
    for name, path in [("GOLD", frozen / "gold"),
                       ("VERDICTS", frozen / "gold_verdicts"),
                       ("BACKUP", tmp_path / "backup"),
                       ("ROOT", tmp_path)]:
        monkey = pytest.MonkeyPatch()
        monkey.setattr(mv, name, path)
        PATCHES.append(monkey)


PATCHES = []


@pytest.fixture(autouse=True)
def _undo():
    yield
    for m in PATCHES:
        m.undo()
    PATCHES.clear()


def verdict(annotator, decisions, confirmed=True, presented=120000,
            implied=("security",), added=None):
    return {
        "item_id": "pure-0001", "annotator": annotator, "split": "dev",
        "finished_at": "2026-11-06T00:00:00+00:00",
        "verdicts": [{"id": rid, "decision": d, **(rest[0] if rest
                     else {})}
                     for rid, d, *rest in decisions],
        "added_requirements": added or [],
        "implied_categories": list(implied),
        "implied_categories_reviewed": True,
        "review_scope": {
            "doc_chars_total": 90000, "llm_scope_chars": 90000,
            "chars_presented_to_reviewer": presented,
            "reviewer_read_full_llm_scope": confirmed,
            "reviewer_confirmed_full_read": confirmed},
        "source_suggestion_file":
            "data/frozen/gold_llm_suggestions/dev/"
            "pure-0001.llm_suggestions.json",
    }


def merged(tmp_path):
    res = mv.promote("dev", "pure-0001", dry=False)
    return res, json.loads(
        (tmp_path / "data" / "frozen" / "gold" / "dev"
         / "pure-0001.gold.json").read_text())


def test_clean_single_annotator_freezes(tmp_path):
    _write_tree(tmp_path, [verdict("a1", [("FR-1", "a"), ("NFR-1", "a"),
                                          ("NFR-2", "r")])])
    res, gold = merged(tmp_path)
    assert res["status"] == "frozen"
    prov = gold["provenance"]
    assert prov["status"].startswith("FROZEN - human-verified (anchored")
    assert prov["review_scope"]["reviewer_read_full_llm_scope"] is True
    assert prov["implied_categories_verified"] is True
    assert gold["implied_categories"] == ["security"]
    assert prov["excluded"] == {"rejected": ["NFR-2"]}
    scorable, reason = gold_doc_scorable(gold)
    assert scorable, reason
    assert len(gold["gold_requirements"]) == 2
    load_gold(str(tmp_path / "data" / "frozen" / "gold" / "dev"
                  / "pure-0001.gold.json"))


def test_skip_blocks_document(tmp_path):
    _write_tree(tmp_path, [verdict("a1", [("FR-1", "a"), ("NFR-1", "s"),
                                          ("NFR-2", "a")])])
    res, gold = merged(tmp_path)
    assert res["status"] == "blocked"
    assert gold["provenance"]["status"].startswith("REVIEW-INCOMPLETE")
    assert gold["provenance"]["excluded"]["skipped"] == ["NFR-1"]
    scorable, _ = gold_doc_scorable(gold)
    assert not scorable
    with pytest.raises(ValueError):
        load_gold(str(tmp_path / "data" / "frozen" / "gold" / "dev"
                  / "pure-0001.gold.json"))


def test_tie_blocks_document(tmp_path):
    _write_tree(tmp_path, [
        verdict("a1", [("FR-1", "a"), ("NFR-1", "a"), ("NFR-2", "r")]),
        verdict("a2", [("FR-1", "a"), ("NFR-1", "r"), ("NFR-2", "r")]),
    ])
    res, gold = merged(tmp_path)
    assert res["status"] == "blocked"
    assert gold["provenance"]["excluded"]["no_majority"] == ["NFR-1"]
    assert gold["provenance"]["excluded"]["rejected"] == ["NFR-2"]
    assert gold["provenance"]["kappa_accept_reject"]["a1~a2"] is not None
    scorable, _ = gold_doc_scorable(gold)
    assert not scorable


def test_contested_edits_block_document(tmp_path):
    _write_tree(tmp_path, [
        verdict("a1", [("FR-1", "a"), ("NFR-1", "a", {"new_kind":
                       "security"}), ("NFR-2", "r")]),
        verdict("a2", [("FR-1", "a"), ("NFR-1", "a", {"new_kind":
                       "performance"}), ("NFR-2", "r")]),
    ])
    res, gold = merged(tmp_path)
    assert res["status"] == "blocked"
    assert gold["provenance"]["excluded"]["contested_edit"] == ["NFR-1"]


def test_skips_excluded_from_kappa(tmp_path):
    v1 = verdict("a1", [("FR-1", "a"), ("NFR-1", "a"), ("NFR-2", "r")])
    v2 = verdict("a2", [("FR-1", "a"), ("NFR-1", "s"), ("NFR-2", "r")])
    _write_tree(tmp_path, [v1, v2])
    res, gold = merged(tmp_path)
    assert res["status"] == "blocked"
    # kappa computed only over FR-1 (a/a) and NFR-2 (r/r) -> perfect
    assert gold["provenance"]["kappa_accept_reject"]["a1~a2"] == 1.0


def test_incomplete_scope_blocks(tmp_path):
    _write_tree(tmp_path, [verdict("a1", [("FR-1", "a"), ("NFR-1", "a"),
                                          ("NFR-2", "a")],
                                   confirmed=False, presented=40000)])
    res, gold = merged(tmp_path)
    assert res["status"] == "blocked"
    assert any("incomplete review scope" in b for b in res["blocked"])
    with pytest.raises(ValueError):
        load_gold(str(tmp_path / "data" / "frozen" / "gold" / "dev"
                  / "pure-0001.gold.json"))


def test_all_rejected_blocks(tmp_path):
    _write_tree(tmp_path, [verdict("a1", [("FR-1", "r"), ("NFR-1", "r"),
                                          ("NFR-2", "r")])])
    res, gold = merged(tmp_path)
    assert res["status"] == "blocked"
    assert any("no verified requirements" in b for b in res["blocked"])
    assert gold["provenance"]["excluded"]["rejected"] == ["FR-1", "NFR-1",
                                                         "NFR-2"]
    scorable, _ = gold_doc_scorable(gold)
    assert scorable  # rejected-only exclusions are resolved judgments


def test_implied_category_disagreement_blocks(tmp_path):
    _write_tree(tmp_path, [
        verdict("a1", [("FR-1", "a"), ("NFR-1", "a"), ("NFR-2", "a")],
                implied=("security",)),
        verdict("a2", [("FR-1", "a"), ("NFR-1", "a"), ("NFR-2", "a")],
                implied=("performance",)),
    ])
    res, gold = merged(tmp_path)
    assert res["status"] == "blocked"
    assert "contested_implied" in gold["provenance"]["excluded"]


def test_added_requirements_deduped(tmp_path):
    add = [{"kind": "FR", "text": "The system shall audit access."}]
    _write_tree(tmp_path, [
        verdict("a1", [("FR-1", "a"), ("NFR-1", "a"), ("NFR-2", "a")],
                added=add),
        verdict("a2", [("FR-1", "a"), ("NFR-1", "a"), ("NFR-2", "a")],
                added=add),
    ])
    res, gold = merged(tmp_path)
    assert res["status"] == "frozen"
    human = [r for r in gold["gold_requirements"] if r["id"].startswith(
        "human-")]
    assert len(human) == 1
