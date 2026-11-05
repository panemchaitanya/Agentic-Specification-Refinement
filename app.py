"""ReqHQ - Agentic Specification Refinement (Streamlit UI).

Three screens: Input -> Questions -> Result.
The UI is a thin shell; all pipeline logic lives in src/specpipe and is
reached only through pipeline.analyze() / pipeline.finalize() and the
dataclasses in schemas.py. UI lane scope: this file and .streamlit/ only.
"""
import html
import json
import re
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).parent / "src"))

from specpipe.llm import GeminiLLM, MockLLM, LLMError  # noqa: E402
from specpipe.pipeline import analyze, finalize  # noqa: E402
from specpipe.schemas import (  # noqa: E402
    NFR_CATEGORIES, TRACE_CRITIC, TRACE_HUMAN, TRACE_PRD,
)

MAX_LLM_RUNS_PER_SESSION = 10  # abuse guard for the shared free-tier key on a public URL

st.set_page_config(page_title="ReqHQ - Spec Refinement", page_icon="📋",
                   layout="wide")

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

KIND_LABEL = {"missing_context": "Missing context",
              "vague": "Vague wording",
              "conflict": "Conflict"}
KIND_CLASS = {"missing_context": "b-kind-mc",
              "vague": "b-kind-vague",
              "conflict": "b-kind-conflict"}
TRACE_LABEL = {TRACE_PRD: "PRD", TRACE_CRITIC: "Critic",
               TRACE_HUMAN: "Your answer"}
TRACE_CLASS = {TRACE_PRD: "t-prd", TRACE_CRITIC: "t-critic",
               TRACE_HUMAN: "t-human"}
CRITIC_ICON = {"security": "🛡", "architecture": "🏗", "performance": "⚡"}

CSS = """
<style>
/* Keep wide layout readable */
.block-container {max-width: 1080px; padding-top: 2.5rem;}

/* Wizard step indicator */
.steps {display: flex; align-items: center; gap: 10px; margin: 2px 0 20px 0;}
.step {display: flex; align-items: center; gap: 7px; font-size: 14px; color: #6b7280;}
.step .num {width: 24px; height: 24px; border-radius: 50%; background: #e5e7eb;
  color: #374151; display: inline-flex; align-items: center; justify-content: center;
  font-size: 13px; font-weight: 600;}
.step.active {color: #111827; font-weight: 600;}
.step.active .num {background: #4F6BED; color: #ffffff;}
.step.done .num {background: #d1fae5; color: #065f46;}
.step-sep {width: 42px; height: 1px; background: #e5e7eb;}

/* Badges and trace chips */
.badge {display: inline-block; padding: 1px 9px; border-radius: 999px;
  font-size: 12px; font-weight: 600; margin: 0 4px 2px 0; white-space: nowrap;}
.b-fr {background: #eff6ff; color: #1d4ed8;}
.b-nfr {background: #f5f3ff; color: #6d28d9;}
.b-kind-mc {background: #fee2e2; color: #b91c1c;}
.b-kind-vague {background: #fef3c7; color: #92400e;}
.b-kind-conflict {background: #ffedd5; color: #c2410c;}
.b-ask {background: #fee2e2; color: #b91c1c;}
.b-auto {background: #d1fae5; color: #065f46;}
.b-open {background: #fee2e2; color: #b91c1c;}
.t-prd {background: #dbeafe; color: #1e40af;}
.t-critic {background: #ffedd5; color: #9a3412;}
.t-human {background: #d1fae5; color: #065f46;}

/* Cards and quotes */
.req-card {border: 1px solid #e5e7eb; border-radius: 10px;
  padding: 10px 14px; margin: 0 0 8px 0;}
.req-card .trace {font-size: 12.5px; color: #6b7280; margin-top: 4px;}
.quote {border-left: 3px solid #d1d5db; padding: 2px 10px; color: #4b5563;
  font-style: italic; margin: 4px 0 6px 0; font-size: 13.5px;}
.small-note {font-size: 12.5px; color: #6b7280;}
</style>
"""

STEPS = [("input", "Describe"), ("questions", "Review & answer"),
         ("result", "Specification")]


def esc(text):
    """HTML-escape any string that came from the user or the model."""
    return html.escape(str(text), quote=True)


def inject_css():
    st.markdown(CSS, unsafe_allow_html=True)


