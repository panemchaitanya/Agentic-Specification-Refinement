"""Tiered local SLM model registry aligned to HardwareTier."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from reqhq.llm.hardware_profiler import HardwareTier


class ModelSpec(BaseModel):
    """Specification for a tier-assigned local Ollama / llama.cpp SLM."""

    model_config = ConfigDict(extra="forbid")

    tier: HardwareTier
    model_name: str = Field(..., min_length=1)
    quantization: str = "Q4_K_M"
    fallback_model: str | None = None
    min_vram_gb: float = Field(..., ge=0.0)
    max_vram_gb: float | None = Field(default=None, ge=0.0)
    context_window: int = Field(..., ge=1024)
    recommended_ram_gb: float = Field(..., ge=4.0)
    supports_structured_output: bool = True
    notes: str = ""


MODEL_TIERS: dict[HardwareTier, ModelSpec] = {
    HardwareTier.TIER_0: ModelSpec(
        tier=HardwareTier.TIER_0,
        model_name="qwen3:4b",
        quantization="Q4_K_M",
        fallback_model="phi4-mini",
        min_vram_gb=0.0,
        max_vram_gb=4.0,
        context_window=32768,
        recommended_ram_gb=8.0,
        supports_structured_output=True,
        notes="<= 4 GB VRAM or CPU-only Windows/Linux laptops; phi4-mini fallback.",
    ),
    HardwareTier.TIER_1: ModelSpec(
        tier=HardwareTier.TIER_1,
        model_name="qwen3:8b",
        quantization="Q4_K_M",
        fallback_model="qwen3:4b",
        min_vram_gb=5.0,
        max_vram_gb=8.0,
        context_window=32768,
        recommended_ram_gb=16.0,
        supports_structured_output=True,
        notes="5-8 GB VRAM mainstream laptops (e.g. Windows 11 i5 / 16 GB RAM / RTX 4060).",
    ),
    HardwareTier.TIER_2: ModelSpec(
        tier=HardwareTier.TIER_2,
        model_name="qwen3:14b",
        quantization="Q4_K_M",
        fallback_model="qwen3:8b",
        min_vram_gb=12.0,
        max_vram_gb=16.0,
        context_window=32768,
        recommended_ram_gb=24.0,
        supports_structured_output=True,
        notes="12-16 GB VRAM enthusiast GPUs or 16+ GB Apple Silicon.",
    ),
    HardwareTier.TIER_3: ModelSpec(
        tier=HardwareTier.TIER_3,
        model_name="gpt-oss:20b",
        quantization="Q4_K_M",
        fallback_model="qwen3:14b",
        min_vram_gb=24.0,
        max_vram_gb=None,
        context_window=65536,
        recommended_ram_gb=32.0,
        supports_structured_output=True,
        notes="24+ GB VRAM workstation GPUs.",
    ),
}


def get_model_for_tier(tier: HardwareTier | int) -> ModelSpec:
    """Return the ModelSpec configured for the given HardwareTier or integer (0..3)."""
    if isinstance(tier, bool):
        raise ValueError(f"Unsupported hardware tier: {tier!r}")
    try:
        resolved = tier if isinstance(tier, HardwareTier) else HardwareTier(int(tier))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Unsupported hardware tier: {tier!r}") from exc
    return MODEL_TIERS[resolved]


def list_model_tiers() -> list[ModelSpec]:
    """Return all ModelSpecs ordered from Tier 0 through Tier 3."""
    return [MODEL_TIERS[tier] for tier in sorted(MODEL_TIERS)]


__all__ = [
    "MODEL_TIERS",
    "ModelSpec",
    "get_model_for_tier",
    "list_model_tiers",
]
