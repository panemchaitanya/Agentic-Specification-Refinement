"""Closed-loop neuro-symbolic Alloy verification & bounded SLM->cloud repair controller."""

from __future__ import annotations

import json
from pathlib import Path
import re
import tempfile

from reqhq.alloy.counterexample import translate_counterexample
from reqhq.alloy.emitter import lint_core_domain_version_agnostic
from reqhq.alloy.extractor import (
    build_req_to_alloy_refs,
    extract_alloy_blocks,
    replace_alloy_block,
)
from reqhq.alloy.models import (
    AlloyCompatMode,
    AlloyRunResult,
    AlloyRunStatus,
    AlloyVerificationOutcome,
    ConflictQuestion,
    CounterexamplePrompt,
    ExtractedAlloyBlock,
)
from reqhq.alloy.runner import AlloyAnalyzer
from reqhq.alloy.unsat_mapper import map_unsat_to_conflicts
from reqhq.alloy.vacuity import inject_vacuity_witnesses
from reqhq.llm.providers.base import LLMRequest
from reqhq.llm.router import EscalationReason, ProviderRouter
from reqhq.schemas.requirements import RequirementGraph
from reqhq.store.event_log import EventKind, SessionEventLog

_ANY_ALLOY_FENCE_RE = re.compile(
    r"```alloy(?:\s+module\s*=\s*([A-Za-z_][A-Za-z0-9_]*))?\s*\r?\n(.*?)\r?\n```",
    re.DOTALL | re.IGNORECASE,
)
_ASSERT_IN_OUTPUT_RE = re.compile(
    r"(?:assertion|assert|check)\s+`?([A-Za-z_][A-Za-z0-9_']*)`?",
    re.IGNORECASE,
)
_FIRST_ASSERT_RE = re.compile(r"^\s*assert\s+([A-Za-z_][A-Za-z0-9_']*)\b", re.MULTILINE)


def _extract_repaired_modules(
    response_text: str,
    *,
    default_target: str,
) -> dict[str, str]:
    """Extract repaired `core_domain` and/or `behavior` Alloy code from an LLM response."""
    updates: dict[str, str] = {}
    text = response_text.strip()
    if not text:
        return updates

    # 1. Check fenced ```alloy [module=...] blocks.
    for match in _ANY_ALLOY_FENCE_RE.finditer(text):
        explicit_mod = match.group(1)
        code = match.group(2).strip()
        if not code:
            continue
        if explicit_mod:
            updates[explicit_mod] = code
        elif code.startswith("module core_domain"):
            updates["core_domain"] = code
        elif code.startswith("module behavior"):
            updates["behavior"] = code
        else:
            updates[default_target] = code

    if updates:
        return updates

    # 2. Check JSON payload {"core_domain": "...", "behavior": "..."}.
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            for mod_name in ("core_domain", "behavior"):
                val = parsed.get(mod_name)
                if isinstance(val, str) and val.strip():
                    updates[mod_name] = val.strip()
            if updates:
                return updates

    # 3. Check raw Alloy text starting with a module declaration.
    if text.startswith("module core_domain"):
        updates["core_domain"] = text
    elif text.startswith("module behavior"):
        updates["behavior"] = text

    return updates


def _pick_violated_assertion(result: AlloyRunResult, behavior_als: str) -> str:
    combined = f"{result.stdout}\n{result.stderr}"
    match = _ASSERT_IN_OUTPUT_RE.search(combined)
    if match:
        return match.group(1)
    first_assert = _FIRST_ASSERT_RE.search(behavior_als)
    if first_assert:
        return first_assert.group(1)
    return "ModelAssertion"