def steps(current):
    """Render the three-step wizard indicator."""
    order = [k for k, _ in STEPS]
    idx = order.index(current)
    parts = ['<div class="steps">']
    for i, (key, label) in enumerate(STEPS):
        cls = "step active" if i == idx else ("step done" if i < idx else "step")
        parts.append(f'<div class="{cls}"><span class="num">{i + 1}</span>'
                     f'{esc(label)}</div>')
        if i < len(STEPS) - 1:
            parts.append('<div class="step-sep"></div>')
    parts.append("</div>")
    st.markdown("".join(parts), unsafe_allow_html=True)


def get_llm():
    """Pick the real model when a key is configured, else the demo mock."""
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


def sidebar(mode):
    with st.sidebar:
        st.header("ReqHQ")
        st.caption("Agentic Specification Refinement - turn a rough product "
                   "description into a complete, trace-linked specification.")
        st.markdown(f"**Model mode:** {esc(mode)}")
        if mode != "Gemini":
            st.caption("Demo mode shows the full flow with a deterministic "
                       "mock; it is not model-quality output.")
        st.divider()
        st.caption("Team ReqHQ - Autonomous Software Engineering (CS6.403), "
                   "IIIT-H, Monsoon 2026.")
        st.caption("How it works: an orchestrator drafts functional "
                   "requirements; Security, Architecture and Performance "
                   "critics flag missing or vague NFRs; only unresolved "
                   "gaps and conflicts are escalated to you. Every final "
                   "line is traced to a PRD sentence, a critic, or your "
                   "answer.")


def badge(text, cls):
    return f'<span class="badge {cls}">{esc(text)}</span>'


def kind_badge(kind):
    if kind == "FR":
        return badge("FR", "b-fr")
    return badge(f"NFR - {str(kind).title()}", "b-nfr")


def trace_badge(trace_kind):
    return badge(TRACE_LABEL.get(trace_kind, trace_kind),
                 TRACE_CLASS.get(trace_kind, ""))


def trace_detail(r):
    """Human-readable trace evidence for one spec line."""
    if r.trace_kind == TRACE_PRD:
        return f'From PRD: "{r.trace_ref}"'
    if r.trace_kind == TRACE_CRITIC:
        return f"From the {r.trace_ref} critic"
    if r.trace_kind == TRACE_HUMAN:
        return f"From your answer to {r.trace_ref}"
    return f"Trace: {r.trace_ref}"


def requirement_card(r):
    open_item = r.text.startswith("OPEN:")
    extra = badge("Open item - unanswered", "b-open") if open_item else ""
    # user_confirmed is optional (None on legacy pipelines): when the core
    # tracks approval of critic-suggested lines, show it.
    confirmed = getattr(r, "user_confirmed", None)
    if confirmed is True:
        extra += badge("You confirmed", "b-auto")
    elif confirmed is False:
        extra += badge("Awaiting your confirmation", "b-ask")
    st.markdown(
        f'<div class="req-card">{kind_badge(r.kind)} {trace_badge(r.trace_kind)} '
        f'{extra}<strong>{esc(r.id)}</strong> &nbsp;{esc(r.text)}'
        f'<div class="trace">{esc(trace_detail(r))}</div></div>',
        unsafe_allow_html=True)


def spec_markdown(spec):
    """Plain-Markdown export of the spec (UI-side formatting of spec data)."""
    lines = [f"# {spec.title} - Specification", "",
             "Every line is traced to a PRD sentence, a critic, "
             "or a human answer.", ""]
    frs = [r for r in spec.requirements if r.kind == "FR"]
    nfrs = [r for r in spec.requirements if r.kind != "FR"]
    lines += ["## Functional requirements", ""]
    for r in frs:
        lines.append(f"- **{r.id}**: {r.text}")
        lines.append(f"  - Trace: {TRACE_LABEL.get(r.trace_kind, r.trace_kind)}"
                     f" ({r.trace_ref})")
    lines += ["", "## Non-functional requirements", ""]
    for r in nfrs:
        lines.append(f"- **{r.id}** ({r.kind}): {r.text}")
        lines.append(f"  - Trace: {TRACE_LABEL.get(r.trace_kind, r.trace_kind)}"
                     f" ({r.trace_ref})")
    return "\n".join(lines)


def load_sample():
    """Copy the chosen sample into the PRD box without clobbering edits."""
    choice = st.session_state.get("sample_choice")
    if choice:
        st.session_state.prd_text = SAMPLES[choice]


