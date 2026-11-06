"""Rubric matcher: parsing discipline and fail-closed gates."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from eval import match_rubric as mr  # noqa: E402


def test_parse_pairs_keeps_only_valid_unique():
    pairs, dropped = mr.parse_pairs(
        '[["G1","FR-1"],["G2","NFR-1"],["G9","FR-2"],["G1","NFR-2"],'
        '"junk",[\"G3\"]]',
        {"G1", "G2", "G3"}, {"FR-1", "NFR-1", "FR-2", "NFR-2"})
    assert pairs == [["G1", "FR-1"], ["G2", "NFR-1"]]
    assert dropped == 4  # unknown gold id, reused gold id, string, 1-element list


def test_parse_pairs_unparseable_is_error_not_empty_match():
    pairs, dropped = mr.parse_pairs("I cannot tell", {"G1"}, {"FR-1"})
    assert pairs == [] and dropped == -1


def test_parse_pairs_code_fence_ok():
    pairs, dropped = mr.parse_pairs('```json\n[["G1","FR-1"]]\n```',
                                    {"G1"}, {"FR-1"})
    assert pairs == [["G1", "FR-1"]] and dropped == 0


class _FakeLLM:
    def __init__(self, reply):
        self.reply = reply

    def complete(self, system, user, temperature=0.2):
        return self.reply


def _write_run_log(tmp_path, reqs=True):
    log = tmp_path / "run.jsonl"
    recs = [{"type": "header", "run_id": "T1", "config": {}},
            {"type": "run", "run_id": "T1", "item_id": "doc1",
             "baseline": "B1",
             "requirements": ([{"id": "FR-1", "kind": "FR",
                                "text": "The system shall log in."}]
                              if reqs else None)}]
    log.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    return str(log)


def _write_gold(tmp_path, monkeypatch, excluded=None):
    gdir = tmp_path / "gold" / "dev"
    gdir.mkdir(parents=True)
    prov = {"status": "FROZEN - human-verified (anchored, 1 annotator)"}
    if excluded:
        prov["excluded"] = excluded
    (gdir / "doc1.gold.json").write_text(json.dumps({
        "item_id": "doc1", "prd_path": "x.pdf",
        "implied_categories": [],
        "gold_requirements": [{"id": "G1", "kind": "FR",
                               "text": "Users can log in."}],
        "provenance": prov}))
    monkeypatch.setattr(mr, "GOLD_DEV", gdir)
    monkeypatch.setattr(mr, "_ROOT", tmp_path)


def test_match_writes_verdict(tmp_path, monkeypatch):
    _write_gold(tmp_path, monkeypatch)
    log = _write_run_log(tmp_path)
    s = mr.match_run_log(log, _FakeLLM('[["G1","FR-1"]]'))
    assert s["verdicts"] == 1 and not s["errors"] and not s["refused"]
    v = json.loads((tmp_path / "eval" / "verdicts" / "T1"
                    / "doc1.B1.json").read_text())
    assert v["matches"] == [["G1", "FR-1"]]
    assert v["pred_ids"] == ["FR-1"] and v["audited"] is False


def test_unresolved_exclusions_refused(tmp_path, monkeypatch):
    _write_gold(tmp_path, monkeypatch, excluded={"skipped": ["S-1"]})
    log = _write_run_log(tmp_path)
    s = mr.match_run_log(log, _FakeLLM('[["G1","FR-1"]]'))
    assert s["verdicts"] == 0 and len(s["refused"]) == 1
    assert "skipped" in s["refused"][0]


def test_missing_persisted_requirements_is_error(tmp_path, monkeypatch):
    _write_gold(tmp_path, monkeypatch)
    log = _write_run_log(tmp_path, reqs=False)
    s = mr.match_run_log(log, _FakeLLM('[]'))
    assert s["verdicts"] == 0 and "no persisted requirements" in \
        s["errors"][0]


def test_cross_kind_proposals_dropped():
    pairs, dropped = mr.parse_pairs(
        '[["G1","NFR-1"],["G2","NFR-2"]]',
        {"G1", "G2"}, {"NFR-1", "NFR-2"},
        gold_kinds={"G1": "FR", "G2": "security"},
        pred_kinds={"NFR-1": "security", "NFR-2": "security"})
    assert pairs == [["G2", "NFR-2"]]  # G1/NFR-1 is cross-kind: dropped
    assert dropped == 1


def test_verdict_carries_matcher_metadata(tmp_path, monkeypatch):
    _write_gold(tmp_path, monkeypatch)
    log = _write_run_log(tmp_path)

    class _NamedLLM(_FakeLLM):
        model = "test-model-1"

    s = mr.match_run_log(log, _NamedLLM('[["G1","FR-1"],"junk"]'))
    assert s["verdicts"] == 1 and s["dropped_pairs"] == 1
    v = json.loads((tmp_path / "eval" / "verdicts" / "T1"
                    / "doc1.B1.json").read_text())
    assert v["dropped_pairs"] == 1
    assert v["matcher"]["model"] == "test-model-1"
    assert v["matcher"]["type"] == "llm-rubric"
    assert v["audited"] is False
