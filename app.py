"""ReqHQ - Agentic Specification Refinement (Streamlit UI).

Three screens: Input -> Questions -> Result.
The UI is a thin shell; all logic lives in src/specpipe.
"""
import json
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).parent / "src"))

from specpipe.llm import GeminiLLM, MockLLM, LLMError  # noqa: E402
from specpipe.pipeline import analyze, finalize  # noqa: E402

MAX_LLM_RUNS_PER_SESSION = 10  # abuse guard for the shared free-tier key on a public URL

st.set_page_config(page_title="ReqHQ - Spec Refinement", page_icon="📋",
                   layout="centered")

SAMPLES = {
    "Shopping app (demo)": (
        "Build a secure shopping app. Users can sign up with an email and "
        "password, browse products, add items to a cart and check out. "
        "The checkout must be fast. We store user profile data and order "
        "history. Admins can refund orders."),
    "Classroom poll (demo)": (
        "Teachers can create a poll with up to 4 options and share a join "
        "code. Students join with the code and vote once. Teachers see "
        "results live. The app should handle a full classroom."),
}

TRACE_LABEL = {"prd_sentence": "PRD", "critic": "Critic",
               "human_answer": "Your answer"}
TRACE_COLOR = {"prd_sentence": "blue", "critic": "orange",
               "human_answer": "green"}


def get_llm():
    key = None
    try:
        key = st.secrets.get("GEMINI_API_KEY", None)
    except Exception:
        key = None
    import os
    if key or os.environ.get("GEMINI_API_KEY"):
        try:
            return GeminiLLM(api_key=key), "Gemini"
        except LLMError:
            pass
    return MockLLM(), "Demo mode (no API key - deterministic mock)"


def go(stage):
    st.session_state.stage = stage
    st.rerun()


def screen_input():
    st.title("ReqHQ: Agentic Specification Refinement")
    st.caption("Turn a rough product description into trace-linked requirements. "
               "Team ReqHQ - Autonomous Software Engineering course project.")
    llm, mode = get_llm()
    st.info(f"Model mode: {mode}")
    choice = st.selectbox("Load a sample PRD", [""] + list(SAMPLES.keys()))
    default = SAMPLES.get(choice, "")
    prd = st.text_area("Product description (PRD)", value=default, height=180,
                       placeholder="Paste or write the product description...")
    title = st.text_input("Product name", value="Untitled product")
    if mode == "Gemini":
        used = st.session_state.setdefault("llm_runs", 0)
        st.caption(f"Real-model runs this session: {used}/{MAX_LLM_RUNS_PER_SESSION} "
                   "(cap protects the free API quota on a public link)")
    if st.button("Analyze", type="primary", disabled=not prd.strip()):
        if mode == "Gemini" and st.session_state.llm_runs >= MAX_LLM_RUNS_PER_SESSION:
            st.error("Session run limit reached. This caps use of the shared free API "
                     "key on the public demo. Run locally with your own key for more.")
            st.stop()
        with st.spinner("Drafting requirements and running the three critics..."):
            result = analyze(prd, llm)
        if mode == "Gemini":
            st.session_state.llm_runs += 1
        st.session_state.update(title=title, prd=prd, **result)
        go("questions")


def screen_questions():
    st.title("What needs your input")
    findings = st.session_state.findings
    questions = st.session_state.questions
    draft = st.session_state.draft

    st.subheader(f"Draft requirements ({len(draft)})")
    for r in draft:
        st.markdown(f"- {r.text}")

    st.subheader(f"Critic findings ({len(findings)})")
    for f in findings:
        st.markdown(f"- **{f.critic}** ({f.kind}): {f.issue}  \n"
                    f"  Suggested: *{f.suggestion}*")
    if not questions:
        st.success("No unresolved gaps - the critics could settle everything.")
    answers = {}
    for q in questions:
        answers[q.id] = st.text_input(q.text, help=q.context, key=q.id)
    if st.button("Generate specification", type="primary"):
        spec = finalize(st.session_state.title, draft, findings,
                        questions, answers)
        st.session_state.spec = spec
        go("result")


def screen_result():
    st.title("Final specification")
    spec = st.session_state.spec
    st.subheader(spec.title)
    report = spec.trace_report()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("From PRD", report["by_source"]["prd_sentence"])
    c2.metric("From critics", report["by_source"]["critic"])
    c3.metric("From your answers", report["by_source"]["human_answer"])
    c4.metric("Untraced", report["untraced"])
    for r in spec.requirements:
        label = TRACE_LABEL.get(r.trace_kind, r.trace_kind)
        color = TRACE_COLOR.get(r.trace_kind, "gray")
        st.markdown(f"**{r.id}** ({r.kind}) :{color}[{label}]  \n{r.text}")
    st.download_button("Download spec (JSON)",
                       json.dumps(spec.to_dict(), indent=2),
                       file_name="specification.json",
                       mime="application/json")
    if st.button("Start over"):
        for k in ("prd", "draft", "findings", "questions", "spec"):
            st.session_state.pop(k, None)
        go("input")


stage = st.session_state.setdefault("stage", "input")
{"input": screen_input,
 "questions": screen_questions,
 "result": screen_result}[stage]()