def screen_input(mode):
    steps("input")
    st.title("ReqHQ: Agentic Specification Refinement")
    st.caption("Turn a rough product description into trace-linked "
               "requirements. Team ReqHQ - Autonomous Software Engineering "
               "course project.")
    st.info(f"Model mode: {mode}")  # demo-mode banner: keep visible

    st.session_state.setdefault("prd_text", "")
    st.selectbox("Load a sample PRD (optional)", [""] + list(SAMPLES.keys()),
                 key="sample_choice", on_change=load_sample,
                 help="Loads the sample into the box below; you can edit it "
                      "before analysing.")
    st.text_area("Product description (PRD)", key="prd_text", height=180,
                 placeholder="Paste or write the product description...")
    prd = st.session_state.prd_text
    words = len(prd.split())
    st.caption(f"{words} word{'s' if words != 1 else ''}")
    st.text_input("Product name", value="Untitled product", key="title_input")

    if mode == "Gemini":
        used = st.session_state.setdefault("llm_runs", 0)
        st.caption(f"Real-model runs this session: {used}/"
                   f"{MAX_LLM_RUNS_PER_SESSION} (cap protects the free API "
                   "quota on a public link)")

    if st.button("Analyze", type="primary", disabled=not prd.strip()):
        if mode == "Gemini" and st.session_state.llm_runs >= MAX_LLM_RUNS_PER_SESSION:
            st.error("Session run limit reached. This caps use of the shared "
                     "free API key on the public demo. Run locally with your "
                     "own key for more.")
            st.stop()
        with st.spinner("Drafting requirements and running the three "
                        "critics..."):
            result = analyze(prd, get_llm()[0])
        if mode == "Gemini":
            st.session_state.llm_runs += 1
        st.session_state.update(title=st.session_state.title_input,
                                prd=prd, **result)
        go("questions")


def finding_card(f, escalated):
    icon = CRITIC_ICON.get(f.critic, "🔍")
    st.markdown(
        f"{icon} {badge(f.critic.title(), 'b-nfr')} "
        f"{badge(KIND_LABEL.get(f.kind, f.kind), KIND_CLASS.get(f.kind, ''))} "
        + (badge("You are asked about this", "b-ask") if escalated
           else badge("Auto-resolved into the spec", "b-auto")),
        unsafe_allow_html=True)
    st.markdown(f"**{esc(f.issue)}**")
    st.markdown(f"*Suggested:* {esc(f.suggestion)}")
    st.markdown(f'<div class="quote">Caused by PRD: "{esc(f.prd_sentence)}"'
                f"</div>", unsafe_allow_html=True)
    st.markdown(f'<div class="small-note">Why it matters: {esc(f.reason)}'
                f"</div>", unsafe_allow_html=True)
    if f.conflicts_with:
        st.markdown(f'<div class="small-note">Conflicts with the '
                    f'{esc(f.conflicts_with)} critic.</div>',
                    unsafe_allow_html=True)
    st.markdown("")


def demo_caption(mode):
    """Demo-mode reminder on every screen; mock output is not model quality."""
    if mode != "Gemini":
        st.caption("Demo mode: output below comes from a deterministic mock "
                   "and shows the flow, not refinement quality.")


