import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from specpipe.llm import MockLLM
from specpipe.pipeline import analyze, finalize
from specpipe.gapcheck import dedupe, select_questions, MAX_QUESTIONS
from specpipe.schemas import Finding

PRD = ("Users can sign up with an email and password. "
       "The app must be fast. We store user profile data.")


def test_analyze_produces_traced_draft():
    r = analyze(PRD, MockLLM())
    assert len(r["draft"]) == 3
    assert all(x.trace_kind == "prd_sentence" for x in r["draft"])
    assert all(x.trace_ref for x in r["draft"])


def test_critics_flag_gaps():
    r = analyze(PRD, MockLLM())
    kinds = {f.kind for f in r["findings"]}
    assert "missing_context" in kinds and "vague" in kinds
    critics = {f.critic for f in r["findings"]}
    assert critics == {"security", "architecture", "performance"}


def test_only_escalated_findings_become_questions():
    r = analyze(PRD, MockLLM())
    escalated = [f for f in r["findings"]
                 if f.kind in ("missing_context", "conflict")]
    assert len(r["questions"]) == min(len(escalated), MAX_QUESTIONS)


def test_every_spec_line_is_traced():
    r = analyze(PRD, MockLLM())
    spec = finalize("T", r["draft"], r["findings"], r["questions"],
                    {q.id: "answer" for q in r["questions"]})
    rep = spec.trace_report()
    assert rep["untraced"] == 0
    assert rep["by_source"]["prd_sentence"] > 0
    assert rep["by_source"]["critic"] > 0
    assert rep["by_source"]["human_answer"] > 0


def test_unanswered_questions_become_open_items():
    r = analyze(PRD, MockLLM())
    spec = finalize("T", r["draft"], r["findings"], r["questions"], {})
    open_items = [x for x in spec.requirements if x.text.startswith("OPEN:")]
    assert len(open_items) == len(r["questions"])


def test_dedupe_merges_identical_flags():
    f = dict(critic="security", category="security", kind="vague",
             issue="same issue", suggestion="s", prd_sentence="p", reason="r")
    merged = dedupe([Finding(**f),
                     Finding(**{**f, "critic": "performance"})])
    assert len(merged) == 1


def test_question_cap():
    findings = [Finding(critic="security", category="security",
                        kind="missing_context", issue=f"gap {i}",
                        suggestion="s", prd_sentence="p", reason="r")
                for i in range(MAX_QUESTIONS + 3)]
    assert len(select_questions(findings)) == MAX_QUESTIONS


def test_quality_statements_typed_as_nfr_not_fr():
    from specpipe.orchestrator import classify_statement, draft_frs
    assert classify_statement("The checkout must be fast.") == "performance"
    assert classify_statement("The system should be secure.") == "security"
    assert classify_statement("Users can reset their password.") == "FR"
    draft = draft_frs(PRD, MockLLM())
    kinds = {r.text: r.kind for r in draft}
    assert kinds["The app must be fast."] == "performance"
    assert any(k == "FR" for k in kinds.values())


def test_critic_auto_lines_marked_unconfirmed():
    r = analyze(PRD, MockLLM())
    spec = finalize("T", r["draft"], r["findings"], r["questions"],
                    {q.id: "answer" for q in r["questions"]})
    auto = [x for x in spec.requirements
            if x.trace_kind == "critic" and not x.text.startswith("OPEN:")]
    assert auto and all(x.user_confirmed is False for x in auto)
    human = [x for x in spec.requirements if x.trace_kind == "human_answer"]
    assert human and all(x.user_confirmed is True for x in human)
    prd = [x for x in spec.requirements if x.trace_kind == "prd_sentence"]
    assert prd and all(x.user_confirmed is True for x in prd)
    assert spec.trace_report()["user_unconfirmed"] == len(auto)


def test_final_ids_are_one_consistent_sequence():
    r = analyze(PRD, MockLLM())
    spec = finalize("T", r["draft"], r["findings"], r["questions"], {})
    fr_ids = [x.id for x in spec.requirements if x.kind == "FR"]
    nfr_ids = [x.id for x in spec.requirements if x.kind != "FR"]
    assert fr_ids == [f"FR-{i}" for i in range(1, len(fr_ids) + 1)]
    assert nfr_ids == [f"NFR-{i}" for i in range(1, len(nfr_ids) + 1)]
