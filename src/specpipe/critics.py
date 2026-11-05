"""The three specialist critics: Security, Architecture, Performance.

Each is the same LLM with a narrow role prompt in this first version;
fine-tuned SLM critics (proposal H2) replace these later behind the
identical Finding schema.
"""
from __future__ import annotations

from .llm import _extract_json
from .schemas import Finding

_ROLE = {
    "security": "security (confidentiality, integrity, access control, data protection)",
    "architecture": "architecture and reliability (structure, durability, failure handling, maintainability)",
    "performance": "performance and scalability (latency, throughput, capacity, resource use)",
}

_SYSTEM_TMPL = """ROLE: {name}_critic
You are a narrow {focus} critic reviewing a draft requirements set against
its PRD. Report ONLY {focus} issues. Return JSON only:
{{"findings": [{{
  "category": "security|performance|scalability|reliability|usability|other",
  "kind": "missing_context|vague|conflict",
  "issue": "<what is missing or vague>",
  "suggestion": "<the concrete NFR sentence you propose>",
  "prd_sentence": "<exact PRD sentence that caused this flag>",
  "reason": "<why this matters>",
  "conflicts_with": "<other critic name, only for kind=conflict, else null>"
}}]}}
Use kind=missing_context when the PRD omits business context only its author
knows (e.g. expected load). Use kind=vague when the PRD says something like
'fast' or 'secure' with no numbers. Use kind=conflict when meeting your
requirement could break another critic's requirement."""


class Critic:
    def __init__(self, name: str, llm):
        if name not in _ROLE:
            raise ValueError(f"unknown critic {name}")
        self.name = name
        self.llm = llm

    def review(self, prd_text: str, draft_frs: list) -> list:
        system = _SYSTEM_TMPL.format(name=self.name, focus=_ROLE[self.name])
        draft_text = "\n".join(f"- {r.text}" for r in draft_frs)
        user = f"PRD:\n{prd_text}\n\nDraft FRs:\n{draft_text}"
        raw = self.llm.complete(system, user)
        data = _extract_json(raw)
        out = []
        for f in data.get("findings", []):
            out.append(Finding(
                critic=self.name,
                category=str(f.get("category", "other")),
                kind=str(f.get("kind", "vague")),
                issue=str(f.get("issue", "")).strip(),
                suggestion=str(f.get("suggestion", "")).strip(),
                prd_sentence=str(f.get("prd_sentence", "")).strip(),
                reason=str(f.get("reason", "")).strip(),
                conflicts_with=f.get("conflicts_with") or None))
        return [f for f in out if f.issue and f.suggestion]


def default_critics(llm) -> list:
    return [Critic(n, llm) for n in ("security", "architecture", "performance")]