def screen_questions(mode):
    demo_caption(mode)
    findings = st.session_state.findings
    questions = st.session_state.questions
    draft = st.session_state.draft
    title = st.session_state.title

    steps("questions")
    st.title("Review and answer")
    st.caption(f"Product: **{title}** - the pipeline drafted requirements "
               "and the critics reviewed them. Only the gaps the critics "
               "could not settle are escalated to you.")

    c1, c2, c3 = st.columns(3)
    c1.metric("Draft requirements", len(draft))
    c2.metric("Critic findings", len(findings))
    c3.metric("Questions for you", len(questions))

    col_draft, col_find = st.columns(2, gap="large")
    with col_draft:
        st.subheader(f"Draft requirements ({len(draft)})")
        st.caption("Each draft line traces to a verbatim PRD sentence.")
        for r in draft:
            requirement_card(r)
    with col_find:
        st.subheader(f"Critic findings ({len(findings)})")
        escalated = {ref for q in questions for ref in q.finding_refs}
        for f in findings:
            finding_card(f, f.issue in escalated)

    st.divider()
    st.subheader("Questions for you")
    escalated_issues = {ref for q in questions for ref in q.finding_refs}
    auto_count = sum(1 for f in findings if f.issue not in escalated_issues)
    if not questions:
        note = "No questions need your input."
        if auto_count:
            note += (f" {auto_count} critic-suggested NFR"
                     f"{'s' if auto_count != 1 else ''} will be added "
                     "automatically - review them in the spec before "
                     "relying on it.")
        st.success(note)
    else:
        st.caption("Answering is optional. An unanswered question becomes "
                   "an open item in the spec, traced to the critic that "
                   "raised it.")
        if auto_count:
            st.caption(f"Separately, {auto_count} suggested NFR"
                       f"{'s are' if auto_count != 1 else ' is'} added "
                       "automatically without your approval (the "
                       "escalation rule) - review those lines in the "
                       "spec.")
        for i, q in enumerate(questions, 1):
            st.markdown(f"**Q{i}. {esc(q.text)}**")
            st.caption(q.context)
            st.text_input(f"Your answer to Q{i}", key=q.id,
                          label_visibility="collapsed",
                          placeholder="Your answer (optional)")

    gen = st.button("Generate specification", type="primary")
    if st.button("Back to description"):
        go("input")
    if gen:
        answers = {q.id: st.session_state.get(q.id, "") for q in questions}
        spec = finalize(title, draft, findings, questions, answers)
        st.session_state.spec = spec
        go("result")


def screen_result(mode):
    demo_caption(mode)
    spec = st.session_state.spec
    steps("result")
    st.title("Final specification")
    st.caption(f"**{spec.title}** - every line below names its source.")

    report = spec.trace_report()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("From PRD", report["by_source"][TRACE_PRD])
    c2.metric("From critics", report["by_source"][TRACE_CRITIC])
    c3.metric("From your answers", report["by_source"][TRACE_HUMAN])
    c4.metric("Untraced", report["untraced"])
    if report.get("user_unconfirmed") is not None:
        st.caption(f"Critic-suggested lines awaiting your confirmation: "
                   f"{report['user_unconfirmed']}")
    st.caption("These counts show where each line came from, not whether "
               "the requirements are correct - correctness is measured in "
               "the frozen-split evaluation. Open items are escalations "
               "left unanswered.")

    frs = [r for r in spec.requirements if r.kind == "FR"]
    nfrs = [r for r in spec.requirements if r.kind != "FR"]

    st.subheader(f"Functional requirements ({len(frs)})")
    for r in frs:
        requirement_card(r)

    st.subheader(f"Non-functional requirements ({len(nfrs)})")

    def id_num(r):
        m = re.search(r"(\d+)$", r.id)
        return int(m.group(1)) if m else 0

    # Group by category for the completeness view, but order groups and
    # lines by id so the numbering still reads in sequence.
    groups = []
    for cat in NFR_CATEGORIES + sorted({r.kind for r in nfrs}
                                       - set(NFR_CATEGORIES)):
        group = sorted([r for r in nfrs if r.kind == cat], key=id_num)
        if group:
            groups.append((cat, group))
    groups.sort(key=lambda cg: id_num(cg[1][0]))
    for cat, group in groups:
        st.markdown(f"**{cat.title()}**")
        for r in group:
            requirement_card(r)

    st.divider()
    d1, d2 = st.columns(2)
    d1.download_button("Download spec (JSON)",
                       json.dumps(spec.to_dict(), indent=2),
                       file_name="specification.json",
                       mime="application/json")
    d2.download_button("Download spec (Markdown)",
                       spec_markdown(spec),
                       file_name="specification.md",
                       mime="text/markdown")

    if st.button("Edit answers"):
        go("questions")
    if st.button("Start over"):
        answer_keys = [q.id for q in st.session_state.get("questions", [])]
        for k in ("prd", "draft", "findings", "questions", "spec",
                  "prd_text", "sample_choice", *answer_keys):
            st.session_state.pop(k, None)
        go("input")


mode = get_llm()[1]
inject_css()
sidebar(mode)

stage = st.session_state.setdefault("stage", "input")
if stage == "questions" and "findings" not in st.session_state:
    stage = "input"
if stage == "result" and "spec" not in st.session_state:
    stage = "input"
{"input": lambda: screen_input(mode),
 "questions": lambda: screen_questions(mode),
 "result": lambda: screen_result(mode)}[stage]()
