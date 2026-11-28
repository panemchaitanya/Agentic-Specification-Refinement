"""Phase 4 smoke & Hypothesis PBT test verifying the Phase 1-4 Brownfield SystemContext seam.

Proves that a custom ``BrownfieldSystemContext`` subclass plugs cleanly into
``SystemContextBase`` and satisfies the runtime-checkable ``SystemContext``
Protocol (``src/reqhq/schemas/system_context.py``) for all four brownfield
artifact kinds (``architecture_md``, ``ast_summary``, ``alloy_model``,
``superseded_adr``) with zero modifications to Phase 1-4 core modules.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ConfigDict, Field, ValidationError

from reqhq.schemas.system_context import (
    BrownfieldArtifactRef,
    GreenfieldEmptyContext,
    SystemContext,
    SystemContextBase,
)
from reqhq.store.event_log import EventKind, SessionEventLog


class BrownfieldSystemContext(SystemContextBase):
    """Concrete brownfield context plugging into the Phase 1 SystemContext seam."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["brownfield"] = "brownfield"
    repo_name: str = Field(..., min_length=1)
    artifacts: list[BrownfieldArtifactRef] = Field(default_factory=list)

    @classmethod
    def from_directory(
        cls,
        repo_name: str,
        root: Path,
        artifact_specs: list[
            tuple[
                Literal["architecture_md", "ast_summary", "alloy_model", "superseded_adr"],
                str,
                str,
            ]
        ],
    ) -> BrownfieldSystemContext:
        """Load existing repo artifacts on disk and compute deterministic SHA-256 digests."""
        refs: list[BrownfieldArtifactRef] = []
        for kind, rel_path, summary in artifact_specs:
            full_path = root / rel_path
            digest = hashlib.sha256(full_path.read_bytes()).hexdigest()
            refs.append(
                BrownfieldArtifactRef(
                    artifact_kind=kind,
                    path=rel_path,
                    sha256=digest,
                    summary=summary,
                )
            )
        return cls(repo_name=repo_name, artifacts=refs)

    def is_empty(self) -> bool:
        """Return True only when no brownfield artifacts are attached."""
        return len(self.artifacts) == 0

    def to_prompt_context(self) -> str:
        """Render a deterministic prompt context block sorted by kind and path."""
        lines = [
            f"[SystemContext: brownfield | repo={self.repo_name.strip()} | artifacts={len(self.artifacts)}]"
        ]
        ordered = sorted(
            self.artifacts,
            key=lambda item: (item.artifact_kind, item.path, item.sha256 or "", item.summary),
        )
        for ref in ordered:
            digest_tag = ref.sha256[:12] if ref.sha256 else "unhashed"
            summary_part = f" — {ref.summary.strip()}" if ref.summary.strip() else ""
            lines.append(
                f"- ({ref.artifact_kind}) {ref.path} [sha256:{digest_tag}]{summary_part}"
            )
        return "\n".join(lines)

    def summary_metadata(self) -> dict[str, Any]:
        """Return deterministic JSON-serializable metadata safe for SessionEventLog."""
        by_kind: dict[str, int] = {}
        for ref in self.artifacts:
            by_kind[ref.artifact_kind] = by_kind.get(ref.artifact_kind, 0) + 1
        return {
            "mode": self.mode,
            "is_empty": self.is_empty(),
            "repo_name": self.repo_name,
            "brownfield_artifacts": len(self.artifacts),
            "artifact_counts_by_kind": dict(sorted(by_kind.items())),
            "phase2_hooks": list(self.brownfield_extension_points()),
        }