class AlloyVerificationController:
    """Closed-loop Alloy verification gate: lint -> vacuity -> compile -> repair -> escalate."""

    def __init__(
        self,
        router: ProviderRouter,
        analyzer: AlloyAnalyzer,
        *,
        max_local_repairs: int = 2,
        max_scope: int = 4,
        work_dir: Path | None = None,
    ) -> None:
        if max_local_repairs < 0:
            raise ValueError("max_local_repairs must be >= 0")
        if max_scope < 1:
            raise ValueError("max_scope must be >= 1")
        self.router = router
        self.analyzer = analyzer
        self.max_local_repairs = max_local_repairs
        self.max_scope = max_scope
        self.work_dir = work_dir

    def _lint_blocks(
        self,
        core_als: str,
        behavior_als: str,
        compat: AlloyCompatMode,
    ) -> tuple[list[str], str]:
        """Run Layer 1 version-agnostic lint (and legacy Layer 2 lint when compat=legacy)."""
        core_errors = lint_core_domain_version_agnostic(core_als)
        if core_errors:
            return core_errors, "core_domain"
        if compat == AlloyCompatMode.LEGACY:
            legacy_errors = lint_core_domain_version_agnostic(behavior_als)
            if legacy_errors:
                return legacy_errors, "behavior"
        return [], "behavior"

    def _build_repair_request(
        self,
        *,
        target_module: str,
        module_code: str,
        error_summary: str,
        compat: AlloyCompatMode,
        attempt_label: str,
    ) -> LLMRequest:
        """Build a compact TIER_0-friendly Alloy repair prompt."""
        rules = (
            "Keep module core_domain 100% version-agnostic (NO var, ', always, eventually, after, until, once)."
            if target_module == "core_domain" or compat == AlloyCompatMode.LEGACY
            else f"Target Alloy mode={compat.value}; keep check scopes <= {self.max_scope} and bitwidth <= 4."
        )
        trimmed_error = error_summary.strip()[:800]
        prompt = (
            f"[ALLOY REPAIR {attempt_label}] Fix module `{target_module}`.\n"
            f"Rules: {rules} Preserve all `// @trace REQ-ID` comments.\n"
            f"Error:\n{trimmed_error}\n\n"
            f"Current `{target_module}.als`:\n```alloy module={target_module}\n{module_code.strip()}\n```\n"
            f"Return ONLY the repaired ```alloy module={target_module} ... ``` block."
        )
        return LLMRequest(
            prompt=prompt,
            system_prompt="You are a concise Alloy 6 formal specification repair assistant.",
            temperature=0.0,
            max_tokens=1024,
        )

    def _run_once(
        self,
        core_als: str,
        behavior_als: str,
        compat: AlloyCompatMode,
        run_dir: Path,
    ) -> tuple[AlloyRunResult, str, bool]:
        """Execute lint + vacuity injection + analyzer check.

        Returns `(run_result, failing_target_module, vacuity_checked)`.
        """
        lint_errors, lint_target = self._lint_blocks(core_als, behavior_als, compat)
        if lint_errors:
            return (
                AlloyRunResult(
                    status=AlloyRunStatus.COMPILE_ERROR,
                    command="static-lint",
                    exit_code=1,
                    stderr="\n".join(lint_errors),
                    vacuity_passed=False,
                ),
                lint_target,
                False,
            )

        augmented_behavior, _ = inject_vacuity_witnesses(
            behavior_als,
            max_scope=self.max_scope,
        )
        res = self.analyzer.run_check(
            core_als,
            augmented_behavior,
            work_dir=run_dir,
            max_scope=self.max_scope,
            check_vacuity=True,
        )
        target = "core_domain" if "core_domain" in (res.stderr + res.stdout) else "behavior"
        return res, target, True

    def verify_spec(
        self,
        spec_md: str,
        graph: RequirementGraph,
        *,
        compat: AlloyCompatMode = AlloyCompatMode.ALLOY6,
        event_log: SessionEventLog | None = None,
        session_id: str | None = None,
    ) -> AlloyVerificationOutcome:
        """Run the full closed-loop neuro-symbolic verification gate on `spec_md`."""
        compat_mode = AlloyCompatMode(compat)
        current_spec = spec_md
        blocks = extract_alloy_blocks(current_spec)
        if "core_domain" not in blocks or "behavior" not in blocks:
            raise ValueError(
                "Spec.md must contain both 'core_domain' and 'behavior' fenced alloy blocks"
            )

        core_als = blocks["core_domain"].code
        behavior_als = blocks["behavior"].code

        temp_dir_ctx = (
            tempfile.TemporaryDirectory(prefix="reqhq_alloy_")
            if self.work_dir is None
            else None
        )
        run_dir = Path(temp_dir_ctx.name) if temp_dir_ctx is not None else Path(self.work_dir)  # type: ignore[arg-type]

        repair_attempts = 0
        escalated = False
        cloud_verified = False
        vacuity_checked = False
        run_results: list[AlloyRunResult] = []

        try:
            last_result, target_module, ran_vacuity = self._run_once(
                core_als, behavior_als, compat_mode, run_dir
            )
            vacuity_checked = vacuity_checked or ran_vacuity
            run_results.append(last_result)

            while last_result.status == AlloyRunStatus.COMPILE_ERROR:
                if repair_attempts < self.max_local_repairs:
                    repair_attempts += 1
                    module_code = core_als if target_module == "core_domain" else behavior_als
                    req = self._build_repair_request(
                        target_module=target_module,
                        module_code=module_code,
                        error_summary=last_result.stderr or last_result.stdout or "Compile error",
                        compat=compat_mode,
                        attempt_label=f"local {repair_attempts}/{self.max_local_repairs}",
                    )
                    resp = self.router.route(req, bypass_cache=True)
                    updates = _extract_repaired_modules(resp.text, default_target=target_module)
                    if "core_domain" in updates:
                        core_als = updates["core_domain"]
                        current_spec = replace_alloy_block(current_spec, "core_domain", core_als)
                    if "behavior" in updates:
                        behavior_als = updates["behavior"]
                        current_spec = replace_alloy_block(current_spec, "behavior", behavior_als)

                    last_result, target_module, ran_vacuity = self._run_once(
                        core_als, behavior_als, compat_mode, run_dir
                    )
                    vacuity_checked = vacuity_checked or ran_vacuity
                    run_results.append(last_result)
                    continue

                if not escalated:
                    escalated = True
                    if event_log is not None and session_id is not None:
                        event_log.append_event(
                            session_id,
                            EventKind.ESCALATION_TRIGGERED,
                            payload={
                                "reason": EscalationReason.ALLOY_REPAIR_EXHAUSTED.value,
                                "local_repairs_attempted": repair_attempts,
                                "target_module": target_module,
                                "last_error": (last_result.stderr or last_result.stdout)[:400],
                            },
                        )

                    module_code = core_als if target_module == "core_domain" else behavior_als
                    req = self._build_repair_request(
                        target_module=target_module,
                        module_code=module_code,
                        error_summary=last_result.stderr or last_result.stdout or "Compile error",
                        compat=compat_mode,
                        attempt_label="cloud escalation",
                    )
                    resp = self.router.route(
                        req,
                        escalation_reason=EscalationReason.ALLOY_REPAIR_EXHAUSTED,
                        bypass_cache=True,
                    )
                    cloud_verified = bool(resp.cloud_verified)
                    updates = _extract_repaired_modules(resp.text, default_target=target_module)
                    if "core_domain" in updates:
                        core_als = updates["core_domain"]
                        current_spec = replace_alloy_block(current_spec, "core_domain", core_als)
                    if "behavior" in updates:
                        behavior_als = updates["behavior"]
                        current_spec = replace_alloy_block(current_spec, "behavior", behavior_als)

                    last_result, target_module, ran_vacuity = self._run_once(
                        core_als, behavior_als, compat_mode, run_dir
                    )
                    vacuity_checked = vacuity_checked or ran_vacuity
                    run_results.append(last_result)
                break
        finally:
            if temp_dir_ctx is not None:
                temp_dir_ctx.cleanup()

        # Re-extract blocks so @trace line maps reflect any repairs applied.
        final_blocks: dict[str, ExtractedAlloyBlock] = extract_alloy_blocks(current_spec)
        req_to_alloy_refs = build_req_to_alloy_refs(final_blocks)

        final_status = last_result.status
        if final_status == AlloyRunStatus.VERIFIED and not last_result.vacuity_passed:
            # Hard anti-vacuity gate: never allow VERIFIED without a passing vacuity witness.
            final_status = AlloyRunStatus.VACUOUS_UNSAT
            last_result = last_result.model_copy(
                update={"status": AlloyRunStatus.VACUOUS_UNSAT, "vacuity_passed": False}
            )
            run_results[-1] = last_result

        conflict_questions: list[ConflictQuestion] = []
        counterexample_prompts: list[CounterexamplePrompt] = []

        if final_status == AlloyRunStatus.VACUOUS_UNSAT:
            conflict_questions = map_unsat_to_conflicts(last_result, final_blocks, graph)
        elif final_status == AlloyRunStatus.COUNTEREXAMPLE_FOUND:
            assertion_name = _pick_violated_assertion(last_result, behavior_als)
            counterexample_prompts = [
                translate_counterexample(last_result, assertion_name, final_blocks)
            ]

        outcome = AlloyVerificationOutcome(
            status=final_status,
            compat_mode=compat_mode,
            spec_markdown=current_spec,
            core_domain_als=core_als,
            behavior_als=behavior_als,
            repair_attempts=repair_attempts,
            escalated=escalated,
            cloud_verified=cloud_verified,
            vacuity_checked=vacuity_checked,
            run_results=run_results,
            conflict_questions=conflict_questions,
            counterexample_prompts=counterexample_prompts,
            req_to_alloy_refs=req_to_alloy_refs,
        )

        if event_log is not None and session_id is not None:
            event_log.append_event(
                session_id,
                EventKind.VERIFICATION_RECORDED,
                payload={
                    "status": outcome.status.value,
                    "compat_mode": outcome.compat_mode.value,
                    "repair_attempts": outcome.repair_attempts,
                    "escalated": outcome.escalated,
                    "cloud_verified": outcome.cloud_verified,
                    "vacuity_checked": outcome.vacuity_checked,
                    "vacuity_passed": bool(last_result.vacuity_passed),
                    "conflict_count": len(outcome.conflict_questions),
                    "counterexample_count": len(outcome.counterexample_prompts),
                    "req_to_alloy_refs": outcome.req_to_alloy_refs,
                },
            )

        return outcome


__all__ = ["AlloyVerificationController"]
