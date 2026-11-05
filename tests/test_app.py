"""UI tests for the ReqHQ Streamlit app (demo mode, no API key needed).

These exercise the three-screen flow end to end with Streamlit's AppTest
runner. The deterministic MockLLM keeps them offline and repeatable.
"""
import json
import os

import pytest
from streamlit.testing.v1 import AppTest

PRD = ("Build a secure shopping app. Users can sign up with an email and "
       "password, browse products, add items to a cart and check out. "
       "The checkout must be fast. We store user profile data and order "
       "history. Admins can refund orders.")

APP = os.path.join(os.path.dirname(__file__), "..", "app.py")


@pytest.fixture(autouse=True)
def no_api_key(monkeypatch):
    # Force demo mode so tests never touch the real model.
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


def run_app():
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    assert not at.exception
    return at


def button(at, label):
    for b in at.button:
        if b.label == label:
            return b
    raise AssertionError(f"button {label!r} not found; "
                         f"have {[b.label for b in at.button]}")


def test_input_screen_shows_demo_banner():
    at = run_app()
    assert at.session_state.stage == "input"
    assert any("Demo mode" in i.value for i in at.info)
    assert any("ReqHQ" in h.value for h in at.title)


def test_analyze_disabled_without_prd():
    at = run_app()
    assert button(at, "Analyze").disabled


def test_sample_loads_without_clobbering_edits():
    at = run_app()
    at.text_area(key="prd_text").set_value("My own product idea.")
    at.run()
    assert at.session_state.prd_text == "My own product idea."
    # Picking a sample loads it...
    at.selectbox(key="sample_choice").set_value("Shopping app (demo)")
    at.run()
    assert at.session_state.prd_text.startswith("Build a secure shopping app")
    # ...and a later edit is kept, not overwritten by a rerun.
    at.text_area(key="prd_text").set_value("Edited sample text.")
    at.run()
    assert at.session_state.prd_text == "Edited sample text."


def analyze_to_questions(at):
    at.text_area(key="prd_text").set_value(PRD)
    at.run()
    button(at, "Analyze").click().run()
    assert not at.exception
    assert at.session_state.stage == "questions"
    return at


def test_questions_screen_shows_trace_evidence():
    at = analyze_to_questions(run_app())
    # Draft FRs show their traced PRD sentence; findings show the causing
    # PRD sentence, reason, and whether they escalate to the user.
    all_md = "\n".join(m.value for m in at.markdown)
    assert 'Caused by PRD: "' in all_md
    assert "Why it matters:" in all_md
    assert "You are asked about this" in all_md
    assert "Auto-resolved into the spec" in all_md
    assert "From PRD:" in all_md


def test_full_flow_to_result_and_back():
    at = analyze_to_questions(run_app())
    # Answer the first question only; the rest become open items.
    questions = at.session_state.questions
    at.text_input(key=questions[0].id).set_value("AES-256 at rest")
    at.run()
    button(at, "Generate specification").click().run()
    assert not at.exception
    assert at.session_state.stage == "result"
    spec = at.session_state.spec
    report = spec.trace_report()
    assert report["untraced"] == 0
    assert report["by_source"]["human_answer"] == 1
    assert report["by_source"]["prd_sentence"] > 0
    assert report["by_source"]["critic"] > 0
    # Both download buttons exist.
    labels = [d.label for d in at.download_button]
    assert "Download spec (JSON)" in labels
    assert "Download spec (Markdown)" in labels
    # Edit answers goes back, state kept.
    button(at, "Edit answers").click().run()
    assert at.session_state.stage == "questions"
    button(at, "Generate specification").click().run()
    assert at.session_state.stage == "result"
    # Start over resets to a clean input screen.
    button(at, "Start over").click().run()
    assert at.session_state.stage == "input"
    assert "spec" not in at.session_state
    assert "draft" not in at.session_state


def test_open_items_marked_when_unanswered():
    at = analyze_to_questions(run_app())
    button(at, "Generate specification").click().run()
    assert not at.exception
    spec = at.session_state.spec
    opens = [r for r in spec.requirements if r.text.startswith("OPEN:")]
    assert len(opens) == len(at.session_state.get("questions", []))
    all_md = "\n".join(m.value for m in at.markdown)
    assert "Open item - unanswered" in all_md



def test_demo_caption_on_all_screens():
    at = analyze_to_questions(run_app())
    assert any("Demo mode" in c.value and "not refinement quality" in c.value
               for c in at.caption)
    button(at, "Generate specification").click().run()
    assert not at.exception
    assert any("Demo mode" in c.value and "not refinement quality" in c.value
               for c in at.caption)
    # Trace-coverage honesty caption on the result screen.
    assert any("not whether" in c.value and "correct" in c.value
               for c in at.caption)


def test_auto_apply_note_when_questions_exist():
    at = analyze_to_questions(run_app())
    assert any("added automatically without your approval" in c.value
               for c in at.caption)