def test_brownfield_seam_plugs_cleanly_into_protocol_and_event_log(tmp_path: Path) -> None:
    """Custom BrownfieldSystemContext satisfies Protocol, loads all 4 kinds, and persists cleanly."""
    repo_root = tmp_path / "legacy_ledger_repo"
    (repo_root / "docs" / "adr").mkdir(parents=True)
    (repo_root / "models").mkdir(parents=True)

    (repo_root / "ARCHITECTURE.md").write_text("# Legacy Ledger Architecture\n", encoding="utf-8")
    (repo_root / "ast_summary.json").write_text('{"modules": ["ledger.py"]}\n', encoding="utf-8")
    (repo_root / "models" / "ledger.als").write_text("sig Account {}\n", encoding="utf-8")
    (repo_root / "docs" / "adr" / "0001-legacy-sync.md").write_text(
        "# 0001. Legacy Sync\nStatus: Superseded\n", encoding="utf-8"
    )

    ctx = BrownfieldSystemContext.from_directory(
        repo_name="legacy-ledger",
        root=repo_root,
        artifact_specs=[
            ("architecture_md", "ARCHITECTURE.md", "Core ledger component boundaries"),
            ("ast_summary", "ast_summary.json", "Public symbols and call graph"),
            ("alloy_model", "models/ledger.als", "Existing account balance invariants"),
            ("superseded_adr", "docs/adr/0001-legacy-sync.md", "Retired synchronous batch flow"),
        ],
    )

    assert isinstance(ctx, SystemContextBase)
    assert isinstance(ctx, SystemContext)
    assert ctx.mode == "brownfield"
    assert ctx.is_empty() is False
    assert set(ctx.brownfield_extension_points()) == {
        "architecture_md",
        "ast_summary",
        "legacy_alloy_models",
        "superseded_adrs",
    }

    prompt_a = ctx.to_prompt_context()
    prompt_b = ctx.to_prompt_context()
    assert prompt_a == prompt_b
    for kind in ("architecture_md", "ast_summary", "alloy_model", "superseded_adr"):
        assert f"({kind})" in prompt_a

    meta = ctx.summary_metadata()
    assert meta["mode"] == "brownfield"
    assert meta["is_empty"] is False
    assert meta["brownfield_artifacts"] == 4
    assert meta["artifact_counts_by_kind"] == {
        "alloy_model": 1,
        "architecture_md": 1,
        "ast_summary": 1,
        "superseded_adr": 1,
    }
    json.dumps(meta)  # Strictly JSON-serializable

    # Verify polymorphic coexistence with GreenfieldEmptyContext and SessionEventLog persistence.
    greenfield: SystemContext = GreenfieldEmptyContext(project_name="new-app")
    assert isinstance(greenfield, SystemContext)
    assert greenfield.is_empty() is True

    log = SessionEventLog(tmp_path / "sessions")
    log.create_session("brownfield-smoke-1", metadata={"system_context": meta})
    log.append_event(
        "brownfield-smoke-1",
        EventKind.SOURCE_INGESTED,
        payload={"prompt_context": prompt_a, "brownfield_artifacts": meta["brownfield_artifacts"]},
    )
    resumed = log.resume("brownfield-smoke-1")
    assert resumed["metadata"]["system_context"]["mode"] == "brownfield"
    assert resumed["metadata"]["system_context"]["brownfield_artifacts"] == 4


def test_brownfield_seam_rejects_extra_fields_and_invalid_hashes() -> None:
    """Strict ConfigDict(extra='forbid') and SHA-256 validation hold on brownfield extensions."""
    with pytest.raises(ValidationError):
        BrownfieldSystemContext(repo_name="repo", unexpected_field=123)  # type: ignore[call-arg]

    with pytest.raises(ValidationError):
        BrownfieldArtifactRef(
            artifact_kind="architecture_md",
            path="ARCHITECTURE.md",
            sha256="not-a-valid-sha256",
        )


_ARTIFACT_KIND_STRAT = st.sampled_from(
    ["architecture_md", "ast_summary", "alloy_model", "superseded_adr"]
)
_REL_PATH_STRAT = st.from_regex(r"[a-z][a-z0-9_/-]{1,30}\.(md|json|als)", fullmatch=True)
_HEX64_STRAT = st.text(alphabet="0123456789abcdef", min_size=64, max_size=64)
_SUMMARY_STRAT = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126),
    min_size=0,
    max_size=60,
)
_ARTIFACT_REF_STRAT = st.builds(
    BrownfieldArtifactRef,
    artifact_kind=_ARTIFACT_KIND_STRAT,
    path=_REL_PATH_STRAT,
    sha256=st.one_of(st.none(), _HEX64_STRAT),
    summary=_SUMMARY_STRAT,
)


@settings(derandomize=True, max_examples=50)
@given(
    repo_name=st.from_regex(r"[a-zA-Z][a-zA-Z0-9_-]{1,24}", fullmatch=True),
    artifacts=st.lists(_ARTIFACT_REF_STRAT, min_size=0, max_size=8),
)
def test_pbt_brownfield_context_determinism_and_metadata_invariants(
    repo_name: str,
    artifacts: list[BrownfieldArtifactRef],
) -> None:
    """PBT: BrownfieldSystemContext is order-invariant in prompt rendering and JSON-safe."""
    ctx_forward = BrownfieldSystemContext(repo_name=repo_name, artifacts=artifacts)
    ctx_reversed = BrownfieldSystemContext(repo_name=repo_name, artifacts=list(reversed(artifacts)))

    assert isinstance(ctx_forward, SystemContext)
    assert ctx_forward.is_empty() is (len(artifacts) == 0)

    # Prompt rendering must be deterministic and invariant to input list permutation.
    prompt_fwd = ctx_forward.to_prompt_context()
    prompt_rev = ctx_reversed.to_prompt_context()
    assert prompt_fwd == prompt_rev
    assert len(prompt_fwd.splitlines()) == 1 + len(artifacts)

    # Metadata counts must sum exactly to len(artifacts) and round-trip through JSON.
    meta = ctx_forward.summary_metadata()
    assert meta["mode"] == "brownfield"
    assert meta["is_empty"] is (len(artifacts) == 0)
    assert meta["brownfield_artifacts"] == len(artifacts)
    assert sum(meta["artifact_counts_by_kind"].values()) == len(artifacts)
    assert json.loads(json.dumps(meta)) == meta
