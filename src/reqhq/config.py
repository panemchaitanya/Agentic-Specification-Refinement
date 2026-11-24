"""Configuration schema and environment loading for ReqHQ v2."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def _parse_bool(raw: str, env_name: str) -> bool:
    normalized = raw.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ValueError(f"Invalid boolean for {env_name}: {raw!r}")


class ReqHQConfig(BaseModel):
    """Strict runtime configuration for the ReqHQ v2 engine."""

    model_config = ConfigDict(extra="forbid")

    sessions_dir: Path = Path(".reqhq_sessions")
    adr_dir: Path = Path("docs/adr")
    strict_local: bool = False
    hardware_tier_override: int | None = Field(default=None, ge=0, le=3)
    ollama_base_url: str = "http://127.0.0.1:11434"
    quota_ceiling_ratio: float = Field(default=0.90, gt=0.0, le=1.0, allow_inf_nan=False)
    quota_db_path: Path | None = None
    cache_db_path: Path | None = None

    @field_validator("hardware_tier_override", mode="before")
    @classmethod
    def _reject_bool_tier(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("hardware_tier_override must be an integer 0..3, not a boolean")
        return value

    @field_validator("ollama_base_url")
    @classmethod
    def _validate_ollama_url(cls, value: str) -> str:
        cleaned = value.strip().rstrip("/")
        parsed = urlparse(cleaned)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("ollama_base_url must be a valid http:// or https:// URL with host")
        return cleaned

    def resolved_quota_db_path(self) -> Path:
        """Return explicit quota DB path or default under sessions_dir."""
        if self.quota_db_path is not None:
            return self.quota_db_path
        return self.sessions_dir / "quota_ledger.db"

    def resolved_cache_db_path(self) -> Path:
        """Return explicit prompt cache DB path or default under sessions_dir."""
        if self.cache_db_path is not None:
            return self.cache_db_path
        return self.sessions_dir / "prompt_cache.db"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ReqHQConfig:
        """Load ReqHQConfig from environment variables (defaults to os.environ)."""
        source = os.environ if env is None else env
        kwargs: dict[str, object] = {}

        if raw_sessions := source.get("REQHQ_SESSIONS_DIR", "").strip():
            kwargs["sessions_dir"] = Path(raw_sessions)
        if raw_adr := source.get("REQHQ_ADR_DIR", "").strip():
            kwargs["adr_dir"] = Path(raw_adr)
        if raw_strict := source.get("REQHQ_STRICT_LOCAL", "").strip():
            kwargs["strict_local"] = _parse_bool(raw_strict, "REQHQ_STRICT_LOCAL")
        if raw_tier := source.get("REQHQ_HARDWARE_TIER", "").strip():
            try:
                kwargs["hardware_tier_override"] = int(raw_tier)
            except ValueError as exc:
                raise ValueError(
                    f"Invalid integer for REQHQ_HARDWARE_TIER: {raw_tier!r}"
                ) from exc
        if raw_ollama := source.get("OLLAMA_BASE_URL", "").strip():
            kwargs["ollama_base_url"] = raw_ollama
        if raw_ratio := source.get("REQHQ_QUOTA_CEILING_RATIO", "").strip():
            try:
                kwargs["quota_ceiling_ratio"] = float(raw_ratio)
            except ValueError as exc:
                raise ValueError(
                    f"Invalid float for REQHQ_QUOTA_CEILING_RATIO: {raw_ratio!r}"
                ) from exc
        if raw_quota_db := source.get("REQHQ_QUOTA_DB_PATH", "").strip():
            kwargs["quota_db_path"] = Path(raw_quota_db)
        if raw_cache_db := source.get("REQHQ_CACHE_DB_PATH", "").strip():
            kwargs["cache_db_path"] = Path(raw_cache_db)

        return cls.model_validate(kwargs)


__all__ = ["ReqHQConfig"]
