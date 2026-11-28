"""SQLite-backed free-tier quota ledger enforcing the 90% ceiling and 429 cooldowns."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field


class ProviderQuotaLimits(BaseModel):
    """Per-provider free-tier rate and token limits."""

    model_config = ConfigDict(extra="forbid")

    rpm: int = Field(gt=0, description="Requests per minute limit.")
    rpd: int = Field(gt=0, description="Requests per day limit.")
    tpm: int = Field(gt=0, description="Tokens per minute limit.")
    tpd: int = Field(gt=0, description="Tokens per day limit.")
    default_cooldown_seconds: float = Field(
        default=60.0,
        gt=0.0,
        description="Default cooldown duration after HTTP 429 when Retry-After is absent.",
    )


DEFAULT_PROVIDER_LIMITS: dict[str, ProviderQuotaLimits] = {
    "gemini": ProviderQuotaLimits(
        rpm=15,
        rpd=1_500,
        tpm=1_000_000,
        tpd=1_000_000,
        default_cooldown_seconds=60.0,
    ),
    "groq": ProviderQuotaLimits(
        rpm=30,
        rpd=14_400,
        tpm=6_000,
        tpd=500_000,
        default_cooldown_seconds=60.0,
    ),
}


class QuotaLedger:
    """Persistent SQLite ledger tracking RPM, RPD, TPM, TPD and 429 cooldowns."""

    def __init__(
        self,
        db_path: Path | str,
        ceiling_ratio: float = 0.90,
        limits: dict[str, ProviderQuotaLimits] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not (0.0 < ceiling_ratio <= 1.0):
            raise ValueError("ceiling_ratio must be in (0.0, 1.0]")
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.ceiling_ratio = float(ceiling_ratio)
        self.limits: dict[str, ProviderQuotaLimits] = dict(DEFAULT_PROVIDER_LIMITS)
        if limits:
            self.limits.update(limits)
        self._clock: Callable[[], float] = clock or time.time
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS usage_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        provider TEXT NOT NULL,
                        timestamp REAL NOT NULL,
                        prompt_tokens INTEGER NOT NULL,
                        completion_tokens INTEGER NOT NULL,
                        total_tokens INTEGER NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_usage_provider_ts
                    ON usage_events(provider, timestamp)
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS provider_cooldowns (
                        provider TEXT PRIMARY KEY,
                        cooldown_until REAL NOT NULL,
                        reason TEXT NOT NULL,
                        updated_at REAL NOT NULL
                    )
                    """
                )

    def set_limits(self, provider: str, limits: ProviderQuotaLimits) -> None:
        """Configure or override quota limits for a provider."""
        self.limits[provider] = limits

    def _window_usage(
        self,
        conn: sqlite3.Connection,
        provider: str,
        now: float,
    ) -> tuple[int, int, int, int]:
        minute_cutoff = now - 60.0
        day_cutoff = now - 86_400.0

        row_min = conn.execute(
            """
            SELECT COUNT(*) AS req_count, COALESCE(SUM(total_tokens), 0) AS tok_sum
            FROM usage_events
            WHERE provider = ? AND timestamp >= ?
            """,
            (provider, minute_cutoff),
        ).fetchone()
        row_day = conn.execute(
            """
            SELECT COUNT(*) AS req_count, COALESCE(SUM(total_tokens), 0) AS tok_sum
            FROM usage_events
            WHERE provider = ? AND timestamp >= ?
            """,
            (provider, day_cutoff),
        ).fetchone()

        return (
            int(row_min["req_count"]),
            int(row_day["req_count"]),
            int(row_min["tok_sum"]),
            int(row_day["tok_sum"]),
        )

    def _active_cooldown(
        self,
        conn: sqlite3.Connection,
        provider: str,
        now: float,
    ) -> tuple[bool, float, str]:
        row = conn.execute(
            "SELECT cooldown_until, reason FROM provider_cooldowns WHERE provider = ?",
            (provider,),
        ).fetchone()
        if row is None:
            return False, 0.0, ""
        remaining = float(row["cooldown_until"]) - now
        if remaining <= 0.0:
            return False, 0.0, ""
        return True, remaining, str(row["reason"])

    def _evaluate_availability(
        self,
        provider: str,
        limits: ProviderQuotaLimits,
        *,
        rpm_used: int,
        rpd_used: int,
        tpm_used: int,
        tpd_used: int,
        in_cooldown: bool,
        remaining: float,
        cd_reason: str,
        est_tokens: int,
    ) -> tuple[bool, str]:
        if in_cooldown:
            return (
                False,
                f"Provider '{provider}' in cooldown for {remaining:.1f}s ({cd_reason})",
            )

        rpm_cap = round(limits.rpm * self.ceiling_ratio, 6)
        rpd_cap = round(limits.rpd * self.ceiling_ratio, 6)
        tpm_cap = round(limits.tpm * self.ceiling_ratio, 6)
        tpd_cap = round(limits.tpd * self.ceiling_ratio, 6)

        if (rpm_used + 1) > rpm_cap:
            return (
                False,
                f"Provider '{provider}' RPM ceiling reached ({rpm_used}/{rpm_cap:.1f})",
            )
        if (rpd_used + 1) > rpd_cap:
            return (
                False,
                f"Provider '{provider}' RPD ceiling reached ({rpd_used}/{rpd_cap:.1f})",
            )
        if (tpm_used + est_tokens) > tpm_cap:
            return (
                False,
                f"Provider '{provider}' TPM ceiling reached "
                f"({tpm_used + est_tokens}/{tpm_cap:.1f})",
            )
        if (tpd_used + est_tokens) > tpd_cap:
            return (
                False,
                f"Provider '{provider}' TPD ceiling reached "
                f"({tpd_used + est_tokens}/{tpd_cap:.1f})",
            )

        return True, "ok"

    def can_consume(self, provider: str, est_tokens: int = 0) -> tuple[bool, str]:
        """Check whether a provider can accept a request within the ceiling and cooldown."""
        if est_tokens < 0:
            raise ValueError("est_tokens must be non-negative")
        limits = self.limits.get(provider)
        if limits is None:
            return False, f"No quota limits configured for provider '{provider}'"

        now = self._clock()
        with closing(self._connect()) as conn:
            in_cooldown, remaining, reason = self._active_cooldown(conn, provider, now)
            rpm_used, rpd_used, tpm_used, tpd_used = self._window_usage(
                conn, provider, now
            )

        return self._evaluate_availability(
            provider,
            limits,
            rpm_used=rpm_used,
            rpd_used=rpd_used,
            tpm_used=tpm_used,
            tpd_used=tpd_used,
            in_cooldown=in_cooldown,
            remaining=remaining,
            cd_reason=reason,
            est_tokens=est_tokens,
        )

    def select_available_provider(
        self,
        providers: Sequence[str] = ("gemini", "groq"),
        est_tokens: int = 0,
    ) -> tuple[str | None, dict[str, str]]:
        """Select the first available cloud provider in failover order (e.g. Gemini <-> Groq)."""
        if est_tokens < 0:
            raise ValueError("est_tokens must be non-negative")
        skipped: dict[str, str] = {}
        now = self._clock()
        with closing(self._connect()) as conn:
            for name in providers:
                limits = self.limits.get(name)
                if limits is None:
                    skipped[name] = f"No quota limits configured for provider '{name}'"
                    continue
                in_cooldown, remaining, cd_reason = self._active_cooldown(conn, name, now)
                rpm_used, rpd_used, tpm_used, tpd_used = self._window_usage(
                    conn, name, now
                )
                allowed, reason = self._evaluate_availability(
                    name,
                    limits,
                    rpm_used=rpm_used,
                    rpd_used=rpd_used,
                    tpm_used=tpm_used,
                    tpd_used=tpd_used,
                    in_cooldown=in_cooldown,
                    remaining=remaining,
                    cd_reason=cd_reason,
                    est_tokens=est_tokens,
                )
                if allowed:
                    return name, skipped
                skipped[name] = reason
        return None, skipped

    def record_usage(
        self,
        provider: str,
        prompt_tokens: int,
        completion_tokens: int = 0,
        total_tokens: int | None = None,
    ) -> None:
        """Record a completed cloud request's token consumption."""
        if prompt_tokens < 0 or completion_tokens < 0:
            raise ValueError("Token counts must be non-negative")
        total = (
            total_tokens
            if total_tokens is not None
            else (prompt_tokens + completion_tokens)
        )
        if total < 0:
            raise ValueError("total_tokens must be non-negative")

        now = self._clock()
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO usage_events
                        (provider, timestamp, prompt_tokens, completion_tokens, total_tokens)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (provider, now, int(prompt_tokens), int(completion_tokens), int(total)),
                )

    def record_rate_limit(
        self,
        provider: str,
        retry_after_seconds: float | None = None,
        reason: str = "HTTP 429 rate limit",
    ) -> float:
        """Record an HTTP 429 cooldown window for a provider and return cooldown_until."""
        limits = self.limits.get(provider)
        default_cd = limits.default_cooldown_seconds if limits is not None else 60.0
        duration = (
            float(retry_after_seconds)
            if retry_after_seconds is not None and retry_after_seconds > 0
            else default_cd
        )
        now = self._clock()
        cooldown_until = now + duration
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO provider_cooldowns (provider, cooldown_until, reason, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(provider) DO UPDATE SET
                        cooldown_until = excluded.cooldown_until,
                        reason = excluded.reason,
                        updated_at = excluded.updated_at
                    """,
                    (provider, cooldown_until, reason, now),
                )
        return cooldown_until

    def clear_cooldown(self, provider: str) -> None:
        """Clear any recorded cooldown for a provider."""
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    "DELETE FROM provider_cooldowns WHERE provider = ?",
                    (provider,),
                )

    def get_status(self, provider: str | None = None) -> dict[str, Any]:
        """Return current quota utilization, ceilings, and cooldown status."""
        now = self._clock()
        providers = [provider] if provider is not None else sorted(self.limits.keys())
        result: dict[str, Any] = {}

        with closing(self._connect()) as conn:
            for name in providers:
                limits = self.limits.get(name)
                if limits is None:
                    result[name] = {
                        "configured": False,
                        "available": False,
                        "reason": f"No quota limits configured for provider '{name}'",
                    }
                    continue
                rpm_used, rpd_used, tpm_used, tpd_used = self._window_usage(
                    conn, name, now
                )
                in_cooldown, remaining, cd_reason = self._active_cooldown(
                    conn, name, now
                )
                available, avail_reason = self._evaluate_availability(
                    name,
                    limits,
                    rpm_used=rpm_used,
                    rpd_used=rpd_used,
                    tpm_used=tpm_used,
                    tpd_used=tpd_used,
                    in_cooldown=in_cooldown,
                    remaining=remaining,
                    cd_reason=cd_reason,
                    est_tokens=0,
                )
                result[name] = {
                    "configured": True,
                    "ceiling_ratio": self.ceiling_ratio,
                    "rpm_used": rpm_used,
                    "rpm_limit": limits.rpm,
                    "rpm_ceiling": round(limits.rpm * self.ceiling_ratio, 6),
                    "rpd_used": rpd_used,
                    "rpd_limit": limits.rpd,
                    "rpd_ceiling": round(limits.rpd * self.ceiling_ratio, 6),
                    "tpm_used": tpm_used,
                    "tpm_limit": limits.tpm,
                    "tpm_ceiling": round(limits.tpm * self.ceiling_ratio, 6),
                    "tpd_used": tpd_used,
                    "tpd_limit": limits.tpd,
                    "tpd_ceiling": round(limits.tpd * self.ceiling_ratio, 6),
                    "cooldown_active": in_cooldown,
                    "cooldown_remaining_seconds": round(remaining, 2),
                    "cooldown_reason": cd_reason if in_cooldown else None,
                    "available": available,
                    "reason": avail_reason,
                }
        return result
